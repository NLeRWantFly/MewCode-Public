import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from mewcode.expert_runs import ExpertRunStore, _normalize_snapshot


MARKER = "候选修复已结束，以审计接受记录为准。"
ERROR = "API error (502): Upstream access forbidden"


def test_frontend_status_semantics():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable")
    result = subprocess.run([node, str(Path(__file__).with_name("orch_status.cjs"))],
                            capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def legacy_snapshot():
    base = dict(node_id="repair", type="expert_node_completed", attempt=16, node_attempt=1)
    return dict(version=2, run_id="a" * 32, conversation_id="c", status="blocked",
                node_states={"repair": dict(status="skipped", text=MARKER, passed=False),
                             "executor": dict(status="completed", text="delivered", passed=True)},
                events=[dict(base, status="review_pending", transcript=ERROR),
                        dict(base, status="skipped", transcript=MARKER)])


def test_legacy_repair_error_is_recovered_without_mutating_disk(tmp_path):
    snapshot = legacy_snapshot()
    path = tmp_path / (snapshot["run_id"] + ".json")
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    before = path.read_bytes()
    store = ExpertRunStore(tmp_path)
    loaded = store.load(snapshot["run_id"])
    assert loaded["node_states"]["repair"]["status"] == "failed"
    assert loaded["node_states"]["repair"]["text"] == ERROR
    assert loaded["stages"]["repair"]["status"] == "failed"
    assert loaded["events"][-1]["transcript"] == ERROR
    assert loaded["events"][-1]["status"] == "failed"
    assert all(loaded["node_states"]["executor"][key] == value
               for key, value in snapshot["node_states"]["executor"].items())
    assert _normalize_snapshot(copy.deepcopy(loaded)) == loaded
    assert path.read_bytes() == before


def test_legacy_projection_does_not_guess_success_or_reuse_old_attempt_error():
    snapshot = legacy_snapshot()
    snapshot["events"][0]["attempt"] = 15
    loaded = _normalize_snapshot(snapshot)
    assert loaded["node_states"]["repair"]["status"] == "needs_attention"
    assert ERROR not in loaded["node_states"]["repair"]["text"]


def test_real_branch_skip_is_not_rewritten():
    snapshot = legacy_snapshot()
    snapshot["node_states"]["repair"].update(text="", reason="branch_not_taken")
    assert _normalize_snapshot(snapshot)["node_states"]["repair"]["status"] == "skipped"


def test_public_cache_without_new_projection_is_invalidated(tmp_path):
    snapshot = legacy_snapshot()
    path = tmp_path / (snapshot["run_id"] + ".json")
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    stat = path.stat()
    (tmp_path / (snapshot["run_id"] + ".public.json")).write_text(json.dumps(dict(
        source_mtime_ns=stat.st_mtime_ns, source_size=stat.st_size, snapshot_version=2,
        conversation_id="c", summary={"node_states": snapshot["node_states"]},
    )), encoding="utf-8")
    public = ExpertRunStore(tmp_path).public_for_conversation("c")[0]
    assert public["node_states"]["repair"]["status"] == "failed"
    assert public["node_states"]["repair"]["status_reason"] == ERROR
