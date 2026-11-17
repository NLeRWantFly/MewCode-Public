import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from frontend import server
from mewcode.expert_runs import ExpertRunStore
from tests.test_expert_retry import blocked_run


def test_immutable_checkpoint_and_node_object_deduplication(tmp_path):
    store, run_id, _ = blocked_run(tmp_path)
    point = store.save_turn_checkpoint(run_id)
    original_bytes = store.checkpoint_archive.paths(run_id, point["checkpoint_id"])[1].read_bytes()
    assert store.save_turn_checkpoint(run_id) == point
    assert len(store.checkpoint_archive.list(run_id)) == 1
    old_meta, old = store.checkpoint_archive.load(run_id, point["checkpoint_id"])
    store.mutate(run_id, lambda s: s["node_states"]["repair"].update(text="new output"))
    new = store.save_turn_checkpoint(run_id, turn_key="next-turn")
    assert new["checkpoint_id"] != point["checkpoint_id"]
    assert store.checkpoint_archive.load(run_id, point["checkpoint_id"])[1] == old
    assert store.checkpoint_archive.paths(run_id, point["checkpoint_id"])[1].read_bytes() == original_bytes
    # Three original nodes + just the modified node, not six copies.
    assert len(list((store.checkpoint_archive.folder(run_id) / "objects").glob("*.gz"))) == 4


@pytest.mark.parametrize("status", ["completed", "blocked", "paused", "terminated", "needs_attention"])
def test_each_stopped_turn_is_archived_and_restored_without_execution(tmp_path, status):
    store, run_id, _ = blocked_run(tmp_path)
    store.mutate(run_id, lambda s: s.update(status=status))
    point = store.save_turn_checkpoint(run_id)
    before = store.load(run_id)
    paper = tmp_path / "paper.txt"
    paper.write_text("newest file content", encoding="utf-8")
    store.mutate(run_id, lambda s: s.update(
        status="completed", attempt=4, last_seq=42,
        node_states={"prepare": {"status": "completed", "text": "later result"}},
        events=[{"type": "later_event", "seq": 42}], usage={"actual_spend": 100},
    ))
    current = store.load(run_id)
    restored = store.restore_turn_checkpoint(run_id, point["checkpoint_id"], conversation_id="c",
                                              expected_updated_at=current["updated_at"])
    assert restored["node_states"] == before["node_states"]
    assert restored["run_id"] == run_id and restored["attempt"] == 4
    assert restored["status"] != "running"
    assert restored["events"][0] == current["events"][0]
    assert restored["usage"] == current["usage"]
    assert restored["last_seq"] == 43
    assert paper.read_text(encoding="utf-8") == "newest file content"
    backup_id = restored["checkpoint_restorations"][-1]["backup_checkpoint_id"]
    backup_meta, backup = store.checkpoint_archive.load(run_id, backup_id)
    assert backup_meta["kind"] == "before_restore"
    assert backup["node_states"] == current["node_states"]


@pytest.mark.parametrize("node_status", ["completed", "review_pending", "failed", "needs_attention", "budget_exhausted"])
def test_substage_checkpoint_is_safe_while_parent_run_is_running(tmp_path, node_status):
    store, run_id, _ = blocked_run(tmp_path)
    store.mutate(run_id, lambda s: (s.update(status="running"),
                                   s["node_states"]["repair"].update(status=node_status)))
    point = store.save_turn_checkpoint(run_id, kind="node_end", node_id="repair", turn_key="node-1")
    # Execution completion alone does not mean a passed acceptance check.
    assert point["status"] == ("needs_attention" if node_status == "completed" else node_status)
    assert point["run_status"] == "running"
    with pytest.raises(ValueError, match="暂停"):
        store.restore_turn_checkpoint(run_id, point["checkpoint_id"], conversation_id="c",
                                      expected_updated_at=store.load(run_id)["updated_at"])
    store.transition(run_id, "paused")
    restored = store.restore_turn_checkpoint(run_id, point["checkpoint_id"], conversation_id="c",
                                              expected_updated_at=store.load(run_id)["updated_at"])
    assert restored["status"] == "paused"
    assert restored["node_states"]["repair"]["status"] == node_status


def test_restore_guards_scope_staleness_and_corrupt_objects(tmp_path):
    store, run_id, _ = blocked_run(tmp_path)
    point = store.save_turn_checkpoint(run_id)
    before = store.load(run_id)
    for conversation_id, updated_at in [("other", before["updated_at"]), ("c", "stale")]:
        with pytest.raises(ValueError):
            store.restore_turn_checkpoint(run_id, point["checkpoint_id"], conversation_id=conversation_id,
                                          expected_updated_at=updated_at)
        assert store.load(run_id) == before
    with pytest.raises(ValueError):
        store.checkpoint_archive.load(run_id, "../../outside")
    blob = next((store.checkpoint_archive.folder(run_id) / "objects").glob("*.gz"))
    blob.write_bytes(b"invalid gzip")
    with pytest.raises((ValueError, OSError)):
        store.restore_turn_checkpoint(run_id, point["checkpoint_id"], conversation_id="c",
                                      expected_updated_at=before["updated_at"])
    assert store.load(run_id) == before


def test_legacy_backfill_only_has_known_latest_state_and_delete_cleans_archive(tmp_path):
    store, run_id, _ = blocked_run(tmp_path)
    assert store.backfill_latest_checkpoints() == 1
    assert store.backfill_latest_checkpoints() == 0
    assert len(store.checkpoint_archive.list(run_id)) == 1
    assert store.delete_conversation("c") == 1
    assert not store.checkpoint_archive.folder(run_id).exists()


@pytest.mark.asyncio
async def test_checkpoint_api_never_starts_tasks_and_rejects_active_restore(tmp_path, monkeypatch):
    store, run_id, _ = blocked_run(tmp_path)
    point = store.save_turn_checkpoint(run_id)
    monkeypatch.setattr(server, "EXPERT_RUNS", store)
    runtime = SimpleNamespace(session=SimpleNamespace(session_id="c"), chat_busy=lambda _: False)
    monkeypatch.setattr(server, "SESSION", runtime)
    publish = AsyncMock()
    monkeypatch.setattr(server, "WS_EVENTS", SimpleNamespace(send_json=publish))
    base = f"/api/expert-runs/{run_id}/checkpoints"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        listing = await client.get(base, params={"conversation_id": "c"})
        assert listing.status_code == 200
        data = listing.json()
        assert len(data["checkpoints"]) == 1 and "node_states" not in data
        assert (await client.get(base, params={"conversation_id": "other"})).status_code == 404
        body = {"conversation_id": "c", "expected_updated_at": data["updated_at"]}
        runtime.chat_busy = lambda _: True
        assert (await client.post(base + f"/{point['checkpoint_id']}/restore", json=body)).status_code == 409
        runtime.chat_busy = lambda _: False
        response = await client.post(base + f"/{point['checkpoint_id']}/restore", json=body)
        assert response.status_code == 200, response.text
        assert response.json()["snapshot"]["status"] != "running"
        assert publish.call_args.args[0]["type"] == "expert_checkpoint_restored"
        assert (await client.post(base + f"/{point['checkpoint_id']}/restore", json=body)).status_code == 409


def test_history_replay_cannot_replace_restored_checkpoint_progress():
    source = (Path(__file__).resolve().parents[1] / "frontend/static/app.js").read_text(encoding="utf-8")
    render = source.split("function renderExpertSnapshot(snapshot)", 1)[1].split("async function loadExpertRoleDetails", 1)[0]
    assert render.rindex("hydrateExpertTrajectory(snapshot)") > render.index("for (const rawEvent")
