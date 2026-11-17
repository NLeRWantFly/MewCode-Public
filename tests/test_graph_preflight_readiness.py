"""Regression coverage for shared graph design lint and authorization gates."""

import pytest

from tests.test_graph_lint import _codes, _node, _spec
from tests.test_graph_readiness import check, doc, edge, node
from mewcode.orchestration.graph_inspection import diagnose


def test_real_plan_review_and_gap_clauses_are_not_full_scan_work():
    examples = [
        ("pilot_review", ["逐项复核试点证据与未关闭问题"], []),
        ("method_review", [], ["仍存在的缺口逐项列出，未解决项不得进入全量结论"]),
        ("batch_plan", [], ["本节点不逐页识别、不写入内容"]),
    ]
    for node_id, steps, acceptance in examples:
        findings = _codes(_spec([_node(node_id, kind="plan", steps=steps,
                                      acceptance=acceptance)]))
        assert "plan_node_full_scan" not in findings


def test_real_plan_full_scan_still_blocks_after_sample_or_negative_clause():
    cases = [
        (["逐页读取全部文件"], True),
        (["先抽取 3 个样本，再逐页读取全部文件"], True),
        (["本节点不执行逐页读取全部文件", "执行节点随后逐页读取全部文件"], False),
        (["本节点仍逐页读取全部文件"], True),
        (["把全量工作交给执行节点逐页读取全部文件"], False),
        (["本节点逐页读取全部文件并交给执行节点写入"], True),
    ]
    for steps, expected in cases:
        assert ("plan_node_full_scan" in _codes(
            _spec([_node("plan", kind="plan", steps=steps)]))) is expected


def test_full_scan_negation_and_trial_words_only_exempt_the_scoped_review():
    cases = [
        (["逐页查看所选3个代表样本"], False),
        (["先查看3个样本，再逐页读取全部文件"], True),
        (["不写入内容的前提下逐页读取全部文件"], True),
        (["完成未关闭问题复核后逐页读取全部文件"], True),
        (["试点完成后逐页读取业务目录内文件"], True),
    ]
    for steps, expected in cases:
        assert ("plan_node_full_scan" in _codes(
            _spec([_node("plan", kind="plan", steps=steps)]))) is expected


def test_scope_gate_recognizes_external_service_scope_and_dominates_all_paths():
    root = _node("root")
    gate = _node("n_scope_gate", kind="gate", label="确认范围、外发与费用门禁",
                 goal="确认具体外发服务范围")
    work = _node("n_survey_plan", steps=["将页面交给外部模型识别"])
    gated = _spec([root, gate, work], [
        {"from_node": "root", "to_node": "n_scope_gate"},
        {"from_node": "n_scope_gate", "to_node": "n_survey_plan"},
    ])
    assert "authority_before_use" not in _codes(gated)

    bypass = _spec([root, gate, work], [
        {"from_node": "root", "to_node": "n_scope_gate"},
        {"from_node": "n_scope_gate", "to_node": "n_survey_plan"},
        {"from_node": "root", "to_node": "n_survey_plan"},
    ])
    assert "authority_before_use" in _codes(bypass)

    late = _spec([work, gate], [{"from_node": "n_survey_plan", "to_node": "n_scope_gate"}])
    assert "authority_before_use" in _codes(late)


@pytest.mark.parametrize('binding', ['required', 'optional', 'unbound', 'design_metadata', 'different_graph', 'classic'])
def test_runtime_evidence_branch_does_not_bypass_mandatory_trial_inputs(monkeypatch, binding):
    from dataclasses import replace
    from types import SimpleNamespace
    from mewcode.orchestration.convert import to_expert_flow
    from mewcode.orchestration.graph_preflight import graph_preflight_findings
    from mewcode.permissions import review_client

    monkeypatch.setattr(review_client, 'load_review_model_settings', lambda: {})
    spec = _spec([
        _node('env'), _node('gate', kind='gate', goal='确认试点外发授权'),
        _node('pilot'), _node('investigation'), _node('audit', steps=['核对外发范围与授权一致']),
    ], [{'from_node': source, 'to_node': target} for source, target in [
        ('env', 'gate'), ('gate', 'pilot'), ('pilot', 'audit'), ('pilot', 'investigation'),
        ('env', 'investigation'), ('investigation', 'audit')]],
        meta={'orchestration_mode': 'classic' if binding == 'classic' else 'collaborative'})
    flow = to_expert_flow(spec)
    contract = {'bound_dependencies': ['pilot', 'investigation']}
    record = {'locked_flow': flow.as_dict(), 'collaboration': {
        'format': 'mewcode.collaboration.v2', 'contracts': {'audit': contract}}}
    if binding == 'optional': contract['bound_dependencies'] = ['investigation']
    elif binding == 'unbound': contract['depends_on'] = contract.pop('bound_dependencies')
    elif binding == 'design_metadata':
        flow = replace(flow, meta={**flow.meta, 'required_predecessors': {'audit': ['pilot']}})
        record = None
    elif binding == 'different_graph': flow = replace(flow, version=flow.version + 1)
    findings = graph_preflight_findings(flow, SimpleNamespace(), checkpoint=record)
    assert ('authority_before_use' in {finding.code for finding in findings}) is (binding != 'required')


def test_authority_gate_and_start_input_require_positive_external_send_authorization():
    root = _node("root")
    work = _node("n_survey_plan", steps=["将页面交给外部模型识别"])
    for wording in ("无需外发授权，只确认本地范围", "不允许外发内容"):
        gate = _node("n_scope_gate", kind="gate", label="范围门禁", goal=wording)
        spec = _spec([root, gate, work], [
            {"from_node": "root", "to_node": "n_scope_gate"},
            {"from_node": "n_scope_gate", "to_node": "n_survey_plan"},
        ])
        assert "authority_before_use" in _codes(spec)

    negative_start = _spec([work], run_inputs=[{
        "name": "consent", "description": "无需外发授权，只确认本地范围",
    }])
    assert "authority_before_use" in _codes(negative_start)


@pytest.mark.parametrize("steps,requires_network", [
    (["仅缺失时使用 PreparePythonEnvironment 与已安装的离线依赖准备唯一 .venv，不覆盖现有环境，不联网安装"], False),
    (["本节点不实际联网，仅生成调用脚本"], False),
    (["本节点未联网，保留本地结果"], False),
    (["不联网安装", "随后联网下载依赖"], True),
    (["不联网安装，随后联网下载依赖"], True),
    (["不修改原件，联网下载依赖"], True),
    (["本节点联网安装依赖"], True),
])
def test_network_negation_is_local_and_does_not_hide_real_network_work(steps, requires_network):
    # The graph does not add a permission gate for ordinary connectivity.
    assert "authority_before_use" not in _codes(_spec([_node("env", steps=steps)]))


def test_explicit_network_approval_requirement_is_preserved():
    gate = _node("scope", kind="gate", steps=["本次不联网，不实际联网下载"])
    work = _node("env", steps=["联网下载依赖"])
    spec = _spec([gate, work], [{"from_node": "scope", "to_node": "env"}],
                 meta={"authorization_requirements": {"env": ["network"]}})
    assert "authority_before_use" in _codes(spec)
    spec = _spec([work], run_inputs=[{
        "name": "offline", "description": "不联网，不实际联网下载", "collect_at": "start",
    }], meta={"authorization_requirements": {"env": ["network"]}})
    assert "authority_before_use" in _codes(spec)


def test_readiness_reports_design_lint_errors_but_ignores_runtime_capability_and_warnings():
    plan = node("plan", "plan")
    plan["data"]["steps"] = ["逐页读取全部文件"]
    result = check(doc(plan))
    assert not result["ready"]
    assert any(d["code"] == "plan_node_full_scan" for d in result["diagnostics"])
    assert not any(d["code"] == "review_channel_capability" for d in result["diagnostics"])

    warning_graph = doc(
        node("plan", "plan"),
        node("audit", "audit", acceptance=["完整处理所有材料"]),
        node("finish", "reduce"),
        edge("e1", "plan", "audit"),
        edge("e2", "audit", "finish"),
    )
    warning_result = check(warning_graph)
    assert warning_result["ready"]
    assert any(d["code"] == "acceptance_unbounded" and d.get("severity") == "warning"
               for d in warning_result["diagnostics"])


def test_candidate_diagnose_uses_the_same_design_lint_for_manual_graphs():
    plan = node("plan", "plan")
    plan["data"]["steps"] = ["逐页读取全部文件"]
    result = diagnose(doc(plan), None, conversation_id="test", revision=1)
    assert not result["valid"]
    assert any(d["code"] == "plan_node_full_scan" for d in result["diagnostics"])
    assert not any(d["code"] == "review_channel_capability" for d in result["diagnostics"])
