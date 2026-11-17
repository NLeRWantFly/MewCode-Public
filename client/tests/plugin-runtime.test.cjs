"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const runtimePath = path.resolve(
  __dirname,
  "..",
  "..",
  "frontend",
  "static",
  "plugin-runtime.js",
);

function createHost() {
  const sandbox = {
    console,
    Promise,
    setTimeout,
    clearTimeout,
    window: {},
  };
  vm.runInNewContext(fs.readFileSync(runtimePath, "utf8"), sandbox, {
    filename: runtimePath,
  });
  return sandbox.window.MewCodeCordis;
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

test("UI plugins own dependent slots, services and cleanup", async () => {
  const host = createHost();
  const events = [];

  host.define("layout", (ctx) => {
    ctx.declare("views", "keyed");
    ctx.provide("theme", "porcelain");
  });
  host.define("trajectory", (ctx) => {
    ctx.register("views", { id: "trajectory" }, { key: "trajectory" });
    ctx.on("view", (value) => events.push(value));
  }, { requires: ["layout"], inject: ["theme"] });

  await host.load("trajectory");
  await host.emit("view", "trajectory");

  assert.equal(host.state("layout"), "active");
  assert.equal(host.state("trajectory"), "active");
  assert.equal(host.resolve("views", null, "trajectory").id, "trajectory");
  assert.deepEqual(events, ["trajectory"]);

  await host.unload("layout");
  assert.equal(host.state("trajectory"), "unloaded");
  assert.equal(host.resolve("views", null, "trajectory"), undefined);
  assert.equal(host.get("theme"), undefined);
});

test("waiting UI plugins resume when injected services appear", async () => {
  const host = createHost();
  let applied = 0;
  host.define("waiting", (ctx) => {
    applied += 1;
    assert.equal(ctx.get("client"), "ready");
  }, { inject: ["client"] });

  await host.load("waiting");
  assert.equal(host.state("waiting"), "waiting");
  host.provide("client", "ready");
  await host.waitForIdle();

  assert.equal(host.state("waiting"), "active");
  assert.equal(applied, 1);
});

test("UI unload awaits cleanup and rejects stale run effects", async () => {
  const host = createHost();
  const cleaned = [];
  let pluginContext;
  host.define("async-cleanup", async (ctx) => {
    pluginContext = ctx;
    return async () => {
      await new Promise((resolve) => setTimeout(resolve, 5));
      cleaned.push("active");
    };
  });

  await host.load("async-cleanup");
  await host.unload("async-cleanup");
  pluginContext.effect(() => cleaned.push("stale"));

  assert.deepEqual(cleaned, ["active", "stale"]);
  assert.equal(host.state("async-cleanup"), "unloaded");
});

test("pending slot injection waits for declaration and follows reloads", async () => {
  const host = createHost();
  const seen = [];
  host.define("consumer", (ctx) => {
    ctx.inject("late-slot", (value) => seen.push(value));
  });
  host.define("provider", (ctx) => {
    ctx.declare("late-slot", "list");
    ctx.register("late-slot", "ready");
  });

  await host.load("consumer");
  assert.deepEqual(seen, []);
  await host.load("provider");
  assert.deepEqual(plain(seen), [[], ["ready"]]);
  await host.unload("provider");
  assert.deepEqual(plain(seen), [[], ["ready"], [], null]);
  await host.load("provider");
  assert.deepEqual(plain(seen), [[], ["ready"], [], null, [], ["ready"]]);
});

test("async slot injections participate in host idle tracking", async () => {
  const host = createHost();
  const seen = [];
  host.define("consumer", (ctx) => {
    ctx.inject("async-slot", async (value) => {
      await new Promise((resolve) => setTimeout(resolve, 5));
      seen.push(value);
    });
  });
  host.define("provider", (ctx) => {
    ctx.declare("async-slot", "list");
    ctx.register("async-slot", "ready", { kind: "list" });
  });

  await host.load("consumer");
  await host.load("provider");
  assert.deepEqual(seen, []);
  await host.waitForIdle();
  assert.deepEqual(plain(seen), [[], ["ready"]]);
});

test("concurrent UI loads share one activation and cycles fail cleanly", async () => {
  const host = createHost();
  let applied = 0;
  host.define("slow", async () => {
    applied += 1;
    await new Promise((resolve) => setTimeout(resolve, 5));
  });
  const [first, second] = await Promise.all([host.load("slow"), host.load("slow")]);
  assert.equal(first, second);
  assert.equal(applied, 1);
  assert.equal(host.state("slow"), "active");

  host.define("cycle-a", () => {}, { requires: ["cycle-b"] });
  host.define("cycle-b", () => {}, { requires: ["cycle-a"] });
  await assert.rejects(host.load("cycle-a"), /cyclic UI plugin dependency/);
  assert.equal(host.state("cycle-a"), "failed");
  assert.equal(host.state("cycle-b"), "failed");
});
