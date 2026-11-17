"""A hard boundary or missing evidence must never become a free retry."""
import pytest

from mewcode.experts.recovery_diagnosis import diagnose_segment


def health(*, progress=0, repeated_reads=0, calls=4, ratio=0, path_errors=0, type_errors=0):
    return {"terminal_status": "budget_exhausted", "sampling": "tool_complete", "work_delta": {"known": True,
        "review_complete": progress, "review_recorded": 0, "write_receipts": 0,
        "submitted_items": 0, "submitted_revisions": 0},
        "repeated_reads": repeated_reads, "terminal_calls": calls,
        "effective_call_ratio": ratio, "path_error_count": path_errors,
        "tool_misuse_count": type_errors}


def test_capacity_and_idle_are_mutually_exclusive():
    assert diagnose_segment(health(progress=1, repeated_reads=5), error_kind="segment_timeout")["primary_kind"] == "capacity_shortfall"
    assert diagnose_segment(health(repeated_reads=5), error_kind="segment_timeout")["primary_kind"] == "idle_stall"
    assert diagnose_segment(health(calls=0), error_kind="segment_timeout")["primary_kind"] == "insufficient_evidence"


def test_partial_legacy_reads_do_not_prove_idle_stall():
    partial = {**health(repeated_reads=5), "sampling": "partial"}
    assert diagnose_segment(partial, error_kind="segment_timeout")["primary_kind"] == "insufficient_evidence"


@pytest.mark.parametrize(("error", "expected"), [
    ("safety_review_rate_limited", "review_infrastructure"),
    ("safety_review_context_limit", "review_infrastructure"),
    ("hash_expired", "protocol_conflict"),
    ("provider_interrupted", "model_service_fault"),
    ("state_divergence", "state_divergence"),
    ("plan_revision_required", "business_ambiguity"),
])
def test_machine_errors_take_priority_over_stall_statistics(error, expected):
    result = diagnose_segment(health(repeated_reads=5), error_kind=error)
    assert result["primary_kind"] == expected
    assert result["confidence_source"] == "deterministic"


def test_typed_tool_error_is_environment_fault():
    assert diagnose_segment(health(path_errors=1), error_kind="segment_timeout")["primary_kind"] == "environment_fault"
    assert diagnose_segment(health(type_errors=1), error_kind="segment_timeout")["primary_kind"] == "environment_fault"


def test_permission_and_unknown_effect_force_human_boundary():
    for blocker in ("permission", "unknown_effect"):
        result = diagnose_segment(health(progress=1), error_kind="segment_timeout",
                                  blockers=[{"kind": blocker, "ref": "call"}])
        assert result["hard_boundary"] is True
        assert result["recommended_action"] == "human_decision"


def test_business_contract_change_never_retries():
    result = diagnose_segment(health(progress=1), error_kind="segment_timeout",
                              host_facts={"contract_effect": "scope"})
    assert result["primary_kind"] == "business_ambiguity"
    assert result["contract_effect"] == "human_boundary"


def _diagnosis_case():
    from mewcode.experts.problem_dossier import build_dossier
    from mewcode.experts.recovery_case import open_case

    record = {"node_states": {"work": {"input_revision": "rev", "status": "paused",
        "tools": {"failed-read": {"name": "ReadFile", "status": "failed", "is_error": True,
            "output": "detail line\n" * 45 + "ValueError: chapter index missing\n" +
                "api_key=secret-token Bearer abc123 sk-sensitive C:\\Users\\alice\\private.txt"}}}},
        "node_ledgers": {}}
    case = open_case(record, "work")
    return record, case, build_dossier(record, case)


def _diagnostic_plan(case, dossier, *, confidence="medium", recommended="fix-a", duplicate=False):
    from tests.test_recovery_case_e2e import option

    excerpt_row = dossier["tool_failures"][0]
    investigator = option(20)
    investigator.update(option_id="investigate", user_label="先核对已有证据",
        actor="auto", consequence="完成只读核对。")
    investigator["strategy"] = {"kind": "investigate", "tools": ["ReadCaseDossier"],
        "target": "读取该节点的错误详情"}
    fixing = option(21)
    fixing.update(option_id="fix-a", hypothesis_id="scan_input", user_label="检查输入文件",
        actor="auto", consequence="检查文件是否包含可读取文本。")
    options = [investigator, fixing]
    hypotheses = [{"id": "scan_input", "summary": "输入文件缺少可读取的章节索引",
        "confidence": confidence, "evidence": [{"id": excerpt_row["evidence_id"],
            "excerpt": "ValueError: chapter index missing"}],
        "open_questions": ["其他页面是否有章节信息？"], "option_id": "fix-a"}]
    if duplicate:
        hypotheses.append({"id": "bad_index", "summary": "导入时章节索引没有写入",
            "confidence": confidence, "evidence": [{"id": excerpt_row["evidence_id"],
                "excerpt": "ValueError: chapter index missing"}],
            "open_questions": [], "option_id": "fix-a"})
    return {"case_id": case["case_id"], "round": 1,
        "root_cause": {"class": "unclassified", "summary": "当前步骤的输入无法读取章节索引",
            "evidence_refs": ["dossier:" + case["case_id"]], "confidence": confidence},
        "options": options,
        "recommended_option_id": recommended,
        "diagnosis": {"symptom": {"summary": "读取时找不到章节索引。",
            "excerpt": "ValueError: chapter index missing", "evidence_id": excerpt_row["evidence_id"]},
            "impact": "当前步骤未完成。", "tried": [], "hypotheses": hypotheses,
            "investigation": {"scope": "只读核对输入文件和当前错误。",
                "tools": ["ReadCaseDossier"], "budget": 120},
            "goal": {"target": "确认原节点能按原目标完成。",
                "milestones": [{"id": "inspect", "label": "检查输入", "evidence": "读取回执",
                    "expected_seconds": 120}], "stop_when": "没有新证据时收束。"}}}


def test_dossier_error_excerpt_is_redacted_bounded_and_stably_identified():
    from mewcode.experts.problem_dossier import build_dossier, dossier_summary

    record, case, dossier = _diagnosis_case()
    first = dossier["tool_failures"][0]
    second = build_dossier(record, case)["tool_failures"][0]
    assert first["evidence_id"] == second["evidence_id"]
    assert first["excerpt"] == first["error_excerpt"]
    assert len(first["excerpt"].splitlines()) <= 40
    assert len(first["excerpt"]) <= 4000
    assert all(secret not in first["excerpt"] for secret in
        ("secret-token", "abc123", "sk-sensitive", "C:\\Users\\"))
    summary = dossier_summary(dossier)
    assert first["evidence_id"] in summary
    assert "chapter index missing" in summary


def test_valid_diagnosis_binds_quotes_to_dossier_and_maps_options():
    from mewcode.experts.root_cause import validate_plan

    record, case, dossier = _diagnosis_case()
    plan = _diagnostic_plan(case, dossier)
    assert validate_plan(plan, case, dossier) is plan


def test_diagnosis_rejects_unsupported_quote_duplicate_causes_and_missing_option():
    from mewcode.experts.root_cause import validate_plan

    record, case, dossier = _diagnosis_case()
    plan = _diagnostic_plan(case, dossier)
    plan["diagnosis"]["symptom"]["excerpt"] = "This quote was not in the dossier."
    with pytest.raises(ValueError, match="证据摘录不在档案中"):
        validate_plan(plan, case, dossier)

    plan = _diagnostic_plan(case, dossier, duplicate=True)
    with pytest.raises(ValueError, match="原因重复"):
        validate_plan(plan, case, dossier)

    plan = _diagnostic_plan(case, dossier)
    plan["diagnosis"]["hypotheses"][0]["option_id"] = "missing"
    with pytest.raises(ValueError, match="方案不存在"):
        validate_plan(plan, case, dossier)


def test_all_low_confidence_requires_readonly_investigation_recommendation():
    from mewcode.experts.root_cause import validate_plan

    record, case, dossier = _diagnosis_case()
    plan = _diagnostic_plan(case, dossier, confidence="low", recommended="fix-a")
    with pytest.raises(ValueError, match="必须推荐只读探查"):
        validate_plan(plan, case, dossier)
    plan["recommended_option_id"] = "investigate"
    assert validate_plan(plan, case, dossier) is plan


def test_investigation_schema_limits_tools_and_verification_to_readonly():
    from mewcode.tools.submit_recovery_plan import Investigation, SubmitRecoveryPlanParams

    Investigation(scope="只读检查当前步骤", tools=["ReadStageResult", "ReadRecoveryContext",
        "ReadAuthorizations"], budget=60)
    with pytest.raises(ValueError, match="探查工具必须是具体的只读工具"):
        Investigation(scope="运行命令", tools=["Bash"], budget=60)

    record, case, dossier = _diagnosis_case()
    plan = _diagnostic_plan(case, dossier)
    plan["options"][0]["verification"]["tool"] = "capability_binding"
    with pytest.raises(ValueError, match="只读工具"):
        SubmitRecoveryPlanParams(**plan)


def test_analysis_failure_builds_fact_only_investigate_card_and_legacy_stays_compatible():
    from mewcode.experts.problem_dossier import incident_report
    from mewcode.experts.recovery_case import open_case

    record, case, dossier = _diagnosis_case()
    report = incident_report(record, case, conclusion="analysis dispatch failed",
        fallback_diagnosis=True, analysis_errors=["schema submission failed"])
    assert report["diagnosis"]["hypotheses"] == []
    assert report["diagnosis"]["symptom"]["evidence_id"] is None
    assert "schema submission failed" in report["diagnosis"]["symptom"]["summary"]
    assert report["options"][0]["value"] == "investigate"
    assert report["options"][0]["recommended"] is True

    legacy_case = {**case, "plan": {}}
    legacy_case.pop("recovery_boundary", None)
    legacy_case.pop("boundary", None)
    legacy = incident_report(record, legacy_case, conclusion="legacy summary")
    assert "diagnosis" not in legacy


def test_saved_investigator_option_runs_analysis_only_and_normal_option_stays_selected():
    from copy import deepcopy

    from mewcode.experts.problem_dossier import incident_report
    from mewcode.experts.case_runtime import answer_case
    from mewcode.experts.recovery_case import apply_event

    record, case, dossier = _diagnosis_case()
    plan = _diagnostic_plan(case, dossier)
    plan["options"][0]["option_id"] = "readonly-check"
    plan["recommended_option_id"] = "readonly-check"
    case["plan"] = plan
    report = incident_report(record, case, conclusion="当前仍需核对原步骤。",
        diagnosis=plan["diagnosis"])
    case = apply_event(record, case["case_id"], {
        "kind": "report", "identity": "diagnosis-report", "report": report})
    investigator = next(row for row in case["pending"]["options"]
        if row.get("value") == "readonly-check")
    assert investigator["action"] == "investigate"

    normal_record = deepcopy(record)
    investigation_case = answer_case(record, case["case_id"], round=0,
        choice="readonly-check", answer="只读检查错误详情")
    assert investigation_case["investigation_requested"]["instruction"] == "只读检查错误详情"
    assert investigation_case["plan"] == {}
    assert "selected_recovery_option" not in investigation_case
    assert record["node_states"]["work"]["status"] == "pending"

    normal_case = answer_case(normal_record, case["case_id"], round=0, choice="fix-a")
    assert normal_case["selected_recovery_option"]["option_id"] == "fix-a"
    assert normal_case["plan"]["recommended_option_id"] == "fix-a"


def test_active_investigation_can_only_close_and_close_replay_is_idempotent():
    from mewcode.experts.case_runtime import answer_case
    from mewcode.experts.recovery_case import save_case

    record, case, _ = _diagnosis_case()
    case.update(status="open", pending={}, investigation_requested={
        "instruction": "只读核对已有材料", "source_ref": "answer:investigate"})
    save_case(record, case)

    with pytest.raises(ValueError, match="只允许取消"):
        answer_case(record, case["case_id"], round=0, choice="investigate")

    stopped = answer_case(record, case["case_id"], round=0, choice="close")
    assert stopped["status"] == "reported"
    assert stopped["closed_by_user"] is True
    assert stopped["pending"] == {}
    assert record["node_states"]["work"]["status"] == "paused"
    event_count = len(stopped["events"])
    replay = answer_case(record, case["case_id"], round=0, choice="close")
    assert replay["closed_by_user"] is True
    assert len(replay["events"]) == event_count


def test_closed_investigation_is_hidden_unrecommended_and_rejected():
    from mewcode.experts.case_runtime import answer_case
    from mewcode.experts.problem_dossier import incident_report
    from mewcode.experts.recovery_case import apply_event, save_case

    record, case, dossier = _diagnosis_case()
    plan = _diagnostic_plan(case, dossier, recommended="investigate")
    case["plan"] = plan
    case["boundary"] = {"investigation_closed": True}
    save_case(record, case)

    report = incident_report(record, case, conclusion="只读核对未发现新证据。",
        diagnosis=plan["diagnosis"])
    assert [row["value"] for row in report["options"]] == ["fix-a", "manual"]
    assert report["diagnosis"]["recommended_option_id"] == "fix-a"
    assert next(row for row in report["options"] if row["value"] == "fix-a")["recommended"] is True
    assert all(row["value"] != "investigate" for row in report["recovery_options"])
    assert all(row.get("strategy", {}).get("kind") != "investigate"
               for row in report["recovery_plan"]["options"])

    case = apply_event(record, case["case_id"], {
        "kind": "report", "identity": "closed-investigation-report", "report": report})
    with pytest.raises(ValueError, match="已收束"):
        answer_case(record, case["case_id"], round=0, choice="investigate")


def test_analysis_failure_report_keeps_submit_error_saved_options_and_flow_stage():
    from mewcode.experts.problem_dossier import incident_report

    record, case, dossier = _diagnosis_case()
    record["locked_flow"] = {"edges": [], "meta": {}, "nodes": [
        {"node_id": "prepare", "label": "准备材料"},
        {"node_id": "work", "label": "处理材料"},
    ]}
    record["node_states"]["work"]["tools"]["submit-failed"] = {
        "name": "SubmitRecoveryPlan", "status": "failed", "is_error": True,
        "output": "ValueError: 诊断证据缺少当前档案编号",
    }
    case["plan"] = _diagnostic_plan(case, dossier)

    report = incident_report(record, case, conclusion="未收到可核验的方案。",
        fallback_diagnosis=True, analysis_errors=["ValueError: 提交失败"])
    assert report["title"] == "第 2 步卡住了：处理材料"
    assert "未收到可核验的方案" in report["reason"]
    assert "SubmitRecoveryPlan 提交失败" in report["diagnosis"]["symptom"]["summary"]
    values = [row["value"] for row in report["options"]]
    assert values == ["investigate", "fix-a", "manual"]
    manual = report["options"][-1]
    assert manual["action"] == "manual" and manual["requires_input"] is True


@pytest.mark.asyncio
async def test_analyst_inherits_iteration_policy_and_boundary_state_on_resume(tmp_path):
    from copy import deepcopy
    from dataclasses import replace

    from mewcode.expert_group import ExpertRoleResult
    from mewcode.experts.problem_dossier import build_dossier
    from mewcode.experts.recovery_analyst import ANALYSIS_CONTEXT_VERSION, RecoveryAnalyst
    from mewcode.experts.recovery_case import open_case
    from tests.test_recovery_case_e2e import RUN, harness, option, plan

    h = harness(tmp_path)

    def prepare(record):
        case = open_case(record, "work")
        case["boundary"] = {"needs_review": True, "boundary_id": "boundary-3"}
        record["node_states"]["work"]["recovery_case"] = deepcopy(case)
        record["node_states"][case["analyst_session"]] = {
            "messages": [{"role": "assistant", "content": "resumed history"}],
            "analysis_context_version": ANALYSIS_CONTEXT_VERSION,
        }

    h.store.mutate(RUN, prepare)
    record = h.store.load(RUN)
    case = record["node_states"]["work"]["recovery_case"]

    async def dispatch(helper, prompt, policy):
        assert policy.max_iterations == 23
        assert '"boundary_id": "boundary-3"' in prompt
        context = policy.recovery_analysis
        payload = plan(context, [option()])
        payload["boundary_review"] = {"action": "continue", "reason": "仍有可核验进展。",
            "next_step": "核对原步骤的实际完成回执。", "evidence_refs": ["dossier:" + case["case_id"]]}
        context["holder"].update(payload)
        return ExpertRoleResult("valid plan")

    analyst = RecoveryAnalyst(dispatch=dispatch, store=h.store, run_id=RUN)
    await analyst.analyze(case, build_dossier(record, case), node=h.node,
        policy=replace(h.policy, max_iterations=23))
