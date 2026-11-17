from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError

from mewcode.tools.submit_verdict import (
    VERDICT_NEEDS_ATTENTION,
    VERDICT_PASS,
    SubmitVerdictParams,
    SubmitVerdictTool,
    VerdictHolder,
)


def make_params(**kwargs):
    defaults = {
        "verdict": VERDICT_PASS,
        "issues": [],
        "evidence": [],
    }
    defaults.update(kwargs)
    return SubmitVerdictParams(**defaults)


def test_holder_round_trips() -> None:
    holder = VerdictHolder()
    assert holder.read() is None
    holder.submit({"verdict": "PASS", "issues": [], "evidence": []})
    assert holder.read() == {"verdict": "PASS", "issues": [], "evidence": []}


@pytest.mark.parametrize('mutation', [None, 'boolean_check', 'missing_location', 'wrong_state', 'unknown_assessment_field'])
def test_compatibility_wire_keeps_nested_verdict_constraints_without_references(mutation):
    from jsonschema import Draft202012Validator
    from mewcode.client import OpenAICompatClient
    from mewcode.tools import ToolRegistry
    registry = ToolRegistry()
    registry.register(SubmitVerdictTool())
    wire = OpenAICompatClient._convert_tools(registry.get_all_schemas('openai'))[0]['function']['parameters']
    assert '$ref' not in json.dumps(wire) and '$defs' not in wire
    item = wire['properties']['assessment']['anyOf'][0]['properties']['checks']['items']
    assert item['type'] == 'object' and set(item['required']) == {'criterion_id', 'state', 'evidence', 'location'}
    payload = {'verdict': 'PASS', 'assessment': {'rubric_hash': 'r', 'artifact_hash': 'a', 'checks': [
        {'criterion_id': 'C-1', 'state': 'PASS', 'evidence': 'Observed matching pixels', 'location': 'page.png'}]},
        'reviewed_decisions': {'saved-answer': {'applicability': 'original_scope_only', 'reason': 'Same pilot scope'}}}
    if mutation == 'boolean_check':
        payload['assessment']['checks'] = [True]
    elif mutation == 'missing_location':
        payload['assessment']['checks'][0].pop('location')
    elif mutation == 'wrong_state':
        payload['assessment']['checks'][0]['state'] = 'OK'
    elif mutation == 'unknown_assessment_field':
        payload['assessment']['unreviewed_override'] = True
    before = copy.deepcopy(payload)
    original = Draft202012Validator(SubmitVerdictParams.model_json_schema())
    delivered = Draft202012Validator(wire)
    assert original.is_valid(payload) == delivered.is_valid(payload) == (mutation is None)
    if mutation:
        with pytest.raises(ValidationError):
            SubmitVerdictParams.model_validate(payload)
    else:
        SubmitVerdictParams.model_validate(payload)
    assert payload == before


@pytest.mark.asyncio
async def test_tool_stores_structured_verdict() -> None:
    holder = VerdictHolder()
    tool = SubmitVerdictTool(holder)
    result = await tool.execute(
        make_params(
            verdict=VERDICT_NEEDS_ATTENTION,
            issues=[{
                "issue_id": "TEST-01",
                "file": "mewcode/x.py",
                "description": "测试失败",
                "resolution": "producer_fix",
                "acceptance_test": "pytest tests/test_x.py exits 0",
            }],
            evidence=[
                {
                    "kind": "test",
                    "command": "pytest tests/test_x.py",
                    "exit_code": 1,
                    "summary": "1 failed",
                }
            ],
        )
    )
    assert result.is_error is False
    assert "结论已提交：NEEDS_ATTENTION" in result.output
    stored = holder.read()
    assert stored is not None
    assert stored["verdict"] == VERDICT_NEEDS_ATTENTION
    assert stored["issues"][0]["issue_id"] == "TEST-01"
    assert stored["issues"][0]["file"] == "mewcode/x.py"
    assert stored["evidence"][0]["exit_code"] == 1


@pytest.mark.asyncio
async def test_tool_defaults_to_pass_with_empty_lists() -> None:
    holder = VerdictHolder()
    tool = SubmitVerdictTool(holder)
    result = await tool.execute(make_params())
    assert result.is_error is False
    assert holder.read() == {"verdict": "PASS", "issues": [], "evidence": []}


@pytest.mark.asyncio
async def test_unclassified_new_findings_cannot_be_submitted():
    holder = VerdictHolder()
    tool = SubmitVerdictTool(holder)
    result = await tool.execute(make_params(verdict='NEEDS_ATTENTION', issues=[{
        'file': 'pilot.json', 'description': 'The provider does not expose this receipt.'}]))
    assert result.is_error and 'capability_gap' in result.output and holder.read() is None
    result = await tool.execute(make_params(verdict='NEEDS_ATTENTION', issues=[{
        'description': 'The provider does not expose this receipt.', 'resolution': 'capability_gap'}]))
    assert not result.is_error and holder.read()['issues'][0]['resolution'] == 'capability_gap'


@pytest.mark.asyncio
async def test_review_cannot_omit_or_invent_existing_user_answers():
    holder = VerdictHolder()
    tool = SubmitVerdictTool(holder)
    tool.required_decisions = {'decision-a': {'answer_excerpt': '{"choice":"accept_pilot_only"}',
        'scope': {'node_ids': ['pilot']}, 'freshness': {'current': False}}}
    result = await tool.execute(make_params(verdict='NEEDS_ATTENTION'))
    assert result.is_error and holder.read() is None
    assert 'accept_pilot_only' in result.output and 'decision-a' in result.output
    reviewed = {'decision-a': {'applicability': 'historical_scope_unverified',
        'reason': 'The saved answer covers the pilot; its legacy binding needs scope verification. Full upload is unanswered.'}}
    result = await tool.execute(make_params(verdict='NEEDS_ATTENTION', reviewed_decisions=reviewed))
    assert not result.is_error
    saved = holder.read()['reviewed_decisions']['decision-a']
    assert saved['host_record']['answer_excerpt'] == '{"choice":"accept_pilot_only"}'
    assert saved['assessment']['applicability'] == 'historical_scope_unverified'
    tool.required_decisions['decision-a']['answer_excerpt'] = 'later change'
    assert saved['host_record']['answer_excerpt'] == '{"choice":"accept_pilot_only"}'
    result = await tool.execute(make_params(reviewed_decisions={**reviewed, 'invented': reviewed['decision-a']}))
    assert result.is_error and holder.read() is None and 'invented' in result.output


@pytest.mark.asyncio
async def test_pass_retains_downstream_followups_but_rejects_current_blockers():
    holder = VerdictHolder()
    tool = SubmitVerdictTool(holder)
    issue = {'description': '公式补偿属于后续阶段', 'resolution': 'downstream',
             'target_node_id': 'stage2', 'blocking': False, 'origin': 'out_of_scope'}
    result = await tool.execute(make_params(issues=[issue]))
    assert not result.is_error and holder.read()['issues'][0]['target_node_id'] == 'stage2'
    result = await tool.execute(make_params(issues=[{**issue, 'blocking': True}]))
    assert result.is_error and holder.read() is None
    result = await tool.execute(make_params(issues=[{**issue, 'origin': 'preexisting'}]))
    assert result.is_error and holder.read() is None


def test_verdict_must_be_pass_or_needs_attention() -> None:
    with pytest.raises(ValidationError):
        make_params(verdict="MAYBE")


def test_issue_requires_description() -> None:
    with pytest.raises(ValidationError):
        make_params(
            verdict=VERDICT_NEEDS_ATTENTION,
            issues=[{"file": "x.py"}],
        )


def test_evidence_exit_code_is_optional_int() -> None:
    params = make_params(
        evidence=[{"kind": "diff", "command": "git diff", "exit_code": None}]
    )
    assert params.evidence[0].exit_code is None


@pytest.mark.asyncio
async def test_frozen_audit_rejects_freeform_pass_before_holder_is_updated():
    holder = VerdictHolder()
    contract = {
        "rubric": {"rubric_hash": "frozen", "criteria": [{"criterion_id": "FIG-01", "acceptance_test": "legend matches"}]},
        "artifact_hash": "new", "baseline_hash": "old",
    }
    tool = SubmitVerdictTool(holder, contract)
    result = await tool.execute(make_params())
    assert result.is_error and holder.read() is None
    row = {"criterion_id": "FIG-01", "state": "PASS", "evidence": "legend verified", "location": "fig5.png"}
    report = {"rubric_hash": "frozen", "artifact_hash": "new", "checks": [row]}
    result = await tool.execute(make_params(assessment=report))
    assert result.is_error and holder.read() is None
    report.update(baseline_hash="old", baseline_checks=[{**row, "state": "FAIL"}])
    result = await tool.execute(make_params(assessment=report))
    assert not result.is_error
    assert holder.read()["assessment"]["baseline_hash"] == "old"
