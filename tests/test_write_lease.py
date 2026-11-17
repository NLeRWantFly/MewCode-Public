from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from mewcode.orchestration.write_lease import WriteLeaseError, WriteLeaseStore
from mewcode.permissions.sandbox import PathSandbox


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    sandbox = PathSandbox(str(root))
    # Keep this test fixture's authority narrow. PathSandbox normally also
    # allows the OS temp root, which is useful in production but not needed here.
    sandbox._allowed_roots = [root.resolve()]
    store = WriteLeaseStore(root / ".host-state" / "write-leases.json", sandbox=sandbox)
    (root / ".host-state").mkdir()
    return root, sandbox, store


def identity(owner="owner-a", run="run-a", conversation="conversation-a"):
    return {"owner_id": owner, "run_id": run, "conversation_id": conversation}


def acquire(store, path, *, who=None, now=1000, ttl_seconds=300):
    return store.acquire(**(who or identity()), exact_paths=[path],
                         now=now, ttl_seconds=ttl_seconds)


def test_exact_lease_conflict_reports_full_holder(project):
    root, _, store = project
    target = root / "shared.txt"
    first = acquire(store, target)

    assert first["acquired"] is True
    result = acquire(store, target, who=identity("owner-b", "run-b", "conversation-b"))
    assert result["acquired"] is False
    assert result["conflict"]["kind"] == "path_in_use"
    assert result["conflict"]["holder"] == {
        **identity(), "lease_id": first["lease"]["lease_id"], "expires_at": 1300.0,
    }

    check = store.check_write(target, **identity("owner-b", "run-b", "conversation-b"), now=1001)
    assert check["lease_conflict"] is True
    assert check["holder"]["owner_id"] == "owner-a"
    assert store.check_write(target, **identity(), now=1001)["lease_conflict"] is False


def test_directory_lease_covers_descendants_but_not_sibling(project):
    root, _, store = project
    protected = root / "protected"
    protected.mkdir()
    held = store.acquire(**identity(), directory_paths=[protected], now=1000)

    inside = store.check_write(protected / "new.txt", **identity("owner-b", "run-b", "conversation-b"), now=1001)
    outside = store.check_write(root / "sibling.txt", **identity("owner-b", "run-b", "conversation-b"), now=1001)
    assert held["acquired"] is True
    assert inside["lease_conflict"] is True
    assert outside["lease_conflict"] is False


def test_entry_leases_cover_only_named_entries_and_allow_missing_parents(project):
    root, _, store = project
    planned_directory = root / "new-output"
    planned_file = planned_directory / "nested" / "result.txt"
    held = store.acquire(**identity(), entry_paths=[planned_directory, planned_file], now=1000)
    other = identity("owner-b", "run-b", "conversation-b")

    assert held["acquired"] is True
    assert held["lease"]["entry_paths"] == sorted(
        [str(planned_directory.resolve()), str(planned_file.resolve())]
    )
    assert store.check_write(planned_directory, **other, now=1001)["lease_conflict"] is True
    assert store.check_write(planned_file, **other, now=1001)["lease_conflict"] is True
    assert store.check_write(planned_directory / "nested" / "sibling.txt",
                             **other, now=1001)["lease_conflict"] is False


def test_entry_leases_conflict_with_exact_and_directory_scopes(project):
    root, _, store = project
    exact_target = root / "existing.txt"
    exact_target.write_text("content", encoding="utf-8")
    exact_held = store.acquire(**identity(), entry_paths=[exact_target], now=1000)
    exact_conflict = store.acquire(**identity("owner-b", "run-b", "conversation-b"),
                                   exact_paths=[exact_target], now=1001)
    assert exact_held["acquired"] is True
    assert exact_conflict["acquired"] is False
    assert exact_conflict["conflict"]["kind"] == "path_in_use"

    protected = root / "protected"
    protected.mkdir()
    subtree_held = store.acquire(**identity("owner-c", "run-c", "conversation-c"),
                                 directory_paths=[protected], now=1002)
    entry_conflict = store.acquire(**identity("owner-d", "run-d", "conversation-d"),
                                   entry_paths=[protected / "missing" / "leaf.txt"], now=1003)
    assert subtree_held["acquired"] is True
    assert entry_conflict["acquired"] is False
    assert entry_conflict["conflict"]["kind"] == "path_in_use"


def test_entry_scope_rejects_duplicate_and_overlapping_scopes(project):
    root, _, store = project
    planned = root / "planned"
    with pytest.raises(WriteLeaseError, match="requested_scopes_overlap"):
        store.acquire(**identity(), entry_paths=[planned, planned], now=1000)
    with pytest.raises(WriteLeaseError, match="requested_scopes_overlap"):
        store.acquire(**identity(), exact_paths=[planned], entry_paths=[planned], now=1000)

    protected = root / "protected"
    protected.mkdir()
    with pytest.raises(WriteLeaseError, match="requested_scopes_overlap"):
        store.acquire(**identity(), directory_paths=[protected],
                      entry_paths=[protected / "child.txt"], now=1000)


def test_entry_scope_counts_toward_combined_scope_limit(project):
    root, _, store = project
    exact = [root / f"planned-{index}.txt" for index in range(256)]
    with pytest.raises(WriteLeaseError, match="lease_scope_empty_or_too_large"):
        store.acquire(**identity(), exact_paths=exact, entry_paths=[root / "one-more"], now=1000)


def test_entry_scope_rejects_volume_home_project_and_allowed_roots(project, tmp_path):
    root, sandbox, store = project
    extra_root = tmp_path / "extra-allowed"
    extra_root.mkdir()
    sandbox._allowed_roots.append(extra_root.resolve())

    for target in (Path(root.anchor), Path.home(), root, extra_root):
        with pytest.raises(WriteLeaseError, match="broad_root_forbidden"):
            store.acquire(**identity(), entry_paths=[target], now=1000)


def test_expired_deleted_entry_directory_can_be_renewed_by_its_owner(project):
    root, _, store = project
    target = root / "planned-directory"
    target.mkdir()
    held = store.acquire(**identity(), entry_paths=[target], now=1000, ttl_seconds=10)
    target.rmdir()

    with pytest.raises(WriteLeaseError, match="recovery_lease_owner_mismatch"):
        store.renew_owned_for_recovery(held["lease"]["lease_id"],
            **identity("owner-b", "run-b", "conversation-b"), ttl_seconds=60)

    renewed = store.renew_owned_for_recovery(held["lease"]["lease_id"], **identity(), ttl_seconds=60)
    assert renewed["entry_paths"] == [str(target)]
    assert renewed["expires_at"] > held["lease"]["expires_at"]
    assert store.snapshot()["leases"] == [renewed]


def test_old_stored_lease_without_entry_paths_remains_readable_and_renewable(project):
    root, sandbox, store = project
    target = root / "legacy.txt"
    acquired = acquire(store, target)
    state_path = root / ".host-state" / "write-leases.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["leases"][0].pop("entry_paths")
    state_path.write_text(json.dumps(state), encoding="utf-8")

    restarted = WriteLeaseStore(root / ".host-state" / "write-leases.json", sandbox=sandbox)
    snapshot = restarted.snapshot(now=1001)
    assert snapshot["leases"][0]["exact_paths"] == acquired["lease"]["exact_paths"]
    renewed = restarted.acquire(**identity(), exact_paths=[target], now=1001)
    assert renewed["acquired"] is True and renewed["renewed"] is True


def test_non_overlapping_scopes_can_be_leased_concurrently(project):
    root, _, store = project
    left = acquire(store, root / "left.txt", who=identity("owner-a", "run-a", "conv-a"))
    right = acquire(store, root / "right.txt", who=identity("owner-b", "run-b", "conv-b"))
    assert left["acquired"] is True
    assert right["acquired"] is True
    assert len(store.snapshot(now=1001)["leases"]) == 2


def test_compare_and_swap_rejects_stale_revision_without_mutation(project):
    root, _, store = project
    first = acquire(store, root / "one.txt")
    before = store.snapshot(now=1001)

    stale = store.acquire(**identity("owner-b", "run-b", "conversation-b"),
                          exact_paths=[root / "two.txt"],
                          expected_revision=first["revision"] - 1, now=1001)
    after = store.snapshot(now=1001)
    assert stale["acquired"] is False
    assert stale["conflict"]["kind"] == "revision_mismatch"
    assert stale["conflict"]["actual_revision"] == first["revision"]
    assert after == before


def test_new_store_instance_reloads_durable_lease(project):
    root, sandbox, store = project
    acquired = acquire(store, root / "restart.txt")
    restarted = WriteLeaseStore(root / ".host-state" / "write-leases.json", sandbox=sandbox)

    state = restarted.snapshot(now=1001)
    assert state["revision"] == acquired["revision"]
    assert state["leases"] == [acquired["lease"]]


def test_expired_lease_is_reclaimed(project):
    root, _, store = project
    first = acquire(store, root / "expiring.txt", now=1000, ttl_seconds=10)
    assert first["acquired"] is True
    assert store.snapshot(now=1010)["leases"] == []

    second = acquire(store, root / "expiring.txt",
                     who=identity("owner-b", "run-b", "conversation-b"), now=1011)
    assert second["acquired"] is True
    assert second["lease"]["holder"]["owner_id"] == "owner-b"


def test_release_requires_identity_and_is_explicit(project):
    root, _, store = project
    acquired = acquire(store, root / "release.txt")

    refused = store.release(acquired["lease"]["lease_id"],
                            **identity("owner-b", "run-b", "conversation-b"), now=1001)
    assert refused["released"] is False
    assert refused["conflict"]["holder"]["owner_id"] == "owner-a"
    assert len(store.snapshot(now=1001)["leases"]) == 1

    released = store.release(acquired["lease"]["lease_id"], **identity(), now=1002)
    assert released["released"] is True
    assert released["revision"] == acquired["revision"] + 1
    assert store.snapshot(now=1003)["leases"] == []


def test_same_path_race_has_single_winner_across_store_instances(project):
    root, sandbox, first_store = project
    second_store = WriteLeaseStore(root / ".host-state" / "write-leases.json", sandbox=sandbox)
    target = root / "race.txt"
    barrier = Barrier(2)

    def attempt(store, owner):
        barrier.wait(timeout=5)
        return acquire(store, target, who=identity(owner, f"run-{owner}", f"conv-{owner}"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda pair: attempt(*pair),
                                [(first_store, "owner-a"), (second_store, "owner-b")]))
    winners = [result for result in results if result["acquired"]]
    losers = [result for result in results if not result["acquired"]]
    assert len(winners) == 1
    assert len(losers) == 1
    assert losers[0]["conflict"]["holder"]["owner_id"] == winners[0]["lease"]["holder"]["owner_id"]


def test_symlink_paths_and_broad_directory_root_are_rejected(project, tmp_path):
    root, _, store = project
    external = tmp_path / "external"
    external.mkdir()
    link = root / "linked"
    try:
        link.symlink_to(external, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this host")

    with pytest.raises(WriteLeaseError, match="symlink_or_reparse_path"):
        acquire(store, link / "escape.txt")
    with pytest.raises(WriteLeaseError, match="broad_root_forbidden"):
        store.acquire(**identity(), directory_paths=[root], now=1000)


def test_failed_scope_request_never_expands_path_sandbox(project, tmp_path):
    root, sandbox, store = project
    outside = tmp_path / "outside.txt"
    before_files = dict(sandbox.file_grants)
    before_dirs = dict(sandbox.dir_grants)

    with pytest.raises(WriteLeaseError, match="path_not_writable_in_sandbox"):
        acquire(store, outside)

    assert sandbox.file_grants == before_files
    assert sandbox.dir_grants == before_dirs
    assert root.exists()


def test_corrupt_durable_state_fails_closed(project):
    root, _, store = project
    state_path = root / ".host-state" / "write-leases.json"
    state_path.write_text("not-json", encoding="utf-8")

    with pytest.raises(WriteLeaseError, match="lease_state_unreadable"):
        store.snapshot()


def test_conflicting_persisted_leases_fail_closed(project):
    root, _, store = project
    acquired = acquire(store, root / "state-conflict.txt")
    state_path = root / ".host-state" / "write-leases.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    duplicate = dict(acquired["lease"])
    duplicate["lease_id"] = "forged-conflicting-lease"
    duplicate["holder"] = identity("owner-b", "run-b", "conversation-b")
    state["leases"].append(duplicate)
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(WriteLeaseError, match="lease_state_conflict"):
        store.snapshot()
