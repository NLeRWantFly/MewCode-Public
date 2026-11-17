"use strict";

const loadingTheme = new URLSearchParams(location.search).get("theme");
if (loadingTheme === "light" || loadingTheme === "dark") {
  document.documentElement.dataset.theme = loadingTheme;
  window.mewcodeDesktop.setWindowTheme(loadingTheme).catch(() => {});
}

window.showStartupError = (message, diagnostics) => {
  document.getElementById("status").textContent = "启动失败：" + message;
  document.getElementById("progress").hidden = true;
  const panel = document.getElementById("error-panel");
  panel.hidden = false;
  document.getElementById("diagnostics").textContent = (diagnostics || []).join("\n") || "没有更多诊断信息。";
};

document.getElementById("retry").addEventListener("click", async () => {
  const button = document.getElementById("retry");
  const panel = document.getElementById("error-panel");
  button.disabled = true;
  panel.hidden = true;
  document.getElementById("progress").hidden = false;
  document.getElementById("status").textContent = "正在重新启动本地后端…";
  try {
    await window.mewcodeDesktop.restartBackend();
  } catch (error) {
    window.showStartupError(error.message || String(error), []);
    button.disabled = false;
  }
});
