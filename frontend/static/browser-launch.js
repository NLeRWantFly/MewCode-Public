(() => {
  "use strict";
  const button = document.getElementById("open-web-client");
  if (!button) return;
  button.addEventListener("click", async () => {
    button.disabled = true;
    const desktop = window.mewcodeDesktop;
    // Reserve the tab in the user gesture so browsers do not block the handoff.
    const tab = desktop?.openExternal ? null : window.open("about:blank", "_blank");
    try {
      const response = await fetch("/api/browser-launch", { method: "POST" });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || "无法打开浏览器");
      const url = new URL(body.path, location.origin).href;
      if (desktop?.openExternal) await desktop.openExternal(url);
      else if (tab) { tab.opener = null; tab.location.replace(url); }
      else throw new Error("请允许此页面打开新标签页后重试。");
    } catch (error) {
      tab?.close();
      alert(error.message || "无法打开浏览器");
    } finally {
      button.disabled = false;
    }
  });
})();
