"""Deterministic, read-only checks for submitted repair artifacts."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
from typing import Any

from pydantic import BaseModel, Field

from mewcode.tools.base import Tool, ToolResult

MAX_PATCH_BYTES = 2 * 1024 * 1024
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_SOURCE_TOTAL_BYTES = 64 * 1024 * 1024
MAX_PATCH_BASE_FILE_BYTES = 32 * 1024 * 1024
MAX_PATCH_FILES = 100
PATCH_TIMEOUT_SECONDS = 8


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _error(message: str, *, identity: dict[str, str] | None = None,
           details: dict[str, Any] | None = None) -> ToolResult:
    identity_value = dict(identity or {})
    result: dict[str, Any] = {"status": "FAIL", "identity": identity_value,
                              "errors": [message]}
    if details:
        result.update(details)
    if not identity_value.get("sha256"):
        identity_value["sha256"] = canonical_digest({"object": identity_value.get("object", ""),
            "version": identity_value.get("version", "unverified"), "result": result})
    return ToolResult(json.dumps(result, ensure_ascii=False, sort_keys=True), is_error=True)


def _safe_relative(value: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("路径为空或格式无效")
    # Both separators are treated as separators even when running on POSIX.
    normalized = value.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if (pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts)
            or ":" in pure.parts[0] or normalized.startswith("//")):
        raise ValueError("路径必须是根目录内的相对路径")
    return Path(*pure.parts)


def _safe_file(root: Path, relative: str, *, max_bytes: int) -> Path:
    root_abs = root.absolute()
    root_real = root_abs.resolve(strict=True)
    if root_real != root_abs:
        raise ValueError("工作区根目录不能是符号链接或重解析点")
    rel = _safe_relative(relative)
    target = root_real / rel
    current = root_real
    for part in rel.parts:
        current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"路径不允许符号链接或重解析点：{part}")
    resolved = target.resolve(strict=True)
    if not resolved.is_relative_to(root_real) or not resolved.is_file():
        raise ValueError("文件必须位于工作区根目录内")
    info = resolved.stat()
    if info.st_size > max_bytes:
        raise ValueError("文件超过只读核验大小上限")
    if getattr(info, "st_nlink", 1) > 1:
        raise ValueError("核验文件不能是硬链接")
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sources_digest(sources: list[dict[str, str]]) -> str:
    """Stable digest for the complete frozen source fingerprint set."""
    canonical = sorted(({"path": str(row["path"]), "sha256": str(row["sha256"])}
                        for row in sources), key=lambda row: (row["path"], row["sha256"]))
    return canonical_digest(canonical)


def _patch_paths(patch: bytes) -> list[str]:
    """Parse the supported Git/unified headers and reject unsafe names."""
    try:
        lines = patch.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("补丁必须是 UTF-8 文本") from exc
    paths: list[str] = []
    in_hunk = False
    for line in lines:
        if line.startswith("diff --git "):
            in_hunk = False
        if line.startswith("diff --git "):
            body = line[len("diff --git "):]
            if '"' in body:
                raise ValueError("不支持带引号或转义的补丁路径")
            parts = body.split()
            if len(parts) != 2:
                raise ValueError("补丁路径格式无效或包含空格")
            for value in parts:
                if not value.startswith(("a/", "b/")):
                    raise ValueError("Git 补丁路径必须使用 a/ 或 b/ 前缀")
                paths.append(value[2:])
        elif line.startswith("@@"):
            in_hunk = True
        elif not in_hunk and line.startswith(("--- ", "+++ ")):
            value = line[4:].split("\t", 1)[0]
            if value == "/dev/null":
                continue
            if not value.startswith(("a/", "b/")):
                raise ValueError("统一差异路径必须使用 a/ 或 b/ 前缀")
            paths.append(value[2:])
    unique = sorted(set(paths))
    if not unique:
        raise ValueError("补丁中没有可核验的文件路径")
    if len(unique) > MAX_PATCH_FILES:
        raise ValueError("补丁涉及文件数超过核验上限")
    for value in unique:
        _safe_relative(value)
    return unique


def check_patch_applicability(root: Path, patch_path: str,
                              expected_sha256: str = "") -> dict[str, Any]:
    """Run `git apply --check` directly (no shell) after path preflight."""
    root_abs = root.absolute()
    root_real = root_abs.resolve(strict=True)
    if root_real != root_abs:
        raise ValueError("仓库根目录不能是符号链接或重解析点")
    patch_file = _safe_file(root_real, patch_path, max_bytes=MAX_PATCH_BYTES)
    patch = patch_file.read_bytes()
    patch_sha = hashlib.sha256(patch).hexdigest()
    if expected_sha256 and patch_sha != expected_sha256:
        raise ValueError("补丁文件 SHA256 与调用方声明不一致")
    paths = _patch_paths(patch)
    base_fingerprints: list[dict[str, str]] = []
    for relative in paths:
        # A dry-run must not follow workspace links into another location.
        candidate = root_real / _safe_relative(relative)
        current = root_real
        for part in _safe_relative(relative).parts:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError(f"补丁目标路径含符号链接或重解析点：{relative}")
        if not candidate.resolve(strict=False).is_relative_to(root_real):
            raise ValueError(f"补丁目标路径离开仓库根目录：{relative}")
        try:
            target = _safe_file(root_real, relative, max_bytes=MAX_PATCH_BASE_FILE_BYTES)
            base_fingerprints.append({"path": relative, "sha256": _sha256(target)})
        except FileNotFoundError:
            base_fingerprints.append({"path": relative, "sha256": "missing"})

    import shutil
    git = shutil.which("git.exe" if os.name == "nt" else "git")
    if not git:
        raise ValueError("找不到 git，无法完成补丁可应用性核验")
    env = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG", "GIT_CONFIG_COUNT"):
        env.pop(key, None)
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    completed = subprocess.run(
        [git, "apply", "--check", "--whitespace=nowarn", "-"],
        cwd=root_real, env=env, input=patch, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, timeout=PATCH_TIMEOUT_SECONDS, check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    output = (completed.stdout or b"").decode("utf-8", errors="replace")[:4000]
    return {"status": "PASS" if completed.returncode == 0 else "FAIL",
            "applicable": completed.returncode == 0,
            "patch_sha256": patch_sha, "paths": paths,
            "base_revision": canonical_digest(base_fingerprints),
            "base_fingerprints": base_fingerprints, "exit_code": completed.returncode,
            "output": output}


class VerifyPatchParams(BaseModel):
    patch_path: str = Field(description="当前候选工作区内 diff.patch 的相对路径")
    expected_sha256: str = Field(default="", pattern=r"^(|[a-f0-9]{64})$")


class VerifyPatchApplicability(Tool):
    """Bounded Git patch dry-run. Never applies or writes the patch."""

    name = "VerifyPatchApplicability"
    description = ("只读预检候选 diff.patch 是否可应用：校验 SHA256、路径包含和链接边界，"
                   "再直接运行 git apply --check（不经 shell、不修改文件）。")
    params_model = VerifyPatchParams
    category = "read"
    is_concurrency_safe = True

    def __init__(self, root: str | Path):
        self.root = Path(root)

    async def execute(self, params: VerifyPatchParams) -> ToolResult:
        try:
            result = await asyncio.to_thread(check_patch_applicability, self.root,
                                             params.patch_path, params.expected_sha256)
            identity = {"object": f"patch:{params.patch_path}", "version": result["base_revision"],
                        "sha256": canonical_digest(result)}
            result["identity"] = identity
            return ToolResult(json.dumps(result, ensure_ascii=False, sort_keys=True),
                              is_error=result["status"] != "PASS")
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            return _error(f"补丁预检失败关闭：{exc}", identity={"object": f"patch:{params.patch_path}",
                "version": "unverified", "sha256": ""})


class SourceFingerprint(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class VerifyArtifactsParams(BaseModel):
    item_id: str = Field(min_length=1, max_length=200)
    candidate_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    sources: list[SourceFingerprint] = Field(min_length=1, max_length=32,
        description="从当前 ReadReviewQueue 候选回执原样复制的相对路径和 sha256 列表")
    issue_ids: list[str] = Field(max_length=30)


class VerifyArtifacts(Tool):
    """Compare a submitted candidate with its frozen work-order receipt."""

    name = "VerifyArtifacts"
    description = ("只读核对当前工作项的候选版本、来源 SHA256、issue_ids、"
                   "冻结批次及 candidate/diff/evidence 文件指纹；可核验时返回稳定身份。")
    params_model = VerifyArtifactsParams
    category = "read"
    is_concurrency_safe = True

    def __init__(self, store, run_id: str, reviewer_node_id: str, root: str | Path,
                 contract_hash: str):
        self.store, self.run_id = store, run_id
        self.reviewer_node_id = reviewer_node_id
        self.root = Path(root)
        self.contract_hash = contract_hash

    def _verify(self, params: VerifyArtifactsParams) -> dict[str, Any]:
        item_id = params.item_id
        if hasattr(self.store, "load_review_queue"):
            queue = self.store.load_review_queue(self.run_id, self.reviewer_node_id)
        else:
            snapshot = self.store.load(self.run_id) or {}
            queue = (snapshot.get("review_queues") or {}).get(self.reviewer_node_id)
        if not queue or queue.get("contract_hash") != self.contract_hash:
            raise ValueError("当前审查队列不存在或合同版本不匹配")
        candidate = (queue.get("repair_candidates") or {}).get(item_id)
        if not candidate:
            raise ValueError("台账中没有该工作项的候选")
        batch_id = candidate.get("batch_id")
        batch = (queue.get("repair_batches") or {}).get(batch_id)
        if not batch or batch.get("status") != "submitted":
            raise ValueError("候选没有来自当前队列的已提交冻结批次")
        batch_candidate = (batch.get("candidates") or {}).get(item_id)
        if not batch_candidate or batch_candidate != candidate:
            raise ValueError("候选台账与冻结批次回执不一致")
        row = next((value for value in batch.get("items", [])
                    if value.get("item_id") == item_id), None)
        if not row:
            raise ValueError("冻结批次缺少对应工作项")
        frozen_issues = row.get("record", {}).get("issue_ids", [])
        issues = candidate.get("issue_ids", [])
        if (not isinstance(issues, list) or len(issues) != len(set(issues))
                or sorted(issues) != sorted(frozen_issues)):
            raise ValueError("候选 issue_ids 与冻结台账不一致")

        sources = row.get("sources", [])
        candidate_sources = candidate.get("sources", [])
        normalize_sources = lambda values: sorted(
            ({"path": str(value["path"]), "sha256": str(value["sha256"])} for value in values),
            key=lambda value: (value["path"], value["sha256"]))
        if normalize_sources(sources) != normalize_sources(candidate_sources):
            raise ValueError("候选来源 SHA256 与冻结台账不一致")
        if normalize_sources([source.model_dump() for source in params.sources]) != normalize_sources(sources):
            raise ValueError("调用方的来源 SHA256 列表与冻结台账不一致")
        if sorted(params.issue_ids) != sorted(frozen_issues):
            raise ValueError("调用方的 issue_ids 与冻结台账不一致")

        payload = {key: candidate[key] for key in ("item_id", "issue_ids", "candidate", "diff", "evidence")}
        computed_revision = canonical_digest({"candidate": payload,
            "input_revision": row.get("record_revision", ""), "sources": sources})
        if candidate.get("candidate_revision") != computed_revision:
            raise ValueError("candidate_revision 与候选、冻结版本或来源指纹不一致")
        if params.candidate_revision != computed_revision:
            raise ValueError("调用方 candidate_revision 与当前候选台账不一致")

        file_checks: list[dict[str, str]] = []
        bytes_scanned = 0
        for source in normalize_sources(sources):
            source_file = _safe_file(self.root, source["path"], max_bytes=MAX_ARTIFACT_BYTES)
            bytes_scanned += source_file.stat().st_size
            if bytes_scanned > MAX_SOURCE_TOTAL_BYTES:
                raise ValueError("来源文件总量超过 64 MiB 核验上限")
            current_sha = _sha256(source_file)
            if current_sha != source["sha256"]:
                raise ValueError(f"来源文件 SHA256 与冻结台账不一致：{source['path']}")
        for kind in ("candidate", "diff", "evidence"):
            artifact = candidate.get(kind) or {}
            path = _safe_file(self.root, str(artifact.get("path", "")), max_bytes=MAX_ARTIFACT_BYTES)
            current_sha = _sha256(path)
            if current_sha != artifact.get("sha256"):
                raise ValueError(f"{kind} 文件 SHA256 与台账不一致：{artifact.get('path', '')}")
            file_checks.append({"kind": kind, "path": str(artifact["path"]), "sha256": current_sha})

            if kind == "evidence" and path.suffix.lower() == ".json":
                try:
                    evidence_data = json.loads(path.read_text(encoding="utf-8"))
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise ValueError("evidence.json 不是有效 UTF-8 JSON") from exc
                metadata = _find_metadata(evidence_data)
                expected_sources_sha = sources_digest(sources)
                # candidate_revision cannot be embedded in a file whose SHA256
                # participates in candidate_revision; compare it against the
                # host receipt and the caller's current queue snapshot instead.
                for field, expected in (("source_sha256", expected_sources_sha),
                                        ("issue_ids", sorted(issues))):
                    if field not in metadata or metadata[field] != expected:
                        raise ValueError(f"evidence.json 的 {field} 与冻结台账不一致")
        patch_check = None
        diff_path = str(candidate.get("diff", {}).get("path", ""))
        if Path(diff_path).suffix.lower() == ".patch":
            patch_check = check_patch_applicability(self.root, diff_path,
                str(candidate["diff"].get("sha256", "")))
            if not patch_check["applicable"]:
                raise ValueError("diff.patch 不能应用到当前工作区")
        return {"item_id": item_id, "candidate_revision": computed_revision,
                "source_sha256": sources_digest(sources), "issue_ids": sorted(issues),
                "files": file_checks, "patch_check": patch_check}

    async def execute(self, params: VerifyArtifactsParams) -> ToolResult:
        try:
            verified = await asyncio.to_thread(self._verify, params)
            identity = {"object": f"review-item:{params.item_id}",
                        "version": verified["candidate_revision"],
                        "sha256": canonical_digest(verified)}
            result = {"status": "PASS", "identity": identity, **verified}
            return ToolResult(json.dumps(result, ensure_ascii=False, sort_keys=True))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            return _error(f"候选与台账核验失败关闭：{exc}", identity={
                "object": f"review-item:{params.item_id}", "version": "unverified", "sha256": ""})


def _find_metadata(value: Any) -> dict[str, Any]:
    """Read required metadata keys from a flat or nested evidence JSON object."""
    found: dict[str, Any] = {}
    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key in ("source_sha256", "issue_ids"):
                if key in node:
                    if key in found and found[key] != node[key]:
                        raise ValueError(f"evidence.json 存在冲突的 {key}")
                    found[key] = node[key]
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)
    visit(value)
    return found
