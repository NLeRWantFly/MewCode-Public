/* Shared pointer-driven optical highlight for dialogs and the chat composer. */
(() => {
  "use strict";

  const PANEL_SELECTOR = ".settings-dialog, .mew-confirm, .modal";
  const COMPOSER_SELECTOR = ".input-wrap";
  const COMPOSER_CLASS = "has-liquid-composer";
  const COMPOSER_ACTIVE_CLASS = "is-liquid-active";
  const COMPOSER_OPTICS_CLASS = "liquid-composer-optics";
  const COMPOSER_SVG_CLASS = "liquid-composer-svg";
  const FILTER_ID = "mew-liquid-composer-refraction";
  const SVG_NAMESPACE = "http://www.w3.org/2000/svg";
  const POINTER_X = "--liquid-glass-pointer-x";
  const POINTER_Y = "--liquid-glass-pointer-y";
  const COMPOSER_POINTER_X = "--liquid-composer-pointer-x";
  const COMPOSER_POINTER_Y = "--liquid-composer-pointer-y";
  const DEFAULT_COMPOSER_POINTER = Object.freeze({ x: 50, y: 18 });
  /* primitiveUnits uses objectBoundingBox, so scale is a fraction of the
     composer bounds rather than a pixel value. */
  const DEFAULT_DISPLACEMENT_SCALE = 0.006;
  const ACTIVE_DISPLACEMENT_SCALE = 0.011;
  const bindings = new WeakMap();
  const composerBindings = new WeakMap();
  let reducedMotion = false;
  let motionQuery = null;

  const requestFrame = (callback) => {
    if (typeof window.requestAnimationFrame === "function") {
      return window.requestAnimationFrame(callback);
    }
    return window.setTimeout(callback, 16);
  };

  const cancelFrame = (handle) => {
    if (typeof window.cancelAnimationFrame === "function") {
      window.cancelAnimationFrame(handle);
    } else {
      window.clearTimeout(handle);
    }
  };

  const clampPercent = (value) => Math.min(100, Math.max(0, value));

  const prefersReducedMotion = () => {
    try {
      return Boolean(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    } catch (error) {
      return false;
    }
  };

  const clearPointer = (panel, state) => {
    if (state && state.frame) {
      cancelFrame(state.frame);
      state.frame = 0;
    }
    if (state) state.pending = null;
    panel.style.removeProperty(POINTER_X);
    panel.style.removeProperty(POINTER_Y);
  };

  const createSvgNode = (name, attributes = {}) => {
    if (typeof document.createElementNS !== "function") return null;
    const node = document.createElementNS(SVG_NAMESPACE, name);
    Object.entries(attributes).forEach(([key, value]) => {
      node.setAttribute(key, String(value));
    });
    return node;
  };

  const findDirectChild = (element, className) => {
    if (!element || !element.children) return null;
    return Array.from(element.children).find((child) => {
      return child.classList && child.classList.contains(className);
    }) || null;
  };

  const ensureComposerOptics = (composer) => {
    if (!composer || typeof document.createElement !== "function") return null;

    let svg = findDirectChild(composer, COMPOSER_SVG_CLASS);
    if (!svg) {
      svg = createSvgNode("svg");
      if (!svg) return null;
      svg.setAttribute("class", COMPOSER_SVG_CLASS);
      svg.setAttribute("aria-hidden", "true");
      svg.setAttribute("focusable", "false");
      svg.setAttribute("width", "0");
      svg.setAttribute("height", "0");
      composer.insertBefore(svg, composer.firstChild || null);
    }

    let defs = svg.querySelector("defs");
    if (!defs) {
      defs = createSvgNode("defs");
      if (!defs) return null;
      svg.appendChild(defs);
    }

    let filter = defs.querySelector(`[id="${FILTER_ID}"]`);
    if (!filter && typeof document.getElementById === "function") {
      filter = document.getElementById(FILTER_ID);
    }
    if (!filter) {
      filter = createSvgNode("filter", {
        id: FILTER_ID,
        x: "-0.18",
        y: "-0.18",
        width: "1.36",
        height: "1.36",
        filterUnits: "objectBoundingBox",
        primitiveUnits: "objectBoundingBox",
        "color-interpolation-filters": "sRGB",
      });
      if (!filter) return null;

      const turbulence = createSvgNode("feTurbulence", {
        type: "fractalNoise",
        baseFrequency: "0.008 0.018",
        numOctaves: "1",
        seed: "17",
        result: "liquid-noise",
      });
      const blur = createSvgNode("feGaussianBlur", {
        in: "liquid-noise",
        stdDeviation: "0.034",
        result: "liquid-normal-field",
      });
      const displacement = createSvgNode("feDisplacementMap", {
        in: "SourceGraphic",
        in2: "liquid-normal-field",
        scale: DEFAULT_DISPLACEMENT_SCALE,
        xChannelSelector: "R",
        yChannelSelector: "B",
        result: "liquid-displaced",
      });
      const specular = createSvgNode("feSpecularLighting", {
        in: "liquid-normal-field",
        surfaceScale: "0.055",
        specularConstant: "0.045",
        specularExponent: "52",
        lightingColor: "#ffffff",
        result: "liquid-specular",
      });
      const pointLight = createSvgNode("fePointLight", {
        x: DEFAULT_COMPOSER_POINTER.x / 100,
        y: DEFAULT_COMPOSER_POINTER.y / 100,
        z: "0.46",
      });
      const composite = createSvgNode("feComposite", {
        in: "liquid-specular",
        in2: "liquid-displaced",
        operator: "in",
        result: "liquid-specular-mask",
      });
      const blend = createSvgNode("feBlend", {
        in: "liquid-displaced",
        in2: "liquid-specular-mask",
        mode: "screen",
      });

      if (![turbulence, blur, displacement, specular, pointLight, composite, blend].every(Boolean)) {
        return null;
      }
      specular.appendChild(pointLight);
      [turbulence, blur, displacement, specular, composite, blend].forEach((node) => filter.appendChild(node));
      defs.appendChild(filter);
    }

    let displacement = filter.querySelector("feDisplacementMap");
    let pointLight = filter.querySelector("fePointLight");
    if (!displacement || !pointLight) return null;

    let optics = findDirectChild(composer, COMPOSER_OPTICS_CLASS);
    if (!optics) {
      optics = document.createElement("div");
      optics.className = COMPOSER_OPTICS_CLASS;
      composer.insertBefore(optics, composer.firstChild || null);
    }
    optics.setAttribute("aria-hidden", "true");
    optics.setAttribute("role", "presentation");

    return { displacement, optics, pointLight };
  };

  const setComposerPointLight = (state, x, y) => {
    if (!state || !state.pointLight) return;
    state.pointLight.setAttribute("x", (x / 100).toFixed(4));
    state.pointLight.setAttribute("y", (y / 100).toFixed(4));
  };

  const setComposerDisplacementScale = (state, scale) => {
    if (!state || !state.displacement || state.scale === scale) return;
    state.scale = scale;
    state.displacement.setAttribute("scale", String(scale));
  };

  const clearComposerPointer = (composer, state) => {
    if (!state) return;
    if (state.frame) {
      cancelFrame(state.frame);
      state.frame = 0;
    }
    state.pending = null;
    state.lastX = null;
    state.lastY = null;
    composer.style.removeProperty(COMPOSER_POINTER_X);
    composer.style.removeProperty(COMPOSER_POINTER_Y);
    setComposerPointLight(state, DEFAULT_COMPOSER_POINTER.x, DEFAULT_COMPOSER_POINTER.y);
  };

  const bindComposerPointer = (composer, binding) => {
    if (!binding || binding.pointerBound || reducedMotion) return;
    const state = binding.state;
    const flush = () => {
      state.frame = 0;
      if (reducedMotion || !state.pending) return;
      const next = state.pending;
      state.pending = null;
      if (next.x === state.lastX && next.y === state.lastY) return;
      state.lastX = next.x;
      state.lastY = next.y;
      composer.style.setProperty(COMPOSER_POINTER_X, `${next.x}%`);
      composer.style.setProperty(COMPOSER_POINTER_Y, `${next.y}%`);
      setComposerPointLight(state, next.x, next.y);
    };
    const onPointerMove = (event) => {
      if (reducedMotion || !event || !Number.isFinite(event.clientX) || !Number.isFinite(event.clientY)) return;
      const rect = composer.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      const x = clampPercent(((event.clientX - rect.left) / rect.width) * 100);
      const y = clampPercent(((event.clientY - rect.top) / rect.height) * 100);
      if (x.toFixed(2) === state.lastX && y.toFixed(2) === state.lastY) return;
      state.pending = { x: Number(x.toFixed(2)), y: Number(y.toFixed(2)) };
      if (!state.frame) state.frame = requestFrame(flush);
    };
    const onPointerLeave = () => clearComposerPointer(composer, state);

    composer.addEventListener("pointermove", onPointerMove, { passive: true });
    composer.addEventListener("pointerleave", onPointerLeave, { passive: true });
    state.onPointerMove = onPointerMove;
    state.onPointerLeave = onPointerLeave;
    binding.pointerBound = true;
  };

  const unbindComposerPointer = (composer, binding) => {
    if (!binding || !binding.pointerBound) return;
    const state = binding.state;
    composer.removeEventListener("pointermove", state.onPointerMove);
    composer.removeEventListener("pointerleave", state.onPointerLeave);
    state.onPointerMove = null;
    state.onPointerLeave = null;
    binding.pointerBound = false;
    clearComposerPointer(composer, state);
  };

  const findUniqueComposer = () => {
    if (!document || typeof document.querySelectorAll !== "function") return null;
    const composers = document.querySelectorAll(COMPOSER_SELECTOR);
    return composers.length === 1 ? composers[0] : null;
  };

  const bindComposer = (composer) => {
    if (!composer) return;
    let binding = composerBindings.get(composer);
    if (!binding) {
      const optics = ensureComposerOptics(composer);
      if (!optics || !composer.classList) return;
      const state = {
        displacement: optics.displacement,
        optics: optics.optics,
        pointLight: optics.pointLight,
        frame: 0,
        pending: null,
        lastX: null,
        lastY: null,
        scale: null,
        onPointerMove: null,
        onPointerLeave: null,
      };
      const onFocusIn = () => {
        state.active = true;
        composer.classList.add(COMPOSER_ACTIVE_CLASS);
        setComposerDisplacementScale(state, ACTIVE_DISPLACEMENT_SCALE);
      };
      const onFocusOut = (event) => {
        if (event && event.relatedTarget && composer.contains(event.relatedTarget)) return;
        state.active = false;
        composer.classList.remove(COMPOSER_ACTIVE_CLASS);
        setComposerDisplacementScale(state, DEFAULT_DISPLACEMENT_SCALE);
      };
      composer.addEventListener("focusin", onFocusIn);
      composer.addEventListener("focusout", onFocusOut);
      binding = { state, onFocusIn, onFocusOut, pointerBound: false };
      composerBindings.set(composer, binding);
      composer.classList.add(COMPOSER_CLASS);
      state.active = Boolean(document.activeElement && composer.contains(document.activeElement));
      composer.classList.toggle(COMPOSER_ACTIVE_CLASS, state.active);
      setComposerDisplacementScale(
        state,
        state.active ? ACTIVE_DISPLACEMENT_SCALE : DEFAULT_DISPLACEMENT_SCALE,
      );
      setComposerPointLight(state, DEFAULT_COMPOSER_POINTER.x, DEFAULT_COMPOSER_POINTER.y);
    }
    if (reducedMotion) unbindComposerPointer(composer, binding);
    else bindComposerPointer(composer, binding);
  };

  const initializeComposer = () => {
    const composer = findUniqueComposer();
    if (composer) bindComposer(composer);
  };

  const refreshComposerMotionBinding = () => {
    const composer = findUniqueComposer();
    if (!composer) return;
    const binding = composerBindings.get(composer);
    if (!binding) {
      bindComposer(composer);
      return;
    }
    if (reducedMotion) unbindComposerPointer(composer, binding);
    else bindComposerPointer(composer, binding);
  };

  const bindPanel = (panel) => {
    if (!panel || bindings.has(panel) || reducedMotion) return;

    const state = { frame: 0, pending: null };
    const flush = () => {
      state.frame = 0;
      if (reducedMotion || !state.pending) return;
      const next = state.pending;
      state.pending = null;
      panel.style.setProperty(POINTER_X, `${next.x}%`);
      panel.style.setProperty(POINTER_Y, `${next.y}%`);
    };
    const onPointerMove = (event) => {
      if (reducedMotion) return;
      const rect = panel.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      state.pending = {
        x: clampPercent(((event.clientX - rect.left) / rect.width) * 100).toFixed(2),
        y: clampPercent(((event.clientY - rect.top) / rect.height) * 100).toFixed(2),
      };
      if (!state.frame) state.frame = requestFrame(flush);
    };
    const onPointerLeave = () => clearPointer(panel, state);

    panel.addEventListener("pointermove", onPointerMove, { passive: true });
    panel.addEventListener("pointerleave", onPointerLeave, { passive: true });
    bindings.set(panel, { state, onPointerMove, onPointerLeave });
  };

  const unbindPanel = (panel) => {
    const binding = bindings.get(panel);
    if (!binding) return;
    panel.removeEventListener("pointermove", binding.onPointerMove);
    panel.removeEventListener("pointerleave", binding.onPointerLeave);
    clearPointer(panel, binding.state);
    bindings.delete(panel);
  };

  const scan = (root) => {
    if (!root) return;
    if (root.nodeType === 1 && typeof root.matches === "function" && root.matches(PANEL_SELECTOR)) {
      bindPanel(root);
    }
    if (typeof root.querySelectorAll !== "function") return;
    root.querySelectorAll(PANEL_SELECTOR).forEach(bindPanel);
  };

  const refreshMotionBindings = () => {
    const next = prefersReducedMotion();
    if (next === reducedMotion) {
      refreshComposerMotionBinding();
      return;
    }
    reducedMotion = next;
    document.querySelectorAll(PANEL_SELECTOR).forEach((panel) => {
      if (reducedMotion) unbindPanel(panel);
      else bindPanel(panel);
    });
    refreshComposerMotionBinding();
  };

  const initialize = () => {
    if (typeof document === "undefined") return;
    reducedMotion = prefersReducedMotion();
    scan(document);
    initializeComposer();

    if (typeof MutationObserver === "function" && document.body) {
      const observer = new MutationObserver((mutations) => {
        mutations.forEach((mutation) => {
          mutation.addedNodes.forEach((node) => scan(node));
        });
        initializeComposer();
      });
      observer.observe(document.body, { childList: true, subtree: true });
    }

    try {
      motionQuery = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)");
      if (motionQuery) {
        const onMotionChange = () => refreshMotionBindings();
        if (typeof motionQuery.addEventListener === "function") {
          motionQuery.addEventListener("change", onMotionChange);
        } else if (typeof motionQuery.addListener === "function") {
          motionQuery.addListener(onMotionChange);
        }
      }
    } catch (error) {
      motionQuery = null;
    }
  };

  if (typeof document === "undefined") return;
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialize, { once: true });
  } else {
    initialize();
  }
})();
