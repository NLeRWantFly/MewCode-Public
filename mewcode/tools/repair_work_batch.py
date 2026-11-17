"""Immutable repair work orders and atomic host registration, never business writes."""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, Field

from mewcode.tools.base import Tool, ToolResult
from mewcode.tools.review_work_queue import ReadReviewQueue, ReviewWorkQueue
from mewcode.experts.repair_lifecycle import item_record_revision, repair_basis, reserved_path


def revision(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def record_revision(record):
    """Backward-compatible spelling for the shared item lock digest."""
    return item_record_revision(record)


class CandidateArtifact(BaseModel):
    path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class RepairCandidate(BaseModel):
    item_id: str
    issue_ids: list[str] = Field(max_length=30)
    candidate: CandidateArtifact
    diff: CandidateArtifact
    evidence: CandidateArtifact


class RepairWorkBatchParams(BaseModel):
    action: Literal["plan", "freeze", "read", "submit", "release"]
    reviewer_node_id: str
    expected_revision: int = Field(default=0, ge=0)
    item_revisions: dict[str, str] = Field(default_factory=dict,
        description="可选条目级版本锁；必须逐项使用 ReadReviewQueue 返回的 record_revision")
    item_ids: list[str] = Field(default_factory=list, max_length=10)
    batch_id: str = ""
    candidates: list[RepairCandidate] = Field(default_factory=list, max_length=10)


class RepairWorkBatch(Tool):
    name = "RepairWorkBatch"
    category = "read"  # Host bookkeeping only, like ReviewWorkQueue.
    is_system_tool = True
    params_model = RepairWorkBatchParams
    description = ("plan只读确定性生成当前全部待返修/未知项的≤10条目批次与版本锁，不修改队列；"
                   "freeze冻结明确item_ids和ReadReviewQueue返回的item_revisions的返修工作单；旧调用仍可用全局expected_revision；"
                   "read恢复原批次；submit整批校验来源和候选文件指纹后"
                   "登记待独立复核，不写业务文件、不授予写权限、不关闭缺陷。release仅释放未提交批次。"
                   "审查打回或派生候选过期时可再次freeze，宿主保留原始来源与前版回执；"
                   "新candidate/diff/evidence必须使用新的版本路径，不能覆盖已提交文件。"
                   "候选文件必须先用原授权工具创建；提交candidate/diff/evidence的原始字节SHA256。")

    def __init__(self, store, run_id, reviewers, contract_hash, root, owner):
        self.store, self.run_id = store, run_id
        self.reviewers, self.contract_hash = set(reviewers), contract_hash
        self.root, self.owner = root, owner

    async def execute(self, params):
        try:
            if params.reviewer_node_id not in self.reviewers:
                raise ValueError("只能访问本循环审查队列")
            reviewer = ReviewWorkQueue(self.store, self.run_id, params.reviewer_node_id,
                                       self.root, self.contract_hash)
            if params.action == "plan":
                # Planning is a genuine read: it neither calls mutate nor
                # reserves an item.  Freeze still performs the authoritative
                # version check for every planned batch.
                from mewcode.orchestration.stage5_workorders import plan_stage5_workorders
                queue = reviewer._queue()
                if not queue:
                    raise ValueError("尚无工作单")
                reviewer._valid(queue)
                plan = plan_stage5_workorders(queue, owner=self.owner,
                    contract_hash=self.contract_hash)
                return ToolResult(output=json.dumps(plan, ensure_ascii=False),
                                  is_error=plan["status"] != "ready")
            reader = ReadReviewQueue(self.store, self.run_id, self.reviewers,
                                     self.contract_hash, self.root)
            output = {}

            def apply(snapshot):
                # Validate before any mutation: store callbacks are not rollback transactions.
                queue = reviewer._queue(snapshot)
                if not queue:
                    raise ValueError("尚无工作单")
                reviewer._valid(queue)
                batches = queue.get("repair_batches", {})
                if params.action == "freeze":
                    ids = sorted(params.item_ids)
                    if not ids or len(ids) != len(set(ids)):
                        raise ValueError("必须声明非空且不重复的item_ids")
                    item_versions = params.item_revisions
                    if item_versions:
                        if set(item_versions) != set(ids) or any(
                                not isinstance(value, str) or len(value) != 64
                                or any(char not in "0123456789abcdef" for char in value)
                                for value in item_versions.values()):
                            raise ValueError("item_revisions必须逐项给出有效的record_revision")
                        for item_id in ids:
                            if record_revision(queue["records"].get(item_id)) != item_versions[item_id]:
                                raise ValueError("条目record_revision已变化；需重新读取该条目")
                        identity = {"owner": self.owner, "item_ids": ids,
                                    "contract": self.contract_hash,
                                    "item_revisions": {key: item_versions[key] for key in ids}}
                    else:
                        identity = {"owner": self.owner, "revision": params.expected_revision,
                                    "item_ids": ids, "contract": self.contract_hash}
                    matching = [b for b in batches.values()
                                if all(b.get(k) == v for k, v in identity.items())]
                    for existing in matching:
                        if existing["status"] != "released":
                            output.update(copy.deepcopy(existing))
                            return
                    batch_id = "repair-" + revision({**identity, "generation": len(matching)})[:24]
                    if not item_versions and queue["revision"] != params.expected_revision:
                        raise ValueError("队列revision已变化；重新读取后选择明确工作项")
                    active = {row["item_id"] for batch in batches.values()
                              if batch["status"] == "frozen" for row in batch["items"]}
                    items = {item["item_id"]: item for item in queue["items"]}
                    rows = []
                    for item_id in ids:
                        record = queue["records"].get(item_id)
                        if item_id in active:
                            raise ValueError("工作项已被活动批次占用")
                        if item_id not in items or not record or record["state"] not in {"needs_repair", "unknown"}:
                            raise ValueError("只能冻结待返修或缺证据项")
                        basis, predecessor = repair_basis(queue, item_id, record, reviewer, self.owner)
                        enriched = reader._record_for_worker(basis)
                        if enriched.get("evidence_stale"):
                            raise ValueError("来源证据已变化，需审查者刷新工作单")
                        sources = copy.deepcopy(basis.get("original_sources", []))
                        for receipt in enriched.get("evidence", []) + enriched.get("images", []):
                            if reviewer._evidence_digest(receipt) != receipt["sha256"]:
                                raise ValueError("来源指纹已变化")
                            path = str(reviewer._path(receipt["path"]).relative_to(reviewer.root))
                            raw_sha = reviewer._digest(path)
                            if receipt.get("file_sha256") and receipt["file_sha256"] != raw_sha:
                                raise ValueError("来源整文件字节指纹已变化")
                            source = {"path": path, "sha256": raw_sha}
                            if source not in sources:
                                sources.append(source)
                        rows.append({**copy.deepcopy(items[item_id]), "record": enriched,
                                     "record_revision": record_revision(record), "sources": sources,
                                     "predecessor_revision": predecessor})
                    batch = {**identity, "revision": queue["revision"],
                             "batch_id": batch_id, "status": "frozen", "items": rows,
                             "manifest_sha256": queue["manifest_sha256"]}
                    queue.setdefault("repair_batches", {})[batch_id] = batch
                    # Once activated, rounds alone cannot redispatch unchanged defects.
                    queue["repair_protocol_version"] = 2
                    output.update(copy.deepcopy(batch))
                    return
                batch = batches.get(params.batch_id)
                if not batch or batch["owner"] != self.owner:
                    raise ValueError("批次不存在或不属于当前执行节点")
                if params.action == "read":
                    output.update(copy.deepcopy(batch))
                    return
                if params.action == "release":
                    if batch["status"] == "submitted":
                        raise ValueError("已提交候选不能释放或撤销审查")
                    batch["status"] = "released"
                    output.update(copy.deepcopy(batch))
                    return
                payload = sorted((c.model_dump() for c in params.candidates), key=lambda c: c["item_id"])
                submission_id = revision(payload)
                if batch["status"] == "submitted":
                    if batch["submission_id"] != submission_id:
                        raise ValueError("同一批次不能覆盖已提交候选")
                    output.update(copy.deepcopy(batch))
                    return
                if batch["status"] != "frozen":
                    raise ValueError("批次已释放；重新读取队列并冻结")
                if batch["manifest_sha256"] != queue["manifest_sha256"]:
                    raise ValueError("冻结清单版本已变化")
                rows = {row["item_id"]: row for row in batch["items"]}
                if len(payload) != len(rows) or {c["item_id"] for c in payload} != set(rows):
                    raise ValueError("必须整批且每项恰好提交一次")
                protected = {reviewer._path(queue["manifest_path"])}
                for record in queue["records"].values():
                    protected.update(reserved_path(reviewer, e["path"]) for e in record.get("evidence", []) + record.get("images", []))
                occupied = {reserved_path(reviewer, c[k]["path"]) for b in batches.values()
                            for c in b.get("candidates", {}).values()
                            for k in ("candidate", "diff", "evidence")}
                staged = {}
                for candidate in payload:
                    row = rows[candidate["item_id"]]
                    if record_revision(queue["records"].get(candidate["item_id"])) != row["record_revision"]:
                        raise ValueError("工作项裁决已变化，不能提交旧工作单")
                    current_revision = queue.get("repair_candidates", {}).get(candidate["item_id"], {}).get("candidate_revision", "")
                    if current_revision != row.get("predecessor_revision", ""):
                        raise ValueError("前一候选版本已变化，不能并发覆盖")
                    if set(candidate["issue_ids"]) != set(row["record"].get("issue_ids", [])):
                        raise ValueError("issue_ids必须匹配冻结工作单")
                    for source in row["sources"]:
                        if reviewer._digest(source["path"]) != source["sha256"]:
                            raise ValueError("原始来源字节指纹已变化")
                    for key in ("candidate", "diff", "evidence"):
                        artifact = candidate[key]
                        path = reviewer._path(artifact["path"])
                        if path in protected or path in occupied:
                            raise ValueError("候选产物与来源或其他候选写入范围重叠")
                        if reviewer._digest(artifact["path"]) != artifact["sha256"] or path.stat().st_size == 0:
                            raise ValueError("候选产物指纹不符或为空")
                        occupied.add(path)
                        artifact["path"] = str(path.relative_to(reviewer.root))
                    staged[candidate["item_id"]] = {**candidate, "batch_id": params.batch_id,
                        "sources": copy.deepcopy(row["sources"]),
                        "source_evidence": copy.deepcopy(row["record"].get("evidence", [])),
                        "predecessor_revision": row.get("predecessor_revision", ""),
                        "candidate_revision": revision({"candidate": candidate,
                            "input_revision": row["record_revision"], "sources": row["sources"]}),
                        "status": "awaiting_review"}
                queue.setdefault("repair_candidates", {}).update(staged)
                batch.update(status="submitted", submission_id=submission_id, candidates=staged)
                output.update(copy.deepcopy(batch))

            self.store.mutate(self.run_id, apply, durable=True)
            return ToolResult(output=json.dumps(output, ensure_ascii=False))
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return ToolResult(output=str(exc), is_error=True)
