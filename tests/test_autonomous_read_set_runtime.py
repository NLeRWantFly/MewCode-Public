"""Real dispatch and acceptance retain host reads, never model claims."""
import copy

import pytest

from mewcode.expert_group import ExpertGroupOrchestrator
from mewcode.kernel import collaboration as ledger
from mewcode.tools.read_stage_result import ReadStageResult, ReadStageResultParams
from tests.test_collaboration_mode import CATALOG, PROFILE, RUN_ID, delivery, flow, store_for


@pytest.mark.asyncio
@pytest.mark.parametrize("read_mode", ["all", "partial", "none", "wrong_attempt"])
async def test_host_read_set_survives_acceptance_and_replay(tmp_path, read_mode):
    store = store_for(tmp_path)
    calls = []
    body = "冻结的上游内容" * 4000

    async def runner(node, model, prompt, policy, member):
        calls.append(node.node_id)
        if node.node_id == "work":
            return delivery(body)
        tool = ReadStageResult(policy.stage_inputs)
        current = ledger.state(store.load(RUN_ID))
        attempt = next(a for a in current["attempts"].values() if a["contract_id"] == "deliver")
        receipts = {}
        if read_mode != "none":
            offset = 0
            while True:
                result = await tool.execute(ReadStageResultParams(node_id="work", offset=offset, limit=24000))
                assert not result.is_error
                receipts[str(offset)] = {"name": "ReadStageResult", "status": "completed", "is_error": False,
                    "node_attempt": attempt["number"], "host_observation": copy.deepcopy(result.host_observation)}
                cursor = result.host_observation["read_cursor"]
                offset = cursor["next_offset"]
                if read_mode != "all" or not cursor["more"]:
                    break
        def save_receipts(record):
            record.setdefault("node_states", {})[node.node_id] = {"node_id": node.node_id,
                "node_attempt": attempt["number"] + int(read_mode == "wrong_attempt"),
                "input_revision": policy.input_revision, "tools": receipts}
        store.mutate(RUN_ID, save_receipts, durable=True)
        result = delivery("已形成下游交付")
        result.handoff["read_set"] = {"coverage": "complete", "reads": [{"node_id": "forged"}]}
        return result

    async def review(model, payload, context):
        return {"passed": True, "evidence": ["独立检查覆盖目标"]}

    host = ExpertGroupOrchestrator(runner=None, node_runner=runner, run_store=store,
        work_dir=tmp_path, archive_root=tmp_path / "archive", collaboration_review=review)
    result = await host.run("交付测试结果", PROFILE, CATALOG, strategy="stable", run_id=RUN_ID,
                            flow=flow(), checkpoint=store.load(RUN_ID))
    assert result.status == "completed"
    current = ledger.state(store.load(RUN_ID))
    certificate = current["certificates"]["deliver"]
    attempt = current["attempts"][certificate["attempt_id"]]
    observed = attempt["read_set"]
    assert observed["coverage"] == {"all": "complete", "partial": "partial",
                                     "none": "none", "wrong_attempt": "none"}[read_mode]
    assert certificate["read_set"] == observed
    assert all(row["node_id"] == "work" for row in observed["reads"])
    # Semantic dependencies stay conservative even when no read was recorded.
    assert current["contracts"]["deliver"]["bound_dependencies"] == ["work"]
    saved = copy.deepcopy(certificate)
    replay = await host.run("交付测试结果", PROFILE, CATALOG, strategy="stable", run_id=RUN_ID,
                            flow=flow(), checkpoint=store.load(RUN_ID))
    assert replay.status == "completed"
    assert calls == ["work", "deliver"]
    assert ledger.state(store.load(RUN_ID))["certificates"]["deliver"] == saved
