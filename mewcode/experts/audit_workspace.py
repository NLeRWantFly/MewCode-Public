"""Candidate artifacts with durable manifests and conflict-checked promotion.

This isolates ordinary workspace edits, not arbitrary subprocess side effects.
The command permission layer still applies. No git reset/clean is used, and
rejected candidates remain available for inspection.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

from mewcode.experts.audit_protocol import content_hash


EXCLUDED_DIRS = {".git", ".mewcode", ".venv", "venv", "node_modules",
                 "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
MAX_FILES = 20_000
MAX_BYTES = 512 * 1024 * 1024


def manifest(root: Path) -> dict[str, str]:
    """Hash regular project files; fail closed on links and excessive copies."""
    import hashlib

    root = root.resolve(strict=True)
    result: dict[str, str] = {}
    total = 0
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(name for name in directories if name not in EXCLUDED_DIRS)
        for name in directories + sorted(files):
            path = Path(current) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError(f"候选工作区不支持链接/重解析点：{path.relative_to(root)}")
            if path.is_dir():
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"候选工作区只支持普通文件：{path.relative_to(root)}")
            total += info.st_size
            if total > MAX_BYTES or len(result) >= MAX_FILES:
                raise ValueError("候选工作区超过 512 MiB/20000 文件，请缩小项目范围后重试")
            with path.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            result[path.relative_to(root).as_posix()] = digest
    return result


def _atomic_json(path: Path, value: dict) -> None:
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".audit-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _file_hash(path: Path) -> str | None:
    import hashlib
    if not path.exists():
        return None
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _replace_file(source: Path, destination: Path) -> None:
    handle, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".audit-")
    os.close(handle)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


class AuditWorkspace:
    @classmethod
    def resume(cls, source: Path, archive: Path, directory: str, *, sealed: bool = False,
               promoted: bool = False) -> "AuditWorkspace":
        """Reopen an unfinished candidate only against its unchanged baseline."""
        root = Path(directory).resolve(strict=True)
        if root.parent != archive.resolve() or not root.name.startswith("candidate-"):
            raise ValueError("候选恢复路径不属于当前审查记录")
        record = json.loads((root / "transaction.json").read_text(encoding="utf-8"))
        allowed_states = ({"reviewing", "accepted"} if promoted else
                          {"reviewing", "unaccepted"} if sealed else {"active", "unaccepted"})
        if promoted and not sealed:
            raise ValueError("已合入候选必须使用固定验收版本")
        if record.get("state") not in allowed_states or (record.get("after") is not None) != sealed:
            raise ValueError("候选已进入验收或合入阶段，不能作为未完成修复接力")
        transaction = cls.__new__(cls)
        transaction.source = source.resolve(strict=True)
        transaction.root = root
        transaction.baseline = root / "baseline"
        transaction.candidate = root / "candidate"
        if (transaction.baseline.resolve() != transaction.baseline
                or transaction.candidate.resolve() != transaction.candidate):
            raise ValueError("候选恢复目录不支持链接/重解析点")
        transaction.before = record["before"]
        transaction.baseline_hash = content_hash(transaction.before)
        transaction.after = record.get("after")
        transaction.state = str(record["state"])
        transaction.runtime_inputs = record.get("runtime_inputs")
        transaction.verify_runtime()
        if (Path(record["source"]).resolve() != transaction.source
                or record.get("baseline_hash") != transaction.baseline_hash
                or manifest(transaction.source) != (transaction.after if promoted else transaction.before)
                or manifest(transaction.baseline) != transaction.before):
            raise ValueError("原稿或基线已变化，不能直接接力旧候选；请重新开始该阶段审查")
        # Validate retained files before handing their directory back to tools.
        candidate = manifest(transaction.candidate)
        if sealed and candidate != transaction.after:
            raise ValueError("待复审候选稿已变化，不能续用已固定的版本")
        return transaction

    def __init__(self, source: Path, archive: Path, *, control_archive: bool = False,
                 freeze_python: bool = False) -> None:
        self.source = source.resolve(strict=True)
        if control_archive:
            expected = self.source / ".mewcode" / "collaboration-candidates"
            if archive.absolute() != expected or archive.resolve() != expected:
                raise ValueError("协作候选必须保存在原项目的宿主控制目录")
        archive.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="candidate-", dir=archive))
        if self.root.resolve().is_relative_to(self.source) and not control_archive:
            raise ValueError("候选存储目录必须位于原项目之外")
        self.baseline = self.root / "baseline"
        self.candidate = self.root / "candidate"
        self.before = manifest(self.source)
        self.baseline_hash = content_hash(self.before)
        self.after: dict[str, str] | None = None
        self.state = "preparing"
        self.runtime_inputs = None
        self.record()
        for destination in (self.baseline, self.candidate):
            destination.mkdir()
            for relative in self.before:
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.source / relative, target)
        if manifest(self.source) != self.before or manifest(self.baseline) != self.before:
            self.record("conflict")
            raise ValueError("建立基线时项目发生变化，请重试")
        if manifest(self.candidate) != self.before:
            self.record("conflict")
            raise ValueError("候选副本与原始文件不一致")
        if freeze_python:
            from mewcode.experts.candidate_runtime import freeze_runtime
            self.runtime_inputs = freeze_runtime(self.source, self.candidate)
        self.record("active")

    def verify_runtime(self) -> None:
        from mewcode.experts.candidate_runtime import verify_runtime
        verify_runtime(self.source, self.candidate, self.runtime_inputs)

    def record(self, state: str | None = None) -> None:
        self.state = state or self.state
        _atomic_json(self.root / "transaction.json", {
            "state": self.state, "source": str(self.source),
            "baseline": str(self.baseline), "candidate": str(self.candidate),
            "baseline_hash": self.baseline_hash, "before": self.before,
            "after": self.after,
            "runtime_inputs": self.runtime_inputs,
        })

    def seal(self) -> str:
        self.verify_runtime()
        self.after = manifest(self.candidate)
        self.record("reviewing")
        return content_hash(self.after)

    def promote(self) -> list[str]:
        self.verify_runtime()
        if self.after is None:
            raise ValueError("候选稿尚未固定版本")
        if manifest(self.source) != self.before:
            self.record("conflict")
            raise ValueError("原项目在复审期间被修改，候选稿未合入；请保留并检查并发修改")
        if manifest(self.candidate) != self.after or manifest(self.baseline) != self.before:
            self.record("conflict")
            raise ValueError("复审后候选稿或基线发生变化，禁止合入")
        changed = sorted(key for key in self.before.keys() | self.after.keys()
                         if self.before.get(key) != self.after.get(key))
        # Deletion and file/directory type changes need explicit integration;
        # do not recursively remove any user-owned directory during promotion.
        if any(key not in self.after for key in changed):
            self.record("rejected")
            raise ValueError("候选稿包含文件删除，已保留供人工合并，原稿未改动")
        self.record("committing")
        written: list[str] = []
        try:
            for key in changed:
                destination = self.source / key
                # Validate before creating directories as well as before use.
                if not destination.resolve().is_relative_to(self.source):
                    raise ValueError("合入目标已离开项目目录，停止合入")
                destination.parent.mkdir(parents=True, exist_ok=True)
                if _file_hash(destination) != self.before.get(key):
                    raise ValueError(f"合入前文件再次发生变化：{key}")
                _replace_file(self.candidate / key, destination)
                written.append(key)
            if manifest(self.source) != self.after:
                raise ValueError("合入期间项目发生变化，回退本次合入并保留并发修改")
        except (OSError, ValueError):
            conflicts = []
            for key in reversed(written):
                destination = self.source / key
                try:
                    if (not destination.resolve().is_relative_to(self.source)
                            or _file_hash(destination) != self.after[key]):
                        conflicts.append(key)
                    elif key in self.before:
                        _replace_file(self.baseline / key, destination)
                    else:
                        # Move new files to retained storage rather than delete.
                        retained = self.root / "unmerged" / key
                        retained.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(destination, retained)
                except OSError:
                    conflicts.append(key)
            self.record("rollback_conflict" if conflicts else "rolled_back")
            raise
        self.record("accepted")
        return changed

    def discard(self, state: str = "rejected") -> None:
        # "Discard" only excludes the candidate from the accepted lineage.
        # Keep both versions, including partial work after timeout/cancellation.
        if self.state not in {"committing", "rollback_conflict", "rolled_back", "conflict"}:
            self.record(state)


def ensure_no_partial_promotion(archive: Path) -> None:
    for path in archive.glob("candidate-*/transaction.json"):
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("state") in {"committing", "rollback_conflict"}:
            raise ValueError(f"上次合入中断，需检查保留的基线与候选文件：{path.parent}")
