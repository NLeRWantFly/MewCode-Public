"""Durable task identity around the existing, single workflow material ledger.

The host owns intent and transitions. Model proposals never manufacture execution
authority, and a queued build is bound to the exact conversation/material revision.
"""
from __future__ import annotations

import copy
import json
import uuid
from contextlib import contextmanager

from .draft_sources import digest
from .draft_store import DraftStore, now

MARKER = "[MEWCODE_TASK_DESIGN]"
ACTIVE = {"designing", "queued", "building", "needs_input", "ready", "running", "blocked", "failed"}
TASK_BUILD_TRANSPORT_FAILURE_LIMIT = 3


class TaskBuildError(ValueError):
    """Host build failure classification, kept separate from model text."""

    def __init__(self, message, *, retryable=False, diagnostic=None):
        super().__init__(message)
        self.retryable = retryable is True
        self.diagnostic = copy.deepcopy(diagnostic) if isinstance(diagnostic, dict) else {}


def can_retry_task_build(request):
    count = request.get("transport_failure_count")
    return (request.get("failure_origin") == "host_build"
            and request.get("failure_kind") == "transport"
            and request.get("retryable") is True
            and type(count) is int and 0 < count < TASK_BUILD_TRANSPORT_FAILURE_LIMIT)


def _revalidate_structural_stop(state, request, base_graph_hash, conversation_id):
    """Reopen only a host-bound structural failure whose blockers disappeared."""
    candidate = state.get("candidate") or {}
    build = candidate.get("autobuild") or {}
    evidence = request.get("failure_evidence") or {}
    old_check = candidate.get("readiness") or {}
    if (request.get("failure_origin") != "host_build" or evidence.get("reason") != "model_stopped"
            or evidence.get("candidate_id") != candidate.get("id") or not candidate.get("id")
            or candidate.get("status") != "ready" or build.get("phase") != "done"
            or build.get("reason") != "model_stopped" or build.get("status") == "ready"
            or old_check.get("ready") is not False
            or not any(d.get("severity") != "warning" for d in old_check.get("diagnostics", []))
            or candidate.get("base_graph_hash") != base_graph_hash
            or digest(candidate.get("base_snapshot")) != base_graph_hash):
        return None
    from .graph_readiness import readiness
    from .graph_autobuild import restart_build_state
    check = readiness(candidate["document"], candidate.get("base_snapshot"),
                      conversation_id=conversation_id, revision=state["revision"] + 1)
    if not check["ready"]:
        return None
    receipt = {"candidate_id": candidate["id"], "candidate_sequence": candidate["sequence"],
               "previous_diagnostics": copy.deepcopy(old_check["diagnostics"]), "readiness": copy.deepcopy(check)}
    # Consume this recovery in the same transaction as queueing. Repeated
    # failures cannot keep using the obsolete readiness verdict to requeue.
    candidate.update(readiness={**check, "at": now()}, sequence=candidate["sequence"] + 1,
                     autobuild=restart_build_state(candidate["document"], build))
    return receipt


def _revalidate_review_capacity_stop(state, request, base_graph_hash, conversation_id, validator=None):
    from .graph_readiness import readiness

    candidate = state.get("candidate") or {}
    build = candidate.get("autobuild") or {}
    evidence = request.get("failure_evidence") or {}
    failure = evidence.get("failure") or {}
    phase = failure.get("phase")
    actual_failure = build.get("failure") or {}
    if (not callable(validator) or request.get("state") != "failed"
            or request.get("failure_origin") != "host_build"
            or evidence.get("reason") != "review_error"
            or failure.get("stage") != "request_preparation"
            or failure.get("category") != "capacity"
            or failure.get("exception_type") != "ReviewRequestTooLarge"
            or phase not in {"review", "final_review"}
            or any(actual_failure.get(key) != failure.get(key) for key in
                ("phase", "stage", "category", "exception_type"))
            or not candidate.get("id") or evidence.get("candidate_id") != candidate["id"]
            or candidate.get("status") != "ready" or build.get("phase") != "done"
            or build.get("status") != "needs_review" or build.get("reason") != "review_error"
            or candidate.get("base_graph_hash") != base_graph_hash
            or digest(candidate.get("base_snapshot")) != base_graph_hash
            or request.get("sequence") != (state.get("materials") or {}).get("sequence")
            or request.get("capacity_revalidation")
            or any(item.get("capacity_revalidation") for item in build.get("capacity_recovery_history", []))):
        return None
    check = readiness(candidate["document"], candidate.get("base_snapshot"),
        conversation_id=conversation_id, revision=state["revision"] + 1)
    if check.get("ready") is not True or check.get("skipped"):
        return None
    preflight = validator(state)
    task = state.get("task_design") or {}
    identity = {"task_id": task.get("task_id"), "task_revision": task.get("revision"),
        "material_sequence": (state.get("materials") or {}).get("sequence"),
        "candidate_id": candidate["id"], "candidate_sequence": candidate["sequence"],
        "candidate_document_hash": digest(candidate["document"]),
        "history_basis_hash": digest(candidate.get("history_basis"))}
    if (not isinstance(preflight, dict) or preflight.get("identity") != identity
            or preflight.get("ready") is not True or preflight.get("model_calls") != 0
            or preflight.get("max_chars") != 250000
            or any(preflight.get(key) is not True for key in
                ("history_scan_complete", "lossless_roundtrip", "all_requirement_ranges_covered"))
            or any(type(preflight.get(key)) is not int or preflight[key] <= 0 for key in
                ("requirement_count", "batch_count", "min_request_chars", "max_request_chars"))
            or not preflight["min_request_chars"] <= preflight["max_request_chars"] <= 250000
            or preflight.get("frozen_source_ids_hash") != digest(candidate.get("history_source_ids"))):
        return None
    receipt = {"candidate_id": candidate["id"], "candidate_sequence": candidate["sequence"],
        "document_hash": identity["candidate_document_hash"], "preflight": copy.deepcopy(preflight),
        "readiness": copy.deepcopy(check), "failed_build_id": request.get("id"),
        "scope": "One verified capacity repair; independent coverage review remains required."}
    restored = copy.deepcopy(build)
    restored.setdefault("capacity_recovery_history", []).append({
        "failure": copy.deepcopy(actual_failure), "review": copy.deepcopy(build.get(phase)),
        "capacity_revalidation": copy.deepcopy(receipt)})
    prior = restored.get(phase)
    if prior is not None:
        restored["retired_review_calls"] = restored.get("retired_review_calls", 0) + prior.get("model_calls", 0)
    restored[phase] = None
    restored.update(phase=phase, status="working", reason="", review_stale=True)
    restored.pop("failure", None)
    candidate.update(autobuild=restored, sequence=candidate["sequence"] + 1)
    return receipt


def host_task_control(text, *, has_attachments=False):
    """Recognize a whole user control message; never derive new authority."""
    if has_attachments or not isinstance(text, str):
        return ""
    return "continue" if text.strip().casefold() in {
        "继续", "继续任务", "继续设计", "继续构图", "continue", "resume",
    } else ""


def execution_scope_error(design):
    """Only a recorded host selection can authorize the execution handoff."""
    if design.get("intent") != "execute":
        return ""
    authority = design.get("intent_authority") or {}
    if authority.get("source") != "host_entry" or authority.get("intent") != "execute":
        return "此任务缺少受信用户入口的有效执行选择，已停止自动执行；请在任务卡重新确认「设计并执行」，或选择「仅设计保存」。"
    if authority.get("revoked"):
        return authority.get("reason") or "用户要求与执行范围不一致，已停止自动执行；请在任务卡修正为「仅设计保存」。"
    return ""


def _restricts_execution(text):
    # Only direct scope statements in a real user turn can restrict execution.
    # Diagnostic prose ("只证明设计已经保存"), quotations and tool text are not
    # control statements. This recognizer never creates execution authority.
    import re
    prefix = r"(?:(?:本次|这次|此次|现在|先|请|我要求)\s*)*"
    restrict = (r"(?:只|仅)(?:需要|要)?(?:设计|构图|绘制|生成|保存)(?:并?保存)?"
                r"(?:设计|编排图|流程图|编排|方案)?")
    stop = r"(?:不要|不得|不用|不需要|禁止|不)(?:实际|自动|开始|进行|去)?(?:执行|运行|启动)(?:业务|任务|编排)?"
    for clause in re.split(r'[，,。；;\n]', text):
        if re.fullmatch(prefix + '(?:' + restrict + '|' + stop + r')[！!。\s]*', clause.strip()):
            return True
    return False

GUIDANCE = """
你正在和用户一起完成同一个任务，持续维护一份任务规格，而不是把每段回复转换成一批节点。
先用 ReadWorkflowBlocks 读取当前规格、完整来源索引、实际模型和工具；必要时按 source_ids 读原文。
设计初期按“构建节点 → 探索 → 收敛验证”推进：先用 SubmitWorkflowBlocks 保存已明确的节点、
输入输出和依赖草稿，再按未知点调查文件和环境、比较可行方法，及时回填。完整用例或回归测试
不是建立节点的前置条件。用户指定节点 ID 时用 entry_ids 精确读取，修改时复用该 ID 及 work_key，
不因改名、移动或调整属性重新编号；修改模型不代表确认整个任务。保留范围、交付物、验收条件与来源，明确替代旧方案。
编排设计沿用当前执行权限，可通过正常工具检查环境、解释器、OCR 和看图能力，并验证少量样本；
不要把构图等同于只读 Plan。实测仍遵守实际授权、网络、费用和沙箱约束。
把缺口分为：系统可调查的事实、上游能产生的产物、只有用户能决定的业务取舍。前两类自行解决
或安排生产节点及输入绑定，不能一概建立人工门禁。真实选型用 AskUserQuestion 给出具体选项；
数值、路径或无法枚举的信息才用输入。解释选项不等于选择，历史助手建议不等于用户授权。
探索只做消除当前未知点所需的最小探测或代表样本，记录输入、方法、结果及适用条件；已有证据
在条件未变时直接复用。设计初期不反复编写、运行用例或全量回归；方案与关键接口收敛后再安排
必要验证，仅在相关实现、输入、模型、环境或验收变化、出现新失败，或用户明确要求时补测受影响范围。
需要样本验证时按任务需要安排为显式节点，已有有效样本直接复用，不将额外模型验收作为构图前置条件。
未来阶段才能获得的输入在使用前收集；不能用未产生的结果当启动条件。计数和范围必须自洽。
可调整实现方法和模型，必须保留用户目标、业务范围、验收和授权边界；能力不可用先调查适配，
无法自行修复再提交带选项的问题。禁止为了让图通过校验而删掉需求或弱化验收。
规格收敛且素材 pending 已清空后，调用 AdvanceTaskDesign(action="build")，使用读取返回的
task_design.revision 和 sequence；宿主在本轮结束后将已保存节点直接编译为新版工作流、保存，并按用户选定意图
启动已有执行入口。只生成图时不启动业务编排，设计验证仍遵守当前权限；设计并执行也不扩大
数据外发、费用或其他授权。
宿主校验修复后，旧 model_stopped 结构失败可以重新提交构图；宿主仅在重新校验确认旧阻塞已消失时续接，仍有阻塞则拒绝同规格重试。不能为重试修改业务要求或授权。
结束回复应说明当前实际状态，不能把已排队、已保存或样本通过说成全量交付。
"""


def design_view(state):
    design = state.get("task_design")
    if not isinstance(design, dict):
        return None
    result = copy.deepcopy(design)
    materials = state.get("materials") or {}
    candidate = state.get("candidate") or {}
    from .workflow_blocks import _baseline
    document = _baseline(state, materials.get("base_spec") or candidate.get("base_snapshot"))
    rows = [e for e in document.get("entries", []) if e.get("state") != "retired"]
    result["specification"] = {
        "entries": copy.deepcopy(rows),
        "material_sequence": materials.get("sequence", 0),
        "candidate_id": candidate.get("id"), "candidate_sequence": candidate.get("sequence"),
        "requirements": [e["id"] for e in rows if e.get("kind") in {"goal", "constraint", "artifact"}],
        "decisions": [e["id"] for e in rows if e.get("kind") == "decision"],
        "questions": [e["id"] for e in rows if e.get("kind") == "question"],
        "node_count": sum(e.get("kind") == "node" for e in rows),
        "pending": copy.deepcopy(materials.get("pending", [])),
        "entry_field_sources": copy.deepcopy((candidate.get("history_basis") or
                                                state.get("history_basis") or {}).get("entry_field_sources", {})),
    }
    result["summary"] = result.get("reason") or result.get("goal") or "发送目标，与主模型一起完善任务。"
    # The native compiler consumes the authored plan directly. Historical
    # automatic-review diagnostics remain in the ledger, not as new gates.
    result['workflow_format'] = 'mewcode.workflow.v2'
    return result


class TaskDesign:
    def __init__(self, project, conversation_id):
        self.store = DraftStore(project, conversation_id)

    def read(self):
        return design_view(self.store.get())

    def _update(self, change, *, task_id=None, revision=None):
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self.store._get(db)
            old = state.get("task_design")
            if task_id is not None and (not old or old["task_id"] != task_id or old["revision"] != revision):
                raise ValueError("任务规格已变化，请重新读取；未采用旧决定。")
            value = change(copy.deepcopy(old), state)
            if value is not None and value != old:
                value["revision"] = (old or {}).get("revision", 0) + 1
                value["updated_at"] = now()
                state["task_design"] = value
                self.store._put(db, state)
            return design_view(state)

    def start(self, intent, *, task_id=None, revision=None, orchestration_mode=None):
        from mewcode.kernel.collaboration import mode
        selected_mode = mode(orchestration_mode)
        if intent not in {"plan", "execute"}:
            raise ValueError("请选择只生成编排图或设计并执行。")
        def change(old, state):
            if old:
                if orchestration_mode is not None and selected_mode != old.get("orchestration_mode", "classic"):
                    raise ValueError("此任务已固定编排模式；请在新对话中选择另一种模式。")
                if intent == "plan" and old.get("intent") == "execute":
                    if task_id is None:
                        raise ValueError("已有任务需使用当前任务版本修正为仅设计保存，请刷新任务卡后重试。")
                    return self._set_plan(old)
                return old  # Reopening never upgrades the selected intent.
            return {"task_id": uuid.uuid4().hex, "intent": intent, "orchestration_mode": selected_mode,
                    "goal": "", "status": "designing",
                    "reason": "", "source_boundary": None, "created_at": now(), "build_attempts": 0,
                    "intent_authority": {"source": "host_entry", "intent": intent, "selected_at": now()}}
        return self._update(change, task_id=task_id, revision=revision)

    def restart(self, *, task_id, revision, run_id, request_id, spec_hash, document):
        """Prepare a separately bound build; keep the old task and its evidence.

        Only the explicit host restart endpoint calls this. It never modifies
        the source execution, grants, costs, acceptance or business files.
        The normal canvas commit and execution claim still own later steps.
        """
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
            raise ValueError('重新开始需要稳定的请求编号')
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            state = self.store._get(db)
            old = state.get('task_design') or {}
            restart = old.get('restart') or {}
            if restart.get('request_id') == request_id:
                if (restart.get('source_task_id'), restart.get('source_run_id'), restart.get('spec_hash')) != (
                        task_id, run_id, spec_hash):
                    raise ValueError('同一重新开始请求不能改写方案或来源')
                return design_view(state)
            if (old.get('task_id'), old.get('revision'), old.get('run_id')) != (task_id, revision, run_id):
                raise ValueError('原任务或版本已变化，未重新开始')
            if old.get('status') != 'paused' or not run_id:
                raise ValueError('请先暂停已有运行，再重新开始')
            archives = state.setdefault('task_restart_archives', {})
            if request_id in archives:
                raise ValueError('此重新开始请求已处理，未覆盖后续任务')
            archives[request_id] = {'task_design': copy.deepcopy(old),
                **{key: copy.deepcopy(state.get(key)) for key in ('materials', 'candidate', 'document')}}
            new = {key: copy.deepcopy(old[key]) for key in
                   ('intent', 'intent_authority', 'orchestration_mode', 'goal', 'source_boundary') if key in old}
            new.update(task_id=digest(['restart', task_id, run_id, request_id])[:32], revision=1,
                status='building', reason='正在保存重构后的编排；原运行及成果已保留。',
                created_at=now(), updated_at=now(), build_attempts=1,
                restart={'request_id': request_id, 'source_task_id': task_id,
                         'source_run_id': run_id, 'spec_hash': spec_hash},
                build_request={'id': uuid.uuid4().hex, 'epoch': 1, 'state': 'building', 'attempts': 1})
            state['task_design'] = new
            if document is not None:
                materials = copy.deepcopy(state.get('materials') or {})
                materials.update(document=copy.deepcopy(document), sequence=materials.get('sequence', 0) + 1,
                                 pending=[])
                state.update(materials=materials, document=copy.deepcopy(document), candidate=None)
            self.store._put(db, state)
            return design_view(state)

    @staticmethod
    def _fence_request(old, state):
        request = old.get("build_request")
        if request:
            request.setdefault("id", uuid.uuid4().hex)
            request["epoch"] = request.get("epoch", 0) + 1
            if state == "superseded" or request.get("state") in {None, "queued", "building", "paused"}:
                request["state"] = state

    @classmethod
    def _set_plan(cls, old):
        if old.get("status") == "running":
            raise ValueError("请先暂停当前执行，再将任务修正为仅设计保存。")
        old["intent"] = "plan"
        old["intent_authority"] = {"source": "host_entry", "intent": "plan", "selected_at": now()}
        cls._fence_request(old, "paused")
        if old.get("status") in {"queued", "building", "failed", "needs_input", "blocked"}:
            old["status"] = "paused"
            if (old.get("build_request") or {}).get("state") == "saved":
                old["status"] = "ready"
        old["reason"] = "任务范围已修正为仅设计保存，不会启动业务执行。"
        return old

    def set_intent(self, intent, *, task_id, revision):
        if intent != "plan":
            raise ValueError("此入口只支持将现有任务修正为仅设计保存，不能增加执行授权。")
        if not isinstance(task_id, str) or not task_id or type(revision) is not int:
            raise ValueError("缺少任务版本，请刷新后重试。")
        return self._update(lambda old, state: self._set_plan(old), task_id=task_id, revision=revision)

    def confirm_execution(self, *, task_id, revision):
        """A new, versioned host choice; ordinary start/resume never grant it."""
        if not isinstance(task_id, str) or not task_id or type(revision) is not int:
            raise ValueError("缺少任务版本，请刷新后重试。")
        def change(old, state):
            request = old.get("build_request") or {}
            if (old.get("run_id") or request.get("execution_claim_id") or request.get("execution_state")
                    or old.get("status") in {"running", "completed"}):
                raise ValueError("已有执行记录，请从当前运行的恢复入口继续；未重新启动任务。")
            if old.get("status") not in {"designing", "paused", "failed", "blocked", "needs_input", "ready"}:
                raise ValueError("构图仍在运行，请暂停后再确认执行。")
            if old.get("intent") == "execute" and not execution_scope_error(old):
                raise ValueError("当前执行意图已经有效，无需重复确认。")
            confirmation = {"source": "host_entry", "intent": "execute", "action": "confirm_execute",
                            "selected_at": now(), "confirmed_revision": revision,
                            "previous_intent": old.get("intent")}
            old.setdefault("intent_confirmations", []).append({**confirmation,
                "previous_authority": copy.deepcopy(old.get("intent_authority") or {})})
            old["intent_authority"] = confirmation
            old["intent"] = "execute"
            # Keep materials/candidates and diagnostics, but require a fresh build
            # under the new choice. Old callbacks and saved-plan claims are stale.
            self._fence_request(old, "superseded")
            old.update(status="designing", reason="已确认设计并执行，将沿用现有规格继续设计与校验。")
            return old
        return self._update(change, task_id=task_id, revision=revision)

    def record_turn(self, text, *, control=""):
        def change(old, state):
            if not old or old.get("status") == "running" or old.get("status") == "completed":
                return old
            if old.get("orchestration_mode") == "collaborative" and old.get("run_id") and control != "continue":
                old["reason"] = "此任务的运行规格已固定；请在任务详情修订具体要求，再继续任务。"
                return old
            if control == "continue":
                if old.get("status") == "paused":
                    return self._resume(old)
                return old  # Controls cannot reset an in-flight queue or clear its failure evidence.
            if control:
                raise ValueError("未知宿主任务控制")
            self._fence_request(old, "superseded")
            old.update(goal=old.get("goal") or text[:8000], status="designing", reason="", latest_request=text[:8000])
            if old.get("intent") == "execute" and _restricts_execution(text):
                authority = old.setdefault("intent_authority", {})
                authority.update(revoked=True, reason="用户目标限定为设计、保存或不执行，现有执行意图已失效；可选择「仅设计保存」，如需执行请在任务卡重新确认「设计并执行」。")
                old["reason"] = authority["reason"]
            return old
        # No design is a read-only no-op; do not create an accidental task.
        if not self.read():
            return None
        return self._update(change)

    @staticmethod
    def _resume(old):
        request = old.get("build_request") or {}
        if old.get("intent") == "execute" and request.get("execution_state") == "dispatching" and not old.get("run_id"):
            raise ValueError("执行启动结果尚未确认，请先核对现有运行记录；未重复启动业务执行。")
        old["resume_epoch"] = old.get("resume_epoch", 0) + 1
        resumable = request and request.get("state") in {"queued", "building", "paused"}
        saved = request.get("state") == "saved"
        failed = request.get("state") == "failed"
        old.update(status="queued" if resumable else "ready" if saved else "failed" if failed else "designing",
                   reason="已续接原构图请求，等待构图。" if resumable else
                          "编排图已保存。" if saved else request.get("error", old.get("reason", "")) if failed else "")
        if resumable:
            request.setdefault("id", uuid.uuid4().hex)
            request["state"] = "queued"
            request["epoch"] = request.get("epoch", 0) + 1
        return old

    def control(self, action, *, task_id, revision, reason=""):
        if action not in {"pause", "resume"}:
            raise ValueError("未知任务操作")
        if not isinstance(task_id, str) or not task_id or type(revision) is not int:
            raise ValueError("缺少任务版本，请刷新后重试。")
        def change(old, state):
            if not old:
                raise ValueError("当前没有任务，请先发起任务对话。")
            if old.get("status") == "completed":
                raise ValueError("此任务已完成；新的目标请在新对话中发起。")
            if action == "resume" and old.get("status") not in {"paused", "failed", "blocked", "needs_input"}:
                raise ValueError("仅暂停或待修正的任务可以续接。")
            if old.get("status") == "running":
                raise ValueError("请使用执行区的暂停或恢复按钮控制当前运行。")
            if action == "resume":
                return self._resume(old)
            self._fence_request(old, "paused")
            old.update(status="paused", reason=reason)
            return old
        return self._update(change, task_id=task_id, revision=revision)

    def queue_build(self, *, task_id, revision, sequence, source_hash, base_graph_hash, user_source_hash="",
                    requirement_fingerprint="", basis_validator=None, capacity_validator=None, compiler=""):
        def change(old, state):
            if old.get("status") not in {"designing", "needs_input", "ready", "failed", "blocked"} or not old.get("goal"):
                raise ValueError("请先在任务对话中明确目标并完善规格。")
            if basis_validator:
                basis_validator()
            materials = state.get("materials") or {}
            if materials.get("sequence", 0) != sequence or materials.get("pending"):
                raise ValueError("素材版本已变化或仍有待修正字段，请先修正后重新读取。")
            rows = (materials.get("document") or state.get("document") or {}).get("entries", [])
            if not any(e.get("kind") == "node" and e.get("state") != "retired" for e in rows):
                raise ValueError("尚无任务步骤，请先提交规格素材。")
            basis = {"sequence": sequence, "base_graph_hash": base_graph_hash,
                     "user_source_hash": user_source_hash, "requirement_fingerprint": requirement_fingerprint}
            previous = old.get("build_request") or {}
            same_basis = all(previous.get(k, "") == v for k, v in basis.items())
            transport_count = previous.get("transport_failure_count")
            if (compiler != 'workflow_core' and same_basis and previous.get("failure_origin") == "host_build"
                    and previous.get("failure_kind") == "transport" and previous.get("retryable") is True
                    and type(transport_count) is int and transport_count >= TASK_BUILD_TRANSPORT_FAILURE_LIMIT):
                raise ValueError("同一规格的构图已达到瞬时连接失败上限，请核对诊断或修正规格后继续。")
            revalidation = None
            capacity_revalidation = None
            if compiler != 'workflow_core' and previous.get("state") == "failed" and same_basis and not can_retry_task_build(previous):
                revalidation = _revalidate_structural_stop(state, previous, base_graph_hash, self.store.scope[0])
                if not revalidation:
                    capacity_revalidation = _revalidate_review_capacity_stop(
                        state, previous, base_graph_hash, self.store.scope[0], capacity_validator)
                if not revalidation and not capacity_revalidation:
                    raise ValueError("同一规格的构图已失败：" + (previous.get("error") or old.get("reason") or "请先修正已报告问题。"))
            old.update(status="queued", reason="规格已提交，等待本轮结束后构图。", build_request={
                "id": uuid.uuid4().hex, "epoch": 0, "state": "queued", "attempts": 0, "compiler": compiler,
                "previous_failure": {"fingerprint": previous.get("failure_fingerprint"),
                                     "basis": {k: previous.get(k, "") for k in basis},
                                     "repair_attempts": previous.get("repair_attempts", 0),
                                     "failure_origin": previous.get("failure_origin"),
                                     "failure_kind": previous.get("failure_kind"), "retryable": previous.get("retryable"),
                                     "transport_failure_count": previous.get("transport_failure_count", 0),
                                     "evidence": copy.deepcopy(previous.get("failure_evidence"))},
                "sequence": sequence, "source_hash": source_hash, "base_graph_hash": base_graph_hash,
                "user_source_hash": user_source_hash, "requirement_fingerprint": requirement_fingerprint})
            if compiler != 'workflow_core':
                from .design_health import design_health
                old["build_request"]["diagnosis_identity"] = design_health(state)["identity"]
            if revalidation:
                old["build_request"]["structural_revalidation"] = revalidation
            if capacity_revalidation:
                old["build_request"]["capacity_revalidation"] = capacity_revalidation
            return old
        return self._update(change, task_id=task_id, revision=revision)

    def claim_build(self, expected):
        def change(old, state):
            request = old.get("build_request") or {}
            if old.get("status") != "queued" or request.get("state", "queued") != "queued":
                raise ValueError("构图请求已由其他回调处理或已停止。")
            request.setdefault("id", uuid.uuid4().hex)
            request.update(state="building", epoch=request.get("epoch", 0) + 1,
                           attempts=request.get("attempts", 0) + 1)
            old.update(status="building", reason="正在保存节点与连接。",
                       build_attempts=old.get("build_attempts", 0) + 1)
            return old
        return self._update(change, task_id=expected["task_id"], revision=expected["revision"])

    def check_build(self, expected):
        value = self.read() or {}
        self._assert_build(value, expected)
        return value

    @staticmethod
    def _assert_build(value, expected):
        request = value.get("build_request") or {}
        if ((value.get("task_id"), value.get("revision")) != (expected.get("task_id"), expected.get("task_revision"))
                or value.get("status") != "building"
                or (request.get("id"), request.get("epoch"), request.get("state")) !=
                   (expected.get("build_id"), expected.get("build_epoch"), "building")):
            raise ValueError("任务已暂停或被新请求替代，未采用旧构图回调。")

    @contextmanager
    def commit_build(self, expected):
        """Fence the existing canvas writer and its receipt with the task lock."""
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self.store._get(db)
            old = state.get("task_design") or {}
            self._assert_build(old, expected)
            receipt = {}
            yield receipt
            if not receipt.get("spec"):
                raise ValueError("保存没有返回编排记录，未完成构图请求。")
            spec = receipt["spec"]
            if spec.get('meta', {}).get('workflow_core'):
                from mewcode.workflow_core.planning import sync_materials
                sync_materials(state, spec)
            version = (spec.get("meta") or {}).get("pipeline_graph_version")
            old["build_request"].update(state="saved", graph_version=version, saved_spec_hash=digest(spec), saved_at=now())
            old.update(status="ready", reason="编排图已保存。", graph_version=version,
                       revision=old["revision"] + 1, updated_at=now())
            self.store._put(db, state)

    def saved_build(self, expected):
        value = self.read() or {}
        request = value.get("build_request") or {}
        if ((value.get("task_id"), request.get("id"), request.get("epoch"), request.get("state")) !=
                (expected.get("task_id"), expected.get("build_id"), expected.get("build_epoch"), "saved")
                or value.get("status") != "ready"):
            raise ValueError("任务保存结果已被新控制或请求替代，未采用旧结果。")
        return value

    def claim_execution(self, expected):
        def change(old, state):
            error = execution_scope_error(old)
            if error:
                raise ValueError(error)
            request = old.get("build_request") or {}
            if old.get("status") != "ready" or request.get("state") != "saved" or request.get("execution_state"):
                raise ValueError("当前保存结果已处理，未重复启动业务执行。")
            recovery = request.get('execution_not_started') or {}
            request["execution_state"] = "dispatching"
            request["execution_claim_id"] = (recovery['claim_id']
                if old.get('orchestration_mode') == 'collaborative'
                and recovery.get('restart_recovery') is True and recovery.get('claim_id')
                else uuid.uuid4().hex)
            request.pop("execution_not_started", None)
            old["reason"] = "编排图已保存，正在按受信用户入口的执行范围启动。"
            return old
        return self._update(change, task_id=expected["task_id"], revision=expected["revision"])

    def reject_execution(self, expected, reason):
        """Release a dispatch claim only after the host explicitly says not started."""
        def change(old, state):
            request = old.get("build_request") or {}
            if request.get("execution_state") != "dispatching":
                raise ValueError("执行启动记录已变化，未覆盖新结果。")
            request.pop("execution_state", None)
            request.pop("execution_claim_id", None)
            request["execution_error"] = reason
            old.update(status="blocked", reason=reason)
            return old
        return self._update(change, task_id=expected["task_id"], revision=expected["revision"])

    @staticmethod
    def execution_run_id(value):
        request = value.get("build_request") or {}
        if not request.get("execution_claim_id"):
            return ""
        return digest([value["task_id"], request["execution_claim_id"]])[:32]

    def bind_run(self, *, task_id, claim_id, run_id):
        """Recoverable binding before business dispatch, including after a crash."""
        def change(old, state):
            request = old.get("build_request") or {}
            if (old.get("task_id") != task_id or request.get("execution_claim_id") != claim_id
                    or self.execution_run_id(old) != run_id):
                raise ValueError("启动身份与任务绑定不一致")
            if old.get("run_id") and old["run_id"] != run_id:
                raise ValueError("此任务已经绑定另一运行")
            old["run_id"] = run_id
            request["execution_state"] = "started"
            return old
        return self._update(change)

    @staticmethod
    def execution_token(value):
        request = value.get("build_request") or {}
        return {"task_id": value["task_id"], "revision": value["revision"], "intent": value["intent"],
                "build_id": request.get("id"), "build_epoch": request.get("epoch"),
                "claim_id": request.get("execution_claim_id"), "run_id": value.get("run_id") or ""}

    def record_execution_not_started(self, expected, reason, *, restart_recovery=False):
        """Record a known pre-worker rejection without replacing user control.

        A pause can change revision/epoch while the socket send is pending.
        Only the unique claim is released; this receipt grants no execution
        authority and leaves the current control revision available for resume.
        """
        if not expected.get("claim_id"):
            return False
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self.store._get(db)
            current = state.get("task_design") or {}
            request = current.get("build_request") or {}
            if ((current.get("task_id"), request.get("id"), request.get("execution_claim_id")) !=
                    (expected.get("task_id"), expected.get("build_id"), expected["claim_id"])
                    or current.get('run_id')
                    or request.get("execution_state") not in (
                        {'dispatching', 'started'} if restart_recovery else {'dispatching'})):
                return False
            request.pop("execution_state", None)
            request.pop("execution_claim_id", None)
            request["execution_not_started"] = {"claim_id": expected["claim_id"], "reason": reason}
            if restart_recovery:
                request['execution_not_started']['restart_recovery'] = True
            self.store._put(db, state)
            return True

    @contextmanager
    def dispatch_guard(self, expected, *, resume_run_id=None):
        """Authorize the last synchronous handoff, after all startup awaits."""
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self.store._get(db)
            current = state.get("task_design") or {}
            if (not current or self.execution_token(current) != {k: expected.get(k) for k in
                    ("task_id", "revision", "intent", "build_id", "build_epoch", "claim_id", "run_id")}
                    or current.get("status") != "ready" or current.get("intent") != "execute"):
                raise ValueError("任务已暂停、范围已修正或启动版本变化，未派发业务执行。")
            scope_error = execution_scope_error(current)
            if scope_error:
                raise ValueError(scope_error)
            request = current.get("build_request") or {}
            if not request and not current.get("run_id"):
                raise ValueError("任务没有已保存构图或已绑定检查点，未派发业务执行。")
            if request and (request.get("state") != "saved" or
                            (not current.get("run_id") and request.get("execution_state") != "dispatching")):
                raise ValueError("保存结果或执行启动记录已变化，未派发业务执行。")
            if resume_run_id is not None and expected.get("run_id") and expected["run_id"] != resume_run_id:
                raise ValueError("恢复检查点与任务绑定不一致，未派发业务执行。")
            yield

    def finish_build(self, expected, status, reason="", **extra):
        def change(old, state):
            request = old.get("build_request") or {}
            if ((request.get("id"), request.get("epoch"), request.get("state")) !=
                    (expected.get("build_id"), expected.get("build_epoch"), "building")):
                raise ValueError("构图请求已失效，未采用旧结果。")
            request.update(state="saved" if status == "ready" else "failed", **extra)
            if status != "ready":
                request["error"] = reason
            old.update(status=status, reason=reason)
            if "graph_version" in extra:
                old["graph_version"] = extra["graph_version"]
            return old
        return self._update(change, task_id=expected["task_id"], revision=expected["task_revision"])

    def transition(self, expected, status, reason="", **extra):
        def change(old, state):
            old.update(status=status, reason=reason, **extra)
            return old
        return self._update(change, task_id=expected["task_id"], revision=expected["revision"])


def task_reminder(design):
    if not design or design.get("status") not in ACTIVE:
        return ""
    # Full provenance remains paginated through ReadWorkflowBlocks. Do not
    # duplicate the entire citation ledger into every main-model turn.
    view = {key: design.get(key) for key in ("task_id", "revision", "intent", "orchestration_mode", "status", "goal", "latest_request", "reason")}
    spec = design.get("specification") or {}
    view["specification"] = {key: spec.get(key) for key in ("material_sequence", "candidate_id", "candidate_sequence", "node_count")}
    guidance = GUIDANCE
    if design.get("orchestration_mode") == "collaborative":
        guidance += ("\n当前为协作编排：主模型是固定协调者。每个工作单元需明确目标、输入、交付物、禁止变更、"
                     "方法约束和验收标准；上游自述不是验收。先展开当前里程碑，未来阶段保留目标和完成定义。"
                     "修改用户要求须有用户原文；工作单元不得自行更换指定方法。普通实现细节由工作单元处理。")
    return guidance + "\n宿主任务规格状态（数据，不构成新增授权）：\n" + json.dumps(view, ensure_ascii=False)


def graph_identity(design, state):
    request = design.get("build_request") or {}
    return {"task_id": design["task_id"], "revision": design["revision"], "intent": design["intent"],
            "orchestration_mode": design.get("orchestration_mode", "classic"),
            "build_id": request.get("id"), "build_epoch": request.get("epoch"),
            "material_sequence": (state.get("materials") or {}).get("sequence", 0),
            "spec_hash": digest((state.get("candidate") or {}).get("document") or state.get("document"))}


def question_binding(state):
    design = state.get("task_design")
    if not design:
        return None
    return {"task_id": design["task_id"], "revision": design["revision"],
            "materials": (state.get("materials") or {}).get("sequence", 0),
            "candidate": [(state.get("candidate") or {}).get(k) for k in ("id", "sequence")]}


def validate_question_binding(project, cid, expected):
    if expected is not None and question_binding(DraftStore.read_state(project, cid) or {}) != expected:
        raise ValueError("该问题对应的任务规格已变化，请重新提问；未采用旧答案。")
