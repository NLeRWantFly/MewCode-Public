"""Extract an orchestration stage graph from a chat dialogue via the current model."""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from contextlib import aclosing
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Callable

from mewcode.client import LLMClient, LLMError, ModelRequestTimeout, ModelStreamAdmission, scheduled_client_stream
from mewcode.conversation import ConversationManager
from mewcode.llm_watchdog import FIRST_EVENT_TIMEOUT, ProviderEventTimeout, next_provider_event
from mewcode.orchestration_guide import orchestration_build_experience, orchestration_component_guide
from mewcode.orchestration.propose import propose_orchestration, try_parse_planner_json
from mewcode.orchestration.spec import OrchestrationError, OrchestrationNode, OrchestrationSpec, PlanBrief
from mewcode.orchestration.plan_integrity import (
    is_workflow_reconstruction_request, require_business_goal, require_resolved_plan_inputs,
)
from mewcode.tools.base import (
    StreamEnd,
    StreamStatus,
    TextDelta,
    ThinkingComplete,
    ThinkingDelta,
    ToolCallComplete,
    ToolCallDelta,
    ToolCallStart,
)

ExtractionProgress = Callable[[dict[str, Any]], None]
ORCHESTRATION_FIRST_EVENT_TIMEOUT_SECONDS = FIRST_EVENT_TIMEOUT
# Reasoning may be silent: its default boundary is the request's total budget,
# not an inference that missing visible text means the model is blocked.
ORCHESTRATION_REASONING_IDLE_TIMEOUT_SECONDS = 900.0
ORCHESTRATION_IDLE_TIMEOUT_SECONDS = 180.0
ORCHESTRATION_SOFT_IDLE_WARNING_SECONDS = 60.0


class OrchestrationStreamTimeout(OrchestrationError):
    """The provider stream made no observable progress within its watchdog."""

    def __init__(
        self,
        *,
        phase: str,
        timeout_seconds: float,
        thinking_chars: int = 0,
        output_chars: int = 0,
    ) -> None:
        self.phase = phase
        self.timeout_seconds = float(timeout_seconds)
        self.thinking_chars = int(thinking_chars)
        self.output_chars = int(output_chars)
        if phase == "queue":
            message = f"本地模型调度排队达到 {timeout_seconds:g} 秒；尚未发送 Provider 请求"
        elif phase == "first_event":
            message = f"请求发送后连续 {timeout_seconds:g} 秒未收到首个 Provider 事件"
        elif phase == "reasoning_idle":
            message = (
                f"Provider 已接单或进入推理，但连续 {timeout_seconds:g} 秒"
                "没有返回新的有效流事件"
            )
        else:
            message = (
                f"模型已开始输出或调用工具，但连续 {timeout_seconds:g} 秒"
                "未收到新的流或工具调用活动"
            )
        super().__init__(message)


class OrchestrationGenerationLimit(OrchestrationError):
    """The read-only completion ended without a complete, usable result."""

    def __init__(self, message: str, *, phase: str = "generation_limit",
                 timeout_seconds: float | None = None) -> None:
        self.phase = phase
        self.timeout_seconds = timeout_seconds
        super().__init__(message)

from mewcode.orchestration.plan_text import PLAN_OUTPUT_CONTRACT, compact_plan_text, unique_plan_items
from mewcode.orchestration.loops import is_loop_edge, LOOP_BUDGET_LIMITS

INVENTORY_SYSTEM = """你是 MewCode 的编排规划助手。用户已经用对话描述了一个流程。
你的任务不是立刻画完整 DAG，而是先盘点：每个实质阶段的目标是什么、已知输入有哪些、哪些值可采用默认或作为开跑时集中收集的运行参数。
只输出一个 JSON 对象，不要使用 markdown 代码围栏。

JSON 形状：
{
  "goal": "整条流程的总目标（紧凑）",
  "stages": [
    {
      "stage_id": "inspect_inputs",
      "label": "核对输入",
      "goal": "这一阶段要达成什么（可执行的 Plan 表述，不是口号）",
      "known_inputs": ["用户已给出的任务范围"],
      "open_questions": []
    }
  ]
}

硬性要求：
1. stages 必须覆盖对话里提出的实质步骤，不要塌成空壳模板。
2. open_questions 仅写连总目标都无法确定等真正阻塞构图的问题；主题、路径、数量等只有用户知道的值留作运行参数，合理默认写入已知假设，不要逐项盘问。
3. known_inputs 写对话里已经给出的事实/约束。
4. goal 禁止空泛套话（如「做好审查」），要写清产出物或判定标准。
5. goal 禁止把「立刻停问 / 关键缺口停问」写成默认策略。应写成：先从已知材料与上游交接自助解决；
   仅当核心目标或用户锁定约束无法满足、且无合理替代时，再整理选项问用户。
6. open_questions 不接收普通运行参数或执行期「对不上就停」；若总目标不明，最多一次汇总三个关键问题。
7. 只盘点最新仍生效的业务步骤；用户已删除或替换的历史阶段不要重新加入。
   删除旧节点、清空画布、整理排版是编辑操作，不是新增业务阶段；除非用户明确要求建立清理工作流，否则不要把这些操作变成任务节点或替换原业务总目标。
"""

EXTRACT_SYSTEM = """你是 MewCode 的编排规划助手。根据用户与助手的对话，以及已经盘点好的阶段目标清单，生成完整阶段图。
只输出一个 JSON 对象，不要使用 markdown 代码围栏，不要输出解释性文字。
每个节点声明 capabilities：command_execution、command_prefixes（明确命令前缀的参数数组）、interpreters、network_hosts、external_targets、write_paths、vision_routes、possible_blockers 和 resolvers。只列实际需要的范围；待定范围先向用户提问，不能用通配符授权外发或写入。

JSON 形状：
{
  "goal": "用对话里的用户目标原话或紧凑改写",
  "nodes": [
    {
      "node_id": "inspect_inputs",
      "kind": "plan|implement|reduce|audit|decision|gate",
      "label": "核对输入",
      "goal": "该阶段要完成什么（可执行）",
      "plan_brief": {
        "summary": "一句话 Plan",
        "steps": ["仅该节点的操作"],
        "outputs": ["可定位的产物"],
        "acceptance": ["可验证的完成条件"],
        "known_inputs": ["..."],
        "open_questions": ["..."]
      }
    },
    {"node_id":"summarize","kind":"reduce","label":"交付结果","goal":"汇总产物及证据", "plan_brief":{"summary":"交付结果清单","outputs":["结果与验证记录"],"open_questions":[]}}
  ],
  "edges": [{"from": "inspect_inputs", "to": "summarize"}],
  "finalizer_node_id": "summarize",
  "goal_contract": {"run_inputs": [{"name":"主题","description":"本次工作主题","required":true,"default":""}]},
  "meta": {"control_flow_version": 2, "shared_constraints": [], "loops": []}
}

硬性要求：
1. 阶段必须忠实反映对话与盘点清单，不要退化成空壳模板。
2. 每个节点必须带 plan_brief；open_questions 仅保留总目标无法确定等确实阻塞构图的问题。运行值写入 goal_contract.run_inputs，合理默认写为假设。
3. 若后续对话已经回答了盘点里的待问，必须把答案写入 known_inputs，并从 open_questions 删除。盘点可能过期，以对话最新事实为准。
4. 对话中标明「人工确认」的检查点用 kind=gate。
5. kind 只能是 plan / implement / reduce / audit / decision / gate。decision 是条件判定节点，必须写明判定证据。
6. 若有验收/审核，使用 audit。audit 成功后的下一阶段用 condition=pass（正确）；若后面要接补充修订模型，用 condition=fail（错误）连到新的 implement，这是正常分流，不算失败。
7. node_id 用简短英文蛇形命名；label 用具体的「动作＋对象」中文短句，通常 8～18 字，例如「拆分引言中的过长句子」「核对实验数据与表格」。名称描述职责，不描述实时状态；禁止用 node_id、任务编号、工具名或「处理阶段」等空泛标签充当名称。用户明确指定的名称保留不改。
8. 前向依赖必须是 DAG。分支用 pass/fail；循环只能用显式 condition=loop 的判定→入口回边。
9. 每个循环在 meta.loops 声明：loop_id、entry_node_id、check_node_id、node_ids（全部循环体节点，必须包含入口、判定与全部审查节点自身）、exit_condition、criteria（criterion_id/acceptance_test）、reviewer_node_ids。新循环默认 control_mode="team"，不强制 Token、累计时长或修订轮数上限；不要替用户编造硬预算。只有用户明确要求严格限额才使用 control_mode="strict" 并填写预算。
   例如 {"loop_id":"improve","control_mode":"team","entry_node_id":"build","check_node_id":"check","node_ids":["build","check"],"exit_condition":"测试通过且需求证据完整","criteria":[{"criterion_id":"REQ-01","acceptance_test":"逐项需求有可定位的验证证据"}],"reviewer_node_ids":["check"]}。
   只有入口可接受外部依赖，只有判定节点可退出；必须有 pass 或 always 退出边连到循环外。最终汇总在循环外。允许串联和严格嵌套，禁止交叉循环。
10. 逐章、逐文件等有限队列使用 mode="foreach"，items 列出 [{"item_id":"Q01","goal":"第一章目标"},...]。criteria 仅验收当前项；首轮不需要先有返修清单。后续未执行项不作为当前项失败理由。既有严格模式和验收条件不得在用户不知情时改变。
11. 输出前忽略 loop 回边做拓扑检查。普通缺证据由团队补齐、评分暂时退步可继续修订；重复无新证据才暂停说明原因。确实需要用户提供材料、选择或授权时提出具体问题。不可把停止当通过。普通单向分支不要伪造循环。
11. goal 与 plan_brief.summary 禁止默认「立刻停问」。缺口处理必须先自助再问，顺序是：
    (a) 从既有材料/已知输入/上游交接查找证据；
    (b) 非关键项用 warning/占位并交给后续一致性阶段；
    (c) 仅核心目标或锁定约束无法推进时，整理 2～3 个选项再问用户。
12. 不要把执行期审计语气、主题、路径、数量写进 open_questions；只有用户知道的运行值在开跑时集中收集。
13. 输出的是完整的新快照，不是历次方案的并集。以最新已保存阶段图和用户最新修改为准；被删除、替换的旧阶段不可因历史对话再次出现。
14. 用户仅要求删旧节点、清空或重排时，修改现有图的成员和布局，不新增“定位旧节点/清理画布/验证清理”等业务节点，也不要把论文等原业务目标改成“清理画布”。明确要求新建清理工作流时除外。
"""

REFILL_SYSTEM = """你是 MewCode 的编排规划助手。阶段图已经确定，不要改动它。
你的任务只有一件：根据最新对话，为每个既有阶段重写 Plan 简报（目标、已知输入、仍未回答的问题）。
只输出一个 JSON 对象，不要使用 markdown 代码围栏，不要输出解释性文字。

JSON 形状：
{
  "stages": [
    {
      "stage_id": "inspect_inputs",
      "goal": "这一阶段要达成什么（可执行，不是口号）",
      "summary": "一句话摘要",
      "steps": ["最多五条独有操作"],
      "outputs": ["最多四项产物"],
      "acceptance": ["最多四条验收"],
      "known_inputs": ["对话里已经给出的事实/约束"],
      "open_questions": ["总目标仍无法确定时的阻塞问题；没有就给空数组"]
    }
  ]
}

硬性要求：
1. stage_id 必须用下面给出的既有阶段 id，不要新增、删除或改名阶段。
2. 禁止输出 nodes / edges / kind / model 等结构字段；结构由用户在画布上决定。
3. 对话已经回答的问题必须写进 known_inputs，并从 open_questions 删除。
4. 除总目标仍无法确定等阻塞构图的情形外，open_questions 给空数组；运行值不在这里追问。
5. 重写 goal 时去掉「立刻停问 / 关键缺口停问」默认策略，改为「先自助尝试，关键且无替代再问用户」。
6. 用户在问答工具中的回答与普通用户消息同样有效；以用户明确给出的最新口径为准，不能仅凭助手说“已确认”清除问题。
7. 用户明确允许按现有定义处理或把某项留待执行阶段确认时，写入 known_inputs / acceptance，不要继续作为开跑阻塞问题。
8. 必须写入具体事实，不得使用“既有阶段 xxx.known_inputs 中的内容”或“最新修复方案第 N 节”等占位引用。
9. 若最新修订需要修改节点、连线、条件或循环，返回 {"requires_graph_review":true,"reason":"需要修改的结构"}；不得只改文字并宣称已修复图。
"""

INVENTORY_SYSTEM += "\n\n" + PLAN_OUTPUT_CONTRACT + "\n\n" + orchestration_component_guide()
EXTRACT_SYSTEM += "\n\n" + PLAN_OUTPUT_CONTRACT + "\n\n" + orchestration_component_guide()
EXTRACT_SYSTEM += "\nstrict 模式的可选预算字段范围（team 模式省略；不要用 0 代替模式选择）：" + json.dumps(LOOP_BUDGET_LIMITS, ensure_ascii=False)
REFILL_SYSTEM += "\n\n" + PLAN_OUTPUT_CONTRACT + "\n\n" + orchestration_component_guide()
_BUILD_EXPERIENCE = ("\n\n以下经验用于理解职责和执行条件；仅输出本轮允许的阶段清单、"
                     "Spec 或计划字段，不扩展字段，不替用户选择，不通过计划回填改变拓扑。\n\n"
                     + orchestration_build_experience())
INVENTORY_SYSTEM += _BUILD_EXPERIENCE
EXTRACT_SYSTEM += _BUILD_EXPERIENCE
REFILL_SYSTEM += _BUILD_EXPERIENCE
_BUSINESS_GOAL_RULE = "\n用户要求复原/恢复/解释已有流程是编辑指令，不是新的业务目标或执行节点。应恢复历史中的业务主流程，不要生成核对历史、还原角色、总结流程的元流程。"
INVENTORY_SYSTEM += _BUSINESS_GOAL_RULE
EXTRACT_SYSTEM += _BUSINESS_GOAL_RULE

EXTRACT_USER_PREFIX = (
    "以下是当前会话对话（按时间顺序）。请据此生成阶段图 JSON。"
    "若已提供阶段目标盘点，请以其阶段划分为准展开节点；"
    "known_inputs / open_questions 必须按对话最新内容更新，"
    "不要把已经回答的问题原样抄回。\n\n"
)


def try_parse_json_object(text: str) -> dict[str, Any] | None:
    """Extract a JSON object from model output (inventory or planner)."""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, count=1, flags=re.I)
        text = re.sub(r"\s*```\s*$", "", text)
        text = text.strip()
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def coerce_dialogue_messages(raw: Any) -> list[Any]:
    """Normalize client- or server-provided message lists."""
    if not isinstance(raw, list):
        return []
    out: list[Any] = []
    human_calls: dict[str, Any] = {}
    def field(item: Any, key: str, default: Any = None) -> Any:
        return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)

    for item in raw:
        if item is None:
            continue
        if field(item, "transient", False):
            continue
        for call in field(item, "tool_uses", []) or []:
            if field(call, "tool_name") in {"AskUserQuestion", "human_oncall"}:
                human_calls[str(field(call, "tool_use_id"))] = field(call, "arguments", {})
        if hasattr(item, "role") and hasattr(item, "content"):
            role = str(getattr(item, "role", "") or "")
            content = str(getattr(item, "content", "") or "").strip()
        elif isinstance(item, dict):
            role = str(item.get("role") or "").strip()
            content = str(item.get("content") or item.get("text") or "").strip()
        else:
            continue
        if role in {"user", "assistant"} and content:
            out.append(SimpleNamespace(role=role, content=content))
        # Only linked human-interaction results are user answers. Shell/file
        # output must never become apparent user instructions in the planner.
        for result in field(item, "tool_results", []) or []:
            call_id = str(field(result, "tool_use_id"))
            if call_id not in human_calls or field(result, "is_error", False):
                continue
            answer = str(field(result, "content", "") or "").strip()
            if not answer:
                continue
            try:
                value = json.loads(answer)
            except (ValueError, TypeError):
                value = None
            if isinstance(value, dict) and value.get("status") in {"cancelled", "skipped"}:
                continue
            out.append(SimpleNamespace(role="user", content=(
                "用户问答工具回复：\n问题："
                + json.dumps(human_calls[call_id], ensure_ascii=False)
                + "\n回答：" + answer
            )))
    return out


def format_dialogue_transcript(
    messages: list[Any],
    *,
    max_chars: int = 16000,
    tail_on_overflow: bool = False,
) -> str:
    """Render chat history into a compact transcript for the extractor prompt.

    ``tail_on_overflow`` keeps only the most recent ``max_chars`` when the
    dialogue is too long. Graph extraction must never do that silently (the
    dropped prefix is where the requirements live). Extraction and brief
    refills both require full history; the opt-in is only for display callers.
    """
    lines: list[str] = []
    for message in coerce_dialogue_messages(messages) or messages:
        role = str(getattr(message, "role", "") or "")
        if role not in {"user", "assistant"}:
            continue
        content = str(getattr(message, "content", "") or "").strip()
        if role == "assistant":
            content = compact_plan_text(content)
        if not content:
            continue
        label = "用户" if role == "user" else "助手"
        lines.append(f"{label}：{content}")
    text = "\n\n".join(lines).strip()
    if len(text) > max_chars:
        if tail_on_overflow:
            text = text[-max_chars:]
            cut = text.find("\n")
            if 0 <= cut < 200:
                text = text[cut + 1 :]
            return "…(前文已截断)\n" + text
        raise OrchestrationError(
            "对话超过单次抽取上限，未截断前文。请打开「流程底稿」分块补全历史，确认底稿后生成流程图。"
        )
    return text


def _goal_from_messages(messages: list[Any]) -> str:
    for message in reversed(coerce_dialogue_messages(messages) or messages):
        role = str(getattr(message, "role", "") or "")
        content = str(getattr(message, "content", "") or "").strip()
        if role == "user" and content and not is_workflow_reconstruction_request(content):
            return content[:500]
    return ""


def normalize_inventory(raw: Any) -> dict[str, Any]:
    """Normalize Pass-1 inventory JSON."""
    if not isinstance(raw, dict):
        raise OrchestrationError("目标盘点结果无效")
    goal = str(raw.get("goal") or "").strip()
    stages_raw = raw.get("stages") if isinstance(raw.get("stages"), list) else []
    if not stages_raw and isinstance(raw.get("nodes"), list):
        stages_raw = raw.get("nodes") or []
    stages: list[dict[str, Any]] = []
    for index, item in enumerate(stages_raw):
        if not isinstance(item, dict):
            continue
        stage_id = str(item.get("stage_id") or item.get("node_id") or f"stage_{index + 1}").strip()
        label = str(item.get("label") or stage_id).strip()
        stage_goal = str(item.get("goal") or "").strip()
        known = item.get("known_inputs") if isinstance(item.get("known_inputs"), list) else []
        questions = item.get("open_questions") if isinstance(item.get("open_questions"), list) else []
        stages.append({
            "stage_id": stage_id,
            "label": label,
            "goal": stage_goal or label,
            "known_inputs": [str(x).strip() for x in known if str(x).strip()],
            "open_questions": [str(x).strip() for x in questions if str(x).strip()],
        })
    if not stages:
        raise OrchestrationError("目标盘点没有列出任何阶段，请补充对话后再试")
    return {"goal": goal, "stages": stages}


def collect_open_questions(spec: OrchestrationSpec) -> list[dict[str, str]]:
    """Flatten unresolved questions for the pre-run clarify turn."""
    items: list[dict[str, str]] = []
    for node in spec.nodes:
        brief = node.plan_brief
        if brief is None:
            continue
        for question in brief.open_questions:
            items.append({
                "node_id": node.node_id,
                "label": node.label,
                "question": question,
            })
    # New source-linked graphs distinguish goal blockers from review notes.
    # Legacy PlanBrief strings remain blockers because their intent is unknown.
    draft_meta = spec.meta.get("workflow_draft")
    rows = draft_meta.get("blocking_questions", []) if isinstance(draft_meta, dict) else []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("blocking") is not True:
            continue
        question = str(row.get("question") or "").strip()
        if question:
            item = {"node_id": str(row.get("node_id") or "goal"),
                    "label": str(row.get("label") or "总目标"), "question": question}
            if item not in items:
                items.append(item)
    return items


def build_clarify_prompt(spec: OrchestrationSpec) -> str:
    """Compose a main-chat clarify message before expert execution."""
    questions = collect_open_questions(spec)
    if not questions:
        return ""
    lines = [
        "【编排开跑前澄清】以下关键输入仍未确定，请在主聊天补充回答；"
        "答完后再次点击「确认开跑」，系统会先回填最新口径。",
        f"总目标：{spec.goal or '（未命名）'}",
        "",
    ]
    for index, item in enumerate(questions, start=1):
        lines.append(f"{index}. [{item['label']}] {item['question']}")
    lines.append("")
    lines.append("若某项无需核验或可留待执行阶段处理，请明确说明；本次尚未启动任务。")
    return "\n".join(lines)


async def collect_model_text(
    client: LLMClient,
    *,
    system: str,
    user_text: str,
    progress: ExtractionProgress | None = None,
    first_event_timeout: float = ORCHESTRATION_FIRST_EVENT_TIMEOUT_SECONDS,
    reasoning_idle_timeout: float | None = None,
    idle_timeout: float = ORCHESTRATION_IDLE_TIMEOUT_SECONDS,
    total_timeout: float | None = 900.0,
    max_generated_chars: int | None = 160_000,
    soft_idle_warning: float = ORCHESTRATION_SOFT_IDLE_WARNING_SECONDS,
    queue_timeout: float = ORCHESTRATION_FIRST_EVENT_TIMEOUT_SECONDS,
) -> str:
    """One-shot non-tool completion; concatenate TextDelta chunks."""
    probe = ConversationManager()
    probe.add_user_message(user_text)
    # Pull and close in the same Task, including cancellation and parse limits.
    options = {"request_timeout": total_timeout, "include_admission_events": True}
    if total_timeout is None:
        options["enforce_deadline"] = False
    async with aclosing(scheduled_client_stream(client, probe, system=system, tools=None, **options)) as stream:
        return await _collect_model_stream(stream.__aiter__(), progress=progress,
            first_event_timeout=first_event_timeout, reasoning_idle_timeout=reasoning_idle_timeout,
            idle_timeout=idle_timeout, total_timeout=total_timeout,
            max_generated_chars=max_generated_chars, soft_idle_warning=soft_idle_warning,
            queue_timeout=queue_timeout)


async def _collect_model_stream(iterator, *, progress, first_event_timeout,
                                reasoning_idle_timeout, idle_timeout, total_timeout,
                                max_generated_chars, soft_idle_warning, queue_timeout):
    chunks: list[str] = []
    thinking_chars = 0
    thinking_summary_chars = 0
    thinking_preview = ""
    output_chars = 0
    output_chunks = 0
    output_preview = ""
    provider_events = 0
    tool_call_chars = 0
    active_tool_name = ""
    visible_stage = "requesting"
    visible_detail = "正在准备模型请求，等待调度与响应。"
    _emit_extraction_progress(
        progress,
        stage=visible_stage,
        detail=visible_detail,
    )
    saw_provider_event = False
    current_phase = "reasoning_idle"
    started_at = time.monotonic()
    queued_at = None

    def total_limit(seconds=total_timeout):
        detail = f"模型请求达到总时限 {seconds:g} 秒；本次输出未完成，原图和已保存块保持不变。"
        _emit_extraction_progress(progress, stage="time_limit", detail=detail,
            thinking_chars=thinking_chars, output_chars=output_chars,
            provider_events=provider_events, stalled_phase="request_total")
        return OrchestrationGenerationLimit(detail, phase="request_total", timeout_seconds=seconds)

    while True:
        remaining = (queue_timeout - (time.monotonic() - queued_at) if queued_at is not None
                     else float("inf") if total_timeout is None
                     else total_timeout - (time.monotonic() - started_at))
        if remaining <= 0:
            if queued_at is not None:
                raise OrchestrationStreamTimeout(phase="queue", timeout_seconds=queue_timeout)
            raise total_limit()
        if max_generated_chars is not None and output_chars + thinking_chars + tool_call_chars > max_generated_chars:
            reason = f"推理 {thinking_chars} 字 + 输出 {output_chars} 字超过预算 {max_generated_chars} 字"
            raise OrchestrationGenerationLimit(f"模型生成{reason}；本次输出未完成，原图和已有进度保持不变，可缩小本次块后继续。")
        if queued_at is not None:
            phase = "queue"
            watchdog = queue_timeout
        elif not saw_provider_event:
            phase = "first_event"
            watchdog = first_event_timeout
        elif current_phase == "stream_idle":
            phase = "stream_idle"
            watchdog = idle_timeout
        else:
            phase = "reasoning_idle"
            watchdog = remaining if reasoning_idle_timeout is None else reasoning_idle_timeout
        wait_limit = min(watchdog, remaining)

        def report_waiting():
            detail = (("模型可能正在静默推理，继续等待当前请求；完整结果保存后再评估后续工作。"
                       if total_timeout is None else
                       f"模型可能正在静默推理，继续等待原请求；本次请求总预算 {total_timeout:g} 秒，未重新提交。")
                      if phase == "reasoning_idle" and reasoning_idle_timeout is None else
                      "仍在本地调度队列中，尚未发送 Provider 请求。" if phase == "queue" else
                      f"当前请求暂时无新事件，仍在等待响应；本次等待上限 {wait_limit:g} 秒，未重新发起请求。")
            _emit_extraction_progress(progress, stage=visible_stage,
                detail=detail,
                waiting=True, thinking_chars=thinking_chars, output_chars=output_chars,
                output_chunks=output_chunks, provider_events=provider_events)

        warning = (asyncio.get_running_loop().call_later(soft_idle_warning, report_waiting)
                   if 0 < soft_idle_warning < wait_limit else None)
        try:
            event = await next_provider_event(iterator, None if wait_limit == float("inf") else wait_limit)
        except StopAsyncIteration:
            break
        except ModelRequestTimeout as exc:
            raise total_limit(exc.seconds) from exc
        except ProviderEventTimeout as exc:
            if queued_at is None and total_timeout is not None and time.monotonic() - started_at >= total_timeout:
                raise total_limit() from exc
            _emit_extraction_progress(
                progress,
                stage="stalled",
                detail=(
                    f"本地调度排队达到 {watchdog:g} 秒，尚未发送 Provider 请求。"
                    if phase == "queue" else
                    f"请求发送后连续 {watchdog:g} 秒未收到首个 Provider 事件。"
                    if phase == "first_event"
                    else (
                        f"Provider 已进入推理，但连续 {watchdog:g} 秒"
                        "没有新有效流事件；不会自动重复提交同一请求。"
                        if phase == "reasoning_idle"
                        else (
                            f"模型已开始输出或调用工具，但连续 {watchdog:g} 秒"
                            "没有新活动，正在准备恢复策略。"
                        )
                    )
                ),
                thinking_chars=thinking_chars,
                thinking_summary_chars=thinking_summary_chars,
                thinking_preview=thinking_preview,
                thinking_truncated=(
                    thinking_summary_chars > len(thinking_preview)
                ),
                output_chars=output_chars,
                output_chunks=output_chunks,
                provider_events=provider_events,
                stalled_phase=phase,
            )
            raise OrchestrationStreamTimeout(
                phase=phase,
                timeout_seconds=watchdog,
                thinking_chars=thinking_chars,
                output_chars=output_chars,
            ) from exc
        except TimeoutError as exc:
            raise LLMError("Provider 连接或请求超时；不是本地静默判定或整体构图预算到期。") from exc
        finally:
            if warning is not None:
                warning.cancel()
        if isinstance(event, ModelStreamAdmission):
            visible_stage, visible_detail = event.stage, event.detail
            if event.stage == "queued":
                queued_at = time.monotonic()
            else:
                queued_at = None
                started_at = time.monotonic()
            _emit_extraction_progress(progress, stage=visible_stage, detail=visible_detail,
                provider_events=provider_events, request_admitted=event.stage != "queued")
            continue
        saw_provider_event = True
        if isinstance(event, StreamStatus):
            if "reason" in event.stage or event.stage == "thinking":
                current_phase = "reasoning_idle"
            provider_events += 1
            activity_only = event.stage == "activity"
            if not activity_only:
                visible_stage = event.stage or "processing"
                visible_detail = event.detail or "Provider 已接收请求，正在处理。"
            _emit_extraction_progress(
                progress,
                stage=visible_stage,
                detail=visible_detail,
                provider_events=provider_events,
                provider_event=True,
                activity_detail=event.detail if activity_only else "",
            )
        elif isinstance(event, ThinkingDelta) and event.text:
            current_phase = "reasoning_idle"
            thinking_chars += len(event.text)
            if event.kind == "summary":
                thinking_summary_chars += len(event.text)
                thinking_preview = (thinking_preview + event.text)[-12000:]
            provider_events += 1
            visible_stage = "thinking"
            visible_detail = (
                "模型正在返回可见推理摘要，随后将输出最终 JSON。"
                if event.kind == "summary"
                else "模型正在推理阶段图结构，尚未输出最终 JSON。"
            )
            _emit_extraction_progress(
                progress,
                stage=visible_stage,
                detail=visible_detail,
                thinking_chars=thinking_chars,
                thinking_summary_chars=thinking_summary_chars,
                thinking_preview=thinking_preview,
                thinking_truncated=(
                    thinking_summary_chars > len(thinking_preview)
                ),
                provider_events=provider_events,
                provider_event=True,
            )
        elif isinstance(event, TextDelta) and event.text:
            chunks.append(event.text)
            output_chars += len(event.text)
            output_chunks += 1
            provider_events += 1
            output_preview = (output_preview + event.text)[-24000:]
            if event.text.strip():
                current_phase = "stream_idle"
                visible_stage = "streaming"
                visible_detail = "模型正在流式输出阶段图 JSON。"
            _emit_extraction_progress(
                progress,
                stage=visible_stage,
                detail=visible_detail,
                thinking_chars=thinking_chars,
                thinking_summary_chars=thinking_summary_chars,
                thinking_preview=thinking_preview,
                thinking_truncated=(
                    thinking_summary_chars > len(thinking_preview)
                ),
                output_chars=output_chars,
                output_chunks=output_chunks,
                output_preview=output_preview,
                output_delta=event.text,
                output_truncated=output_chars > len(output_preview),
                provider_events=provider_events,
                provider_event=True,
            )
        elif isinstance(event, ThinkingComplete):
            current_phase = "reasoning_idle"
            provider_events += 1
            visible_stage = "thinking"
            visible_detail = "模型推理块已完成，正在继续生成结构化结果。"
            _emit_extraction_progress(
                progress,
                stage=visible_stage,
                detail=visible_detail,
                thinking_chars=thinking_chars,
                thinking_summary_chars=thinking_summary_chars,
                thinking_preview=thinking_preview,
                thinking_truncated=(
                    thinking_summary_chars > len(thinking_preview)
                ),
                provider_events=provider_events,
                provider_event=True,
            )
        elif isinstance(event, ToolCallStart):
            current_phase = "stream_idle"
            provider_events += 1
            active_tool_name = event.tool_name or "未命名工具"
            visible_stage = "tooling"
            visible_detail = f"模型正在准备工具调用：{active_tool_name}。"
            _emit_extraction_progress(
                progress,
                stage=visible_stage,
                detail=visible_detail,
                tool_name=active_tool_name,
                tool_call_chars=tool_call_chars,
                provider_events=provider_events,
                provider_event=True,
            )
        elif isinstance(event, ToolCallDelta):
            current_phase = "stream_idle"
            provider_events += 1
            tool_call_chars += len(event.text or "")
            visible_stage = "tooling"
            visible_detail = (
                f"模型正在生成工具 {active_tool_name or '调用'} 的参数。"
            )
            _emit_extraction_progress(
                progress,
                stage=visible_stage,
                detail=visible_detail,
                tool_name=active_tool_name,
                tool_call_chars=tool_call_chars,
                provider_events=provider_events,
                provider_event=True,
            )
        elif isinstance(event, ToolCallComplete):
            current_phase = "stream_idle"
            provider_events += 1
            active_tool_name = event.tool_name or active_tool_name or "未命名工具"
            visible_stage = "tooling"
            visible_detail = f"模型工具调用已生成：{active_tool_name}。"
            _emit_extraction_progress(
                progress,
                stage=visible_stage,
                detail=visible_detail,
                tool_name=active_tool_name,
                tool_call_chars=tool_call_chars,
                provider_events=provider_events,
                provider_event=True,
            )
        elif isinstance(event, StreamEnd):
            if event.stop_reason in {"length", "max_tokens", "max_output_tokens"}:
                raise OrchestrationGenerationLimit("模型达到单次输出上限，结构化结果未完整结束；未应用本次结果。")
            provider_events += 1
            visible_stage = "received"
            visible_detail = "模型输出接收完成，正在解析结构化结果。"
            _emit_extraction_progress(
                progress,
                stage=visible_stage,
                detail=visible_detail,
                thinking_chars=thinking_chars,
                thinking_summary_chars=thinking_summary_chars,
                thinking_preview=thinking_preview,
                thinking_truncated=(
                    thinking_summary_chars > len(thinking_preview)
                ),
                output_chars=output_chars,
                output_chunks=output_chunks,
                output_preview=output_preview,
                output_truncated=output_chars > len(output_preview),
                input_tokens=int(event.input_tokens or 0),
                output_tokens=int(event.output_tokens or 0),
                provider_events=provider_events,
                provider_event=True,
            )
            break
    return CollectedModelText("".join(chunks).strip(), time.monotonic() - started_at)


class CollectedModelText(str):
    """String-compatible result with admitted request time, excluding queueing."""

    def __new__(cls, value, active_seconds):
        instance = super().__new__(cls, value)
        instance.active_seconds = max(0.0, active_seconds)
        return instance


def _emit_extraction_progress(
    progress: ExtractionProgress | None,
    *,
    stage: str,
    detail: str,
    **metrics: Any,
) -> None:
    if progress is None:
        return
    try:
        progress({"status": "running", "stage": stage, "detail": detail, **metrics})
    except Exception:
        # Progress reporting must never make a valid model response fail.
        return


async def inventory_orchestration_from_dialogue(
    client: LLMClient,
    messages: list[Any],
    *,
    revision: str = "",
    progress: ExtractionProgress | None = None,
) -> dict[str, Any]:
    """Pass 1: inventory stage goals / known inputs / open questions."""
    normalized = coerce_dialogue_messages(messages)
    transcript = format_dialogue_transcript(normalized or messages)
    if not transcript:
        raise OrchestrationError("请先在聊天中与模型商量目标，再盘点阶段目标")

    user_prompt = "以下是当前会话对话。请盘点各阶段目标 JSON。\n\n" + transcript
    if revision.strip():
        user_prompt += "\n\n补充修订要求：\n" + revision.strip()

    collect_kwargs = {"progress": progress} if progress is not None else {}
    raw_text = await collect_model_text(
        client,
        system=INVENTORY_SYSTEM,
        user_text=user_prompt,
        **collect_kwargs,
    )
    if not raw_text:
        raise OrchestrationError("当前模型未返回目标盘点，请重试")
    _emit_extraction_progress(
        progress,
        stage="parsing",
        detail="正在解析阶段目标并检查缺失输入。",
    )
    parsed = try_parse_json_object(raw_text)
    if parsed is None:
        preview = raw_text[:240].replace("\n", " ")
        raise OrchestrationError("目标盘点无法解析为 JSON。预览：" + preview)
    inventory = normalize_inventory(parsed)
    if not inventory["goal"]:
        inventory["goal"] = _goal_from_messages(normalized or messages)
    require_business_goal(inventory["goal"], normalized or messages)
    return {
        "inventory": inventory,
        "extractor_raw": raw_text,
        "transcript_chars": len(transcript),
        "source_turns": len(normalized or messages),
        "message": "已盘点各阶段目标。请确认后生成阶段图。",
    }


async def refill_plan_briefs_from_dialogue(
    client: LLMClient,
    messages: list[Any],
    *,
    spec: OrchestrationSpec | dict[str, Any],
    revision: str = "",
    progress: ExtractionProgress | None = None,
) -> dict[str, Any]:
    """Refresh每个阶段的 Plan 简报，但保持阶段图结构原样。

    Structure, edges, canvas layout and model bindings are owned by the user
    here — only stage goals and ``plan_brief`` are rewritten from the latest dialogue.
    """
    current = spec if isinstance(spec, OrchestrationSpec) else OrchestrationSpec.from_value(spec)
    normalized = coerce_dialogue_messages(messages)
    transcript = format_dialogue_transcript(normalized or messages)
    if not transcript:
        raise OrchestrationError("请先在聊天中与模型商量目标，再回填 Plan 简报")

    stage_digest = [
        {
            "stage_id": node.node_id,
            "label": node.label,
            "goal": node.goal,
            **(node.plan_brief.as_dict() if node.plan_brief else {}),
        }
        for node in current.nodes
    ]
    user_prompt = (
        "以下是当前会话对话（按时间顺序）。请只为既有阶段回填 Plan 简报。\n\n"
        + transcript
        + "\n\n既有阶段（stage_id 必须原样使用）：\n"
        + json.dumps(stage_digest, ensure_ascii=False, indent=2)
    )
    if revision.strip():
        user_prompt += "\n\n补充修订要求：\n" + revision.strip()

    collect_kwargs = {"progress": progress} if progress is not None else {}
    raw_text = await collect_model_text(
        client,
        system=REFILL_SYSTEM,
        user_text=user_prompt,
        **collect_kwargs,
    )
    if not raw_text:
        raise OrchestrationError("当前模型未返回 Plan 简报，请重试或换模型后再回填")

    _emit_extraction_progress(
        progress,
        stage="parsing",
        detail="正在解析各阶段 Plan 简报与剩余待问。",
    )
    parsed = try_parse_json_object(raw_text)
    if parsed is None:
        preview = raw_text[:240].replace("\n", " ")
        raise OrchestrationError("Plan 简报无法解析为 JSON。预览：" + preview)
    if parsed.get("requires_graph_review") or set(parsed) & {"nodes", "edges", "loops", "meta", "goal", "model", "kind"}:
        raise OrchestrationError("本次修订涉及流程结构，请在流程底稿核对节点与连线差异；未保存部分回填")
    if not isinstance(parsed.get("stages"), list) or not parsed["stages"]:
        raise OrchestrationError("未返回有效阶段简报，未保存回填")

    by_id: dict[str, dict[str, Any]] = {}
    by_label: dict[str, dict[str, Any]] = {}
    for item in parsed.get("stages") or []:
        if not isinstance(item, dict):
            raise OrchestrationError("阶段简报格式错误，未保存回填")
        if set(item) - {"stage_id", "node_id", "label", "goal", "summary", "known_inputs", "open_questions", "steps", "outputs", "acceptance"}:
            raise OrchestrationError("阶段回填包含结构字段，请在流程底稿核对，未保存部分回填")
        stage_id = str(item.get("stage_id") or item.get("node_id") or "").strip()
        label = str(item.get("label") or "").strip()
        if stage_id not in current.node_map() or stage_id in by_id:
            raise OrchestrationError("阶段回填包含未知或重复 ID，未保存回填")
        if stage_id:
            by_id[stage_id] = item
        if label:
            by_label[label] = item

    nodes: list[OrchestrationNode] = []
    for node in current.nodes:
        hit = by_id.get(node.node_id) or by_label.get(node.label.strip())
        if hit is None:
            nodes.append(node)
            continue
        previous_brief = node.plan_brief.as_dict() if node.plan_brief else {}
        brief = PlanBrief.from_value({
            "summary": hit.get("summary") or hit.get("goal") or node.goal or node.label,
            **{key: hit[key] if isinstance(hit.get(key), list) else previous_brief.get(key, [])
               for key in ("known_inputs", "open_questions", "steps", "outputs", "acceptance")},
        })
        nodes.append(replace(node, plan_brief=brief,
                             goal=str(hit.get("goal") or node.goal).strip()) if brief is not None else node)

    refreshed = replace(current, nodes=tuple(nodes))
    require_resolved_plan_inputs(refreshed)
    inventory = inventory_from_spec(refreshed, (current.meta or {}).get("inventory"))
    meta = dict(refreshed.meta or {})
    if inventory["stages"]:
        meta["inventory"] = inventory
    refreshed = replace(refreshed, meta=meta)

    questions = collect_open_questions(refreshed)
    return {
        "spec": refreshed.as_dict(),
        "orchestration": refreshed.as_dict(),
        "inventory": inventory,
        "open_questions": questions,
        "needs_clarify": bool(questions),
        "clarify_prompt": build_clarify_prompt(refreshed),
        "extractor_raw": raw_text,
        "transcript_chars": len(transcript),
        "source_turns": len(normalized or messages),
        "message": (
            "已按最新对话回填 Plan 简报，阶段结构与模型绑定保持不变"
            if not questions
            else "已回填 Plan 简报；仍有待澄清项，开跑前会先在主聊天问清"
        ),
    }


def _merge_inventory_into_nodes(parsed: dict[str, Any], inventory: dict[str, Any] | None) -> dict[str, Any]:
    if not inventory or not isinstance(inventory.get("stages"), list):
        return parsed
    by_id = {
        str(stage.get("stage_id") or ""): stage
        for stage in inventory["stages"]
        if isinstance(stage, dict)
    }
    by_label = {
        str(stage.get("label") or ""): stage
        for stage in inventory["stages"]
        if isinstance(stage, dict)
    }
    nodes = parsed.get("nodes") if isinstance(parsed.get("nodes"), list) else []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        hit = by_id.get(str(node.get("node_id") or "")) or by_label.get(str(node.get("label") or ""))
        if not hit:
            continue
        if not str(node.get("goal") or "").strip():
            node["goal"] = hit.get("goal") or node.get("label") or ""
        extracted = PlanBrief.from_value(node.get("plan_brief"))
        inventory_brief = PlanBrief.from_value({
            "summary": hit.get("goal") or node.get("goal") or "",
            "known_inputs": hit.get("known_inputs") or [],
            "open_questions": hit.get("open_questions") or [],
        })
        if extracted is None:
            if inventory_brief is not None:
                node["plan_brief"] = inventory_brief.as_dict()
            continue
        known = _unique_texts(
            *(inventory_brief.known_inputs if inventory_brief is not None else ()),
            *extracted.known_inputs,
        )
        node["plan_brief"] = PlanBrief(
            summary=(
                extracted.summary
                or (inventory_brief.summary if inventory_brief is not None else "")
                or str(node.get("goal") or "")
            ),
            known_inputs=known,
            open_questions=extracted.open_questions,
            steps=extracted.steps,
            outputs=extracted.outputs,
            acceptance=extracted.acceptance,
        ).as_dict()
    return parsed


def _unique_texts(*items: Any) -> tuple[str, ...]:
    return unique_plan_items(items)


def inventory_from_spec(
    spec: OrchestrationSpec,
    fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Rebuild the Pass-1 inventory from the latest node briefs."""
    fallback = fallback if isinstance(fallback, dict) else {}
    fallback_stages = fallback.get("stages") if isinstance(fallback.get("stages"), list) else []
    fallback_by_id = {
        str(item.get("stage_id") or ""): item
        for item in fallback_stages
        if isinstance(item, dict)
    }
    stages: list[dict[str, Any]] = []
    for node in spec.nodes:
        brief = node.plan_brief
        prev = fallback_by_id.get(node.node_id) or {}
        stages.append({
            "stage_id": node.node_id,
            "label": node.label or str(prev.get("label") or node.node_id),
            "goal": (
                (brief.summary if brief is not None else "")
                or node.goal
                or str(prev.get("goal") or node.label)
            ),
            "known_inputs": (
                list(brief.known_inputs) if brief is not None
                else [str(x).strip() for x in (prev.get("known_inputs") or []) if str(x).strip()]
            ),
            "open_questions": (
                list(brief.open_questions) if brief is not None
                else [str(x).strip() for x in (prev.get("open_questions") or []) if str(x).strip()]
            ),
        })
    return {
        "goal": spec.goal or str(fallback.get("goal") or ""),
        "stages": stages,
    }


def _edge_endpoints(edge: Any) -> tuple[str, str]:
    if not isinstance(edge, dict):
        return "", ""
    return (
        str(edge.get("from_node") or edge.get("from") or "").strip(),
        str(edge.get("to_node") or edge.get("to") or "").strip(),
    )


def _find_cycle_edge_indices(
    node_ids: list[str],
    edges: list[Any],
) -> list[int]:
    """Return the edge indices for one directed cycle, or an empty list."""
    known = set(node_ids)
    adjacency: dict[str, list[tuple[str, int]]] = {node_id: [] for node_id in node_ids}
    for index, edge in enumerate(edges):
        source, target = _edge_endpoints(edge)
        if source in known and target in known:
            adjacency[source].append((target, index))

    state = {node_id: 0 for node_id in node_ids}
    parent: dict[str, tuple[str, int]] = {}

    def visit(node_id: str) -> list[int]:
        state[node_id] = 1
        for target, edge_index in adjacency[node_id]:
            if state[target] == 0:
                parent[target] = (node_id, edge_index)
                found = visit(target)
                if found:
                    return found
            elif state[target] == 1:
                cycle = [edge_index]
                cursor = node_id
                while cursor != target:
                    previous, parent_edge = parent[cursor]
                    cycle.append(parent_edge)
                    cursor = previous
                return cycle
        state[node_id] = 2
        return []

    for node_id in node_ids:
        if state[node_id] == 0:
            found = visit(node_id)
            if found:
                return found
    return []


def repair_extracted_graph_cycles(
    parsed: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Migrate legacy audit returns; preserve typed loops and reject other cycles."""
    # A failed candidate must not mutate the caller's saved graph or draft.
    parsed = copy.deepcopy(parsed)
    nodes = parsed.get("nodes") if isinstance(parsed.get("nodes"), list) else []
    node_items = [node for node in nodes if isinstance(node, dict)]
    node_ids = [str(node.get("node_id") or node.get("id") or "").strip() for node in node_items]
    node_ids = [node_id for node_id in node_ids if node_id]
    nodes_by_id = {
        str(node.get("node_id") or node.get("id") or "").strip(): node
        for node in node_items
        if str(node.get("node_id") or node.get("id") or "").strip()
    }
    order = {node_id: index for index, node_id in enumerate(node_ids)}
    edges = list(parsed.get("edges") or []) if isinstance(parsed.get("edges"), list) else []
    repairs: list[str] = []
    strict = int((parsed.get("meta") or {}).get("control_flow_version") or 0) >= 2 or bool(
        (parsed.get("meta") or {}).get("loops") or parsed.get("loops"))

    contracts = parsed.get("loops") or (parsed.get("meta") or {}).get("loops") or []
    returns = {(str(loop.get("check_node_id") or ""), str(loop.get("entry_node_id") or ""))
               for loop in contracts if isinstance(loop, dict)}
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        source, target = _edge_endpoints(edge)
        condition = str(edge.get("condition") or "").strip()
        if not condition and is_loop_edge(edge.get("label", "")):
            edge["condition"] = "loop"
            repairs.append(f"将 {source} → {target} 的显式循环标签规范为 condition=loop")
            continue
        if (source, target) in returns and condition.casefold() in {"", "always", "fail", "false", "失败", "错误", "不通过"}:
            edge["condition"] = "loop"
            repairs.append(f"按已声明循环合同将 {source} → {target} 的 {condition or '普通依赖'} 标为 loop；保留全部依赖")

    while True:
        forward_indices = [i for i, edge in enumerate(edges)
                           if not isinstance(edge, dict) or not is_loop_edge(edge.get("condition", ""))]
        forward_cycle = _find_cycle_edge_indices(node_ids, [edges[i] for i in forward_indices])
        cycle = [forward_indices[i] for i in forward_cycle]
        if not cycle:
            break
        if strict:
            path = "；".join(f"{_edge_endpoints(edges[i])[0]} → {_edge_endpoints(edges[i])[1]} ({edges[i].get('condition') or 'always'})" for i in reversed(cycle))
            raise OrchestrationError(f"新流程存在未声明回边：{path}。请将实际的判定→入口返回边设为 condition=loop，并定义循环合同；未删除任何依赖。")

        self_loops = [
            index
            for index in cycle
            if _edge_endpoints(edges[index])[0] == _edge_endpoints(edges[index])[1]
        ]
        audit_routes = []
        for index in cycle:
            source, target = _edge_endpoints(edges[index])
            source_node = nodes_by_id.get(source) or {}
            target_node = nodes_by_id.get(target) or {}
            if source_node.get("kind") == "audit" and target_node.get("kind") == "implement":
                audit_routes.append(index)

        if self_loops:
            raise OrchestrationError("自环必须声明 condition=loop、可验证退出条件和循环预算")
        elif audit_routes:
            remove_index = max(
                audit_routes,
                key=lambda index: order.get(_edge_endpoints(edges[index])[0], 0)
                - order.get(_edge_endpoints(edges[index])[1], 0),
            )
            source, target = _edge_endpoints(edges[remove_index])
            audit_node = nodes_by_id[source]
            existing_target = str(audit_node.get("audit_target") or "").strip()
            existing_node = nodes_by_id.get(existing_target)
            if not existing_target or not existing_node or existing_node.get("kind") != "implement":
                audit_node["audit_target"] = target
            actual_target = str(audit_node.get("audit_target") or target)
            repairs.append(
                f"已将审核返工回边 {source} → {target} 改为 audit_target={actual_target}"
            )
        else:
            raise OrchestrationError("发现未声明的循环；请标记 condition=loop 回边并定义退出条件。未删除或改写任何依赖。")

        edges.pop(remove_index)

    if repairs:
        parsed["edges"] = edges
        meta = dict(parsed.get("meta") or {}) if isinstance(parsed.get("meta"), dict) else {}
        meta["extraction_repairs"] = repairs
        parsed["meta"] = meta
    return parsed, repairs


async def extract_orchestration_from_dialogue(
    client: LLMClient,
    messages: list[Any],
    *,
    bindings: dict[str, Any] | None = None,
    previous_spec: dict[str, Any] | None = None,
    revision: str = "",
    inventory: dict[str, Any] | None = None,
    progress: ExtractionProgress | None = None,
) -> dict[str, Any]:
    """Ask the current model for a stage-graph JSON, then normalize via propose."""
    normalized = coerce_dialogue_messages(messages)
    transcript = format_dialogue_transcript(normalized or messages)
    if not transcript:
        raise OrchestrationError("请先在聊天中与模型商量目标，再抽出阶段图")

    user_prompt = EXTRACT_USER_PREFIX + transcript
    if previous_spec:
        user_prompt += "\n\n最新已保存阶段图（当前编辑基线；不是待合并的历史方案）：\n" + json.dumps(
            previous_spec, ensure_ascii=False, indent=2
        )
    if inventory:
        user_prompt += "\n\n已确认的阶段目标盘点 JSON：\n" + json.dumps(
            inventory, ensure_ascii=False, indent=2
        )
    if revision.strip():
        user_prompt += "\n\n补充修订要求：\n" + revision.strip()

    collect_kwargs = {"progress": progress} if progress is not None else {}
    raw_text = await collect_model_text(
        client,
        system=EXTRACT_SYSTEM,
        user_text=user_prompt,
        **collect_kwargs,
    )
    if not raw_text:
        raise OrchestrationError("当前模型未返回阶段图内容，请重试或换模型后再抽")

    _emit_extraction_progress(
        progress,
        stage="parsing",
        detail="已收到模型输出，正在解析阶段节点与依赖。",
    )
    parsed = try_parse_planner_json(raw_text)
    if parsed is None:
        fallback = try_parse_json_object(raw_text)
        if isinstance(fallback, dict) and fallback.get("stages") and not fallback.get("nodes"):
            fallback["nodes"] = [
                {
                    "node_id": str(stage.get("stage_id") or f"stage_{index + 1}"),
                    "kind": "implement",
                    "label": str(stage.get("label") or stage.get("stage_id") or f"stage_{index + 1}"),
                    "goal": str(stage.get("goal") or stage.get("label") or ""),
                    "plan_brief": {
                        "summary": str(stage.get("summary") or stage.get("goal") or ""),
                        "known_inputs": stage.get("known_inputs") or [],
                        "open_questions": stage.get("open_questions") or [],
                        "steps": stage.get("steps") or [],
                        "outputs": stage.get("outputs") or [],
                        "acceptance": stage.get("acceptance") or [],
                    },
                }
                for index, stage in enumerate(fallback.get("stages") or [])
                if isinstance(stage, dict)
            ]
            parsed = fallback if fallback.get("nodes") else None
    if parsed is None:
        preview = raw_text[:240].replace("\n", " ")
        raise OrchestrationError(
            "当前模型返回的内容无法解析为阶段图 JSON。预览：" + preview
        )

    _emit_extraction_progress(
        progress,
        stage="normalizing",
        detail="正在补齐 Plan 简报并规范审核返工关系。",
    )
    parsed = _merge_inventory_into_nodes(parsed, inventory)
    previous_loops = {item.get("loop_id"): item for item in (previous_spec or {}).get("meta", {}).get("loops", [])}
    for loop in parsed.get("loops") or (parsed.get("meta") or {}).get("loops") or []:
        previous_loop = previous_loops.get(loop.get("loop_id"))
        loop.setdefault("control_mode", (previous_loop or {}).get("control_mode", "strict") if previous_loop else "team")
    parsed, extraction_repairs = repair_extracted_graph_cycles(parsed)
    goal = str(parsed.get("goal") or "").strip() or _goal_from_messages(normalized or messages)
    require_business_goal(goal, normalized or messages)
    # Never let a stale template_id collapse a rich node list back to a built-in template.
    parsed.pop("template_id", None)
    _emit_extraction_progress(
        progress,
        stage="validating",
        detail="正在执行 DAG 校验并生成控制计划。",
        repairs=len(extraction_repairs),
    )
    result = propose_orchestration(
        goal,
        bindings=bindings,
        raw_spec=parsed,
        previous_spec=previous_spec,
        revision=revision,
    )
    spec = _reattach_plan_briefs(
        OrchestrationSpec.from_value(result["spec"]),
        parsed,
        inventory,
    )
    spec = carry_over_user_edits(spec, _coerce_previous_spec(previous_spec))
    require_resolved_plan_inputs(spec)
    refreshed_inventory = inventory_from_spec(spec, inventory)
    if refreshed_inventory["stages"]:
        meta = dict(spec.meta or {})
        meta["inventory"] = refreshed_inventory
        spec = replace(spec, meta=meta)
    result["spec"] = spec.as_dict()
    session = result.get("plan_session")
    if isinstance(session, dict):
        result["plan_session"] = {**session, "spec": spec.as_dict()}
    result["clarify_prompt"] = build_clarify_prompt(spec)
    result["open_questions"] = collect_open_questions(spec)
    result["needs_clarify"] = bool(result["open_questions"])
    result["extractor_raw"] = raw_text
    result["inventory"] = refreshed_inventory or inventory
    result["normalization_warnings"] = extraction_repairs
    if extraction_repairs:
        result["warnings"] = extraction_repairs + list(result.get("warnings") or [])
        result["message"] = "已生成阶段图，并自动修复循环依赖；请检查后绑定模型开跑"
    else:
        result["message"] = "已从当前对话生成阶段图（含 Plan 简报），请绑定模型后确认开跑"
    result["transcript_chars"] = len(transcript)
    result["source_turns"] = len(normalized or messages)
    return result


def _coerce_previous_spec(value: Any) -> OrchestrationSpec | None:
    if value is None:
        return None
    if isinstance(value, OrchestrationSpec):
        return value
    if not isinstance(value, dict):
        return None
    raw = value if "nodes" in value else value.get("spec")
    if not isinstance(raw, dict):
        return None
    try:
        return OrchestrationSpec.from_value(raw)
    except (OrchestrationError, TypeError, ValueError):
        return None


def _reattach_plan_briefs(
    spec: OrchestrationSpec,
    parsed: dict[str, Any],
    inventory: dict[str, Any] | None,
) -> OrchestrationSpec:
    """Fill plan_brief from extractor JSON / inventory when propose omitted it."""
    brief_by_id: dict[str, Any] = {}
    brief_by_label: dict[str, Any] = {}
    for node in parsed.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        if node.get("plan_brief"):
            nid = str(node.get("node_id") or "")
            label = str(node.get("label") or "")
            if nid:
                brief_by_id[nid] = node.get("plan_brief")
            if label:
                brief_by_label[label] = node.get("plan_brief")
    if inventory and isinstance(inventory.get("stages"), list):
        for stage in inventory["stages"]:
            if not isinstance(stage, dict):
                continue
            payload = {
                "summary": stage.get("goal") or "",
                "known_inputs": stage.get("known_inputs") or [],
                "open_questions": stage.get("open_questions") or [],
            }
            sid = str(stage.get("stage_id") or "")
            label = str(stage.get("label") or "")
            if sid and sid not in brief_by_id:
                brief_by_id[sid] = payload
            if label and label not in brief_by_label:
                brief_by_label[label] = payload
    rebuilt = []
    for node in spec.nodes:
        brief = node.plan_brief or PlanBrief.from_value(
            brief_by_id.get(node.node_id) or brief_by_label.get(node.label)
        )
        if brief is None and (node.goal or node.label):
            brief = PlanBrief(summary=node.goal or node.label)
        rebuilt.append(replace(node, plan_brief=brief) if brief is not None else node)
    meta = dict(spec.meta or {})
    if inventory:
        meta["inventory"] = inventory
    return replace(spec, nodes=tuple(rebuilt), meta=meta)


_CARRIED_META_KEYS = ("pipeline_graph_id", "pipeline_graph_version", "session_id")


def carry_over_user_edits(
    spec: OrchestrationSpec,
    previous: OrchestrationSpec | None,
) -> OrchestrationSpec:
    """Restore hand-made canvas work that a regenerated graph cannot express.

    The planner returns stages without positions or canvas grouping, so a plain
    re-extract would reset the layout and orphan the canvas graph. Stages are
    matched by ``node_id`` first, then by a label that identifies exactly one
    previous stage.
    """
    if previous is None or not spec.nodes:
        return spec
    prev_by_id = previous.node_map()
    prev_by_label: dict[str, OrchestrationNode] = {}
    for node in previous.nodes:
        label = node.label.strip()
        if not label:
            continue
        if label in prev_by_label:
            prev_by_label[label] = None  # type: ignore[assignment]
        else:
            prev_by_label[label] = node
    prev_by_label = {k: v for k, v in prev_by_label.items() if v is not None}

    nodes: list[OrchestrationNode] = []
    old_to_new: dict[str, str] = {}
    claimed_old: set[str] = set()
    for node in spec.nodes:
        match = prev_by_id.get(node.node_id)
        if match is None or match.node_id in claimed_old:
            match = prev_by_label.get(node.label.strip())
        if match is None or match.node_id in claimed_old:
            nodes.append(node)
            continue
        claimed_old.add(match.node_id)
        old_to_new[match.node_id] = node.node_id
        updates: dict[str, Any] = {}
        if node.position is None and match.position is not None:
            updates["position"] = dict(match.position)
        if not node.prefers and match.prefers:
            updates["prefers"] = match.prefers
        nodes.append(replace(node, **updates) if updates else node)

    live_ids = {node.node_id for node in nodes}
    meta = dict(spec.meta or {})
    prev_meta = dict(previous.meta or {})
    for key in _CARRIED_META_KEYS:
        if not meta.get(key) and prev_meta.get(key):
            meta[key] = prev_meta[key]
    groups = prev_meta.get("canvas_groups")
    if isinstance(groups, dict) and "canvas_groups" not in meta:
        kept: dict[str, Any] = {}
        for group_id, info in groups.items():
            if not isinstance(info, dict):
                continue
            new_id = old_to_new.get(str(group_id), str(group_id) if str(group_id) in live_ids else "")
            if not new_id:
                continue
            children = info.get("children") if isinstance(info.get("children"), list) else []
            kept[new_id] = {
                **info,
                "children": [
                    old_to_new.get(str(c), str(c))
                    for c in children
                    if old_to_new.get(str(c), str(c)) in live_ids
                ],
            }
        if kept:
            meta["canvas_groups"] = kept
    spec_id = spec.spec_id
    if (not spec_id or spec_id == "orchestration") and previous.spec_id:
        spec_id = previous.spec_id
    return replace(spec, nodes=tuple(nodes), meta=meta, spec_id=spec_id)


def overlay_plan_briefs(spec: OrchestrationSpec, source: OrchestrationSpec | None) -> OrchestrationSpec:
    """Copy plan_brief from a richer spec (e.g. plan session) onto matching nodes."""
    if source is None:
        return spec
    src = source.node_map()
    nodes = []
    for node in spec.nodes:
        prev = src.get(node.node_id)
        if prev is not None and prev.plan_brief is not None and node.plan_brief is None:
            nodes.append(replace(node, plan_brief=prev.plan_brief))
        else:
            nodes.append(node)
    goal = spec.goal or source.goal
    return replace(spec, nodes=tuple(nodes), goal=goal)


def dialogue_has_user_turn(messages: list[Any]) -> bool:
    for message in coerce_dialogue_messages(messages) or messages:
        if str(getattr(message, "role", "") or "") != "user":
            continue
        if str(getattr(message, "content", "") or "").strip():
            return True
    return False
