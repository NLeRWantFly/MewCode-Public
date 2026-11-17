"""Compare-and-swap behavior for the shared host network policy file."""
from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import os
from pathlib import Path
import stat
import threading

import pytest

from mewcode.permissions.network_policy import NetworkPolicyStore


def _competing_process(path, expected_revision, domain, start, results):
    store = NetworkPolicyStore(Path(path))
    start.wait(15)
    try:
        store.save({"enabled": True, "denied_domains": [domain]},
                   expected_revision=expected_revision)
    except ValueError as exc:
        results.put(("conflict", domain, str(exc)))
    except Exception as exc:  # Report child failures in the parent test process.
        results.put(("error", domain, type(exc).__name__, str(exc)))
    else:
        results.put(("saved", domain))


def _thread_attempt(store, expected_revision, domain, start):
    start.wait(timeout=10)
    try:
        store.save({"enabled": True, "denied_domains": [domain]},
                   expected_revision=expected_revision)
    except ValueError:
        return "conflict", domain
    return "saved", domain


def test_independent_store_instances_compare_and_swap_under_thread_race(tmp_path):
    path = tmp_path / "sandbox-network.json"
    stores = [NetworkPolicyStore(path), NetworkPolicyStore(path)]
    expected_revision = stores[0].read().revision
    start = threading.Barrier(3)
    domains = ["first.example", "second.example"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_thread_attempt, store, expected_revision, domain, start)
                   for store, domain in zip(stores, domains)]
        start.wait(timeout=10)
        results = [future.result(timeout=10) for future in futures]

    winners = [domain for state, domain in results if state == "saved"]
    assert len(winners) == 1, results
    assert sum(state == "conflict" for state, _ in results) == 1, results
    assert stores[0].read().denied_domains == (winners[0],)


def test_multiprocess_competitors_with_same_revision_keep_one_winner(tmp_path):
    path = tmp_path / "sandbox-network.json"
    expected_revision = NetworkPolicyStore(path).read().revision
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    domains = ["process-a.example", "process-b.example"]
    processes = [
        context.Process(target=_competing_process,
                        args=(str(path), expected_revision, domain, start, results))
        for domain in domains
    ]

    try:
        for process in processes:
            process.start()
        start.set()
        outcomes = [results.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=20)
        assert [process.exitcode for process in processes] == [0, 0]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        results.close()
        results.join_thread()

    assert all(outcome[0] != "error" for outcome in outcomes), outcomes
    winners = [outcome[1] for outcome in outcomes if outcome[0] == "saved"]
    assert len(winners) == 1, outcomes
    assert sum(outcome[0] == "conflict" for outcome in outcomes) == 1, outcomes
    assert NetworkPolicyStore(path).read().denied_domains == (winners[0],)

    lock_path = Path(str(path) + ".lock")
    assert lock_path.is_file()
    if os.name == "posix":
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


def test_write_failure_releases_lock_for_later_save(tmp_path, monkeypatch):
    path = tmp_path / "sandbox-network.json"
    first = NetworkPolicyStore(path)
    expected_revision = first.read().revision
    original_write_text = Path.write_text

    def fail_temp_write(target, *args, **kwargs):
        if target.name.endswith(".tmp"):
            raise OSError("simulated policy write failure")
        return original_write_text(target, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "write_text", fail_temp_write)
        with pytest.raises(OSError, match="simulated policy write failure"):
            first.save({"enabled": False, "denied_domains": []},
                       expected_revision=expected_revision)

    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    process = context.Process(target=_competing_process,
                              args=(str(path), expected_revision, "after-error.example", start, results))
    try:
        process.start()
        start.set()
        outcome = results.get(timeout=20)
        process.join(timeout=20)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        results.close()
        results.join_thread()

    assert outcome == ("saved", "after-error.example"), outcome
    saved = NetworkPolicyStore(path).read()
    assert saved.denied_domains == ("after-error.example",)
    assert not list(tmp_path.glob("sandbox-network.json.*.tmp"))
