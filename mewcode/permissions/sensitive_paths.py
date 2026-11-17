"""Deterministic path classification for locations that must not be read.

This module only classifies a requested path.  It does not grant access or
enforce a sandbox; callers must apply the result before reading or executing a
command.  Windows paths and common PowerShell / cmd environment-variable forms
are normalized so Read tools and command adapters can share the same policy.
"""
from __future__ import annotations

import ntpath
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class SensitivePathClassification:
    """Result of classifying one path, without returning the private path."""

    sensitive: bool
    reason: str = ""


_ENV_REFERENCE = re.compile(
    r"(?i)\$\{?env:([A-Za-z_][A-Za-z0-9_]*)\}?|%([A-Za-z_][A-Za-z0-9_]*)%"
)
_DRIVE_PATH = re.compile(r"^[a-zA-Z]:[\\/]")
_EXTENDED_PREFIX = re.compile(r"(?i)^\\\\\?\\")


def _environment(env: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if env is None else env
    return {str(key).casefold(): str(value) for key, value in source.items()}


def _expand_environment(text: str, values: Mapping[str, str]) -> str:
    # Expand nested values as well (for example APPDATA=%USERPROFILE%\AppData\Roaming).
    result = text
    for _ in range(8):
        updated = _ENV_REFERENCE.sub(
            lambda match: values.get((match.group(1) or match.group(2)).casefold(), match.group(0)),
            result,
        )
        if updated == result:
            break
        result = updated
    return result


def normalize_windows_path(
    path: str | os.PathLike[str],
    *,
    base_dir: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Return a case-insensitive, absolute-style normalized path string.

    ``base_dir`` makes relative inputs workspace-relative.  The function is
    intentionally lexical; filesystem links are resolved separately by the
    classifier so this helper remains useful for synthetic paths in tests.
    """

    values = _environment(env)
    raw = os.fspath(path).strip().strip("\"'")
    raw = _expand_environment(raw, values)

    if raw == "~" or raw.startswith(("~/", "~\\")):
        home = values.get("userprofile") or values.get("home")
        if home:
            raw = home + raw[1:]

    # Keep native POSIX absolute paths usable in tests and on non-Windows hosts,
    # while representing both path families with Windows separators for policy
    # comparisons.
    raw = raw.replace("/", "\\")
    raw = _EXTENDED_PREFIX.sub("", raw)
    if raw.casefold().startswith("unc\\"):
        raw = "\\\\" + raw[4:]

    if not ntpath.isabs(raw) and base_dir is not None:
        base = os.fspath(base_dir).strip().strip("\"'").replace("/", "\\")
        base = _expand_environment(base, values)
        raw = ntpath.join(base, raw)

    normalized = ntpath.normpath(raw)
    # normcase also folds case, matching the Windows filesystem's default
    # comparison semantics. Keep root paths normalized without a trailing slash.
    return ntpath.normcase(normalized)


def _native_resolution_path(expanded_raw: str) -> Path | None:
    """Build a host-native Path only when the input uses this host's syntax."""

    if os.path.isabs(expanded_raw):
        return Path(expanded_raw)
    if os.name == "nt" and ntpath.isabs(expanded_raw):
        return Path(expanded_raw)
    # Do not reinterpret synthetic C:\ paths as relative POSIX names.
    if _DRIVE_PATH.match(expanded_raw) or expanded_raw.startswith("\\\\"):
        return None
    return None


def _resolved_candidates(
    path: str | os.PathLike[str],
    *,
    base_dir: str | os.PathLike[str] | None,
    env: Mapping[str, str] | None,
) -> tuple[list[str], str]:
    values = _environment(env)
    raw = _expand_environment(os.fspath(path).strip().strip("\"'"), values)
    if raw == "~" or raw.startswith(("~/", "~\\")):
        home = values.get("userprofile") or values.get("home")
        if home:
            raw = home + raw[1:]

    lexical = normalize_windows_path(raw, base_dir=base_dir, env=values)
    candidates = [lexical]
    native_raw = raw
    if base_dir is not None and not ntpath.isabs(raw):
        native_base = os.fspath(base_dir).strip().strip("\"'")
        native_base = _expand_environment(native_base, values)
        if os.path.isabs(native_base):
            native_raw = os.path.join(native_base, raw)
        elif os.name == "nt" and ntpath.isabs(native_base):
            native_raw = ntpath.join(native_base, raw)
    native_path = _native_resolution_path(native_raw)
    if native_path is not None:
        try:
            resolved = normalize_windows_path(native_path.resolve(strict=False), env=values)
        except (OSError, RuntimeError, ValueError):
            resolved = lexical
        if resolved not in candidates:
            candidates.append(resolved)
    # The final item is the resolved target when the host could determine one.
    return candidates, candidates[-1]


def _is_same_or_child(path: str, root: str) -> bool:
    try:
        return ntpath.commonpath((path, root)) == root
    except (ValueError, OSError):
        return False


def _configured_roots(values: Mapping[str, str]) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return sensitive directory and exact/prefix-file roots with safe labels."""

    profile = values.get("userprofile") or values.get("home", "")
    appdata = values.get("appdata", "")
    local_appdata = values.get("localappdata", "")
    if profile:
        appdata = appdata or ntpath.join(profile, "AppData", "Roaming")
        local_appdata = local_appdata or ntpath.join(profile, "AppData", "Local")

    directories: list[tuple[str, str]] = []
    files: list[tuple[str, str]] = []

    if profile:
        for relative, label in (
            (r".ssh", "SSH credentials"),
            (r".aws", "AWS credentials"),
            (r".azure", "Azure credentials"),
            (r".config\gcloud", "Google Cloud credentials"),
            (r".codex\auth.json", "Codex authentication"),
        ):
            target = ntpath.join(profile, relative)
            if relative.casefold().endswith("auth.json"):
                files.append((normalize_windows_path(target, env=values), label))
            else:
                directories.append((normalize_windows_path(target, env=values), label))
        for relative, label in (
            (r".docker\config.json", "Docker credentials"),
            (r".git-credentials", "Git credentials"),
            (r".netrc", "network credentials"),
        ):
            files.append((normalize_windows_path(ntpath.join(profile, relative), env=values), label))

    if appdata:
        app_root = normalize_windows_path(ntpath.join(appdata, "com.mewcode.desktop"), env=values)
        files.append((ntpath.join(app_root, "providers.json"), "MewCode provider credentials"))
        files.append((ntpath.join(app_root, "usage-accounts.json"), "MewCode account credentials"))
        directories.extend((
            (normalize_windows_path(ntpath.join(appdata, r"Mozilla\Firefox\Profiles"), env=values), "Firefox profiles"),
            (normalize_windows_path(ntpath.join(appdata, r"Microsoft\Credentials"), env=values), "Windows credentials"),
            (normalize_windows_path(ntpath.join(appdata, r"Microsoft\Protect"), env=values), "Windows protected credentials"),
        ))

    if local_appdata:
        directories.extend((
            (normalize_windows_path(ntpath.join(local_appdata, r"Google\Chrome\User Data"), env=values), "Chrome profile data"),
            (normalize_windows_path(ntpath.join(local_appdata, r"Microsoft\Edge\User Data"), env=values), "Edge profile data"),
            (normalize_windows_path(ntpath.join(local_appdata, r"Microsoft\Credentials"), env=values), "Windows credentials"),
        ))

    return directories, files


def classify_sensitive_read_path(
    path: str | os.PathLike[str],
    *,
    workspace_root: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> SensitivePathClassification:
    """Classify whether a read target matches the checklist H sensitive list.

    Fixed credential/profile locations are sensitive wherever they are reached,
    including through links. Secret-looking filenames are blocked outside the
    workspace. When no workspace is supplied, their location cannot be proven
    safe, so the filename rule fails closed.
    """

    return SensitiveReadFilter(workspace_root=workspace_root, env=env).classify(path)


class SensitiveReadFilter:
    """One scan's policy roots; each target is still resolved before reading."""
    def __init__(self, *, workspace_root=None, env=None):
        self.workspace_root = workspace_root
        self.values = _environment(env)
        self.workspace_normalized = (_resolved_candidates(
            workspace_root, base_dir=None, env=self.values)[1]
            if workspace_root is not None else "")
        self.directories, self.files = _configured_roots(self.values)

    def classify(self, path):
        if not os.fspath(path).strip():
            return SensitivePathClassification(False)
        candidates, effective_path = _resolved_candidates(
            path, base_dir=self.workspace_root, env=self.values)
        workspace_root_normalized = self.workspace_normalized
        directories, files = self.directories, self.files
        for candidate in candidates:
            for sensitive_root, label in directories:
                if _is_same_or_child(candidate, sensitive_root):
                    return SensitivePathClassification(True, label)
            for sensitive_file, label in files:
                if candidate == sensitive_file:
                    return SensitivePathClassification(True, label)
                # Checklist H names providers.json* to include rotated/backup files.
                if label == "MewCode provider credentials" and candidate.startswith(sensitive_file):
                    return SensitivePathClassification(True, label)

        filename = ntpath.basename(effective_path).casefold()
        looks_like_secret_file = (
            filename == ".env"
            or filename.startswith(".env.")
            or filename.endswith((".pem", ".key", ".pfx"))
            or filename.startswith(("id_rsa", "id_ed25519"))
        )
        if looks_like_secret_file:
            inside_workspace = bool(workspace_root_normalized) and _is_same_or_child(
                effective_path, workspace_root_normalized
            )
            # If a workspace alias resolves outside the workspace, use the resolved
            # target above; lexical containment alone must not hide link escapes.
            if not inside_workspace:
                return SensitivePathClassification(True, "credential or private-key file outside workspace")

        return SensitivePathClassification(False)


def is_sensitive_read_path(
    path: str | os.PathLike[str],
    *,
    workspace_root: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Boolean convenience wrapper around :func:`classify_sensitive_read_path`."""

    return classify_sensitive_read_path(path, workspace_root=workspace_root, env=env).sensitive
