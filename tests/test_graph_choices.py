"""Explicit choices across the durable draft, execution and stage boundaries."""
import copy
from datetime import datetime, timezone

import pytest

from mewcode.orchestration.choices import validate_options
from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.graph_discussion import pause_for_user
from mewcode.orchestration.graph_generation import graph_state, ready_graph_document
from mewcode.orchestration.graph_readiness import readiness
from mewcode.orchestration.workflow_draft import project_graph, seed_document
from mewcode.orchestration.convert import to_expert_flow
from mewcode.orchestration.run_inputs import resolve_run_inputs
from tests.test_graph_discussion import prepare
from tests.test_stage_run_inputs import input_flow


def options():
    return [{"value":"目录实际名称", "label":"按实际目录", "description":"保留五层结构，名称随书变化", "recommended":True},
            {"value":"固定分类名称", "label":"固定分类", "description":"所有书使用同一套名称"}]


def prepare_choices(store, *, legacy=False, second=False):
    candidate = prepare(store)
    document = copy.deepcopy(candidate["document"])
    for item in document["entries"]:
        if item["id"] == "meaning":
            item["data"].update(options=options())
        if item["id"] == "labels":
            item["data"].update(label="层级命名方式", options=options())
    if second:
        question = copy.deepcopy(next(item for item in document["entries"] if item["id"] == "meaning"))
        question.update(id="other", title="另一个选择")
        question["data"].pop("input_id")
        document["entries"].append(question)
    build = candidate["autobuild"]
    if legacy:
        build.update(phase="done", status="needs_review", reason="pending_questions")
        build.pop("waiting_since", None)
    return store.checkpoint(candidate, document, candidate["processed_ids"], "ready", autobuild=build)


def choose(store, selections):
    candidate = store.get()["candidate"]
    return store.discuss(None, "choose", candidate_id=candidate["id"], sequence=candidate["sequence"], selections=selections)


def test_choice_is_atomic_traceable_and_does_not_use_model_budget(tmp_path):
    store = DraftStore(tmp_path, "chat")
    before = prepare_choices(store)
    choose(store, [{"question_id":"meaning", "value":"目录实际名称"}])
    state = store.get(); after = state["candidate"]
    assert after["processed_ids"] == before["processed_ids"] and state["source_ids"] == []
    discussion = after["autobuild"]["discussion"]
    assert discussion["calls"] == 0 and after["autobuild"]["call_attempts"] == 4
    assert not discussion["proposal"] and not discussion.get("pending")
    entries = {item["id"]:item for item in after["document"]["entries"]}
    assert entries["meaning"]["kind"] == "decision" and entries["meaning"]["data"]["category"] == "confirmed"
    assert entries["labels"]["data"]["default"] == "目录实际名称"
    receipt = entries["meaning"]["sources"][-1]
    assert receipt["quote"] == "目录实际名称" and "目录实际名称" in store.source(receipt["source_id"])["text"]
    assert after["autobuild"]["phase"] == "autofix" and after["autobuild"]["review_stale"]
    unchanged = {item["id"]:item for item in before["document"]["entries"] if item["id"] not in {"meaning","labels"}}
    assert all(entries[key] == value for key,value in unchanged.items())
    with pytest.raises(ValueError, match="版本"):
        store.discuss(None, "choose", candidate_id=before["id"], sequence=before["sequence"],
                      selections=[{"question_id":"meaning","value":"固定分类名称"}])


@pytest.mark.parametrize("default", [None, "sampled_human_review"])
@pytest.mark.parametrize("value", ["extend_reference_to_all_pages", "retain_sampled_partial_delivery"])
def test_business_strategy_choice_preserves_a_linked_input_with_different_values(tmp_path, default, value):
    store = DraftStore(tmp_path, "chat")
    candidate = prepare_choices(store)
    entries = {item["id"]: item for item in candidate["document"]["entries"]}
    entries["meaning"]["data"]["options"] = [
        {"value": "extend_reference_to_all_pages", "label": "扩展参考覆盖范围"},
        {"value": "retain_sampled_partial_delivery", "label": "保留抽样，仅交付部分验证结果"},
    ]
    entries["labels"]["data"].update(default=default, options=[
        {"value": "sampled_human_review", "label": "人工抽样参考"},
        {"value": "user_provided", "label": "用户提供参考"},
    ])
    before = store.checkpoint(candidate, candidate["document"], candidate["processed_ids"],
                              "ready", autobuild=candidate["autobuild"])
    choose(store, [{"question_id": "meaning", "value": value}])
    # Reopen the durable store: this must be an adopted choice, not just a preview.
    reopened = DraftStore(tmp_path, "chat")
    after = reopened.get()["candidate"]
    updated = {item["id"]: item for item in after["document"]["entries"]}
    assert updated["meaning"]["kind"] == "decision"
    assert updated["meaning"]["data"]["value"] == value
    assert all(updated[item["id"]] == item for item in before["document"]["entries"]
               if item["id"] != "meaning")
    receipt = updated["meaning"]["sources"][-1]
    assert value in reopened.source(receipt["source_id"])["text"]
    build = after["autobuild"]
    assert build["phase"] == "autofix" and build["status"] == "working"
    assert build["review_stale"] and build["review"] is None and build["final_review"] is None
    assert build["call_attempts"] == before["autobuild"]["call_attempts"]
    assert build["discussion"]["decisions"][-1]["changed_ids"] == ["meaning"]


@pytest.mark.parametrize("selections", [[{"question_id":"meaning","value":"unknown"}],
    [{"question_id":"meaning","value":"目录实际名称"},{"question_id":"missing","value":"固定分类名称"}],
    [{"question_id":"meaning","value":"目录实际名称"}]*2])
def test_invalid_choices_preserve_all_durable_state(tmp_path, selections):
    store = DraftStore(tmp_path, "chat"); prepare_choices(store)
    before = store.get()
    with pytest.raises(ValueError):
        choose(store, selections)
    assert store.get() == before


def test_partial_choices_stay_paused_until_all_human_choices_are_answered(tmp_path):
    store = DraftStore(tmp_path, "chat"); prepare_choices(store, second=True)
    choose(store, [{"question_id":"meaning","value":"目录实际名称"}])
    assert store.get()["candidate"]["autobuild"]["phase"] == "waiting_user"
    choose(store, [{"question_id":"other","value":"固定分类名称"}])
    assert store.get()["candidate"]["autobuild"]["phase"] == "autofix"
    assert store.get()["candidate"]["autobuild"]["call_attempts"] == 4


def test_legacy_done_questions_cannot_save_or_auto_accept_recommendation(tmp_path):
    store=DraftStore(tmp_path,"chat"); prepare_choices(store, legacy=True)
    state={**store.get(), "conversation_id":"chat", "project":str(tmp_path), "draft_id":"main", "running":False, "progress":None}
    assert not graph_state(state,None)["can_preview"]
    with pytest.raises(ValueError, match="选择|讨论|确认"):
        ready_graph_document(state,state["candidate"],None,conversation_id="chat")
    assert next(e for e in state["candidate"]["document"]["entries"] if e["id"] == "labels")["data"]["default"] is None


def test_legacy_wait_preserves_remaining_active_time_instead_of_expiring_during_user_wait():
    candidate={"at":datetime.fromtimestamp(100,timezone.utc).isoformat(),
               "autobuild":{"phase":"done","deadline_at":400,"call_attempts":4}}
    pause_for_user(candidate,clock=lambda:10000)
    build=candidate["autobuild"]
    assert build["deadline_at"] == 10300 and build["remaining_active_seconds"] == 300
    pause_for_user(candidate,clock=lambda:20000)
    assert build["deadline_at"] == 10300 and build["call_attempts"] == 4


def test_draft_projection_retains_executable_predicates_and_models():
    from mewcode.orchestration.convert import from_expert_flow
    spec=from_expert_flow(input_flow())
    raw=spec.as_dict()
    raw["nodes"][0]["model"]={"provider_key":"configured:one","model_id":"chosen-model","effort":"high"}
    document=seed_document(raw)
    projected=project_graph(document,None,conversation_id="renamed-chat",revision=1)
    assert projected.nodes[0].model.as_dict() == raw["nodes"][0]["model"]
    assert [e.as_dict() for e in projected.edges] == [e.as_dict() for e in spec.edges]
    assert [e.as_dict() for e in to_expert_flow(projected).edges] == [e.as_dict() for e in input_flow().edges]
    assert not [d for d in readiness(document,None,conversation_id="renamed-chat",revision=1)["diagnostics"] if d["code"] == "entry_not_projected"]


@pytest.mark.parametrize("bad", [[], options()*2, [dict(options()[0],value=""),options()[1]],
    [options()[0],dict(options()[1],recommended=True)], [dict(options()[0],extra="bad"),options()[1]]])
def test_malformed_option_contracts_fail(bad):
    with pytest.raises(ValueError):
        validate_options(bad)


def test_declared_choice_values_are_required_at_start_and_later_stages():
    declaration={"name":"labels","label":"命名方式","required":True,"default":None,"options":options()}
    with pytest.raises(ValueError,match="提供"):
        resolve_run_inputs([declaration],{"labels":"any text"})
    assert resolve_run_inputs([declaration],{"labels":"目录实际名称"}) == {"labels":"目录实际名称"}
    declaration["collect_at"]="publish"
    assert resolve_run_inputs([declaration],{}) == {"labels":""}
    with pytest.raises(ValueError,match="必填"):
        resolve_run_inputs([declaration],{},node_id="publish")


def test_branch_and_conditional_values_must_match_the_displayed_choices():
    from mewcode.experts.flow import ExpertFlowSpec
    raw=input_flow().as_dict()
    raw["meta"]["run_input_contract"][0]["options"]=[{"value":"true","label":"开启"},{"value":"false","label":"关闭"}]
    ExpertFlowSpec.from_value(raw).validate()
    raw["edges"][1]["input_when"]["equals"]="other"
    with pytest.raises(ValueError,match="选择项"):
        ExpertFlowSpec.from_value(raw).validate()
    raw["edges"][1]["input_when"]["equals"]="false"
    raw["meta"]["run_input_contract"][1]["required_when"]["equals"]="other"
    with pytest.raises(ValueError,match="选择项"):
        ExpertFlowSpec.from_value(raw).validate()


def test_binding_unbound_nodes_preserves_phase_input_and_permission_contracts():
    from mewcode.orchestration.convert import apply_profile_bindings, from_expert_flow
    from mewcode.orchestration.spec import OrchestrationSpec
    raw=from_expert_flow(input_flow()).as_dict()
    raw["goal_contract"]["constraints"]=["只有签认后才能发布"]
    raw["goal_contract"]["acceptance"]=["最终文件实际存在"]
    spec=OrchestrationSpec.from_value(raw)
    bound=apply_profile_bindings(spec,manager={"provider_key":"configured:one","model_id":"chosen-model"})
    assert bound.goal_contract == spec.goal_contract
    assert [e.as_dict() for e in bound.edges] == [e.as_dict() for e in spec.edges]
    assert bound.meta == spec.meta and all(node.model for node in bound.nodes)


@pytest.mark.asyncio
async def test_stage_choices_present_impact_and_validate_the_real_value(tmp_path):
    from types import SimpleNamespace
    from mewcode.experts.flow import ExpertFlowSpec
    from mewcode.experts.stage_run_inputs import collect_stage_run_inputs
    from mewcode.expert_runs import ExpertRunStore
    from tests.test_expert_flow import _profile
    raw=input_flow().as_dict()
    declaration=raw["meta"]["run_input_contract"][2]
    declaration.update(label="交付方式", options=options())
    flow=ExpertFlowSpec.from_value(raw)
    run_id="0123456789abcdef0123456789abcdef"
    store=ExpertRunStore(tmp_path/"runs")
    store.create(run_id=run_id,conversation_id="chat",task="数据库",attachments=[],profile=_profile().as_dict(),strategy="stable",flow=flow)
    session=SimpleNamespace(); events=[]
    async def emit(event):
        if event.get("type") == "ask_user":
            events.append(event)
            assert event["questions"][0]["options"][0]["description"] == options()[0]["description"]
            session._ask_user_request.future.set_result({"publication":"目录实际名称"})
    values=await collect_stage_run_inputs(node=flow.node_map["publish"],flow=flow,session=session,
                                         store=store,run_id=run_id,emit=emit)
    assert values["publication"] == "目录实际名称" and len(events) == 1
    assert not store.load(run_id).get("gate_confirmations")


def test_graph_model_preflight_refuses_silent_effort_downgrade():
    from types import SimpleNamespace
    from mewcode.expert_web import expert_flow_model_error
    from mewcode.experts.flow import ExpertFlowSpec
    from mewcode.model_registry import ModelRef
    flow=ExpertFlowSpec.from_value({"flow_id":"model-check","version":1,"label":"模型检查",
        "nodes":[{"node_id":"worker","role":"executor","label":"执行节点",
                  "model":{"provider_key":"configured:one","model_id":"chosen-model","effort":"max"}}],
        "edges":[],"finalizer_node_id":"worker"})
    registry=SimpleNamespace(resolve=lambda binding:SimpleNamespace(ref=ModelRef(binding.provider_key,binding.model_id,"high")))
    assert "未自动降档" in expert_flow_model_error(registry,flow)


def test_deferred_choice_shows_input_without_a_duplicate_pending_question():
    from mewcode.orchestration.graph_generation import delivery_view
    from tests.test_graph_discussion import discussion_graph
    document=discussion_graph()
    next(e for e in document["entries"] if e["id"]=="meaning")["data"]["issue_type"]="input"
    next(e for e in document["entries"] if e["id"]=="labels")["data"].update(collect_at="plan",label="命名方式",options=options())
    delivery=delivery_view(document,{"phase":"done","status":"ready"})
    assert delivery["pending"] == [] and delivery["issue_groups"] == []
    assert delivery["run_inputs"][0]["collect_at"] == "plan" and delivery["run_inputs"][0]["label"] == "命名方式"


def test_generation_model_routes_never_include_credentials():
    from mewcode.model_registry import ProviderRegistry
    from mewcode.orchestration.graph_models import available_model_routes
    registry=ProviderRegistry()
    registry.replace_desktop_profiles([{"id":"configured-one","name":"Configured provider","protocol":"openai-compat",
        "base_url":"https://example.invalid/v1","default_model":"gpt-6.1-sol","models":["gpt-6.1-sol"],"api_key":"DO-NOT-EXPORT"}])
    result=available_model_routes(registry)
    assert result[0]["provider_key"] == "desktop:configured-one"
    assert result[0]["models"][0]["model_id"] == "gpt-6.1-sol"
    assert "DO-NOT-EXPORT" not in str(result) and "base_url" not in str(result)


def test_completion_can_fill_missing_model_but_cannot_replace_an_existing_binding():
    from mewcode.orchestration.graph_completion import apply_completion
    from tests.test_graph_discussion import discussion_graph
    document=discussion_graph()
    binding={"provider_key":"configured:one","model_id":"chosen-model","effort":"high"}
    filled,applied,rejected=apply_completion(document,{"upserts":[{"id":"plan","data":{"model":binding},"rationale":"按已配置的指定规划路由补齐模型"}]})
    assert applied and not rejected
    assert next(e for e in filled["entries"] if e["id"]=="plan")["data"]["model"] == binding
    changed,applied,rejected=apply_completion(filled,{"upserts":[{"id":"plan","data":{"model":{**binding,"model_id":"other-model"}},"rationale":"试图改模型"}]})
    assert not applied and rejected and changed == filled
