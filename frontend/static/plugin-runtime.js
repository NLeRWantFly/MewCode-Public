/* Browser-side Cordis runtime for lifecycle-owned UI contributions. */
(() => {
  "use strict";

  const isPromise = (value) => value && typeof value.then === "function";

  class Effect {
    constructor(cleanup) {
      if (cleanup == null) this.cleanup = () => {};
      else if (typeof cleanup === "function") this.cleanup = cleanup;
      else if (typeof cleanup.dispose === "function") this.cleanup = () => cleanup.dispose();
      else throw new TypeError("UI plugin cleanup must be callable");
      this.disposed = false;
    }

    dispose() {
      if (this.disposed) return undefined;
      this.disposed = true;
      const cleanup = this.cleanup;
      this.cleanup = () => {};
      return cleanup();
    }
  }

  class Slot {
    constructor(name, kind, owner, host) {
      this.name = name;
      this.kind = kind;
      this.owner = owner;
      this.host = host;
      this.entries = new Map();
      this.listeners = new Map();
    }

    register(plugin, value, options = {}) {
      const key = options.key || null;
      if (this.kind === "single" && this.entries.size) {
        throw new Error("single UI slot is occupied: " + this.name);
      }
      if (this.kind === "keyed" && !key) {
        throw new Error("keyed UI slot needs a key: " + this.name);
      }
      if (
        this.kind === "keyed"
        && [...this.entries.values()].some((entry) => entry.key === key)
      ) {
        throw new Error("UI slot key is occupied: " + key);
      }
      const token = plugin + ":" + Math.random().toString(36).slice(2);
      this.entries.set(token, {
        token,
        plugin,
        key,
        value,
        priority: Number(options.priority) || 0,
        select: options.select,
      });
      this.notify();
      return new Effect(() => {
        if (!this.entries.delete(token)) return;
        this.notify();
      });
    }

    resolve(owner, key) {
      const entries = [...this.entries.values()].sort((a, b) => b.priority - a.priority);
      if (this.kind === "list") return entries.map((entry) => entry.value);
      if (this.kind === "keyed") {
        if (key == null) {
          return Object.fromEntries(entries.map((entry) => [entry.key, entry.value]));
        }
        return entries.find((entry) => entry.key === key)?.value;
      }
      if (this.kind === "chain") {
        return entries.find((entry) => !entry.select || entry.select(owner))?.value;
      }
      return entries[0]?.value;
    }

    inject(injection) {
      this.listeners.set(injection.token, injection);
      this.invoke(injection, this.resolve(injection.owner, injection.key));
    }

    removeInjection(token) {
      this.listeners.delete(token);
    }

    notify() {
      for (const injection of this.listeners.values()) {
        this.invoke(injection, this.resolve(injection.owner, injection.key));
      }
    }

    notifyMissing() {
      for (const injection of this.listeners.values()) this.invoke(injection, undefined);
    }

    invoke(injection, value) {
      try {
        const result = injection.callback(value);
        if (isPromise(result)) {
          this.host._trackPromise(result, "UI slot injection failed");
        }
      } catch (error) {
        console.error("UI slot injection failed", error);
      }
    }
  }

  class PluginContext {
    constructor(host, name, runId) {
      this.host = host;
      this.name = name;
      this.runId = runId;
    }

    effect(cleanup) {
      return this.host._effect(this, cleanup);
    }

    declare(name, kind = "single") {
      return this.host._declare(name, kind, this.name, this);
    }

    register(slotName, value, options = {}) {
      return this.host._register(slotName, this.name, value, options, this);
    }

    inject(slotName, callback, options = {}) {
      return this.host._inject(slotName, this.name, callback, options, this);
    }

    provide(name, value, options = {}) {
      return this.host._provide(name, value, options, this);
    }

    consume(name, callback) {
      return this.host._consume(name, callback, this);
    }

    get(name, fallback) {
      return this.host.get(name, fallback);
    }

    on(event, callback) {
      return this.host._on(event, callback, this);
    }

    emit(event, payload) {
      return this.host.emit(event, payload);
    }

    resolve(slotName, owner, key) {
      return this.host.resolve(slotName, owner, key);
    }
  }

  class Host {
    constructor() {
      this.slots = new Map();
      this.pendingInjections = new Map();
      this.plugins = new Map();
      this.services = new Map();
      this.consumers = new Map();
      this.events = new Map();
      this.effects = [];
      this.pending = new Set();
      this.closed = false;
      this.runSequence = 0;
    }

    define(nameOrSpec, apply, options = {}) {
      const spec = typeof nameOrSpec === "string"
        ? { name: nameOrSpec, apply, ...options }
        : { ...nameOrSpec };
      if (!spec.name || typeof spec.apply !== "function") {
        throw new TypeError("UI plugin needs a name and apply function");
      }
      const existing = this.plugins.get(spec.name);
      if (existing && ["loading", "waiting", "active", "unloading"].includes(existing.state)) {
        throw new Error("cannot replace running UI plugin: " + spec.name);
      }
      const record = {
        spec: {
          name: spec.name,
          apply: spec.apply,
          requires: [...(spec.requires || [])],
          inject: [...(spec.inject || [])],
          version: String(spec.version || "0"),
          purpose: String(spec.purpose || ""),
        },
        state: "registered",
        runId: "",
        effects: [],
        error: null,
        loading: null,
      };
      this.plugins.set(spec.name, record);
      return spec.name;
    }

    plugin(nameOrSpec, apply, options = {}) {
      return this.define(nameOrSpec, apply, options);
    }

    async load(name, stack = []) {
      if (this.closed) throw new Error("UI plugin host is closed");
      if (stack.includes(name)) {
        throw new Error("cyclic UI plugin dependency: " + [...stack, name].join(" -> "));
      }
      const record = this.plugins.get(name);
      if (!record) throw new Error("UI plugin is not defined: " + name);
      if (record.state === "active") return record;
      if (record.state === "loading" && record.loading) return record.loading;
      if (record.state === "waiting" && this._missing(record).length) return record;

      if (record.state !== "waiting") {
        record.runId = "ui-run-" + (++this.runSequence);
        record.effects = [];
        record.error = null;
      }
      record.state = "loading";
      const nextStack = [...stack, name];
      const activation = (async () => {
        try {
          for (const dependency of record.spec.requires) {
            await this.load(dependency, nextStack);
          }
          const missing = this._missing(record);
          if (missing.length) {
            record.state = "waiting";
            return record;
          }
          const context = new PluginContext(this, name, record.runId);
          let cleanup = record.spec.apply(context);
          if (isPromise(cleanup)) cleanup = await cleanup;
          if (cleanup != null) this._effect(context, cleanup);
          record.state = "active";
          this._resumeWaiting();
          return record;
        } catch (error) {
          await this._disposeEffects(record.effects);
          record.state = "failed";
          record.error = String(error?.stack || error);
          throw error;
        }
      })();
      record.loading = activation;
      activation.then(
        () => {
          if (record.loading === activation) record.loading = null;
        },
        () => {
          if (record.loading === activation) record.loading = null;
        },
      );
      return activation;
    }

    async unload(name) {
      const record = this.plugins.get(name);
      if (!record || ["registered", "unloaded"].includes(record.state)) return;
      if (record.loading) {
        try {
          await record.loading;
        } catch (_) {
          return;
        }
      }
      for (const [dependentName, dependent] of [...this.plugins.entries()].reverse()) {
        if (
          dependent.spec.requires.includes(name)
          && ["loading", "waiting", "active"].includes(dependent.state)
        ) {
          await this.unload(dependentName);
        }
      }
      record.state = "unloading";
      const errors = await this._disposeEffects(record.effects);
      record.state = "unloaded";
      record.error = errors.length ? errors.map(String).join("\n") : null;
    }

    async reload(name) {
      await this.unload(name);
      return this.load(name);
    }

    _missing(record) {
      const missingServices = record.spec.inject.filter((name) => !this.services.has(name));
      const missingPlugins = record.spec.requires
        .filter((name) => this.plugins.get(name)?.state !== "active")
        .map((name) => "plugin:" + name);
      return [...missingServices, ...missingPlugins];
    }

    _resumeWaiting() {
      for (const [name, record] of this.plugins) {
        if (record.state !== "waiting" || this._missing(record).length) continue;
        this._trackPromise(this.load(name), "UI plugin resume failed: " + name);
      }
    }

    _trackPromise(promise, label) {
      this.pending.add(promise);
      promise
        .catch((error) => console.error(label, error))
        .finally(() => this.pending.delete(promise));
    }

    async waitForIdle() {
      while (this.pending.size) await Promise.allSettled([...this.pending]);
    }

    _effect(owner, cleanup) {
      const effect = cleanup instanceof Effect ? cleanup : new Effect(cleanup);
      const record = owner ? this.plugins.get(owner.name) : null;
      if (
        this.closed
        || (owner && (
          !record
          || record.runId !== owner.runId
          || !["loading", "waiting", "active"].includes(record.state)
        ))
      ) {
        const result = effect.dispose();
        if (isPromise(result)) this._trackPromise(result, "stale UI effect cleanup failed");
        return this._disposer(effect);
      }
      this.effects.push(effect);
      if (record) record.effects.push(effect);
      return this._disposer(effect);
    }

    _disposer(effect) {
      const dispose = () => effect.dispose();
      dispose.dispose = dispose;
      return dispose;
    }

    async _disposeEffects(effects) {
      const errors = [];
      for (const effect of [...effects].reverse()) {
        try {
          const result = effect.dispose();
          if (isPromise(result)) await result;
        } catch (error) {
          errors.push(error);
          console.error("UI plugin cleanup failed", error);
        }
      }
      effects.length = 0;
      return errors;
    }

    declare(name, kind = "single", owner = "root") {
      return this._declare(name, kind, owner, null);
    }

    _declare(name, kind, owner, effectOwner) {
      if (!["single", "list", "keyed", "chain"].includes(kind)) {
        throw new Error("unsupported UI slot kind: " + kind);
      }
      const current = this.slots.get(name);
      if (current) {
        if (current.kind !== kind || current.owner !== owner) {
          throw new Error("UI slot declaration conflict: " + name);
        }
        return current;
      }
      const slot = new Slot(name, kind, owner, this);
      this.slots.set(name, slot);
      for (const injection of this.pendingInjections.get(name)?.values() || []) {
        slot.inject(injection);
      }
      this.pendingInjections.delete(name);
      if (effectOwner) this._effect(effectOwner, () => this._undeclare(name, slot));
      return slot;
    }

    _undeclare(name, slot) {
      if (this.slots.get(name) !== slot) return;
      if (slot.entries.size) throw new Error("cannot undeclare occupied UI slot: " + name);
      slot.notifyMissing();
      if (slot.listeners.size) this.pendingInjections.set(name, new Map(slot.listeners));
      this.slots.delete(name);
    }

    register(slotName, plugin, value, options = {}) {
      return this._register(slotName, plugin, value, options, null);
    }

    _register(slotName, plugin, value, options, effectOwner) {
      const slot = this.slots.get(slotName)
        || this._declare(slotName, options.kind || "single", plugin, effectOwner);
      if (options.kind && slot.kind !== options.kind) {
        throw new Error("UI slot kind conflict: " + slotName);
      }
      const effect = slot.register(plugin, value, options);
      return effectOwner ? this._effect(effectOwner, effect) : this._disposer(effect);
    }

    inject(slotName, owner, callback, options = {}) {
      return this._inject(slotName, owner, callback, options, null);
    }

    _inject(slotName, owner, callback, options, effectOwner) {
      const token = owner + ":" + Math.random().toString(36).slice(2);
      const injection = { token, owner: options.owner, key: options.key, callback };
      const slot = this.slots.get(slotName);
      if (slot) slot.inject(injection);
      else {
        if (!this.pendingInjections.has(slotName)) this.pendingInjections.set(slotName, new Map());
        this.pendingInjections.get(slotName).set(token, injection);
      }
      const effect = new Effect(() => {
        this.pendingInjections.get(slotName)?.delete(token);
        this.slots.get(slotName)?.removeInjection(token);
      });
      return effectOwner ? this._effect(effectOwner, effect) : this._disposer(effect);
    }

    provide(name, value, options = {}) {
      return this._provide(name, value, options, null);
    }

    _provide(name, value, options, effectOwner) {
      if (this.services.has(name) && !options.replace) {
        throw new Error("UI service is already provided: " + name);
      }
      const previous = this.services.has(name) ? this.services.get(name) : undefined;
      const hadPrevious = this.services.has(name);
      this.services.set(name, value);
      this._notifyConsumers(name, value);
      this._resumeWaiting();
      const effect = new Effect(() => {
        if (this.services.get(name) !== value) return;
        if (hadPrevious) this.services.set(name, previous);
        else this.services.delete(name);
        this._notifyConsumers(name, hadPrevious ? previous : undefined);
      });
      return effectOwner ? this._effect(effectOwner, effect) : this._disposer(effect);
    }

    get(name, fallback) {
      return this.services.has(name) ? this.services.get(name) : fallback;
    }

    consume(name, callback) {
      return this._consume(name, callback, null);
    }

    _consume(name, callback, effectOwner) {
      const token = Math.random().toString(36).slice(2);
      if (!this.consumers.has(name)) this.consumers.set(name, new Map());
      this.consumers.get(name).set(token, callback);
      this._invokeCallback(callback, this.get(name), "UI service consumer failed");
      const effect = new Effect(() => this.consumers.get(name)?.delete(token));
      return effectOwner ? this._effect(effectOwner, effect) : this._disposer(effect);
    }

    _notifyConsumers(name, value) {
      for (const callback of this.consumers.get(name)?.values() || []) {
        this._invokeCallback(callback, value, "UI service consumer failed");
      }
    }

    on(event, callback) {
      return this._on(event, callback, null);
    }

    _on(event, callback, effectOwner) {
      if (!this.events.has(event)) this.events.set(event, new Map());
      const token = Math.random().toString(36).slice(2);
      this.events.get(event).set(token, callback);
      const effect = new Effect(() => this.events.get(event)?.delete(token));
      return effectOwner ? this._effect(effectOwner, effect) : this._disposer(effect);
    }

    emit(event, payload) {
      const pending = [];
      for (const callback of this.events.get(event)?.values() || []) {
        try {
          const result = callback(payload);
          if (isPromise(result)) pending.push(result);
        } catch (error) {
          console.error("UI event listener failed", error);
        }
      }
      const completion = Promise.allSettled(pending);
      if (pending.length) this._trackPromise(completion, "UI event listener failed");
      return completion;
    }

    _invokeCallback(callback, value, label) {
      try {
        const result = callback(value);
        if (isPromise(result)) this._trackPromise(result, label);
      } catch (error) {
        console.error(label, error);
      }
    }

    resolve(slotName, owner, key) {
      return this.slots.get(slotName)?.resolve(owner, key);
    }

    state(name) {
      return this.plugins.get(name)?.state;
    }

    inspect() {
      return {
        plugins: [...this.plugins.values()].map((record) => ({
          name: record.spec.name,
          version: record.spec.version,
          purpose: record.spec.purpose,
          requires: [...record.spec.requires],
          inject: [...record.spec.inject],
          state: record.state,
          run_id: record.runId,
          error: record.error,
        })),
        slots: [...this.slots.values()].map((slot) => ({
          name: slot.name,
          kind: slot.kind,
          owner: slot.owner,
          occupants: [...slot.entries.values()].map((entry) => entry.plugin),
        })),
      };
    }

    async close() {
      if (this.closed) return;
      for (const name of [...this.plugins.keys()].reverse()) await this.unload(name);
      this.closed = true;
      await this._disposeEffects(this.effects);
      await this.waitForIdle();
    }
  }

  window.MewCodeCordis = new Host();
})();
