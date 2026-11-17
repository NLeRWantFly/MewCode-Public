"""Chat intent and durable focus are separate from an orchestration UI setting."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from mewcode.orchestration.revisions import replace_plan_reminder

CHAT_TASK_MARKER = "[MEWCODE_CURRENT_CHAT_TASK]"


def dialogue_focus(runtime) -> dict:
    meta = getattr(getattr(runtime, "session", None), "meta", None)
    value = getattr(meta, "dialogue_focus", {})
    return value if isinstance(value, dict) else {}


def set_dialogue_focus(runtime, kind: str, *, task: str = "", run_id: str = "",
                       turn_id: str = "", latest_request: str = "", task_id: str | None = None) -> None:
    meta = getattr(getattr(runtime, "session", None), "meta", None)
    if meta is None:
        return
    if task_id is not None:
        from mewcode.sandbox.task_identity import require_task_identity
        task_id = require_task_identity(task_id)
    previous_focus = dialogue_focus(runtime)
    same_task = previous_focus.get("kind") == kind and (
        (kind == "chat" and previous_focus.get("task") == str(task)[:8000])
        or (kind == "orchestration" and bool(run_id) and previous_focus.get("run_id") == run_id)
    )
    meta.dialogue_focus = {"kind": kind, "task": str(task)[:8000], "run_id": run_id,
                           "task_id": task_id or (previous_focus.get("task_id") if same_task else "") or uuid.uuid4().hex,
                           "updated_at": datetime.now(timezone.utc).isoformat()}
    if (previous_focus.get("kind") == kind
            and meta.dialogue_focus["task_id"] == previous_focus.get("task_id")
            and previous_focus.get("workflow_run_ids")):
        meta.dialogue_focus["workflow_run_ids"] = list(previous_focus["workflow_run_ids"])
        if previous_focus.get('workflow_view_run_id'):
            meta.dialogue_focus['workflow_view_run_id'] = previous_focus['workflow_view_run_id']
    if turn_id:
        meta.dialogue_focus.update(turn_id=turn_id, latest_request=latest_request)
    save = getattr(runtime.session, "save_meta", None)
    if callable(save):
        try:
            save()
        except OSError:
            # An unpersisted task identity must not become executable state.
            meta.dialogue_focus = previous_focus
            raise


def bind_task_permission_scope(runtime, *, task_id: str):
    """Bind host execution identity; restore only its explicit saved scope."""
    agent = getattr(runtime, "agent", None)
    checker = getattr(agent, "permission_checker", None)
    if checker is None:
        return None
    from mewcode.sandbox.task_identity import require_task_identity
    require_task_identity(task_id)
    from mewcode.permissions.task_authorization import TaskAuthorization

    owner = getattr(runtime, "owner", runtime)
    state = getattr(owner, "web_state", {})
    records = state.get("task_authorizations") if isinstance(state, dict) else None
    stored = records.get(f"{runtime.conversation_id}:{task_id}") if isinstance(records, dict) else None
    try:
        authorization = TaskAuthorization.from_record(stored) if isinstance(stored, dict) else None
        if authorization is not None and authorization.task_id != task_id:
            authorization = None
    except (ValueError, OSError, TypeError):
        authorization = None
    checker.set_task_authorization(authorization, task_id=task_id)
    reviewer = getattr(agent, "safety_reviewer", None)
    if reviewer is not None:
        reviewer.authorization_version = authorization.version if authorization else ""
    return authorization


def is_bare_continuation(text: str) -> bool:
    return bool(re.fullmatch(r"(?:请\s*)?(?:继续|接着|恢复)(?:任务|执行)?[\s。.!！]*|(?:continue|resume)[\s.!]*",
                             str(text or "").strip(), re.IGNORECASE))


def requests_orchestration_resume(text: str, focus: dict) -> bool:
    """Do not interpret '继续安装…' as a workflow control command.

    A standalone continue belongs to the currently focused task, not whichever
    old run happens to remain resumable. Legacy sessions have unknown focus.
    """
    from mewcode.expert_runs import parse_resume_message
    value = str(text or "").strip()
    if re.search(r"[?？]|怎么|如何|为什么|能否|是否|按钮", value):
        return False
    # A constraint such as '不要让我接管' is not a negated resume request.
    # Explicitly negated execution still wins over a leading continuation.
    if re.search(r"(?:不要|别|不必|暂不|先不|不)(?:再|先|要)?\s*"
                 r"(?:继续|恢复|重试|启动|运行|执行)(?:当前|这个|该|任何|全部|所有|的)*"
                 r"(?:编排|流程|任务|节点|阶段|检查点|$)", value):
        return False
    continuation, _ = parse_resume_message(re.sub(r"^请\s*", "", value))
    explicit_flow = bool(re.match(
        r"^(?:请\s*)?(?:继续|恢复|接着|重试)(?:之前的|原来的|上次的|当前的|这个|这条|之前|原来|上次|当前)?"
        r"(?:编排图任务|编排任务|编排图|流程任务|编排|流程|阶段图|检查点|失败节点|受阻节点)"
        r"(?:执行|运行)?(?=$|[\s:：,，。.!！;；]|至(?:完整|全部|流程))", value))
    explicit_checkpoint = bool(re.match(
        r"^(?:请\s*)?(?:从|接着从).{0,40}(?:(?:失败|受阻)(?:的)?(?:节点|阶段|位置)|检查点).{0,20}(?:继续|恢复|重试)", value))
    explicit_english = bool(re.match(
        r"^(?:resume|continue)\s+(?:the\s+)?(?:orchestration|workflow)\s*(?=$|[:：,，。.!！;；])",
        value, re.IGNORECASE))
    if explicit_flow or explicit_checkpoint or explicit_english:
        return True
    if not continuation:
        return False
    if focus.get("kind") != "orchestration" or not focus.get("run_id"):
        return False
    return is_bare_continuation(value) or bool(re.match(
        r"^(?:请\s*)?(?:继续|恢复|接着)(?:任务|执行)?\s*[:：,，。.!！-]", value))


def validate_chat_continuation(runtime, task_id: str) -> None:
    """An explicit UI continuation selects identity, never supplies authority."""
    focus = dialogue_focus(runtime)
    if (not isinstance(task_id, str) or not task_id or focus.get("kind") != "chat"
            or focus.get("task_id") != task_id):
        raise ValueError("当前任务已变化或不是普通对话任务，请刷新后重新选择续接；未发送消息。")


def prepare_chat_focus(runtime, text: str, *, continuation_task_id: str | None = None,
                       latest_request: str | None = None) -> None:
    previous = dialogue_focus(runtime)
    if continuation_task_id is not None:
        validate_chat_continuation(runtime, continuation_task_id)
    task = previous.get("task", "") if (continuation_task_id is not None or
        previous.get("kind") == "chat" and is_bare_continuation(text)) else text
    current = text if latest_request is None else latest_request
    effective = ((previous.get("latest_request") or task)
                 if previous.get("kind") == "chat" and is_bare_continuation(current) else current)
    turn_id = uuid.uuid4().hex
    set_dialogue_focus(runtime, "chat", task=task, turn_id=turn_id, latest_request=effective)
    replace_plan_reminder(runtime.conversation, CHAT_TASK_MARKER,
        "本轮由主对话模型处理用户当前的对话任务。其他任务已保存的编排图仅是背景，"
        "不得将本轮工作塞入不相关的旧图、虚称已恢复编排，也不要因旧节点的工具限制拒绝当前任务；"
        "以本轮实际工具和权限为准。"
        "若出现 [MEWCODE_CANVAS_NODES] 清单，本对话拥有自己的编排画布："
        "report_step 照常把当前工作归属到清单中的节点，对不上时填 node=无。"
        "若用户只说继续，接续下面的当前对话任务；不要擅自恢复历史编排。"
        "新的无关要求独立处理；历史持续目标不能覆盖当前要求。"
        "续接只保留任务身份与已有授权，不表示重放旧请求。历史工具回执不是本轮调用；"
        "以 BackgroundTasks 的 dispatch_relation 和 AgentStatus 的 current_turn 判断派发轮次，"
        "不能因为历史清单中出现旧任务就声称本轮派发过它。\n"
        "当前执行轮次以 AgentStatus.current_turn 的实时宿主回执为准。\n"
        "任务归属（背景，不代替最新请求）：\n" + str(task) +
        "\n本轮用户原文：\n" + current +
        "\n本轮有效请求（简短继续接续最近请求，不回退到旧任务文本；原权限约束仍有效）：\n" + effective)
