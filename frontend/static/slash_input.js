(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.MewSlashInput = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  function bind(input, getContext) {
    const listener = (event) => {
      const context = getContext();
      if (event.isComposing || event.keyCode === 229) return;
      const matches = context.matches || [];
      if (context.menuOpen && matches.length) {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          const direction = event.key === "ArrowDown" ? 1 : -1;
          context.setSelectedIndex(
            (context.selectedIndex + direction + matches.length) % matches.length
          );
          context.render();
          return;
        }
        if (event.key === "Escape") {
          event.preventDefault();
          context.hide();
          return;
        }
        if (event.key === "Tab") {
          event.preventDefault();
          context.select(matches[context.selectedIndex]);
          return;
        }
        if (event.key === "Enter" && !event.shiftKey) {
          const selected = matches[context.selectedIndex];
          const token = String(context.value || "").trim().slice(1).toLowerCase();
          const exact = token === selected.name || (selected.aliases || []).includes(token);
          if (!exact) {
            event.preventDefault();
            context.select(selected);
            return;
          }
          context.hide();
        }
      }
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        context.submit();
      }
    };
    input.addEventListener("keydown", listener);
    return () => input.removeEventListener("keydown", listener);
  }

  return { bind };
});
