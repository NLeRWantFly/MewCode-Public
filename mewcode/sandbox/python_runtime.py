"""Discover the bounded base-runtime dependency of a literal workspace venv.

The staged request enumerates exact readable leaves itself.  This module only
recognizes a small literal PowerShell command prefix, validates the frozen
``pyvenv.cfg`` and verifies an already-readable Python installation; it never
creates a permission grant or changes the interpreter/configuration.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat

from mewcode.sandbox.lpac_runtime import checked_path
from mewcode.sandbox.windows import NativeBoundaryUnavailable

MAX_PYVENV_CFG_BYTES = 64 * 1024
_AUTHORITY_COMPONENTS = frozenset({".mewcode", ".codex", ".agents"})
_CONFIG_KEYS = frozenset({"home", "include-system-site-packages", "version",
                          "version_info", "executable", "command", "prompt",
                          "implementation", "uv"})
_VENV_PATH = (r"(?:\.[\\/])?(?:[A-Za-z0-9_.\- \u0080-\uffff]+[\\/])*"
              r"(?:\.venv|venv)[\\/]Scripts[\\/]pythonw?\.exe")
_PREFIX = re.compile(
    rf"^\s*(?P<call>&\s*)?(?:'(?P<single>{_VENV_PATH})'(?=\s|;|$)|"
    rf'"(?P<double>{_VENV_PATH})"(?=\s|;|$)|'
    rf"(?P<bare>{_VENV_PATH})(?=\s|;|$))",
    re.IGNORECASE,
)
_PYTHON_ALIAS = re.compile(r"^\s*(?:&\s*)?python(?:3)?(?:\.exe)?(?=\s|;|$)", re.IGNORECASE)


class PythonRuntimeUnavailable(NativeBoundaryUnavailable):
    error_code = "python_runtime_missing"


def literal_interpreter(command: str) -> str | None:
    """Return only a leading, literal workspace venv interpreter path."""
    if not isinstance(command, str) or "\0" in command:
        return None
    match = _PREFIX.match(command)
    if not match:
        return None
    if match.group("call") is None and (match.group("single") is not None or match.group("double") is not None):
        return None
    literal = next(value for value in match.group("single", "double", "bare") if value is not None)
    parts = literal.replace("\\", "/").split("/")
    if parts[0] == ".":
        parts = parts[1:]
    if (any(part in {"", ".", ".."} or part.rstrip(" .") != part
            or part.casefold() in _AUTHORITY_COMPONENTS for part in parts)
            or (match.group("bare") is not None and any(char.isspace() for char in literal))):
        return None
    return literal


def _reject(message: str):
    raise NativeBoundaryUnavailable(message)


def _read_regular(path: Path, maximum: int) -> tuple[bytes, os.stat_result]:
    """Read a regular single-link file with identity and growth checks."""
    path = checked_path(path, file=True)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        _reject("Python runtime inputs must be ordinary, single-link files")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino, opened.st_size)
                != (before.st_dev, before.st_ino, before.st_size)):
            _reject("Python runtime input changed while opening")
        chunks = []
        size = 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, maximum - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > maximum:
                _reject("Python runtime input exceeds its host bound")
            chunks.append(chunk)
        after = os.fstat(fd)
        current = path.lstat()
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        if identity(after) != identity(opened) or identity(current) != identity(before):
            _reject("Python runtime input changed while reading")
        return b"".join(chunks), after
    finally:
        os.close(fd)


def _parse_cfg(raw: bytes) -> tuple[Path, dict[str, str]]:
    try:
        text = raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as error:
        raise NativeBoundaryUnavailable("pyvenv.cfg must be bounded UTF-8") from error
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            _reject("Unsupported pyvenv.cfg syntax")
        key, value = (part.strip() for part in stripped.split("=", 1))
        key = key.casefold()
        if key not in _CONFIG_KEYS or not value or key in values:
            _reject("Unsupported or duplicate pyvenv.cfg setting")
        values[key] = value
    home_value = values.get("home")
    if not home_value or any(char in home_value for char in ('\0', '"', "'")):
        _reject("pyvenv.cfg needs one literal home path")
    if values.get("include-system-site-packages", "false").casefold() != "false":
        _reject("Python venvs with system site-packages enabled are unsupported")
    home = Path(home_value)
    if (not home.is_absolute() or ".." in home.parts
            or any(":" in part for part in home.parts[1:])
            or home.anchor.startswith("\\\\") or home.drive.startswith("\\\\")):
        _reject("pyvenv.cfg home must be a local absolute path without traversal or streams")
    return home, values


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _validate_external_home(home: Path, *, source: Path, scope, forbidden_roots: tuple[Path, ...]):
    try:
        resolved = checked_path(home)
    except (OSError, ValueError) as error:
        raise NativeBoundaryUnavailable("Python base home is not an ordinary existing directory") from error
    if not resolved.is_dir() or resolved == Path(resolved.anchor):
        _reject("Python base home must be an existing bounded directory")
    if (resolved.anchor.startswith("\\\\") or resolved.drive.startswith("\\\\")
            or resolved == Path.home().resolve()):
        _reject("Python base home must not be a UNC or user-home root")
    if any(part.casefold() in _AUTHORITY_COMPONENTS for part in resolved.parts):
        _reject("Python base home must not contain an authority-store path")
    roots = [source, *forbidden_roots]
    for root in roots:
        try:
            bounded = checked_path(root)
        except (OSError, ValueError):
            bounded = Path(root).resolve(strict=False)
        if _overlaps(resolved, bounded):
            _reject("Python base home overlaps the workspace or host state")
    if not scope.current() or not scope.allows(resolved, write=False):
        _reject("Python base home is not readable under current host scope")
    return resolved


def _ordinary_runtime(home: Path, scope) -> dict:
    """Validate normal Python runtime shape and fingerprint its identity."""
    executable = checked_path(home / "python.exe", file=True)
    dlls = sorted((path for path in home.iterdir()
                   if path.is_file() and re.fullmatch(r"python3.*\.dll", path.name, re.IGNORECASE)),
                  key=lambda path: path.name.casefold())
    dlls = [checked_path(path, file=True) for path in dlls]
    encodings = checked_path(home / "Lib" / "encodings" / "__init__.py", file=True)
    if not dlls:
        _reject("Python base home is missing python3*.dll")
    required = [executable, *dlls, encodings]
    if any(not scope.allows(path, write=False) for path in required):
        _reject("Python runtime files are not readable under current host scope")
    root_info = home.stat()
    facts = []
    for path in required:
        info = path.stat()
        facts.append({"relative": path.relative_to(home).as_posix(), "size": info.st_size,
                      "device": info.st_dev, "inode": info.st_ino,
                      "modified_ns": info.st_mtime_ns})
    facts.sort(key=lambda item: item["relative"].casefold())
    value = {"device": root_info.st_dev, "inode": root_info.st_ino, "files": facts}
    canonical = "\n".join(f"{item['relative']}\0{item['size']}\0{item['device']}\0{item['inode']}\0{item['modified_ns']}"
                         for item in facts).encode("utf-8")
    value["sha256"] = hashlib.sha256(
        f"{root_info.st_dev}\0{root_info.st_ino}\0".encode() + canonical).hexdigest()
    return value


@dataclass(frozen=True)
class PythonRuntimeDependency:
    config_path: Path
    source_config_path: Path
    source_workspace: Path
    home: Path
    config_sha256: str
    source_identity: tuple[int, int, int]
    scope: object
    external: bool
    home_fingerprint: dict | None
    configured_home: Path | None = None
    original_config: bytes | None = None

    @property
    def extra_read(self) -> tuple[Path, ...]:
        return (self.home,) if self.external else ()

    @property
    def executable(self) -> Path:
        return checked_path(self.config_path.parent / "Scripts/python.exe", file=True)

    @property
    def receipt(self) -> dict:
        return {"kind": "workspace_venv", "config_path": str(self.source_config_path),
                "config_sha256": self.config_sha256, "home": str(self.home),
                "external": self.external, "home_fingerprint": self.home_fingerprint,
                "configured_home": str(self.configured_home) if self.configured_home else str(self.home),
                "home_canonicalized": self.configured_home is not None}

    def _execution_config(self) -> bytes:
        text = self.original_config.decode("utf-8-sig")
        return re.sub(r"(?im)^[ \t]*home[ \t]*=[^\r\n]*", lambda _: "home = " + str(self.home), text).encode("utf-8")

    def prepare_execution_config(self) -> tuple[int, int] | None:
        """Pin the same configured runtime in the private, read-only venv copy.

        The original config is never written. The private overlay is restored
        and checked before candidate verification/publication, after cleanup.
        """
        if self.configured_home is None:
            return None
        current, info = _read_regular(self.config_path, MAX_PYVENV_CFG_BYTES)
        if current != self.original_config:
            _reject("Frozen pyvenv.cfg changed before canonical runtime preparation")
        self.config_path.write_bytes(self._execution_config())
        return info.st_atime_ns, info.st_mtime_ns

    def restore_execution_config(self, times: tuple[int, int] | None):
        if times is None:
            return
        current, _ = _read_regular(self.config_path, MAX_PYVENV_CFG_BYTES)
        if current != self._execution_config():
            _reject("Read-only execution pyvenv.cfg changed; candidate cannot be published")
        self.config_path.write_bytes(self.original_config)
        # _read_regular has just verified every ancestor and the single-link
        # leaf. Windows Python does not support utime(follow_symlinks=False).
        os.utime(self.config_path, ns=times)

    def validate_before_launch(self) -> None:
        """Bind the frozen config to its source and the inspected base runtime."""
        frozen, _ = _read_regular(self.config_path, MAX_PYVENV_CFG_BYTES)
        source, info = _read_regular(self.source_config_path, MAX_PYVENV_CFG_BYTES)
        if (hashlib.sha256(frozen).hexdigest() != self.config_sha256
                or hashlib.sha256(source).hexdigest() != self.config_sha256
                or (info.st_dev, info.st_ino, info.st_size) != self.source_identity):
            _reject("Original or frozen pyvenv.cfg changed before native launch")
        if self.external:
            if self.configured_home is not None:
                if (checked_path(self.configured_home.resolve(strict=True)) != self.home
                        or not self.scope.allows(self.home, write=False)):
                    _reject("Configured Python runtime target changed before native launch")
            current = _ordinary_runtime(self.home, self.scope)
            if current != self.home_fingerprint:
                _reject("Python base runtime changed before native launch")

    def validate_request(self, request: dict) -> None:
        """Check the request builder's exact leaves stay read-only and bounded."""
        read_paths = {os.path.normcase(os.path.abspath(os.fspath(item)))
                      for item in request.get("read_paths", ())}
        write_paths = {os.path.normcase(os.path.abspath(os.fspath(item)))
                       for item in request.get("write_paths", ())}
        home_key = os.path.normcase(os.path.abspath(os.fspath(self.home)))
        if not self.external:
            return
        if home_key not in read_paths:
            _reject("Python base home was not included by the current host read scope")
        required = {"python.exe", "lib/encodings/__init__.py"}
        observed = set()
        for item in request.get("read_paths", ()):
            path = Path(item)
            try:
                relative = path.relative_to(self.home)
            except ValueError:
                # On Windows Path.relative_to is case-insensitive; keep a
                # normcase fallback for synthetic request builders too.
                try:
                    relative = Path(os.path.relpath(path, self.home))
                except ValueError:
                    continue
                if relative == Path("..") or ".." in relative.parts:
                    continue
            if any(part.casefold() in _AUTHORITY_COMPONENTS for part in relative.parts):
                _reject("Python base read scope contains an authority-store subtree")
            lowered = relative.as_posix().casefold()
            if lowered == "python.exe" or lowered == "lib/encodings/__init__.py":
                observed.add(lowered)
            if re.fullmatch(r"python3.*\.dll", relative.name, re.IGNORECASE):
                observed.add("python3*.dll")
        if not required.issubset(observed) or "python3*.dll" not in observed:
            _reject("Python base read scope is missing a required runtime leaf")
        if any(key == home_key or key.startswith(home_key.rstrip("\\/") + os.sep.casefold())
               for key in write_paths):
            _reject("Python base runtime must remain read-only")


def discover_python_runtime(command: str, *, staged_workspace: Path, source_workspace: Path,
                            baseline: tuple, scope, forbidden_roots=()) -> PythonRuntimeDependency | None:
    """Return the one validated base-home dependency for a literal workspace venv."""
    literal = literal_interpreter(command)
    if literal is None:
        if not _PYTHON_ALIAS.match(command):
            return None
        configs = [name for name in (".venv", "venv") if any(
            item.relative.casefold() == name + "/pyvenv.cfg" and item.kind == "file" for item in baseline)]
        if len(configs) != 1:
            raise PythonRuntimeUnavailable(
                "Python requires one frozen workspace venv (.venv or venv). "
                "Create the authorized project environment, or select an explicit existing workspace interpreter; "
                "no global interpreter is substituted. Command not started.")
        literal = configs[0] + "/Scripts/python.exe"
    # Keep the complete bound relative path: an authorized output directory
    # may contain its own venv without granting writes at the project root.
    relative_cfg = Path(literal.replace("\\", "/")).parent.parent / "pyvenv.cfg"
    preparation_marker = relative_cfg.parent / ".mewcode-python-preparing"
    if (any(item.relative.casefold() == preparation_marker.as_posix().casefold() for item in baseline)
            or os.path.lexists(source_workspace / preparation_marker)):
        raise PythonRuntimeUnavailable("Project Python preparation is incomplete. "
            "Inspect the failed preparation before retrying; command not started.")
    source_cfg = source_workspace / relative_cfg
    staged_cfg = staged_workspace / relative_cfg
    entry = next((item for item in baseline if item.relative.casefold() == relative_cfg.as_posix().casefold()), None)
    if entry is None or entry.kind != "file":
        raise PythonRuntimeUnavailable("The selected workspace Python venv has no frozen pyvenv.cfg. "
            "Prepare the authorized project environment separately; command not started.")
    if not scope.current() or not scope.allows(source_cfg, write=False):
        _reject("pyvenv.cfg is not readable under current host scope")
    frozen, _ = _read_regular(staged_cfg, MAX_PYVENV_CFG_BYTES)
    digest = hashlib.sha256(frozen).hexdigest()
    if len(frozen) != entry.size or digest != entry.sha256:
        _reject("Frozen pyvenv.cfg does not match the host snapshot")
    original, original_info = _read_regular(source_cfg, MAX_PYVENV_CFG_BYTES)
    if (hashlib.sha256(original).hexdigest() != digest or len(original) != entry.size
            or (original_info.st_dev, original_info.st_ino) != (entry.device, entry.inode)):
        _reject("Original pyvenv.cfg changed after workspace staging")
    home, _ = _parse_cfg(frozen)
    # A configured external runtime may use a stable installer junction (uv).
    # Resolve it only for this read-only dependency, never for workspace paths
    # or grants. Both configured and canonical targets retain current policy.
    if (any(part.casefold() in _AUTHORITY_COMPONENTS for part in home.parts)
            or home == source_workspace or home.is_relative_to(source_workspace)):
        resolved_home = checked_path(home)
    else:
        if not scope.current() or not scope.allows(home, write=False):
            _reject("Configured Python base home is not readable under current host scope")
        resolved_home = checked_path(home.resolve(strict=True))
    source_root = checked_path(source_workspace)
    if resolved_home == source_root or resolved_home.is_relative_to(source_root):
        # The staged workspace already has exact workspace grants. Do not add
        # a wide mapping to an in-workspace Python base installation.
        return PythonRuntimeDependency(staged_cfg, source_cfg, source_root, resolved_home,
            digest, (original_info.st_dev, original_info.st_ino, original_info.st_size),
            scope, False, None)
    bounded_roots = tuple(Path(root) for root in forbidden_roots)
    resolved_home = _validate_external_home(resolved_home, source=source_root,
                                            scope=scope, forbidden_roots=bounded_roots)
    # The request builder performs the bounded exact-leaf walk and repeats
    # policy, hard-link, stream and reparse checks for the complete tree.
    home_fingerprint = _ordinary_runtime(resolved_home, scope)
    return PythonRuntimeDependency(staged_cfg, source_cfg, source_root, resolved_home,
        digest, (original_info.st_dev, original_info.st_ino, original_info.st_size),
        scope, True, home_fingerprint, home if home != resolved_home else None,
        frozen if home != resolved_home else None)


def merge_extra_read(explicit, derived: tuple[Path, ...]) -> tuple[Path, ...]:
    """Append discovered roots while preserving and deduplicating caller roots."""
    result, seen = [], set()
    for item in (*tuple(explicit), *derived):
        path = Path(item)
        key = os.path.normcase(os.path.abspath(os.fspath(path)))
        if key not in seen:
            seen.add(key)
            result.append(path)
    return tuple(result)
