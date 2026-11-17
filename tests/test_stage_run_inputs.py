"""Phase-aware input collection and real scheduler routing without providers."""
import asyncio
import copy
from types import SimpleNamespace

import pytest

from mewcode.expert_group import ExpertGroupOrchestrator, ExpertRoleResult
from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.experts.stage_run_inputs import collect_stage_run_inputs
from mewcode.orchestration.convert import from_expert_flow, to_expert_flow
from mewcode.orchestration.pipeline_bridge import from_pipeline_graph, to_pipeline_graph
from mewcode.orchestration.run_inputs import resolve_run_inputs, validate_input_contract
from mewcode.orchestration.validate import validate_spec
from tests.test_expert_flow import _profile


def input_flow():
    return ExpertFlowSpec.from_value({"flow_id":"conditional", "version":1, "label":"可选向量",
        "nodes":[{"node_id":name,"role":"manager","label":name} for name in ("database","vector","audit","publish")],
        "edges":[
            {"from_node":"database","to_node":"vector","condition":"pass","input_when":{"input":"vector_enabled","equals":"true"},"branch_group":"vector-choice"},
            {"from_node":"database","to_node":"audit","condition":"pass","input_when":{"input":"vector_enabled","equals":"false"},"branch_group":"vector-choice"},
            {"from_node":"vector","to_node":"audit"}, {"from_node":"audit","to_node":"publish"}],
        "finalizer_node_id":"publish", "meta":{"run_input_contract":[
            {"name":"vector_enabled","required":False,"default":"false"},
            {"name":"embedding","required":False,"default":None,"collect_at":"vector","required_when":{"input":"vector_enabled","equals":"true"}},
            {"name":"publication","required":True,"default":None,"collect_at":"publish"}]}})


def test_phase_inputs_and_conditional_requirements():
    declarations = input_flow().meta["run_input_contract"]
    values = resolve_run_inputs(declarations, {})
    assert values == {"vector_enabled":"false","embedding":"","publication":""}
    assert resolve_run_inputs(declarations, {"vector_enabled":"true"})["embedding"] == ""
    with pytest.raises(ValueError, match="embedding"):
        resolve_run_inputs(declarations, {"vector_enabled":"true"}, node_id="vector")
    assert resolve_run_inputs(declarations, {"vector_enabled":"false"}, node_id="vector")["embedding"] == ""
    with pytest.raises(ValueError, match="publication"):
        resolve_run_inputs(declarations, {}, node_id="publish")


@pytest.mark.parametrize("condition", [{"input":"missing","equals":"true"}, {"input":"embedding","equals":"true"},
    {"input":"vector_enabled","equals":""}, {"input":"vector_enabled","equals":True}])
def test_bad_conditional_input_contracts_are_rejected(condition):
    declarations = input_flow().meta["run_input_contract"]
    declarations[1]["required_when"] = condition
    with pytest.raises(ValueError):
        validate_input_contract(declarations, node_ids=set(input_flow().node_map))


def test_contract_and_branch_predicates_survive_flow_and_canvas_roundtrip():
    flow = input_flow(); flow.validate()
    spec = from_expert_flow(flow)
    validate_spec(spec)
    converted = to_expert_flow(spec)
    assert converted.meta["run_input_contract"] == flow.meta["run_input_contract"]
    assert [edge.as_dict() for edge in converted.edges] == [edge.as_dict() for edge in flow.edges]
    canvas = to_pipeline_graph(spec)
    reopened = from_pipeline_graph(canvas)
    assert reopened.goal_contract.run_inputs == spec.goal_contract.run_inputs
    assert [edge.as_dict() for edge in reopened.edges] == [edge.as_dict() for edge in spec.edges]
    conditional = next(edge for edge in canvas.edges if edge.from_node == "database" and edge.to_node == "vector")
    conditional.to_node = "publish"
    with pytest.raises(ValueError, match="重新连线"):
        from_pipeline_graph(canvas)


@pytest.mark.parametrize("bad", ["unbound", "same_value", "unknown_name", "future_stage"])
def test_unexecutable_branch_declarations_fail_host_validation(bad):
    raw = input_flow().as_dict()
    if bad == "unbound":
        raw["edges"][0].pop("input_when")
    elif bad == "same_value":
        raw["edges"][1]["input_when"]["equals"] = "true"
    elif bad == "unknown_name":
        raw["edges"][1]["input_when"]["input"] = "undeclared"
    else:
        raw["meta"]["run_input_contract"][2]["collect_at"] = "does_not_exist"
    with pytest.raises(ValueError):
        ExpertFlowSpec.from_value(raw).validate()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,expected", [("true",["database","vector","audit","publish"]),("false",["database","audit","publish"])])
async def test_real_scheduler_executes_only_selected_branch(tmp_path, enabled, expected):
    flow = input_flow()
    calls = []
    async def fallback(*args):
        raise AssertionError("No provider may be dispatched")
    async def node_runner(node, model, prompt, policy, member=None):
        calls.append(node.node_id)
        return ExpertRoleResult(text=node.node_id, handoff={"summary":node.node_id,"artifacts":[],"open_items":[]})
    orchestrator = ExpertGroupOrchestrator(runner=fallback,node_runner=node_runner,archive_root=tmp_path/"archives")
    result = await orchestrator.run("构建数据库",_profile(),{"providers":[]},strategy="stable",flow=flow,
                                   checkpoint={"run_inputs":{"vector_enabled":enabled}})
    assert calls == expected and result.status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("choice,expected",[("keep",["approval","handover"]),("publish",["approval","promote","handover"])])
async def test_delivery_choice_skips_publishing_when_user_keeps_candidate(tmp_path,choice,expected):
    flow=ExpertFlowSpec.from_value({"flow_id":"delivery-choice","version":1,"label":"交付选择",
        "nodes":[{"node_id":name,"role":"manager","label":name} for name in ("approval","promote","handover")],
        "edges":[{"from_node":"approval","to_node":"promote","condition":"pass","input_when":{"input":"delivery","equals":"publish"},"branch_group":"delivery"},
                 {"from_node":"approval","to_node":"handover","condition":"pass","input_when":{"input":"delivery","equals":"keep"},"branch_group":"delivery"},
                 {"from_node":"promote","to_node":"handover"}],"finalizer_node_id":"handover",
        "meta":{"run_input_contract":[{"name":"delivery","required":True,"collect_at":"approval",
            "options":[{"value":"keep","label":"保留候选"},{"value":"publish","label":"正式发布"}]}]}})
    flow.validate(); calls=[]
    async def fallback(*args):raise AssertionError("No provider dispatch")
    async def run_node(node,model,prompt,policy,member=None):
        calls.append(node.node_id)
        return ExpertRoleResult(text=node.node_id,handoff={"summary":node.node_id,"artifacts":[],"open_items":[]})
    result=await ExpertGroupOrchestrator(runner=fallback,node_runner=run_node,archive_root=tmp_path/"archives").run(
        "交付成果",_profile(),{"providers":[]},strategy="stable",flow=flow,checkpoint={"run_inputs":{"delivery":choice}})
    assert calls == expected and result.status == "completed"


@pytest.mark.asyncio
async def test_input_values_are_collected_only_at_stage_and_remain_durable(tmp_path):
    flow = input_flow(); session = SimpleNamespace()
    store = ExpertRunStore(tmp_path/"runs")
    store.create(run_id="0123456789abcdef0123456789abcdef",conversation_id="chat",task="数据库",attachments=[],profile=_profile().as_dict(),strategy="stable",flow=flow)
    store.mutate("0123456789abcdef0123456789abcdef",lambda value:value.__setitem__("run_inputs",{"vector_enabled":"false"}))
    asked = []
    async def emit(event):
        if event.get("type") == "ask_user":
            asked.append((event["node_id"],[item["name"] for item in event["questions"]]))
            session._ask_user_request.future.set_result({item["name"]:"保留本地交付" for item in event["questions"]})
    before = store.load("0123456789abcdef0123456789abcdef")
    for name in ("database","vector","audit"):
        await collect_stage_run_inputs(node=flow.node_map[name],flow=flow,session=session,store=store,run_id="0123456789abcdef0123456789abcdef",emit=emit)
    assert not asked and store.load("0123456789abcdef0123456789abcdef")["run_inputs"] == before["run_inputs"]
    values = await collect_stage_run_inputs(node=flow.node_map["publish"],flow=flow,session=session,store=store,run_id="0123456789abcdef0123456789abcdef",emit=emit)
    assert asked == [("publish",["publication"])] and values["publication"] == "保留本地交付"
    reopened = ExpertRunStore(tmp_path/"runs")
    await collect_stage_run_inputs(node=flow.node_map["publish"],flow=flow,session=session,store=reopened,run_id="0123456789abcdef0123456789abcdef",emit=emit)
    assert asked == [("publish",["publication"])]
    assert not store.load("0123456789abcdef0123456789abcdef").get("gate_confirmations")


@pytest.mark.asyncio
async def test_interrupted_stage_input_restores_same_question(tmp_path):
    flow=input_flow(); session=SimpleNamespace(); store=ExpertRunStore(tmp_path/"runs")
    store.create(run_id="0123456789abcdef0123456789abcdef",conversation_id="chat",task="数据库",attachments=[],profile=_profile().as_dict(),strategy="stable",flow=flow)
    store.mutate("0123456789abcdef0123456789abcdef",lambda value:value.__setitem__("run_inputs",{"vector_enabled":"true"}))
    entered=asyncio.Event(); requests=[]
    async def first_emit(event):
        if event.get("type") == "ask_user":
            requests.append(event["request_id"]); entered.set()
    task=asyncio.create_task(collect_stage_run_inputs(node=flow.node_map["vector"],flow=flow,session=session,store=store,run_id="0123456789abcdef0123456789abcdef",emit=first_emit))
    await asyncio.wait_for(entered.wait(),2); task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async def resumed_emit(event):
        if event.get("type") == "ask_user":
            assert event["resumed"] and event["request_id"] == requests[0]
            session._ask_user_request.future.set_result({"embedding":"chosen-local"})
    values=await collect_stage_run_inputs(node=flow.node_map["vector"],flow=flow,session=session,store=store,run_id="0123456789abcdef0123456789abcdef",emit=resumed_emit)
    assert values["embedding"] == "chosen-local" and "pending_interaction" not in store.load("0123456789abcdef0123456789abcdef")["node_states"]["vector"]


def test_gate_receipt_is_bound_to_inputs_even_if_generated_proposal_text_is_identical():
    from mewcode.experts.gate_dialogue import confirmation_valid, digest, inputs
    from mewcode.experts.human_gate import gate_scope_hash
    flow = input_flow(); node = flow.node_map["publish"]
    snapshot = {"run_inputs":{"publication":"保留本地"}}
    source_hash = digest(inputs(node, flow, snapshot))
    draft = {"proposal":"交付数据库", "inputs_hash":source_hash}
    receipt = {"decision":"continue", "scope_hash":gate_scope_hash(node, flow.meta),
               "proposal_hash":digest(draft["proposal"]), "inputs_hash":source_hash}
    snapshot.update(gate_dialogues={node.node_id:draft}, gate_confirmations={node.node_id:receipt})
    assert confirmation_valid(node, flow, snapshot)
    snapshot["run_inputs"]["publication"] = "对外发布"
    draft["inputs_hash"] = digest(inputs(node, flow, snapshot))
    assert not confirmation_valid(node, flow, snapshot)


@pytest.mark.asyncio
async def test_stage_default_is_durable_for_router_without_a_startup_snapshot(tmp_path):
    flow=input_flow(); session=SimpleNamespace(); store=ExpertRunStore(tmp_path/"runs")
    run_id="0123456789abcdef0123456789abcdef"
    store.create(run_id=run_id,conversation_id="chat",task="数据库",attachments=[],profile=_profile().as_dict(),strategy="stable",flow=flow)
    async def no_question(event):
        assert event.get("type") != "ask_user"
    values=await collect_stage_run_inputs(node=flow.node_map["database"],flow=flow,session=session,
        store=store,run_id=run_id,emit=no_question)
    assert values["vector_enabled"] == "false"
    assert store.load(run_id)["run_inputs"] == {"vector_enabled":"false"}
    assert not store.load(run_id).get("gate_confirmations")
