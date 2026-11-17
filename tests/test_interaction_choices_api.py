"""Offline checks for the host-owned orchestration choice preparation API."""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from pypdf import PdfWriter

from frontend import interaction_choices as api


def _pdf(path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as stream:
        writer.write(stream)


def _spec(*, default: str = "") -> dict:
    item = {"name": "sources", "type": "list", "required": True, "collect_at": "start"}
    if default:
        item["default"] = default
    return {
        "spec_id": "choice-spec",
        "label": "候选准备测试",
        "nodes": [{"node_id": "work", "kind": "implement", "label": "处理"}],
        "goal_contract": {"run_inputs": [item]},
        "meta": {"conversation_id": "chat"},
    }


class FakeRuntime:
    def __init__(self, client=True):
        self.client = object() if client else None
        self.orchestration_run_inputs = {}


class FakeRuns:
    def __init__(self, records=None):
        self.records = copy.deepcopy(records or {})
        self.mutations = 0

    def load(self, run_id):
        return copy.deepcopy(self.records.get(run_id))

    def mutate(self, run_id, callback, durable=True):
        self.mutations += 1
        current = self.records.get(run_id)
        if current is None:
            raise ValueError("run missing")
        callback(current)
        self.records[run_id] = current
        return copy.deepcopy(current)


class FakeSrv:
    def __init__(self, workdir: Path, *, client=True, runs=None):
        self.SESSION = SimpleNamespace(work_dir=str(workdir), conversation_id="chat")
        self.runtime = FakeRuntime(client)
        self.EXPERT_RUNS = FakeRuns(runs)
        self.draft_state = None

    def _active_conversation_id(self):
        return self.SESSION.conversation_id

    def _own_orchestration_spec(self, spec, *, conversation_id=None, project=None):
        if conversation_id != self._active_conversation_id():
            raise ValueError("foreign conversation")
        return spec

    def _runtime_for(self, cid):
        return self.runtime if cid == self._active_conversation_id() else None

    def read_draft_state(self, project, cid, draft_id):
        return copy.deepcopy(self.draft_state)


def _app(srv):
    app = FastAPI()
    api.install(app, srv)
    return app


def _model(monkeypatch, srv, *, files=()):
    calls = []

    async def complete(_client, *, system, user_text, total_timeout, max_generated_chars):
        calls.append(user_text)
        if len(calls) % 2:
            return json.dumps({"searches": [{"root": str(Path(srv.SESSION.work_dir)), "pattern": "*.pdf"}]})
        return json.dumps({"questions": [{"name": "sources", "choices": [{
            "id": "pdfs", "label": "实际材料", "kind": "value", "value": [str(path) for path in files],
            "evidence": [str(path) for path in files], "verification": "verified",
        }]}]})

    monkeypatch.setattr(api, "collect_model_text", complete)
    return calls


@pytest.mark.asyncio
async def test_four_pdf_startup_returns_verified_candidate_and_cache(monkeypatch, tmp_path):
    files = [tmp_path / f"source-{index}.pdf" for index in range(4)]
    for path in files:
        _pdf(path)
    srv = FakeSrv(tmp_path)
    calls = _model(monkeypatch, srv, files=files)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(srv)), base_url="http://test") as client:
        first = await client.post(api._ROUTE, json={"conversation_id": "chat", "spec": _spec(), "request_id": "startup"})
        assert first.status_code == 200, first.text
        payload = first.json()
        assert payload["choice_bundle"]["status"] == "ready"
        assert payload["questions"][0]["default"] is None
        assert payload["choice_bundle"]["questions"][0]["choices"][0]["value"] == [str(path) for path in files]
        assert len(calls) == 2
        repeated = await client.post(api._ROUTE, json={"conversation_id": "chat", "spec": _spec(), "request_id": "startup"})
        assert repeated.status_code == 200, repeated.text
        assert len(calls) == 2


@pytest.mark.asyncio
async def test_stale_conversation_spec_and_case_are_rejected(monkeypatch, tmp_path):
    srv = FakeSrv(tmp_path)
    _model(monkeypatch, srv)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(srv)), base_url="http://test") as client:
        stale_cid = await client.post(api._ROUTE, json={"conversation_id": "other", "spec": _spec()})
        assert stale_cid.status_code == 409

        candidate = {"id": "candidate", "sequence": 2, "status": "ready", "base_snapshot": _spec(),
                     "document": {"entries": [{"id": "q", "kind": "question", "state": "active",
                         "title": "取舍", "body": "选择", "data": {"issue_type": "business", "options": [
                             {"value": "a", "label": "A"}]}}]}}
        srv.draft_state = {"candidate": candidate}
        stale_spec = _spec(); stale_spec["label"] = "另一个图"
        stale_graph = await client.post(api._ROUTE, json={"conversation_id": "chat", "candidate_id": "candidate",
            "sequence": 2, "spec": stale_spec})
        assert stale_graph.status_code == 409

        srv.EXPERT_RUNS.records["run"] = {"run_id": "run", "conversation_id": "chat",
            "node_states": {"work": {"epoch": 2, "input_revision": "r2", "pending_interaction": {
                "request_id": "req", "questions": [{"name": "q", "options": [{"value": "x", "label": "X"}]}]}}}}
        stale_case = await client.post(api._ROUTE, json={"conversation_id": "chat", "run_id": "run",
            "node_id": "work", "request_id": "req", "case_id": "missing"})
        assert stale_case.status_code == 409


@pytest.mark.asyncio
async def test_saved_pending_is_reused_and_model_failure_has_safe_fallback(monkeypatch, tmp_path):
    source_run = {"run": {"run_id": "run", "conversation_id": "chat", "node_states": {
        "work": {"epoch": 1, "input_revision": "r1", "pending_interaction": {
            "request_id": "req", "source_questions": [{"name": "q", "options": [{"value": "x", "label": "X"}]}],
            "questions": [{"name": "q", "options": [{"value": "x", "label": "X"}]}]}}}}}
    srv = FakeSrv(tmp_path, runs=source_run)

    async def prepare_q(_client, *, system, user_text, total_timeout, max_generated_chars):
        if "路径发现器" in system:
            return json.dumps({"searches": []})
        return json.dumps({"questions": [{"name": "q", "choices": [
            {"id": "x", "label": "X", "kind": "value", "value": "x"},
        ]}]})

    monkeypatch.setattr(api, "collect_model_text", prepare_q)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(srv)), base_url="http://test") as client:
        initial = await client.post(api._ROUTE, json={"conversation_id": "chat", "run_id": "run",
            "node_id": "work", "request_id": "req"})
        assert initial.status_code == 200, initial.text
    # A new host instance has no in-process cache; the durable pending card is
    # the only source available for the second request.
    persisted = copy.deepcopy(srv.EXPERT_RUNS.records)
    srv = FakeSrv(tmp_path, runs=persisted)
    calls = []

    async def should_not_call(*args, **kwargs):
        calls.append(True)
        raise AssertionError("saved pending should be served from cache")

    monkeypatch.setattr(api, "collect_model_text", should_not_call)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(srv)), base_url="http://test") as client:
        response = await client.post(api._ROUTE, json={"conversation_id": "chat", "run_id": "run", "node_id": "work", "request_id": "req"})
        assert response.status_code == 200, response.text
        assert response.json()["choice_bundle"]["status"] == "ready"
        assert not calls

    failed_srv = FakeSrv(tmp_path, client=False)
    failed_calls = []

    async def failed_model(*args, **kwargs):
        failed_calls.append(True)
        raise AssertionError("the failed fallback should be reusable until refresh")

    monkeypatch.setattr(api, "collect_model_text", failed_model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(failed_srv)), base_url="http://test") as client:
        response = await client.post(api._ROUTE, json={"conversation_id": "chat", "spec": _spec()})
        assert response.status_code == 200, response.text
        value = response.json()["choice_bundle"]
        assert value["status"] == "failed"
        assert value["questions"][0]["choices"]
        assert any(choice.get("action") == "retry_prepare" for choice in value["questions"][0]["choices"])
        first_failed_count = len(failed_calls)
        repeated = await client.post(api._ROUTE, json={"conversation_id": "chat", "spec": _spec()})
        assert repeated.status_code == 200, repeated.text
        assert len(failed_calls) == first_failed_count


@pytest.mark.asyncio
async def test_concurrent_prepare_cas_and_authorization_change(monkeypatch, tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []
    srv = FakeSrv(tmp_path, runs={"run": {"run_id": "run", "conversation_id": "chat", "authorizations": {"entries": [{"auth_id": "a"}]},
        "node_states": {"work": {"epoch": 1, "input_revision": "r1", "pending_interaction": {
            "request_id": "req", "questions": [{"name": "q", "options": [{"value": "x", "label": "X"}]}]}}}}})

    async def complete(_client, *, system, user_text, total_timeout, max_generated_chars):
        calls.append(user_text)
        if len(calls) == 1:
            entered.set()
            await release.wait()
        return json.dumps({"searches": []}) if len(calls) % 2 else json.dumps({"questions": [{"name": "q", "choices": [{"id": "x", "label": "X", "value": "x"}]}]})

    monkeypatch.setattr(api, "collect_model_text", complete)
    app = _app(srv)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = asyncio.create_task(client.post(api._ROUTE, json={"conversation_id": "chat", "run_id": "run", "node_id": "work", "request_id": "req"}))
        await entered.wait()
        # The CAS sees this changed authorization summary when the model returns.
        srv.EXPERT_RUNS.records["run"]["authorizations"]["entries"].append({"auth_id": "new"})
        release.set()
        response = await first
        assert response.status_code == 409
        assert srv.EXPERT_RUNS.records["run"]["node_states"]["work"]["pending_interaction"].get("choice_bundle") is None


@pytest.mark.asyncio
async def test_graph_resolution_returns_canonical_value_without_recording_decision(monkeypatch, tmp_path):
    srv = FakeSrv(tmp_path)
    files = []
    _model(monkeypatch, srv, files=files)
    candidate = {"id": "candidate", "sequence": 1, "status": "ready", "base_snapshot": _spec(),
                 "document": {"entries": [{"id": "q", "kind": "question", "state": "active",
                     "title": "取舍", "body": "选择", "data": {"issue_type": "business", "options": [
                         {"value": "canonical", "label": "正式值"}]}}]}}
    srv.draft_state = {"candidate": candidate}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(srv)), base_url="http://test") as client:
        response = await client.post(api._ROUTE, json={"conversation_id": "chat", "candidate_id": "candidate", "sequence": 1, "spec": _spec()})
        assert response.status_code == 200, response.text
        bundle = response.json()["choice_bundle"]
    choice_id = bundle["questions"][0]["choices"][0]["id"]
    resolved = api.resolve_graph_selections(srv, [{"question_id": "q", "value": choice_id}],
        bundle["id"], bundle["version"], candidate_id="candidate", sequence=1)
    # The graph's existing option is canonical and remains available beside
    # any model candidates; the resolver maps the displayed id back to it.
    assert resolved["selections"] == [{"question_id": "q", "value": "canonical"}]
    assert resolved["prepared_options"]["q"][0]["value"] == "canonical"
