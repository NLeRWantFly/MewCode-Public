"use strict";

const { app, BrowserWindow, dialog, ipcMain, nativeTheme, safeStorage, session, shell } = require("electron");
const { createHash, randomBytes, randomUUID } = require("node:crypto");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const fsp = require("node:fs/promises");
const http = require("node:http");
const net = require("node:net");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { isTrustedLoadingUrl } = require("./loading-sender.cjs");

const rootDir = path.resolve(__dirname, "..");
const providerPresets = {
  typesafe: { label: "desktop:example_provider_7（评审）", protocol: "typesafe", base_url: "https://api.typesafe.ai/v1", default_model: "jev-latest" },
  openai: { label: "OpenAI", protocol: "openai", base_url: "https://api.openai.com/v1" },
  "opencode-go": {
    label: "OpenCode Go",
    provider_type: "opencode-go",
    protocol: "openai-compat",
    base_url: "https://opencode.ai/zen/go",
    default_model: "deepseek-v4-flash",
  },
  anthropic: { label: "Anthropic", protocol: "anthropic", base_url: "https://api.anthropic.com" },
  kimi: { label: "Moonshot / Kimi", protocol: "openai-compat", base_url: "https://api.moonshot.cn/v1" },
  deepseek: { label: "DeepSeek", protocol: "openai-compat", base_url: "https://api.deepseek.com" },
  openrouter: { label: "OpenRouter", protocol: "openai-compat", base_url: "https://openrouter.ai/api/v1" },
  custom: { label: "OpenAI Compatible", protocol: "openai-compat", base_url: "" },
};

let mainWindow = null;
let backend = null;
let backendUrl = "";
let desktopToken = "";
let diagnostics = [];
let restartPromise = null;
let bootedOnce = false;
let recovering = false;
let quitting = false;
const recoveryAttempts = [];
let currentWindowTheme = "dark";
const sessionProfiles = new Map();
const sessionUsageAccounts = new Map();
const usageWindows = new Map();
const pipelineWindows = new Map();

const windowThemeColors = {
  dark: { background: "#0d0d0d", symbol: "#f2f2ee" },
  light: { background: "#f2f0eb", symbol: "#111111" },
};

function uiStatePath() {
  return path.join(app.getPath("userData"), "ui-state.json");
}

function readWindowTheme() {
  try {
    const state = JSON.parse(fs.readFileSync(uiStatePath(), "utf8"));
    if (state && ["light", "dark"].includes(state.theme)) return state.theme;
  } catch (_) {
    // First launch or an invalid snapshot: follow the OS theme.
  }
  return nativeTheme.shouldUseDarkColors ? "dark" : "light";
}

async function writeWindowTheme(theme) {
  const target = uiStatePath();
  const temporary = `${target}.${process.pid}.${randomUUID()}.tmp`;
  await fsp.mkdir(path.dirname(target), { recursive: true });
  await fsp.writeFile(temporary, JSON.stringify({ theme }), "utf8");
  await fsp.rename(temporary, target);
}

function rememberDiagnostic(line) {
  const safe = String(line || "").replace(/(sk-|api[_-]?key[=: ]+)\S+/gi, "$1[REDACTED]");
  diagnostics.push(safe.slice(0, 1000));
  diagnostics = diagnostics.slice(-80);
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      server.close(() => resolve(address.port));
    });
  });
}

function backendCommand(port) {
  if (app.isPackaged) {
    const executable = process.platform === "win32" ? "mewcode-backend.exe" : "mewcode-backend";
    return {
      command: path.join(process.resourcesPath, "backend", executable),
      args: ["--host", "127.0.0.1", "--port", String(port)],
      cwd: path.join(process.resourcesPath, "backend"),
    };
  }
  const candidates = process.platform === "win32"
    ? [path.join(rootDir, ".venv", "Scripts", "python.exe"), "python"]
    : [path.join(rootDir, ".venv", "bin", "python"), "python3"];
  const command = candidates.find((candidate) => candidate === "python" || candidate === "python3" || fs.existsSync(candidate));
  return {
    command,
    args: [path.join(rootDir, "frontend", "server.py"), "--host", "127.0.0.1", "--port", String(port)],
    cwd: rootDir,
  };
}

function probeBackend(url, timeoutMs = 2500) {
  return new Promise((resolve, reject) => {
    const request = http.get(url + "/api/health", {
      agent: false,
      timeout: timeoutMs,
      headers: { Accept: "application/json" },
    }, (response) => {
      response.resume();
      resolve(response.statusCode >= 200 && response.statusCode < 300);
    });
    request.once("timeout", () => request.destroy(new Error("health probe timed out")));
    request.once("error", reject);
  });
}

async function waitForBackend(url, processRef, timeoutMs = 120000) {
  const deadline = Date.now() + timeoutMs;
  const startedAt = Date.now();
  let attempts = 0;
  let lastError = "backend has not accepted a connection yet";
  while (Date.now() < deadline) {
    if (processRef && processRef.exitCode !== null) {
      throw new Error(`Python backend exited early (code ${processRef.exitCode})`);
    }
    try {
      if (await probeBackend(url)) return;
      lastError = "health endpoint returned a non-success status";
    } catch (error) {
      lastError = error && error.message ? error.message : String(error);
      // Sidecar is still starting.
    }
    attempts += 1;
    if (attempts % 20 === 0) {
      rememberDiagnostic(`waiting for backend health (${Math.round((Date.now() - startedAt) / 1000)}s): ${lastError}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`Timed out waiting for the Python backend after ${Math.round(timeoutMs / 1000)}s (${lastError})`);
}

async function stopBackend() {
  const processRef = backend;
  backend = null;
  if (!processRef || processRef.exitCode !== null) return;
  processRef.kill();
  await new Promise((resolve) => {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve();
    };
    const timer = setTimeout(finish, 5000);
    processRef.once("exit", finish);
  });
  if (processRef.exitCode === null) processRef.kill("SIGKILL");
}

async function startBackend() {
  const port = await freePort();
  desktopToken = randomBytes(32).toString("base64url");
  backendUrl = `http://127.0.0.1:${port}`;
  const spec = backendCommand(port);
  const userDataDir = app.getPath("userData");
  const runtimeDir = path.join(userDataDir, "runtime");
  await fsp.mkdir(runtimeDir, { recursive: true });
  if (app.isPackaged) spec.cwd = runtimeDir;
  backend = spawn(spec.command, spec.args, {
    cwd: spec.cwd,
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
    env: {
      ...process.env,
      MEWCODE_DESKTOP_TOKEN: desktopToken,
      MEWCODE_USER_DATA_DIR: userDataDir,
      PYTHONUNBUFFERED: "1",
    },
  });
  backend.stdout.on("data", (chunk) => rememberDiagnostic(chunk));
  backend.stderr.on("data", (chunk) => rememberDiagnostic(chunk));
  backend.on("exit", (code) => {
    rememberDiagnostic(`backend exited: ${code}`);
    scheduleBackendRecovery(code);
  });
  await waitForBackend(backendUrl, backend);
  await session.defaultSession.cookies.set({
    url: backendUrl,
    name: "mewcode_desktop_session",
    value: desktopToken,
    httpOnly: true,
    sameSite: "strict",
    secure: false,
    expirationDate: Math.floor(Date.now() / 1000) + 12 * 60 * 60,
  });
}

function createWindow() {
  currentWindowTheme = readWindowTheme();
  const initialColors = windowThemeColors[currentWindowTheme];
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 940,
    minWidth: 900,
    minHeight: 640,
    show: false,
    backgroundColor: initialColors.background,
    titleBarStyle: process.platform === "darwin" ? "hiddenInset" : "hidden",
    ...(process.platform === "darwin" ? {} : {
      titleBarOverlay: {
        color: initialColors.background,
        symbolColor: initialColors.symbol,
        height: 52,
      },
    }),
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      webSecurity: true,
    },
  });
  mainWindow.removeMenu();
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//i.test(url)) shell.openExternal(url);
    return { action: "deny" };
  });
  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (!url.startsWith(backendUrl)) event.preventDefault();
  });
  mainWindow.once("ready-to-show", () => mainWindow.show());
}

async function openPipelineWindow(rawOptions = {}) {
  const options = rawOptions && typeof rawOptions === "object"
    ? rawOptions : { graphId: rawOptions };
  const graphId = String(options.graphId || "").trim();
  const mode = String(options.mode || "pipeline");
  if (!new Set(["pipeline", "expert-flow", "orch-canvas", "orchestration"]).has(mode)) {
    throw new Error("Invalid Pipeline window mode");
  }
  if (
    graphId.length > 512 ||
    graphId === "." ||
    graphId === ".." ||
    /[\\/\0?#]/.test(graphId)
  ) {
    throw new Error("Invalid Pipeline graph id");
  }
  const key = `${mode}:${graphId || "__workspace__"}`;
  const existing = pipelineWindows.get(key);
  if (existing && !existing.isDestroyed()) {
    if (existing.isMinimized()) existing.restore();
    existing.show();
    existing.focus();
    return true;
  }
  if (!backendUrl) throw new Error("Pipeline workspace is unavailable");
  const colors = windowThemeColors[currentWindowTheme];
  const window = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 900,
    minHeight: 640,
    show: false,
    backgroundColor: colors.background,
    titleBarStyle: process.platform === "darwin" ? "hiddenInset" : "hidden",
    ...(process.platform === "darwin" ? {} : {
      titleBarOverlay: {
        color: colors.background,
        symbolColor: colors.symbol,
        height: 52,
      },
    }),
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      webSecurity: true,
    },
  });
  pipelineWindows.set(key, window);
  window.removeMenu();
  window.webContents.setWindowOpenHandler(({ url }) => {
    let internal = false;
    try {
      internal = new URL(url).origin === backendUrl;
    } catch (_) {}
    if (/^https?:\/\//i.test(url) && !internal) shell.openExternal(url);
    return { action: "deny" };
  });
  window.webContents.on("will-navigate", (event, url) => {
    try {
      if (new URL(url).origin !== backendUrl) event.preventDefault();
    } catch (_) {
      event.preventDefault();
    }
  });
  window.on("closed", () => {
    if (pipelineWindows.get(key) === window) pipelineWindows.delete(key);
  });
  const pagePath = mode === "expert-flow"
    ? "/expert-flow"
    : ((mode === "orch-canvas" || mode === "orchestration") ? "/orch-canvas" : "/pipeline");
  const target = new URL(pagePath, backendUrl);
  target.searchParams.set("desktop_theme", currentWindowTheme);
  if (graphId && pagePath !== "/expert-flow") target.searchParams.set("id", graphId);
  const boot = new URL("/desktop-boot", backendUrl);
  boot.searchParams.set("session", desktopToken || "");
  boot.searchParams.set("next", `${target.pathname}${target.search}`);
  try {
    await window.loadURL(boot.href);
    window.show();
    window.focus();
    return true;
  } catch (error) {
    if (!window.isDestroyed()) window.destroy();
    throw error;
  }
}

function applyWindowTheme(theme) {
  currentWindowTheme = theme === "light" ? "light" : "dark";
  if (!mainWindow || mainWindow.isDestroyed()) return;
  const colors = windowThemeColors[currentWindowTheme];
  mainWindow.setBackgroundColor(colors.background);
  if (process.platform !== "darwin" && typeof mainWindow.setTitleBarOverlay === "function") {
    mainWindow.setTitleBarOverlay({
      color: colors.background,
      symbolColor: colors.symbol,
      height: 52,
    });
  }
  for (const window of pipelineWindows.values()) {
    if (window.isDestroyed()) continue;
    window.setBackgroundColor(colors.background);
    if (process.platform !== "darwin" && typeof window.setTitleBarOverlay === "function") {
      window.setTitleBarOverlay({
        color: colors.background,
        symbolColor: colors.symbol,
        height: 52,
      });
    }
  }
}

function providerStorePath() {
  return path.join(app.getPath("userData"), "providers.json");
}

async function readStoredProfiles() {
  try {
    const value = JSON.parse(await fsp.readFile(providerStorePath(), "utf8"));
    return Array.isArray(value)
      ? value
      : (value && typeof value === "object" ? [value] : []);
  } catch (_) {
    return [];
  }
}

async function writeStoredProfiles(profiles) {
  const target = providerStorePath();
  await fsp.mkdir(path.dirname(target), { recursive: true });
  const temporary = target + ".tmp";
  await fsp.writeFile(temporary, JSON.stringify(profiles, null, 2), { mode: 0o600 });
  await fsp.rename(temporary, target);
}

function encryptionSafety() {
  const available = safeStorage.isEncryptionAvailable();
  const backendName = process.platform === "linux" && typeof safeStorage.getSelectedStorageBackend === "function"
    ? safeStorage.getSelectedStorageBackend()
    : "platform";
  return { available, backendName, safe: available && backendName !== "basic_text" };
}

function publicProfile(record) {
  const { encrypted_api_key, api_key, ...metadata } = record;
  return { ...metadata, has_secret: Boolean(encrypted_api_key || api_key) };
}

async function materializedProfiles() {
  const stored = await readStoredProfiles();
  const result = [];
  for (const record of stored) {
    try {
      const api_key = safeStorage.decryptString(Buffer.from(record.encrypted_api_key, "base64"));
      result.push({ ...publicProfile(record), api_key });
    } catch (_) {
      rememberDiagnostic(`Unable to decrypt provider ${record.provider_key}`);
    }
  }
  for (const record of sessionProfiles.values()) result.push({ ...record });
  return result;
}

async function backendRequest(route, body, timeoutMs = 30000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(backendUrl + route, {
      method: "POST",
      headers: {
        "Authorization": "Bearer " + desktopToken,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    return payload;
  } catch (error) {
    if (error && error.name === "AbortError") {
      throw new Error(`后端请求超时（${route}，${Math.round(timeoutMs / 1000)} 秒无响应）`);
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

async function syncProviders() {
  if (!backendUrl) return;
  await backendRequest("/api/client/providers/sync", { profiles: await materializedProfiles() });
}

function usageAccountStorePath() {
  return path.join(app.getPath("userData"), "usage-accounts.json");
}

async function readStoredUsageAccounts() {
  try {
    const value = JSON.parse(await fsp.readFile(usageAccountStorePath(), "utf8"));
    return Array.isArray(value) ? value : [];
  } catch (_) {
    return [];
  }
}

async function writeStoredUsageAccounts(accounts) {
  const target = usageAccountStorePath();
  const temporary = `${target}.${process.pid}.tmp`;
  await fsp.mkdir(path.dirname(target), { recursive: true });
  await fsp.writeFile(temporary, JSON.stringify(accounts, null, 2), { mode: 0o600 });
  await fsp.rename(temporary, target);
}

function publicUsageAccount(record) {
  const { encrypted_secrets, secret_values, ...metadata } = record;
  return {
    ...metadata,
    has_secret: Boolean(
      metadata.has_secret || secret_values && Object.keys(secret_values).length
    ),
  };
}

function usageConfigContainsSecret(value) {
  if (Array.isArray(value)) return value.some(usageConfigContainsSecret);
  if (!value || typeof value !== "object") return false;
  return Object.entries(value).some(([key, item]) => {
    const normalized = String(key).trim().toLowerCase().replaceAll("-", "_");
    return new Set([
      "api_key", "admin_api_key", "password", "secret", "token", "cookie",
      "authorization", "credential", "credentials",
    ]).has(normalized) || /_(?:password|secret|token|cookie)$/.test(normalized) || usageConfigContainsSecret(item);
  });
}

async function materializedUsageAccounts() {
  const result = [];
  for (const record of await readStoredUsageAccounts()) {
    try {
      const decoded = safeStorage.decryptString(Buffer.from(record.encrypted_secrets, "base64"));
      const secret_values = JSON.parse(decoded);
      result.push({ ...publicUsageAccount(record), secret_values });
    } catch (_) {
      rememberDiagnostic(`Unable to decrypt usage account ${record.id}`);
    }
  }
  for (const record of sessionUsageAccounts.values()) result.push({ ...record });
  return result;
}

function validateUsageAccountInput(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new Error("Invalid usage account");
  }
  const adapter_id = String(input.adapter_id || "").trim().toLowerCase();
  if (!new Set(["openai", "anthropic", "deepseek", "plugin"]).has(adapter_id)) {
    throw new Error("Unsupported usage adapter");
  }
  const id = String(input.id || `usage:${randomUUID()}`).trim();
  if (!/^[A-Za-z0-9._:-]{3,180}$/.test(id)) throw new Error("Invalid usage account id");
  const name = String(input.name || "").trim();
  if (!name || name.length > 120) throw new Error("A valid usage account name is required");
  const linked_provider_keys = Array.from(new Set(
    (Array.isArray(input.linked_provider_keys) ? input.linked_provider_keys : [])
      .map((item) => String(item || "").trim()).filter(Boolean)
  )).slice(0, 100);
  const budget_override = {};
  for (const [currency, raw] of Object.entries(input.budget_override || {})) {
    const code = String(currency).trim().toUpperCase();
    const value = String(raw).trim();
    if (!/^[A-Z0-9]{1,12}$/.test(code) || !/^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(value)) {
      throw new Error("Budget values must be non-negative decimal strings by currency");
    }
    budget_override[code] = value;
  }
  const plugin_id = String(input.plugin_id || "").trim().toLowerCase();
  if (adapter_id === "plugin" && !/^[a-z0-9][a-z0-9._-]{2,127}$/.test(plugin_id)) {
    throw new Error("A valid installed plugin id is required");
  }
  const config = input.config && typeof input.config === "object" && !Array.isArray(input.config)
    ? JSON.parse(JSON.stringify(input.config)) : {};
  if (JSON.stringify(config).length > 100000) throw new Error("Usage account config is too large");
  if (usageConfigContainsSecret(config)) {
    throw new Error("Usage account config cannot contain credentials; use the secure secret fields");
  }
  const secret_values = {};
  if (input.secret_values !== undefined && (
    !input.secret_values || typeof input.secret_values !== "object" || Array.isArray(input.secret_values)
  )) throw new Error("Usage secrets must be an object");
  for (const [key, raw] of Object.entries(input.secret_values || {})) {
    const field = String(key).trim();
    const value = String(raw || "");
    if (!/^[A-Za-z0-9._-]{1,100}$/.test(field) || value.length > 10000) {
      throw new Error("Invalid usage secret field");
    }
    if (value) secret_values[field] = value;
  }
  return {
    id, name, adapter_id, linked_provider_keys,
    period_policy: "current_month", budget_override, plugin_id, config,
    enabled: input.enabled !== false, secret_values,
  };
}

async function syncUsageAccounts() {
  if (!backendUrl) return;
  await backendRequest("/api/client/usage/accounts/sync", {
    accounts: await materializedUsageAccounts(),
  });
}

async function saveUsageAccount(input) {
  const validated = validateUsageAccountInput(input);
  const existing = (await materializedUsageAccounts()).find((item) => item.id === validated.id);
  const secret_values = { ...(existing && existing.secret_values || {}), ...validated.secret_values };
  const metadata = { ...validated, has_secret: Object.keys(secret_values).length > 0 };
  delete metadata.secret_values;
  const safety = encryptionSafety();
  if (!safety.safe || input.persist === false) {
    const record = { ...metadata, secret_values, storage: "session" };
    sessionUsageAccounts.set(validated.id, record);
    await syncUsageAccounts();
    return { ...publicUsageAccount(record), persisted: false, reason: safety.safe ? "session requested" : `unsafe storage backend: ${safety.backendName}` };
  }
  const record = {
    ...metadata,
    encrypted_secrets: safeStorage.encryptString(JSON.stringify(secret_values)).toString("base64"),
    storage: "safeStorage",
  };
  const next = (await readStoredUsageAccounts()).filter((item) => item.id !== validated.id);
  next.push(record);
  await writeStoredUsageAccounts(next);
  sessionUsageAccounts.delete(validated.id);
  await syncUsageAccounts();
  return { ...publicUsageAccount(record), persisted: true };
}

async function removeUsageAccount(accountId) {
  const id = String(accountId || "");
  if (!/^[A-Za-z0-9._:-]{3,180}$/.test(id)) throw new Error("Invalid usage account id");
  await writeStoredUsageAccounts(
    (await readStoredUsageAccounts()).filter((item) => item.id !== id)
  );
  sessionUsageAccounts.delete(id);
  await syncUsageAccounts();
  // Clearing the built-in DeepSeek portal account means clearing only its
  // isolated persistent browser partition. Keep the managed source available
  // so the user can immediately log in again.
  if (backendUrl && id !== "managed:deepseek-portal") {
    await fetch(`${backendUrl}/api/usage/accounts/${encodeURIComponent(id)}`, {
      method: "DELETE",
      headers: { Authorization: `Bearer ${desktopToken}` },
    });
  }
  const record = usageWindows.get(id);
  if (record && !record.window.isDestroyed()) record.window.destroy();
  usageWindows.delete(id);
  const isolatedSession = session.fromPartition(usagePartition(id));
  await isolatedSession.clearStorageData();
  await isolatedSession.clearCache();
  return true;
}

function normalizeUsageOrigin(rawUrl) {
  let parsed;
  try {
    parsed = new URL(String(rawUrl || ""));
  } catch (_) {
    throw new Error("Usage browser URL must be valid HTTP(S)");
  }
  if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) {
    throw new Error("Usage browser URL must be valid HTTP(S)");
  }
  return parsed.origin.toLowerCase();
}

function allowedUsageUrl(rawUrl, allowedOrigins) {
  try {
    const parsed = new URL(rawUrl);
    if (["about:", "data:", "blob:"].includes(parsed.protocol)) return true;
    return allowedOrigins.has(parsed.origin.toLowerCase());
  } catch (_) {
    return false;
  }
}

function usagePartition(accountId) {
  const digest = createHash("sha256").update(accountId).digest("hex").slice(0, 24);
  return `persist:mewcode-usage-${digest}`;
}

function ensureUsageWindow(accountId, rawOrigins) {
  const origins = new Set((rawOrigins || []).map(normalizeUsageOrigin));
  let record = usageWindows.get(accountId);
  if (record && !record.window.isDestroyed()) {
    // Permissions belong to the currently selected plugin manifest.  Reusing
    // an account partition must not retain origins granted to an older plugin.
    record.allowedOrigins = origins;
    return record;
  }
  const isolatedSession = session.fromPartition(usagePartition(accountId), { cache: true });
  record = { allowedOrigins: origins, window: null };
  isolatedSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  isolatedSession.webRequest.onBeforeRequest(
    { urls: ["http://*/*", "https://*/*"] },
    (details, callback) => callback({ cancel: !allowedUsageUrl(details.url, record.allowedOrigins) })
  );
  const window = new BrowserWindow({
    width: 1100,
    height: 800,
    minWidth: 720,
    minHeight: 520,
    show: false,
    title: "MewCode 用量账户登录",
    autoHideMenuBar: true,
    webPreferences: {
      session: isolatedSession,
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      webSecurity: true,
    },
  });
  record.window = window;
  usageWindows.set(accountId, record);
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  window.webContents.on("will-navigate", (event, url) => {
    if (!allowedUsageUrl(url, record.allowedOrigins)) event.preventDefault();
  });
  window.on("closed", () => {
    if (usageWindows.get(accountId) === record) usageWindows.delete(accountId);
  });
  return record;
}

async function usageAccountSecrets(accountId) {
  const account = (await materializedUsageAccounts()).find((item) => item.id === accountId);
  return account && account.secret_values || {};
}

async function providerApiKey(providerKey) {
  const profile = (await materializedProfiles()).find(
    (item) => String(item.provider_key || "") === String(providerKey || "")
  );
  const localValue = String(profile && profile.api_key || "");
  if (localValue) return localValue;
  const resolved = await backendRequest("/api/client/providers/secret", {
    provider_key: String(providerKey || ""),
  });
  return String(resolved && resolved.value || "");
}

async function runUsageBrowserCommand(command) {
  if (!command || typeof command !== "object" || Array.isArray(command)) {
    throw new Error("Invalid usage browser command");
  }
  const accountId = String(command.account_id || "");
  if (!/^[A-Za-z0-9._:-]{3,180}$/.test(accountId)) throw new Error("Invalid usage account id");
  const method = String(command.method || "");
  const allowedMethods = new Set([
    "browser.open", "browser.goto", "browser.wait_for", "browser.click",
    "browser.fill", "browser.text", "browser.evaluate", "browser.fetch",
    "browser.pause_for_user", "browser.close",
  ]);
  if (!allowedMethods.has(method)) throw new Error("Unsupported usage browser command");
  const params = command.params && typeof command.params === "object" ? command.params : {};
  if (method === "browser.close") {
    const existing = usageWindows.get(accountId);
    if (existing && !existing.window.isDestroyed()) existing.window.destroy();
    usageWindows.delete(accountId);
    return { closed: true };
  }
  const record = ensureUsageWindow(accountId, command.allowed_origins || []);
  const window = record.window;
  const execute = (script) => window.webContents.executeJavaScript(script, true);
  const assertUrl = (rawUrl) => {
    const url = String(rawUrl || "");
    if (!allowedUsageUrl(url, record.allowedOrigins)) throw new Error("Usage browser origin is not allowed");
    return url;
  };
  const assertCurrentPage = () => {
    const current = window.webContents.getURL();
    if (!allowedUsageUrl(current, record.allowedOrigins) || !/^https?:/i.test(current)) {
      throw new Error("Usage browser is not on a declared origin");
    }
  };
  if (method === "browser.open" || method === "browser.goto") {
    const url = assertUrl(params.url);
    await window.loadURL(url);
    if (params.visible === true) window.show();
    return { url: window.webContents.getURL() };
  }
  if (method === "browser.pause_for_user") {
    window.show();
    const prompt = String(params.prompt || "请完成登录、验证码或二次验证。完成后继续。").slice(0, 1000);
    const choice = await dialog.showMessageBox(mainWindow, {
      type: "info",
      title: "用量账户需要操作",
      message: prompt,
      detail: "登录窗口使用该账户独立的持久化会话；Cookie 不会发送给 Python 插件。",
      buttons: ["已完成，继续", "取消采集"],
      defaultId: 0,
      cancelId: 1,
      noLink: true,
    });
    if (choice.response !== 0) throw new Error("用户取消了登录操作");
    return { resumed: true, url: window.webContents.getURL() };
  }
  if (method === "browser.wait_for") {
    assertCurrentPage();
    const selector = String(params.selector || "");
    if (!selector || selector.length > 2000) throw new Error("Invalid selector");
    const timeoutMs = Math.max(0, Math.min(Number(params.timeout_ms) || 30000, 60000));
    const deadline = Date.now() + timeoutMs;
    while (Date.now() <= deadline) {
      if (await execute(`Boolean(document.querySelector(${JSON.stringify(selector)}))`)) {
        return { found: true };
      }
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    throw new Error(`Timed out waiting for selector: ${selector}`);
  }
  if (method === "browser.click") {
    assertCurrentPage();
    const selector = String(params.selector || "");
    return execute(`(() => { const el = document.querySelector(${JSON.stringify(selector)}); if (!el) throw new Error("selector not found"); el.click(); return true; })()`);
  }
  if (method === "browser.fill") {
    assertCurrentPage();
    const selector = String(params.selector || "");
    let value = String(params.value || "");
    if (params.secret_ref) {
      const secrets = await usageAccountSecrets(accountId);
      value = String(secrets[String(params.secret_ref)] || "");
      if (!value && String(params.secret_ref) === "provider_api_key" && command.bound_provider_key) {
        value = await providerApiKey(command.bound_provider_key);
      }
      if (!value) throw new Error("Requested usage secret is unavailable");
    }
    return execute(`(() => { const el = document.querySelector(${JSON.stringify(selector)}); if (!el) throw new Error("selector not found"); const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), "value"); if (setter && setter.set) setter.set.call(el, ${JSON.stringify(value)}); else el.value = ${JSON.stringify(value)}; el.dispatchEvent(new Event("input", { bubbles: true })); el.dispatchEvent(new Event("change", { bubbles: true })); return true; })()`);
  }
  if (method === "browser.text") {
    assertCurrentPage();
    const selector = String(params.selector || "");
    return execute(`(() => { const el = document.querySelector(${JSON.stringify(selector)}); if (!el) throw new Error("selector not found"); return String(el.textContent || "").slice(0, 1048576); })()`);
  }
  if (method === "browser.evaluate") {
    assertCurrentPage();
    const script = String(params.script || "");
    if (!script || script.length > 200000) throw new Error("Invalid browser evaluation script");
    return execute(script);
  }
  if (method === "browser.fetch") {
    assertCurrentPage();
    const url = assertUrl(params.url);
    const options = params.options && typeof params.options === "object" ? params.options : {};
    return execute(`(async () => { const response = await fetch(${JSON.stringify(url)}, ${JSON.stringify(options)}); const headers = Object.fromEntries(response.headers.entries()); const body = (await response.text()).slice(0, 1048576); return { status: response.status, ok: response.ok, url: response.url, headers, body }; })()`);
  }
  throw new Error("Unsupported usage browser command");
}

function validateSender(event) {
  const rawUrl = event.senderFrame && event.senderFrame.url || "";
  try {
    if (!backendUrl || new URL(rawUrl).origin !== backendUrl) throw new Error();
  } catch (_) {
    throw new Error("Untrusted IPC sender");
  }
}

function validateLoadingSender(event) {
  const rawUrl = event.senderFrame && event.senderFrame.url || "";
  const loadingUrl = pathToFileURL(path.join(__dirname, "loading.html")).href;
  if (!isTrustedLoadingUrl(rawUrl, loadingUrl)) {
    throw new Error("Untrusted startup IPC sender");
  }
}

function isOfficialOpenRouter(brand, base_url) {
  if (String(brand || "").toLowerCase() !== "openrouter") return false;
  try {
    const host = new URL(String(base_url || "")).hostname.toLowerCase();
    return host === "openrouter.ai" || host === "www.openrouter.ai";
  } catch (_) {
    return false;
  }
}

function validateProviderInput(input, options = {}) {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new Error("Invalid provider profile");
  }
  const brand = String(input.brand || "custom").trim().toLowerCase();
  if (!Object.prototype.hasOwnProperty.call(providerPresets, brand)) {
    throw new Error("Unsupported provider brand");
  }
  const preset = providerPresets[brand];
  const protocol = String(input.protocol || preset.protocol).trim();
  if (!new Set(["anthropic", "openai", "openai-compat", "typesafe"]).has(protocol)) {
    throw new Error("Unsupported provider protocol");
  }
  const base_url = String(input.base_url || preset.base_url).trim();
  let endpoint;
  try {
    endpoint = new URL(base_url);
  } catch (_) {
    throw new Error("Provider endpoint must be a valid HTTP(S) URL");
  }
  if (!["http:", "https:"].includes(endpoint.protocol) || endpoint.username || endpoint.password) {
    throw new Error("Provider endpoint must be a valid HTTP(S) URL without credentials");
  }
  const provider_type = String(input.provider_type || preset.provider_type || "standard").trim();
  if (!new Set(["standard", "opencode-go"]).has(provider_type)) {
    throw new Error("Unsupported provider type");
  }
  if (brand === "opencode-go" && provider_type !== "opencode-go") {
    throw new Error("OpenCode Go preset requires provider_type opencode-go");
  }
  if (provider_type === "opencode-go" && brand !== "opencode-go") {
    throw new Error("OpenCode Go provider type requires the OpenCode Go preset");
  }
  const officialOpenRouter = isOfficialOpenRouter(brand, base_url);
  const forTest = Boolean(options.forTest);
  const default_model = String(input.default_model || preset.default_model || "").trim();
  if ((!officialOpenRouter || !forTest) && (!default_model || default_model.length > 300)) {
    throw new Error("A valid default model is required");
  }
  if (default_model.length > 300) {
    throw new Error("A valid default model is required");
  }
  if (input.models !== undefined && !Array.isArray(input.models)) {
    throw new Error("Provider models must be a list");
  }
  const models = (input.models || []).map((model) => {
    if (typeof model !== "string" || !model.trim() || model.trim().length > 300) {
      throw new Error("Each provider model must be a non-empty string");
    }
    return model.trim();
  }).slice(0, officialOpenRouter ? 2000 : 100);
  if (officialOpenRouter && !forTest) {
    if (!models.length) throw new Error("OpenRouter account model catalog is required");
    if (!models.includes(default_model)) {
      throw new Error("OpenRouter default model must be in the account catalog");
    }
  }
  let provider_key = String(input.provider_key || `desktop:${randomUUID()}`).trim();
  if (!provider_key.startsWith("desktop:")) provider_key = `desktop:${provider_key}`;
  if (!/^desktop:[A-Za-z0-9._:-]{1,180}$/.test(provider_key)) {
    throw new Error("Invalid provider key");
  }
  const name = String(input.name || preset.label).trim();
  if (!name || name.length > 100 || base_url.length > 1000) {
    throw new Error("Invalid provider name or endpoint");
  }
  const context_channel = String(input.context_channel || "auto").trim();
  if (!new Set(["auto", "deepseek", "generic"]).has(context_channel)) {
    throw new Error("Unsupported context channel");
  }
  return {
    provider_key, name, brand, protocol, provider_type, base_url, default_model, models,
    context_channel,
  };
}

function installIpc() {
  ipcMain.handle("dialog:select-directory", async (event) => {
    validateSender(event);
    const result = await dialog.showOpenDialog(mainWindow, { properties: ["openDirectory"] });
    return result.canceled ? "" : result.filePaths[0];
  });
  ipcMain.handle("shell:open-directory", async (event, directoryPath) => {
    validateSender(event);
    if (typeof directoryPath !== "string" || directoryPath.length > 32768 || !path.isAbsolute(directoryPath)) {
      throw new Error("Directory path must be an absolute path");
    }
    const normalized = path.resolve(directoryPath);
    let stats;
    try {
      stats = await fsp.stat(normalized);
    } catch (_) {
      throw new Error("Directory does not exist");
    }
    if (!stats.isDirectory()) throw new Error("Path is not a directory");
    const failure = await shell.openPath(normalized);
    if (failure) throw new Error(`Unable to open directory: ${failure}`);
    return true;
  });
  ipcMain.handle("shell:open-external", async (event, url) => {
    validateSender(event);
    if (typeof url !== "string" || url.length > 2048) throw new Error("Only HTTP(S) URLs are allowed");
    let external;
    try {
      external = new URL(url);
    } catch (_) {
      throw new Error("Only HTTP(S) URLs are allowed");
    }
    if (!["http:", "https:"].includes(external.protocol)) throw new Error("Only HTTP(S) URLs are allowed");
    await shell.openExternal(external.href);
  });
  ipcMain.handle("window:open-pipeline", async (event, options) => {
    validateSender(event);
    return openPipelineWindow(options);
  });
  ipcMain.handle("window:close-pipeline", async (event) => {
    validateSender(event);
    const source = BrowserWindow.fromWebContents(event.sender);
    const owned = [...pipelineWindows.values()].includes(source);
    if (!source || !owned || source === mainWindow) {
      throw new Error("Only Pipeline workspace windows can close themselves");
    }
    source.close();
    return true;
  });
  ipcMain.handle("window:set-theme", async (event, theme) => {
    validateSender(event);
    applyWindowTheme(theme);
    await writeWindowTheme(currentWindowTheme);
    return true;
  });
  ipcMain.handle("provider:presets", (event) => {
    validateSender(event);
    return providerPresets;
  });
  ipcMain.handle("provider:list", async (event) => {
    validateSender(event);
    const stored = (await readStoredProfiles()).map(publicProfile);
    return [...stored, ...[...sessionProfiles.values()].map(publicProfile)];
  });
  ipcMain.handle("provider:save", async (event, input) => {
    validateSender(event);
    const metadata = validateProviderInput(input);
    const provider_key = metadata.provider_key;
    const existing = (await materializedProfiles()).find((item) => item.provider_key === provider_key);
    const api_key = String(input.api_key || existing && existing.api_key || "");
    if (!api_key || api_key.length > 10000) throw new Error("A valid API key is required");
    const safety = encryptionSafety();
    if (!safety.safe || input.persist === false) {
      sessionProfiles.set(provider_key, { ...metadata, api_key, storage: "session" });
      await syncProviders();
      return { ...publicProfile(sessionProfiles.get(provider_key)), persisted: false, reason: safety.safe ? "session requested" : `unsafe storage backend: ${safety.backendName}` };
    }
    const stored = await readStoredProfiles();
    const record = { ...metadata, encrypted_api_key: safeStorage.encryptString(api_key).toString("base64"), storage: "safeStorage" };
    const next = stored.filter((item) => item.provider_key !== provider_key);
    next.push(record);
    await writeStoredProfiles(next);
    sessionProfiles.delete(provider_key);
    await syncProviders();
    return { ...publicProfile(record), persisted: true };
  });
  ipcMain.handle("provider:remove", async (event, providerKey) => {
    validateSender(event);
    if (typeof providerKey !== "string" || !/^desktop:[A-Za-z0-9._:-]{1,180}$/.test(providerKey)) {
      throw new Error("Invalid provider key");
    }
    const key = providerKey;
    const next = (await readStoredProfiles()).filter((item) => item.provider_key !== key);
    await writeStoredProfiles(next);
    sessionProfiles.delete(key);
    await syncProviders();
    return true;
  });
  ipcMain.handle("provider:test", async (event, input) => {
    validateSender(event);
    const brand = String(input && input.brand || "").toLowerCase();
    const base_url = String(input && input.base_url || (providerPresets[brand] || {}).base_url || "");
    const metadata = validateProviderInput(input, {
      forTest: isOfficialOpenRouter(brand, base_url),
    });
    const existing = (await materializedProfiles()).find((item) => item.provider_key === metadata.provider_key);
    const api_key = String(input.api_key || existing && existing.api_key || "");
    if (!api_key || api_key.length > 10000) throw new Error("A valid API key is required");
    const profile = { ...metadata, api_key };
    return backendRequest("/api/client/providers/test", { profile });
  });
  ipcMain.handle("usage:list-accounts", async (event) => {
    validateSender(event);
    const stored = (await readStoredUsageAccounts()).map(publicUsageAccount);
    return [...stored, ...[...sessionUsageAccounts.values()].map(publicUsageAccount)];
  });
  ipcMain.handle("usage:save-account", async (event, input) => {
    validateSender(event);
    return saveUsageAccount(input);
  });
  ipcMain.handle("usage:remove-account", async (event, accountId) => {
    validateSender(event);
    return removeUsageAccount(accountId);
  });
  ipcMain.handle("usage:browser-command", async (event, command) => {
    validateSender(event);
    return runUsageBrowserCommand(command);
  });
  ipcMain.handle("runtime:diagnostics", (event) => {
    validateSender(event);
    return diagnostics.slice();
  });
  ipcMain.handle("runtime:restart", async (event) => {
    validateLoadingSender(event);
    if (restartPromise) return restartPromise;
    restartPromise = (async () => {
      await stopBackend();
      await bootApp();
      return true;
    })();
    try {
      return await restartPromise;
    } finally {
      restartPromise = null;
    }
  });
}

async function bootApp() {
  await startBackend();
  rememberDiagnostic("boot: backend healthy");
  await syncProviders();
  rememberDiagnostic("boot: providers synced");
  await syncUsageAccounts();
  rememberDiagnostic("boot: usage accounts synced");
  await mainWindow.loadURL(`${backendUrl}/?desktop_theme=${currentWindowTheme}`);
  rememberDiagnostic("boot: app loaded");
  bootedOnce = true;
}

function scheduleBackendRecovery(code) {
  if (!bootedOnce || recovering || restartPromise || quitting) return;
  const now = Date.now();
  while (recoveryAttempts.length && now - recoveryAttempts[0] > 5 * 60 * 1000) {
    recoveryAttempts.shift();
  }
  if (recoveryAttempts.length >= 3) {
    rememberDiagnostic("backend keeps exiting; auto recovery disabled, waiting for manual restart");
    return;
  }
  recoveryAttempts.push(now);
  recovering = true;
  (async () => {
    try {
      rememberDiagnostic(`backend exited unexpectedly (code ${code}); recovering`);
      await mainWindow.loadFile(path.join(__dirname, "loading.html"), {
        query: { theme: currentWindowTheme },
      });
      await bootApp();
    } catch (error) {
      rememberDiagnostic(error.stack || error.message);
      try {
        await mainWindow.webContents.executeJavaScript(
          `window.showStartupError(${JSON.stringify(String(error.message || error))}, ${JSON.stringify(diagnostics)})`
        );
      } catch (_) {}
    } finally {
      recovering = false;
    }
  })();
}

const gotSingleInstanceLock = app.requestSingleInstanceLock();
if (!gotSingleInstanceLock) app.quit();
app.on("second-instance", () => {
  if (mainWindow && !mainWindow.isDestroyed()) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.focus();
  }
});

app.whenReady().then(async () => {
  if (!gotSingleInstanceLock) return;
  session.defaultSession.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false));
  installIpc();
  createWindow();
  try {
    await mainWindow.loadFile(path.join(__dirname, "loading.html"), {
      query: { theme: currentWindowTheme },
    });
    rememberDiagnostic("boot: loading window shown");
    await bootApp();
  } catch (error) {
    rememberDiagnostic(error.stack || error.message);
    try {
      await mainWindow.webContents.executeJavaScript(
        `window.showStartupError(${JSON.stringify(String(error.message || error))}, ${JSON.stringify(diagnostics)})`
      );
    } catch (_) {}
  }
});

app.on("window-all-closed", () => app.quit());
app.on("before-quit", () => {
  quitting = true;
  for (const window of pipelineWindows.values()) {
    if (!window.isDestroyed()) window.destroy();
  }
  pipelineWindows.clear();
  for (const record of usageWindows.values()) {
    if (!record.window.isDestroyed()) record.window.destroy();
  }
  usageWindows.clear();
  if (backend && backend.exitCode === null) backend.kill();
});
