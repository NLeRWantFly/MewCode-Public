from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from frontend import server
from mewcode.orchestration import OrchestrationSpec, get_template, to_pipeline_graph
from mewcode.orchestration.plan_session import PlanSessionStore, build_plan_session
from mewcode.pipeline.store import PipelineStore


@pytest.fixture(autouse=True)
def isolated_execution_store(tmp_path, monkeypatch):
    from mewcode.experts.flow import ExpertFlowStore
    monkeypatch.setattr(server, "EXPERT_FLOWS_STORE", ExpertFlowStore(tmp_path / "flows.json"))


@pytest.mark.asyncio
async def test_generic_loop_canvas_save_validates_before_overwriting(tmp_path, monkeypatch):
    from tests.test_generic_loops import spec_for
    project = tmp_path / "generic-project"
    project.mkdir()
    plan_store = PlanSessionStore(tmp_path / "generic-plans.json")
    spec = OrchestrationSpec.from_value(_bind_all_nodes(spec_for().as_dict()))
    graph = to_pipeline_graph(spec, graph_id="g_generic")
    plan_store.put(project, build_plan_session(spec, draft_plan=True))
    monkeypatch.setattr(server.SESSION, "work_dir", str(project))
    monkeypatch.setattr(server, "get_plan_session_store", lambda: plan_store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        saved = await client.post("/api/orchestration/canvas-sync", json={"graph": graph.model_dump(mode="json")})
        assert saved.status_code == 200, saved.text
        assert saved.json()["spec"]["meta"]["loops"][0]["loop_id"] == "improve"
        assert any(edge["condition"] == "loop" for edge in saved.json()["spec"]["edges"])
        before = plan_store.get(project).spec.as_dict()
        bad = saved.json()["graph"]
        bad["edges"] = [edge for edge in bad["edges"] if not (
            (edge.get("from") or edge.get("from_node")) == "check"
            and (edge.get("to") or edge.get("to_node")) == "done")]
        rejected = await client.post("/api/orchestration/canvas-sync", json={"graph": bad})
        assert rejected.status_code == 400
        assert plan_store.get(project).spec.as_dict() == before


def _bind_all_nodes(raw: dict) -> dict:
    binding = {"provider_key": "p", "model_id": "m"}
    nodes = []
    for node in raw.get("nodes") or []:
        item = dict(node)
        if item.get("kind") != "gate" and not item.get("model"):
            item["model"] = dict(binding)
        nodes.append(item)
    raw = dict(raw)
    raw["nodes"] = nodes
    return raw


@pytest.mark.asyncio
async def test_orchestration_canvas_rejects_stale_id_and_allows_explicit_new_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "active-project"
    project.mkdir()
    plan_store = PlanSessionStore(tmp_path / "plan-sessions.json")

    raw = get_template("linear", goal="修复编排画布").as_dict()
    raw.setdefault("meta", {})["pipeline_graph_id"] = "g_stale"
    spec = OrchestrationSpec.from_value(raw)
    plan_store.put(project, build_plan_session(spec, draft_plan=True))

    monkeypatch.setattr(server.SESSION, "work_dir", str(project))
    monkeypatch.setattr(server, "get_plan_session_store", lambda: plan_store)

    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        opened = await client.post(
            "/api/orchestration/open-canvas",
            json={"graph_id": "g_stale"},
        )
        assert opened.status_code == 409
        assert PipelineStore(str(project)).list_pipelines() == []
        raw["meta"].pop("pipeline_graph_id")
        opened = await client.post("/api/orchestration/open-canvas", json={"spec": raw})
        assert opened.status_code == 200
        payload = opened.json()
        recovered_id = payload["graph_id"]
        assert recovered_id != "g_stale"
        assert payload["graph"]["graph_id"] == recovered_id
        assert PipelineStore(str(project)).get_pipeline(recovered_id) is not None
        assert plan_store.get(project).spec.meta["pipeline_graph_id"] == recovered_id

        graph = payload["graph"]
        task = next(node for node in graph["nodes"] if node["type"] == "task")
        task["label"] = "已从当前项目保存"
        orch_payload = json.loads(graph["variables"]["__orch.payload__"])
        orch_payload["nodes"][task["id"]]["canvas_size"] = {
            "width": 420,
            "height": 260,
        }
        orch_payload["nodes"][task["id"]]["checkpoint_layout_version"] = 1
        graph["variables"]["__orch.payload__"] = json.dumps(
            orch_payload,
            ensure_ascii=False,
        )
        saved = await client.post(
            "/api/orchestration/canvas-sync",
            json={"graph": graph},
        )
        assert saved.status_code == 200
        saved_payload = saved.json()
        assert saved_payload["graph"]["version"] == graph["version"] + 1
        persisted = PipelineStore(str(project)).get_pipeline(recovered_id)
        assert persisted is not None
        assert next(node for node in persisted.nodes if node.type.value == "task").label == (
            "已从当前项目保存"
        )
        persisted_payload = json.loads(persisted.variables["__orch.payload__"])
        assert persisted_payload["nodes"][task["id"]]["canvas_size"] == {
            "width": 420,
            "height": 260,
        }
        assert persisted_payload["nodes"][task["id"]]["checkpoint_layout_version"] == 1

        conflict = await client.post(
            "/api/orchestration/canvas-sync",
            json={"graph": graph},
        )
        assert conflict.status_code == 409
        assert conflict.json()["current_version"] == saved_payload["graph"]["version"]


@pytest.mark.asyncio
async def test_open_canvas_preserves_nesting_when_plan_meta_lost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "nested-project"
    project.mkdir()
    plan_store = PlanSessionStore(tmp_path / "plan-sessions.json")

    raw = _bind_all_nodes(get_template("linear", goal="重开保留嵌套").as_dict())
    parent_id = raw["nodes"][1]["node_id"]
    raw["nodes"] = list(raw["nodes"]) + [
        {
            "node_id": "child_a",
            "kind": "implement",
            "label": "子阶段 A",
            "goal": "验收 A",
            "model": {"provider_key": "p", "model_id": "m"},
            "position": {"x": 160.0, "y": 340.0},
        }
    ]
    raw["edges"] = list(raw["edges"]) + [
        {"from_node": parent_id, "to_node": "child_a"},
    ]
    raw["meta"] = {
        "pipeline_graph_id": "g_keep_nest",
        "canvas_groups": {
            parent_id: {
                "expanded": True,
                "children": ["child_a"],
                "label": raw["nodes"][1]["label"],
            }
        },
    }
    nested = OrchestrationSpec.from_value(raw)
    store = PipelineStore(str(project))
    store.create_pipeline(to_pipeline_graph(nested, graph_id="g_keep_nest"))

    # Plan session drops canvas_groups — the old open-canvas path would flatten.
    flat = OrchestrationSpec.from_value(
        {
            **nested.as_dict(),
            "meta": {"pipeline_graph_id": "g_keep_nest"},
        }
    )
    plan_store.put(project, build_plan_session(flat, draft_plan=True))

    monkeypatch.setattr(server.SESSION, "work_dir", str(project))
    monkeypatch.setattr(server, "get_plan_session_store", lambda: plan_store)

    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        opened = await client.post(
            "/api/orchestration/open-canvas",
            json={"graph_id": "g_keep_nest"},
        )
        assert opened.status_code == 200
        payload = opened.json()
        orch_payload = json.loads(payload["graph"]["variables"]["__orch.payload__"])
        assert orch_payload["nodes"][parent_id].get("refine_group") is True
        assert orch_payload["nodes"]["child_a"].get("parent_group") == parent_id
        groups = (payload["spec"]["meta"] or {}).get("canvas_groups") or {}
        assert parent_id in groups
        assert groups[parent_id]["children"] == ["child_a"]
        assert plan_store.get(project).spec.meta["canvas_groups"][parent_id]["children"] == [
            "child_a"
        ]


@pytest.mark.asyncio
async def test_open_canvas_keeps_stored_extra_when_posted_spec_omits_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Folded / stale reopen must reuse the stored canvas instead of a new graph."""
    from mewcode.pipeline.models import GraphNode

    project = tmp_path / "keep-extra-project"
    project.mkdir()
    plan_store = PlanSessionStore(tmp_path / "plan-sessions.json")

    raw = _bind_all_nodes(get_template("linear", goal="保留画布节点").as_dict())
    spec = OrchestrationSpec.from_value(raw)
    graph = to_pipeline_graph(spec, graph_id="g_keep_extra")
    extra = {
        "id": "revise_1",
        "type": "task",
        "title": "[implement] 修订实验",
        "label": "修订实验",
        "goal": "按审计意见改实验",
        "position": {"x": 420.0, "y": 280.0},
    }
    nodes = list(graph.nodes)
    end_index = next(i for i, node in enumerate(nodes) if node.type.value == "end")
    nodes.insert(end_index, GraphNode.model_validate(extra))
    payload = json.loads(graph.variables["__orch.payload__"])
    payload["nodes"]["revise_1"] = {"kind": "implement", "model": {}}
    graph = graph.model_copy(
        update={
            "nodes": nodes,
            "variables": {
                **graph.variables,
                "__orch.payload__": json.dumps(payload, ensure_ascii=False),
            },
        }
    )
    store = PipelineStore(str(project))
    store.create_pipeline(graph)

    stale = OrchestrationSpec.from_value({**spec.as_dict(), "meta": {}})
    plan_store.put(project, build_plan_session(stale, draft_plan=True))

    monkeypatch.setattr(server.SESSION, "work_dir", str(project))
    monkeypatch.setattr(server, "get_plan_session_store", lambda: plan_store)

    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        opened = await client.post(
            "/api/orchestration/open-canvas",
            json={"spec": stale.as_dict(), "mode": "refresh"},
        )
        assert opened.status_code == 200, opened.text
        task_ids = {
            node["id"]
            for node in opened.json()["graph"]["nodes"]
            if node["type"] == "task"
        }
        assert "revise_1" in task_ids
        assert opened.json()["graph_id"] == "g_keep_extra"

        reopened = await client.post(
            "/api/orchestration/open-canvas",
            json={"graph_id": "g_keep_extra"},
        )
        assert reopened.status_code == 200, reopened.text
        again = {
            node["id"]
            for node in reopened.json()["graph"]["nodes"]
            if node["type"] == "task"
        }
        assert "revise_1" in again


@pytest.mark.asyncio
async def test_canvas_sync_nests_new_child_under_flat_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "add-child-project"
    project.mkdir()
    plan_store = PlanSessionStore(tmp_path / "plan-sessions.json")

    raw = _bind_all_nodes(get_template("linear", goal="画布加子阶段").as_dict())
    spec = OrchestrationSpec.from_value(raw)
    graph = to_pipeline_graph(spec, graph_id="g_add_child")
    store = PipelineStore(str(project))
    store.create_pipeline(graph)
    plan_store.put(project, build_plan_session(spec, draft_plan=True))

    monkeypatch.setattr(server.SESSION, "work_dir", str(project))
    monkeypatch.setattr(server, "get_plan_session_store", lambda: plan_store)

    parent_id = raw["nodes"][1]["node_id"]
    dumped = graph.model_dump(mode="json", by_alias=True)
    payload = json.loads(dumped["variables"]["__orch.payload__"])
    payload["nodes"][parent_id]["refine_group"] = True
    payload["nodes"][parent_id]["group_expanded"] = True
    payload["nodes"]["stage_new1"] = {
        "kind": "implement",
        "parent_group": parent_id,
        "model": {},
    }
    dumped["variables"]["__orch.payload__"] = json.dumps(payload, ensure_ascii=False)
    dumped["nodes"].insert(
        -1,
        {
            "id": "stage_new1",
            "type": "task",
            "title": "[implement] 子阶段",
            "label": "子阶段",
            "goal": "新增 checkpoint",
            "position": {"x": 180, "y": 360},
        },
    )
    dumped["edges"].append(
        {"id": "e_parent_new", "from": parent_id, "to": "stage_new1", "label": ""}
    )

    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        saved = await client.post(
            "/api/orchestration/canvas-sync",
            json={"graph": dumped},
        )
        assert saved.status_code == 200, saved.text
        spec_payload = saved.json()["spec"]
        node_ids = {node["node_id"] for node in spec_payload["nodes"]}
        assert "stage_new1" in node_ids
        groups = (spec_payload.get("meta") or {}).get("canvas_groups") or {}
        assert parent_id in groups
        assert "stage_new1" in groups[parent_id]["children"]
