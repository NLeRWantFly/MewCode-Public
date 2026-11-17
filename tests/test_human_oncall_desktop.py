from __future__ import annotations

import asyncio
from pathlib import Path
import socket

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "frontend" / "static"


def _helper(source: str, name: str) -> str:
    start = source.index("  function " + name + "(")
    end = source.index("\n  }\n", start)
    return source[start : end + 4]


def _browser_bundle() -> str:
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    helpers = "\n".join(
        _helper(source, name)
        for name in (
            "setHumanOnCallFeedback",
            "humanOnCallDraftKey",
            "rememberHumanOnCallDraft",
            "forgetHumanOnCallDraft",
            "savedHumanOnCallDraft",
            "preserveHumanOnCallDraft",
            "clearHumanOnCallAckTimer",
            "resetHumanOnCallControls",
            "hideHumanOnCallCard",
            "setHumanOnCallSubmitting",
            "selectHumanOnCallChoice",
            "setHumanOnCallPanelMode",
            "interactionChoiceBundle",
            "preparedQuestionChoices",
            "preparedQuestionOptions",
            "appendChoiceEvidence",
            "choiceInputValue",
            "showHumanOnCall",
            "answerHumanOnCall",
            "handleHumanOnCallResponseResult",
            "markHumanOnCallDisconnected",
        )
    )
    return """
(() => {
  const $ = id => document.getElementById(id);
  const humanOnCallPanel = $("human-oncall-panel");
  let currentHumanOnCall = null, currentAskUser = null;
  const humanOnCallDrafts = new Map();
  const interactionChoiceBundleCache = new Map();
  const activeConversationId = () => "desktop-conversation";
  const addErrorMsg = text => { throw new Error(text); };
  const addSystemMsg = () => {};
  const sent = [];
  let send = message => { sent.push(message); return true; };
  __HELPERS__
  const context = Array.from({length: 80}, (_, i) =>
    "背景第" + (i + 1) + "行：已记录方案范围、证据出处和暂停边界。"
  ).join("\\n");
  const base = {
    conversation_id: "desktop-conversation", name: "choice", question: "选择下一步",
    context, options: [{value: "continue", label: "继续当前方案"}, {value: "pause", label: "保持暂停"}],
    allowOther: false, requires_explicit_selection: true,
  };
  const prompt = extra => ({...extra, conversation_id: "desktop-conversation", questions: [{...base, ...extra}]});
  const fail = message => { throw new Error(message); };
  const expect = (condition, message) => { if (!condition) fail(message); };
  window.runHumanOnCallChecks = () => {
    showHumanOnCall(prompt({request_id: "request-long"}));
    expect(!humanOnCallPanel.classList.contains("hidden"), "Human card hidden");
    expect($("human-oncall-options").clientHeight > 0, "Choice region collapsed to zero");
    expect($("human-oncall-context").scrollHeight > $("human-oncall-context").clientHeight, "Long context has no controlled scroll");
    expect($("human-oncall-other-wrap").classList.contains("hidden"), "Other shown before selection");
    expect($("human-oncall-submit").disabled, "Unselected request was enabled");
    const beforeEmpty = sent.length;
    answerHumanOnCall(false);
    expect(sent.length === beforeEmpty, "Empty choice reached backend");
    humanOnCallPanel.querySelector('[data-value="continue"]').click();
    expect(!$("human-oncall-submit").disabled, "Selected choice stayed disabled");
    showHumanOnCall(prompt({request_id: "request-long"}));
    expect(humanOnCallPanel.querySelector('[data-value="continue"]').getAttribute("aria-checked") === "true", "Same-request replay lost selection");
    expect(!$("human-oncall-submit").disabled, "Same-request replay locked selection");
    answerHumanOnCall(false);
    const submissionsBeforeReplay = sent.length;
    showHumanOnCall(prompt({request_id: "request-long"}));
    expect(currentHumanOnCall.submitting && $("human-oncall-submit").disabled, "In-flight replay unlocked card");
    expect(sent.length === submissionsBeforeReplay, "In-flight replay duplicated submission");
    handleHumanOnCallResponseResult({request_id: "request-long", conversation_id: "desktop-conversation", ok: false, error: "暂时未接受", error_type: "invalid_answer"});
    expect(!currentHumanOnCall.submitting, "Failed in-flight ack left card locked");

    send = () => false;
    answerHumanOnCall(false);
    expect(!humanOnCallPanel.classList.contains("hidden"), "Send failure cleared card");
    expect($("human-oncall-feedback").textContent.includes("未提交"), "Send failure feedback missing");

    send = message => { sent.push(message); return true; };
    answerHumanOnCall(false);
    expect(currentHumanOnCall.submitting, "Submit did not enter pending state");
    handleHumanOnCallResponseResult({request_id: "other", conversation_id: "desktop-conversation", ok: true});
    expect(!humanOnCallPanel.classList.contains("hidden"), "Late ack cleared active card");
    handleHumanOnCallResponseResult({request_id: "request-long", conversation_id: "desktop-conversation", ok: false,
      error: "当前没有可响应的活动人工选择；请恢复任务后再响应，不会自动恢复任务。", error_type: "no_live_request"});
    expect($("human-oncall-feedback").textContent.includes("恢复任务后再响应"), "Paused response feedback missing");
    expect(!humanOnCallPanel.classList.contains("hidden"), "Failed ack cleared card");
    expect(humanOnCallPanel.querySelector('[data-value="continue"]').getAttribute("aria-checked") === "true", "Failed ack lost selection");
    handleHumanOnCallResponseResult({request_id: "request-long", conversation_id: "desktop-conversation", ok: true});
    expect(humanOnCallPanel.classList.contains("hidden"), "Matching success ack did not close card");

    showHumanOnCall(prompt({request_id: "request-busy"}));
    humanOnCallPanel.querySelector('[data-value="continue"]').click();
    answerHumanOnCall(false);
    showHumanOnCall(prompt({request_id: "request-fresh"}));
    expect(!$("human-oncall-cancel").disabled && !$("human-oncall-other").disabled, "New card inherited disabled controls");
    expect($("human-oncall-submit").getAttribute("aria-busy") === "false", "New card inherited busy state");
    handleHumanOnCallResponseResult({request_id: "request-fresh", conversation_id: "desktop-conversation", ok: true});

    showHumanOnCall(prompt({request_id: "request-other", allowOther: true}));
    expect($("human-oncall-other-wrap").classList.contains("hidden"), "Other input shown without choice");
    humanOnCallPanel.querySelector('[data-value="__other__"]').click();
    expect(!$("human-oncall-other-wrap").classList.contains("hidden"), "Other input did not open after choice");
    const beforeOther = sent.length;
    answerHumanOnCall(false);
    expect(sent.length === beforeOther, "Empty Other answer submitted");
    $("human-oncall-other").value = "补充方案";
    $("human-oncall-other").dispatchEvent(new Event("input"));
    preserveHumanOnCallDraft();
    currentHumanOnCall = null;
    humanOnCallPanel.classList.add("hidden");
    showHumanOnCall(prompt({request_id: "request-other", allowOther: true}));
    expect(humanOnCallPanel.querySelector('[data-value="__other__"]').getAttribute("aria-checked") === "true", "Reconnect lost Other selection");
    expect($("human-oncall-other").value === "补充方案", "Reconnect lost Other draft");
    answerHumanOnCall(false);
    expect(sent.at(-1).answers.choice === "补充方案", "Other answer changed");
    handleHumanOnCallResponseResult({request_id: "request-other", conversation_id: "desktop-conversation", ok: true});

    const prepared = {request_id: "request-prepared", conversation_id: "desktop-conversation", questions: [{
      ...base, name: "prepared-choice", question: "选择准备后的方案", options: [{value: "candidate-a", label: "核验方案"}],
      prepared_choices: [
        {id: "candidate-a", label: "核验方案", value: "canonical-a", kind: "value", requires_input: true,
          input_label: "填写核验范围", evidence: ["用户原话：核验范围"], verification: "verified"},
        {id: "manual", label: "其他方案", kind: "action", action: "manual", requires_input: true,
          input_label: "说明其他方案"},
      ], allowOther: true,
    }], choice_bundle: {id: "human-bundle", version: "v1", status: "ready", questions: [{
      name: "prepared-choice", choices: [
        {id: "candidate-a", label: "核验方案", value: "canonical-a", kind: "value", requires_input: true,
          input_label: "填写核验范围", evidence: ["用户原话：核验范围"], verification: "verified"},
        {id: "manual", label: "其他方案", kind: "action", action: "manual", requires_input: true,
          input_label: "说明其他方案"},
      ],
    }]}};
    showHumanOnCall(prepared);
    const preparedCard = humanOnCallPanel.querySelector('[data-value="candidate-a"]');
    expect(preparedCard && preparedCard.getAttribute("aria-checked") === "false", "Prepared recommendation was auto-selected");
    expect(humanOnCallPanel.querySelector(".human-oncall-evidence"), "Prepared evidence was not rendered");
    preparedCard.click();
    $("human-oncall-other").value = "本次核验范围";
    $("human-oncall-other").dispatchEvent(new Event("input"));
    answerHumanOnCall(false);
    expect(sent.at(-1).choice_bundle_id === "human-bundle" && sent.at(-1).choice_version === "v1",
      "Prepared HumanOnCall bundle identity missing");
    expect(sent.at(-1).answers["prepared-choice"] === "candidate-a" &&
      sent.at(-1).choice_inputs["prepared-choice"] === "本次核验范围",
      "Prepared HumanOnCall did not submit candidate id plus custom input");
    handleHumanOnCallResponseResult({request_id: "request-prepared", conversation_id: "desktop-conversation", ok: true});

    showHumanOnCall(prepared);
    humanOnCallPanel.querySelector('[data-value="__other__"]').click();
    $("human-oncall-other").value = "用户自定义方案";
    $("human-oncall-other").dispatchEvent(new Event("input"));
    answerHumanOnCall(false);
    expect(sent.at(-1).answers["prepared-choice"]?.custom === "用户自定义方案" &&
      sent.at(-1).choice_inputs["prepared-choice"] === "用户自定义方案",
      "Prepared HumanOnCall Other did not submit the custom envelope");
    handleHumanOnCallResponseResult({request_id: "request-prepared", conversation_id: "desktop-conversation", ok: true});

    showHumanOnCall(prompt({request_id: "request-old"}));
    humanOnCallPanel.querySelector('[data-value="pause"]').click();
    answerHumanOnCall(false);
    showHumanOnCall(prompt({request_id: "request-new"}));
    handleHumanOnCallResponseResult({request_id: "request-old", conversation_id: "desktop-conversation", ok: true});
    expect(!humanOnCallPanel.classList.contains("hidden"), "Late old ack cleared new card");

    const realSetTimeout = window.setTimeout;
    window.setTimeout = callback => { callback(); return 0; };
    humanOnCallPanel.querySelector('[data-value="continue"]').click();
    answerHumanOnCall(false);
    window.setTimeout = realSetTimeout;
    expect(!currentHumanOnCall.submitting, "Ack timeout left card locked");
    expect($("human-oncall-feedback").textContent.includes("未收到确认回执"), "Ack timeout feedback missing");

    markHumanOnCallDisconnected();
    expect(!humanOnCallPanel.classList.contains("hidden"), "Disconnect discarded card");
    expect(humanOnCallPanel.querySelector('[data-value="continue"]').getAttribute("aria-checked") === "true", "Disconnect discarded choice");
    answerHumanOnCall(true);
    expect(humanOnCallPanel.classList.contains("hidden"), "Expired cancel did not close locally");
    return {choices:true,emptyProtected:true,sendFailure:true,replayRetained:true,controlsReset:true,ack:true,pausedError:true,other:true,lateAck:true,ackTimeout:true,disconnectRetained:true,expiredCancel:true};
  };
  window.showLongHumanOnCallFixture = () => showHumanOnCall(prompt({request_id: "layout-request"}));
})();
""".replace("__HELPERS__", helpers)


@pytest.mark.asyncio
async def test_human_oncall_confirmation_and_long_context_renderer(tmp_path: Path) -> None:
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    shell = (STATIC / "index.html").read_text(encoding="utf-8")
    start = shell.index('<section id="human-oncall-panel"')
    end = shell.index("</section>", start) + len("</section>")
    panel = shell[start:end]

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (
            '<!doctype html><html lang="zh-CN" data-theme="light"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<link rel="stylesheet" href="/static/_tokens.css"><link rel="stylesheet" href="/static/_primitives.css"><link rel="stylesheet" href="/static/style.css">'
            '<body style="margin:0;display:block;padding:0;background:var(--bg);color:var(--text)">'
            + panel
            + '</body></html>'
        )

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    runner = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    serving = asyncio.create_task(runner.serve(sockets=[listener]))
    screenshots = tmp_path / "screenshots"
    screenshots.mkdir()
    try:
        for _ in range(200):
            if runner.started:
                break
            await asyncio.sleep(.01)
        assert runner.started
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="msedge", headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1239, "height": 762})
                await page.goto(f"http://127.0.0.1:{port}")
                await page.evaluate(_browser_bundle())
                behavior = await page.evaluate("window.runHumanOnCallChecks()")
                assert behavior == {
                    "choices": True, "emptyProtected": True, "sendFailure": True,
                    "replayRetained": True, "controlsReset": True,
                    "ack": True, "pausedError": True, "other": True, "lateAck": True,
                    "ackTimeout": True, "disconnectRetained": True, "expiredCancel": True,
                }
                for width, height in ((1239, 762), (1100, 700)):
                    await page.set_viewport_size({"width": width, "height": height})
                    await page.evaluate("window.showLongHumanOnCallFixture()")
                    geometry = await page.locator("#human-oncall-panel").evaluate(
                        "panel=>{const context=document.querySelector('#human-oncall-context'), options=document.querySelector('#human-oncall-options'), actions=document.querySelector('.human-oncall-actions'), submit=document.querySelector('#human-oncall-submit'), other=document.querySelector('#human-oncall-other-wrap'), b=panel.getBoundingClientRect(), a=actions.getBoundingClientRect(); return {panel:b.toJSON(), context:{client:context.clientHeight,scroll:context.scrollHeight}, options:{client:options.clientHeight,scroll:options.scrollHeight}, actions:{bottom:a.bottom,height:a.height}, submitDisabled:submit.disabled, otherHidden:other.classList.contains('hidden'), viewport:{width:innerWidth,height:innerHeight}};}"
                    )
                    assert geometry["panel"]["left"] >= 0 and geometry["panel"]["right"] <= width, geometry
                    assert geometry["panel"]["bottom"] <= height, geometry
                    assert geometry["context"]["scroll"] > geometry["context"]["client"], geometry
                    assert geometry["options"]["client"] >= 48 and geometry["options"]["scroll"] > 0, geometry
                    assert geometry["actions"]["bottom"] <= height and geometry["actions"]["height"] >= 30, geometry
                    assert geometry["submitDisabled"] and geometry["otherHidden"], geometry
                    await page.screenshot(path=str(screenshots / f"human-oncall-{width}x{height}.png"), full_page=False)
            finally:
                await browser.close()
    finally:
        runner.should_exit = True
        await asyncio.wait_for(serving, 5)
        listener.close()
