const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const staticDir = path.resolve(__dirname, "..", "..", "frontend", "static");
const index = fs.readFileSync(path.join(staticDir, "index.html"), "utf8");
const app = fs.readFileSync(path.join(staticDir, "app.js"), "utf8");
const style = fs.readFileSync(path.join(staticDir, "style.css"), "utf8");

test("conversation and trajectory are first-class session views", () => {
  assert.match(index, /role="tablist"[^>]+aria-label="会话视图"/);
  assert.match(index, /<header id="topbar">[\s\S]*?<nav id="conversation-view-tabs"[\s\S]*?<\/header>/);
  assert.doesNotMatch(index, /<\/header>\s*<nav id="conversation-view-tabs"/);
  assert.match(index, /id="view-chat-tab"[^>]+role="tab"/);
  assert.match(index, /id="view-trajectory-tab"[^>]+role="tab"/);
  assert.match(index, /id="trajectory-view"[^>]+role="tabpanel"/);
  assert.match(index, /id="trajectory-overview-track"/);
  assert.match(index, /id="trajectory-details"/);
  assert.doesNotMatch(index, /id="pipeline-panel"/);
  assert.doesNotMatch(index, /id="pipeline-toggle"/);
});

test("trajectory projection contains the DSH-style ledger controls", () => {
  assert.match(app, /function setConversationView\(view, persist = true\)/);
  assert.match(app, /function renderTrajectoryOverview\(state, turns\)/);
  assert.match(app, /function renderTrajectoryDetails\(state\)/);
  assert.match(app, /data-detail-tab/);
  assert.match(app, /trajectorySearchQuery/);
  assert.match(app, /item\.turn_id/);
  assert.match(style, /\.trajectory-row\s*\{/);
  assert.match(style, /\.trajectory-overview-span\s*\{/);
  assert.match(style, /\.trajectory-details\s*\{/);
});

test("chat projection does not insert a reasoning disclosure", () => {
  assert.doesNotMatch(app, /currentThink\s*=\s*document\.createElement/);
  assert.doesNotMatch(app, /<summary>思考过程<\/summary>/);
});
