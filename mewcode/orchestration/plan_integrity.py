"""Fail closed on unresolved brief inputs and workflow-edit/task confusion."""
from __future__ import annotations

import re

from .spec import OrchestrationError, OrchestrationSpec


def unresolved_plan_inputs(spec: OrchestrationSpec) -> list[str]:
    problems = []
    for node in spec.nodes:
        for text in node.plan_brief.known_inputs if node.plan_brief else ():
            self_pointer = re.search(
                rf"(?<![\w-]){re.escape(node.node_id)}\s*\.\s*(?:known_inputs|plan_brief|goal|steps|outputs|acceptance)\b", text
            )
            old_pointer = re.match(r"\s*既有阶段\s+\S+\.(?:known_inputs|plan_brief|goal|steps|outputs|acceptance)\b", text)
            # These are chat-relative placeholders, not file paths or upstream
            # stage results. Preserve real artifact references (e.g. repair.md).
            chat_pointer = re.match(r"\s*(?:最新|上述|前述|上文|之前|先前)(?:的)?(?:修复方案|简报|约定)\s*(?:第|中|里|：|:|$)", text)
            if self_pointer or old_pointer or chat_pointer:
                problems.append(f"{node.node_id}: 未解析的已知输入引用「{text}」，请从原始来源填入具体事实或明确产物路径")
    return problems


def require_resolved_plan_inputs(spec: OrchestrationSpec) -> None:
    if problems := unresolved_plan_inputs(spec):
        raise OrchestrationError("；".join(problems))


def is_workflow_reconstruction_request(text: str) -> bool:
    """Narrow signal: restoring an existing workflow, not e.g. restoring data."""
    return len(text) <= 500 and bool(re.search(r"(?:复原|还原|恢复|重建).{0,45}(?:流程|编排图)", text)) and not bool(
        re.search(r"(?:新建|创建|设计|开发).{0,12}(?:一个|一套).{0,20}(?:复原|还原|恢复|重建)", text)
    )


def require_business_goal(goal: str, messages) -> None:
    users = [str(getattr(m, "content", "") or "") for m in messages if getattr(m, "role", "") == "user"]
    if len(users) > 1 and is_workflow_reconstruction_request(users[-1]) and is_workflow_reconstruction_request(goal):
        raise OrchestrationError("把“复原流程”的编辑请求误作了业务目标；请在流程底稿核对原始主流程，未覆盖当前图")
