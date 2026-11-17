"""Regressions for the 2026-09-24 desktop-provider incident.

An external script rewrote ``providers.json`` with a UTF-8 BOM. The desktop shell
(silently) and this backend (as a warning) both fell back to "no providers", and
the next save persisted that empty list over a file that still held all eight
profiles. These tests pin the fail-closed behaviour:

* a BOM is tolerated;
* a snapshot that exists but cannot be used is reported, never read as empty;
* an empty provider sync is refused while the snapshot is unusable.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from mewcode import desktop_providers
from mewcode.desktop_providers import (
    EncryptedSnapshot,
    bootstrap_desktop_providers,
    clobber_block_reason,
    read_snapshot,
)

PROFILES = [
    {"provider_key": "desktop:example_provider_7", "protocol": "typesafe", "api_key": "k"},
    {"provider_key": "desktop:example_provider_6", "protocol": "openai-compat", "api_key": "k2"},
]


class FakeRegistry:
    def __init__(self) -> None:
        self.replaced: list | None = None

    def replace_desktop_profiles(self, payloads):
        self.replaced = list(payloads)
        return payloads


@pytest.fixture(autouse=True)
def _reset_load_state():
    desktop_providers._record_failure("")
    yield
    desktop_providers._record_failure("")


@pytest.fixture
def tmp_path():
    """Workspace-local scratch directory.

    The session sandbox denies directory creation under the OS temp root, so
    pytest's own ``tmp_path`` cannot be used here.
    """
    root = Path(__file__).with_name("_desktop_providers_scratch")
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _write(tmp_path, name="providers.json", *, text=None, data=None):
    path = tmp_path / name
    if text is not None:
        path.write_bytes(text.encode("utf-8"))
    else:
        path.write_bytes(data)
    return path


def test_bominated_snapshot_is_still_restored(tmp_path):
    """PowerShell 5.1 `Set-Content -Encoding utf8` prepends EF BB BF."""
    raw = b"\xef\xbb\xbf" + json.dumps(PROFILES).encode("utf-8")
    _write(tmp_path, data=raw)
    registry = FakeRegistry()

    restored = bootstrap_desktop_providers(registry, tmp_path, desktop_authenticated=True)

    assert restored == 2
    assert registry.replaced == PROFILES


def test_truncated_snapshot_is_reported_and_not_treated_as_empty(tmp_path):
    _write(tmp_path, text='[{"provider_key": "desktop:x"')
    registry = FakeRegistry()

    restored = bootstrap_desktop_providers(registry, tmp_path, desktop_authenticated=True)

    assert restored == 0
    assert registry.replaced is None, "an unusable snapshot must not publish an empty list"
    assert "invalid JSON" in desktop_providers.last_load_failure()


def test_missing_snapshot_is_not_an_incident(tmp_path):
    registry = FakeRegistry()
    assert bootstrap_desktop_providers(registry, tmp_path, desktop_authenticated=True) == 0
    assert desktop_providers.last_load_failure() == ""
    assert clobber_block_reason(tmp_path) == ""


def test_non_list_snapshot_is_rejected(tmp_path):
    _write(tmp_path, text=json.dumps({"provider_key": "desktop:x"}))
    with pytest.raises(ValueError, match="expected a list"):
        read_snapshot(tmp_path / "providers.json")


def test_unstable_identity_and_duplicates_are_rejected(tmp_path):
    _write(tmp_path, text=json.dumps([{"provider_key": "other:x"}]))
    with pytest.raises(ValueError, match="unstable provider identity"):
        read_snapshot(tmp_path / "providers.json")

    _write(tmp_path, text=json.dumps([PROFILES[0], PROFILES[0]]))
    with pytest.raises(ValueError, match="duplicate provider identity"):
        read_snapshot(tmp_path / "providers.json")


def test_encrypted_snapshot_is_skipped_without_alarm(tmp_path):
    _write(tmp_path, text=json.dumps([{"provider_key": "desktop:x", "encrypted_api_key": "..."}]))
    registry = FakeRegistry()

    assert bootstrap_desktop_providers(registry, tmp_path, desktop_authenticated=True) == 0
    assert desktop_providers.last_load_failure() == ""
    assert clobber_block_reason(tmp_path) == ""


def test_empty_sync_is_refused_while_the_snapshot_is_unusable(tmp_path):
    _write(tmp_path, text="not json at all")

    bootstrap_desktop_providers(FakeRegistry(), tmp_path, desktop_authenticated=True)

    reason = clobber_block_reason(tmp_path)
    assert reason, "an empty sync must be refused after a failed load"
    assert "refusing to apply an empty provider list" in reason


def test_empty_sync_is_allowed_when_the_snapshot_is_simply_absent(tmp_path):
    assert bootstrap_desktop_providers(FakeRegistry(), tmp_path, desktop_authenticated=True) == 0
    assert clobber_block_reason(tmp_path) == ""


def test_non_desktop_session_never_reads_or_flags_credentials(tmp_path):
    _write(tmp_path, text='[{"provider_key": "desktop:x"')
    registry = FakeRegistry()

    assert bootstrap_desktop_providers(registry, tmp_path, desktop_authenticated=False) == 0
    assert registry.replaced is None
    assert desktop_providers.last_load_failure() == ""


def test_successful_restore_clears_a_previous_failure(tmp_path):
    _write(tmp_path, text="broken")
    bootstrap_desktop_providers(FakeRegistry(), tmp_path, desktop_authenticated=True)
    assert desktop_providers.last_load_failure()

    _write(tmp_path, text=json.dumps(PROFILES))
    registry = FakeRegistry()
    assert bootstrap_desktop_providers(registry, tmp_path, desktop_authenticated=True) == 2
    assert desktop_providers.last_load_failure() == ""
    assert clobber_block_reason(tmp_path) == ""


def test_error_reasons_never_contain_payload_or_credentials(tmp_path):
    secret = "sk-do-not-log-me-0123456789"
    _write(tmp_path, text='[{"provider_key": "desktop:x", "api_key": "' + secret + '"')

    bootstrap_desktop_providers(FakeRegistry(), tmp_path, desktop_authenticated=True)

    assert secret not in desktop_providers.last_load_failure()
    assert secret not in clobber_block_reason(tmp_path)


def test_encrypted_sentinel_is_a_distinct_type():
    assert issubclass(EncryptedSnapshot, Exception)
    assert not issubclass(EncryptedSnapshot, ValueError)


@pytest.mark.parametrize("payload", [
    "bad-json",
    {},
    [PROFILES[0], PROFILES[0]],
    [PROFILES[0], {"protocol": "unknown"}],
    [{**PROFILES[0], "provider_key": "unstable"}],
    [{"provider_key": "desktop:encrypted", "encrypted_api_key": "ciphertext"}],
])
def test_live_registry_survives_every_unusable_snapshot(tmp_path, caplog, payload):
    """Mirrors tests/test_desktop_provider_bootstrap.py, which cannot run under
    the session sandbox because it needs pytest's own tmp_path."""
    from mewcode.model_registry import ModelRef, ProviderRegistry

    profile = {
        "provider_key": "desktop:example_provider_7", "name": "TypeSafe", "brand": "typesafe",
        "protocol": "typesafe", "base_url": "https://api.typesafe.ai/v1",
        "default_model": "jev-latest", "models": ["jev-latest", "jev-preview"],
        "api_key": "test-bootstrap-secret",
    }
    ref = ModelRef(profile["provider_key"], "jev-latest")
    registry = ProviderRegistry()
    registry.replace_desktop_profiles([profile])
    live = registry.get(ref.provider_key)

    _write(tmp_path, text=payload if isinstance(payload, str) else json.dumps(payload))
    with caplog.at_level("DEBUG"):
        assert bootstrap_desktop_providers(registry, tmp_path, desktop_authenticated=True) == 0

    assert registry.get(ref.provider_key) is live, "a live binding was replaced by an unusable snapshot"
    assert "test-bootstrap-secret" not in caplog.text
    assert "ciphertext" not in caplog.text


def test_bom_snapshot_reaches_a_real_registry(tmp_path):
    from mewcode.model_registry import ModelRef, ProviderRegistry

    profile = {
        "provider_key": "desktop:example_provider_7", "name": "TypeSafe", "brand": "typesafe",
        "protocol": "typesafe", "base_url": "https://api.typesafe.ai/v1",
        "default_model": "jev-latest", "models": ["jev-latest", "jev-preview"],
        "api_key": "test-bootstrap-secret",
    }
    raw = b"\xef\xbb\xbf" + json.dumps([profile]).encode("utf-8")
    path = tmp_path / "providers.json"
    path.write_bytes(raw)
    before = path.read_bytes()

    registry = ProviderRegistry()
    assert bootstrap_desktop_providers(registry, tmp_path, desktop_authenticated=True) == 1
    resolved = registry.resolve(ModelRef(profile["provider_key"], "jev-latest"))
    assert resolved.config.api_key == "test-bootstrap-secret"
    assert path.read_bytes() == before, "bootstrap must never rewrite the snapshot"
