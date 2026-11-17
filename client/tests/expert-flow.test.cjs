"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const repo = path.resolve(__dirname, "..", "..");
const html = fs.readFileSync(path.join(repo, "frontend", "static", "index.html"), "utf8");
const js = fs.readFileSync(path.join(repo, "frontend", "static", "app.js"), "utf8");
const css = fs.readFileSync(path.join(repo, "frontend", "static", "style.css"), "utf8");
const canvasHtml = fs.readFileSync(path.join(repo, "frontend", "static", "expert-flow.html"), "utf8");
const canvasJs = fs.readFileSync(path.join(repo, "frontend", "static", "expert-flow-canvas.js"), "utf8");
const canvasCss = fs.readFileSync(path.join(repo, "frontend", "static", "expert-flow-canvas.css"), "utf8");

test("expert flow editor persists validated project DAGs", () => {
  assert.match(js, /function createExpertFlowEditor\(/);
  assert.match(js, /fetch\("\/api\/expert-flow"\)/);
  assert.match(js, /method:\s*"PUT"/);
  assert.match(js, /expert-flow-node-editor/);
  assert.match(js, /expert-flow-edge-editor/);
  assert.match(js, /finalizer_plugin/);
  assert.match(css, /\.expert-flow-editor\s*\{/);
  assert.match(css, /\.expert-flow-node-fields\s*\{/);
  assert.match(css, /\.expert-flow-edge-row\s*\{/);
  assert.match(css, /:has\(\.expert-flow-editor\)/);
});

test("expert flow trajectory is separate from the final chat answer", () => {
  assert.match(html, /id="expert-flow-map"/);
  assert.match(js, /expert_flow_defined/);
  assert.match(js, /expert_member_completed/);
  assert.match(js, /expert_handoff/);
  assert.match(js, /msg\.className = "msg assistant expert-message hidden"/);
  assert.match(js, /case "expert_summary":\s*addAssistantMarkdown\(msg\.text\)/);
  assert.match(css, /\.expert-flow-map\s*\{/);
  assert.match(css, /\.expert-flow-node\.is-running/);
});

test("expert flow embeds reusable project pipelines", () => {
  assert.match(js, /let pipelines = flowState\.pipelines/);
  assert.match(js, /value === "pipeline" \? "Pipeline 子流程"/);
  assert.match(js, /function openPipelineCanvas\(/);
  assert.match(js, /mewcodeDesktop\.openPipeline/);
  assert.match(js, /expert-flow-pipeline-workspace/);
  assert.match(js, /Pipeline 列表已刷新/);
  assert.match(js, /expert-pipeline-canvas-jump/);
  assert.match(js, /expert_pipeline_step_started/);
  assert.match(js, /expert-pipeline-step:/);
  assert.match(css, /\.expert-flow-pipeline-reference\s*\{/);
  assert.match(css, /\.expert-flow-pipeline-track\s*\{/);
  assert.match(css, /\.expert-flow-pipeline-workspace\s*\{/);
});

test("expert team and Pipeline canvas share one live flow model", () => {
  assert.match(js, /new BroadcastChannel\("mewcode-expert-flow"\)/);
  assert.match(js, /expert-flow:runtime/);
  assert.match(js, /function expertFlowRuntimeSnapshot/);
  assert.match(js, /publishExpertFlowRuntime\(conversationId\)/);
  assert.match(js, /onFlowChange\(clone\(flow\)\)/);
  assert.match(js, /renderExpertIntro/);
  assert.match(js, /mode: "expert-flow"/);
  assert.match(canvasHtml, /id="expert-svg"/);
  assert.match(canvasHtml, /id="btn-close-flow"/);
  assert.match(canvasHtml, /id="runtime-state"/);
  assert.match(canvasHtml, /desktopTheme \|\| localStorage\.getItem\("mewcode-theme"\)/);
  assert.match(canvasJs, /fetch\("\/api\/expert-flow"/);
  assert.match(canvasJs, /expert-flow:draft/);
  assert.match(canvasJs, /expert-flow:saved/);
  assert.match(canvasJs, /expert-flow:runtime-request/);
  assert.match(canvasJs, /runtimeNodeState/);
  assert.match(canvasJs, /runtimeMembers/);
  assert.match(canvasJs, /saveQueued/);
  assert.match(canvasJs, /new ResizeObserver/);
  assert.match(canvasJs, /viewportTouched/);
  assert.match(canvasJs, /flow\.nodes\.forEach/);
  assert.match(canvasJs, /flow\.edges\.forEach/);
  assert.match(canvasCss, /\.expert-node-card/);
  assert.match(canvasCss, /\.expert-node-card\.is-running/);
  assert.match(canvasCss, /\.expert-edge\.is-active/);
  assert.match(canvasCss, /\.expert-runtime-state\.is-running/);
  assert.match(canvasCss, /\.expert-flow-canvas \.inspector \{ width: 0; \}/);
  assert.match(canvasCss, /\.expert-flow-canvas \.inspector\.open \{ width: 340px; \}/);
});

test("expert Pipeline canvas has a reliable return path", () => {
  assert.match(canvasJs, /async function closeCanvas\(\)/);
  assert.match(canvasJs, /mewcodeDesktop\.closePipeline\(\)/);
  assert.match(canvasJs, /event\.key !== "Escape"/);
  assert.match(canvasJs, /无法关闭 Pipeline/);
});
