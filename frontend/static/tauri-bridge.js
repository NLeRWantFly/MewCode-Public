(() => {
  "use strict";

  function resolveInvoke() {
    const internals = window.__TAURI_INTERNALS__;
    if (internals && typeof internals.invoke === "function") {
      return (command, args) => internals.invoke(command, args || {});
    }
    const core = window.__TAURI__ && window.__TAURI__.core;
    if (core && typeof core.invoke === "function") {
      return (command, args) => core.invoke(command, args);
    }
    return null;
  }

  function install(invoke) {
    if (window.mewcodeDesktop) return;
    const unsupportedUsageBrowser = () => Promise.reject(new Error("the Tauri shell does not provide an isolated Usage browser window yet"));

    window.mewcodeDesktop = Object.freeze({
      runtime: /Android/i.test(navigator.userAgent) ? "tauri-mobile" : "tauri",
      mobileBackendUrl: () => invoke("mobile_backend_url"),
      mobilePairingState: () => invoke("mobile_pairing_state"),
      mobileDiscoverNodes: () => invoke("mobile_discover_nodes"),
      mobileProbeHost: (host) => invoke("mobile_probe_host", { host }),
      mobileRequestPairing: (endpoint, nodeId, label) => invoke("mobile_request_pairing", { endpoint, nodeId, label }),
      mobilePollPairing: (endpoint, requestId) => invoke("mobile_poll_pairing", { endpoint, requestId }),
      mobileSetActivePeer: (endpoint) => invoke("mobile_set_active_peer", { endpoint }),
      mobileClearPairing: (endpoint) => invoke("mobile_clear_pairing", { endpoint }),
      mobileRemoteRequest: (path, method = "GET", payload) => invoke("mobile_remote_request", { path, method, payload }),
      selectDirectory: () => invoke("select_directory"),
      openDirectory: (directoryPath) => invoke("open_directory", { directoryPath }),
      openExternal: (url) => invoke("open_external", { url }),
      openPipeline: (options = {}) => invoke("open_pipeline", { options }),
      closePipeline: () => invoke("close_pipeline"),
      setWindowTheme: (theme) => invoke("set_theme", { theme }),
      getProviderPresets: () => invoke("provider_presets"),
      listProviders: () => invoke("provider_list"),
      saveProvider: (profile) => invoke("provider_save", { profile }),
      removeProvider: (providerKey) => invoke("provider_remove", { providerKey }),
      testProvider: (profile) => invoke("provider_test", { profile }),
      listUsageAccounts: () => invoke("usage_list_accounts"),
      saveUsageAccount: (account) => invoke("usage_save_account", { account }),
      removeUsageAccount: (accountId) => invoke("usage_remove_account", { accountId }),
      runUsageBrowserCommand: unsupportedUsageBrowser,
      diagnostics: () => invoke("diagnostics"),
      restartBackend: () => invoke("restart_backend"),
    });
  }

  const immediate = resolveInvoke();
  if (immediate) {
    install(immediate);
    return;
  }

  let tries = 0;
  const timer = setInterval(() => {
    tries += 1;
    const invoke = resolveInvoke();
    if (invoke) {
      install(invoke);
      clearInterval(timer);
      return;
    }
    if (tries > 80) clearInterval(timer);
  }, 50);
})();
