"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const root = path.resolve(__dirname, "../..");
const main = fs.readFileSync(path.join(root, "client/main.cjs"), "utf8");
const app = fs.readFileSync(path.join(root, "frontend/static/app.js"), "utf8");

test("desktop accepts the native Jev protocol without loading Electron", () => {
  const presets = main.slice(main.indexOf("const providerPresets ="), main.indexOf("let mainWindow ="));
  const validate = main.slice(main.indexOf("function validateProviderInput("), main.indexOf("function installIpc()"));
  const context = vm.createContext({URL, randomUUID: () => "test", isOfficialOpenRouter: () => false});
  vm.runInContext(presets + validate + '\nglobalThis.check = validateProviderInput;', context);
  const result = context.check({brand: "typesafe", api_key: "secret"});
  assert.equal(result.protocol, "typesafe");
  assert.equal(result.default_model, "jev-latest");
  assert.equal(result.base_url, "https://api.typesafe.ai/v1");
  assert.equal(result.api_key, undefined);
  assert.throws(() => context.check({brand: "typesafe", base_url: "https://key@example.test"}));
});

test("review UI has an explicit visual fallback and excludes text-only models", () => {
  const review = app.slice(app.indexOf('const review = (modelCatalog'), app.indexOf('metaStrip.appendChild(reviewBox)'));
  assert.match(review, /vision_model_id/);
  assert.match(review, /vision_provider_key/);
  assert.match(review, /!model\.vision \|\| model\.review_only/);
  assert.match(review, /视觉评审模型/);
  assert.match(app, /if \(model\.review_only\) return false/);
});

test("review picker reflects saved provider and rolls back unavailable choices", () => {
  const source = app.slice(app.indexOf("function restoreReviewSelection("), app.indexOf("function renderRuntimeSettingsPanel("));
  const context = vm.createContext({document: {createElement: () => ({dataset: {}})}});
  vm.runInContext(source + '\nglobalThis.restore = restoreReviewSelection;', context);
  const select = {options: [
    {value: "", dataset: {}},
    {value: "jev-latest", dataset: {providerKey: "first"}},
    {value: "jev-latest", dataset: {providerKey: "second"}},
  ], appendChild(option) { this.options.push(option); }};
  context.restore(select, "jev-latest", "second");
  assert.equal(select.options[2].selected, true);
  assert.equal(select.options[1].selected, false);
  context.restore(select, "missing-model", "missing-provider");
  assert.equal(select.options[3].selected, true);
  assert.equal(select.options[3].disabled, true);
  context.restore(select, "", "");
  assert.equal(select.options[0].selected, true);
  assert.equal(select.options[3].selected, false);
  const review = app.slice(app.indexOf('const review = (modelCatalog'), app.indexOf('metaStrip.appendChild(reviewBox)'));
  assert.doesNotMatch(review, /const luna/);
  assert.match(review, /restoreReviewSelection\(reviewSelect, saved\.model_id/);
});
