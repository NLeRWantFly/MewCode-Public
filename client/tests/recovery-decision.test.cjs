"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const app = fs.readFileSync(path.resolve(__dirname, "../../frontend/static/app.js"), "utf8");

test("recovery decision renders choices, real model, host and execution result safely", () => {
  const source = app.slice(app.indexOf("function recoveryDecisionText("), app.indexOf("function applyExpertNodeLight("));
  const context = vm.createContext({});
  vm.runInContext(source + "\nglobalThis.format = recoveryDecisionText;", context);
  const text = context.format({action: "diagnose_path", planner_model: "primary", review_route: {model: "jev-version", provider_key: "user-choice"},
    candidates: [{action: "diagnose_path", reason: "<script>untrusted</script>", next_step: "check path", expected_progress: "read file"}],
    confidence: .95, probabilities: {diagnose_path: .95}, host_check: "通过", execution_status: "执行段已结束"});
  assert.match(text, /jev-version/);
  assert.match(text, /95%/);
  assert.match(text, /主模型依据/);
  assert.match(text, /执行段已结束/);
  const render = app.slice(app.indexOf("function applyExpertStep("), app.indexOf("function recoveryDecisionText("));
  assert.match(render, /textContent = recoveryDecisionText/);
  assert.doesNotMatch(render, /innerHTML/);
});

test("replayed decisions update their own bubble without replacing node output", () => {
  const source = app.slice(app.indexOf("function orchChatKey("), app.indexOf("function ensureOrchChatBubble("));
  const context = vm.createContext({orchChatStem: () => "node", orchChatLiveKeys: new Map()});
  vm.runInContext(source + "\nglobalThis.key = orchChatKey;", context);
  const decision = {recovery_decision: {decision_id: "one"}};
  assert.equal(context.key(decision), context.key({...decision, seq: 99}));
  assert.notEqual(context.key(decision), context.key({}));
  assert.notEqual(context.key(decision), context.key({recovery_decision: {decision_id: "two"}}));
});
