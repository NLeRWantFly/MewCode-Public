"""Host-only synthetic acceptance adapter; not a production permission route.

The CLI probe opts in explicitly. This module never changes gate records and
cannot be selected by tool arguments or an environment fallback in production.
It shares request construction, identity validation and cancellation with LPAC.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import os
import re
from pathlib import Path
import tempfile

from mewcode.sandbox import lpac_runtime as lpac

ROOT = Path(__file__).resolve().parents[1]
EXTERNAL_FIXTURE_ROOT = Path("E:/MewCodeAutoAcceptance")


def bounded_directory(path: Path, *, must_exist: bool = True) -> Path:
    """Diagnostic host state stays under this checkout's .tmp, never the app."""
    path = Path(path).absolute()
    base = lpac.checked_path(ROOT / ".tmp")
    if ".." in path.parts or path == base or not path.is_relative_to(base):
        raise ValueError("Diagnostic paths must be strict descendants of the project .tmp")
    ancestor = path
    while not os.path.lexists(ancestor):
        ancestor = ancestor.parent
    lpac.checked_path(ancestor)
    if must_exist:
        checked = lpac.checked_path(path)
        if not checked.is_dir():
            raise ValueError("Diagnostic roots must be directories")
        return checked
    return path


class AcceptanceExecutor:
    def __init__(self, data_root: Path, fixtures: tuple[Path, ...]):
        self.data_root = bounded_directory(data_root)
        self.fixtures = tuple(bounded_fixture(p) for p in fixtures)
        if not self.fixtures:
            raise ValueError("An explicit synthetic fixture is required")
        for fixture in self.fixtures:
            if fixture == self.data_root or fixture.is_relative_to(self.data_root) or self.data_root.is_relative_to(fixture):
                raise ValueError("Diagnostic fixture and private host state must be disjoint")
        self.helper = lpac.find_runtime_helper()
        if self.helper.parent != self.data_root / "native" / "windows-lpac-v1":
            raise ValueError("Installed helper is not in the explicit diagnostic data root")
        self.runs = self.data_root / "native" / "acceptance-runs"
        self.runs.mkdir(exist_ok=True)

    async def __call__(self, command: str, *, scope, work_dir: str, timeout: int,
                       extra_read=(), on_receipt=None):
        workspace = lpac.checked_path(Path(work_dir))
        if (scope is None or scope.workspace != workspace
                or workspace not in {p / "中文 workspace 空格" for p in self.fixtures}):
            raise ValueError("Diagnostic execution is limited to the declared synthetic workspaces")
        # Exact read grants still change ACLs temporarily. Diagnostic callers
        # may only select copied runtimes in this fixture, never a shared
        # interpreter or a user's unrelated directory.
        allowed_runtime = workspace.parent / "python-runtime"
        if any(lpac.checked_path(Path(path)) != allowed_runtime for path in extra_read):
            raise ValueError("Diagnostic extra reads must use this fixture's copied Python runtime")
        if lpac.find_runtime_helper() != self.helper:
            raise ValueError("Diagnostic helper changed during acceptance")
        from mewcode.sandbox.staged_execution import execute_staged
        from mewcode.sandbox.transaction_registry import TransactionRegistry
        # One production lifecycle for all acceptance commands. A private
        # same-volume sibling is inside the explicit synthetic fixture only.
        parent = workspace.parent / "host-transactions"
        parent.mkdir(exist_ok=True)
        result = await execute_staged(command, scope=scope, helper=self.helper, timeout=timeout,
            registry=TransactionRegistry(self.data_root / "native"), host_parent=parent,
            extra_read=extra_read, on_receipt=on_receipt)
        self.last_transaction = result.transaction_id
        if on_receipt:
            on_receipt(result)
        return result

    def observed_path(self, workspace: Path, relative: str) -> Path:
        # Host-only diagnostic lookup. Never accept a worker-supplied path.
        from mewcode.sandbox.transaction_registry import TransactionRegistry
        if relative not in {"cancel-child.pid", "child.pid"}:
            raise ValueError("Only synthetic child PIDs may be observed")
        registry = TransactionRegistry(self.data_root / "native")
        records = registry.pending(workspace)
        if relative == "child.pid" and getattr(self, "last_transaction", ""):
            records = [registry.get(self.last_transaction)]
        if len(records) != 1 or not records[0].get("transaction"):
            raise ValueError("No unique active synthetic transaction")
        if Path(records[0]["workspace"]) != workspace:
            raise ValueError("Synthetic transaction workspace mismatch")
        folder = "workspace" if relative == "cancel-child.pid" else "runtime/temp"
        return Path(records[0]["transaction"]) / folder / relative


def bounded_fixture(path: Path) -> Path:
    path = Path(path).absolute()
    if path.is_relative_to(ROOT / ".tmp"):
        return bounded_directory(path, must_exist=False)
    # Explicit E-volume matrix only. Never accept an arbitrary existing
    # project/business directory, a volume root, or a nested link target.
    if (path.parent != EXTERNAL_FIXTURE_ROOT
            or not re.fullmatch(r"case-[0-9a-f]{12,32}", path.name)):
        raise ValueError("External fixtures must be new E:/MewCodeAutoAcceptance/case-<id> directories")
    ancestor = path
    while not os.path.lexists(ancestor):
        ancestor = ancestor.parent
    lpac.checked_path(ancestor)
    return path
