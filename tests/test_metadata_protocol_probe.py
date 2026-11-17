"""Offline regressions for the metadata probe's synthetic parent-loss check."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from scripts import probe_metadata_protocol as probe


class _Function:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


class _FakeParentProcess:
    """Represents the Popen launcher, not the process that owns the pipe."""

    def __init__(self, launcher_pid: int, owner_pid: int, pipe: int, on_trigger):
        self.pid = launcher_pid
        self.stdin_writes = []
        self.on_trigger = on_trigger
        self.stdout = SimpleNamespace(
            readline=lambda: json.dumps(
                {"pipe": pipe, "pid": 8765, "owner_pid": owner_pid}
            )
            + "\n"
        )
        self.stdin = SimpleNamespace(write=self._write, flush=lambda: None)
        self.wait_timeouts = []

    def _write(self, value):
        self.stdin_writes.append(value)
        self.on_trigger()
        return len(value)

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        return 0

    def poll(self):
        return 0

    def terminate(self):
        raise AssertionError("successful fake parent should already have exited")


class _FakeWorkerProcess:
    def __init__(self, pid: int):
        self.pid = pid
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


class _FakeKernel:
    def __init__(self, owner_pid: int, helper_pid: int, *, owner_exits: bool,
                 helper_exits_before_trigger: bool):
        self.owner_handle = object()
        self.helper_handle = object()
        self.retained_handle = None
        self.owner_exits = owner_exits
        self.helper_exits_before_trigger = helper_exits_before_trigger
        self.triggered = False
        self.opened = []
        self.duplicated = []
        self.waited = []
        self.closed = []
        self.pid_handles = {
            owner_pid: self.owner_handle,
            helper_pid: self.helper_handle,
        }
        self.OpenProcess = _Function(self._open_process)
        self.DuplicateHandle = _Function(self._duplicate_handle)
        self.WaitForSingleObject = _Function(self._wait_for_single_object)
        self.CloseHandle = _Function(self.closed.append)

    def _open_process(self, access, inherit, pid):
        self.opened.append((access, inherit, pid))
        return self.pid_handles.get(pid)

    def _duplicate_handle(self, source_process, source_handle, target_process,
                          target_handle, access, inherit, options):
        self.duplicated.append((source_process, source_handle, target_process,
                                access, inherit, options))
        probe.C.cast(target_handle, probe.C.POINTER(probe.W.HANDLE))[0] = probe.W.HANDLE(0x333)
        self.retained_handle = probe.C.cast(
            target_handle, probe.C.POINTER(probe.W.HANDLE)
        )[0]
        return True

    def _wait_for_single_object(self, handle, timeout):
        self.waited.append((handle, timeout))
        if handle is self.owner_handle:
            if timeout == 0:
                return 258  # Owner must still be alive before synthetic loss.
            return 0 if self.owner_exits else 258  # WAIT_TIMEOUT after trigger.
        if handle is self.helper_handle:
            if timeout == 0:
                return 0 if self.helper_exits_before_trigger else 258
            return 0 if self.triggered else 258
        raise AssertionError("wait must target the pinned owner and helper processes")


def _run_quick_probe(monkeypatch, tmp_path: Path, *, owner_exits: bool,
                     helper_exits_before_trigger: bool = False):
    bundle = tmp_path / "synthetic-bundle"
    bundle.mkdir()
    fixture = tmp_path / "synthetic-fixture"
    launcher_pid, owner_pid, helper_pid, pipe_handle = 1234, 5678, 8765, 0xABC
    worker = _FakeWorkerProcess(2222)
    kernel = _FakeKernel(
        owner_pid, helper_pid, owner_exits=owner_exits,
        helper_exits_before_trigger=helper_exits_before_trigger,
    )
    parent = _FakeParentProcess(
        launcher_pid, owner_pid, pipe_handle,
        on_trigger=lambda: setattr(kernel, "triggered", True),
    )
    popen_calls = []

    def fake_popen(command, **kwargs):
        popen_calls.append((command, kwargs))
        if "-c" in command:
            return worker
        assert "--mode" in command and "orphan-child" in command
        return parent

    monkeypatch.setattr(probe, "verify_bundle", lambda path: {"ok": True})
    monkeypatch.setattr(probe.mb, "installed_artifact", lambda path: (path, "a" * 64))
    monkeypatch.setattr(probe, "installed_bundle_identity", lambda path: "synthetic-bundle-id")
    monkeypatch.setattr(probe, "machine_guid", lambda: "synthetic-machine-id")
    monkeypatch.setattr(probe, "bounded_directory", lambda path, must_exist: Path(path))
    monkeypatch.setattr(probe.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        probe.subprocess,
        "CREATE_NO_WINDOW",
        getattr(probe.subprocess, "CREATE_NO_WINDOW", 0),
        raising=False,
    )
    monkeypatch.setattr(probe.mb, "_kernel", lambda: kernel)

    class FakeBroker:
        def __init__(self, *artifact):
            self.artifact = artifact
            self.nonce = "synthetic-nonce"

        def request(self, operation):
            raise probe.mb.BrokerError("connection failed: synthetic PID mismatch")

        def close(self):
            pass

    monkeypatch.setattr(probe.mb, "Broker", FakeBroker)
    monkeypatch.setattr(probe, "ping", lambda client: None)
    monkeypatch.setattr(probe, "send", lambda client, payload: None)
    answers = iter((
        {"ok": False, "error": "nonce mismatch"},
        {"ok": False, "error": "Duplicate field"},
    ))
    monkeypatch.setattr(probe, "response", lambda client, body: next(answers))
    exit_codes = iter((0, 124))
    monkeypatch.setattr(probe, "exited", lambda client, **kwargs: next(exit_codes))
    ticks = iter((100.0, 115.0))
    monkeypatch.setattr(probe, "time", SimpleNamespace(monotonic=lambda: next(ticks)))

    error = None
    result = None
    try:
        result = probe.main(SimpleNamespace(bundle=bundle, fixture=fixture, mode="quick"))
    except BaseException as caught:
        error = caught
    report = json.loads((fixture / "report.json").read_text(encoding="utf-8"))
    return (result, report, parent, worker, kernel, popen_calls,
            (launcher_pid, owner_pid, helper_pid, pipe_handle), error)


def test_parent_exit_check_uses_pipe_owner_not_venv_launcher(monkeypatch, tmp_path):
    result, report, parent, worker, kernel, popen_calls, ids, error = _run_quick_probe(
        monkeypatch, tmp_path, owner_exits=True
    )
    launcher_pid, owner_pid, helper_pid, pipe_handle = ids

    assert result == 0
    assert error is None
    assert launcher_pid != owner_pid
    assert report["complete"] is True
    assert any(case["case"] == "parent_exit_enforced_with_pipe_retained" and case["ok"] is True
               for case in report["cases"])
    assert [(pid, access) for access, _inherit, pid in kernel.opened] == [
        (owner_pid, 0x40 | 0x100000),
        (helper_pid, 0x100000),
    ]
    assert len(kernel.duplicated) == 1
    duplicate = kernel.duplicated[0]
    assert duplicate[0] is kernel.owner_handle
    assert duplicate[1] == pipe_handle
    assert duplicate[3:] == (0, False, 2)
    assert kernel.waited == [
        (kernel.owner_handle, 0),
        (kernel.helper_handle, 0),
        (kernel.owner_handle, 8000),
        (kernel.helper_handle, 8000),
    ]
    assert parent.wait_timeouts == [10]
    assert parent.stdin_writes == ["exit-synthetic-parent\n"]
    assert worker.terminated is True
    assert len(popen_calls) == 2


def test_failed_owner_termination_never_records_successful_report(monkeypatch, tmp_path):
    _result, report, parent, _worker, kernel, _popen_calls, _ids, error = _run_quick_probe(
        monkeypatch, tmp_path, owner_exits=False
    )
    assert isinstance(error, AssertionError)
    assert parent.stdin_writes == ["exit-synthetic-parent\n"]
    assert kernel.waited == [
        (kernel.owner_handle, 0),
        (kernel.helper_handle, 0),
        (kernel.owner_handle, 8000),
    ]
    assert report["complete"] is False
    assert "error" in report
    assert not any(case["case"] == "parent_exit_enforced_with_pipe_retained"
                   for case in report["cases"])


def test_helper_dead_before_parent_loss_trigger_never_records_success(monkeypatch, tmp_path):
    _result, report, parent, _worker, kernel, _popen_calls, _ids, error = _run_quick_probe(
        monkeypatch, tmp_path, owner_exits=True, helper_exits_before_trigger=True
    )

    assert isinstance(error, AssertionError)
    assert str(error) == "broker exited before parent-loss trigger"
    assert kernel.waited == [
        (kernel.owner_handle, 0),
        (kernel.helper_handle, 0),
    ]
    assert parent.stdin_writes == []
    assert parent.wait_timeouts == []
    assert report["complete"] is False
    assert "error" in report
    assert not any(case["case"] == "parent_exit_enforced_with_pipe_retained"
                   for case in report["cases"])
