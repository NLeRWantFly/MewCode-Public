"""Desktop regressions for recovery cards projected from authoritative run snapshots."""

import asyncio
import copy
from pathlib import Path
import re

import pytest
from playwright.async_api import async_playwright, expect

from tests.test_orchestration_maintenance import maintenance_env as maintenance_env
from tests.test_orchestration_maintenance_ui import install_chat_shell, start_host


ROOT = Path(__file__).resolve().parents[1]
EDGE = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")


def _flow():
    nodes = [
        {"node_id": "n_scope_gate", "role": "manager", "label": "范围门禁", "kind": "role"},
        {"node_id": "n_prep_plan", "role": "planner", "label": "准备阶段方向交接与验证口径", "kind": "role"},
    ]
    nodes.extend({
        "node_id": f"n_stage_{index}", "role": "executor", "label": f"后续阶段 {index}", "kind": "role",
    } for index in range(3, 32))
    return {
        "flow_id": "desktop-recovery-checkpoint",
        "version": 1,
        "label": "桌面恢复检查点回归",
        "nodes": nodes,
        "edges": [{"from": nodes[index - 1]["node_id"], "to": nodes[index]["node_id"]}
                  for index in range(1, len(nodes))],
        "finalizer_node_id": nodes[-1]["node_id"],
        "meta": {},
    }


def _case(case_id="old-prep-case", *, status="reported", closed_by_user=False):
    return {
        "case_id": case_id,
        "node_id": "n_prep_plan",
        "epoch": 0,
        "input_revision": "recovery-revision-1",
        "status": status,
        "closed_by_user": closed_by_user,
        "rounds": [],
        "max_rounds": 5,
        "report": {
            "kind": "incident",
            "case_id": case_id,
            "node_id": "n_prep_plan",
            "node_label": "准备阶段方向交接与验证口径",
            "reason": "旧恢复卡只留下了关闭入口。",
        },
        "pending": {
            "kind": "incident",
            "round": 1,
            "question": "旧恢复卡只留下了关闭入口。",
            "options": [{"value": "close", "label": "关闭当前入口卡"}],
        },
    }


def _snapshot(conversation_id, run_id, *, waiting=False, events=None):
    flow = _flow()
    states = {}
    for index, node in enumerate(flow["nodes"]):
        states[node["node_id"]] = {
            "status": "completed" if index < 2 else "pending",
            "epoch": 0,
            "input_revision": "recovery-revision-1",
        }
    if waiting:
        active_case = _case()
        states["n_prep_plan"].update(status="paused", recovery_case=active_case)
        stage_cursor = "n_prep_plan"
        status = "paused"
        phase = "paused"
    else:
        states["n_prep_plan"].update(status="completed")
        states["n_stage_3"].update(status="running")
        stage_cursor = "n_stage_3"
        status = "running"
        phase = "running"
    return {
        "run_id": run_id,
        "conversation_id": conversation_id,
        "status": status,
        "attempt": 1,
        "stage_cursor": stage_cursor,
        "locked_flow": flow,
        "node_states": states,
        "stages": copy.deepcopy(states),
        "flow_state": {"phase": phase, "status": status, "reason": ""},
        "events": copy.deepcopy(events or []),
        "last_seq": 1,
        "display_index": 0,
        "updated_at": "2026-10-09T00:00:00+00:00",
        "created_at": "2026-10-09T00:00:00+00:00",
        "resume_advice": {
            "action": "continue_checkpoint",
            "run_id": run_id,
            "conversation_id": conversation_id,
        },
    }


def _historical_event(conversation_id, run_id):
    return {
        "type": "recovery_case",
        "conversation_id": conversation_id,
        "run_id": run_id,
        "node_id": "n_prep_plan",
        "node_label": "准备阶段方向交接与验证口径",
        "phase": "已汇报",
        "case": _case(),
    }


def _terminal_snapshot(initial, kind):
    snapshot = copy.deepcopy(initial)
    snapshot["last_seq"] = 2
    snapshot["status"] = "running"
    snapshot["stage_cursor"] = "n_stage_3"
    snapshot["flow_state"] = {"phase": "running", "status": "running", "reason": ""}
    node = snapshot["node_states"]["n_prep_plan"]
    if kind == "completed":
        node["status"] = "completed"
        node.pop("recovery_case", None)
    elif kind == "closed":
        node["status"] = "paused"
        node["recovery_case"] = _case(closed_by_user=True)
    elif kind == "superseded":
        node["status"] = "paused"
        node["recovery_case"] = _case(status="superseded")
    else:  # pragma: no cover - kept local so a typo cannot silently test a live card.
        raise AssertionError(kind)
    snapshot["stages"] = copy.deepcopy(snapshot["node_states"])
    snapshot["events"] = []
    return snapshot


def _skip_without_edge():
    if not EDGE.exists():
        pytest.skip("Isolated headless Edge unavailable")


@pytest.mark.asyncio
async def test_archived_recovery_event_stays_read_only_across_load_refresh_and_snapshot_switch(
    maintenance_env, tmp_path
):
    """A completed old node cannot reappear as a current waiting/stop card."""
    _skip_without_edge()
    app, owner, _, _, _ = maintenance_env
    run_id = "desktop-archived-checkpoint-run"
    old_event = _historical_event(owner["conversation_id"], run_id)
    snapshot = _snapshot(owner["conversation_id"], run_id, events=[old_event])

    @app.get("/api/conversations/project/{identity}")
    async def conversation(identity: str):
        return {"id": identity, "entries": [], "expert_runs": [copy.deepcopy(snapshot)]}

    frames, connections, status = install_chat_shell(app, owner)
    status["status"]["expert_mode"] = True
    host, task, origin = await start_host(app)
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="msedge", headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1400, "height": 1000})
                page_errors = []
                request_urls = []
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.on("request", lambda request: request_urls.append(request.url))
                await page.goto(origin)

                position = page.locator("#orch-board-position")
                await expect(position).to_contain_text("第 3 / 31 步", timeout=15000)
                await expect(page.locator('.orch-board-segments > span')).to_have_count(31)
                assert '进行中 0' not in await page.locator('#orch-board-counts').inner_text()
                await page.locator('#orch-board-tab-progress').click()
                groups = page.locator('.orch-board-node-group')
                await expect(groups).to_have_count(2)
                assert await groups.evaluate_all('(rows)=>rows.every(x=>!x.open)')
                assert await page.locator('#orch-board-stages').evaluate('(x)=>getComputedStyle(x).overflowY') == 'visible'

                toggle = page.locator('#orch-board-toggle')
                await toggle.click()
                await expect(page.locator('#orch-stage-board')).to_have_class(re.compile(r'\bis-collapsed\b'))
                assert await toggle.get_attribute('aria-expanded') == 'false'
                assert await page.locator('.orch-board-title').get_attribute('aria-expanded') == 'false'
                for selector in ('#orch-board-run-controls', '#orch-board-recovery-cases', '#orch-board-tabs',
                                 '#orch-board-live', '#orch-board-stages', '.orch-board-segments'):
                    assert await page.locator(selector).evaluate('(node)=>getComputedStyle(node).display') == 'none'
                await page.locator('.orch-board-title').press('Enter')
                await expect(page.locator('#orch-stage-board')).not_to_have_class(re.compile(r'\bis-collapsed\b'))
                assert await toggle.get_attribute('aria-expanded') == 'true'

                await page.screenshot(path=str(ROOT / '.tmp/recovery-board-desktop.png'), full_page=True)

                board = page.locator("#orch-board-recovery-cases")
                await expect(board.locator(".recovery-case")).to_have_count(0)
                assert "等待你的选择" not in await board.inner_text()

                historical = page.locator("#messages .recovery-case")
                await expect(historical).to_have_count(1)
                await expect(historical.locator(".recovery-case-header strong")).to_have_text("历史修复记录")
                assert await historical.locator("button, textarea, select, input").count() == 0
                assert "当前检查点不再等待这张恢复卡" in await historical.inner_text()
                assert not [url for url in request_urls if url.endswith("/api/orchestration/run/case/answer")]
                assert not [frame for frame in frames if frame.get("type") == "expert_resume"]
                assert not page_errors, page_errors

                await page.reload()
                await expect(position).to_contain_text("第 3 / 31 步", timeout=15000)
                await expect(page.locator("#messages .recovery-case")).to_have_count(1)
                await expect(page.locator("#messages .recovery-case .recovery-case-header strong")).to_have_text(
                    "历史修复记录"
                )
                await expect(board.locator(".recovery-case")).to_have_count(0)
                assert not [frame for frame in frames if frame.get("type") == "expert_resume"]

                switched = copy.deepcopy(snapshot)
                switched["last_seq"] = 2
                switched["events"] = [old_event]
                for _ in range(100):
                    if connections:
                        break
                    await asyncio.sleep(0.01)
                assert connections
                await connections[-1].send_json({
                    "type": "expert_checkpoint_restored",
                    "conversation_id": owner["conversation_id"],
                    "run_id": run_id,
                    "snapshot": switched,
                })
                await expect(position).to_contain_text("第 3 / 31 步", timeout=15000)
                await expect(board.locator(".recovery-case")).to_have_count(0)
                historical = page.locator("#messages .recovery-case")
                await expect(historical).to_have_count(1)
                await expect(historical.locator(".recovery-case-header strong")).to_have_text("历史修复记录")
                assert await historical.locator("button, textarea, select, input").count() == 0
                assert not [url for url in request_urls if url.endswith("/api/orchestration/run/case/answer")]
                assert not [frame for frame in frames if frame.get("type") == "expert_resume"]
                assert not page_errors, page_errors
            finally:
                await browser.close()
    finally:
        host.should_exit = True
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_kind", ["completed", "closed", "superseded"])
async def test_waiting_card_cleanup_drops_inflight_preparation_for_terminal_snapshot(
    maintenance_env, terminal_kind
):
    """A late prepare response cannot revive a case removed by a newer snapshot."""
    _skip_without_edge()
    app, owner, _, _, _ = maintenance_env
    run_id = f"desktop-waiting-cleanup-{terminal_kind}"
    initial = _snapshot(owner["conversation_id"], run_id, waiting=True)

    @app.get("/api/conversations/project/{identity}")
    async def conversation(identity: str):
        return {"id": identity, "entries": [], "expert_runs": [copy.deepcopy(initial)]}

    frames, connections, status = install_chat_shell(app, owner)
    status["status"]["expert_mode"] = True
    prepare_seen = asyncio.Event()
    release_prepare = asyncio.Event()
    prepare_payloads = []
    prepared_candidate = "late-preparation-candidate"

    async def prepare_route(route):
        prepare_payloads.append(route.request.post_data_json)
        prepare_seen.set()
        await release_prepare.wait()
        await route.fulfill(json={
            "conversation_id": owner["conversation_id"],
            "run_id": run_id,
            "node_id": "n_prep_plan",
            "request_id": "old-prep-case",
            "case_id": "old-prep-case",
            "choice_bundle": {
                "id": "late-bundle",
                "version": "1",
                "status": "ready",
                "questions": [{
                    "name": "plan",
                    "choices": [{"id": prepared_candidate, "label": prepared_candidate,
                                 "kind": "value", "verification": "verified", "evidence": ["fixture"]}],
                }],
            },
            "questions": [{
                "name": "plan",
                "choices": [{"id": prepared_candidate, "label": prepared_candidate}],
            }],
        })

    host, task, origin = await start_host(app)
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="msedge", headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1400, "height": 1000})
                page_errors = []
                request_urls = []
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.on("request", lambda request: request_urls.append(request.url))
                await page.route("**/api/orchestration/interaction-choices/prepare", prepare_route)
                await page.goto(origin)
                await asyncio.wait_for(prepare_seen.wait(), timeout=15)
                board = page.locator("#orch-board-recovery-cases")
                await expect(board.locator(".recovery-case")).to_have_count(1)
                await expect(board.get_by_text("正在准备处理方案", exact=True)).to_be_visible()
                assert len(prepare_payloads) == 1
                assert prepare_payloads[0]["run_id"] == run_id
                assert prepare_payloads[0]["case_id"] == "old-prep-case"

                terminal = _terminal_snapshot(initial, terminal_kind)
                for _ in range(100):
                    if connections:
                        break
                    await asyncio.sleep(0.01)
                assert connections
                await connections[-1].send_json({
                    "type": "expert_checkpoint_restored",
                    "conversation_id": owner["conversation_id"],
                    "run_id": run_id,
                    "snapshot": terminal,
                })
                await expect(board.locator(".recovery-case")).to_have_count(0, timeout=15000)
                assert "等待你的选择" not in await board.inner_text()
                assert prepared_candidate not in await page.locator("body").inner_text()
                historical = page.locator("#messages .recovery-case")
                await expect(historical).to_have_count(1)
                await expect(historical.locator(".recovery-case-header strong")).to_have_text("历史修复记录")
                assert await historical.locator("button, textarea, select, input").count() == 0

                release_prepare.set()
                await page.wait_for_timeout(250)
                await expect(board.locator(".recovery-case")).to_have_count(0)
                assert prepared_candidate not in await page.locator("body").inner_text()
                assert len(prepare_payloads) == 1
                assert not [url for url in request_urls if url.endswith("/api/orchestration/run/case/answer")]
                assert not [frame for frame in frames if frame.get("type") == "expert_resume"]
                assert not page_errors, page_errors
            finally:
                release_prepare.set()
                await browser.close()
    finally:
        host.should_exit = True
        await task
