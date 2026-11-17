import json

import pytest

from mewcode.orchestration.activity import ActivityStore, candidates
from mewcode.orchestration.constraints import compile_constraints, resolve_constraints, attachment_store, attachment_owner
from mewcode.pipeline_events import compact_pipeline_log, PipelineRecorder, PipelineEventStore


@pytest.mark.asyncio
async def test_lifecycle_transitions_survive_compaction_and_overview(tmp_path):
    store = bound(tmp_path)
    path = tmp_path / "transitions.pipeline.jsonl"
    recorder = PipelineRecorder(conversation_id="conv", task_id="task", store=PipelineEventStore.from_path(path))
    for status in ("running", "waiting", "running", "paused"):
        await recorder.lifecycle(status, status)
    compact_pipeline_log(path)
    store.replay_pipeline(path, "conv")
    rows = store.page("conv", "graph", "chat:task")["items"]
    assert [e["status"] for e in reversed(rows)] == ["running", "waiting", "running", "paused"]
    assert len({e["id"] for e in rows}) == 4
    assert next(e for e in store.overview("conv", "graph", "chat:task")["latest"] if e["kind"] == "lifecycle")["status"] == "paused"


def bound(tmp_path):
    store = ActivityStore(tmp_path)
    store.bind("conv", "graph", "chat:task", nodes=[{"id":"build", "label":"实现画布"}, {"id":"check", "label":"检查画布"}], version=3)
    return store


def test_time_and_filename_matches_are_only_candidates():
    event = {"at":"2026-09-11T10:02:00Z", "text":"saved result.md"}
    nodes = [{"id":"a", "label":"工作甲", "outputs":["result.md"]}, {"id":"b", "label":"工作乙"}]
    windows = [{"node":id, "start":"2026-09-11T10:00:00Z", "end":"2026-09-11T10:03:00Z"} for id in ("a","b")]
    node, method, options = candidates(event, nodes, windows)
    assert node == "" and method == "unassigned"
    assert {x["node_id"] for x in options} == {"a", "b"}
    assert "文件" in options[0]["reason"] and "时间" in options[0]["reason"]


def test_activity_without_report_replay_compaction_and_scope(tmp_path):
    store = bound(tmp_path)
    rows = [{"conversation_id":"conv", "task_id":"task", "item_id":"tool1", "kind":"tool", "name":"WriteFile", "timestamp":"2026-09-11T10:00:00Z", "started_at":"2026-09-11T10:00:00Z", "phase":"start", "seq":1, "text":"", "meta":{"arguments":{"path":"canvas.js"}}},
            {"conversation_id":"conv", "task_id":"task", "item_id":"tool1", "kind":"tool", "name":"WriteFile", "timestamp":"2026-09-11T10:00:01Z", "phase":"complete", "seq":2, "text":"写入成功", "status":"completed"}]
    path=tmp_path / "conv.pipeline.jsonl"
    path.write_text("".join(json.dumps(r)+"\n" for r in rows),encoding="utf-8")
    store.replay_pipeline(path,"conv")
    page=store.page("conv","graph","chat:task")
    assert len(page["items"])==1
    event=page["items"][0]
    assert event["text"]=="写入成功" and event["node_id"]==""
    assert event["at"]=="2026-09-11T10:00:00Z"
    compact_pipeline_log(path)
    store.replay_pipeline(path,"conv")
    assert store.page("conv","graph","chat:task")["items"]==page["items"]
    fresh=ActivityStore(tmp_path / "fresh")
    fresh.bind("conv","graph","chat:task",nodes=[])
    fresh.replay_pipeline(path,"conv")
    assert fresh.page("conv","graph","chat:task")["items"][0]["text"]=="写入成功"
    with pytest.raises(LookupError): store.page("another-conversation","graph","chat:task")
    with pytest.raises(LookupError): store.assign("conv","graph","chat:other",event["id"],"build")


def test_manual_assignment_undo_pagination_and_snapshot(tmp_path):
    store=bound(tmp_path)
    ids=[store.record("conv","chat:task",f"event:{i}",{"text":"实现画布", "kind":"tool", "at":str(i)}) for i in range(130)]
    first=store.page("conv","graph","chat:task",limit=50)
    second=store.page("conv","graph","chat:task",before=first["next_cursor"],limit=100)
    assert len(first["items"])+len(second["items"])==130
    assert all(not e["node_id"] and e["candidates"] for e in first["items"])
    store.assign("conv","graph","chat:task",ids[0],"build")
    update=store.page("conv","graph","chat:task",after=first["cursor"])
    assert update["items"][0]["node_id"]=="build"
    store.assign("conv","graph","chat:task",ids[0],None)
    assert store.page("conv","graph","chat:task",node_id="build")["items"]==[]
    store.bind("conv","graph","chat:task",nodes=[{"id":"renamed"}],version=4)
    assert store.bindings("conv","graph")[0]["graph_version"]==3


def test_parallel_retries_dedup_and_actual_used_versions(tmp_path):
    store=bound(tmp_path)
    for seq,node,attempt in [(1,"build",1),(2,"check",1),(3,"build",2)]:
        event={"type":"expert_constraints_applied","seq":seq,"conversation_id":"conv","run_id":"chat:task", "node_id":node,"node_attempt":attempt,"at":"2026-09-11", "constraints":[{"id":"n", "version":str(attempt)}]}
        store.expert(event);store.expert(event,historical=True)
    assert len(store.page("conv","graph","chat:task")["items"])==3
    attempts=store.page("conv","graph","chat:task",node_id="build",attempt=2)["items"]
    assert len(attempts)==1 and attempts[0]["constraints"][0]["version"]=="2"


def test_segment_health_projection_is_idempotent_and_keeps_unknown_timing(tmp_path):
    store = bound(tmp_path)
    health = {"schema_version": 1, "segment_id": "seg-1", "node_id": "build",
              "timing": {"model_seconds": None, "coverage": "partial"},
              "work_delta": {"known": False}}
    event = {"type": "expert_segment_health", "seq": 10,
             "conversation_id": "conv", "run_id": "chat:task", "node_id": "build",
             "health": health, "at": "2026-09-11T10:00:00Z"}
    store.expert(event)
    store.expert(event, historical=True)
    rows = store.page("conv", "graph", "chat:task", node_id="build")["items"]
    assert len(rows) == 1
    assert rows[0]["health"] == health
    assert rows[0]["text"] == "运行分段健康记录"


def test_constraint_version_ignores_layout_and_notes_but_not_content(tmp_path):
    old={"id":"old","target_id":"build","text":"旧备注"}
    note={"id":"new","target_id":"build","text":"必须保留测试", "mode":"constraint"}
    before=compile_constraints([old,note],{"build"})
    assert len(before["build"])==1
    assert before==compile_constraints([old,{**note,"offset":{"dx":22},"width":500,"collapsed":True}],{"build"})
    assert before!=compile_constraints([{**note,"text":"新增要求"}],{"build"})
    blocks,used=resolve_constraints({"meta":{"node_constraints":before}},"build",tmp_path,vision_ok=False)
    assert "必须保留测试" in blocks[0]["text"] and used[0]["version"]==before["build"][0]["version"]


def test_candidates_unique_substring_unresolved_and_sentinel():
    nodes = [{"id": "draw_figures", "label": "绘图"}, {"id": "write_paper", "label": "写论文"}]
    node, method, _ = candidates({"node_id": "绘图"}, nodes)
    assert node == "draw_figures"
    node, method, _ = candidates({"node_id": "绘图工作"}, nodes)
    assert (node, method) == ("draw_figures", "explicit")
    node, method, _ = candidates({"node_id": "不存在的节点"}, nodes)
    assert node == "" and method == "unresolved"
    assert candidates({"node_id": "无"}, nodes) == ("", "unassigned", [])
    assert candidates({"node_id": "NONE"}, nodes) == ("", "unassigned", [])


def test_follow_attribution_within_turn(tmp_path):
    store = bound(tmp_path)

    def ev(item, name, phase, turn, seq, **extra):
        event = {"conversation_id": "conv", "task_id": "task", "item_id": item,
                 "kind": "call", "name": name, "phase": phase, "turn_id": turn,
                 "seq": seq, "timestamp": f"2026-09-11T10:00:{seq:02d}Z",
                 "started_at": f"2026-09-11T10:00:{seq:02d}Z"}
        event.update(extra)
        return event

    def report(item, turn, seq, node, status):
        return ev(item, "report_step", "complete", turn, seq,
                  meta={"arguments": {"node": node, "status": status, "label": "步骤"}})

    store.pipeline(report("r1", "t1", 1, "build", "running"))
    store.pipeline(ev("w1", "WriteFile", "start", "t1", 2, meta={"arguments": {"path": "a.md"}}))
    store.pipeline(ev("w2", "ReadFile", "start", "t1", 3, meta={"arguments": {"path": "b.md"}}))
    store.pipeline(report("r2", "t1", 4, "build", "completed"))
    store.pipeline(ev("w3", "WriteFile", "start", "t2", 5, meta={"arguments": {"path": "c.md"}}))
    items = store.page("conv", "graph", "chat:task", node_id="build")["items"]
    assert {e["source_ref"] for e in items if e["attribution"] == "follow"} == {"w1", "w2"}
    assert {e["source_ref"] for e in items} == {"r1", "w1", "w2", "r2"}
    w3 = next(e for e in store.page("conv", "graph", "chat:task")["items"] if e["source_ref"] == "w3")
    assert w3["node_id"] == "" and w3["attribution"] == "unassigned"
    with store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM attribution").fetchone()[0] == 0


def test_bind_chat_history_keeps_bound_at(tmp_path):
    store = ActivityStore(tmp_path)
    nodes = [{"id": "a", "label": "甲"}]
    store.bind_chat_history("conv", "graph", nodes=nodes, version=3)
    first = store.bindings("conv", "graph")[0]["bound_at"]
    store.bind_chat_history("conv", "graph", nodes=nodes, version=3)
    store.bind_chat_history("conv", "graph", nodes=nodes, version=3)
    assert store.bindings("conv", "graph")[0]["bound_at"] == first


def test_replay_pending_tracks_offset(tmp_path):
    store = bound(tmp_path)
    path = tmp_path / "conv.pipeline.jsonl"
    row = {"conversation_id": "conv", "task_id": "task", "item_id": "e1",
           "kind": "message", "text": "hi", "timestamp": "2026-09-11T10:00:00Z", "seq": 1}
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert store.replay_pending(path)
    store.replay_pipeline(path, "conv")
    assert not store.replay_pending(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({**row, "item_id": "e2", "seq": 2}) + "\n")
    assert store.replay_pending(path)


def test_full_image_attachment_scope_and_vision_fail_closed(tmp_path):
    import base64
    import io
    from PIL import Image
    buffer=io.BytesIO()
    Image.new("RGB",(2,2),"blue").save(buffer,format="PNG")
    png=buffer.getvalue()
    assets=attachment_store(tmp_path)
    ref=assets.save(attachment_owner("graph"),"pixel.png",png,"image/png")
    note={"id":"n","mode":"constraint","target_id":"build","text":"参考图片", "attachments":[{"attachment_id":ref.id,"kind":"image","name":"pixel.png"}]}
    flow={"meta":{"pipeline_graph_id":"graph","node_constraints":compile_constraints([note],{"build"})}}
    blocks,used=resolve_constraints(flow,"build",tmp_path,vision_ok=True)
    assert base64.b64decode(blocks[1]["data"])==png and used[0]["attachments"][0]["attachment_id"]==ref.id
    with pytest.raises(ValueError,match="不支持图片"): resolve_constraints(flow,"build",tmp_path,vision_ok=False)
    flow["meta"]["pipeline_graph_id"]="other"
    with pytest.raises(ValueError): resolve_constraints(flow,"build",tmp_path,vision_ok=True)
