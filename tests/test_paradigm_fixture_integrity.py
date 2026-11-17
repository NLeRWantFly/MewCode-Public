"""Offline integrity checks for sanitized orchestration replay fixtures."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from mewcode.experts.flow import ExpertFlowSpec
from mewcode.orchestration.convert import from_expert_flow
from mewcode.orchestration.graph_lint import lint_graph


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "paradigm"
FIXTURE_NAMES = (
    "capability_missing_scheduler_hold.json",
    "audit_tool_recovery_pause.json",
)


@pytest.fixture(autouse=True)
def _keep_fixture_checks_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture_data_root = Path(__file__).resolve().parents[1] / ".tmp" / "paradigm-fixture-integrity"
    monkeypatch.setenv("MEWCODE_USER_DATA_DIR", str(fixture_data_root))


def _load(name: str) -> tuple[dict, str]:
    path = FIXTURE_ROOT / name
    raw = path.read_text(encoding="utf-8")
    return json.loads(raw), raw


def test_fixtures_are_local_sanitized_and_provenanced() -> None:
    absolute_path = re.compile(r"(?:[A-Za-z]:\\|\\\\(?:Users|home)\\|/(?:Users|home)/)", re.I)
    for name in FIXTURE_NAMES:
        fixture, raw = _load(name)
        assert fixture["schema_version"] == 1
        assert fixture["source"]
        assert all(
            re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
            for item in fixture["source"].values()
        )
        assert not absolute_path.search(raw)
        assert "AppData" not in raw


def test_capability_missing_fixture_preserves_recovery_and_maintenance_shape() -> None:
    fixture, _ = _load("capability_missing_scheduler_hold.json")
    counts = fixture["shape_counts"]
    assert counts["locked_flow_nodes"] == len(fixture["locked_flow"]["nodes"]) == 32
    assert counts["locked_flow_edges"] == len(fixture["locked_flow"]["edges"]) == 36
    assert counts["scheduler_decisions"] == 18
    assert counts["hold_until_clearance"] == 12
    assert counts["capability_missing_triggers"] == 12

    node = fixture["node_states"]["N03"]
    assert node["status"] == "needs_attention"
    assert [receipt["status"] for receipt in node["command_receipts"]] == [
        "outcome_unknown",
        "not_started",
    ]
    decisions = node["scheduler_recovery"]["decisions"]
    assert decisions["count"] == len(decisions["decisions"]) == 18
    assert decisions["action_counts"]["hold_until_clearance"] == 12
    assert decisions["trigger_counts"]["capability_missing"] == 12
    assert [row["source_rowid"] for row in fixture["maintenance_rows"]] == [6, 10, 445]
    assert fixture["maintenance_rows"][-1]["agent_event_count"] == 100


def test_audit_pause_fixture_preserves_counts_without_prompt_or_document_text() -> None:
    fixture, raw = _load("audit_tool_recovery_pause.json")
    counts = fixture["shape_counts"]
    state = fixture["node_states"]["N01"]
    assert counts["ReadAuditArtifact_calls"] == 71
    assert counts["pdf_paths"] == 4
    assert counts["pdf_pages"] == 1073
    assert counts["ignored_replan_signals"] == 3
    assert counts["first_user_prompt_terms"] == {
        "不得": 5,
        "不要": 4,
        "不能": 5,
        "禁止": 2,
        "不可": 0,
        "切勿": 0,
        "不应": 0,
    }
    assert counts["first_user_prompt_term_total"] == 16
    assert state["status"] == "paused"
    assert len(state["message_trace"]) == counts["N01_messages"] == 222
    assert len(state["ReadAuditArtifact_calls"]) == 71
    assert all(call["name"] == "ReadAuditArtifact" for call in state["ReadAuditArtifact_calls"])
    pdf_input = fixture["run_inputs"]["pdf_paths"]
    assert pdf_input["path_count"] == 4
    assert pdf_input["flattened_as_single_line"] is True
    assert pdf_input["value"].count("<fixture>/input_") == 4
    assert "\n" not in pdf_input["value"]
    assert "AppData" not in raw


def test_locked_flows_parse_and_audit_lint_projection_is_explicitly_derived() -> None:
    capability, _ = _load("capability_missing_scheduler_hold.json")
    capability_flow = ExpertFlowSpec.from_value(capability["locked_flow"])
    assert len(capability_flow.nodes) == capability["shape_counts"]["locked_flow_nodes"]

    fixture, _ = _load("audit_tool_recovery_pause.json")
    flow = ExpertFlowSpec.from_value(fixture["locked_flow"])
    assert len(flow.nodes) == fixture["shape_counts"]["locked_flow_nodes"] == 32
    assert len(flow.edges) == fixture["shape_counts"]["locked_flow_edges"] == 35
    assert all(
        node.model is None
        or (node.model.provider_key, node.model.model_id) == ("fixture", "fixture-mock")
        for node in flow.nodes
    )

    derived = fixture["derived_lint_inputs"]
    assert derived["derived"] is True
    assert derived["verbatim_source"] is False
    assert derived["source_evidence"]["full_scan"]["pdf_page_count"] == 1073
    assert derived["source_evidence"]["multiline_text_input"]["placeholder_path_count"] == 4

    pdf_paths = next(item for item in fixture["run_input_contract"] if item["name"] == "pdf_paths")
    assert pdf_paths["source_type_field_present"] is False
    assert pdf_paths["default"].count("<fixture>/input_") == 4
    assert pdf_paths["default"].count("\n") == 3

    spec = from_expert_flow(flow)
    findings = lint_graph(spec, fixture["run_input_contract"])
    codes = {item.code for item in findings}
    assert {
        "plan_node_full_scan",
        "known_input_placeholder",
    } <= codes
    # Missing type is now decoded as multiline when its preserved default
    # contains newlines. This derived lint projection is not source evidence.
    assert 'multiline_text_input' not in codes
