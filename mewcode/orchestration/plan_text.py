"""Deterministic cleanup for authored/generated node plans (no semantic rewrite)."""
from __future__ import annotations

import re
import unicodedata


def compact_plan_text(value: object) -> str:
    text = str(value or "").strip()
    for marker in ("决策指南（执行期必须遵守）：", "约束管线说明："):
        if marker in text:
            text = text.split(marker, 1)[0].rstrip()
    seen: set[str] = set()
    parts = []
    for part in re.split(r"(?<=[。！？!?])\s*|\n+", text):
        part = part.strip()
        if not part:
            continue
        key = unicodedata.normalize("NFKC", part)
        key = re.sub(r"^\s*(?:[-*•]\s+|\d+[.)]\s+|\d+、)", "", key)
        key = re.sub(r"\s+", "", key).rstrip("。.!！?？")
        if key and key not in seen:
            seen.add(key)
            parts.append(part)
    return "\n".join(parts)


def unique_plan_items(values: object) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        return ()
    return tuple(dict.fromkeys(text for value in values if (text := compact_plan_text(value))))


PLAN_OUTPUT_CONTRACT = """
计划写作协议：每个节点只写该节点独有的操作、产物和验收条件。
summary 一句话，steps 最多 5 条，outputs 与 acceptance 各最多 4 条；每条只表达一个可验证事实。
不要重复 goal，不要复述完整对话、其他节点的计划或公共执行规则；共享约束仅在图的 meta.shared_constraints 写一次。
已知输入只引用位置/上游节点 ID；主题、路径、数量等运行值声明在 goal_contract.run_inputs，合理默认记为假设。open_questions 仅用于总目标无法确定等确实阻塞构图的问题。
禁止把同一句话换行、改编号、换少量措辞反复输出。退出条件在循环合同中固定，各角色引用同一条件 ID。
""".strip()
