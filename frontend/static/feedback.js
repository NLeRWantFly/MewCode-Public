/* Shared toast and confirmation feedback for MewCode's native web pages. */
(() => {
  "use strict";

  let toastHost = null;
  let activeConfirm = null;

  function ensureToastHost() {
    if (toastHost && toastHost.isConnected) return toastHost;
    toastHost = document.createElement("div");
    toastHost.id = "mew-feedback-toasts";
    toastHost.setAttribute("aria-live", "polite");
    toastHost.setAttribute("aria-atomic", "false");
    document.body.appendChild(toastHost);
    return toastHost;
  }

  function toast(message, type = "info", options = {}) {
    const host = ensureToastHost();
    const duration = Number(options.duration) || 3400;
    const item = document.createElement("div");
    item.className = `mew-toast ${type}`;
    item.setAttribute("role", type === "error" ? "alert" : "status");

    const icon = document.createElement("span");
    icon.className = "mew-toast-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = type === "success" ? "✓" : type === "error" ? "!" : type === "warning" ? "!" : "i";
    const text = document.createElement("span");
    text.className = "mew-toast-text";
    text.textContent = String(message || "");
    const close = document.createElement("button");
    close.type = "button";
    close.className = "mew-toast-close";
    close.setAttribute("aria-label", "关闭提示");
    close.innerHTML = '<svg class="ui-icon" aria-hidden="true"><use href="/static/icons.svg#close"></use></svg>';
    item.append(icon, text, close);
    host.appendChild(item);

    let timer = null;
    const dismiss = () => {
      if (!item.isConnected) return;
      if (timer) clearTimeout(timer);
      item.classList.remove("show");
      window.setTimeout(() => item.remove(), 150);
    };
    close.addEventListener("click", dismiss);
    requestAnimationFrame(() => item.classList.add("show"));
    timer = window.setTimeout(dismiss, duration);
    return { dismiss };
  }

  function confirm(options) {
    const config = typeof options === "string" ? { message: options } : (options || {});
    if (activeConfirm) activeConfirm(false);
    return new Promise((resolve) => {
      const opener = document.activeElement;
      const mask = document.createElement("div");
      mask.className = "mew-confirm-mask";
      const dialog = document.createElement("section");
      dialog.className = "mew-confirm" + (config.tone === "danger" ? " danger" : "");
      dialog.setAttribute("role", "dialog");
      dialog.setAttribute("aria-modal", "true");
      dialog.setAttribute("aria-labelledby", "mew-confirm-title");

      const title = document.createElement("h2");
      title.id = "mew-confirm-title";
      title.textContent = config.title || "请确认操作";
      const message = document.createElement("p");
      message.textContent = config.message || "";
      const actions = document.createElement("div");
      actions.className = "mew-confirm-actions";
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "mew-confirm-cancel";
      cancel.textContent = config.cancelLabel || "取消";
      const proceed = document.createElement("button");
      proceed.type = "button";
      proceed.className = "mew-confirm-accept";
      proceed.textContent = config.confirmLabel || "确认";
      actions.append(cancel, proceed);
      dialog.append(title, message, actions);
      mask.appendChild(dialog);
      document.body.appendChild(mask);

      const finish = (accepted) => {
        if (!mask.isConnected) return;
        document.removeEventListener("keydown", onKeydown, true);
        mask.remove();
        activeConfirm = null;
        if (opener && typeof opener.focus === "function") opener.focus();
        resolve(accepted);
      };
      const onKeydown = (event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          finish(false);
        }
      };
      activeConfirm = finish;
      cancel.addEventListener("click", () => finish(false));
      proceed.addEventListener("click", () => finish(true));
      mask.addEventListener("click", (event) => {
        if (event.target === mask) finish(false);
      });
      document.addEventListener("keydown", onKeydown, true);
      requestAnimationFrame(() => {
        mask.classList.add("show");
        proceed.focus();
      });
    });
  }

  window.MewFeedback = { toast, confirm };
})();
