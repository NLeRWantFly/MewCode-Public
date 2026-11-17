"""Recognize host repair envelopes, not arbitrary talk about graphs."""
from __future__ import annotations

import json
import re

SCHEMA = "mewcode.graph-repair.v1"
MAX_CHANGES = 32
CONTROL_PURPOSE = "graph_repair_protocol"
_FENCE = re.compile(r"```(?:mewcode-graph-repair|json)\s*\n([\s\S]*?)\n```")


def is_repair_protocol(text, role):
    if role == "user":
        return (text.startswith("请帮我分析并修订当前阶段图的问题。我不熟悉流程图")
                and "InspectStageGraph" in text and "editable_ids" in text
                and "```mewcode-graph-repair" in text)
    if role != "assistant":
        return False
    for block in _FENCE.finditer(text):
        try:
            value = json.loads(block[1])
        except ValueError:
            continue
        if (isinstance(value, dict) and value.get("schema") == SCHEMA
                and re.fullmatch(r"[a-f0-9]{32}", str(value.get("ticket", "")))
                and isinstance(value.get("changes"), list)):
            return True
    return False


def is_control_source(source):
    return source.get("purpose") == CONTROL_PURPOSE or is_repair_protocol(source["text"], source["role"])
