"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("mewcodeDesktop", Object.freeze({
  isDesktop: true,
  selectDirectory: () => ipcRenderer.invoke("dialog:select-directory"),
  openDirectory: (directoryPath) => ipcRenderer.invoke("shell:open-directory", directoryPath),
  openExternal: (url) => ipcRenderer.invoke("shell:open-external", url),
  openPipeline: (options = {}) => ipcRenderer.invoke("window:open-pipeline", options),
  closePipeline: () => ipcRenderer.invoke("window:close-pipeline"),
  setWindowTheme: (theme) => ipcRenderer.invoke("window:set-theme", theme),
  getProviderPresets: () => ipcRenderer.invoke("provider:presets"),
  listProviders: () => ipcRenderer.invoke("provider:list"),
  saveProvider: (profile) => ipcRenderer.invoke("provider:save", profile),
  removeProvider: (providerKey) => ipcRenderer.invoke("provider:remove", providerKey),
  testProvider: (profile) => ipcRenderer.invoke("provider:test", profile),
  listUsageAccounts: () => ipcRenderer.invoke("usage:list-accounts"),
  saveUsageAccount: (account) => ipcRenderer.invoke("usage:save-account", account),
  removeUsageAccount: (accountId) => ipcRenderer.invoke("usage:remove-account", accountId),
  runUsageBrowserCommand: (command) => ipcRenderer.invoke("usage:browser-command", command),
  diagnostics: () => ipcRenderer.invoke("runtime:diagnostics"),
  restartBackend: () => ipcRenderer.invoke("runtime:restart"),
}));
