"""Legacy chat_activity canvases are recognised only to redirect/reject; they are never created or projected."""
from __future__ import annotations

import json

from mewcode.orchestration.activity import ActivityStore

MODE = "chat_activity"


def metadata(graph):
    try:
        value = json.loads(graph.variables.get("__orch.payload__", "{}"))
        meta = value.get("meta", {}) if isinstance(value, dict) else {}
        return meta if isinstance(meta, dict) else {}
    except (ValueError, TypeError):
        return {}


def is_chat_canvas(graph):
    return graph is not None and metadata(graph).get("canvas_mode") == MODE


def validate(graph, conversation_id, run_id=""):
    if not is_chat_canvas(graph) or metadata(graph).get("conversation_id") != conversation_id:
        raise ValueError("进度画布不属于当前对话")
    if run_id and run_id != ActivityStore.chat_view_run(conversation_id):
        raise ValueError("运行不属于此对话进度视图")


def read_file(work_dir, conversation_id, graph, run_id, file_id):
    validate(graph, conversation_id, run_id)
    store = ActivityStore(work_dir)
    with store.db() as db:
        row = db.execute("SELECT run_id FROM artifact WHERE id=? AND conversation_id=? AND run_id LIKE 'chat:%'", (file_id, conversation_id)).fetchone()
    if not row:
        raise LookupError("找不到此对话的文件快照")
    return store.read_artifact(conversation_id, row[0], file_id)
