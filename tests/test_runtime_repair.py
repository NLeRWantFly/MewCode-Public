import asyncio
import copy
from types import SimpleNamespace

import pytest
from mcp import types

from mewcode.mcp.tool_wrapper import _extract_content
from mewcode.tools.web_search import WebSearch, Params
from mewcode.tools.browser import Browser, WebFetch, WebFetchParams
from mewcode.llm_watchdog import watch_provider_stream, ProviderStreamTimeout
from mewcode.tools.base import TextDelta, StreamStatus
from mewcode.experts.loop_runtime import run_bounded_loop
from mewcode.expert_group import ExpertRoleResult, ExpertExecutionPolicy
from mewcode.orchestration.loops import LoopContract
from mewcode.orchestration.convert import to_expert_flow
from tests.test_generic_loops import spec_for, report
from tests.test_acceptance_completion import old_false_completion, branch_flow


def test_mcp_text_is_not_lost_without_images():
    assert _extract_content([types.TextContent(type="text", text="Actual page evidence")]) == ("Actual page evidence", [])
    assert _extract_content([]) == ("(no output)", [])
    text, blocks = _extract_content([types.ImageContent(type="image", data="AA==", mimeType="image/png")])
    assert text == "(image output)" and blocks


def test_search_rejects_unrelated_public_response():
    from mewcode.tools.public_search import format_results
    with pytest.raises(ValueError, match="unrelated"):
        format_results([{"title": "Yoga strength", "url": "https://example.com/yoga"}], "Attention arxiv 1706.03762", "bing", 5)


def test_installed_edge_selected_for_ephemeral_browser(tmp_path, monkeypatch):
    from mewcode.mcp import playwright_browser
    monkeypatch.setattr(playwright_browser.os, "name", "nt")
    edge = tmp_path / "Microsoft/Edge/Application/msedge.exe"
    edge.parent.mkdir(parents=True)
    edge.touch()
    cfg = playwright_browser.build_playwright_config("ephemeral", env={"PROGRAMFILES(X86)": str(tmp_path)})
    assert "--isolated" in cfg.args and str(edge) in cfg.args


@pytest.mark.asyncio
async def test_search_missing_config_and_failure_cooldown_cache():
    calls = []
    async def fetch(url):
        calls.append(url)
        raise RuntimeError("429")
    search = WebSearch(auth_resolver=lambda _: ("", "", ""), fetch_text=fetch, browser_available=lambda: True,
                       tavily_auth_resolver=lambda _: "")
    first, second = await asyncio.gather(search.execute(Params(query="same")), search.execute(Params(query=" SAME ")))
    assert first.is_error and second.is_error and len(calls) == 2
    # Distinct query text is kept distinct: case-sensitive searches must not be
    # silently merged. Provider cooldown still prevents another network call.
    assert "browser_navigate" in first.output and "browser_navigate" in second.output
    repeated = await search.execute(Params(query="same"))
    assert "cached search" in repeated.output
    await search.execute(Params(query="different"))
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_fetch_does_not_repeat_failed_url():
    calls = []
    async def fetch(url):
        calls.append(url)
        raise RuntimeError("429")
    tool = WebFetch(browser=Browser(fetch_bytes=fetch))
    for _ in range(3):
        result = await tool.execute(WebFetchParams(url="https://example.com/source"))
        assert result.is_error
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_slow_stream_keeps_same_request_then_recovers():
    starts = []
    async def stream():
        starts.append(1)
        yield TextDelta("first")
        await asyncio.sleep(.035)
        yield TextDelta("second")
    events = [e async for e in watch_provider_stream(stream(), idle_timeout=.02)]
    assert starts == [1]
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["first", "second"]
    assert any(isinstance(e, StreamStatus) and e.stage == "provider_slow" for e in events)


@pytest.mark.asyncio
async def test_stream_hard_timeout_is_typed_and_closed():
    closed = []
    async def stream():
        try:
            yield TextDelta("partial")
            await asyncio.sleep(2)
        finally:
            closed.append(True)
    with pytest.raises(ProviderStreamTimeout) as failure:
        async for _ in watch_provider_stream(stream(), idle_timeout=.01):
            pass
    assert failure.value.phase == "output" and closed
    from mewcode.errors import is_transient_error
    assert not is_transient_error(failure.value)


@pytest.mark.asyncio
async def test_foreach_resets_convergence_not_global_usage():
    spec = spec_for()
    spec.meta["loops"][0].update(mode="foreach", items=[{"item_id": str(i), "goal": "summarize"} for i in range(4)], max_iterations=1)
    flow = to_expert_flow(spec)
    contract = LoopContract.from_value(flow.meta["loops"][0])
    states, calls = {}, []
    async def emit(event):
        pass
    async def runner(node, incoming, policy, prompt):
        calls.append((node.node_id, policy.loop_context["current_item"]["item_id"], policy.resume_messages))
        return ExpertRoleResult(text="done", input_tokens=10, loop_decision=report(policy.loop_context) if node.node_id == "check" else None)
    _, result = await run_bounded_loop(contract=contract, flow=flow, saved=states, run_node=runner, emit=emit,
                                     policy_for=lambda _: ExpertExecutionPolicy("stable", False, 0), incoming=[])
    assert result.status == "completed" and len(calls) == 8
    assert all(not resume for _, _, resume in calls)
    assert states["improve"]["completed_items"] == ["0", "1", "2", "3"]
    assert states["improve"]["tokens"] == 80 and states["improve"]["round"] == 4
    assert list(result.handoff["queue_results"]) == ["0", "1", "2", "3"]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_interruption_attributed_and_resume_preserves_finished_nodes(tmp_path, legacy):
    flow = to_expert_flow(spec_for())
    contract = LoopContract.from_value(flow.meta["loops"][0])
    states, calls, events = {}, [], []
    fail = True
    async def emit(event):
        events.append(copy.deepcopy(event))
    async def runner(node, incoming, policy, prompt):
        calls.append((node.node_id, policy.resume_messages))
        if node.node_id == "check" and fail:
            return ExpertRoleResult(text="stream stopped", status="needs_attention", error_kind="provider_interrupted", failed_node_id="check", input_tokens=10)
        return ExpertRoleResult(text="done", input_tokens=10, loop_decision=report(policy.loop_context) if node.node_id == "check" else None)
    kwargs = dict(contract=contract, flow=flow, run_node=runner, emit=emit, policy_for=lambda _: ExpertExecutionPolicy("stable", False, 0), incoming=[])
    _, stopped = await run_bounded_loop(saved=states, **kwargs)
    assert stopped.error_kind == "provider_interrupted" and stopped.failed_node_id == "check"
    if legacy:
        states["improve"].pop("error_kind")
        states["improve"].pop("failed_node_id")
        states["improve"]["round_results"]["check"]["text"] = "provider stalled: no response activity for 60s"
    runs, run_id = old_false_completion(tmp_path)
    runs.mutate(run_id, lambda s: s.update(loop_states=copy.deepcopy(states), status="blocked", locked_flow=flow.as_dict(),
        node_states={"prepare": {"status": "completed"}, "work": {"status": "completed"},
                     "check": {"status": "needs_attention"}, "done": {"status": "pending"}}))
    resumed = runs.resume(run_id, strategy="stable")
    state = resumed["loop_states"]["improve"]
    assert state["tokens"] == 20 and state["round"] == 1 and "work" in state["round_results"]
    fail = False
    _, done = await run_bounded_loop(saved=resumed["loop_states"], **kwargs)
    assert done.status == "completed" and calls == [("work", False), ("check", False), ("check", True)]
    assert resumed["loop_states"]["improve"]["tokens"] == 30


@pytest.mark.asyncio
async def test_gate_modification_requires_new_proposal_and_explicit_approval(tmp_path):
    from mewcode.experts.human_gate import await_gate_decision
    from mewcode.experts.gate_dialogue import confirmation_valid
    runs, run_id = old_false_completion(tmp_path)
    flow, session, proposals, visible = branch_flow(), SimpleNamespace(), [], []
    async def build(prompt):
        proposals.append(prompt)
        return f"方案{len(proposals)}：仅整理已核验资料。"
    async def emit(event):
        if event["type"] == "ask_user":
            session._ask_user_request.future.set_result({"requirements": "保留引用"})
        if event["type"] == "human_oncall":
            assert not runs.load(run_id).get("gate_confirmations")
            visible.append(event)
            session._human_oncall_request.future.set_result({"choice": "增加 DOI" if len(visible) == 1 else "continue"})
    assert await await_gate_decision(node=flow.node_map["handoff"], flow=flow, session=session, store=runs,
                                     run_id=run_id, emit=emit, proposal_builder=build)
    assert len(proposals) == 2 and "增加 DOI" in proposals[1]
    assert confirmation_valid(flow.node_map["handoff"], flow, runs.load(run_id))
    runs.mutate(run_id, lambda s: s["node_states"]["audit"].update(text="changed upstream"))
    assert not confirmation_valid(flow.node_map["handoff"], flow, runs.load(run_id))


@pytest.mark.asyncio
async def test_failed_worker_is_not_attributed_to_unexecuted_reviewer():
    flow = to_expert_flow(spec_for())
    events, calls = [], []
    async def emit(event):
        events.append(event)
    async def runner(node, *_):
        calls.append(node.node_id)
        raise RuntimeError("broken configuration")
    _, result = await run_bounded_loop(contract=LoopContract.from_value(flow.meta["loops"][0]), flow=flow,
        saved={}, run_node=runner, emit=emit, policy_for=lambda _: ExpertExecutionPolicy("stable", False, 0), incoming=[])
    assert result.failed_node_id == "work" and result.error_kind == "node_failure"
    assert calls == ["work"]
    assert [e["node_id"] for e in events if e["type"] == "expert_stage"] == ["work"]


@pytest.mark.asyncio
async def test_configured_search_rejection_is_cooled_down():
    calls = []
    async def post(*args):
        calls.append(1)
        return {"error": "unsupported search tool"}
    async def fetch(url):
        return '<rss><channel><item><title>Relevant source</title><link>https://example.com/source</link></item></channel></rss>'
    search = WebSearch(auth_resolver=lambda _: ("test-key", "https://example.com", "test-model"), post_json=post, fetch_text=fetch,
                       tavily_auth_resolver=lambda _: "")
    assert not (await search.execute(Params(query="source one"))).is_error
    assert not (await search.execute(Params(query="source two"))).is_error
    assert calls == [1]
