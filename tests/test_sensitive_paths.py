from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from mewcode.permissions.sensitive_paths import (
    classify_sensitive_read_path,
    is_sensitive_read_path,
    normalize_windows_path,
)


TEST_ENV = {
    "USERPROFILE": r"C:\Users\example-user",
    "APPDATA": r"C:\Users\example-user\AppData\Roaming",
    "LOCALAPPDATA": r"C:\Users\example-user\AppData\Local",
}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (r'"%USERPROFILE%/folder/../.ssh/id_ed25519"', r"c:\users\alice\.ssh\id_ed25519"),
        (r"$env:APPDATA\com.mewcode.desktop\providers.json", r"c:\users\alice\appdata\roaming\com.mewcode.desktop\providers.json"),
        (r"%USERPROFILE%\folder\..\file.txt", r"c:\users\alice\file.txt"),
    ],
)
def test_normalize_windows_path_expands_vars_and_dot_segments(path: str, expected: str) -> None:
    assert normalize_windows_path(path, env=TEST_ENV) == expected


@pytest.mark.parametrize(
    "path",
    [
        r"%USERPROFILE%\.ssh\id_ed25519",
        r"$env:USERPROFILE\.aws\credentials",
        r"%USERPROFILE%\.azure\profile.json",
        r"%USERPROFILE%\.config\gcloud\application_default_credentials.json",
        r"%USERPROFILE%\.docker\config.json",
        r"%USERPROFILE%\.git-credentials",
        r"%USERPROFILE%\.netrc",
        r"%USERPROFILE%\.codex\auth.json",
        r"%APPDATA%\com.mewcode.desktop\providers.json",
        r"%APPDATA%\com.mewcode.desktop\providers.json.bak",
        r"%APPDATA%\com.mewcode.desktop\providers.json.bak.20260925",
        r"%APPDATA%\com.mewcode.desktop\usage-accounts.json",
        r"%LOCALAPPDATA%\Google\Chrome\User Data\Default\Preferences",
        r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Default\Preferences",
        r"%APPDATA%\Mozilla\Firefox\Profiles\abc.default\prefs.js",
        r"%APPDATA%\Microsoft\Credentials\credential",
        r"%LOCALAPPDATA%\Microsoft\Credentials\credential",
        r"%APPDATA%\Microsoft\Protect\S-1-5-21\masterkey",
    ],
)
def test_checklist_h_credential_locations_are_sensitive(path: str) -> None:
    decision = classify_sensitive_read_path(path, workspace_root=r"C:\work\repo", env=TEST_ENV)
    assert decision.sensitive
    assert decision.reason


def test_provider_backup_prefix_is_case_insensitive_but_does_not_match_sibling() -> None:
    assert is_sensitive_read_path(
        r"%APPDATA%\COM.MEWCODE.DESKTOP\Providers.JSON.BAK1", env=TEST_ENV
    )
    assert not is_sensitive_read_path(
        r"%APPDATA%\com.mewcode.desktop\providers-old.json", env=TEST_ENV
    )


@pytest.mark.parametrize(
    "name",
    [".env", ".env.local", "client.pem", "private.key", "bundle.pfx", "id_rsa", "id_rsa_old", "id_ed25519_work"],
)
def test_secret_filenames_outside_workspace_are_sensitive(name: str) -> None:
    assert is_sensitive_read_path(
        rf"D:\shared\secrets\{name}", workspace_root=r"C:\work\repo", env=TEST_ENV
    )


@pytest.mark.parametrize(
    "name",
    [".env", ".env.local", "client.pem", "private.key", "bundle.pfx", "id_rsa_work"],
)
def test_secret_filenames_inside_workspace_follow_explicit_checklist_exception(name: str) -> None:
    assert not is_sensitive_read_path(
        rf"C:\work\repo\fixtures\{name}", workspace_root=r"C:\work\repo", env=TEST_ENV
    )


def test_unknown_workspace_fails_closed_for_secret_filename() -> None:
    assert is_sensitive_read_path(r"relative\keys\private.pem", env=TEST_ENV)


def test_ordinary_paths_are_not_sensitive() -> None:
    assert not is_sensitive_read_path(r"C:\work\repo\README.md", workspace_root=r"C:\work\repo", env=TEST_ENV)
    assert not is_sensitive_read_path(r"C:\Users\example-user\AppData\Roaming\com.mewcode.desktop\settings.json", env=TEST_ENV)


def test_workspace_symlink_to_external_env_file_is_classified_by_resolved_target() -> None:
    with tempfile.TemporaryDirectory(prefix=".test-sensitive-", dir=Path.cwd()) as raw_root:
        root = Path(raw_root)
        workspace = root / "workspace"
        external = root / "external"
        workspace.mkdir()
        external.mkdir()
        (external / ".env").write_text("synthetic fixture", encoding="utf-8")
        link = workspace / "linked"
        try:
            link.symlink_to(external, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"directory symlink unavailable on this host: {exc}")

        assert is_sensitive_read_path(link / ".env", workspace_root=workspace, env={})


def test_workspace_symlink_to_configured_credential_root_is_classified() -> None:
    with tempfile.TemporaryDirectory(prefix=".test-sensitive-", dir=Path.cwd()) as raw_root:
        root = Path(raw_root)
        workspace = root / "workspace"
        appdata = root / "roaming"
        sensitive_dir = appdata / "Microsoft" / "Credentials"
        workspace.mkdir()
        sensitive_dir.mkdir(parents=True)
        (sensitive_dir / "synthetic.dat").write_text("fixture", encoding="utf-8")
        link = workspace / "profile-cache"
        try:
            link.symlink_to(sensitive_dir, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"directory symlink unavailable on this host: {exc}")

        custom_env = {"APPDATA": str(appdata), "USERPROFILE": str(root / "profile")}
        assert is_sensitive_read_path(
            link / "synthetic.dat", workspace_root=workspace, env=custom_env
        )
