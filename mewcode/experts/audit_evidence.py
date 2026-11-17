"""Evidence delivery keys and compact repair handoff for quantified audits."""
from __future__ import annotations

import hashlib
import io
import json
import re
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any

from PIL import Image


IMAGE_DELIVERED = "IMAGE_DELIVERED: yes"
IMAGE_BLOCKED_NO_VISION = (
    "当前模型不具备图像能力，像素未进入模型请求。"
    "禁止继续重复读取；请改用 InspectVisualEvidence 取证；仍不可用时将相关项标为 UNKNOWN。"
)
DUPLICATE_EVIDENCE = (
    "该图像证据已进入模型请求。禁止重复读取。"
    "请转入修改对应图注/正文/图示，或说明仍缺什么材料。"
)
DUPLICATE_IMAGE_PROBE = (
    "该图片已用命令探测过尺寸/裁剪/像素统计。禁止再用 Bash 反复探测。"
    "请使用 ReadAuditArtifact 把像素送入模型；若场景或标签仍无法从已有证据确认，向用户索取，不要用灰度亮度推断。"
)
COMMAND_RETRY_EXHAUSTED = "同一命令第三次仍失败"
_IMAGE_PATH_RE = re.compile(
    r"[\w./\\-]+\.(?:png|jpe?g|gif|webp|bmp|tiff?)",
    re.IGNORECASE,
)
_PROBE_HINT_RE = re.compile(
    r"(?i)\b(?:PIL|Image\.open|imageio|cv2|identify|magick|exiftool|pnginfo|"
    r"imagesize|pixel|crop|grayscale|brightness|histogram|numpy|"
    r"size\s*=|shape\s*=|mean\()\b",
)

_LEDGER: ContextVar["DuplicateEvidenceLedger | None"] = ContextVar(
    "mewcode_audit_evidence_ledger", default=None
)


def evidence_key(pixel_sha256: str, criterion_id: str, crop: str) -> str:
    region = (crop or "").strip() or "full"
    item = (criterion_id or "").strip() or "-"
    return f"{pixel_sha256}|{item}|{region}"


def normalize_crop(crop: str) -> str:
    return (crop or "").strip().replace(" ", "")


def pixel_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def apply_crop(data: bytes, crop: str) -> bytes:
    region = normalize_crop(crop)
    if not region:
        return data
    parts = region.split(",")
    if len(parts) != 4 or not all(part.lstrip("-").isdigit() for part in parts):
        raise ValueError("crop 必须是 x,y,w,h")
    left, top, width, height = (int(part) for part in parts)
    if width <= 0 or height <= 0 or left < 0 or top < 0:
        raise ValueError("裁剪区域必须落在图片内且宽高为正")
    with Image.open(io.BytesIO(data)) as picture:
        right, bottom = left + width, top + height
        if right > picture.width or bottom > picture.height:
            raise ValueError(f"裁剪超出图片范围：原图 {picture.width}x{picture.height}，请求 {region}；"
                             "请按原图像素坐标修正区域，未自动截断或放大")
        cropped = picture.crop((left, top, right, bottom))
        buffer = io.BytesIO()
        cropped.save(buffer, format=picture.format or "PNG")
        return buffer.getvalue()


class DuplicateEvidenceLedger:
    def __init__(
        self,
        delivered: list[str] | None = None,
        probes: list[str] | None = None,
    ) -> None:
        self._delivered: list[str] = []
        self._probes: list[str] = []
        for key in delivered or []:
            self.record(str(key))
        for key in probes or []:
            self.record_probe(str(key))

    def record(self, key: str) -> None:
        if key and key not in self._delivered:
            self._delivered.append(key)

    def contains(self, key: str) -> bool:
        return key in self._delivered

    def record_probe(self, target: str) -> bool:
        key = _posix(target)
        if not key:
            return False
        if key in self._probes:
            return False
        self._probes.append(key)
        return True

    def contains_probe(self, target: str) -> bool:
        return _posix(target) in self._probes

    def as_list(self) -> list[str]:
        return list(self._delivered)

    def probes(self) -> list[str]:
        return list(self._probes)


def current_evidence_ledger() -> DuplicateEvidenceLedger | None:
    return _LEDGER.get()


def bind_evidence_ledger(ledger: DuplicateEvidenceLedger) -> Token:
    return _LEDGER.set(ledger)


def reset_evidence_ledger(token: Token) -> None:
    _LEDGER.reset(token)


def open_checks(accepted: dict[str, Any] | None) -> list[dict[str, Any]]:
    checks = list((accepted or {}).get("checks") or [])
    return [check for check in checks if str(check.get("state") or "") != "PASS"]


_PATH_RE = re.compile(
    r"[\w./\\-]+\.(?:docx|tex|pdf|png|jpe?g|txt|md|bib)",
    re.IGNORECASE,
)
_DELIVERY_SUFFIXES = {".docx", ".tex", ".pdf", ".md", ".txt", ".bib"}
_SCRATCH_TOKENS = (
    "crop", "preview", "extract", "tmp", "temp", "scratch", "_label",
    "debug",
)


def _posix(path: str) -> str:
    return str(path or "").replace("\\", "/").strip()


def _basename(path: str) -> str:
    return _posix(path).rsplit("/", 1)[-1]


def is_scratch_path(path: str) -> bool:
    name = _posix(path).lower()
    if any(part in name for part in ("/tmp/", "/scratch/", "/crops/", "/labels/", ".mewcode/")):
        return True
    stem = _basename(name)
    return any(token in stem for token in _SCRATCH_TOKENS)


def mentioned_delivery_paths(accepted: dict[str, Any] | None, issues: list[Any] | None) -> list[str]:
    blobs: list[str] = []
    for check in (accepted or {}).get("checks") or []:
        if isinstance(check, dict):
            blobs.extend(str(check.get(key) or "") for key in ("location", "evidence", "acceptance_test"))
    for issue in issues or []:
        if isinstance(issue, dict):
            blobs.extend(str(value) for value in issue.values())
        else:
            blobs.append(str(issue))
    found: list[str] = []
    seen: set[str] = set()
    for blob in blobs:
        for match in _PATH_RE.findall(blob):
            path = _posix(match)
            key = _basename(path).lower()
            if key and key not in seen:
                seen.add(key)
                found.append(path)
    return found


def split_delivery_and_scratch(
    paths: list[str] | None,
    *,
    accepted: dict[str, Any] | None = None,
    issues: list[Any] | None = None,
) -> tuple[list[str], list[str]]:
    mentioned = {_basename(path).lower() for path in mentioned_delivery_paths(accepted, issues)}
    delivery: list[str] = []
    scratch: list[str] = []
    for raw in paths or []:
        path = _posix(raw)
        if not path:
            continue
        name = _basename(path).lower()
        suffix = ""
        if "." in name:
            suffix = "." + name.rsplit(".", 1)[-1]
        if is_scratch_path(path) and name not in mentioned:
            scratch.append(path)
            continue
        if name in mentioned or suffix in _DELIVERY_SUFFIXES:
            delivery.append(path)
            continue
        if suffix in {".png", ".jpg", ".jpeg"} and name not in mentioned:
            scratch.append(path)
            continue
        delivery.append(path)
    return delivery, scratch


def infer_repair_step(
    state: dict[str, Any],
    *,
    changed: bool,
    delivery_changed: bool | None = None,
) -> str:
    saved = str(state.get("repair_step") or "")
    progress = state.get("repair_progress") or {}
    if delivery_changed is None:
        delivery_changed = bool(progress.get("edited_delivery"))
    if saved in {"verify", "handoff"}:
        return saved
    if delivery_changed or progress.get("edited_delivery"):
        return "modify"
    return "locate"


def _dedupe(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for path in paths:
        key = _posix(path)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def compact_repair_activity(tools: dict[str, Any] | None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in (tools or {}).values():
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        args = item.get("args") if isinstance(item.get("args"), dict) else {}
        path = _posix(str(args.get("file_path") or args.get("path") or ""))
        if name in {"ReadFile", "ReadAuditArtifact"} and path:
            records.append({"kind": "read", "path": path, "ok": not item.get("is_error")})
        elif name in {"EditFile", "WriteFile"} and path:
            records.append({"kind": "edit", "path": path})
        elif name == "report_step":
            label = str(args.get("label") or "").strip()
            if label:
                records.append({
                    "kind": "step",
                    "label": label,
                    "status": str(args.get("status") or "running"),
                })
        elif name == "Bash":
            command = str(args.get("command") or "")
            targets = image_probe_targets(command)
            records.append({
                "kind": "probe" if targets else "command",
                "command": command[:200],
                "paths": targets,
                "ok": not item.get("is_error"),
            })
        elif name in {"Glob", "Grep"}:
            records.append({
                "kind": "search",
                "pattern": str(args.get("pattern") or args.get("glob") or "")[:200],
            })
    return records[-40:]


def looks_like_image_probe(command: str) -> bool:
    text = str(command or "")
    if not text.strip():
        return False
    has_image = bool(_IMAGE_PATH_RE.search(text) or re.search(r"(?i)\b(?:image|figure|fig\d|png|jpeg)\b", text))
    return has_image and bool(_PROBE_HINT_RE.search(text) or re.search(
        r"(?i)\b(?:python|print|stat|file |identify|hash)\b", text,
    ))


def image_probe_targets(command: str) -> list[str]:
    if not looks_like_image_probe(command):
        return []
    found = [_posix(match) for match in _IMAGE_PATH_RE.findall(command)]
    return _dedupe(found) or ["<unnamed-image>"]


def issue_contracts(accepted: dict[str, Any] | None, issues: list[Any] | None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    open_items = open_checks(accepted)
    by_criterion: dict[str, list[dict[str, Any]]] = {}
    for issue in issues or []:
        if not isinstance(issue, dict):
            continue
        criterion = str(issue.get("criterion_id") or "")
        by_criterion.setdefault(criterion, []).append(issue)
    mentioned = mentioned_delivery_paths(accepted, issues)
    for check in open_items:
        criterion = str(check.get("criterion_id") or "")
        related = by_criterion.get(criterion) or [{
            "issue_id": criterion, "criterion_id": criterion,
            "acceptance_test": check.get("acceptance_test") or "",
        }]
        files = mentioned_delivery_paths({"checks": [check]}, related) or mentioned
        for issue in related:
            items.append({
                "issue_id": str(issue.get("issue_id") or criterion),
                "criterion_id": criterion,
                "files": files[:8],
                "allowed_edits": (
                    "仅修改这些交付文件中与该 issue 对应的图注、正文或图示；"
                    "禁止改无关章节；裁剪图/标签图不算完成。"
                ),
                "acceptance_test": str(
                    issue.get("acceptance_test") or check.get("acceptance_test") or ""
                ),
                "if_unknown": (
                    "已有图像仍无法确认场景或标签时，向用户索取该信息；"
                    "不要用灰度、亮度或反复裁剪推断。"
                ),
            })
    return items


def repair_progress_signature(state: dict[str, Any]) -> list[str]:
    progress = state.get("repair_progress") if isinstance(state.get("repair_progress"), dict) else {}
    issues = ((state.get("baseline_result") or {}).get("verdict") or {}).get("issues")
    delivery, _scratch = split_delivery_and_scratch(
        list(state.get("changed_files") or []) + list(progress.get("edited_delivery") or []),
        accepted=state.get("accepted"),
        issues=issues,
    )
    evidence = [
        str(key) for key in (state.get("delivered_evidence") or [])
        if str(key) and not str(key).startswith("probe:")
    ]
    edited = [_posix(path) for path in (progress.get("edited_delivery") or [])]
    return [
        "evidence:" + ",".join(evidence),
        "edited:" + ",".join(edited),
        "delivery:" + ",".join(_posix(path) for path in delivery),
    ]


def stall_without_progress(state: dict[str, Any]) -> bool:
    current = repair_progress_signature(state)
    previous = list(state.get("progress_signature") or [])
    state["progress_signature"] = current
    if current == previous:
        return True
    return current == ["evidence:", "edited:", "delivery:"]


def remaining_repair_budget(
    state: dict[str, Any],
    *,
    max_iterations: int,
    timeout_seconds: float,
    resume: bool,
) -> tuple[int, float]:
    if not resume:
        return int(max_iterations), float(timeout_seconds)
    budget = state.get("repair_budget") if isinstance(state.get("repair_budget"), dict) else {}
    if int(budget.get("round") or 0) != int(state.get("round") or 0):
        return int(max_iterations), float(timeout_seconds)
    if budget.get("exhausted"):
        return 0, 0.0
    used_iter = int(budget.get("iterations_used") or 0)
    used_time = float(budget.get("elapsed_seconds") or 0.0)
    return (
        max(0, int(max_iterations) - used_iter),
        max(0.0, float(timeout_seconds) - used_time),
    )


def charge_repair_budget(
    state: dict[str, Any],
    *,
    elapsed_seconds: float,
    iterations_used: int = 0,
    exhausted: bool = False,
) -> dict[str, Any]:
    previous = state.get("repair_budget") if isinstance(state.get("repair_budget"), dict) else {}
    same_round = int(previous.get("round") or 0) == int(state.get("round") or 0)
    budget = {
        "round": int(state.get("round") or 0),
        "elapsed_seconds": (
            (float(previous.get("elapsed_seconds") or 0.0) if same_round else 0.0)
            + max(0.0, float(elapsed_seconds))
        ),
        "iterations_used": (
            (int(previous.get("iterations_used") or 0) if same_round else 0)
            + max(0, int(iterations_used))
        ),
        "exhausted": bool((previous.get("exhausted") if same_round else False) or exhausted),
    }
    state["repair_budget"] = budget
    return budget


def reusable_baseline(state: dict[str, Any], baseline_hash: str) -> dict[str, Any] | None:
    saved = state.get("baseline_result")
    accepted = state.get("accepted")
    if not isinstance(saved, dict) or not saved.get("verdict"):
        return None
    if not isinstance(accepted, dict):
        return None
    if str(accepted.get("artifact_hash") or "") != str(baseline_hash or ""):
        return None
    return saved


def _command_fingerprint(command: str) -> str:
    return " ".join(str(command or "").split())


def _missing_path_error(output: str) -> bool:
    lowered = str(output or "").lower()
    return any(token in lowered for token in (
        "no such file", "cannot find the path", "not found",
        "系统找不到", "无法找到", "file not found",
    ))


def suggest_command_fix(command: str, work_dir: str | None) -> str | None:
    text = str(command or "")
    if not work_dir or not text.strip():
        return None
    search_root = Path(work_dir)
    matches = list(_IMAGE_PATH_RE.findall(text)) + list(_PATH_RE.findall(text))
    for match in matches:
        raw = str(match).replace("\\", "/")
        current = Path(raw)
        if current.is_absolute() and current.exists():
            continue
        candidate = (search_root / raw) if not current.is_absolute() else current
        try:
            exists = candidate.exists()
        except OSError:
            exists = False
        if exists:
            return text.replace(str(match), str(candidate), 1)
        name = current.name
        if not name or "." not in name:
            continue
        try:
            found = [path for path in search_root.rglob(name) if path.is_file()][:4]
        except OSError:
            found = []
        if len(found) == 1:
            return text.replace(str(match), str(found[0]), 1)
    return None


def wrap_repair_command(tool: Any, *, work_dir: str | None = None) -> Any:
    """Block repeated Bash image probes and retry a corrected command once."""
    from mewcode.tools.base import Tool, ToolResult, active_work_dir

    if not isinstance(tool, Tool) or tool.name != "Bash":
        return tool

    class RepairCommandGuard(Tool):
        name = tool.name
        description = tool.description
        params_model = tool.params_model
        category = tool.category
        is_concurrency_safe = tool.is_concurrency_safe
        is_system_tool = getattr(tool, "is_system_tool", False)
        should_defer = getattr(tool, "should_defer", False)
        consumes_runtime_slot = getattr(tool, "consumes_runtime_slot", True)

        def __init__(self) -> None:
            self._inner = tool
            self._work_dir = work_dir
            self._failures: dict[str, int] = {}
            self._auto_fixed: set[str] = set()

        def permission_risk(self, arguments: dict[str, Any]):
            return self._inner.permission_risk(arguments)

        def _record_failure(self, command: str) -> int:
            key = _command_fingerprint(command)
            self._failures[key] = int(self._failures.get(key) or 0) + 1
            return self._failures[key]

        async def _guarded(self, params: Any, runner: Any) -> Any:
            command = str(getattr(params, "command", "") or "")
            targets = image_probe_targets(command)
            ledger = current_evidence_ledger()
            if targets and ledger is not None:
                blocked = [item for item in targets if ledger.contains_probe(item)]
                if blocked:
                    return ToolResult(output=DUPLICATE_IMAGE_PROBE + " 目标：" + ", ".join(blocked), is_error=True)
                for item in targets:
                    ledger.record_probe(item)
            result = await runner(params)
            if not getattr(result, "is_error", False):
                return result
            output = str(getattr(result, "output", "") or "")
            count = self._record_failure(command)
            if count >= 3:
                return ToolResult(output=COMMAND_RETRY_EXHAUSTED + "：" + command[:300], is_error=True)
            key = _command_fingerprint(command)
            if key not in self._auto_fixed and _missing_path_error(output):
                fixed = suggest_command_fix(command, self._work_dir or active_work_dir())
                if fixed and _command_fingerprint(fixed) != key:
                    self._auto_fixed.add(key)
                    patched = params
                    if hasattr(params, "model_copy"):
                        patched = params.model_copy(update={"command": fixed})
                    elif hasattr(params, "command"):
                        try:
                            params.command = fixed
                        except (AttributeError, TypeError):
                            pass
                    retry = await runner(patched)
                    if not getattr(retry, "is_error", False):
                        note = f"已自动修正命令路径后执行成功：{fixed}"
                        return ToolResult(
                            output=note + "\n" + str(getattr(retry, "output", "") or ""),
                            is_error=False,
                        )
                    self._record_failure(command)
                    if self._failures[key] >= 3:
                        return ToolResult(output=COMMAND_RETRY_EXHAUSTED + "：" + command[:300], is_error=True)
                    return retry
            return result

        async def execute(self, params: Any) -> Any:
            return await self._guarded(params, self._inner.execute)

        async def run(self, params: Any, *, work_dir: str | None = None) -> Any:
            async def runner(item: Any) -> Any:
                return await self._inner.run(item, work_dir=work_dir)

            return await self._guarded(params, runner)

    return RepairCommandGuard()


def _remaining_actions(progress: dict[str, Any]) -> str:
    edited = progress.get("edited_delivery") or []
    located = progress.get("located_files") or []
    delivery = progress.get("delivery_files") or []
    if edited:
        pending = [path for path in delivery if _basename(path) not in {_basename(item) for item in edited}]
        if pending:
            return "同步其余交付文件（" + ", ".join(pending[:6]) + "）并验证后提交复审"
        return "验证已改交付文件并提交配对复审，不要重新全局检索"
    if located or delivery:
        targets = ", ".join((delivery or located)[:6])
        return f"根据已定位文件完成对应的最小修改（{targets}），不要重新全面盘点"
    return "根据未通过项的 location 锁定相关文件及失败证据后再修改"


def build_repair_progress(
    *,
    repaired: Any,
    changed_files: list[str] | None,
    accepted: dict[str, Any] | None,
    issues: list[Any] | None,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    previous = previous or {}
    activity = list(getattr(repaired, "activity_records", None) or [])
    located = list(previous.get("located_files") or [])
    edited = list(previous.get("edited_delivery") or [])
    steps = list(previous.get("steps") or [])
    for item in activity:
        kind = str(item.get("kind") or "")
        path = _posix(str(item.get("path") or ""))
        if kind == "read" and path:
            located.append(path)
        elif kind == "edit" and path:
            edited.append(path)
        elif kind == "step" and item.get("label"):
            steps.append(f"{item.get('status') or 'running'}:{item['label']}")
    delivery, scratch = split_delivery_and_scratch(
        list(changed_files or []) + edited,
        accepted=accepted,
        issues=issues,
    )
    mentioned = mentioned_delivery_paths(accepted, issues)
    delivery_files = _dedupe(list(previous.get("delivery_files") or []) + mentioned + delivery)
    edited_delivery = []
    for path in edited + delivery:
        name = _basename(path).lower()
        if is_scratch_path(path) and name not in {_basename(item).lower() for item in mentioned}:
            continue
        edited_delivery.append(path)
    text = str(getattr(repaired, "text", "") or previous.get("text") or "")[:2000]
    progress = {
        "text": text,
        "status": str(getattr(repaired, "status", "") or ""),
        "located_files": _dedupe(located)[-20:],
        "edited_delivery": _dedupe(edited_delivery)[-20:],
        "scratch_files": _dedupe(list(previous.get("scratch_files") or []) + scratch)[-20:],
        "delivery_files": delivery_files[:20],
        "steps": steps[-12:],
        "remaining": "",
    }
    if not progress["edited_delivery"] and delivery and not scratch:
        progress["edited_delivery"] = list(delivery)
    progress["remaining"] = _remaining_actions(progress)
    return progress


def compact_repair_handoff(
    *,
    step: str,
    open_items: list[dict[str, Any]],
    delivered_evidence: list[str],
    changed_files: list[str],
    notes: str = "",
    delivery_files: list[str] | None = None,
    scratch_files: list[str] | None = None,
    located_files: list[str] | None = None,
    remaining: str = "",
    edited_delivery: list[str] | None = None,
    domain: str = "",
) -> dict[str, Any]:
    from mewcode.experts.repair_domain import repair_domain
    domain = domain or repair_domain(notes, delivery_files or changed_files)
    visual = domain in {"document", "visual"}
    edited = list(edited_delivery or [])
    if not edited:
        edited = [path for path in (changed_files or []) if path in (delivery_files or [])]
    lines = [
        "【轻量接续】从上次中断处继续，不要重新全面盘点。",
        f"当前步骤：{step}",
        "未通过项：" + json.dumps(open_items, ensure_ascii=False),
        "验收交付文件：" + ", ".join(delivery_files or ["尚未锁定"]),
        "已定位文件：" + ", ".join(located_files or ["无"]),
        "已修改的交付文件：" + ", ".join(edited or ["无"]),
        "临时文件（不算完成定位/修改）：" + ", ".join(scratch_files or ["无"]),
    ]
    if notes.strip():
        lines.append("已确认结论：" + notes.strip()[:2000])
    lines.append("剩余动作：" + (remaining.strip() or "完成交付文件修改并验证后提交复审"))
    if delivered_evidence and visual:
        lines.append(
            "下列图像像素已进入过模型请求，同一 PIXEL_SHA256+验收项+裁剪不要再调用 ReadAuditArtifact："
            + ", ".join(delivered_evidence)
        )
        lines.append("读取 Word/LaTeX、计算哈希、打开未在上述键中的图片仍允许。")
    elif visual:
        lines.append("尚未经 ReadAuditArtifact 交付图像像素；定位和修改所需的读文件、读段落、计算哈希仍允许。")
    else:
        lines.append("读取相关文件、计算哈希与检查新证据仍允许；不要重复已失败的方法。")
    return {
        "repair_step": step,
        "open_items": open_items,
        "delivered_evidence": list(delivered_evidence),
        "changed_files": list(changed_files),
        "delivery_files": list(delivery_files or []),
        "scratch_files": list(scratch_files or []),
        "text": "\n".join(lines),
    }


def stage_brief_for_safety(stage_prompt: str) -> str:
    from mewcode.experts.recovery_scope import current_approved_revision

    text = (stage_prompt or "").strip()
    scope = current_approved_revision()
    approved = ""
    if scope:
        approved = (
            "本次计划修订及其有界执行已批准。"
            "读取交付文件、修改允许路径、运行验收检查不再逐次弹出无关权限卡。"
        )
    if "【候选稿修复】" in text or "【轻量接续】" in text:
        return (
            (approved + "\n" if approved else "")
            + "在候选目录内修复冻结验收未通过项。"
            "在既有授权内读取相关文件、计算哈希、修改候选交付文件和执行定向验证。"
            "原项目及基线不得修改，命令仍须经过现有权限审查。重复证据检测不等于禁止正常读取。"
        )
    if approved:
        text = approved + "\n" + text
    if len(text) <= 3500:
        return text
    # Summarize repetitive background without hiding the tail of a handoff.
    # Exact delegated commands are separately supplied by the host, and the
    # full text is retained in the review source for evidence-gap recovery.
    return text[:2200] + "\n[背景摘要；完整内容见 FULL_REVIEW_SOURCE]\n" + text[-1200:]


def repair_instructions(
    *,
    candidate_dir: str,
    original_dir: str,
    rubric: dict[str, Any],
    accepted: dict[str, Any],
    issues: list[Any],
    step: str,
    delivered_evidence: list[str],
    previous_attempt: dict[str, Any],
    max_iterations: int,
    changed_files: list[str] | None = None,
    notes: str = "",
    resume: bool = False,
    delivery_files: list[str] | None = None,
    scratch_files: list[str] | None = None,
    located_files: list[str] | None = None,
    remaining: str = "",
    edited_delivery: list[str] | None = None,
    domain: str = "",
    verification_environment: dict[str, Any] | None = None,
) -> str:
    items = open_checks(accepted)
    delivery = list(delivery_files or mentioned_delivery_paths(accepted, issues))
    from mewcode.experts.repair_domain import repair_domain
    domain = domain or repair_domain(" ".join(str(item.get("acceptance_test") or "") for item in items), delivery)
    visual = domain in {"document", "visual"}
    contracts = issue_contracts(accepted, issues)
    handoff = compact_repair_handoff(
        step=step,
        open_items=items,
        delivered_evidence=delivered_evidence,
        changed_files=changed_files or [],
        notes=notes,
        delivery_files=delivery,
        scratch_files=scratch_files,
        located_files=located_files,
        remaining=remaining,
        edited_delivery=edited_delivery,
        domain=domain,
    )
    payload = {
        "candidate_dir": candidate_dir,
        "original_dir": original_dir,
        "open_items": items,
        "issues": issues,
        "issue_contracts": contracts,
        "delivered_evidence": delivered_evidence,
        "previous_attempt": previous_attempt,
        "repair_step": step,
        "delivery_files": delivery,
        "located_files": list(located_files or []),
        "remaining": remaining,
        "task_domain": domain,
        "verification_environment": verification_environment or {},
    }
    prefix = handoff["text"] + "\n" if resume else ""
    ledger_rule = (
        "ReadAuditArtifact 成功读取图片时输出 IMAGE_DELIVERED: yes，表示像素已进入模型请求。"
        "同一 PIXEL_SHA256＋验收项＋裁剪已交付后，不要再调用 ReadAuditArtifact 读该键；"
        "读取 Word/LaTeX、计算哈希、打开尚未交付的图片仍允许。"
        "禁止用 Bash 反复探测同一图片的尺寸、裁剪、灰度或像素统计。\n"
        if delivered_evidence else
        "尚未交付图像像素。定位和修改所需的读文件、读段落、计算哈希均允许；"
        "不要把“避免无收益重复看图”理解成禁止读取交付文件。"
        "图像核验必须通过 ReadAuditArtifact 把像素送入模型，不要用 Bash 输出尺寸文本代替视觉证据。\n"
    )
    if not visual:
        ledger_rule = "读取相关源文件、失败测试和已有日志仍允许；验证必须针对候选内容，不能用原项目测试通过冒充候选通过。\n"
    contract_lines = []
    for item in contracts:
        contract_lines.append(
            f"- {item['issue_id']}（{item['criterion_id']}）：文件 "
            + ", ".join(item["files"] or ["按 location 锁定"])
            + f"；完成条件：{item['acceptance_test']}"
        )
    contract_block = (
        "【执行契约】按 issue_id 分开处理，不要把不同问题混成一个宽泛调查：\n"
        + ("\n".join(contract_lines) + "\n" if contract_lines else "按未通过项 location 锁定具体文件。\n")
        + ("场景标签不能从灰度亮度可靠推断；所需视觉信息在一次 ReadAuditArtifact 后仍不可用时，向用户索取。\n" if visual else "")
    )
    if domain == "code":
        steps = ("1. 定位失败测试：读取失败命令、堆栈、相关源文件，确认导入和执行对象是候选代码。\n"
                 "2. 最小修改：只修对应缺陷，不额外重构或降低测试标准。\n"
                 "3. 验证本次修改：用 VerifyCandidate 运行相关 pytest/unittest/npm_test；依赖按需复制为独立快照，原环境不写入。\n"
                 "4. 交付复审：提交实际退出码、工作目录、测试输出与基线/候选 diff；没有 .git 时不要反复运行 git diff。\n")
    elif visual:
        steps = ("1. 定位未通过项：根据未通过项锁定 Word/LaTeX/图片/PDF 对应关系，列出文件位置。\n"
                 "2. 修改对应图注/正文/图示：只改与未通过项对应的交付文件。\n"
                 "3. 验证本次修改：只核验刚改的内容，不重新全面盘点论文。\n"
                 "4. 交付复审：提交配对复审所需的问题清单、已改文件和关键证据。\n")
    else:
        steps = ("1. 定位未通过项：锁定相关输入、交付物及失败证据。\n"
                 "2. 最小修改：只改变与未通过项对应的内容。\n"
                 "3. 验证本次修改：使用符合本任务的验证方法，不重新全面盘点。\n"
                 "4. 交付复审：提交已改文件、实际验证结果与未解决项。\n")
    return (
        prefix
        + "【候选稿修复】仅修改候选目录内文件，原项目及 baseline 只读。"
        "把任务中原项目的绝对路径改为候选目录相应路径。"
        "只使用已有证据，不编造场景属性或验收材料。"
        "不得编造原始数据、场景属性或验收证据，不得自行降低冻结验收标准。"
        "UNKNOWN 项先在授权范围内查找已有证据、同步可生成的交付物或纠正无依据表述；"
        "若确实需要用户独有材料或新授权，明确所缺材料及已尝试方法并请求用户，不能假报完成。\n"
        + contract_block
        + "必须按顺序推进，完成当前一步再进入下一步，不得用宽泛指令独立驱动整轮：\n"
        + steps
        + f"当前步骤：{step}\n"
        "本次验收交付文件：" + ", ".join(delivery or ["按未通过项 location 锁定"]) + "。"
        + ("裁剪图、标签图、预览图等临时文件不算已完成定位或已完成修改。\n" if visual else "临时日志和依赖快照不算已完成业务修改。\n")
        + ledger_rule
        + f"本次最多 {max_iterations} 轮；预留末尾轮次用于验证和交接。\n"
        + json.dumps(payload, ensure_ascii=False)
    )
