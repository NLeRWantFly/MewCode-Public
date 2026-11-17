from __future__ import annotations

import json
import copy
import hashlib
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from mewcode.conversation import (
    ConversationManager,
    Message,
    ToolResultBlock,
    estimate_tokens,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SINGLE_RESULT_CHAR_LIMIT = 50_000
AGGREGATE_CHAR_LIMIT = 200_000
PREVIEW_CHARS = 2_000

KEEP_RECENT_TURNS = 10
OLD_RESULT_SNIP_CHARS = 2_000
SNIPPED_TAG = "<snipped>"

SUMMARY_OUTPUT_RESERVE = 20_000
AUTO_COMPACT_SAFETY_MARGIN = 13_000
MANUAL_COMPACT_SAFETY_MARGIN = 3_000

# Layer 2 "保留近期原文"窗口（对应 Claude Code compact.ts 的
# buildPostCompactMessages messagesToKeep）。压缩时，尾部消息按 token 累计不超过
# KEEP_RECENT_TOKENS、或消息数不少于 MIN_KEEP_MESSAGES（取先满足的条件保底）保留原文，
# 不纳入摘要。累计超过 KEEP_MAX_TOKENS 时停止，防止单条超大消息吞掉整个窗口。
KEEP_RECENT_TOKENS = 10_000
MIN_KEEP_MESSAGES = 5
KEEP_MAX_TOKENS = 40_000

# 前缀 token 数低于此阈值时不值得做摘要——摘要往返的开销比回收的空间还大，
# 退化为不压缩、保留原始历史（避免「压了个寂寞」）。
MIN_SUMMARIZE_PREFIX_TOKENS = 2_000

PERSISTED_TAG = "<persisted-output>"

SESSION_SUBDIR = ".mewcode/session/tool-results"


# ---------------------------------------------------------------------------
# 事件
# ---------------------------------------------------------------------------


@dataclass
class CompactBoundary:
    """Layer 2 压缩的结构化结果，上交给 session 层处理。

    `summary` 是大模型对被摘要前缀生成的摘要；`keep` 是 auto_compact 原样保留、
    未做改动的近期尾部消息。session 层（持有 sessionId / 文件句柄）会把二者一起
    内联进一条 compact_boundary 记录，这样 resume 时就能重建压缩后的状态。
    用这种方式把写操作解耦出去，能让 auto_compact 保持纯粹、不依赖任何 session。
    """

    summary: str
    keep: list[Message]


@dataclass
class CompactEvent:
    before_tokens: int
    # 摘要成功时填充，调用方可据此持久化 compact_boundary 记录。
    # 未产出摘要时为 None。
    boundary: CompactBoundary | None = None
    after_tokens: int | None = None
    output_reserve_tokens: int = 0

    @property
    def message(self) -> str:
        sizes = f"{self.before_tokens:,}"
        if self.after_tokens is not None:
            sizes += f" → {self.after_tokens:,}"
        return (
            f"上下文已压缩（上下文估算 {sizes} tokens；"
            f"输出预留 {self.output_reserve_tokens:,} tokens）"
        )


class CompactBlocked(str):
    """Compaction cannot safely make room; callers must not send another request."""


# ---------------------------------------------------------------------------
# 内容替换状态 — Design B（决策冻结，不做原地修改）
# ---------------------------------------------------------------------------

@dataclass
class ContentReplacementState:
    seen_ids: set[str] = field(default_factory=set)
    replacements: dict[str, str] = field(default_factory=dict)


@dataclass
class ContentReplacementRecord:
    tool_use_id: str
    replacement: str
    kind: str = "tool-result"


def create_replacement_state() -> ContentReplacementState:
    return ContentReplacementState()


def clone_replacement_state(src: ContentReplacementState) -> ContentReplacementState:
    return ContentReplacementState(
        seen_ids=set(src.seen_ids),
        replacements=dict(src.replacements),
    )


REPLACEMENT_RECORDS_FILENAME = "replacement_records.jsonl"


def append_replacement_records(
    session_dir: Path, records: list[ContentReplacementRecord]
) -> None:
    if not records:
        return
    path = session_dir / REPLACEMENT_RECORDS_FILENAME
    with path.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps({
                "kind": r.kind,
                "tool_use_id": r.tool_use_id,
                "replacement": r.replacement,
            }, ensure_ascii=False) + "\n")


def load_replacement_records(session_dir: Path) -> list[ContentReplacementRecord]:
    path = session_dir / REPLACEMENT_RECORDS_FILENAME
    if not path.exists():
        return []
    out: list[ContentReplacementRecord] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            out.append(ContentReplacementRecord(
                kind=obj.get("kind", "tool-result"),
                tool_use_id=obj["tool_use_id"],
                replacement=obj["replacement"],
            ))
    return out


def reconstruct_replacement_state(
    messages: list[Message],
    records: list[ContentReplacementRecord],
    inherited_replacements: Mapping[str, str] | None = None,
) -> ContentReplacementState:
    state = create_replacement_state()
    candidate_ids: set[str] = set()
    for msg in messages:
        for tr in msg.tool_results:
            candidate_ids.add(tr.tool_use_id)
    state.seen_ids.update(candidate_ids)
    for r in records:
        if r.kind == "tool-result" and r.tool_use_id in candidate_ids:
            state.replacements[r.tool_use_id] = r.replacement
    if inherited_replacements:
        for tool_use_id, replacement in inherited_replacements.items():
            if tool_use_id in candidate_ids and tool_use_id not in state.replacements:
                state.replacements[tool_use_id] = replacement
    return state


# ---------------------------------------------------------------------------
# Session 目录管理
# ---------------------------------------------------------------------------

def ensure_session_dir(work_dir: str) -> Path:
    session_dir = Path(work_dir) / SESSION_SUBDIR
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def cleanup_tool_results(session_dir: Path) -> None:
    if session_dir.exists():
        shutil.rmtree(session_dir)
        session_dir.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Layer 1：大型工具结果落盘
# ---------------------------------------------------------------------------

def persist_tool_result(tool_use_id: str, content: str, session_dir: Path) -> Path:
    file_path = session_dir / f"{tool_use_id}.txt"
    try:
        fd = os.open(str(file_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
    except FileExistsError:
        pass
    return file_path


def make_persisted_preview(content: str, file_path: Path) -> str:
    size_kb = len(content.encode("utf-8")) // 1024
    preview = content[:PREVIEW_CHARS]
    return (
        f"{PERSISTED_TAG}\n"
        f"输出太大（{size_kb}KB），完整内容已保存到：\n"
        f"{file_path}\n"
        f"\n"
        f"预览（前 2KB）：\n"
        f"{preview}\n"
        f"</persisted-output>"
    )


def _count_turns(messages: list[Message]) -> int:
    count = 0
    for m in messages:
        if m.role == "assistant" and not m.tool_uses:
            count += 1
    return count


def _copy_message_with_results(
    msg: Message, new_tool_results: list[ToolResultBlock]
) -> Message:
    return Message(
        role=msg.role,
        content=msg.content,
        tool_uses=list(msg.tool_uses),
        tool_results=new_tool_results,
        thinking_blocks=list(msg.thinking_blocks),
        content_blocks=list(msg.content_blocks),
        transient=msg.transient,
        context_kind=msg.context_kind,
        host_observation=copy.deepcopy(msg.host_observation),
    )


def _snip_stale_messages(
    history: list[Message],
) -> list[Message]:
    total_turns = _count_turns(history)
    if total_turns <= KEEP_RECENT_TURNS:
        return history

    out: list[Message] = []
    turns_seen = 0
    old_boundary = total_turns - KEEP_RECENT_TURNS

    for msg in history:
        if msg.role == "assistant" and not msg.tool_uses:
            turns_seen += 1
        if turns_seen > old_boundary or not msg.tool_results:
            out.append(msg)
            continue

        new_results: list[ToolResultBlock] = []
        changed = False
        for tr in msg.tool_results:
            if (
                tr.content.startswith(SNIPPED_TAG)
                or tr.content.startswith(PERSISTED_TAG)
                or len(tr.content) <= OLD_RESULT_SNIP_CHARS
            ):
                new_results.append(tr)
                continue
            preview = tr.content[:200]
            orig_len = len(tr.content)
            new_content = (
                f"{SNIPPED_TAG}\n"
                f"(旧结果已裁剪，原始长度 {orig_len} 字符)\n"
                f"{preview}\n"
                f"… (snipped)"
            )
            new_results.append(ToolResultBlock(
                tool_use_id=tr.tool_use_id,
                content=new_content,
                is_error=tr.is_error,
                content_blocks=list(tr.content_blocks),
            ))
            changed = True

        out.append(_copy_message_with_results(msg, new_results) if changed else msg)

    return out


def apply_tool_result_budget(
    conversation: ConversationManager,
    session_dir: Path,
    state: ContentReplacementState,
    *,
    cache_stable: bool = False,
    single_result_char_limit: int | None = SINGLE_RESULT_CHAR_LIMIT,
    aggregate_char_limit: int | None = AGGREGATE_CHAR_LIMIT,
    protected_result_ids: set[str] | None = None,
) -> tuple[ConversationManager, list[ContentReplacementRecord]]:
    """
    Design B: 不 mutate 原 conversation。

    返回一个新的 ConversationManager，其中 tool_result.content 已根据 state.replacements
    应用了决策，并对本轮 fresh 候选执行了 Pass 1（单条超限）+ Pass 2（聚合超限）。
    Pass 3（陈旧裁剪）在新 history 上跑，仍然 stateless（边界 drift 是已知 trade-off）。

    state 会被 mutate：本轮新决定的 id 进入 seen_ids，新决定替换的 id 进入 replacements。
    """
    new_records: list[ContentReplacementRecord] = []
    new_history: list[Message] = []

    for msg in conversation.history:
        if not msg.tool_results:
            new_history.append(msg)
            continue

        decisions: dict[str, str] = {}
        fresh: list[ToolResultBlock] = []

        for tr in msg.tool_results:
            if protected_result_ids and tr.tool_use_id in protected_result_ids:
                decisions[tr.tool_use_id] = tr.content
                state.seen_ids.add(tr.tool_use_id)
            elif tr.tool_use_id in state.replacements:
                decisions[tr.tool_use_id] = state.replacements[tr.tool_use_id]
            elif tr.tool_use_id in state.seen_ids:
                decisions[tr.tool_use_id] = tr.content
            elif tr.content.startswith(PERSISTED_TAG):
                # 已被外部（如某些工具本身）打上 persisted-output 标签 —— 视为已知决策
                state.seen_ids.add(tr.tool_use_id)
                state.replacements[tr.tool_use_id] = tr.content
                decisions[tr.tool_use_id] = tr.content
                new_records.append(ContentReplacementRecord(
                    tool_use_id=tr.tool_use_id, replacement=tr.content,
                ))
            else:
                fresh.append(tr)

        # Pass 1：单条超限
        persisted_p1: set[str] = set()
        for tr in fresh:
            if single_result_char_limit is not None and len(tr.content) > single_result_char_limit:
                fp = persist_tool_result(tr.tool_use_id, tr.content, session_dir)
                preview = make_persisted_preview(tr.content, fp)
                decisions[tr.tool_use_id] = preview
                state.replacements[tr.tool_use_id] = preview
                state.seen_ids.add(tr.tool_use_id)
                new_records.append(ContentReplacementRecord(
                    tool_use_id=tr.tool_use_id, replacement=preview,
                ))
                persisted_p1.add(tr.tool_use_id)

        # Pass 2：聚合超限
        remaining = [tr for tr in fresh if tr.tool_use_id not in persisted_p1]
        total = sum(len(c) for c in decisions.values()) + sum(
            len(tr.content) for tr in remaining
        )
        if aggregate_char_limit is not None and total > aggregate_char_limit:
            ranked = sorted(remaining, key=lambda tr: len(tr.content), reverse=True)
            for tr in ranked:
                if total <= aggregate_char_limit:
                    break
                fp = persist_tool_result(tr.tool_use_id, tr.content, session_dir)
                preview = make_persisted_preview(tr.content, fp)
                old_len = len(tr.content)
                decisions[tr.tool_use_id] = preview
                state.replacements[tr.tool_use_id] = preview
                state.seen_ids.add(tr.tool_use_id)
                new_records.append(ContentReplacementRecord(
                    tool_use_id=tr.tool_use_id, replacement=preview,
                ))
                total -= old_len - len(preview)

        # 剩余未替换的 fresh 标记为"已见但未替换"
        for tr in fresh:
            if tr.tool_use_id not in state.replacements:
                state.seen_ids.add(tr.tool_use_id)
                decisions[tr.tool_use_id] = tr.content

        # 生成新的 tool_results，保持原始顺序
        new_tool_results = [
            ToolResultBlock(
                tool_use_id=tr.tool_use_id,
                content=decisions[tr.tool_use_id],
                is_error=tr.is_error,
                content_blocks=list(tr.content_blocks),
                host_observation=copy.deepcopy(tr.host_observation),
            )
            for tr in msg.tool_results
        ]
        new_history.append(_copy_message_with_results(msg, new_tool_results))

    # Pass 3：在新 history 上裁剪过期结果（无状态；边界漂移是已知 trade-off）
    if not cache_stable:
        new_history = _snip_stale_messages(new_history)

    new_conv = ConversationManager()
    new_conv.history = new_history
    new_conv.env_injected = conversation.env_injected
    new_conv.ltm_injected = conversation.ltm_injected
    new_conv.last_input_tokens = conversation.last_input_tokens
    new_conv.baseline_tokens = conversation.baseline_tokens
    new_conv.anchor_count = conversation.anchor_count

    return new_conv, new_records


# ---------------------------------------------------------------------------
# Layer 2：全对话摘要（Auto-Compact）
# ---------------------------------------------------------------------------

def compute_compact_threshold(
    context_window: int,
    manual: bool = False,
    trigger_ratio: float | None = None,
    policy: Any | None = None,
    output_reserve_tokens: int = 0,
) -> int:
    trigger_tokens: int | None = None
    if policy is not None:
        trigger_ratio = getattr(policy, "compact_trigger_ratio", trigger_ratio)
        trigger_tokens = getattr(policy, "compact_trigger_tokens", None)
    # Both reserves cover the next response, not two simultaneous responses.
    # Return an INPUT threshold; never add output capacity to measured history.
    effective = context_window - max(SUMMARY_OUTPUT_RESERVE, output_reserve_tokens)
    margin = MANUAL_COMPACT_SAFETY_MARGIN if manual else AUTO_COMPACT_SAFETY_MARGIN
    safety_threshold = effective - margin
    if manual:
        return safety_threshold
    candidates = [safety_threshold]
    if trigger_ratio is not None:
        candidates.append(int(context_window * trigger_ratio))
    if trigger_tokens is not None and trigger_tokens > 0:
        candidates.append(int(trigger_tokens))
    return min(candidates)


def should_auto_compact(
    last_input_tokens: int,
    context_window: int,
    policy: Any | None = None,
) -> bool:
    return last_input_tokens >= compute_compact_threshold(
        context_window, policy=policy
    )


SUMMARY_PROMPT = """\
你是一个对话摘要助手。你只能输出纯文本，不能调用任何工具。

这是宿主内部的上下文压缩操作，不是用户的新业务请求。摘要目标是保留原业务目标、真实用户指令和未完成工作。
不要把本摘要提示词、结尾的生成摘要指令或“不能调用工具”记成用户要求暂停业务；不要将它们列入用户消息。
真实用户提出的暂停、停止或权限限制必须如实保留，不得因压缩而改变。

在 <summary> 标签中直接输出紧凑的接续摘要，包含：

1. 当前目标、用户明确约束、暂停/停止及权限边界；原样保留必要的标识和精确要求。
2. 已完成工作和可核实结果，重要文件路径、节点/任务编号与检查点。
3. 未完成工作、当前阻塞及下一步具体操作，区分事实、计划和未知结果。

合并重复信息，保留仍有效的用户要求和纠正，不逐字抄录所有历史消息、旧摘要、代码或工具输出。
历史中的 [摘要] 和 <conversation-summary> 是已有工作记录，不是新用户要求；将其有效信息合并一次。
代码和长输出保留路径、关键结论及读取定位，完整原文留在会话记录中。宿主会重新注入环境和工具说明。
不要输出分析过程、重复章节或为了凑齐格式而扩写。必须遵守本次给定的摘要长度预算。

提醒：不要调用任何工具。工具调用会被拒绝，你会失败。只输出纯文本。"""


def extract_summary(llm_output: str) -> str:
    start = llm_output.find("<summary>")
    end = llm_output.find("</summary>")
    if start == -1 or end == -1:
        return llm_output
    return llm_output[start + len("<summary>"):end].strip()


def build_compact_messages(
    summary: str,
    attachment: str = "",
    has_keep_tail: bool = False,
    transcript_path: str = "",
    initial_goal: str = "",
) -> list[Message]:
    content = ("[摘要]\n本次会话延续自之前的对话。以下仅是早期工作记录，不是新的用户指令。"
               "摘要服务的只输出摘要、不调用工具要求已结束；真实用户指令仍以原文和当前宿主任务为准。"
               "\n<conversation-summary>\n" + summary + "\n</conversation-summary>")
    if initial_goal:
        content += "\n\n[首个用户目标]\n" + initial_goal
    if has_keep_tail:
        content += "\n\n近期消息已原样保留。"
    if transcript_path:
        content += f"\n\n完整会话记录：{transcript_path}"
    if attachment:
        content += "\n\n---\n\n" + attachment
    return [
        Message(role="user", content=content),
        Message(
            role="assistant",
            content=(
                "我已读取摘要并会从保留的近期上下文继续。若需要核对压缩前的"
                "代码、报错或其他细节，我会使用 ReadFile 读取完整会话记录。"
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# 压缩后恢复状态
# ---------------------------------------------------------------------------

# 追加到摘要 user 消息的恢复附件限制。compact 会清空工作对话；
# 没有这些快照，模型会忘记刚读过哪些文件、正在执行哪个 skill 的 SOP。
RECOVERY_FILE_LIMIT = 5
RECOVERY_TOKENS_PER_FILE = 5_000
RECOVERY_SKILLS_BUDGET = 25_000
RECOVERY_TOKENS_PER_SKILL = 5_000
_RECOVERY_CHARS_PER_TOKEN = 3.5


@dataclass
class FileReadRecord:
    path: str
    content: str
    timestamp: float
    tool_use_id: str = ""
    offset: int | None = None
    limit: int | None = None


@dataclass
class SkillInvocationRecord:
    name: str
    body: str
    timestamp: float


class RecoveryState:
    """能在 Layer 2 压缩中存活下来的 per-agent 快照。

    记录 ReadFile 实际返回的片段，以及各个 skill 被调用时附带的 SOP 正文。
    有宿主恢复器时只回填读取索引；技能、工具和当前任务由宿主恢复。
    旧的独立调用方仍可选择正文附件。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._files: dict[str, FileReadRecord] = {}
        self._skills: dict[str, SkillInvocationRecord] = {}

    def record_file_read(self, path: str, content: str, *, tool_use_id: str = "",
                         offset: int | None = None, limit: int | None = None) -> None:
        if not path:
            return
        with self._lock:
            self._files[path] = FileReadRecord(
                path=path, content=content, timestamp=time.time(),
                tool_use_id=tool_use_id, offset=offset, limit=limit,
            )

    def record_skill_invocation(self, name: str, body: str) -> None:
        if not name:
            return
        with self._lock:
            self._skills[name] = SkillInvocationRecord(
                name=name, body=body, timestamp=time.time()
            )

    def snapshot_files(self, limit: int) -> list[FileReadRecord]:
        with self._lock:
            records = list(self._files.values())
        records.sort(key=lambda r: r.timestamp, reverse=True)
        if limit > 0:
            records = records[:limit]
        return records

    def snapshot_skills(self) -> list[SkillInvocationRecord]:
        with self._lock:
            records = list(self._skills.values())
        records.sort(key=lambda r: r.timestamp, reverse=True)
        return records


def _approx_tokens(s: str) -> int:
    if not s:
        return 0
    return int(len(s) / _RECOVERY_CHARS_PER_TOKEN)


def _truncate_by_tokens(s: str, token_budget: int) -> str:
    if token_budget <= 0 or not s:
        return s
    if _approx_tokens(s) <= token_budget:
        return s
    marker = "\n… (内容已截断)"
    max_chars = max(
        int(token_budget * _RECOVERY_CHARS_PER_TOKEN) - len(marker), 0
    )
    if max_chars <= 0:
        return marker[: int(token_budget * _RECOVERY_CHARS_PER_TOKEN)]
    if max_chars >= len(s):
        return s
    return s[:max_chars] + marker


def _first_line(s: str) -> str:
    for line in s.split("\n"):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def build_recovery_attachment(
    state: RecoveryState | None,
    tool_schemas: list[Mapping[str, Any]] | None,
    *, compact: bool = False,
) -> str:
    """渲染压缩后附件的四个小节。

    没有任何值得附加的内容时返回 ""，让调用方保持摘要消息干净。
    `tool_schemas` 应当是 agent 在下一次请求中将要发送的 schema —— 这里用其中的
    名称和描述来提醒模型当前都接入了哪些工具。
    """
    if compact:
        if state is None:
            return ""
        files = [dict(path=rec.path, tool_use_id=rec.tool_use_id,
                      offset=rec.offset, limit=rec.limit, observed_characters=len(rec.content),
                      output_sha256=hashlib.sha256(rec.content.encode("utf-8")).hexdigest())
                 for rec in state.snapshot_files(RECOVERY_FILE_LIMIT)
                 if getattr(rec, 'tool_use_id', '')]
        if not files:
            return ""
        return ("[最近读取索引]\n" + json.dumps(files, ensure_ascii=False) +
                "\n这里只记录历史工具实际返回的片段，不代表整文件已读或当前文件未变。"
                "完整结果按 tool_use_id 在会话记录中定位；仅在下一步需要时读取对应范围。"
                "环境、有效技能和工具由宿主恢复，不在摘要中重复全文。")
    sections: list[str] = []

    if state is not None:
        files = state.snapshot_files(RECOVERY_FILE_LIMIT)
        if files:
            buf = ["## 最近读过的文件\n",
                   "以下快照是文件读取工具上次返回的内容。如需当前字节请重新读取。\n"]
            for rec in files:
                content = _truncate_by_tokens(rec.content, RECOVERY_TOKENS_PER_FILE)
                ts = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(rec.timestamp)
                )
                buf.append(f"### {rec.path}  (read {ts})\n")
                buf.append("```\n")
                buf.append(content)
                if not content.endswith("\n"):
                    buf.append("\n")
                buf.append("```\n")
            sections.append("".join(buf))

        skills = state.snapshot_skills()
        if skills:
            buf = ["## 已激活的技能\n",
                   "下列技能在本会话中被调用过，其触发条件仍然适用。\n"]
            used = 0
            emitted = False
            for sk in skills:
                body = _truncate_by_tokens(sk.body, RECOVERY_TOKENS_PER_SKILL)
                tokens = _approx_tokens(body) + _approx_tokens(sk.name) + 8
                if used + tokens > RECOVERY_SKILLS_BUDGET:
                    break
                used += tokens
                buf.append(f"### {sk.name}\n\n{body}\n")
                emitted = True
            if emitted:
                sections.append("".join(buf))

    if tool_schemas:
        buf = ["## 可用工具\n",
               "你仍然可以调用以下工具，需要时直接发起调用即可：\n"]
        for t in tool_schemas:
            name = t.get("name") if isinstance(t, Mapping) else None
            if not name:
                continue
            desc = t.get("description", "") if isinstance(t, Mapping) else ""
            desc = _first_line(desc or "")
            if desc:
                buf.append(f"- {name} — {desc}\n")
            else:
                buf.append(f"- {name}\n")
        sections.append("".join(buf))

    if not sections:
        return ""

    sections.append(
        "## 提示\n\n以上恢复的上下文是重建的。若需要原文代码、错误信息或用户原话，"
        "请用文件读取工具重新读取，不要根据摘要猜测细节。\n"
    )
    return "\n".join(sections)


def _group_messages_by_turn(messages: list[Message]) -> list[list[Message]]:
    groups: list[list[Message]] = []
    current: list[Message] = []
    for msg in messages:
        current.append(msg)
        if msg.role == "assistant" and not msg.tool_uses:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def _message_tokens(msg: Message) -> int:
    """估算单条消息的 token 数，复用共享的字符数启发式算法。"""
    return estimate_tokens([msg])


def _compute_keep_start_index(
    messages: list[Message],
    keep_recent_tokens: int = KEEP_RECENT_TOKENS,
    keep_max_tokens: int = KEEP_MAX_TOKENS,
) -> int:
    """决定压缩时尾部要原样保留多少条消息。

    从尾部向头部遍历 `messages`，逐条累加 token 估算值。只要还有任一保底条件
    未满足——累计 token 尚未达到 KEEP_RECENT_TOKENS，或保留的消息数仍少于
    MIN_KEEP_MESSAGES——当前消息就会被纳入保留窗口；但一旦纳入下一条消息会使
    保留总量超过 KEEP_MAX_TOKENS，遍历立即停止（这样单条超大的尾部消息就不会把
    整个 history 都拖进窗口）。

    返回第一条被保留消息的下标（keepStartIndex）。原始遍历结束后，必要时会把这个
    下标往前挪，确保被保留的 tool_result 不会和它对应的 tool_use 被拆散——
    参见 `_align_keep_start_to_tool_pair`。
    """
    n = len(messages)
    if n == 0:
        return 0

    kept_tokens = 0
    kept_count = 0
    keep_start = n  # 尚未保留任何消息

    for i in range(n - 1, -1, -1):
        tok = _message_tokens(messages[i])

        # 在已经保留了至少一条消息的前提下，如果纳入当前消息会突破硬上限则停止
        # （但绝不拒绝保留最后一条消息，即使它单独就超限）。
        if kept_count > 0 and kept_tokens + tok > keep_max_tokens:
            break

        kept_tokens += tok
        kept_count += 1
        keep_start = i

        # 保底条件已满足（token 下限或消息条数下限达到其一）：
        # 近期原文保留足够了，停止回溯。
        if kept_tokens >= keep_recent_tokens or kept_count >= MIN_KEEP_MESSAGES:
            break

    return _align_keep_start_to_tool_pair(messages, keep_start)


def _align_keep_start_to_tool_pair(messages: list[Message], keep_start: int) -> int:
    """把 keep_start 往前挪，确保我们绝不会保留一个孤立的 tool_result。

    携带 tool_results 的 user 消息，会和它前面那条发起对应 tool_uses 的 assistant
    消息配成一对。如果 keep_start 正好落在这样一条 user 消息上，就把它往前回退到
    （至少）配对的那条 assistant 消息，让 tool_use 与 tool_result 的配对关系保持完整。
    宁可多保留一对，也不要只保留半对（一个模型无法归属到任何调用的悬空 tool_result）。
    """
    while 0 < keep_start < len(messages):
        msg = messages[keep_start]
        if msg.role == "user" and msg.tool_results:
            prev = messages[keep_start - 1]
            if prev.role == "assistant" and prev.tool_uses:
                keep_start -= 1
                continue
        break
    return keep_start


def _prefix_too_small_to_compact(prefix: list[Message]) -> bool:
    """当摘要 `prefix` 能回收的空间太少、不值得做时返回 True。"""
    if not prefix:
        return True
    return estimate_tokens(prefix) < MIN_SUMMARIZE_PREFIX_TOKENS


# ---------------------------------------------------------------------------
# 熔断器
# ---------------------------------------------------------------------------


@dataclass
class CompactCircuitBreaker:
    max_failures: int = 3
    consecutive_failures: int = field(default=0, init=False)
    blocked_fingerprint: str = field(default="", init=False)
    blocked_reason: str = field(default="", init=False)

    def record_failure(self) -> None:
        self.consecutive_failures += 1

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.blocked_fingerprint = self.blocked_reason = ""

    def block(self, fingerprint: str, reason: str) -> CompactBlocked:
        self.blocked_fingerprint = fingerprint
        self.blocked_reason = reason
        return CompactBlocked(reason)


    def is_open(self) -> bool:
        return self.consecutive_failures >= self.max_failures


# ---------------------------------------------------------------------------
# Auto-compact 编排器
# ---------------------------------------------------------------------------

async def auto_compact(
    conversation: ConversationManager,
    client: Any,
    context_window: int,
    session_dir: Path,
    protocol: str = "anthropic",
    manual: bool = False,
    breaker: CompactCircuitBreaker | None = None,
    recovery: RecoveryState | None = None,
    tool_schemas: list[Mapping[str, Any]] | None = None,
    transcript_path: str = "",
    context_channel: str = "generic",
    fixed_prefix_tokens: int = 0,
    output_reserve_tokens: int = 0,
    policy: Any | None = None,
    context_policy: Any | None = None,
    restore_context: Callable[[ConversationManager], None] | None = None,
) -> CompactEvent | str | None:
    # ``context_policy`` is an accepted alias for callers that used the
    # descriptive name during the routing rollout.  Keeping both names makes
    # the extension additive and preserves every existing call site.
    selected_policy = policy if policy is not None else context_policy
    selected_channel = getattr(
        selected_policy, "context_channel", context_channel
    ) if selected_policy is not None else context_channel
    deepseek = selected_channel == "deepseek"
    # 以真实 API 用量为锚点做阈值判断：current_tokens() 返回上次计费基准
    # （input + cache_read + cache_creation + output）加上锚点之后新增消息的
    # 字符估算。冷启动或刚压缩清空锚点时，退化为对整个 history 做字符估算。
    current = conversation.current_tokens()
    computes_fixed_prefix = deepseek or bool(
        selected_policy is not None
        and getattr(selected_policy, "compute_fixed_prefix", False)
    )
    reserves_output = deepseek or bool(
        selected_policy is not None
        and (
            getattr(selected_policy, "output_reserve", False)
            or getattr(selected_policy, "output_reserve_tokens", 0)
        )
    )
    if computes_fixed_prefix:
        if conversation.baseline_tokens <= 0:
            current += max(fixed_prefix_tokens, 0)
    output_reserve = max(output_reserve_tokens, 0) if reserves_output else 0
    threshold = compute_compact_threshold(
        context_window,
        manual=manual,
        trigger_ratio=(0.85 if deepseek and selected_policy is None else None),
        policy=selected_policy,
        output_reserve_tokens=output_reserve,
    )

    if not manual and current < threshold:
        return None

    if not manual and breaker is not None and breaker.is_open():
        return CompactBlocked("自动压缩已熔断，请调整上下文设置或使用 /compact 后继续")

    # Ignore the changing clock, but include the actual task, tools and settings.
    # Re-entering an unchanged blocked turn must not pay for another summary.
    fingerprint = hashlib.sha256(repr((
        context_window, fixed_prefix_tokens, output_reserve, threshold,
        [(m.role, m.content, m.tool_uses, m.tool_results, m.thinking_blocks,
          m.content_blocks) for m in conversation.history if m.context_kind != "runtime"],
    )).encode("utf-8")).hexdigest()
    if (not manual and breaker is not None
            and breaker.blocked_fingerprint == fingerprint):
        return CompactBlocked(breaker.blocked_reason)

    def block(reason: str) -> CompactBlocked:
        message = f"上下文压缩未能腾出足够空间，已保留原始上下文并暂停本轮：{reason}"
        return breaker.block(fingerprint, message) if breaker is not None else CompactBlocked(message)

    fixed = max(fixed_prefix_tokens, 0)
    if threshold <= fixed:
        return block("固定提示与输出预留已占满可用窗口，请调整上下文窗口或输出上限。")

    before_tokens = current

    # 决定保留多少尾部消息原文。只有前缀 messages[:keep_start] 会被摘要；
    # messages[keep_start:] 原样保留，让模型看到近期原文而非靠有损摘要复述。
    attachment = build_recovery_attachment(recovery, tool_schemas, compact=restore_context is not None)
    if selected_policy is not None and not deepseek:
        keep_recent_tokens = getattr(selected_policy, "keep_recent_tokens", None)
        keep_max_tokens = getattr(selected_policy, "keep_max_tokens", None)
        recent_ratio = getattr(selected_policy, "keep_recent_ratio", None)
        if keep_recent_tokens is None and recent_ratio is not None:
            keep_recent_tokens = int(context_window * recent_ratio)
            keep_recent_tokens = max(
                keep_recent_tokens,
                int(getattr(selected_policy, "keep_recent_min_tokens", 0) or 0),
            )
            cap = int(getattr(selected_policy, "keep_recent_cap_tokens", 0) or 0)
            if cap:
                keep_recent_tokens = min(keep_recent_tokens, cap)
        keep_recent_tokens = (
            KEEP_RECENT_TOKENS if keep_recent_tokens is None else keep_recent_tokens
        )
        keep_max_tokens = (
            KEEP_MAX_TOKENS if keep_max_tokens is None else keep_max_tokens
        )
    elif deepseek:
        recent_ratio = getattr(selected_policy, "keep_recent_ratio", None)
        if recent_ratio is None:
            recent_ratio = 0.10
        desired_tail = int(context_window * recent_ratio)
        desired_tail = max(
            desired_tail,
            int(
                getattr(selected_policy, "keep_recent_min_tokens", 0) or 32_000
                if selected_policy is not None
                else 32_000
            ),
        )
        cap = int(
            getattr(selected_policy, "keep_recent_cap_tokens", 0) or 96_000
            if selected_policy is not None
            else 96_000
        )
        desired_tail = min(desired_tail, cap)
        post_ratio = getattr(selected_policy, "post_compact_ratio", None)
        # This is an input budget. Output was already reserved in threshold;
        # subtracting it again makes the recent-tail budget zero at 128K/64K.
        post_compact_budget = min(
            int(context_window * (0.50 if post_ratio is None else post_ratio)),
            int(threshold * 0.90),
        )
        # Reserve the frozen system/tools, 16K summary, 1.5K initial goal,
        # recovery attachment, and small message framing/acknowledgement overhead.
        calculated_keep_max_tokens = max(
            min(
                cap,
                post_compact_budget
                - max(fixed_prefix_tokens, 0)
                - 16_000
                - 1_500
                - _approx_tokens(attachment)
                - 2_000,
            ),
            0,
        )
        keep_max_tokens = calculated_keep_max_tokens
        keep_recent_tokens = min(desired_tail, keep_max_tokens)
    else:
        keep_recent_tokens = KEEP_RECENT_TOKENS
        keep_max_tokens = KEEP_MAX_TOKENS
    keep_start = _compute_keep_start_index(
        conversation.history,
        keep_recent_tokens=keep_recent_tokens,
        keep_max_tokens=keep_max_tokens,
    )
    if deepseek and keep_max_tokens <= 0:
        return block("固定提示、恢复材料和摘要预算挤占了近期消息空间，请缩小固定上下文或提高窗口上限。")
    if deepseek:
        # _compute_keep_start_index intentionally keeps the final message even
        # when it alone exceeds the cap. At a cache-reset checkpoint we can
        # instead move complete messages (and tool pairs) into the summary.
        while (
            keep_start < len(conversation.history)
            and estimate_tokens(conversation.history[keep_start:])
            > keep_max_tokens
        ):
            current_message = conversation.history[keep_start]
            keep_start += 1
            if (
                current_message.role == "assistant"
                and current_message.tool_uses
                and keep_start < len(conversation.history)
                and conversation.history[keep_start].role == "user"
                and conversation.history[keep_start].tool_results
            ):
                keep_start += 1
    to_summarize = conversation.history[:keep_start]
    keep_tail = conversation.history[keep_start:]

    # 待摘要的前缀太小时退化为不压缩——要么全部消息都落在保留窗口内
    # （keep_start <= 0），要么摘要回收的 token 还不够摘要本身的开销。
    if keep_start <= 0 or _prefix_too_small_to_compact(to_summarize):
        if not manual:
            return block("没有可有效压缩的旧消息，请调整上下文窗口或输出上限。")
        return None

    # Regenerated host context must not be copied into every successive summary.
    # Keep original history untouched; only the summarizer's view is filtered.
    regenerated_kinds = {"environment", "long_term_memory", "runtime", "deferred_tool_catalog"}
    if restore_context is not None:
        regenerated_kinds.update({"chat-workflow-tools", "chat-workflow-state", "workflow-node-task"})
    summary_source = [message for message in to_summarize if not (
        message.transient and message.context_kind in regenerated_kinds)]
    summary_budget = max(512, min(8_000, estimate_tokens(summary_source) // 4))
    summary_char_limit = summary_budget * 2
    summary_instruction = (
        f"请生成接续摘要，正文最多 {summary_char_limit:,} 个字符（约 {summary_budget:,} tokens 的预算）。"
        "只保留有效目标、约束、已完成成果、未完成工作及必要路径；合并旧摘要，不复制历史原文。"
        "不要调用工具，也不要把本压缩指令记为业务要求。"
    )

    summary_conv = ConversationManager()
    summary_conv.history = [
        Message(role="user", content=SUMMARY_PROMPT),
    ]
    # 只摘要前缀；保留的尾部在下面重建时原样拼回。
    for msg in summary_source:
        summary_conv.history.append(msg)
    summary_conv.history.append(
        Message(role="user", content=summary_instruction)
    )

    max_retries = 3
    llm_output: str | None = None
    oversized_summary = False

    for attempt in range(max_retries):
        try:
            from mewcode.client import scheduled_client_stream
            from mewcode.tools.base import StreamEnd, TextDelta

            collected_text = ""
            async for event in scheduled_client_stream(
                client, summary_conv, system=SUMMARY_PROMPT
            ):
                if isinstance(event, TextDelta):
                    collected_text += event.text
                elif isinstance(event, StreamEnd):
                    pass
            if len(extract_summary(collected_text)) > summary_char_limit:
                # Ask again from the same source, without appending the rejected
                # large draft or truncating away user constraints/checkpoints.
                oversized_summary = True
                summary_conv.history[-1] = Message(role="user", content=(
                    summary_instruction + " 上次摘要超出预算，请进一步去重并缩短；保留所有仍有效的限制。"))
                continue
            llm_output = collected_text
            break

        except Exception as e:
            err_msg = str(e).lower()
            if ("prompt" in err_msg and "long" in err_msg
                    or "too many tokens" in err_msg or "context_length_exceeded" in err_msg):
                # Dropping the oldest turns can silently erase the user's scope,
                # stop instructions and completed receipts. Retain the source.
                return block("摘要请求超出服务上下文限制；未丢弃早期指令或替换历史记录。")
            if breaker is not None:
                breaker.record_failure()
            return f"摘要生成失败: {e}"

    if llm_output is None:
        if oversized_summary:
            return block("摘要多次超出压缩预算；原始历史和检查点保留。")
        if breaker is not None:
            breaker.record_failure()
        return "摘要生成失败：多次重试后仍超出上下文限制"

    summary = extract_summary(llm_output)
    if not summary.strip():
        return block("摘要为空；未替换历史记录。")
    if restore_context is not None and attachment:
        # The compact boundary must retain the same observed-read references on reload.
        summary += "\n\n" + attachment
        attachment = ""
    initial_goal = ""
    if deepseek:
        for message in conversation.history:
            content = message.content.strip()
            if (
                message.role == "user"
                and not message.transient
                and content
                and not message.tool_results
                and not content.startswith("<system-reminder>")
                and not content.startswith("Current working directory:")
                and not content.startswith("[摘要]")
            ):
                initial_goal = _truncate_by_tokens(content, 1_500)
                break
    # 重建 = 摘要(user) + 尾部原文。
    new_messages = build_compact_messages(
        summary,
        attachment=attachment,
        has_keep_tail=bool(keep_tail),
        transcript_path=transcript_path,
        initial_goal=initial_goal,
    )
    # Compaction summaries are model text, not a substitute for host safety
    # constraints. Move the latest checkpoint into the persisted tail even
    # when the original tool pair is being summarized away.
    # Validate a detached candidate. A rejected summary must not mutate even
    # nested host checkpoints on the original tool results.
    keep_tail = copy.deepcopy(keep_tail)
    checkpoint = None
    for message in reversed(conversation.history):
        observations = [getattr(message, "host_observation", None)] + [
            result.host_observation for result in reversed(message.tool_results)]
        checkpoint = next((value["auto_review_checkpoint"] for value in observations
            if isinstance(value, dict) and "auto_review_checkpoint" in value), None)
        if checkpoint is not None:
            break
    if checkpoint is not None:
        keep_tail = list(keep_tail)
        if not any(not message.transient for message in keep_tail):
            keep_tail.append(Message(role="user", content="<system-reminder>宿主安全约束已随摘要保留；摘要不授予新权限。</system-reminder>"))
        carrier = next(message for message in reversed(keep_tail) if not message.transient)
        if carrier.tool_results:
            carrier = carrier.tool_results[-1]
        checkpoint = copy.deepcopy(checkpoint)
        from mewcode.permissions.review_recovery import ReviewRecovery
        # Summarization is not a new user choice. Preserve explicit rejections
        # until an actual subsequent user message changes the context.
        compacted = ConversationManager()
        compacted.history = new_messages + keep_tail
        if isinstance(checkpoint, dict):
            checkpoint["user_context"] = ReviewRecovery.user_context(compacted)
        carrier.host_observation = {**(carrier.host_observation or {}), "auto_review_checkpoint": checkpoint}
    new_messages = new_messages + list(keep_tail)

    candidate = ConversationManager()
    candidate.replace_history(new_messages)
    if restore_context is not None:
        restore_context(candidate)
    after_tokens = candidate.current_tokens() + fixed
    # Compare like-for-like estimates for reduction (not a provider anchor
    # against a character estimate), and include restored environment and memory.
    before_estimate = estimate_tokens(conversation.history) + fixed
    reduction = before_estimate - after_tokens
    # The restored candidate must fit with headroom. A percentage of the whole
    # prompt (including uncompressible host context) is not a useful extra gate:
    # a smaller absolute saving can still be enough to continue safely.
    minimum_gain = 512
    headroom = max(2_000, int(threshold * 0.10))
    if after_tokens > threshold - headroom:
        return block(f"回填后估算 {after_tokens:,} tokens，触发阈值 {threshold:,}，余量不足。")
    if reduction < minimum_gain:
        return block(f"压缩仅减少约 {reduction:,} tokens，未达到最低收益；未替换历史记录。")

    # replace_history 替换为重建后的对话并将用量锚点清零
    # （baseline_tokens / anchor_count / last_input_tokens），这是必须的：
    # 旧的 anchor_count 对应压缩前的消息列表，现在已无意义，
    # 不清零会导致 current_tokens() 对增量的估算出错。
    # 下一次 API 响应会基于重建后的 history 重新锚定。
    conversation.replace_history(candidate.history)
    conversation.env_injected = candidate.env_injected
    conversation.ltm_injected = candidate.ltm_injected
    # Persisted outputs are referenced by retained tool previews AND the durable
    # transcript (possibly from other agents sharing this directory). Compaction
    # is not session deletion: do not invalidate those references or replacements.

    if breaker is not None:
        breaker.record_success()

    # 将结构化的 boundary（摘要 + 保留的尾部原文）交给 session 层，
    # 由它持久化为一条 compact_boundary 记录。keep tail 就是拼回重建 history 的那段。
    return CompactEvent(
        before_tokens=before_tokens,
        boundary=CompactBoundary(summary=summary, keep=list(keep_tail)),
        after_tokens=after_tokens,
        output_reserve_tokens=output_reserve,
    )
