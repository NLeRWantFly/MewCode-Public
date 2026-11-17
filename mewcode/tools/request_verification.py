"""Yield an immutable repair batch to an existing independent review node."""
from __future__ import annotations

import copy
import json

from pydantic import BaseModel, ConfigDict, Field

from mewcode.experts.repair_lifecycle import is_superseded_candidate
from mewcode.tools.base import Tool, ToolResult
from mewcode.tools.repair_work_batch import revision
from mewcode.tools.review_work_queue import ReviewWorkQueue


class VerificationParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reviewer_node_id: str
    batch_ids: list[str] = Field(min_length=1, max_length=8)
    summary: str = Field(min_length=1, max_length=2000)
    remaining_work: list[str] = Field(default_factory=list, max_length=32)


class RequestVerification(Tool):
    name = "RequestVerification"
    category = "read"
    is_system_tool = True
    params_model = VerificationParams
    description = ("交接已用 RepairWorkBatch.submit 登记的候选批次给图中现有独立审查节点。"
        "不需要 Agent 工具、不创建子 Agent、不代表 PASS 或本阶段全部完成。"
        "提供明确 reviewer_node_id 和 batch_ids，剩余任务放 remaining_work；审查完成后由原循环继续。"
        "只接受当前节点拥有、尚待复核且文件指纹未变化的提交批次。")

    def __init__(self, *, store, run_id, owner, reviewers, contract_hash, root):
        self.store, self.run_id, self.owner = store, run_id, owner
        self.reviewers = set(reviewers)
        self.contract_hash, self.root = contract_hash, root
        self.value = None
        self._params = None

    def _validate(self, params, snapshot):
        if params.reviewer_node_id == self.owner or params.reviewer_node_id not in self.reviewers:
            raise ValueError("必须交给冻结图中已连接的独立审查节点；不能自行验收或新建路线")
        if len(set(params.batch_ids)) != len(params.batch_ids):
            raise ValueError("batch_ids 不能重复")
        reviewer = ReviewWorkQueue(self.store, self.run_id, params.reviewer_node_id, self.root, self.contract_hash)
        queue = reviewer._queue(snapshot)
        if not queue:
            raise ValueError("审阅台账尚未初始化")
        reviewer._valid(queue)
        candidates = {}
        for batch_id in params.batch_ids:
            batch = queue.get("repair_batches", {}).get(batch_id) or {}
            if batch.get("owner") != self.owner or batch.get("status") != "submitted":
                raise ValueError("只能请求复核本节点已提交的批次")
            for item_id, item in (batch.get("candidates") or {}).items():
                current = queue.get("repair_candidates", {}).get(item_id) or {}
                record = queue.get("records", {}).get(item_id) or {}
                if current.get("candidate_revision") != item.get("candidate_revision"):
                    if is_superseded_candidate(queue, item_id, item.get("candidate_revision"), self.owner):
                        # A mixed historical batch can still hand off its
                        # unchanged items. Replaced items use their new batch.
                        continue
                    raise ValueError("候选已更新；不能复核旧版本")
                if record.get("candidate_revision") == item.get("candidate_revision"):
                    continue
                for artifact in [item[k] for k in ("candidate", "diff", "evidence")] + item.get("sources", []):
                    if reviewer._digest(artifact["path"]) != artifact["sha256"]:
                        raise ValueError("候选或来源在提交后改变，需修正版本，不得交接陈旧证据")
                candidates[item_id] = item["candidate_revision"]
        if not candidates:
            raise ValueError("批次没有尚待独立复核的候选；读取台账后继续剩余任务")
        return {"version": 1, "owner": self.owner, "reviewer_node_id": params.reviewer_node_id,
                "contract_hash": self.contract_hash, "batch_ids": sorted(params.batch_ids),
                "candidate_revisions": candidates, "summary": params.summary,
                "remaining_work": params.remaining_work}

    async def execute(self, params):
        try:
            output = {}
            def apply(snapshot):
                receipt = self._validate(params, snapshot)
                receipt["request_id"] = revision(receipt)
                snapshot.setdefault("node_states", {}).setdefault(self.owner, {})["verification_request"] = receipt
                output.update(copy.deepcopy(receipt))
            self.store.mutate(self.run_id, apply, durable=True)
            self._params, self.value = params, output
            return ToolResult(json.dumps({"status": "review_pending", **output,
                "instruction": "本轮工具完成后交接独立复核；未授予 PASS，不再创建 Agent。"}, ensure_ascii=False))
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return ToolResult(str(exc), is_error=True)

    def revalidate(self):
        """Called at the turn boundary, after all declared tool calls finished."""
        if self._params is None:
            return None
        current = self._validate(self._params, self.store.load(self.run_id) or {})
        if revision(current) != self.value["request_id"]:
            raise ValueError("复核交接版本在本轮工具执行期间发生变化")
        return copy.deepcopy(self.value)
