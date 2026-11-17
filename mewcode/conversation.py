from __future__ import annotations

import json
import copy
from dataclasses import dataclass, field
from typing import Any

ContentBlock = dict[str, Any]



@dataclass
class ToolUseBlock:
    tool_use_id: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False
    content_blocks: list[ContentBlock] = field(default_factory=list)
    host_observation: Any = None


@dataclass
class ThinkingBlock:
    thinking: str
    signature: str = ""
    kind: str = "auto"


@dataclass
class Message:
    role: str  # "user" | "assistant"
    content: str
    tool_uses: list[ToolUseBlock] = field(default_factory=list)
    tool_results: list[ToolResultBlock] = field(default_factory=list)
    thinking_blocks: list[ThinkingBlock] = field(default_factory=list)
    content_blocks: list[ContentBlock] = field(default_factory=list)
    # Synthetic runtime context participates in provider input but must not be
    # appended to session transcripts. Rebuilding it on resume keeps dates,
    # paths and memory fresh without duplicating user messages.
    transient: bool = False
    context_kind: str = ""
    # Host-only constraints may survive compaction on an ordinary message.
    # Provider serializers deliberately ignore this metadata.
    host_observation: Any = None


INTERRUPTED_TOOL_RESULT = "Tool execution was interrupted before completion."


def repair_tool_call_chain(
    messages: list[Message],
    reason: str = INTERRUPTED_TOOL_RESULT,
) -> tuple[list[Message], int]:
    """Repair tool-call adjacency without losing later user turns or observations.

    Every assistant tool call must be followed by exactly one matching tool
    result before another conversational message is sent.  Cancellation used
    to leave an assistant tool-call message at the end of the in-memory
    conversation, which makes the next OpenAI Responses request fail with
    ``No tool output found for function call``.  This repair is intentionally
    idempotent and also accepts partially completed parallel batches.

    Existing matching results are moved next to their calls.  Missing results
    are represented as explicit errors. Truly orphaned outputs remain structured
    results for recovery and audit. Provider serializers render those as explicitly
    untrusted ordinary observations in their wire view without changing history.
    """
    results_by_id: dict[str, ToolResultBlock] = {}
    known_call_ids = {
        call.tool_use_id
        for candidate in messages
        for call in candidate.tool_uses
        if call.tool_use_id
    }
    for message in messages:
        for result in message.tool_results:
            if result.tool_use_id and result.tool_use_id not in results_by_id:
                results_by_id[result.tool_use_id] = result

    repaired: list[Message] = []
    consumed: set[str] = set()
    synthesized = 0
    for message in messages:
        if message.tool_results:
            # Matching results are emitted directly after their assistant
            # call. Preserve unrelated structured outputs; serializers own
            # their provider-only conversion into untrusted ordinary observations.
            orphaned = [
                result for result in message.tool_results
                if result.tool_use_id not in consumed
                and result.tool_use_id not in known_call_ids
            ]
            if orphaned:
                # Preserve standalone result records for imported transcripts
                # and diagnostics. They are left untouched when no matching
                # tool call exists anywhere in the chain.
                repaired.append(
                    Message(role="user", content="", tool_results=orphaned)
                )
            continue

        repaired.append(message)
        if not message.tool_uses:
            continue

        outputs: list[ToolResultBlock] = []
        for call in message.tool_uses:
            existing = results_by_id.get(call.tool_use_id)
            if existing is None:
                existing = ToolResultBlock(
                    tool_use_id=call.tool_use_id,
                    content=reason,
                    is_error=True,
                )
                synthesized += 1
            outputs.append(existing)
            consumed.add(call.tool_use_id)
        repaired.append(Message(role="user", content="", tool_results=outputs))

    return repaired, synthesized


# 估算最后一次 API 用量锚点之后追加的消息 token 开销时使用的字符/token 比率。
# 与 context.manager 中的恢复状态启发值保持一致，全代码库统一使用同一比率。
_CHARS_PER_TOKEN = 3.5


def _message_chars(m: Message) -> int:
    n = len(m.content)
    for block in m.content_blocks:
        if block.get("type") == "text":
            n += len(str(block.get("text", "")))
        elif block.get("type") == "image":
            n += 3500
    for tb in m.thinking_blocks:
        n += len(tb.thinking)
    for tu in m.tool_uses:
        n += len(tu.tool_name) + len(json.dumps(tu.arguments, ensure_ascii=False))
    for tr in m.tool_results:
        n += len(tr.content)
        for block in tr.content_blocks:
            if block.get("type") == "text":
                n += len(str(block.get("text", "")))
            elif block.get("type") == "image":
                n += 3500
    return n


def estimate_tokens(messages: list[Message]) -> int:
    """基于字符数对一组消息做 token 估算。

    刻意做得粗略——它只覆盖那些尚未锚定到真实 API 用量数值的消息，这部分的
    精确度本就无关紧要。统计内容包括消息正文、thinking、工具调用参数以及
    工具结果内容。
    """
    total = sum(_message_chars(m) for m in messages)
    return int(total / _CHARS_PER_TOKEN)


@dataclass
class ConversationManager:
    history: list[Message] = field(default_factory=list)
    # Host-only state; never injected into provider messages as authorization.
    safety_review_state: dict[str, Any] = field(default_factory=dict)
    retired_safety_reviews: list[dict[str, Any]] = field(default_factory=list)
    env_injected: bool = field(default=False, init=False)
    ltm_injected: bool = field(default=False, init=False)
    # API 报告的每轮真实 prompt 大小，保留用于向后兼容。
    # 现在与 baseline_tokens 一致（input + cache_read + cache_creation + output）。
    last_input_tokens: int = field(default=0, init=False)
    # 真实用量锚点。baseline_tokens 是上一轮 API 计费的完整 prompt+output 大小；
    # anchor_count 是记录该数值时的消息数量。两者配合让 current_tokens() 在
    # anchor_count 以内信任 API 数据，只对之后追加的消息做字符估算。
    # baseline_tokens == 0 表示"尚无锚点"（冷启动），此时退化为纯字符估算。
    baseline_tokens: int = field(default=0, init=False)
    anchor_count: int = field(default=0, init=False)
    # Marker-scoped reminders waiting for a safe history boundary. Appending a
    # plain user message while a tool call is still unanswered breaks strict
    # providers, so mid-turn refreshes queue here and flush after tool results.
    _pending_reminders: list[tuple[str, str]] = field(default_factory=list, init=False)

    def record_usage_anchor(
        self,
        input_tokens: int,
        output_tokens: int = 0,
        cache_read: int = 0,
        cache_creation: int = 0,
    ) -> None:
        """根据一次 API 响应钉下一个真实用量锚点。

        baseline = input + cache_read + cache_creation + output。各家服务商
        返回的 input_tokens 已经排除了命中缓存的 token，所以这三个 input 分量
        是相加关系，合起来才是真正的 prompt 大小；之所以再加上 output，是因为
        assistant 的回复此刻已成为历史的一部分。anchor_count 对齐到当前的消息
        数量，这样后续新追加的消息就成了唯一需要估算的部分。
        """
        self.baseline_tokens = (
            input_tokens + cache_read + cache_creation + output_tokens
        )
        self.anchor_count = len(self.history)
        # 保持旧字段同步，兼容仍在使用它的读取方。
        self.last_input_tokens = self.baseline_tokens

    def current_tokens(self) -> int:
        """对当前对话中的 token 数量做出最佳估算。

        有锚点时：baseline（真实用量）+ 仅对锚点之后追加的那些消息做字符估算。
        没有锚点时（冷启动，或刚经历一次压缩重置）：对整个历史做字符估算，
        这样在第一次 API 响应到来之前阈值检查依然能正常工作。
        """
        if self.baseline_tokens <= 0:
            return estimate_tokens(self.history)
        tail = self.history[self.anchor_count:]
        return self.baseline_tokens + estimate_tokens(tail)

    def reset_usage_anchor(self) -> None:
        """Drop provider-specific token accounting without changing history."""
        self.baseline_tokens = 0
        self.anchor_count = 0
        self.last_input_tokens = 0

    def add_user_message(
        self,
        content: str,
        content_blocks: list[ContentBlock] | None = None,
    ) -> None:
        self.history.append(Message(
            role="user", content=content,
            content_blocks=list(content_blocks or []),
        ))
        self.drain_pending_reminders()

    def add_assistant_message(
        self,
        content: str,
        tool_uses: list[ToolUseBlock] | None = None,
        thinking_blocks: list[ThinkingBlock] | None = None,
    ) -> None:
        self.history.append(
            Message(
                role="assistant",
                content=content,
                tool_uses=tool_uses or [],
                thinking_blocks=thinking_blocks or [],
            )
        )
    def add_system_reminder(self, content: str) -> None:
        self.history.append(
            Message(
                role="user",
                content=f"<system-reminder>\n{content}\n</system-reminder>",
            )
        )

    def add_tool_results_message(self, tool_results: list[ToolResultBlock]) -> None:
        self.history.append(
            Message(role="user", content="", tool_results=tool_results)
        )
        # Tool results are a safe boundary: every pending tool_use is now
        # answered, so queued reminders may be appended without splitting a
        # call/result pair for strict providers.
        self.drain_pending_reminders()

    def queue_marker_reminder(self, marker: str, text: str) -> None:
        """Buffer a marker-scoped reminder for the next safe history boundary.

        Same-marker entries replace each other so only the newest text lands.
        """
        self._pending_reminders = [
            item for item in self._pending_reminders if item[0] != marker
        ]
        if text:
            self._pending_reminders.append((marker, text))

    def drain_pending_reminders(self) -> int:
        drained = 0
        while self._pending_reminders:
            marker, text = self._pending_reminders.pop(0)
            self.replace_marker_reminder(marker, text)
            drained += 1
        return drained

    def replace_marker_reminder(self, marker: str, text: str) -> None:
        """Replace the marker-scoped reminder, or clear it when text is empty."""
        prefix = "<system-reminder>\n" + marker + "\n"
        matching = [message for message in self.history
                    if message.role == "user" and str(message.content).startswith(prefix)
                    and not message.tool_results and not message.tool_uses]
        if (text and len(matching) == 1 and matching[0].transient
                and matching[0].context_kind == marker
                and matching[0].content == prefix + text + "\n</system-reminder>"):
            return
        previous_count = len(self.history)
        self.history[:] = [
            message for message in self.history
            if not (message.role == "user" and str(message.content).startswith(prefix)
                    and not message.tool_results and not message.tool_uses)
        ]
        if len(self.history) != previous_count:
            self.reset_usage_anchor()
        if text:
            self.add_system_reminder(marker + "\n" + text)
            # Guard: a conversation that refuses the reminder (empty history or a
            # filtered add) must not raise IndexError in the middle of a resume.
            if self.history:
                self.history[-1].transient = True
                self.history[-1].context_kind = marker

    def repair_incomplete_tool_turns(
        self, reason: str = INTERRUPTED_TOOL_RESULT
    ) -> int:
        """Repair incomplete/partially ordered tool turns in-place.

        Returns the number of synthetic cancellation results that were added.
        Calling this method repeatedly is safe.
        """
        repaired, synthesized = repair_tool_call_chain(self.history, reason)
        self.history = repaired
        return synthesized


    def prune_tool_result_images(self, keep_last: int = 3) -> ConversationManager:
        """Return a provider view; evidence pages and stored history stay intact."""
        view = copy.copy(self)
        view.history = copy.deepcopy(self.history)
        evidence_calls = {call.tool_use_id for message in view.history for call in message.tool_uses
                          if call.tool_name == 'ReadAuditArtifact'}
        locations: list[tuple[ToolResultBlock, int]] = []
        for message in view.history:
            for result in message.tool_results:
                for index, block in enumerate(result.content_blocks):
                    if (block.get("type") == "image" and result.tool_use_id not in evidence_calls
                            and block.get('_mewcode_source_tool') != 'ReadAuditArtifact'):
                        locations.append((result, index))
        keep_last = max(0, keep_last)
        stale = locations[:-keep_last] if keep_last else locations
        for result, index in stale:
            block = result.content_blocks[index]
            name = block.get("name") or block.get("mime_type") or "screenshot"
            result.content_blocks[index] = {
                "type": "text",
                "text": f"[older image omitted from context: {name}]",
            }
        return view

    def inject_environment(self, context: str) -> None:
        if not self.env_injected:
            self.history.insert(0, Message(
                role="user",
                content=context,
                transient=True,
                context_kind="environment",
            ))
            self.env_injected = True

    def refresh_environment(self, context: str) -> None:
        """Refresh the host-owned transient context, never a user's saved text."""
        for message in self.history:
            if message.transient and message.context_kind == "environment":
                message.content = context
                self.env_injected = True
                return
        # Restored or compacted history may no longer contain the old slot.
        self.env_injected = False
        self.inject_environment(context)

    def inject_runtime_context(self, context: str) -> None:
        """Insert volatile context immediately before the newest real user turn."""
        if not context:
            return
        if any(
            message.context_kind == "runtime" and message.content == context
            for message in self.history
        ):
            return
        insert_at = len(self.history)
        for index in range(len(self.history) - 1, -1, -1):
            message = self.history[index]
            if (
                message.role == "user"
                and not message.transient
                and not message.tool_results
            ):
                insert_at = index
                break
        self.history.insert(insert_at, Message(
            role="user",
            content=context,
            transient=True,
            context_kind="runtime",
        ))

    def inject_long_term_memory(
        self, instructions: str, memories: str
    ) -> None:
        if self.ltm_injected:
            return
        sections: list[str] = []
        if instructions:
            sections.append(
                "# mewcodeMd\n"
                "Codebase and user instructions are shown below. "
                "Be sure to adhere to these instructions. "
                "IMPORTANT: These instructions OVERRIDE any default behavior "
                "and you MUST follow them exactly as written.\n\n" + instructions
            )
        if memories:
            sections.append("# autoMemory\n" + memories)
        if not sections:
            return
        from datetime import date

        sections.append(f"# currentDate\nToday's date is {date.today().isoformat()}.")
        body = "\n\n".join(sections)
        wrapped = (
            "<system-reminder>\n"
            "As you answer the user's questions, you can use the following context:\n"
            + body
            + "\n\n      IMPORTANT: this context may or may not be relevant to your tasks."
            " You should not respond to this context unless it is highly relevant to your task.\n"
            "</system-reminder>"
        )
        pos = 1 if self.env_injected else 0
        self.history.insert(pos, Message(
            role="user",
            content=wrapped,
            transient=True,
            context_kind="long_term_memory",
        ))
        self.ltm_injected = True

    def restore_history(self, messages: list[Message]) -> None:
        """Restore durable dialogue while dropping legacy synthetic prefixes.

        Older session writers persisted injected environment/memory messages
        and could append the same user turn again after a head insertion. The
        migration is intentionally narrow: it only de-duplicates equal
        messages when a recognized generated prefix occurred between them.
        """
        restored: list[Message] = []
        removed_generated = False
        for message in messages:
            is_legacy_generated = (
                message.context_kind in {
                    "environment", "long_term_memory", "runtime"
                }
                or message.content.startswith("Current working directory:")
                or message.content.startswith(
                    "<system-reminder>\nAs you answer the user's questions"
                )
            )
            if message.transient or is_legacy_generated:
                removed_generated = True
                continue
            if (
                removed_generated
                and restored
                and message.role == restored[-1].role
                and message.content == restored[-1].content
                and message.content_blocks == restored[-1].content_blocks
                and not message.tool_uses
                and not message.tool_results
                and not restored[-1].tool_uses
                and not restored[-1].tool_results
            ):
                removed_generated = False
                continue
            restored.append(message)
            removed_generated = False
        self.history = restored
        self.env_injected = False
        self.ltm_injected = False
        self.reset_usage_anchor()

    def replace_history(self, new_messages: list[Message]) -> None:
        self.history = new_messages
        self.env_injected = False
        self.ltm_injected = False
        # 旧的用量锚点描述的是压缩前的对话记录，这里清除它，
        # 使 current_tokens() 退化为字符估算，直到下次 API 响应
        # 基于摘要后的历史重新建立锚点。
        self.reset_usage_anchor()


    def get_messages(self) -> list[Message]:
        return list(self.history)
