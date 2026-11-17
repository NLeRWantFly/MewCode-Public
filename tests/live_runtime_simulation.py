"""Opt-in live model + browser + public search scenario. Never run in normal pytest.

Usage: python -m tests.live_runtime_simulation --work-dir <isolated test directory>
Uses existing desktop provider credentials in memory; never exports secrets.
"""
import argparse
import asyncio
import copy
import json
from pathlib import Path

from mewcode.agent import Agent, StreamText, ToolUseEvent, ToolResultEvent, ErrorEvent, UsageEvent
from mewcode.conversation import ConversationManager
from mewcode.expert_group import ExpertExecutionPolicy, ExpertRoleResult
from mewcode.experts.loop_runtime import run_bounded_loop
from mewcode.mcp.client import MCPClient
from mewcode.mcp.playwright_browser import build_playwright_config
from mewcode.mcp.tool_wrapper import MCPToolWrapper
from mewcode.model_registry import ProviderRegistry, ModelRef
from mewcode.orchestration.convert import to_expert_flow
from mewcode.orchestration.loops import LoopContract
from mewcode.runtime_paths import user_data_root
from mewcode.tools import ToolRegistry
from mewcode.tools.browser import WebFetch
from mewcode.tools.web_search import WebSearch
from mewcode.tools.submit_handoff import HandoffHolder, SubmitHandoff
from mewcode.tools.submit_verdict import VerdictHolder
from mewcode.tools.submit_loop_decision import SubmitLoopDecisionTool
from tests.test_generic_loops import spec_for


async def main(root):
    root.mkdir(parents=True, exist_ok=True)
    profiles = json.loads((user_data_root() / "providers.json").read_text(encoding="utf-8"))
    providers = ProviderRegistry()
    providers.replace_desktop_profiles(profiles)
    refs = {}
    for model, effort in [("grok-4.6", "high"), ("gpt-6-astra", "high")]:
        record = next(r for r in providers.records() if model in r.models)
        record.provider.usage.enabled = False
        refs[model] = ModelRef(record.key, model, effort)
    spec = spec_for("真实论文资料核验模拟")
    spec.meta["loops"][0].update(mode="foreach", items=[
        {"item_id": "Q01", "goal": "Attention Is All You Need: https://arxiv.org/abs/1706.03762"},
        {"item_id": "Q02", "goal": "LoRA Low-Rank Adaptation of Large Language Models: https://arxiv.org/abs/2106.09685"}],
        max_iterations=2, token_budget=180000, timeout_seconds=1800, node_max_iterations=8, reviewer_max_iterations=4,
        criteria=[{"criterion_id": "REQ-01", "acceptance_test": "当前论文标题、官方链接和一句方法概述由实际打开的官方页面支持；不得把摘要阅读冒充全文核验"}])
    flow = to_expert_flow(spec)
    contract = LoopContract.from_value(flow.meta["loops"][0])
    states, traces, conversations = {}, [], []
    mcp = MCPClient(build_playwright_config("ephemeral"))
    await mcp.connect()
    definitions = await mcp.list_tools()
    print("MCP connected; isolated headless browser", flush=True)
    search = WebSearch(auth_resolver=lambda _: ("", "", ""), browser_available=lambda: True)

    def save():
        (root / "trace.json").write_text(json.dumps(traces, ensure_ascii=False, indent=2), encoding="utf-8")
        (root / "checkpoint.json").write_text(json.dumps(states, ensure_ascii=False, indent=2), encoding="utf-8")

    async def emit(event):
        if event.get("loop_state"):
            save()

    async def runner(node, incoming, policy, prompt):
        reviewer = policy.loop_context["reviewer"]
        item = policy.loop_context["current_item"]
        name = "gpt-6-astra" if reviewer else "grok-4.6"
        resolved = providers.resolve(refs[name])
        registry, handoff, decision = ToolRegistry(), HandoffHolder(), VerdictHolder()
        if reviewer:
            registry.register(WebFetch())
            registry.register(SubmitLoopDecisionTool(decision, policy.loop_context))
        else:
            registry.register(search)
            for definition in definitions:
                if definition.name in {"browser_navigate", "browser_snapshot"}:
                    registry.register(MCPToolWrapper("playwright", definition, mcp, canonical_names=True))
            registry.register(SubmitHandoff(handoff, work_dir=str(root)))
        conversation = ConversationManager()
        conversations.append(conversation)
        task = ("真实场景测试，仅处理当前论文。先 WebSearch 一次检索论文，再通过 browser_navigate 和 browser_snapshot 打开给定的官方页面。"
                "不下载、不点按钮、不登录、不绕过限制。若搜索不可用，用浏览器搜索或给定的已知来源，不重复配置错误。"
                "根据实际页面提交 SubmitHandoff：标题、官方URL、一句中文方法概述、一个短证据摘录；evidence.kind=web。"
                "只读了摘要就明确说明。不要声称写了文件。提交后立即结束，不处理下一项。") if not reviewer else (
                "只核验当前项上游交接。可 WebFetch 官方页面一次独立验证，提交 SubmitLoopDecision 后结束。"
                "不得把后续未执行论文记为当前项失败；若证据不符，诚实 CONTINUE 或 BLOCKED。")
        conversation.add_user_message(task + "\n" + prompt + "\n上游交接：" + json.dumps(incoming, ensure_ascii=False))
        agent = Agent(resolved.client, registry, resolved.protocol, work_dir=str(root), max_iterations=policy.max_iterations,
                      instructions_content="这是隔离测试。仅允许本注册表中的只读网页访问和交接，不执行其他工作。", stall_policy=False)
        agent.provider_watchdog = True
        text, status = [], "completed"
        trace = {"node": node.node_id, "item": item["item_id"], "model": name, "independent_context": len(conversation.get_messages()) == 1,
                 "tools": []}
        traces.append(trace)
        print(f"START {item['item_id']} {node.node_id} {name}", flush=True)
        stream = agent.run(conversation)
        try:
            async with asyncio.timeout(480):
                async for event in stream:
                    if isinstance(event, StreamText):
                        text.append(event.text)
                    elif isinstance(event, ToolUseEvent):
                        trace["tools"].append({"tool": event.tool_name, "args": event.arguments})
                        print(f"TOOL {item['item_id']} {event.tool_name}", flush=True)
                    elif isinstance(event, ToolResultEvent):
                        trace["tools"].append({"result_for": event.tool_name, "error": event.is_error, "output": event.output[:16000]})
                    elif isinstance(event, UsageEvent):
                        await policy.usage_callback(event.billable_tokens if event.billable_tokens is not None else agent.total_input_tokens + agent.total_output_tokens)
                    elif isinstance(event, ErrorEvent):
                        status = "failed"
                        text.append(event.message)
                    save()
                    if handoff.read() or decision.read():
                        break
        except Exception as exc:
            status = "needs_attention"
            text.append(type(exc).__name__ + ": " + str(exc)[:400])
        finally:
            await stream.aclose()
        trace.update(status=status, text="".join(text), handoff=handoff.read(), decision=decision.read(),
                     tokens=agent.total_input_tokens + agent.total_output_tokens)
        if not (handoff.read() if not reviewer else decision.read()):
            status = "needs_attention"
        elif not reviewer:
            status = handoff.read().get("status", "completed")
        print(f"END {item['item_id']} {node.node_id} {status}", flush=True)
        save()
        return ExpertRoleResult(text="".join(text), status=status, handoff=handoff.read(), loop_decision=decision.read(),
                                input_tokens=agent.total_input_tokens, output_tokens=agent.total_output_tokens)
    try:
        _, result = await run_bounded_loop(contract=contract, flow=flow, saved=states, run_node=runner, emit=emit,
            policy_for=lambda _: ExpertExecutionPolicy("stable", False, 0), incoming=[])
        report = {"status": result.status, "reason": result.text, "queue": states["improve"].get("completed_items", []),
                  "rounds": states["improve"]["round"], "tokens": states["improve"]["tokens"],
                  "live_models": [t["model"] for t in traces], "calls": len(traces),
                  "independent_contexts": len({id(c) for c in conversations}) == len(conversations),
                  "mode": "real Agent tool calls and provider requests; no mocked responses; no original task mutation"}
        (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False), flush=True)
    finally:
        await mcp.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.work_dir.resolve()))
