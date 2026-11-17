import copy
import hashlib
import json

import pytest
from PIL import Image

from mewcode.expert_runs import ExpertRunStore
from mewcode.tools.read_audit_artifact import ReadAuditArtifact, ReadAuditArtifactParams
from mewcode.tools.review_work_queue import ReviewWorkQueue, ReviewWorkQueueParams
from mewcode.tools.submit_loop_decision import SubmitLoopDecisionTool, SubmitLoopDecisionParams
from mewcode.tools.submit_verdict import VerdictHolder
from tests.test_generic_loops import spec_for
from mewcode.orchestration.convert import to_expert_flow


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def setup_queue(tmp_path):
    flow = to_expert_flow(spec_for())
    store = ExpertRunStore(tmp_path / "runs")
    run_id = "e" * 32
    store.create(run_id=run_id, conversation_id="chat", task="review", attachments=[], profile={},
                 strategy="stable", flow=flow)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"pages": [{"doc_id": "book", "pdf_page": p, "tier": t}
        for p, t in [(1, "T2"), (2, "T3"), (3, "T1")]]}), encoding="utf-8")
    for p in range(1, 4):
        Image.new("RGB", (12, 12), (p * 30, 0, 0)).save(tmp_path / f"page_{p:04}.png")
    for name in ("left.md", "right.md"):
        (tmp_path / name).write_text("actual source text", encoding="utf-8")
    tool = ReviewWorkQueue(store, run_id, "check", tmp_path, "frozen")
    init = ReviewWorkQueueParams(action="initialize", manifest_path="manifest.json", manifest_sha256=digest(manifest),
        items_path="pages", filters={"tier": ["T2", "T3"]}, expected_count=2,
        image_path_template="page_{pdf_page:04d}.png")
    return tool, init, store, run_id


async def submitted_candidate(tmp_path, *, diff_suffix=".txt", candidate_text=None):
    from mewcode.tools.repair_work_batch import RepairWorkBatch, RepairWorkBatchParams
    from mewcode.tools.read_audit_artifact import ReadAuditArtifact, ReadAuditArtifactParams

    reviewer, init, store, run_id = setup_queue(tmp_path)
    assert not (await reviewer.execute(init)).is_error
    queue = store.load(run_id)["review_queues"]["check"]
    item_id = queue["items"][0]["item_id"]
    source_receipts = [{"path": name, "sha256": digest(tmp_path / name),
                        "quote": "actual source text"}
                       for name in ("left.md", "right.md")]
    def seed(snapshot):
        saved = snapshot["review_queues"]["check"]
        saved["records"][item_id] = {"state": "needs_repair", "summary": "verified defect",
            "issue_ids": [item_id + "-issue"], "review_round": 0,
            "evidence": copy.deepcopy(source_receipts), "images": []}
        saved["revision"] += 1
    store.mutate(run_id, seed, durable=True)

    repair = RepairWorkBatch(store, run_id, ["check"], "frozen", tmp_path, "repair")
    frozen = await repair.execute(RepairWorkBatchParams(action="freeze", reviewer_node_id="check",
        expected_revision=1, item_ids=[item_id]))
    assert not frozen.is_error, frozen.output
    batch = json.loads(frozen.output)

    candidate_path = tmp_path / f"{item_id}-candidate.md"
    preview_path = tmp_path / f"{item_id}-preview.png"
    Image.new("RGB", (12, 12), (0, 160, 0)).save(preview_path)
    candidate_path.write_text(candidate_text or f"# Candidate\n\n![page]({preview_path.name})\n",
                              encoding="utf-8")
    diff_path = tmp_path / f"{item_id}-diff{diff_suffix}"
    diff_path.write_text("not an applicable patch" if diff_suffix == ".patch" else "diff", encoding="utf-8")
    evidence_path = tmp_path / f"{item_id}-evidence.txt"
    evidence_path.write_text("evidence", encoding="utf-8")
    candidate = {"item_id": item_id, "issue_ids": [item_id + "-issue"],
        "candidate": {"path": candidate_path.name, "sha256": digest(candidate_path)},
        "diff": {"path": diff_path.name, "sha256": digest(diff_path)},
        "evidence": {"path": evidence_path.name, "sha256": digest(evidence_path)}}
    submitted = await repair.execute(RepairWorkBatchParams(action="submit", reviewer_node_id="check",
        batch_id=batch["batch_id"], candidates=[candidate]))
    assert not submitted.is_error, submitted.output
    saved_candidate = store.load(run_id)["review_queues"]["check"]["repair_candidates"][item_id]

    reader = ReadAuditArtifact([str(tmp_path)])
    reader.image_observer = reviewer.observe_image
    image_path = tmp_path / queue["items"][0]["image_path"]
    assert not (await reader.execute(ReadAuditArtifactParams(file_path=str(image_path),
        criterion_id=item_id))).is_error
    evidence_ids = []
    for path in ("left.md", "right.md", candidate_path.name):
        receipt = await reviewer.execute(ReviewWorkQueueParams(action="evidence", item_id=item_id,
            source_path=path))
        assert not receipt.is_error, receipt.output
        evidence_ids.append(json.loads(receipt.output)["evidence_id"])
    record = ReviewWorkQueueParams(action="record", item_id=item_id, candidate_revision=saved_candidate[
        "candidate_revision"], state="complete", summary="independent review complete",
        evidence_ids=evidence_ids)
    return reviewer, store, run_id, item_id, diff_path, image_path, preview_path, record


@pytest.mark.asyncio
async def test_queue_is_durable_idempotent_and_full_scope(tmp_path):
    tool, init, store, run_id = setup_queue(tmp_path)
    assert not (await tool.execute(init)).is_error
    before = copy.deepcopy(store.load(run_id)["review_queues"])
    assert not (await tool.execute(init)).is_error
    assert store.load(run_id)["review_queues"] == before
    queue = before["check"]
    assert len(queue["items"]) == 2 and queue["source_count"] == 3
    narrowed = init.model_copy(update={"filters": {"tier": ["T2"]}, "expected_count": 1})
    assert (await tool.execute(narrowed)).is_error
    reloaded = ReviewWorkQueue(ExpertRunStore(tmp_path / "runs"), run_id, "check", tmp_path, "frozen")
    result = await reloaded.execute(ReviewWorkQueueParams(action="next", limit=1))
    assert len(json.loads(result.output)["items"]) == 1
    assert reloaded.progress()["total"] == 2


@pytest.mark.asyncio
async def test_record_requires_exact_page_and_two_sources_then_resumes_remaining(tmp_path):
    tool, init, store, run_id = setup_queue(tmp_path)
    await tool.execute(init)
    items = json.loads((await tool.execute(ReviewWorkQueueParams(action="next"))).output)["items"]
    record = ReviewWorkQueueParams(action="record", item_id=items[0]["item_id"], state="complete",
        summary="two sources match actual page", evidence=[{"path": name, "sha256": digest(tmp_path / name),
            "quote": "actual source text"} for name in ("left.md", "right.md")])
    assert (await tool.execute(record)).is_error
    reader = ReadAuditArtifact([str(tmp_path)])
    reader.image_observer = tool.observe_image
    # A neighbour's pixels cannot establish coverage for this item.
    await reader.execute(ReadAuditArtifactParams(file_path=str(tmp_path / "page_0002.png"), criterion_id=record.item_id))
    assert (await tool.execute(record)).is_error
    image = await reader.execute(ReadAuditArtifactParams(file_path=str(tmp_path / "page_0001.png"), criterion_id=record.item_id))
    assert not image.is_error
    fabricated = {**record.model_dump(), 'evidence': [
        {**e.model_dump(), 'quote': 'not present in actual source'} for e in record.evidence]}
    assert (await tool.execute(ReviewWorkQueueParams.model_validate(fabricated))).is_error
    assert not (await tool.execute(record)).is_error
    revision = tool.progress()["revision"]
    assert not (await tool.execute(record)).is_error
    assert tool.progress()["revision"] == revision
    next_items = json.loads((await tool.execute(ReviewWorkQueueParams(action="next"))).output)["items"]
    assert [item["item_id"] for item in next_items] == [items[1]["item_id"]]
    assert tool.exit_blocker()
    unknown = ReviewWorkQueueParams(action="record", item_id=items[1]["item_id"], state="unknown", summary="not yet verified")
    assert not (await tool.execute(unknown)).is_error
    assert tool.progress()["unknown"] == 1 and tool.exit_blocker()
    await reader.execute(ReadAuditArtifactParams(file_path=str(tmp_path / "page_0002.png"), criterion_id=items[1]["item_id"]))
    assert not (await tool.execute(record.model_copy(update={"item_id": items[1]["item_id"]}))).is_error
    assert not tool.exit_blocker()
    (tmp_path / "right.md").write_text("changed", encoding="utf-8")
    assert "变化" in tool.exit_blocker()
    public = ExpertRunStore._public_value(store.load(run_id), include_details=False)
    assert "review_queues" not in public
    assert public["review_progress"]["check"]["complete"] == 0
    pending = json.loads((await tool.execute(ReviewWorkQueueParams(action="next"))).output)["items"]
    assert len(pending) == 2 and pending[0]["previous"]["state"] == "unknown"
    repaired = ReviewWorkQueueParams.model_validate({**record.model_dump(), "evidence": [
        {"path": name, "sha256": digest(tmp_path / name), "quote": (tmp_path / name).read_text(encoding='utf-8')}
        for name in ("left.md", "right.md")]})
    assert not (await tool.execute(repaired)).is_error
    assert tool.progress()["complete"] == 1


@pytest.mark.asyncio
async def test_changed_manifest_or_contract_does_not_erase_progress(tmp_path):
    tool, init, store, run_id = setup_queue(tmp_path)
    await tool.execute(init)
    changed = ReviewWorkQueue(store, run_id, "check", tmp_path, "new-contract")
    assert (await changed.execute(ReviewWorkQueueParams(action="next"))).is_error
    (tmp_path / "manifest.json").write_text("[]", encoding="utf-8")
    assert (await tool.execute(ReviewWorkQueueParams(action="next"))).is_error
    assert len(store.load(run_id)["review_queues"]["check"]["items"]) == 2


@pytest.mark.asyncio
async def test_candidate_record_runs_host_integrity_and_rendering_preflights(tmp_path):
    reviewer, store, run_id, item_id, _, _, preview_path, record = await submitted_candidate(tmp_path)
    result = await reviewer.execute(record)
    assert not result.is_error, result.output
    saved = store.load(run_id)["review_queues"]["check"]["records"][item_id]
    assert saved["candidate_revision"] == record.candidate_revision
    assert saved["rendered_images"][0]["sha256"] == digest(preview_path)
    Image.new("RGB", (12, 12), (180, 0, 0)).save(preview_path)
    assert reviewer.exit_blocker()
    stale = store.load(run_id)["review_queues"]["check"]["records"][item_id]
    assert stale["state"] == "unknown" and stale["evidence_stale"] is True


@pytest.mark.asyncio
async def test_candidate_record_rejects_unapplicable_patch(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (tmp_path / "tracked.txt").write_text("current\n", encoding="utf-8")
    reviewer, store, run_id, item_id, _, _, _, record = await submitted_candidate(
        tmp_path, diff_suffix=".patch")
    result = await reviewer.execute(record)
    assert result.is_error and "候选宿主完整性预检失败" in result.output
    assert item_id not in store.load(run_id)["review_queues"]["check"]["records"] or \
        store.load(run_id)["review_queues"]["check"]["records"][item_id]["state"] not in {"complete", "pass"}


@pytest.mark.asyncio
async def test_candidate_record_rejects_unrenderable_linked_image(tmp_path):
    reviewer, store, run_id, item_id, _, _, preview_path, record = await submitted_candidate(tmp_path)
    preview_path.write_bytes(b"not a decodable image")
    result = await reviewer.execute(record)
    assert result.is_error and "候选渲染预检失败" in result.output
    assert item_id not in store.load(run_id)["review_queues"]["check"]["records"] or \
        store.load(run_id)["review_queues"]["check"]["records"][item_id]["state"] not in {"complete", "pass"}


@pytest.mark.asyncio
async def test_candidate_record_rejects_artifact_hash_change(tmp_path):
    reviewer, store, run_id, item_id, diff_path, _, _, record = await submitted_candidate(tmp_path)
    diff_path.write_text("changed after submission", encoding="utf-8")
    result = await reviewer.execute(record)
    assert result.is_error and "候选宿主完整性预检失败" in result.output
    assert item_id not in store.load(run_id)["review_queues"]["check"]["records"] or \
        store.load(run_id)["review_queues"]["check"]["records"][item_id]["state"] not in {"complete", "pass"}


@pytest.mark.asyncio
async def test_exit_guard_does_not_accept_model_claim_over_missing_records(tmp_path):
    tool, init, _, _ = setup_queue(tmp_path)
    await tool.execute(init)
    holder = VerdictHolder()
    decider = SubmitLoopDecisionTool(holder, {"contract_hash": "frozen", "criteria": [{"criterion_id": "A"}]})
    decider.completion_guard = tool.exit_blocker
    result = await decider.execute(SubmitLoopDecisionParams(contract_hash="frozen", decision="EXIT",
        checks=[{"criterion_id": "A", "state": "PASS", "evidence": "claimed", "location": "somewhere"}]))
    assert result.is_error and holder.read() is None


@pytest.mark.parametrize("tools, expected", [(["SubmitLoopDecision", "ReadAuditArtifact", "ReviewWorkQueue"], "SubmitLoopDecision"),
                                           (["SubmitVerdict"], "SubmitVerdict")])
def test_auditor_prompt_only_requires_registered_delivery_tools(tools, expected):
    from mewcode.experts.delivery import delivery_prompt
    prompt = delivery_prompt("auditor", tools)
    assert expected in prompt
    assert "SubmitHandoff" not in prompt
    assert "file_requests" not in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize('repair_mode', [False, True])
async def test_web_resume_uses_saved_queue_not_old_transcript(tmp_path, monkeypatch, repair_mode):
    import asyncio
    from dataclasses import replace
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from mewcode import expert_web
    from mewcode.agent import Agent, StreamText, TurnComplete
    from mewcode.tools.review_work_queue import ReadReviewQueueParams
    from mewcode.conversation import ConversationManager
    from mewcode.experts.flow import ExpertFlowStore
    from mewcode.orchestration.plan_session import PlanSessionStore
    from mewcode.permissions.policy import build_permission_checker
    from mewcode.tools.submit_handoff import SubmitHandoffParams
    from tests.test_expert_children import FakeSession, make_registry
    from tests.test_agent import MockLLMClient
    from tests.test_expert_flow import _profile
    from tests.test_generic_loops import report

    _, init, runs, _ = setup_queue(tmp_path)
    spec = spec_for()
    spec = replace(spec, nodes=tuple(replace(node, capabilities=(
        {'write_paths': ['right.md']} if node.node_id == 'work' and repair_mode else {})) for node in spec.nodes))
    spec.meta['loops'][0]['control_mode'] = 'team'
    flow = to_expert_flow(spec)
    flows = ExpertFlowStore(tmp_path / 'flows.json')
    flows.put(tmp_path, flow)
    calls, events = [], []
    class FakeAgent(Agent):
        def __init__(self, role, policy, holder):
            host = FakeSession(make_registry())
            host.work_dir = str(tmp_path)
            registry = expert_web._role_registry(host, role, policy, holder)
            super().__init__(MockLLMClient([]), registry, 'openai', str(tmp_path),
                             permission_checker=build_permission_checker(tmp_path))
            self.policy = policy
            self.notification_fn = None
        def set_review_authorization_context(self, context):
            self.node_id = context.split('STAGE: ', 1)[1].split(' ', 1)[0]
        async def run(self, conversation):
            calls.append(self.node_id)
            if self.node_id == 'work':
                assert all(route['request_tool'] == 'SubmitHandoff' for route in self.verification_routes)
                assert bool(self.registry.get('RepairWorkBatch')) == (repair_mode and calls.count('work') == 2)
            if self.node_id != 'check':
                if repair_mode and self.node_id == 'work' and calls.count('work') == 2:
                    worklist = await self.registry.get('ReadReviewQueue').execute(ReadReviewQueueParams(reviewer_node_id='check'))
                    assert json.loads(worklist.output)['total_unresolved'] == 2
                    (tmp_path / 'right.md').write_text('repaired candidate text', encoding='utf-8')
                assert not (await self.registry.get('SubmitHandoff').execute(
                    SubmitHandoffParams(summary='done'))).is_error
            else:
                queue = self.registry.get('ReviewWorkQueue')
                assert queue is not None and self.registry.get('SubmitHandoff') is None
                assert bool(self.registry.get('VerifyArtifacts')) == (calls.count('check') > 1)
                reader = self.registry.get('ReadAuditArtifact')
                if calls.count('check') == 1:
                    assert not (await queue.execute(init)).is_error
                else:
                    assert 'OLD_HUGE_TRANSCRIPT' not in str(conversation.get_messages())
                    assert queue.progress()['complete'] == (0 if repair_mode else 1)
                items = json.loads((await queue.execute(ReviewWorkQueueParams(action='next'))).output)['items']
                for item in items if repair_mode else items[:1]:
                    assert not (await reader.execute(ReadAuditArtifactParams(
                        file_path=str(tmp_path / item['image_path']), criterion_id=item['item_id']))).is_error
                    assert not (await queue.execute(ReviewWorkQueueParams(action='record', item_id=item['item_id'],
                        state='needs_repair' if repair_mode and calls.count('check') == 1 else 'complete',
                        summary='verified pixels and two texts', evidence=[
                            {'path': name, 'sha256': digest(tmp_path / name), 'quote': (tmp_path / name).read_text(encoding='utf-8')}
                            for name in ('left.md', 'right.md')]))).is_error
                if calls.count('check') == 1:
                    if repair_mode:
                        # Host relays completed review pass, without a model inventing PASS.
                        yield TurnComplete(turn=1)
                        return
                    conversation.add_assistant_message('OLD_HUGE_TRANSCRIPT')
                    # Exercise a user pause. Provider interruptions may now
                    # recover automatically and do not guarantee a pause.
                    raise asyncio.CancelledError()
                assert not (await self.registry.get('SubmitLoopDecision').execute(
                    SubmitLoopDecisionParams(**report(self.policy.loop_context)))).is_error
            yield StreamText(text='done')
    def build(_s, _r, role, _m, policy, holder):
        return FakeAgent(role, policy, holder), None, SimpleNamespace(conversation=ConversationManager(), aclose=AsyncMock())
    monkeypatch.setattr(expert_web, '_build_role_agent', build)
    session = SimpleNamespace(work_dir=str(tmp_path), conversation_id='queue-web', multi_agent=False,
        session=SimpleNamespace(meta=SimpleNamespace(message_count=0), append=lambda *a: None,
            append_expert_run=lambda *a, **kw: None), conversation=ConversationManager(), expert_run_id='',
        expert_control_action='pause', context=SimpleNamespace(get=lambda key, default=None: default,
            slots=SimpleNamespace(resolve=lambda key: {})),
        task_manager=SimpleNamespace(list_tasks=lambda: [], mark_notified=lambda ids: None),
        expert_child_event_callback=None, status_payload=lambda: {}, tasks_payload=lambda: [])
    async def send(event):
        events.append(copy.deepcopy(event))
    kwargs = dict(session=session,
        registry_service=SimpleNamespace(catalog=AsyncMock(return_value={'providers': []}), resolve=lambda model: None),
        profile_store=SimpleNamespace(get=lambda _: _profile()), flow_store=flows,
        plan_store=PlanSessionStore(tmp_path / 'plans.json'), run_store=runs)
    ws = SimpleNamespace(send_json=send)
    await expert_web.run_expert_chat(ws, '逐页复核', confirmed_new_flow=flow, **kwargs)
    run_id = next(e['run_id'] for e in events if e.get('run_id'))
    if repair_mode:
        assert calls == ['prepare', 'work', 'check', 'work', 'check', 'done']
    else:
        assert calls == ['prepare', 'work', 'check']
        assert runs.load(run_id)['status'] == 'paused'
        await expert_web.run_expert_chat(ws, '继续', resume_run_id=run_id, **kwargs)
        assert calls == ['prepare', 'work', 'check', 'check', 'done']
    assert runs.load(run_id)['status'] == 'completed'
    records = runs.load(run_id)['review_queues']['check']['records']
    assert len(records) == 2 and all(r['state'] == 'pass' for r in records.values())
