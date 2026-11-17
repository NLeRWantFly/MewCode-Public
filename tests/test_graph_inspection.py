"""Candidate -> real host tool -> repair -> explicit save, without paid models."""
import copy
import json
from collections import defaultdict
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from frontend import server
from mewcode.orchestration.draft_sources import read_sources
from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.graph_inspection import PAGE_CHARS, READER_SERVICE, page
from mewcode.orchestration.graph_repair import SCHEMA
from mewcode.orchestration.workflow_draft import project_graph
from mewcode.tools.base import MAX_OUTPUT_CHARS
from mewcode.tools.inspect_stage_graph import InspectStageGraph, Params
from tests.test_graph_generation import API
from tests.test_workflow_drafts import api_env, cited, entry, ready


def broken_graph():
    values = [entry("work"), entry("side"), entry("check", data={"node_kind": "audit"}),
              entry("finish", data={"node_kind": "reduce"}),
              entry("first_edge", "edge", data={"from_node": "work", "to_node": "side", "condition": "always"}),
              entry("wrong_edge", "edge", data={"from_node": "side", "to_node": "finish", "condition": "always"}),
              entry("exit_edge", "edge", data={"from_node": "check", "to_node": "finish", "condition": "pass"}),
              entry("retry_edge", "edge", data={"from_node": "check", "to_node": "work", "condition": "loop"}),
              entry("cycle", "loop", data={"loop_id": "repair", "entry_node_id": "work", "check_node_id": "check",
                    "node_ids": ["work", "side", "check"], "reviewer_node_ids": ["check"],
                    "exit_condition": "必须独立复核通过", "criteria": [{"criterion_id": "CHECK", "acceptance_test": "必须逐项核对"}],
                    "control_mode": "team"})]
    return {"schema_version": 1, "entries": values, "ignored": []}


def host_tool(owner):
    return InspectStageGraph(SimpleNamespace(work_dir=owner["project"], session_id=owner["conversation_id"],
                                             runtime_context=server.APP_CONTEXT))


async def read_all(tool, *, ticket=None):
    rows, pages, cursor, version = [], [], 0, None
    while True:
        result = await tool.run(Params(ticket=ticket, cursor=cursor, snapshot_id=version), work_dir=tool.agent.work_dir)
        assert not result.is_error, result.output
        assert len(result.output) <= PAGE_CHARS < MAX_OUTPUT_CHARS
        p = json.loads(result.output)
        assert not version or p["snapshot_id"] == version
        version = p["snapshot_id"]
        assert p["cursor"] == cursor
        rows.extend(p["rows"]); pages.append(p)
        cursor = p["next_cursor"]
        if cursor is None:
            assert len(rows) == p["total_frames"]
            break
    sections, fragments = defaultdict(dict), defaultdict(dict)
    for row in rows:
        key = row["section"], row["index"]
        if "value" in row:
            sections[key[0]][key[1]] = row["value"]
        else:
            assert row["encoding"] == "json-fragment"
            fragments[key][row["part"]] = row["content"]
            if len(fragments[key]) == row["parts"]:
                sections[key[0]][key[1]] = json.loads("".join(fragments[key][i] for i in range(row["parts"])))
    data = {section: [items[i] for i in sorted(items)] for section, items in sections.items()}
    for section, count in pages[0]["counts"].items():
        assert len(data.get(section, [])) == count
    return data, pages


def install_candidate(owner, history_path, document=None):
    store = DraftStore(owner["project"], owner["conversation_id"])
    sources, warnings, marker = read_sources(history_path)
    document = document or broken_graph()
    document["entries"] = [cited(e, sources[0]) for e in document["entries"]]
    ready(store, sources, document)
    return store


@pytest.mark.asyncio
async def test_complete_invalid_graph_tool_handoff_repair_adopt_and_save(api_env):
    _, plans, path, owner = api_env
    store = install_candidate(owner, path)
    original = copy.deepcopy(store.get()["candidate"])
    with pytest.raises(ValueError, match="无法到达判定"):
        project_graph(original["document"], None, conversation_id="paper", revision=1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        state = (await client.get(API, params=owner)).json()
        assert state["issues"] and not state["can_preview"]
        handoff = await client.post(API + "/repair", json={**owner, "action": "request"})
        assert handoff.status_code == 200, handoff.text
        packet = handoff.json()
        assert "InspectStageGraph" in packet["prompt"] and "snapshot_id" in packet["prompt"]
        before_read, file_before = store.get(), store.path.read_bytes()
        data, _ = await read_all(host_tool(owner), ticket=packet["ticket"])
        assert store.path.read_bytes() == file_before and store.get() == before_read
        assert {n["node_id"] for n in data["nodes"]} == {"work", "side", "check", "finish"}
        assert len(data["edges"]) == 4 and data["meta.loops"][0]["check_node_id"] == "check"
        problem = next(p for p in data["validation.diagnostics"] if p["code"] == "loop_unreachable")
        assert problem["unreachable_from_entry"] == ["check"]
        assert problem["cannot_reach_check"] == ["side", "work"]
        witness = next(p for p in problem["failure_paths"] if p["node_id"] == "side")
        assert witness["path_from_entry"] == ["work", "side"] and witness["outgoing_edge_ids"] == ["wrong_edge"]
        proposal = {"schema": SCHEMA, "ticket": packet["ticket"], "summary": "支线先进入独立审查，不能直接结束。",
                    "changes": [{"entry_id": "wrong_edge", "reason": "将脱离循环的支线接回独立判定，不改验收。",
                                 "changes": {"data": {"to_node": "check"}}}]}
        reply = await client.post(API + "/repair", json={**owner, "action": "preview", "proposal": proposal})
        assert reply.status_code == 200, reply.text
        preview = reply.json()
        assert preview["validation"]["valid"], preview["validation"]
        assert store.get()["candidate"] == original
        assert (await client.post(API + "/repair", json={**owner, "action": "adopt", "preview_id": preview["id"]})).status_code == 200
        current = store.get()["candidate"]
        assert current["processed_ids"] == original["processed_ids"]
        for before, after in zip(original["document"]["entries"], current["document"]["entries"]):
            assert after["sources"] == before["sources"]
            if before["id"] != "wrong_edge":
                assert after == before
        assert not plans.get(owner["project"]), "adoption is not graph publication or execution"
        graph = await client.post(API + "/graph", json=owner)
        assert graph.status_code == 200, graph.text
        saved = await client.post(API + "/graph", json={**owner, "action": "apply", "preview_id": graph.json()["id"]})
        assert saved.status_code == 200, saved.text
        assert server._conversation_plan_store("paper").get(owner["project"]).status == "drafting"
        assert (await host_tool(owner).execute(Params())).is_error, "saved canvas must not masquerade as a candidate"


@pytest.mark.asyncio
async def test_pagination_lossless_long_rows_all_edges_and_stale_versions(api_env):
    _, _, path, owner = api_env
    document = broken_graph()
    document["entries"][0]["body"] = "中文\\\"\n" * 950
    for i in range(120):
        document["entries"].append(entry(f"extra_{i}", body=f"第{i}个阶段"))
    store = install_candidate(owner, path, document)
    before = store.get()
    data, pages = await read_all(host_tool(owner))
    assert len(pages) > 3 and not any(p["complete"] for p in pages[:-1]) and pages[-1]["complete"]
    assert data["entries"] == before["candidate"]["document"]["entries"]
    assert len(data["nodes"]) == 124 and len(data["edges"]) == 4
    c = before["candidate"]
    store.edit(c["id"], c["sequence"], "work", {"body": "后来的人工修改"})
    stale = await host_tool(owner).execute(Params(snapshot_id=pages[0]["snapshot_id"], cursor=pages[0]["next_cursor"]))
    assert stale.is_error and "版本" in stale.output
    missing_version = await host_tool(owner).execute(Params(cursor=1))
    assert missing_version.is_error and "版本" in missing_version.output


@pytest.mark.asyncio
async def test_broken_reference_still_readable_and_repairable(api_env):
    _, _, path, owner = api_env
    doc = broken_graph(); doc["entries"][0]["data"]["parent_id"] = "ghost"
    store = install_candidate(owner, path, doc)
    before = store.get()
    data, _ = await read_all(host_tool(owner))
    assert any(p["path"] == "entries/work/data" and p["code"] == "invalid_reference" for p in data["validation.diagnostics"])
    assert store.get() == before
    packet = store.request_graph_repair(None, "work")
    assert "InspectStageGraph" in packet["prompt"]
    proposal = {"schema": SCHEMA, "ticket": packet["ticket"], "summary": "移除不存在的父级引用。",
                "changes": [{"entry_id": "work", "reason": "宿主确认该父级不在本图中。", "changes": {"data": {"parent_id": None}}}]}
    preview = store.preview_graph_repair(None, proposal)
    assert not preview["validation"]["valid"], "partial repair must not pretend the loop is fixed"


@pytest.mark.asyncio
async def test_conflict_ticket_can_repair_related_edge_but_not_change_other_business_requirements(api_env):
    _, _, path, owner = api_env
    doc = broken_graph(); doc["entries"][0]["state"] = "conflict"
    store = install_candidate(owner, path, doc)
    packet = store.request_graph_repair(None)
    assert packet["entry_ids"] == ["work"]
    proposal = {"schema": SCHEMA, "ticket": packet["ticket"], "summary": "按原验收修复通向独立复核的依赖。",
                "changes": [{"entry_id": "wrong_edge", "reason": "先复核，再结束。", "changes": {"data": {"to_node": "check"}}}]}
    before = store.get()["candidate"]
    preview = store.preview_graph_repair(None, proposal)
    assert preview["changes"][0]["entry_id"] == "wrong_edge"
    assert not preview["validation"]["valid"], "a relation repair alone cannot resolve the requirement conflict"
    for change in ({"body": "改写其他节点职责"}, {"data": {"criteria": []}}, {"data": {"exit_condition": "直接放行"}}, {"data": {"token_budget": 0}}):
        bad = copy.deepcopy(proposal)
        bad["changes"][0].update(entry_id="cycle", changes=change)
        with pytest.raises(ValueError, match="关联结构"):
            store.preview_graph_repair(None, bad)
    assert store.get()["candidate"] == before


def test_base_drift_never_mixes_new_saved_settings_with_old_candidate(tmp_path):
    store = DraftStore(tmp_path, "paper")
    ready(store, [{"id": "s1", "text": "要求", "role": "user"}], broken_graph())
    with pytest.raises(ValueError, match="基线已变化"):
        page(store.get(), {"goal": "后来修改的画布"}, conversation_id="paper")


def test_large_associated_graph_handoff_is_only_a_pointer_without_losing_readable_data(tmp_path):
    store = DraftStore(tmp_path, "paper")
    doc = broken_graph(); doc["entries"][0]["state"] = "conflict"
    for i in range(100):
        doc["entries"].append(entry(f"e_{i}", "edge", body="关联依赖", data={"from_node": "work", "to_node": "check", "condition": "when " + "条件" * 200}))
    ready(store, [{"id": "s1", "text": "要求", "role": "user"}], doc)
    packet = store.request_graph_repair(None)
    assert len(packet["prompt"]) < 10000 and "partial_context_only" in packet["prompt"]
    # This used to reject the handoff because adjacent edges filled the prompt.
    view = page(store.get(), None, conversation_id="paper", ticket=packet["ticket"])
    assert view["counts"]["edges"] == 104 and view["next_cursor"] is not None


@pytest.mark.asyncio
async def test_scope_ticket_service_absence_and_strictly_read_only_missing_store(api_env, tmp_path):
    current, _, path, owner = api_env
    tool = host_tool(owner)
    assert not (tmp_path / ".mewcode/workflow-drafts.sqlite3").exists()
    assert (await tool.execute(Params())).is_error
    assert not (tmp_path / ".mewcode/workflow-drafts.sqlite3").exists()
    assert not (tmp_path / ".mewcode/pipelines").exists()
    store = install_candidate(owner, path)
    packet = store.request_graph_repair(None)
    assert (await tool.execute(Params(ticket="0" * 32))).is_error
    assert (await tool.run(Params(), work_dir=str(tmp_path.parent))).is_error
    current["cid"] = "other"
    assert (await tool.execute(Params(ticket=packet["ticket"]))).is_error
    assert (await InspectStageGraph(SimpleNamespace(work_dir=str(tmp_path), session_id="paper")).execute(Params())).is_error
    with pytest.raises(ValidationError):
        Params(conversation_id="other")
    with pytest.raises(ValidationError):
        Params(cursor=True)


@pytest.mark.asyncio
async def test_http_read_snapshot_matches_tool_without_writes(api_env):
    _, _, path, owner = api_env
    store = install_candidate(owner, path)
    before = store.path.read_bytes()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        result = await client.get(API + "/snapshot", params=owner)
        assert result.status_code == 200, result.text
        tool_result = json.loads((await host_tool(owner).execute(Params())).output)
        assert result.json() == tool_result
        assert (await client.get(API + "/snapshot", params={**owner, "conversation_id": "wrong"})).status_code == 409
        assert (await client.get(API + "/snapshot", params={**owner, "cursor": -1})).status_code == 409
    assert store.path.read_bytes() == before
    assert not (path.parent.parent / "pipelines").exists(), "readers must not create empty canvas storage"


@pytest.mark.asyncio
async def test_runtime_registration_protocol_visibility_and_child_rebinding(api_env):
    from mewcode.plugins import ApplicationHost
    from mewcode.plugins.runtime import AgentRuntimeRequest
    from mewcode.plugins.services import AGENT_FACTORY
    from tests.test_decision_runtime import harness

    current, _, path, owner = api_env
    store = install_candidate(owner, path)
    runtime, _, usage = harness(path.parent)
    host = ApplicationHost(owner["project"], discover_plugins=False)
    await host.astart()
    scopes = []
    try:
        host.context.provide(READER_SERVICE, server.APP_CONTEXT.get(READER_SERVICE))
        factory = host.context.get(AGENT_FACTORY)
        base = {"provider": runtime._client.config, "work_dir": owner["project"], "client": runtime._client,
                "auxiliary_client": runtime._client, "enable_memory": False, "enable_skills": False,
                "usage_service": usage, "instructions_content": ""}
        parent = await factory.acreate(AgentRuntimeRequest(**base)); scopes.append(parent)
        parent.agent.session_id = "paper"
        tool = parent.registry.get("InspectStageGraph")
        assert isinstance(tool, InspectStageGraph) and tool.agent is parent.agent
        from mewcode.tools.impl.invoke_deferred import InvokeDeferredTool
        parent.registry.register(InvokeDeferredTool(parent.registry))
        assert parent.registry.is_enabled("InvokeDeferredTool"), "exercise the real fixed-schema branch"
        for channel in ("generic", "deepseek"):
            schemas = parent.registry.get_context_schemas("openai", channel)
            assert "InspectStageGraph" in [s["name"] for s in schemas]
        assert not (await tool.execute(Params())).is_error
        child = await factory.acreate(AgentRuntimeRequest(**base, include_default_tools=False,
            inherit_parent_tools=False, extra_tools=(tool,)))
        scopes.append(child); child.agent.session_id = "unrelated"
        assert child.registry.get(tool.name).agent is child.agent
        assert (await child.registry.get(tool.name).execute(Params())).is_error
        assert store.get()["candidate"]["status"] == "ready"
    finally:
        for scope in reversed(scopes):
            await scope.aclose()
        await host.aclose()
        await runtime.aclose()
