"""Host-scoped Windows LPAC transport; activation is a separate closed gate.

The launcher reads its request from a private stdin pipe, not a writable JSON
file. Child output is nested inside the host receipt and is never interpreted
as a receipt. No subprocess, provider, Docker or shell fallback is available.
"""
from __future__ import annotations

import asyncio
import ctypes
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
from typing import Any
import uuid

from mewcode.sandbox.receipt import NativeExecutionReceipt
from mewcode.sandbox.powershell_compat import remove_item_wrapper
from mewcode.sandbox.windows import NativeBoundaryUnavailable
from mewcode.sandbox import execution_progress

BACKEND = "mewcode-windows-lpac"
CAPABILITIES = ["registryRead", "lpacInstrumentation", "lpacCom"]
MAX_PATHS = 12000
MAX_REQUEST = 1024 * 1024
MAX_RECEIPT = 8 * 1024 * 1024
_AUTHORITY = {".mewcode", ".codex", ".agents"}
_BUNDLE_SUBTREES = {"git", "broker"}
_GIT_METADATA = ".git"
# Tool runtime trees stay read-only. The helper's grants are exact and never
# inherited, so every required directory/file must be enumerated explicitly.
_OPAQUE_TOOL_DIRS = frozenset({".venv", "venv"})
# Repository-control surfaces stay read-only even with an explicit git_write
# task behavior: hooks execute on later host-side Git runs, and config can
# redirect hooksPath, aliases and URL rewriting. Content-addressed objects,
# refs, the index and logs are the only writable .git payload.
_GIT_CONTROL = {"hooks", "config", "config.worktree", "info", "modules", "description"}
GIT_WRITE_BEHAVIOR = "git_write"
GIT_CAPTURE_SLOTS = 32


def checked_path(value: Path, *, file: bool = False) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or any(":" in p for p in path.parts[1:]):
        raise NativeBoundaryUnavailable("A native scope needs absolute paths without traversal or streams")
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise NativeBoundaryUnavailable(f"Native scope contains a reparse path: {part}")
    info = path.stat()
    if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
        raise NativeBoundaryUnavailable(f"Native scope contains a hard-linked file: {path}")
    if not (stat.S_ISREG(info.st_mode) if file else (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))):
        raise NativeBoundaryUnavailable(f"Unsupported native scope object: {path}")
    return path.resolve(strict=True)


@dataclass(frozen=True)
class NativeScope:
    """Host policy reference. It is never constructed from tool arguments."""
    checker: Any
    workspace: Path
    task_id: str
    mode: Any
    publication_mode: str = "publish"
    independent_offline: bool = False

    @classmethod
    def from_checker(cls, checker: Any, *, publication_mode: str | None = None,
                     independent_offline: bool = False) -> NativeScope:
        if checker is None:
            raise NativeBoundaryUnavailable("Native execution needs a host permission scope")
        from mewcode.sandbox.task_identity import require_task_identity
        try:
            task_id = require_task_identity(getattr(checker, "task_id", ""))
        except ValueError as exc:
            raise NativeBoundaryUnavailable(str(exc)) from exc
        root = checked_path(Path(checker.sandbox.project_root))
        if root == Path(root.anchor) or root == Path.home().resolve():
            raise NativeBoundaryUnavailable("A home or volume root is not a bounded workspace")
        host_mode = getattr(checker, "native_publication_mode", "publish")
        selected = publication_mode or host_mode
        if host_mode not in {"publish", "discard"} or selected not in {"publish", "discard"}:
            raise NativeBoundaryUnavailable("Invalid host publication mode")
        if host_mode == "discard" and selected != "discard":
            raise NativeBoundaryUnavailable("A read-only host publication scope cannot be upgraded")
        if independent_offline and selected != "discard":
            raise NativeBoundaryUnavailable("Independent offline work must use a disposable copy")
        return cls(checker, root, task_id, checker.mode, selected, independent_offline)

    def current(self) -> bool:
        host_mode = getattr(self.checker, "native_publication_mode", "publish")
        return (host_mode in {"publish", "discard"}
                and (not self.independent_offline or self.publication_mode == "discard")
                and (host_mode != "discard" or self.publication_mode == "discard")
                and self.checker.task_id == self.task_id and self.checker.mode == self.mode
                and Path(self.checker.sandbox.project_root).resolve() == self.workspace)

    def allows(self, path: Path, *, write: bool) -> bool:
        if not self.current():
            return False
        relative = path.relative_to(self.workspace) if path.is_relative_to(self.workspace) else None
        # The running process cannot edit its authority or receipt stores.
        if relative is not None and any(part.casefold() in _AUTHORITY for part in relative.parts):
            return False
        if write and relative is not None and not self._allows_git_payload(relative):
            return False
        from mewcode.tools.read_file import ReadFile
        from mewcode.tools.write_file import WriteFile
        tool = WriteFile() if write else ReadFile()
        args = {"file_path": str(path)}
        if write:
            args["content"] = ""
        return self.checker.check(tool, args).effect == "allow"

    def _allows_git_payload(self, relative: Path) -> bool:
        """``.git`` writes need an explicit git_write behavior; control files never."""
        parts = [part.casefold() for part in relative.parts]
        if _GIT_METADATA not in parts:
            return True
        index = parts.index(_GIT_METADATA)
        # The .git entry itself (directory grant or worktree pointer file) is
        # never writable: a directory grant would cover hooks, and rewriting
        # the pointer redirects the metadata directory.
        if index == len(parts) - 1:
            return False
        if any(part in _GIT_CONTROL for part in parts[index + 1:]):
            return False
        authorization = getattr(self.checker, "task_authorization", None)
        if authorization is None or getattr(authorization, "task_id", None) != self.task_id:
            return False
        if GIT_WRITE_BEHAVIOR not in getattr(authorization, "allowed_behaviors", ()):
            return False
        import time
        return time.time() < getattr(authorization, "expires_at", 0)


class HostCancelEvent:
    def __init__(self):
        if os.name != "nt":
            raise NativeBoundaryUnavailable("LPAC is Windows-only")
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
        self.api.CreateEventW.restype = ctypes.c_void_p
        self.api.SetEvent.argtypes = [ctypes.c_void_p]
        self.api.SetEvent.restype = ctypes.c_int
        self.api.CloseHandle.argtypes = [ctypes.c_void_p]
        self.api.CloseHandle.restype = ctypes.c_int
        self.name = "MewCodeProbeCancel_" + uuid.uuid4().hex
        self.handle = self.api.CreateEventW(None, True, False, self.name)
        error = ctypes.get_last_error()
        if not self.handle:
            raise ctypes.WinError(error)
        if error == 183:
            self.close()
            raise NativeBoundaryUnavailable("Refusing an existing cancellation object")

    def signal(self):
        if not self.handle or not self.api.SetEvent(self.handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            handle, self.handle = self.handle, None
            if not self.api.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())


def validate_bundle_files(root: Path, files: object) -> None:
    """Validate the optional extra-payload map of a native bundle manifest.

    ``files`` is ``None`` for legacy helper-only bundles; any present map must
    be complete and byte-exact. This is the single authority for what an
    installed bundle may carry beyond the helper itself.
    """
    if files is None:
        return
    if not isinstance(files, dict) or not files:
        raise NativeBoundaryUnavailable("Native helper manifest has an invalid files map")
    for relative, digest in files.items():
        if (not isinstance(relative, str) or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise NativeBoundaryUnavailable("Native bundle entry is malformed")
        parts = PurePosixPath(relative).parts
        if (not relative or PurePosixPath(relative).is_absolute() or ".." in parts
                or any(":" in part for part in parts) or len(parts) < 2
                or parts[0] not in _BUNDLE_SUBTREES
                or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", part) for part in parts)):
            raise NativeBoundaryUnavailable("Native bundle entry path is not allowed")
        path = checked_path(root.joinpath(*parts), file=True)
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise NativeBoundaryUnavailable("Native bundle artifact drift")


def find_runtime_helper(*, require_protocol: str | None = None) -> Path:
    """Installed artifact only. Never resolve a helper from PATH or tool input."""
    from mewcode.runtime_paths import user_data_root
    try:
        root = checked_path(user_data_root() / "native" / "windows-lpac-v1")
    except OSError as exc:
        # A missing bundle directory is a distinct, typed "not installed"
        # state; consumers must be able to tell it apart from tampering.
        raise NativeBoundaryUnavailable("Native runtime bundle is not installed") from exc
    manifest = checked_path(root / "manifest.json", file=True)
    if manifest.stat().st_size > 16384:
        raise NativeBoundaryUnavailable("Native helper manifest is oversized")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    helper = checked_path(root / "MewCode.AppContainerRuntime.exe", file=True)
    if (data.get("schema") != "mewcode-lpac-bundle-v1" or
            data.get("sha256") != hashlib.sha256(helper.read_bytes()).hexdigest()):
        raise NativeBoundaryUnavailable("Native helper bundle identity mismatch")
    validate_bundle_files(root, data.get("files"))
    if require_protocol is not None:
        protocols = data.get("protocols")
        if not isinstance(protocols, list) or require_protocol not in protocols or any(type(p) is not str for p in protocols):
            raise NativeBoundaryUnavailable("Installed runtime does not support staged publication")
    return helper


def _oem_python_encoding() -> str:
    """The child converges on the system OEM page; pin Python to it by name."""
    if os.name != "nt":
        return "utf-8"
    return f"cp{ctypes.windll.kernel32.GetOEMCP()}"


def _git_capture_wrapper(git_exe: Path, capture_dir: Path | None = None) -> str:
    """A PowerShell function that keeps Git's UTF-8 bytes lossless.

    Git for Windows always writes UTF-8 to pipes, but Windows PowerShell 5
    decodes native output in the OEM page and its OutputEncoding setter is
    blocked in ConstrainedLanguage. Start-Process redirection captures raw
    bytes instead, and Get-Content -Encoding UTF8 re-reads them losslessly;
    PowerShell then re-emits them in the OEM page like everything else.
    """
    literal = str(git_exe).replace("'", "''")
    if capture_dir is None:
        allocate = ("$cap = Join-Path $env:TEMP ('mewgit-' + [guid]::NewGuid().ToString('N') + '.out'); "
                    "$err = $cap + '.err'; $infile = $cap + '.stdin'; "
                    "New-Item -ItemType File -Path $infile | Out-Null; ")
    else:
        prefix = str(capture_dir / "git-").replace("'", "''")
        allocate = (
            f"if ($global:MewGitCaptureSlot -ge {GIT_CAPTURE_SLOTS}) {{ throw 'Git capture capacity exhausted; split the command' }}; "
            f"$base = '{prefix}' + ([int]$global:MewGitCaptureSlot).ToString('D3'); "
            "$global:MewGitCaptureSlot++; $cap = $base + '.out'; $err = $base + '.err'; "
            "$infile = $base + '.stdin'; ")
    return (
        "function global:git { "
        + allocate +
        # Windows argv quoting: double backslashes before a quote AND before
        # the closing quote. A trailing slash must not escape that quote and
        # swallow the following argument (e.g. git -C 'a directory\\').
        r'''$argv = @($args) | ForEach-Object { '"' + (("$_" -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"' }; '''
        f"$p = Start-Process -PassThru -NoNewWindow -Wait -FilePath '{literal}' "
        "-WorkingDirectory (Get-Location).ProviderPath "
        "-ArgumentList ($argv -join ' ') -RedirectStandardInput $infile "
        "-RedirectStandardOutput $cap -RedirectStandardError $err; "
        # Scratch belongs to the private host run. Child delete rights are
        # deliberately absent; host retention/GC owns capture cleanup.
        "if ($null -eq $p.ExitCode) { throw 'Git exit state was not captured' }; "
        "if (-not (Test-Path -LiteralPath $cap)) { throw 'Git stdout capture is missing' }; "
        "if (Test-Path -LiteralPath $cap) { Get-Content -Raw -Encoding UTF8 -LiteralPath $cap }; "
        "if ((Test-Path -LiteralPath $err) -and (Get-Item -LiteralPath $err).Length -gt 0) { "
        "Write-Error -Message (Get-Content -Raw -Encoding UTF8 -LiteralPath $err) -ErrorAction Continue; "
        "}; "
        "$global:LASTEXITCODE = $p.ExitCode "
        "}; "
    )


def build_runtime_request(command: str, scope: NativeScope, runtime: Path, *, timeout: int,
                          cancel_name: str, git_dir: Path | None = None,
                          extra_read: tuple[Path, ...] = (), python_executable: Path | None = None) -> dict:
    if not command.strip() or "\0" in command or type(timeout) is not int or not 1 <= timeout <= 600:
        raise NativeBoundaryUnavailable("Invalid command or bounded timeout")
    if not scope.current():
        raise NativeBoundaryUnavailable("Native host scope changed before execution")
    root = checked_path(scope.workspace)
    runtime = checked_path(runtime)
    if runtime == root or runtime.is_relative_to(root) or root.is_relative_to(runtime):
        raise NativeBoundaryUnavailable("Host runtime state must be separate from the command workspace")
    if git_dir is not None:
        git_dir = checked_path(git_dir)
        checked_path(git_dir / "git.exe", file=True)
        if git_dir == root or git_dir.is_relative_to(root) or root.is_relative_to(git_dir):
            raise NativeBoundaryUnavailable("The bundled Git must stay outside the command workspace")
    extra_read_dirs: list[Path] = []
    for item in extra_read:
        # Host-side tool runtimes only (e.g. the project Python base install):
        # an exact existing directory, never a volume root, a home directory,
        # the workspace itself, or an authority store. Leaves are enumerated
        # below: granting the directory alone cannot authorize its children.
        resolved = checked_path(Path(item))
        if not resolved.is_dir() or resolved == Path(resolved.anchor):
            raise NativeBoundaryUnavailable("Extra read roots must be existing bounded directories")
        if resolved == Path.home().resolve() or resolved == root or resolved.is_relative_to(root):
            raise NativeBoundaryUnavailable("Extra read roots must stay outside the command workspace")
        if any(part.casefold() in _AUTHORITY for part in resolved.parts):
            raise NativeBoundaryUnavailable("Extra read roots must not contain authority stores")
        if resolved == runtime or resolved.is_relative_to(runtime):
            raise NativeBoundaryUnavailable("Extra read roots must stay outside the host runtime state")
        extra_read_dirs.append(resolved)
    read: set[str] = set()
    write: set[str] = set()
    pending = [(root, False), *((p, True) for p in extra_read_dirs)]
    while pending:
        path, read_only = pending.pop()
        path = checked_path(path)
        if not scope.allows(path, write=False):
            continue
        read.add(str(path))
        if path != root and path.is_dir() and path.name.casefold() in _OPAQUE_TOOL_DIRS:
            read_only = True
        if not read_only and scope.allows(path, write=True):
            write.add(str(path))
        if len(read) > MAX_PATHS:
            raise NativeBoundaryUnavailable("Native scope exceeds the exact-path budget; split the task")
        if path.is_dir():
            pending.extend((p, read_only) for p in sorted(path.iterdir(), reverse=True))
    if str(root) not in read:
        raise NativeBoundaryUnavailable("Native workspace is not readable in current policy")
    # Fresh per-call profile directories only; never borrow the user's profile.
    for path in [runtime / "temp", runtime / "profile", runtime / "profile/AppData",
                 runtime / "profile/AppData/Local", runtime / "profile/AppData/Roaming"]:
        checked_path(path)
        read.add(str(path))
        write.add(str(path))
    system = Path(os.environ.get("SystemRoot", ""))
    if not system.is_absolute():
        raise NativeBoundaryUnavailable("Windows system directory is unavailable")
    shell = system / "System32/WindowsPowerShell/v1.0/powershell.exe"
    # A denied volume root can leave Windows PowerShell without its initial
    # FileSystem drive. This is a runspace-only drive, not an OS mapping or ACL
    # grant, and its root is exactly the frozen workspace.
    literal_root = str(root).replace("'", "''")
    bootstrap = (
        "$ErrorActionPreference='Stop'; "
        "New-PSDrive -Name MewWork -PSProvider FileSystem -Root '" + literal_root + "' | Out-Null; "
        "Set-Location -LiteralPath 'MewWork:\\'; "
    )
    bootstrap += remove_item_wrapper()
    # Windows may rewrite TEMP to a package-profile path that was never
    # created in the isolated USERPROFILE. Restore the exact host-granted
    # per-call temp directory after PowerShell starts, without widening ACLs.
    literal_temp = str(runtime / "temp").replace("'", "''")
    bootstrap += f"$env:TEMP='{literal_temp}'; $env:TMP=$env:TEMP; "
    path_entries = str(system / "System32")
    if python_executable is not None:
        executable = checked_path(python_executable, file=True)
        if not executable.is_relative_to(root) or str(executable) not in read:
            raise NativeBoundaryUnavailable("Python alias must name a frozen readable workspace interpreter")
        literal_python = str(executable).replace("'", "''")
        bootstrap += "".join(f"Set-Alias -Name {name} -Value '{literal_python}'; "
                             for name in ("python", "python.exe", "python3", "python3.exe"))
        path_entries = str(executable.parent) + ";" + path_entries
    if git_dir is not None:
        # The bundled AppContainer-compatible Git goes on PATH so native
        # grandchildren (which read bytes themselves) can resolve it, while
        # PowerShell call sites hit the function wrapper first (functions
        # outrank PATH executables) and stay lossless; see
        # _git_capture_wrapper. Its global config is a fresh per-call file
        # with a fixed AUTO identity; repo-local config still wins, and
        # hooks/config writes stay denied by the scope walk above.
        read.add(str(git_dir))
        for artifact in git_dir.rglob("*"):
            read.add(str(checked_path(artifact)))
            if len(read) > MAX_PATHS:
                raise NativeBoundaryUnavailable("Native scope exceeds the exact-path budget; split the task")
        path_entries = str(git_dir) + ";" + path_entries
        # Exact ACLs do not inherit onto files created by the child. Allocate
        # capture files on the host and grant only these private leaves, not
        # recursive write/delete rights on the workspace or .git directory.
        for slot in range(GIT_CAPTURE_SLOTS):
            for suffix in ("out", "err", "stdin"):
                capture = runtime / "temp" / f"git-{slot:03d}.{suffix}"
                capture.write_bytes(b"")
                read.add(str(capture))
                write.add(str(capture))
        # Git does not exclude its current directory from the ceiling. Since
        # the helper starts at the workspace root, ceiling its direct parent:
        # Git can still discover repositories in the workspace or descendants
        # but cannot probe the private staging parent above that boundary.
        # Git's getcwd() uses slash-normalized Windows paths. Keep the raw
        # inaccessible parent comparable without asking Git to canonicalize it.
        literal_ceiling = root.parent.as_posix().replace("'", "''")
        # A leading empty Windows path-list element tells Git to skip realpath
        # canonicalization for the inaccessible staging parent itself.
        bootstrap += f"$env:GIT_CEILING_DIRECTORIES=';{literal_ceiling}'; "
        literal_git_dir = str(git_dir).replace("'", "''")
        bootstrap += f"$env:GIT_EXEC_PATH='{literal_git_dir}'; "
        bootstrap += _git_capture_wrapper(git_dir / "git.exe", runtime / "temp")
        git_configuration = (
            "[user]\n\tname = MewCode AUTO\n\temail = mewcode-auto@localhost\n"
            "[core]\n\tquotepath = false\n")
        certificate_bundle = git_dir / "ca-bundle.crt"
        if certificate_bundle.is_file():
            # Schannel cannot acquire TLS credentials in LPAC. Use the fixed
            # private OpenSSL transport and its pinned trust bundle, retaining
            # normal certificate verification for task HTTPS requests.
            checked_path(git_dir / "libcurl-openssl-4.dll", file=True)
            certificate_path = checked_path(certificate_bundle, file=True).as_posix()
            quoted_certificate = certificate_path.replace('"', '\\"')
            git_configuration += (
                "[http]\n\tsslBackend = openssl\n\tsslVerify = true\n"
                f'\tsslCAInfo = "{quoted_certificate}"\n')
        (runtime / "profile" / ".gitconfig").write_text(git_configuration, encoding="utf-8")
        read.add(str(runtime / "profile" / ".gitconfig"))
    environment = {
        "SystemRoot": str(system), "WINDIR": str(system), "SystemDrive": system.drive,
        "COMSPEC": str(system / "System32/cmd.exe"), "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "PATH": path_entries, "TEMP": str(runtime / "temp"), "TMP": str(runtime / "temp"),
        "USERPROFILE": str(runtime / "profile"), "HOME": str(runtime / "profile"),
        "APPDATA": str(runtime / "profile/AppData/Roaming"), "LOCALAPPDATA": str(runtime / "profile/AppData/Local"),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PYTHONIOENCODING": _oem_python_encoding(),
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
    }
    request = {"schema": "mewcode-lpac-request-v1", "request_id": uuid.uuid4().hex,
        "workspace": str(root), "cwd": str(root), "executable": str(shell),
        "arguments": ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", bootstrap + command],
        "timeout_ms": timeout * 1000, "cancel_event_name": cancel_name,
        "read_paths": sorted(read), "write_paths": sorted(write), "environment": environment}
    if len(json.dumps(request, ensure_ascii=True).encode()) > MAX_REQUEST:
        raise NativeBoundaryUnavailable("Native request exceeds the transport bound; split the task")
    return request


def decode_receipt(raw: bytes, request_id: str, helper_exit: int | None) -> NativeExecutionReceipt:
    def failed(reason, cleanup=False):
        return NativeExecutionReceipt("spawn_failed", detail=reason, backend=BACKEND, cleanup_confirmed=cleanup,
                                      command_started=None, error_code="native_receipt_unverified")
    if not raw or len(raw) > MAX_RECEIPT:
        return failed("Missing or oversized LPAC host receipt")
    try:
        envelope = json.loads(raw.decode("utf-8-sig"))
        if (envelope["schema"] != "mewcode-lpac-receipt-v1" or envelope["backend"] != BACKEND
                or envelope["request_id"] != request_id):
            return failed("LPAC receipt identity mismatch")
        receipt = envelope["receipt"]
        cleanup = all(receipt.get(k) is True for k in ("job_cleanup_ok", "acl_cleanup_ok", "profile_cleanup_ok"))
        if not cleanup:
            return failed("LPAC cleanup is unconfirmed; write handoff remains blocked", False)
        if receipt.get("cancelled") is True:
            return NativeExecutionReceipt("cancelled", detail="LPAC job cancelled and cleaned", backend=BACKEND,
                                          cleanup_confirmed=True)
        if receipt.get("timed_out") is True:
            return NativeExecutionReceipt("timed_out", detail="LPAC deadline reached; job cleaned", backend=BACKEND,
                                          cleanup_confirmed=True)
        if (receipt.get("error") or receipt.get("appcontainer_verified") is not True
                or receipt.get("lpac_verified") is not True or receipt.get("capabilities") != CAPABILITIES
                or (receipt.get("lpac_verification_method") == "access_check" and receipt.get("lpac_access_mask") != 2)
                or receipt.get("lpac_verification_method") not in {"access_check", "token_query"}):
            detail = receipt.get("error")
            return failed("LPAC launch or identity validation failed" +
                          (": " + detail[:600] if isinstance(detail, str) and detail else ""), cleanup)
        exit_code = receipt.get("exit_code")
        if type(exit_code) is not int or exit_code != helper_exit:
            return failed("LPAC exit status mismatch", cleanup)
        if any(type(receipt.get(k)) is not str for k in ("stdout", "stderr")):
            return failed("LPAC output has an invalid type", cleanup)
        return NativeExecutionReceipt("exited", stdout=receipt["stdout"][:40000], stderr=receipt["stderr"][:10000],
            exit_code=exit_code, backend=BACKEND, cleanup_confirmed=True,
            output_truncated=bool(receipt.get("stdout_truncated") or receipt.get("stderr_truncated")
                                  or len(receipt["stdout"]) > 40000 or len(receipt["stderr"]) > 10000))
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        return failed("Malformed LPAC host receipt")


def encode_request(request: dict) -> bytes:
    """Validate the complete transport envelope before registering a launch."""
    payload = json.dumps(request, ensure_ascii=True).encode()
    if len(payload) > MAX_REQUEST:
        raise NativeBoundaryUnavailable("Native request exceeds the transport bound")
    return payload


async def _capture_helper_progress(process, payload: bytes, request_id: str):
    """Drain trusted helper stderr live; child stderr is nested in final stdout.

    A marker on stdout, in model text, or in the nested child output is never
    interpreted as startup. Keep draining oversized output to avoid pipe locks.
    """
    async def stdout():
        chunks, size = [], 0
        while chunk := await process.stdout.read(65536):
            if size <= MAX_RECEIPT:
                chunks.append(chunk[:MAX_RECEIPT + 1 - size])
                size += len(chunks[-1])
        return b"".join(chunks)

    async def stderr():
        pending = b""
        discarding = False
        started = False
        while chunk := await process.stderr.read(4096):
            for part in chunk.splitlines(keepends=True):
                pending += part
                if len(pending) > 256:
                    discarding = True
                    pending = b""
                if part.endswith(b"\n"):
                    if not discarding and not started:
                        match = re.fullmatch(rb"MEWCODE_CHILD_PID=([1-9][0-9]{0,9})\r?\n", pending)
                        if match and int(match[1]) <= 0xFFFFFFFF:
                            from datetime import datetime, timezone
                            started = True
                            execution_progress.emit("running", process_started=True,
                                child_pid=int(match[1]), helper_pid=process.pid,
                                request_id=request_id, request_sha256=hashlib.sha256(payload).hexdigest(),
                                start_observed_at=datetime.now(timezone.utc).isoformat(),
                                evidence_source="helper_stderr_after_resume")
                    pending, discarding = b"", False

    async def feed():
        try:
            process.stdin.write(payload)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # A helper may reject stdin and still return a valid denial.
        finally:
            process.stdin.close()

    tasks = [asyncio.create_task(stdout()), asyncio.create_task(stderr()),
             asyncio.create_task(feed()), asyncio.create_task(process.wait())]
    try:
        results = await asyncio.gather(*tasks)
        return results[0], b""
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def invoke_helper(helper: Path, request: dict, cancel: HostCancelEvent, *, on_receipt=None) -> NativeExecutionReceipt:
    """Cancellation waits for the helper's Job/ACL/profile cleanup receipt."""
    payload = encode_request(request)
    try:
        process = await asyncio.create_subprocess_exec(
            str(helper), "--host-stdin-v1", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except OSError as error:
        result = NativeExecutionReceipt("spawn_failed", detail=str(error), backend=BACKEND,
            cleanup_confirmed=True, command_started=False, error_code="native_helper_start_failed")
        execution_progress.finish(result)
        if on_receipt is not None:
            on_receipt(result)
        return result
    execution_progress.emit("starting", helper_pid=getattr(process, "pid", None),
                            request_id=request["request_id"])
    capture = asyncio.create_task(
        _capture_helper_progress(process, payload, request["request_id"])
        if execution_progress.observer.get() is not None else process.communicate(payload))
    cancelled = False

    async def reap_failed_transport():
        # A broken stdin/stdout pipe can happen after CreateProcess succeeds.
        # Do not leave our helper (and its Job) running on that error path.
        try:
            async with asyncio.timeout(5):
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await process.wait()
        finally:
            capture.cancel()
            await asyncio.gather(capture, return_exceptions=True)

    try:
        try:
            out, _ = await asyncio.wait_for(asyncio.shield(capture), request["timeout_ms"] / 1000 + 25)
        except (asyncio.CancelledError, asyncio.TimeoutError) as error:
            cancelled = isinstance(error, asyncio.CancelledError)
            execution_progress.emit("cleaning", cancellation_requested=cancelled)
            cancel.signal()
            try:
                out, _ = await asyncio.wait_for(asyncio.shield(capture), 25)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                raise RuntimeError("LPAC helper cleanup unconfirmed; do not transfer write ownership") from error
    except BaseException:
        # Repeated UI cancellation must not cancel the bounded reap itself.
        reaper = asyncio.create_task(reap_failed_transport())
        while not reaper.done():
            try:
                await asyncio.shield(reaper)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        cleanup_error = reaper.exception() if not reaper.cancelled() else None
        failure = NativeExecutionReceipt("spawn_failed", backend=BACKEND, cleanup_confirmed=False,
            command_started=None, error_code="native_transport_unknown",
            detail="LPAC transport failed; ACL/profile cleanup unconfirmed; do not transfer write ownership")
        execution_progress.finish(failure)
        if on_receipt is not None:
            on_receipt(failure)
        if cleanup_error is not None:
            raise RuntimeError("LPAC helper cleanup unconfirmed; do not transfer write ownership") from cleanup_error
        raise
    result = decode_receipt(out, request["request_id"], process.returncode)
    execution_progress.finish(result)
    if on_receipt is not None:
        on_receipt(result)
    if cancelled:
        if result.cleanup_confirmed is not True:
            raise RuntimeError("LPAC cancellation cleanup unconfirmed; do not transfer write ownership")
        raise asyncio.CancelledError
    return result


async def execute_lpac(command: str, *, scope: NativeScope | None, work_dir: str, timeout: int,
                       on_receipt=None, extra_read: tuple[Path, ...] = ()) -> NativeExecutionReceipt:
    with execution_progress.operation():
        result = await _execute_lpac_queued(command, scope=scope, work_dir=work_dir,
            timeout=timeout, on_receipt=on_receipt, extra_read=extra_read)
        execution_progress.finish(result)
        return result


async def _execute_lpac_queued(command: str, *, scope: NativeScope | None, work_dir: str, timeout: int,
                               on_receipt=None, extra_read: tuple[Path, ...] = ()) -> NativeExecutionReceipt:
    from mewcode.runtime_scheduler import current_runtime_scheduler, ResourceQueueTimeout
    from mewcode.usage.service import current_usage_scope
    from mewcode.sandbox.windows import native_runtime_ready
    if not native_runtime_ready():
        return await _execute_lpac_admitted(command, scope=scope, work_dir=work_dir,
            timeout=timeout, on_receipt=on_receipt, extra_read=extra_read)
    admitted = False
    try:
        if scope is None or checked_path(Path(work_dir)) != scope.workspace:
            raise NativeBoundaryUnavailable("Missing or mismatched host execution scope")
        usage = current_usage_scope()
        # Admission precedes reconciliation. A sibling holding this workspace
        # is ordinary queued work, not an interrupted transaction to recover.
        async with current_runtime_scheduler().serialized(
            "native-workspace:" + os.path.normcase(str(scope.workspace)),
            label="Native workspace", session_id=usage.session_id, agent_id=usage.agent_id,
            wait_timeout=max(1, timeout),
        ):
            execution_progress.emit("preparing")
            admitted = True
            return await _execute_lpac_admitted(command, scope=scope, work_dir=work_dir,
                timeout=timeout, on_receipt=on_receipt, extra_read=extra_read)
    except ResourceQueueTimeout:
        return NativeExecutionReceipt("denied", backend=BACKEND,
            detail="Native workspace queue wait timed out; command not started", command_started=False,
            error_code="native_queue_timeout")
    except (OSError, ValueError, NativeBoundaryUnavailable) as error:
        return NativeExecutionReceipt("denied", detail=str(error), backend=BACKEND,
            command_started=None if admitted else False, error_code="native_admission_failed")


async def _execute_lpac_admitted(command: str, *, scope: NativeScope | None, work_dir: str,
                                timeout: int, on_receipt=None,
                                extra_read: tuple[Path, ...] = ()) -> NativeExecutionReceipt:
    # This second gate is deliberate; direct use must not bypass tool policy.
    from mewcode.sandbox.windows import native_runtime_ready
    if not native_runtime_ready():
        from mewcode.sandbox.native_gate import native_gate_status
        gate = native_gate_status()
        return NativeExecutionReceipt(
            "denied", backend=BACKEND,
            detail=f"Production LPAC activation is disabled ({gate.state.value}: {gate.reason})",
            command_started=False, error_code="native_gate_" + gate.state.value)
    try:
        if scope is None or not scope.current() or checked_path(Path(work_dir)) != scope.workspace:
            return NativeExecutionReceipt("denied", backend=BACKEND, detail="Missing or mismatched host execution scope",
                                          command_started=False, error_code="native_scope_invalid")
        from mewcode.sandbox.staged_execution import PROTOCOL, execute_staged, reconcile
        helper = find_runtime_helper(require_protocol=PROTOCOL)
        # Fresh authority is available here, unlike bare application startup.
        # An interrupted candidate is never silently rerun or committed.
        if getattr(scope, "independent_offline", False):
            from mewcode.sandbox.transaction_registry import workspace_command_block
            blocked = workspace_command_block(scope.workspace, independent_command=command)
            if blocked:
                raise NativeBoundaryUnavailable(blocked)
            # Old private cleanup is independent pending work, not a serial
            # prerequisite for this fresh offline copy. Do not reconcile it.
            recovery = []
        else:
            recovery = reconcile(scope)
        if any(item["state"] == "busy" for item in recovery):
            # Same-scheduler siblings queue before getting here. A separate
            # host's OS claim is still authoritative; do not steal its work.
            raise NativeBoundaryUnavailable("Native workspace is busy in another host operation; command not started")
        if any(item["state"] == "recovery_required" for item in recovery):
            raise NativeBoundaryUnavailable("Prior native transaction needs reconciliation")
    except (OSError, ValueError, NativeBoundaryUnavailable) as error:
        return NativeExecutionReceipt("denied", detail=str(error), backend=BACKEND,
                                      command_started=False, error_code="native_preparation_failed")
    result = await execute_staged(command, scope=scope, helper=helper, timeout=timeout,
                                  extra_read=extra_read, on_receipt=on_receipt)
    if on_receipt is not None:
        on_receipt(result)
    return result
