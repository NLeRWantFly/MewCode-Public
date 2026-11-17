"""Host-owned item review records; evidence channels never select the judge."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from mewcode.tools.base import Tool, ToolResult
from mewcode.tools.artifact_read_cache import ReadUsage, SourceVersion, source_fingerprint
from mewcode.tools.check_rendering import CheckRendering, CheckRenderingParams
from mewcode.experts.repair_lifecycle import (candidate_artifacts_changed,
    item_record_revision, needs_new_candidate, repair_basis)
from mewcode.experts.review_evidence import (
    RAW_FILE_DIGEST_KIND,
    TEXT_DIGEST_KIND,
    content_digest,
    evidence_content,
    file_digest,
    queue_progress,
    typed_text_receipt,
    canonical_review_state,
)
from mewcode.tools.verify_artifacts import VerifyArtifacts, VerifyArtifactsParams


class ReviewEvidence(BaseModel):
    path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    quote: str = Field(min_length=1, max_length=2000)


class ReviewWorkQueueParams(BaseModel):
    action: Literal["initialize", "next", "evidence", "record", "summary", "read"]
    manifest_path: str = ""
    manifest_sha256: str = ""
    items_path: str = Field(default="", description="JSON 对象中的数组路径，用点分隔；根数组留空")
    filters: dict[str, list[str]] = Field(default_factory=dict,
        description="仅使用任务明确指定的筛选范围，例如 tier=[T2,T3]；筛选规则随清单冻结")
    key_fields: list[str] = Field(default_factory=lambda: ["doc_id", "pdf_page"], max_length=8)
    expected_count: int = Field(default=0, ge=0, le=10000)
    image_path_template: str = Field(default="",
        description="任务原图路径模板，例如 gemini_out/_pages/{doc_id}/page_{pdf_page:04d}.png；只能用唯一键字段")
    limit: int = Field(default=5, ge=1, le=10)
    offset: int = Field(default=0, ge=0, le=10000)
    item_id: str = ""
    candidate_revision: str = Field(default="", description="返修复核必须引用next返回的candidate_revision并读取候选文本证据")
    state: Literal['pending', 'pass', "complete", "needs_repair", "unknown"] = "unknown"
    summary: str = Field(default="", max_length=4000)
    evidence: list[ReviewEvidence] = Field(default_factory=list, max_length=12)
    issue_ids: list[str] = Field(default_factory=list, max_length=30)
    evidence_ids: list[str] = Field(default_factory=list, max_length=12,
        description="本项 evidence 动作返回的宿主引用 ID；无需手工复制 SHA256 和引文")
    source_path: str = Field(default="", description="evidence：两侧之一的 UTF-8 文本或 JSON 文件，不能是图片")
    source_items_path: str = Field(default="", description="evidence：JSON 数组路径；根数组留空")
    source_selector: dict[str, str | int] = Field(default_factory=dict,
        description="evidence：精确选择单页 JSON 块，如 page_idx=物理页减1，必须按实际来源映射")
    state_filter: Literal["", 'pending', 'pass', "complete", "needs_repair", "unknown"] = ""


class ReviewWorkQueue(Tool):
    name = "ReviewWorkQueue"
    category = "read"
    is_system_tool = True
    params_model = ReviewWorkQueueParams
    description = ("宿主持久化逐项审阅台账，不修改业务文件。initialize 必须使用任务指定的完整清单、"
        "准确 SHA256 和数量；不能创建抽样清单替代。next 返回未完成项；读取原图时 criterion_id 使用 item_id，"
        "两侧文本用 evidence 获取引用ID（JSON可按页selector），随后 record(evidence_ids=...)；图像不能放入文本evidence。"
        "文本 sha256 是换行规范化后的审查摘要，file_sha256 才是原始文件字节摘要，两者不得混用。"
        "存在真实缺陷用 needs_repair、无法确认用 unknown；next 不重复派发本轮已记录的未决项，应交执行者补齐。"
        "状态为 pending/pass/needs_repair/unknown；只有已核实无未决缺陷的项可 pass（旧 complete 输入兼容）。"
        "证据按 text/pixel/command 通道登记；旧聊天和机器意见不能替代实际图像证据。")

    def __init__(self, store, run_id, node_id, root, contract_hash, *, review_round=0, review_channels=None):
        self.store, self.run_id, self.node_id = store, run_id, node_id
        self.root = Path(root).resolve()
        self.contract_hash = contract_hash
        self.on_progress = None
        self.review_round = review_round
        from mewcode.orchestration.spec import parse_review_channels
        self.review_channels = parse_review_channels(review_channels) if review_channels is not None else ('text', 'pixel')

    def _path(self, value, *, pdf_source=False):
        path = (self.root / value).resolve(strict=True)
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError("证据必须是当前工作区内文件")
        limit = 2 * 1024 * 1024 * 1024 if pdf_source and path.suffix.lower() == ".pdf" else 64 * 1024 * 1024
        if path.stat().st_size > limit:
            raise ValueError("证据文件超过读取上限")
        return path

    def _digest(self, value):
        path = self._path(value, pdf_source=True)
        if path.suffix.lower() == ".pdf":
            return source_fingerprint(SourceVersion.read(path), ReadUsage())
        return file_digest(path)

    def _queue(self, snapshot=None):
        if snapshot is None and hasattr(self.store, "load_review_queue"):
            queue = self.store.load_review_queue(self.run_id, self.node_id)
        else:
            if snapshot is None:
                snapshot = self.store.load(self.run_id) or {}
            queue = (snapshot.get("review_queues") or {}).get(self.node_id)
        if queue and queue["contract_hash"] != self.contract_hash:
            raise ValueError("验收合同已变化，旧台账不可当作新验收；请通过计划修订迁移")
        return queue

    def _valid(self, queue):
        if self._digest(queue["manifest_path"]) != queue["manifest_sha256"]:
            raise ValueError("任务清单已变化，保留旧记录；请重新核对计划版本")

    def progress(self):
        queue = self._queue()
        if not queue:
            return None
        return {**queue_progress(queue),
                "location": f"expert-run:{self.run_id}/review-queue/{self.node_id}",
                "manifest_path": queue["manifest_path"], "manifest_sha256": queue["manifest_sha256"],
                'command_evidence': list(queue.get('command_receipts', {}).values())[-12:]}

    def _evidence_digest(self, evidence):
        if evidence.get("typed_text"):
            return content_digest(evidence_content(self._path(evidence["path"]),
                items_path=evidence.get("items_path", ""), selector=evidence.get("selector")))
        return self._digest(evidence["path"])

    async def _verify_candidate_for_record(self, candidate, item):
        """Run host-owned candidate integrity and rendering checks before a verdict."""
        verifier = VerifyArtifacts(self.store, self.run_id, self.node_id, self.root,
                                   self.contract_hash)
        result = await verifier.execute(VerifyArtifactsParams(
            item_id=candidate["item_id"],
            candidate_revision=candidate["candidate_revision"],
            sources=candidate["sources"],
            issue_ids=candidate["issue_ids"],
        ))
        if result.is_error:
            raise ValueError(f"候选宿主完整性预检失败：{result.output}")
        try:
            checked = json.loads(result.output)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("候选宿主完整性预检返回无效结果") from exc
        if (checked.get("status") != "PASS"
                or checked.get("candidate_revision") != candidate["candidate_revision"]):
            raise ValueError("候选宿主完整性预检未通过")

        identity = item.get("identity") or {}
        raw_page = identity.get("pdf_page", 0)
        page = raw_page if isinstance(raw_page, int) and not isinstance(raw_page, bool) else 0
        renderer = CheckRendering(self.root)
        rendered_result = await renderer.execute(CheckRenderingParams(
            markdown_path=candidate["candidate"]["path"],
            document=str(identity.get("doc_id", "")),
            page=page,
        ))
        if rendered_result.is_error:
            raise ValueError(f"候选渲染预检失败：{rendered_result.output}")
        try:
            rendered = json.loads(rendered_result.output)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("候选渲染预检返回无效结果") from exc
        if (rendered.get("status") != "PASS"
                or rendered.get("markdown_sha256") != candidate["candidate"]["sha256"]):
            raise ValueError("候选渲染预检未通过或候选 Markdown 指纹已变化")
        return rendered.get("images") or []

    def _upgrade_evidence_metadata(self, queue):
        """Add digest semantics to legacy receipts without changing decisions."""
        changed = False

        def upgrade(receipt):
            nonlocal changed
            if receipt.get("typed_text"):
                if receipt.get("digest_kind") != TEXT_DIGEST_KIND:
                    receipt["digest_kind"] = TEXT_DIGEST_KIND
                    changed = True
                try:
                    valid = self._evidence_digest(receipt) == receipt["sha256"]
                except (OSError, ValueError, KeyError, TypeError):
                    valid = False
                if valid and not receipt.get("file_sha256"):
                    receipt["file_sha256"] = self._digest(receipt["path"])
                    receipt["file_digest_kind"] = RAW_FILE_DIGEST_KIND
                    changed = True
            elif receipt.get("digest_kind") != RAW_FILE_DIGEST_KIND:
                receipt["digest_kind"] = RAW_FILE_DIGEST_KIND
                changed = True

        def apply(snapshot):
            nonlocal changed
            saved = self._queue(snapshot)
            for record in saved.get("records", {}).values():
                for receipt in record.get("evidence", []) + record.get("images", []):
                    upgrade(receipt)
            for receipts in saved.get("text_receipts", {}).values():
                for receipt in receipts.values():
                    upgrade(receipt)
            for receipts in saved.get("image_receipts", {}).values():
                for receipt in receipts:
                    upgrade(receipt)

        # Work on a copy first so no-op reads do not rewrite the run snapshot.
        probe = copy.deepcopy(queue)
        for record in probe.get("records", {}).values():
            for receipt in record.get("evidence", []) + record.get("images", []):
                upgrade(receipt)
        for receipts in probe.get("text_receipts", {}).values():
            for receipt in receipts.values():
                upgrade(receipt)
        for receipts in probe.get("image_receipts", {}).values():
            for receipt in receipts:
                upgrade(receipt)
        if not changed:
            return False
        changed = False
        self.store.mutate(self.run_id, apply, durable=True)
        return changed or True

    def _actionable(self, queue):
        records = queue["records"]
        pending = [item for item in queue["items"] if item["item_id"] not in records]
        # A known defect isn't new review work until repair/input revision changes.
        pending += [item for item in queue["items"] if item["item_id"] in records
                    and canonical_review_state(records[item["item_id"]]['state']) != 'pass'
                    and (records[item['item_id']]['state'] == 'pending' or (
                    not needs_new_candidate(queue, item["item_id"])
                    and (records[item["item_id"]].get("evidence_stale")
                         or (queue.get("repair_candidates", {}).get(item["item_id"], {}).get("candidate_revision", "")
                             != records[item["item_id"]].get("candidate_revision", ""))
                         or (not queue.get("repair_protocol_version")
                             and records[item["item_id"]].get("review_round", -1) < self.review_round))))]
        return pending

    def dispatch_state(self):
        queue = self._queue()
        if not queue:
            return {}
        progress = self.progress()
        progress["actionable"] = len(self._actionable(queue))
        progress["next_action"] = ("review" if progress["actionable"] else
            "repair" if progress["needs_repair"] else "supply_evidence" if progress["unknown"] else "submit_decision")
        return progress

    def _refresh_evidence(self, queue):
        """Invalidate changed evidence without losing history or trapping completed items."""
        digests = {}
        def current(evidence):
            path = json.dumps(evidence, sort_keys=True, ensure_ascii=False)
            if path not in digests:
                try:
                    digests[path] = self._evidence_digest(evidence)
                except (OSError, ValueError):
                    digests[path] = None
            return digests[path] == evidence["sha256"]
        def candidate_current(key):
            candidate = queue.get("repair_candidates", {}).get(key)
            return not candidate or (all(current(candidate[k]) for k in ("candidate", "diff", "evidence"))
                                     and all(current(e) for e in candidate.get("sources", [])))
        stale = [key for key, record in queue["records"].items()
                 if record["state"] != "unknown"
                 and (not candidate_current(key)
                      or not all(current(e) for e in record["evidence"] + record["images"]
                                 + record.get("rendered_images", [])))]
        if not stale:
            return False
        invalidated = {key: queue["repair_candidates"][key]["candidate_revision"] for key in stale
                       if key in queue.get("repair_candidates", {})
                       and candidate_artifacts_changed(self, queue["repair_candidates"][key])}
        def invalidate(snapshot):
            saved = self._queue(snapshot)
            for key in stale:
                if (saved["records"].get(key) != queue["records"].get(key)
                        or saved.get("repair_candidates", {}).get(key) != queue.get("repair_candidates", {}).get(key)):
                    continue  # Another revision won; never invalidate its receipt.
                old = saved["records"][key]
                saved.setdefault("history", []).append({"item_id": key, "previous": copy.deepcopy(old)})
                saved["records"][key] = {**old, "state": "unknown",
                    "summary": "证据已变化或缺失，必须重新读取并复核。", "evidence_stale": True}
                if key in invalidated:
                    saved["records"][key]["invalidated_candidate_revision"] = invalidated[key]
                saved["revision"] += 1
        self.store.mutate(self.run_id, invalidate, durable=True)
        return True

    def observe_image(self, params, source_sha256, pixel_sha256):
        queue = self._queue()
        item_id = params.criterion_id
        if not queue or item_id not in {item["item_id"] for item in queue["items"]}:
            return
        path = str(self._path(params.file_path, pdf_source=True).relative_to(self.root))
        item = next(item for item in queue["items"] if item["item_id"] == item_id)
        if path != item["image_path"]:
            return  # Neighbouring pages may inform a decision, but cannot replace its own pixels.
        if Path(path).suffix.lower() == ".pdf" and params.page != item["identity"].get("pdf_page"):
            return
        if params.crop:
            return  # A crop alone cannot certify full-page coverage.
        reference = 'pixel:' + content_digest(json.dumps([item_id, path, params.page, params.crop, source_sha256, pixel_sha256]))[:24]
        receipt = {"path": path, "sha256": source_sha256, 'source_sha256': source_sha256,
                   'channel': 'pixel', 'evidence_id': reference, 'criterion_id': item_id,
                   "digest_kind": RAW_FILE_DIGEST_KIND, "pixel_sha256": pixel_sha256,
                   "page": params.page, "crop": params.crop}
        def apply(snapshot):
            current = self._queue(snapshot)
            receipts = current.setdefault("image_receipts", {}).setdefault(item_id, [])
            if receipt not in receipts:
                receipts.append(receipt)
                del receipts[:-12]
        self.store.mutate(self.run_id, apply, durable=True)
        return receipt

    def lookup_evidence(self, reference):
        queue = self._queue() or {}
        for receipts in queue.get('image_receipts', {}).values():
            for receipt in receipts:
                if receipt.get('evidence_id') == reference:
                    return {**copy.deepcopy(receipt), 'file_path': str(self.root / receipt['path'])}
        return None

    def observe_command(self, tool_id, arguments, output):
        """Called only from the host's actual RunVerification result event."""
        queue = self._queue()
        if not queue:
            return None
        try:
            value = json.loads(output)
        except (TypeError, ValueError):
            return None
        if (not isinstance(value, dict) or type(value.get('exit_code')) is not int
                or value.get('diagnostic_only') or value.get('command') != arguments.get('command')
                or value.get('status') != ('PASS' if value['exit_code'] == 0 else 'FAIL')):
            return None
        receipt = {'channel': 'command', 'tool': 'RunVerification', 'tool_id': tool_id,
                   'command': value['command'], 'exit_code': value['exit_code'],
                   'output_sha256': content_digest(output), 'status': value.get('status'),
                   'source': 'host_tool_result'}
        receipt['evidence_id'] = 'command:' + content_digest(json.dumps([self.run_id, self.node_id, receipt], sort_keys=True))[:24]
        def save(snapshot):
            self._queue(snapshot).setdefault('command_receipts', {})[receipt['evidence_id']] = receipt
        self.store.mutate(self.run_id, save, durable=True)
        return receipt

    def exit_blocker(self):
        queue = self._queue()
        if not queue:
            return ""  # Non-batch reviewers retain their existing contract.
        self._valid(queue)
        if self._refresh_evidence(queue):
            return "审阅证据已变化，相关项已转为 UNKNOWN；next 可继续复核"
        progress = self.progress()
        if progress["complete"] != progress["total"]:
            return f"逐项审阅未完成：{progress['complete']}/{progress['total']}；缺陷、UNKNOWN 和缺记录不能 EXIT"
        return ""

    async def execute(self, params):
        try:
            queue = self._queue()
            if params.action == "initialize":
                if (not params.expected_count or not params.key_fields or not params.manifest_sha256
                        or ('pixel' in self.review_channels and not params.image_path_template)):
                    raise ValueError("必须给出任务指定的完整清单、SHA256、唯一键字段、准确数量及原图路径模板")
                path = self._path(params.manifest_path)
                raw = path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                if digest != params.manifest_sha256:
                    raise ValueError("清单 SHA256 与任务指定值不符")
                payload = json.loads(raw)
                for key in params.items_path.split(".") if params.items_path else []:
                    payload = payload[key]
                if not isinstance(payload, list):
                    raise ValueError("清单必须为数组")
                source_count = len(payload)
                if any(not isinstance(item, dict) for item in payload):
                    raise ValueError("清单每项必须为对象")
                if params.filters:
                    payload = [item for item in payload if all(str(item.get(key)) in values
                               for key, values in params.filters.items())]
                if len(payload) != params.expected_count:
                    raise ValueError("清单必须是准确数量的数组；不得抽样或漏项")
                items, keys = [], set()
                for value in payload:
                    identity = [value[key] for key in params.key_fields]
                    if any(v is None or isinstance(v, (dict, list, bool)) or str(v).strip() == "" for v in identity):
                        raise ValueError("清单唯一键无效")
                    canonical = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
                    if canonical in keys:
                        raise ValueError("清单存在重复项，不能建立队列")
                    keys.add(canonical)
                    identity_map = dict(zip(params.key_fields, identity))
                    image_path = None
                    if params.image_path_template:
                        image_path = (self.root / params.image_path_template.format_map(identity_map)).resolve()
                        if not image_path.is_relative_to(self.root) or image_path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".pdf"}:
                            raise ValueError("原图模板必须指向工作区内图片或 PDF")
                    items.append({"item_id": "review-" + hashlib.sha256(canonical.encode()).hexdigest()[:24],
                                  "identity": identity_map, "image_path": str(image_path.relative_to(self.root)) if image_path else '', "input": value})
                definition = {"manifest_path": str(path.relative_to(self.root)), "manifest_sha256": digest,
                    "items_path": params.items_path, "key_fields": params.key_fields,
                    "filters": params.filters, "source_count": source_count,
                    "image_path_template": params.image_path_template,
                    "contract_hash": self.contract_hash, "items": items,
                    'review_channels': list(self.review_channels)}
                def initialize(snapshot):
                    existing = self._queue(snapshot)
                    if existing:
                        if any(existing.get(k, ['text', 'pixel'] if k == 'review_channels' else None) != v for k, v in definition.items()):
                            raise ValueError("队列已冻结，不能替换来源、缩小范围或清除既有进度")
                        return
                    snapshot.setdefault("review_queues", {})[self.node_id] = {
                        **definition, "records": {}, "image_receipts": {}, "revision": 0}
                self.store.mutate(self.run_id, initialize, durable=True)
                if self.on_progress is not None:
                    await self.on_progress(self.progress())
                return ToolResult(output=json.dumps(self.progress(), ensure_ascii=False))
            if queue is None:
                raise ValueError("先以任务指定的完整清单 initialize；既有聊天不自动计为完成")
            self._valid(queue)
            self._refresh_evidence(queue)
            queue = self._queue()
            if self._upgrade_evidence_metadata(queue):
                queue = self._queue()
            if params.action == "evidence":
                if params.item_id not in {item["item_id"] for item in queue["items"]}:
                    raise ValueError("item_id 不在冻结队列内")
                path = self._path(params.source_path)
                text = evidence_content(path, items_path=params.source_items_path, selector=params.source_selector)
                if len(text) > 16000:
                    raise ValueError("证据超过单项16000字符，请按页 source_selector 定位或由准备节点提供逐页文本；未登记回执")
                receipt = typed_text_receipt(
                    path,
                    relative_path=str(path.relative_to(self.root)),
                    items_path=params.source_items_path,
                    selector=params.source_selector,
                    text=text,
                )
                reference = 'text:' + content_digest(json.dumps([params.item_id, receipt], sort_keys=True))[:24]
                receipt['evidence_id'] = reference
                def save_receipt(value):
                    receipts = self._queue(value).setdefault("text_receipts", {}).setdefault(params.item_id, {})
                    receipts[reference] = receipt
                    for obsolete in list(receipts)[:-32]:
                        receipts.pop(obsolete)
                self.store.mutate(self.run_id, save_receipt, durable=True)
                return ToolResult(output=json.dumps({"evidence_id": reference, "item_id": params.item_id,
                    "source": receipt, "text": text}, ensure_ascii=False))
            if params.action == "record":
                if params.item_id not in {item["item_id"] for item in queue["items"]}:
                    raise ValueError("item_id 不在冻结队列内")
                if not params.summary.strip():
                    raise ValueError("必须给出实际裁决")
                if canonical_review_state(params.state) == 'pass' and params.issue_ids:
                    raise ValueError("未决问题必须先返修复核，不能标记 complete")
                evidence = [item.model_dump() for item in params.evidence]
                commands = []
                for item in evidence:
                    item["path"] = str(self._path(item["path"]).relative_to(self.root))
                    if self._digest(item["path"]) != item["sha256"] or not item["quote"].strip():
                        raise ValueError("文本证据指纹不匹配或引文为空")
                    item["digest_kind"] = RAW_FILE_DIGEST_KIND
                    source = evidence_content(self._path(item["path"]))
                    if " ".join(item["quote"].split()) not in " ".join(source.split()):
                        raise ValueError("引文未在指定文本来源中找到，请提供可定位的实际短引文")
                for reference in dict.fromkeys(params.evidence_ids):
                    if reference.startswith('command:'):
                        receipt = queue.get('command_receipts', {}).get(reference)
                        if not receipt:
                            raise ValueError('命令引用不存在或不属于当前宿主审阅台账')
                        if canonical_review_state(params.state) == 'pass' and receipt['exit_code'] != 0:
                            raise ValueError('失败命令不能作为 PASS 凭证')
                        commands.append(copy.deepcopy(receipt))
                        continue
                    if reference.startswith('pixel:'):
                        receipt = next((row for row in queue.get('image_receipts', {}).get(params.item_id, [])
                                        if row.get('evidence_id') == reference), None)
                        if not receipt or self._digest(receipt['path']) != receipt['sha256']:
                            raise ValueError('像素引用不存在、不属于本项或已经变化，请重新取证')
                        continue
                    receipt = queue.get("text_receipts", {}).get(params.item_id, {}).get(reference)
                    if not receipt or self._evidence_digest(receipt) != receipt["sha256"]:
                        raise ValueError("文本引用不存在、不属于本项或已经变化，请重新用 evidence 读取")
                    evidence.append(copy.deepcopy(receipt))
                images = []
                for receipt in queue.get("image_receipts", {}).get(params.item_id, []):
                    try:
                        if self._digest(receipt["path"]) == receipt["sha256"]:
                            images.append(copy.deepcopy(receipt))
                    except (OSError, ValueError):
                        pass
                required = set(queue.get('review_channels', ['text', 'pixel']))
                if params.state not in {'unknown', 'pending'}:
                    if 'pixel' in required and (not images or len({e['path'] for e in evidence}) < 2):
                        raise ValueError('需本项实际图像读取回执及两侧文本证据，机器意见不能代替')
                    if 'text' in required and not evidence:
                        raise ValueError('需本项实际文本证据，观察或旧聊天不能代替')
                    if 'command' in required and not commands:
                        raise ValueError('需引用实际 RunVerification 宿主命令回执，文件名或文字声明不能代替')
                for image in images:
                    if self._digest(image["path"]) != image["sha256"]:
                        raise ValueError("图像证据已变化，请重新读取")
                evidence_ids = list(dict.fromkeys([
                    *(item.get('evidence_id') or 'text:' + content_digest(json.dumps(item, sort_keys=True))[:24] for item in evidence),
                    *(item.get('evidence_id') or 'pixel:' + item['pixel_sha256'][:24] for item in images),
                    *(item['evidence_id'] for item in commands)]))
                record = {"state": canonical_review_state(params.state), "summary": params.summary, "evidence": evidence,
                          "images": images, "issue_ids": params.issue_ids, "review_round": self.review_round,
                          'evidence_ids': evidence_ids, 'commands': commands,
                          'channels': sorted({'text' for item in evidence} | {'pixel' for item in images} | {'command' for item in commands})}
                candidate = queue.get("repair_candidates", {}).get(params.item_id)
                if candidate:
                    if params.candidate_revision != candidate["candidate_revision"]:
                        raise ValueError("必须复核当前candidate_revision，不能以旧证据关闭缺陷")
                    rendered_images = await self._verify_candidate_for_record(candidate, next(
                        item for item in queue["items"] if item["item_id"] == params.item_id))
                    for kind in ("candidate", "diff", "evidence"):
                        if self._digest(candidate[kind]["path"]) != candidate[kind]["sha256"]:
                            raise ValueError("已提交候选产物变化，不能复用旧候选裁决")
                    for source in candidate.get("sources", []):
                        if self._digest(source["path"]) != source["sha256"]:
                            raise ValueError("候选提交后原始来源变化，不能复用旧候选裁决")
                    for source in candidate.get("source_evidence", []):
                        if not any(e["path"] == source["path"]
                                   and (e.get("selector") or {}) == (source.get("selector") or {})
                                   and (e.get("items_path") or "") == (source.get("items_path") or "")
                                   for e in evidence):
                            raise ValueError("必须保留冻结工作单的原始文本证据，候选不能替代双侧原文")
                    if str(self._path(candidate["candidate"]["path"]).relative_to(self.root)) not in {e["path"] for e in evidence}:
                        raise ValueError("必须实际读取候选文本并提交对应证据")
                    record["candidate_revision"] = params.candidate_revision
                    record["rendered_images"] = rendered_images
                def save(snapshot):
                    current = self._queue(snapshot)
                    if current.get("repair_candidates", {}).get(params.item_id) != candidate:
                        raise ValueError("复核期间候选已变化，请重新读取")
                    if candidate:
                        # Keep the checked bytes stable through the ledger write.
                        for kind in ("candidate", "diff", "evidence"):
                            artifact = candidate[kind]
                            if self._digest(artifact["path"]) != artifact["sha256"]:
                                raise ValueError(f"{kind} 文件在预检后变化，不能登记复核")
                        for source in candidate.get("sources", []):
                            if self._digest(source["path"]) != source["sha256"]:
                                raise ValueError("来源文件在预检后变化，不能登记复核")
                        for image in record.get("rendered_images", []):
                            if self._digest(image["path"]) != image["sha256"]:
                                raise ValueError("候选图片在预检后变化，不能登记复核")
                    old = current["records"].get(params.item_id)
                    if old == record:
                        return
                    if old:
                        current.setdefault("history", []).append({"item_id": params.item_id, "previous": old})
                    current["records"][params.item_id] = record
                    current["revision"] += 1
                self.store.mutate(self.run_id, save, durable=True)
                if self.on_progress is not None:
                    await self.on_progress(self.progress())
            summary = self.dispatch_state()
            if params.action == "next":
                pending = self._actionable(queue)
                summary["items"] = [{**item, "previous": queue["records"].get(item["item_id"]),
                                     "candidate": queue.get("repair_candidates", {}).get(item["item_id"])}
                                    for item in pending[:params.limit]]
            elif params.action == "read":
                selected = [item for item in queue["items"] if (not params.item_id or item["item_id"] == params.item_id)
                    and (not params.state_filter or canonical_review_state((queue['records'].get(item['item_id']) or {}).get('state', 'pending'))
                         == canonical_review_state(params.state_filter))]
                summary["items"] = [{**item, "record": queue["records"].get(item["item_id"])}
                                    for item in selected[params.offset:params.offset + params.limit]]
                summary["next_offset"] = min(params.offset + params.limit, len(selected))
            return ToolResult(output=json.dumps(summary, ensure_ascii=False))
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return ToolResult(output=str(exc), is_error=True)


class ReadReviewQueueParams(BaseModel):
    reviewer_node_id: str
    include_resolved: bool = Field(default=False, description="仅为汇总产物分页读取已完成项；不会授予修改审核记录的能力")
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=5, ge=1, le=10)


class ReadReviewQueue(Tool):
    name = "ReadReviewQueue"
    category = "read"
    is_system_tool = True
    params_model = ReadReviewQueueParams
    description = ("执行成员读取本循环审查者的未决工作单、稳定item_id、issue_ids及证据。"
                   "文本 sha256 是规范化审查摘要，file_sha256 是原始文件字节摘要。"
                   "不能修改审核台账或代审查者关闭缺陷。")

    def __init__(self, store, run_id, reviewers, contract_hash, root=None):
        self.store, self.run_id, self.reviewers, self.contract_hash = store, run_id, set(reviewers), contract_hash
        self.root = Path(root).resolve() if root else None

    def _record_for_worker(self, record):
        if not record:
            return None
        result = copy.deepcopy(record)
        for receipt in result.get("evidence", []):
            if receipt.get("typed_text"):
                receipt.setdefault("digest_kind", TEXT_DIGEST_KIND)
                if self.root is not None:
                    try:
                        path = (self.root / receipt["path"]).resolve(strict=True)
                        valid_path = path.is_relative_to(self.root) and path.is_file()
                        current_text_sha = content_digest(evidence_content(
                            path,
                            items_path=receipt.get("items_path", ""),
                            selector=receipt.get("selector"),
                        )) if valid_path else ""
                        if current_text_sha != receipt.get("sha256"):
                            result["evidence_stale"] = True
                            receipt["file_fingerprint_unavailable"] = True
                            receipt["file_fingerprint_reason"] = "text_evidence_changed"
                        elif not receipt.get("file_sha256"):
                            receipt["file_sha256"] = file_digest(path)
                            receipt["file_digest_kind"] = RAW_FILE_DIGEST_KIND
                    except (OSError, ValueError, KeyError, TypeError):
                        result["evidence_stale"] = True
                        receipt["file_fingerprint_unavailable"] = True
                        receipt["file_fingerprint_reason"] = "source_unavailable"
            else:
                receipt.setdefault("digest_kind", RAW_FILE_DIGEST_KIND)
        return result

    async def execute(self, params):
        if params.reviewer_node_id not in self.reviewers:
            return ToolResult(output="只能读取本循环已配置的审查队列", is_error=True)
        try:
            if hasattr(self.store, "load_review_queue"):
                queue = self.store.load_review_queue(self.run_id, params.reviewer_node_id)
            else:
                queue = ((self.store.load(self.run_id) or {}).get("review_queues") or {}).get(params.reviewer_node_id)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return ToolResult(output=f"审核队列分片不可读取，失败关闭：{exc}", is_error=True)
        if queue is None:
            return ToolResult(output=json.dumps({'items': [], 'queue_initialized': False,
                'input_kind': 'stage_handoff',
                'note': '未初始化逐项返修台账；按本节点当前合同与普通阶段交接路线推进。'
                        '空台账不表示没有独立复核，也不要求创建或猜造批次。'}, ensure_ascii=False))
        if not isinstance(queue, dict) or not queue.get('contract_hash'):
            return ToolResult(output='审阅台账无效，需核对宿主状态，不能当作空台账继续', is_error=True)
        if queue["contract_hash"] != self.contract_hash:
            return ToolResult(output="合同已变化，需先迁移检查点", is_error=True)
        unresolved = sum(record['state'] in {'needs_repair', 'unknown'} for record in queue['records'].values())
        rows = [{**item,
                 "record": self._record_for_worker(queue["records"].get(item["item_id"])),
                 "record_revision": item_record_revision(queue["records"].get(item["item_id"]))}
                for item in queue["items"]
                if params.include_resolved or queue["records"].get(item["item_id"], {}).get("state") in {"needs_repair", "unknown"}]
        selected = rows[params.offset:params.offset + params.limit]
        for row in selected:
            record = row.get("record") or {}
            candidate = queue.get("repair_candidates", {}).get(row["item_id"])
            if not candidate:
                row["next_action"] = ("complete" if canonical_review_state(record.get('state')) == 'pass' else
                                      "reconcile_sources" if record.get("evidence_stale") else "freeze")
                continue
            row["candidate_revision"] = candidate["candidate_revision"]
            row["batch_id"] = candidate["batch_id"]
            if canonical_review_state(record.get('state')) == 'pass':
                row["next_action"] = "complete"
            elif self.root is not None:
                reviewer = ReviewWorkQueue(self.store, self.run_id, params.reviewer_node_id, self.root, self.contract_hash)
                owner = queue.get("repair_batches", {}).get(candidate["batch_id"], {}).get("owner")
                try:
                    repair_basis(queue, row["item_id"], record, reviewer, owner)
                    row["next_action"] = "freeze_new_version"
                    row["instruction"] = "原始来源未变。freeze后使用新版本路径提交，再RequestVerification；不要覆盖旧路径或要求审查者给旧版本代签。"
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    if str(exc).startswith("本项已有候选等待独立复核"):
                        row["next_action"] = "request_verification"
                    else:
                        row.update(next_action="reconcile_sources", blocker=str(exc))
        return ToolResult(output=json.dumps({"summary": queue_progress(queue),
            "items": selected, "total_unresolved": unresolved,
            "repair_batches": [{"batch_id": b["batch_id"], "owner": b["owner"], "status": b["status"],
                                "item_ids": b["item_ids"]} for b in queue.get("repair_batches", {}).values()
                               if b["status"] != "released"],
            "next_offset": min(len(rows), params.offset + params.limit)}, ensure_ascii=False))
