"""Prepare a small, offline Python environment inside one project directory.

This module intentionally has no package installer. It creates a fresh
``.venv`` with the running CPython and can copy selected installed distributions
plus their applicable dependency closure from sysconfig site-packages.
"""
from __future__ import annotations

import csv
import io
import ntpath
import importlib.metadata
import os
import re
import stat
import sys
import sysconfig
import venv
from email import policy
from email.parser import BytesParser
from pathlib import Path, PurePosixPath, PureWindowsPath


MAX_PACKAGE_FILES = 20_000
MAX_PACKAGE_FILE_BYTES = 128 * 1024 * 1024
MAX_PACKAGE_TOTAL_BYTES = 512 * 1024 * 1024
MAX_REQUESTED_PACKAGES = 32
MAX_RESOLVED_PACKAGES = 128
MAX_RECORD_BYTES = 8 * 1024 * 1024
MAX_METADATA_BYTES = 4 * 1024 * 1024
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_PREPARING_MARKER = ".mewcode-python-preparing"
_DISTRIBUTION_NAME = re.compile(r"^[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*$")


class _PreparationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _base_runtime() -> dict:
    paths = sysconfig.get_paths()
    return {
        "executable": getattr(sys, "_base_executable", None),
        "version": ".".join(str(part) for part in sys.version_info[:3]),
        "implementation": sys.implementation.name,
        "purelib": paths.get("purelib"),
        "platlib": paths.get("platlib"),
    }


def _is_link_or_reparse(info) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _lstat(path: Path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _PreparationError("path_unavailable", f"Cannot inspect {path}: {exc}") from exc


def _root_path(root) -> Path:
    if not isinstance(root, (str, os.PathLike)):
        raise _PreparationError("invalid_root", "Project root must be a filesystem path.")
    path = Path(os.path.abspath(os.fspath(root)))
    _check_path_no_links(path)
    info = _lstat(path)
    if info is None or not stat.S_ISDIR(info.st_mode) or _is_link_or_reparse(info):
        raise _PreparationError("invalid_root", "Project root must be an existing ordinary directory.")
    return path


def _entry_state(root: Path, name: str) -> dict:
    path = root / name
    info = _lstat(path)
    rel_interpreter = _relative_interpreter(name)
    if info is None:
        return {"path": name, "exists": False, "state": "missing", "interpreter": None}
    if _is_link_or_reparse(info):
        return {"path": name, "exists": True, "state": "unsafe", "interpreter": None,
                "error": "Environment directory is a link or reparse point."}
    if not stat.S_ISDIR(info.st_mode):
        return {"path": name, "exists": True, "state": "invalid", "interpreter": None,
                "error": "Environment path is not a directory."}
    try:
        marker = path / _PREPARING_MARKER
        _check_no_links(path, marker)
        marker_info = _lstat(marker)
        if marker_info is not None:
            if not stat.S_ISREG(marker_info.st_mode) or _is_link_or_reparse(marker_info):
                return {"path": name, "exists": True, "state": "unsafe", "interpreter": None,
                        "error": "Environment preparation marker is unsafe."}
            return {"path": name, "exists": True, "state": "incomplete", "interpreter": None,
                    "error": "Environment preparation did not finish; validation is still required."}
        cfg = path / "pyvenv.cfg"
        interpreter = root / Path(*PurePosixPath(rel_interpreter).parts)
        _check_no_links(path, cfg)
        _check_no_links(path, interpreter)
        cfg_info, exe_info = _lstat(cfg), _lstat(interpreter)
        if (cfg_info is None or not stat.S_ISREG(cfg_info.st_mode)
                or exe_info is None or not stat.S_ISREG(exe_info.st_mode)):
            return {"path": name, "exists": True, "state": "incomplete", "interpreter": None,
                    "error": "Environment needs pyvenv.cfg and its interpreter."}
    except _PreparationError as exc:
        return {"path": name, "exists": True, "state": "unsafe", "interpreter": None,
                "error": str(exc)}
    return {"path": name, "exists": True, "state": "available", "interpreter": rel_interpreter}


def _relative_interpreter(name: str) -> str:
    suffix = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    return (Path(name) / Path(suffix)).as_posix()


def _check_no_links(root: Path, path: Path) -> None:
    """Reject links/reparse points in every component below root."""
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise _PreparationError("path_escape", "Path escaped the project environment.") from exc
    current = root
    for part in relative.parts:
        current = current / part
        info = _lstat(current)
        if info is None:
            return
        if _is_link_or_reparse(info):
            raise _PreparationError("unsafe_link", f"Link or reparse point is not allowed: {current}")


def _check_path_no_links(path: Path) -> None:
    """Reject links/reparse points from the filesystem anchor through path."""
    path = Path(os.path.abspath(path))
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        info = _lstat(current)
        if info is not None and _is_link_or_reparse(info):
            raise _PreparationError("unsafe_link", f"Link or reparse point is not allowed: {current}")


def inspect_project_python(root) -> dict:
    """Inspect conventional project environments without executing Python."""
    result = {"ok": False, "available": False, "status": "invalid", "interpreter": None,
              "validation_required": True, "base_runtime": _base_runtime(),
              "environments": {}}
    try:
        project = _root_path(root)
        entries = {name: _entry_state(project, name) for name in (".venv", "venv")}
        result["environments"] = entries
        present = [entry for entry in entries.values() if entry["exists"]]
        if not present:
            status = "missing"
        elif len(present) > 1:
            status = "ambiguous"
        elif present[0]["state"] == "available":
            status = "available"
            result["interpreter"] = present[0]["interpreter"]
        else:
            status = "invalid"
        result.update(ok=True, status=status, available=(status == "available"))
    except _PreparationError as exc:
        result["error"] = {"code": exc.code, "message": str(exc)}
    return result


def _valid_relative_name(raw: str) -> tuple[str, ...]:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise _PreparationError("unsafe_package_path", "Package contains an invalid file path.")
    normalized = raw.replace("\\", "/")
    posix, windows = PurePosixPath(normalized), PureWindowsPath(normalized)
    parts = normalized.split("/")
    if (posix.is_absolute() or windows.is_absolute() or windows.drive
            or any(part in {"..", ".", ""} for part in parts)
            or any(":" in part or any(ord(character) < 32 for character in part) for part in parts)):
        raise _PreparationError("unsafe_package_path", f"Package path escapes site-packages: {raw}")
    return tuple(parts)


def _is_sibling_scripts_entry(raw: str) -> bool:
    """Recognize pip's known external ``../Scripts`` RECORD entries to omit."""
    normalized = raw.replace("\\", "/")
    posix, windows = PurePosixPath(normalized), PureWindowsPath(normalized)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        return False
    parts = posix.parts
    index = 0
    while index < len(parts) and parts[index] == "..":
        index += 1
    return (index > 0 and index < len(parts) - 1 and parts[index].lower() == "scripts"
            and all(part not in {".", "..", ""} for part in parts[index:]))


def _site_roots() -> list[Path]:
    paths = sysconfig.get_paths()
    roots, seen = [], set()
    for key in ("purelib", "platlib"):
        value = paths.get(key)
        if not value:
            continue
        path = Path(os.path.abspath(value))
        normalized = os.path.normcase(str(path))
        if normalized not in seen:
            roots.append(path)
            seen.add(normalized)
    return roots


def _canonical_distribution_name(name: str) -> str:
    if (not isinstance(name, str) or len(name) > 200
            or not _DISTRIBUTION_NAME.fullmatch(name)):
        raise _PreparationError("invalid_package_name", "Package names must be plain distribution names (letters, digits, dots, underscores, or hyphens).")
    return re.sub(r"[-_.]+", "-", name).lower()


def _ensure_plain_directory(path: Path) -> None:
    _check_path_no_links(path)
    info = _lstat(path)
    if info is None or not stat.S_ISDIR(info.st_mode) or _is_link_or_reparse(info):
        raise _PreparationError("unsafe_package_source", f"Package source directory is missing or unsafe: {path}")


def _validate_source_file(root: Path, relative: tuple[str, ...]) -> tuple[Path, int]:
    source = root.joinpath(*relative)
    _check_no_links(root, source)
    info = _lstat(source)
    if info is None or _is_link_or_reparse(info) or not stat.S_ISREG(info.st_mode):
        raise _PreparationError("unsafe_package_source", f"Package source is not a regular file: {source}")
    try:
        source.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise _PreparationError("unsafe_package_source", f"Package source escapes site-packages: {source}") from exc
    size = info.st_size
    if size < 0 or size > MAX_PACKAGE_FILE_BYTES:
        raise _PreparationError("package_too_large", f"Package file exceeds the per-file limit: {source}")
    return source, size


def _read_safe_bytes(root: Path, relative: tuple[str, ...], limit: int, label: str) -> bytes:
    source, size = _validate_source_file(root, relative)
    if size > limit:
        raise _PreparationError("package_metadata_invalid", f"{label} exceeds its size limit.")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(source, flags)
    except OSError as exc:
        raise _PreparationError("unsafe_package_source", f"Cannot read {label}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or _is_link_or_reparse(info) or info.st_size != size:
            raise _PreparationError("unsafe_package_source", f"{label} changed while it was being inspected.")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            value = stream.read(limit + 1)
            if len(value) > limit or stream.read(1):
                raise _PreparationError("package_metadata_invalid", f"{label} exceeds its size limit.")
            return value
    except OSError as exc:
        raise _PreparationError("package_metadata_invalid", f"Cannot read {label}: {exc}") from exc
    finally:
        os.close(fd)


def _excluded_package_path(parts: tuple[str, ...]) -> bool:
    lowered = tuple(part.lower() for part in parts)
    filename = lowered[-1]
    return ("__pycache__" in lowered or lowered[0] in {"scripts", "bin"}
            or any(part in {".cache", ".pytest_cache"} for part in lowered)
            or filename.endswith((".pyc", ".pyo", ".pth")))


def _distribution_records() -> dict[str, list[dict]]:
    """Read inert metadata only from ordinary sysconfig purelib/platlib roots."""
    index: dict[str, list[dict]] = {}
    for root in _site_roots():
        if _lstat(root) is None:
            continue
        _ensure_plain_directory(root)
        try:
            candidates = importlib.metadata.distributions(path=[str(root)])
        except Exception as exc:
            raise _PreparationError("package_metadata_invalid", f"Cannot inspect host distribution metadata: {exc}") from exc
        for distribution in candidates:
            dist_info = Path(getattr(distribution, "_path", "")).absolute()
            try:
                relative_dir = dist_info.relative_to(root)
            except ValueError:
                continue
            if len(relative_dir.parts) != 1 or not relative_dir.name.lower().endswith(".dist-info"):
                continue
            _ensure_plain_directory(dist_info)
            dist_info_parts = _valid_relative_name(relative_dir.as_posix())
            metadata_parts = (*dist_info_parts, "METADATA")
            try:
                metadata_bytes = _read_safe_bytes(root, metadata_parts, MAX_METADATA_BYTES,
                                                  f"{relative_dir.name}/METADATA")
                metadata = BytesParser(policy=policy.compat32).parsebytes(metadata_bytes)
            except UnicodeError as exc:
                raise _PreparationError("package_metadata_invalid", f"{relative_dir.name}/METADATA is not valid text.") from exc
            if metadata.defects:
                raise _PreparationError("package_metadata_invalid", f"{relative_dir.name}/METADATA is malformed.")
            raw_name = metadata.get("Name")
            if not isinstance(raw_name, str) or not _DISTRIBUTION_NAME.fullmatch(raw_name):
                continue
            name = re.sub(r"[-_.]+", "-", raw_name).lower()
            version = metadata.get("Version")
            if not isinstance(version, str) or not version.strip():
                raise _PreparationError("package_metadata_invalid", f"{relative_dir.name}/METADATA has no Version.")
            index.setdefault(name, []).append({
                "name": name, "version": version.strip(), "requires_python": metadata.get("Requires-Python"),
                "requires_dist": metadata.get_all("Requires-Dist", []), "root": root,
                "dist_info_parts": dist_info_parts,
            })
    return index


def _distribution_files(item: dict) -> list[tuple[tuple[str, ...], Path, int]]:
    root = item["root"]
    dist_info_parts = item["dist_info_parts"]
    record_parts = (*dist_info_parts, "RECORD")
    record_bytes = _read_safe_bytes(root, record_parts, MAX_RECORD_BYTES,
                                    f"{dist_info_parts[0]}/RECORD")
    try:
        record_text = record_bytes.decode("utf-8")
        rows = list(csv.reader(io.StringIO(record_text, newline="")))
    except (UnicodeError, csv.Error) as exc:
        raise _PreparationError("package_metadata_invalid", f"{dist_info_parts[0]}/RECORD is malformed.") from exc
    if not rows or len(rows) > MAX_PACKAGE_FILES:
        raise _PreparationError("package_too_large", f"{dist_info_parts[0]}/RECORD has an invalid or excessive file count.")

    manifest: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for row in rows:
        if len(row) != 3 or not row[0]:
            raise _PreparationError("package_metadata_invalid", f"{dist_info_parts[0]}/RECORD contains an invalid row.")
        raw = row[0].replace("\\", "/")
        if _is_sibling_scripts_entry(raw):
            continue
        parts = _valid_relative_name(raw)
        key = _destination_key(parts)
        if key in seen:
            raise _PreparationError("package_metadata_invalid", f"{dist_info_parts[0]}/RECORD lists a file more than once.")
        seen.add(key)
        if (any(part.lower().endswith(".dist-info") for part in parts)
                and parts[0] != dist_info_parts[0]):
            raise _PreparationError("package_metadata_invalid", f"{dist_info_parts[0]}/RECORD points into another distribution's metadata.")
        if not _excluded_package_path(parts):
            manifest.append(parts)
    if _destination_key(record_parts) not in seen:
        raise _PreparationError("package_metadata_invalid", f"{dist_info_parts[0]}/RECORD omits itself.")
    if _destination_key((*dist_info_parts, "METADATA")) not in seen:
        raise _PreparationError("package_metadata_invalid", f"{dist_info_parts[0]}/RECORD omits METADATA.")

    selected = []
    total = 0
    for parts in manifest:
        source, size = _validate_source_file(root, parts)
        total += size
        if total > MAX_PACKAGE_TOTAL_BYTES:
            raise _PreparationError("package_too_large", f"{item['name']} exceeds the per-distribution copy size limit.")
        selected.append((parts, source, size))
    if not selected:
        raise _PreparationError("package_metadata_invalid", f"No safe package files were found for {item['name']}.")
    return selected


def _plan_packages(requested: tuple[str, ...]) -> dict[str, dict]:
    try:
        from packaging.markers import default_environment
        from packaging.requirements import InvalidRequirement, Requirement
        from packaging.specifiers import InvalidSpecifier, SpecifierSet
        from packaging.version import InvalidVersion, Version
    except ImportError as exc:
        raise _PreparationError("metadata_parser_unavailable", "The host packaging library is required to read dependency metadata; nothing was created.") from exc

    index = _distribution_records()
    resolved: dict[str, dict] = {}
    visiting: set[str] = set()
    marker_environment = default_environment()
    marker_environment["extra"] = ""
    python_version = ".".join(str(part) for part in sys.version_info[:3])

    def resolve(name: str, specifier=None, parent: str = ""):
        candidates = index.get(name, [])
        if not candidates:
            code = "package_not_installed" if not parent else "dependency_not_installed"
            subject = f"Package {name}" if not parent else f"Dependency {name} required by {parent}"
            raise _PreparationError(code, f"{subject} is not installed in host sysconfig site-packages.")
        if len(candidates) != 1:
            raise _PreparationError("package_ambiguous", f"More than one host distribution named {name} was found.")
        item = candidates[0]
        try:
            version = Version(item["version"])
        except InvalidVersion as exc:
            raise _PreparationError("package_metadata_invalid", f"Host distribution {name} has an invalid version.") from exc
        if specifier and not specifier.contains(version, prereleases=True):
            raise _PreparationError("dependency_version_mismatch",
                                    f"{name} {version} does not satisfy required version {specifier}"
                                    + (f" from {parent}." if parent else "."))
        if name in resolved or name in visiting:
            return
        if len(resolved) >= MAX_RESOLVED_PACKAGES:
            raise _PreparationError("package_too_large", f"Dependency closure exceeds {MAX_RESOLVED_PACKAGES} distributions.")
        visiting.add(name)
        requires_python = item.get("requires_python")
        if requires_python:
            try:
                python_spec = SpecifierSet(requires_python)
            except InvalidSpecifier as exc:
                raise _PreparationError("package_metadata_invalid", f"{name} has an invalid Requires-Python value.") from exc
            if not python_spec.contains(python_version, prereleases=True):
                raise _PreparationError("python_version_mismatch",
                                        f"Host Python {python_version} does not satisfy {name} Requires-Python {requires_python}.")
        requirements = []
        if len(item["requires_dist"]) > 4096:
            raise _PreparationError("package_too_large", f"{name} has too many Requires-Dist entries.")
        for raw_requirement in item["requires_dist"]:
            try:
                requirement = Requirement(raw_requirement)
            except InvalidRequirement as exc:
                raise _PreparationError("package_metadata_invalid", f"{name} has an invalid Requires-Dist entry.") from exc
            if requirement.marker:
                try:
                    if not requirement.marker.evaluate(environment=marker_environment):
                        continue
                except (KeyError, TypeError, ValueError) as exc:
                    raise _PreparationError("package_metadata_invalid", f"{name} has an unsupported dependency marker.") from exc
            if requirement.url:
                raise _PreparationError("unsupported_dependency", f"{name} requires direct URL dependency {requirement.name}; offline path preparation does not fetch or resolve URLs.")
            if requirement.extras:
                extras = ", ".join(sorted(requirement.extras))
                raise _PreparationError("unsupported_extra", f"{name} requires extras [{extras}] for {requirement.name}; package extras are not supported.")
            try:
                dependency = _canonical_distribution_name(requirement.name)
            except _PreparationError as exc:
                raise _PreparationError("package_metadata_invalid", f"{name} has an invalid dependency name.") from exc
            requirements.append((dependency, requirement.specifier, name))
        resolved[name] = item
        for dependency, dependency_specifier, source_name in requirements:
            resolve(dependency, dependency_specifier, source_name)
        visiting.remove(name)

    for name in requested:
        resolve(name)

    plan: dict[str, dict] = {}
    destination_owners: dict[tuple[str, ...], str] = {}
    total_files = total_bytes = 0
    for name in sorted(resolved):
        item = resolved[name]
        files = _distribution_files(item)
        for parts, source, size in files:
            key = _destination_key(parts)
            previous = destination_owners.get(key)
            if previous is not None:
                raise _PreparationError("package_file_conflict", f"Distributions {previous} and {name} both own {PurePosixPath(*parts)}.")
            destination_owners[key] = name
            total_files += 1
            total_bytes += size
            if total_files > MAX_PACKAGE_FILES or total_bytes > MAX_PACKAGE_TOTAL_BYTES:
                raise _PreparationError("package_too_large", "Dependency closure exceeds the total file or copy size limit.")
        plan[name] = {"version": item["version"], "source_root": item["root"], "files": files}
    return plan


def _destination_key(parts: tuple[str, ...], *, windows: bool | None = None) -> tuple[str, ...]:
    """Reject Windows aliases before creating an environment or copying files."""
    if windows is None:
        windows = os.name == "nt"
    if not windows:
        return parts
    reserved = getattr(ntpath, "isreserved", None)
    for part in parts:
        if part.rstrip(" .") != part or (reserved(part) if reserved else PureWindowsPath(part).is_reserved()):
            raise _PreparationError("package_metadata_invalid", "RECORD contains a non-canonical Windows filename.")
    return tuple(part.casefold() for part in parts)


def _destination_site_packages(env_root: Path) -> Path:
    variables = {"base": str(env_root), "platbase": str(env_root), "prefix": str(env_root),
                 "exec_prefix": str(env_root), "installed_base": str(env_root),
                 "installed_platbase": str(env_root)}
    destination = Path(sysconfig.get_path("purelib", vars=variables))
    try:
        destination.absolute().relative_to(env_root.absolute())
    except ValueError as exc:
        raise _PreparationError("unsafe_environment_path", "Host sysconfig produced a site-packages path outside .venv.") from exc
    return destination


def _copy_file(source_root: Path, source: Path, destination: Path, expected_size: int) -> None:
    # Recheck source links immediately before opening; O_NOFOLLOW is used where available.
    _check_no_links(source_root, source)
    source_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(source, source_flags)
    except OSError as exc:
        raise _PreparationError("unsafe_package_source", f"Cannot open package source {source}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or _is_link_or_reparse(info) or info.st_size != expected_size:
            raise _PreparationError("unsafe_package_source", f"Package source changed during preparation: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(fd, "rb", closefd=False) as src, destination.open("xb") as dst:
            remaining = expected_size
            while remaining:
                chunk = src.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                dst.write(chunk)
                remaining -= len(chunk)
            if remaining or src.read(1):
                raise _PreparationError("unsafe_package_source", f"Package source size changed during copy: {source}")
        destination_info = _lstat(destination)
        if (destination_info is None or not stat.S_ISREG(destination_info.st_mode)
                or _is_link_or_reparse(destination_info) or destination_info.st_size != expected_size):
            raise _PreparationError("copy_failed", f"Copied package file has the wrong size: {destination}")
    except OSError as exc:
        raise _PreparationError("copy_failed", f"Cannot copy package file to {destination}: {exc}") from exc
    finally:
        os.close(fd)


def _failure(code: str, message: str, *, base_runtime=None, created=False) -> dict:
    return {"ok": False, "prepared": False, "ready": False, "available": False,
            "status": "invalid", "created": created,
            "environment": ".venv" if created else None,
            "interpreter": _relative_interpreter(".venv") if created else None,
            "validation_required": True, "base_runtime": base_runtime or _base_runtime(),
            "packages": {}, "error": {"code": code, "message": message}}


def prepare_project_python(root, packages=()) -> dict:
    """Create a fresh .venv and copy a requested host-installed dependency closure offline.

    No command, shell, package installer, package import, network request, or
    executable validation is performed. Existing .venv/venv paths are never
    changed. The complete metadata and file plan is validated before creating
    the environment. Failed new environments are left in place and reported.
    """
    base = _base_runtime()
    if not isinstance(packages, (tuple, list, set, frozenset)):
        return _failure("invalid_packages", "packages must be a list, tuple, or set of distribution names.", base_runtime=base)
    if len(packages) > MAX_REQUESTED_PACKAGES:
        return _failure("invalid_packages", f"At most {MAX_REQUESTED_PACKAGES} packages may be requested.", base_runtime=base)
    try:
        requested = tuple(sorted(_canonical_distribution_name(name) for name in packages))
    except _PreparationError as exc:
        return _failure(exc.code, str(exc), base_runtime=base)
    if len(set(requested)) != len(requested):
        return _failure("duplicate_package", "packages must not contain duplicate distribution names.", base_runtime=base)

    try:
        project = _root_path(root)
        current = inspect_project_python(project)
        if not current.get("ok"):
            error = current.get("error") or {}
            return _failure(error.get("code", "invalid_root"), error.get("message", "Invalid project root."), base_runtime=base)
        if current["status"] != "missing":
            return _failure("environment_exists", "An existing .venv or venv path is present; it was left unchanged.", base_runtime=base)

        package_plan = _plan_packages(requested) if requested else {}

        target = project / ".venv"
        try:
            os.mkdir(target)
        except FileExistsError as exc:
            raise _PreparationError("environment_exists", "The .venv target appeared concurrently and was left unchanged.") from exc
        except OSError as exc:
            raise _PreparationError("environment_create_failed", f"Cannot reserve .venv: {exc}") from exc
        created = True
        _check_no_links(project, target)
        if not stat.S_ISDIR(_lstat(target).st_mode):
            raise _PreparationError("unsafe_environment_path", ".venv is not an ordinary directory.")
        marker = target / _PREPARING_MARKER
        marker_fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(marker_fd, "wb") as marker_file:
            marker_file.write(b"Project Python preparation in progress.\n")

        builder = venv.EnvBuilder(with_pip=False, symlinks=False)
        builder.create(str(target))
        _check_no_links(project, target)
        interpreter = project / Path(*PurePosixPath(_relative_interpreter(".venv")).parts)
        cfg = target / "pyvenv.cfg"
        for required in (cfg, interpreter):
            _check_no_links(target, required)
            info = _lstat(required)
            if info is None or not stat.S_ISREG(info.st_mode) or _is_link_or_reparse(info):
                raise _PreparationError("environment_create_failed", f"venv did not create required file: {required.name}")

        copied = {}
        if package_plan:
            site_packages = _destination_site_packages(target)
            _check_no_links(project, target)
            _check_no_links(target, site_packages)
            site_packages.mkdir(parents=True, exist_ok=True)
            for package, item in package_plan.items():
                count, byte_count = 0, 0
                for relative, source, size in item["files"]:
                    destination = site_packages.joinpath(*relative)
                    try:
                        destination.relative_to(target)
                    except ValueError as exc:
                        raise _PreparationError("unsafe_environment_path", "Package destination escaped .venv.") from exc
                    _check_no_links(target, destination.parent)
                    _copy_file(item["source_root"], source, destination, size)
                    count += 1
                    byte_count += size
                copied[package] = {"version": item["version"], "copied_files": count, "copied_bytes": byte_count}

        _check_no_links(target, marker)
        marker_info = _lstat(marker)
        if marker_info is None or not stat.S_ISREG(marker_info.st_mode) or _is_link_or_reparse(marker_info):
            raise _PreparationError("environment_create_failed", "Preparation marker changed before completion.")
        marker.unlink()

        return {"ok": True, "prepared": True, "ready": False, "available": True,
                "status": "available", "created": True,
                "environment": ".venv", "interpreter": _relative_interpreter(".venv"),
                "base_runtime": base, "packages": copied, "validation_required": True}
    except _PreparationError as exc:
        return _failure(exc.code, str(exc), base_runtime=base, created=locals().get("created", False))
    except Exception as exc:
        # Preserve a partially-created directory for review; never claim success.
        return _failure("preparation_failed", f"Project Python preparation failed: {exc}",
                        base_runtime=base, created=locals().get("created", False))
