"""Resource ownership tests; no actual drive mapping or ACL mutation."""
import ctypes
from pathlib import Path

import pytest

from scripts import native_probe_resources as resources


class FakeKernel:
    def __init__(self):
        self.mappings = {"Z:": [r"\Device\AlreadyOwned"]}
        self.defines = []
        self.closed = []
        self.signalled = []
        self.last_error = 0

    def QueryDosDeviceW(self, drive, buffer, size):
        values = self.mappings.get(drive, [])
        if not values:
            self.last_error = 2
            return 0
        value = "\0".join(values) + "\0\0"
        for index, char in enumerate(value):
            buffer[index] = char
        self.last_error = 0
        return len(value)

    def DefineDosDeviceW(self, flags, drive, target):
        self.defines.append((flags, drive, target))
        if flags & 2:
            assert flags == 15  # Exact target is mandatory on every removal.
            self.mappings[drive].remove(target)
        else:
            assert flags == 9
            self.mappings.setdefault(drive, []).insert(0, target)
        return 1

    def CreateEventW(self, security, manual, initial, name):
        assert security is None and manual and not initial
        assert name.startswith("MewCodeProbeCancel_") and len(name) == 51
        return 42

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1

    def SetEvent(self, handle):
        self.signalled.append(handle)
        return 1


@pytest.fixture
def api(monkeypatch):
    fake = FakeKernel()
    monkeypatch.setattr(resources, "_kernel", lambda: fake)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: fake.last_error, raising=False)
    return fake


def make_alias(api):
    # Validation is separately exercised below; do not create a real OS alias.
    alias = object.__new__(resources.ProbeDriveAlias)
    alias.target = r"\??\C:\fixture\workspace"
    alias._api = api
    alias.drive = None
    alias.cleanup_ok = False
    return alias


def test_alias_skips_existing_mapping_and_removes_only_owned_target(api):
    alias = make_alias(api)
    with alias:
        assert alias.drive == "Y:"
        # A concurrent mapping must remain after our exact-match removal.
        api.mappings["Y:"].insert(0, r"\Device\OtherTask")
    assert alias.cleanup_ok
    assert api.mappings["Y:"] == [r"\Device\OtherTask"]
    assert api.mappings["Z:"] == [r"\Device\AlreadyOwned"]
    assert api.defines == [(9, "Y:", alias.target), (15, "Y:", alias.target)]
    alias.close()
    assert len(api.defines) == 2


def test_alias_cleanup_on_body_exception(api):
    alias = make_alias(api)
    with pytest.raises(ValueError):
        with alias:
            raise ValueError("probe failed")
    assert alias.cleanup_ok


def test_alias_never_overwrites_when_no_free_drive(api):
    api.mappings = {letter + ":": [r"\Device\Taken"] for letter in "GHIJKLMNOPQRSTUVWXYZ"}
    with pytest.raises(RuntimeError, match="No unused"):
        make_alias(api).__enter__()
    assert not api.defines


def test_alias_rejects_nonfixture_before_kernel_access(tmp_path, monkeypatch):
    monkeypatch.setattr(resources, "_kernel", lambda: pytest.fail("must validate before Win32"))
    with pytest.raises(ValueError):
        resources.ProbeDriveAlias(tmp_path)


def test_cancel_event_is_owned_and_closed_once(api):
    with resources.ProbeCancelEvent() as event:
        event.signal()
    assert api.signalled == [42]
    assert api.closed == [42]
    event.close()
    assert api.closed == [42]
    with pytest.raises(RuntimeError, match="closed"):
        event.signal()


def test_existing_event_is_not_reused(api):
    api.last_error = 183
    with pytest.raises(RuntimeError, match="already exists"):
        resources.ProbeCancelEvent()
    assert api.closed == [42]
    assert not api.signalled
