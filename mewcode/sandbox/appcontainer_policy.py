"""Build a narrowly scoped request for an experimental AppContainer probe.

This module only validates and serializes a probe request. It does not create
files, change ACLs, or establish an operating-system sandbox. The fixture
sentinel is a mistake-prevention check; it is not a trust decision or proof of
authorization. A successful policy build must never be treated as evidence
that a process was confined by Windows.
"""

from __future__ import annotations

import ntpath
import os
import stat
from pathlib import Path
from typing import Any


_FIXTURE_PREFIX = "mewcode-lpac-probe-"
_SENTINEL_NAME = ".mewcode-synthetic-fixture"
_SENTINEL_CONTENT = b"mewcode-synthetic-only-v1\n"
_RUNTIME_DIRECTORY = "temp"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_MIN_TIMEOUT_MS = 1_000
_MAX_TIMEOUT_MS = 60_000


def _path_from(value: Path, label: str) -> Path:
    """Return an absolute, lexically safe Path without resolving links."""
    try:
        raw = os.fspath(value)
    except TypeError as exc:
        raise ValueError(f"{label} must be a filesystem path") from exc
    if not isinstance(raw, str) or "\x00" in raw:
        raise ValueError(f"{label} must be a path string without NUL")
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    if ".." in path.parts:
        raise ValueError(f"{label} must not contain parent traversal")
    # Windows permits alternate data streams via ':' in a path component.
    # The drive component is the only colon-bearing component we accept.
    for part in path.parts[1:]:
        if ":" in part:
            raise ValueError(f"{label} must not name an alternate data stream")
    return Path(os.path.abspath(raw))


def _check_no_reparse_ancestry(path: Path, label: str) -> None:
    """Reject symbolic links and Windows reparse points at every path level."""
    # All ancestors must exist for the corresponding requested object to exist.
    # lstat() is intentional: stat() would follow a link before we can reject it.
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except OSError as exc:
            raise ValueError(f"{label} and its full path ancestry must exist") from exc
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise ValueError(f"{label} must not use a symlink or reparse point")


def _existing_path(
    value: Path,
    label: str,
    *,
    directory: bool | None = None,
    reject_hardlinks: bool = False,
) -> Path:
    path = _path_from(value, label)
    _check_no_reparse_ancestry(path, label)
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise ValueError(f"{label} must exist") from exc
    if directory is True and not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"{label} must be an existing directory")
    if directory is False and not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{label} must be an existing regular file")
    if reject_hardlinks and stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
        raise ValueError(f"{label} must not be a hard link")
    return resolved


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.fspath(path)))


def _is_strictly_beneath(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((_path_key(path), _path_key(root))) == _path_key(root) and (
            _path_key(path) != _path_key(root)
        )
    except ValueError:
        # Different Windows drives or a malformed path are never beneath root.
        return False


def _assert_not_broad_fixture_root(root: Path) -> None:
    if _path_key(root) == _path_key(Path(root.anchor)):
        raise ValueError("fixture_root must not be a filesystem root")

    try:
        user_home = Path.home().resolve(strict=False)
    except OSError:
        user_home = Path.home()
    if _path_key(root) == _path_key(user_home):
        raise ValueError("fixture_root must not be the user home directory")

    # appcontainer_policy.py is stored at <repo>/mewcode/sandbox/.
    repo_root = Path(__file__).resolve().parents[2]
    if _path_key(root) == _path_key(repo_root):
        raise ValueError("fixture_root must not be the repository root")


def _windows_system_root() -> Path:
    raw_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    if not raw_root:
        raise ValueError("Windows system root is unavailable")
    return _existing_path(Path(raw_root), "Windows system root", directory=True)


def _trusted_system_executables(system_root: Path) -> set[str]:
    system32 = system_root / "System32"
    return {
        _path_key(system32 / "WindowsPowerShell" / "v1.0" / "powershell.exe"),
        _path_key(system32 / "cmd.exe"),
    }


def _validate_fixture_root(value: Path) -> Path:
    root = _existing_path(value, "fixture_root", directory=True)
    _assert_not_broad_fixture_root(root)
    if not root.name.casefold().startswith(_FIXTURE_PREFIX):
        raise ValueError(f"fixture_root name must start with {_FIXTURE_PREFIX!r}")

    sentinel = root / _SENTINEL_NAME
    sentinel = _existing_path(
        sentinel,
        "fixture sentinel",
        directory=False,
        reject_hardlinks=True,
    )
    try:
        if sentinel.stat().st_size != len(_SENTINEL_CONTENT):
            raise ValueError("fixture sentinel content does not match")
        with sentinel.open("rb") as stream:
            content = stream.read(len(_SENTINEL_CONTENT) + 1)
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError("fixture sentinel must be readable") from exc
    if content != _SENTINEL_CONTENT:
        raise ValueError("fixture sentinel content does not match")
    return root


def _validate_grants(values: list[Path], label: str, root: Path) -> list[str]:
    if not isinstance(values, list):
        raise ValueError(f"{label} must be a list of paths")
    result: list[str] = []
    for index, value in enumerate(values):
        item_label = f"{label}[{index}]"
        path = _existing_path(value, item_label, reject_hardlinks=True)
        if not _is_strictly_beneath(path, root):
            raise ValueError(f"{item_label} must exist strictly beneath fixture_root")
        result.append(str(path))
    return result


def _validate_execute_grants(values: list[Path], root: Path) -> list[str]:
    """Validate exact regular files that receive read plus execute rights."""
    if not isinstance(values, list):
        raise ValueError("execute_paths must be a list of paths")
    result: list[str] = []
    for index, value in enumerate(values):
        item_label = f"execute_paths[{index}]"
        path = _existing_path(value, item_label, directory=False, reject_hardlinks=True)
        if not _is_strictly_beneath(path, root):
            raise ValueError(f"{item_label} must exist strictly beneath fixture_root")
        result.append(str(path))
    return result


def _validate_arguments(arguments: list[str]) -> list[str]:
    if not isinstance(arguments, list):
        raise ValueError("arguments must be a list of strings")
    checked: list[str] = []
    for index, argument in enumerate(arguments):
        if not isinstance(argument, str) or "\x00" in argument:
            raise ValueError(f"arguments[{index}] must be a string without NUL")
        checked.append(argument)
    return checked


def build_probe_request(
    *,
    fixture_root: Path,
    executable: Path,
    arguments: list[str],
    cwd: Path,
    read_paths: list[Path],
    write_paths: list[Path],
    execute_paths: list[Path] | None = None,
    timeout_ms: int = 20_000,
    lpac: bool = True,
    runtime_registry_read: bool = False,
    runtime_instrumentation: bool = False,
    runtime_com: bool = False,
) -> dict[str, Any]:
    """Validate a synthetic fixture and return a minimal host policy request.

    The only executable accepted outside the fixture is the exact Windows
    PowerShell or command processor binary under the discovered system root.
    This function never searches PATH or writes to the fixture.
    """
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int):
        raise ValueError("timeout_ms must be an integer")
    if not _MIN_TIMEOUT_MS <= timeout_ms <= _MAX_TIMEOUT_MS:
        raise ValueError("timeout_ms must be between 1000 and 60000")
    if not isinstance(lpac, bool):
        raise ValueError("lpac must be a boolean")
    if not isinstance(runtime_registry_read, bool):
        raise ValueError("runtime_registry_read must be a boolean")
    if runtime_registry_read and not lpac:
        raise ValueError("runtime_registry_read is only available for LPAC requests")
    if not isinstance(runtime_instrumentation, bool):
        raise ValueError("runtime_instrumentation must be a boolean")
    if runtime_instrumentation and not lpac:
        raise ValueError("runtime_instrumentation is only available for LPAC requests")
    if runtime_instrumentation and not runtime_registry_read:
        raise ValueError("runtime_instrumentation requires runtime_registry_read")
    if not isinstance(runtime_com, bool):
        raise ValueError("runtime_com must be a boolean")
    if runtime_com and (not lpac or not runtime_registry_read):
        raise ValueError("runtime_com requires LPAC and runtime_registry_read")

    root = _validate_fixture_root(fixture_root)
    working_directory = _existing_path(cwd, "cwd", directory=True)
    if not _is_strictly_beneath(working_directory, root):
        raise ValueError("cwd must exist strictly beneath fixture_root")

    runtime_directory = _existing_path(
        working_directory / _RUNTIME_DIRECTORY,
        "cwd runtime directory",
        directory=True,
    )
    if not _is_strictly_beneath(runtime_directory, root):
        raise ValueError("cwd runtime directory must exist beneath fixture_root")

    profile_directory = _existing_path(
        root / "profile",
        "USERPROFILE",
        directory=True,
    )
    roaming_directory = _existing_path(
        profile_directory / "AppData" / "Roaming",
        "APPDATA",
        directory=True,
    )
    local_directory = _existing_path(
        profile_directory / "AppData" / "Local",
        "LOCALAPPDATA",
        directory=True,
    )
    for label, path in (
        ("USERPROFILE", profile_directory),
        ("APPDATA", roaming_directory),
        ("LOCALAPPDATA", local_directory),
    ):
        if not _is_strictly_beneath(path, root):
            raise ValueError(f"{label} must exist strictly beneath fixture_root")

    system_root = _windows_system_root()
    executable_candidate = _path_from(executable, "executable")
    trusted_executables = _trusted_system_executables(system_root)
    is_trusted_executable = _path_key(executable_candidate) in trusted_executables
    executable_path = _existing_path(
        executable_candidate,
        "executable",
        directory=False,
        reject_hardlinks=not is_trusted_executable,
    )
    if executable_path.suffix.casefold() != ".exe":
        raise ValueError("executable must have an .exe suffix")
    if not _is_strictly_beneath(executable_path, root) and (
        _path_key(executable_path) not in trusted_executables
    ):
        raise ValueError("executable must be inside fixture_root or a trusted Windows system path")

    arguments_checked = _validate_arguments(arguments)
    read_grants = _validate_grants(read_paths, "read_paths", root)
    write_grants = _validate_grants(write_paths, "write_paths", root)
    execute_grants = _validate_execute_grants([] if execute_paths is None else execute_paths, root)

    system32 = system_root / "System32"
    system_drive = ntpath.splitdrive(str(system_root))[0]
    environment = {
        "SystemRoot": str(system_root),
        "WINDIR": str(system_root),
        "SystemDrive": system_drive,
        "COMSPEC": str(system32 / "cmd.exe"),
        # Hosted Windows PowerShell otherwise silently loses native commands.
        # Never inherit the caller's extension list or arbitrary script types.
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "PATH": str(system32),
        "TEMP": str(runtime_directory),
        "TMP": str(runtime_directory),
        "USERPROFILE": str(profile_directory),
        "APPDATA": str(roaming_directory),
        "LOCALAPPDATA": str(local_directory),
    }

    return {
        "fixture_root": str(root),
        "executable": str(executable_path),
        "arguments": arguments_checked,
        "cwd": str(working_directory),
        "read_paths": read_grants,
        "write_paths": write_grants,
        "execute_paths": execute_grants,
        "timeout_ms": timeout_ms,
        "lpac": lpac,
        "runtime_registry_read": runtime_registry_read,
        "runtime_instrumentation": runtime_instrumentation,
        "runtime_com": runtime_com,
        "environment": environment,
    }
