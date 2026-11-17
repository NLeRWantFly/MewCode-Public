import json

import pytest

from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.recovery_state import blockers, reconcile_saved


def new_run(tmp_path):
    store = ExpertRunStore(tmp_path)
    run_id = "a" * 32
    store.create(run_id=run_id, conversation_id="conversation", task="task",
                 attachments=[], profile={}, strategy="stable")
    return store, run_id


def test_journal_recovers_events_newer_than_snapshot_without_duplicate(tmp_path):
    store, run_id = new_run(tmp_path)
    store.append_sequenced_events(run_id, [
        {"type": "expert_delta", "seq": 1, "text": "volatile"},
        {"type": "expert_step", "seq": 2, "label": "persisted"},
    ])
    disk_snapshot = json.loads((tmp_path / f"{run_id}.json").read_text(encoding="utf-8"))
    assert disk_snapshot["last_seq"] == 0  # Event-only update did not rewrite 47 MB.
    restored = ExpertRunStore(tmp_path)
    value = restored.load(run_id)
    assert value["last_seq"] == 2
    assert [row["seq"] for row in value["events"]] == [2]
    restored.append_sequenced_events(run_id, [{"type": "expert_step", "seq": 2}])
    assert [row["seq"] for row in ExpertRunStore(tmp_path).load(run_id)["events"]] == [2]
    assert any(row["label"] == "persisted" for row in
               restored.public_for_conversation("conversation")[0]["events"])


def test_full_checkpoint_and_journal_replay_are_idempotent(tmp_path):
    store, run_id = new_run(tmp_path)
    store.append_event(run_id, {"type": "expert_step", "label": "one"})
    store.mutate(run_id, lambda snapshot: snapshot.update(status_reason="checkpoint"), durable=True)
    restored = ExpertRunStore(tmp_path).load(run_id)
    assert restored["last_seq"] == 1
    assert [row["label"] for row in restored["events"]] == ["one"]


def test_incomplete_tail_keeps_prior_committed_events_and_refuses_append(tmp_path):
    store, run_id = new_run(tmp_path)
    store.append_event(run_id, {"type": "expert_step", "label": "committed"})
    with (tmp_path / f"{run_id}.events.jsonl").open("ab") as handle:
        handle.write(b'{"journal_version":1')
    loaded = ExpertRunStore(tmp_path).load(run_id)
    assert 'event_log_incomplete_tail' not in loaded  # UI telemetry is not the kernel fact source.
    assert [row["label"] for row in loaded["events"]] == ["committed"]
    with pytest.raises(OSError, match="incomplete tail"):
        store.append_event(run_id, {"type": "expert_step", "label": "must not hide"})


def test_large_stage_message_is_externalized_and_restored_exactly(tmp_path):
    store, run_id = new_run(tmp_path)
    large_message = "原始证据🙂" * 4_000
    store.mutate(run_id, lambda snapshot: snapshot.setdefault("node_states", {}).update(
        n1={"messages": [{"role": "user", "content": large_message}]}), durable=True)
    on_disk = (tmp_path / f"{run_id}.json").read_text(encoding="utf-8")
    assert large_message not in on_disk
    marker = json.loads(on_disk)["node_states"]["n1"]["messages"][0]["content"]
    assert marker["$mewcode_large_field"] == 1
    assert marker["chars"] == len(large_message)
    assert (tmp_path / "blobs" / run_id / f"{marker['sha256']}.json").is_file()
    assert ExpertRunStore(tmp_path).load(run_id)["node_states"]["n1"]["messages"][0]["content"] == large_message


def test_missing_or_corrupt_large_field_never_restores_partial_checkpoint(tmp_path):
    store, run_id = new_run(tmp_path)
    store.mutate(run_id, lambda snapshot: snapshot.setdefault("node_states", {}).update(
        n1={"partial_text": "large" * 4_000}), durable=True)
    marker = json.loads((tmp_path / f"{run_id}.json").read_text(encoding="utf-8"))[
        "node_states"]["n1"]["partial_text"]
    blob = tmp_path / "blobs" / run_id / f"{marker['sha256']}.json"
    blob.write_bytes(b'"tampered"')
    assert ExpertRunStore(tmp_path).load(run_id)['node_states']['n1']['partial_text'] == 'large' * 4_000
    with pytest.raises(OSError, match="not persisted"):
        store.mutate(run_id, lambda snapshot: snapshot.update(status_reason="retry"), durable=True)


def test_node_knowledge_sources_remain_private(tmp_path):
    store, run_id = new_run(tmp_path)
    store.mutate(run_id, lambda snapshot: snapshot.setdefault("node_states", {}).update(
        n1={"status": "paused", "knowledge_cards": [{"text": "private route"}],
            "knowledge_observations": [{"path": "secret/path"}]}), durable=True)
    public = store.public_for_conversation("conversation")[0]
    assert "knowledge_cards" not in public["node_states"]["n1"]
    assert "knowledge_observations" not in public["node_states"]["n1"]


def test_durable_write_reports_persist_failure_instead_of_false_success(tmp_path, monkeypatch):
    store, run_id = new_run(tmp_path)
    monkeypatch.setattr(store, "_persist", lambda snapshot: (_ for _ in ()).throw(OSError("disk failure")))
    with pytest.raises(OSError, match="not persisted"):
        store.mutate(run_id, lambda snapshot: snapshot.update(status_reason="must not claim durable"),
                     durable=True)
    disk = ExpertRunStore(tmp_path).load(run_id)
    assert disk.get("status_reason") != "must not claim durable"


def test_journaled_tool_start_survives_crash_and_blocks_side_effect_replay(tmp_path):
    store, run_id = new_run(tmp_path)
    store.append_sequenced_events(run_id, [
        {"type": "expert_tool", "seq": 1, "node_id": "build", "id": "call-1",
         "name": "Bash", "args": {"command": "apply change"}},
        {"type": "expert_tool_start", "seq": 2, "node_id": "build", "id": "call-1",
         "name": "Bash", "started_at": 123.0},
    ])
    assert json.loads((tmp_path / f"{run_id}.json").read_text(encoding="utf-8"))["last_seq"] == 0
    restarted = ExpertRunStore(tmp_path)
    stage = restarted.load(run_id)["node_states"]["build"]
    assert stage["tools"]["call-1"]["args"] == {"command": "apply change"}
    assert stage["tools"]["call-1"]["status"] == "running"
    assert blockers(stage) == [{"kind": "unknown_effect", "ref": "call-1",
                               "clearance": "independent_effect_receipt",
                               "clearance_satisfied": False}]
    assert reconcile_saved(restarted, run_id, "build")["stage"]["tools"]["call-1"]["status"] == "running"


def test_journaled_tool_result_reconstructs_completed_receipt_without_snapshot(tmp_path):
    store, run_id = new_run(tmp_path)
    store.append_sequenced_events(run_id, [
        {"type": "expert_tool", "seq": 1, "node_id": "build", "member_id": "worker",
         "id": "call-2", "name": "WriteFile", "args": {"file_path": "result.txt"}},
        {"type": "expert_tool_start", "seq": 2, "node_id": "build", "member_id": "worker",
         "id": "call-2", "name": "WriteFile", "started_at": 124.0},
        {"type": "expert_tool_result", "seq": 3, "node_id": "build", "member_id": "worker",
         "id": "call-2", "name": "WriteFile", "completed_at": 125.0,
         "output": "saved", "is_error": False, "elapsed": 1.0},
    ])
    restarted = ExpertRunStore(tmp_path)
    stage = restarted.load(run_id)["members"]["build::worker"]
    assert stage["tools"]["call-2"]["status"] == "completed"
    assert stage["tools"]["call-2"]["output"] == "saved"
    assert not blockers(stage)
    # A later checkpoint must not replay the same journal entries twice.
    restarted.mutate(run_id, lambda value: value.update(status_reason="checkpoint"), durable=True)
    again = ExpertRunStore(tmp_path).load(run_id)["members"]["build::worker"]
    assert again["tools"] == stage["tools"]
