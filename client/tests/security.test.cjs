"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.resolve(__dirname, "..");
const main = fs.readFileSync(path.join(root, "main.cjs"), "utf8");
const preload = fs.readFileSync(path.join(root, "preload.cjs"), "utf8");

test("renderer is sandboxed and isolated", () => {
  assert.match(main, /sandbox:\s*true/);
  assert.match(main, /contextIsolation:\s*true/);
  assert.match(main, /nodeIntegration:\s*false/);
});

test("desktop window uses the integrated secure title bar", () => {
  assert.match(main, /titleBarStyle:\s*process\.platform/);
  assert.match(main, /titleBarOverlay/);
  assert.match(main, /height:\s*52/);
  assert.match(main, /setTitleBarOverlay/);
  assert.match(main, /window:set-theme/);
  assert.match(preload, /setWindowTheme/);
  assert.match(main, /readWindowTheme/);
  assert.match(main, /ui-state\.json/);
  assert.match(main, /nativeTheme\.shouldUseDarkColors/);
});

test("provider secrets use safeStorage and are not exposed by preload", () => {
  assert.match(main, /safeStorage\.encryptString/);
  assert.match(main, /backendName !== "basic_text"/);
  assert.doesNotMatch(preload, /decryptString|encrypted_api_key/);
});

test("legacy single provider profile is normalized without rewriting secrets", () => {
  const reader = main.slice(
    main.indexOf("async function readStoredProfiles"),
    main.indexOf("async function writeStoredProfiles"),
  );
  assert.match(reader, /Array\.isArray\(value\)/);
  assert.match(reader, /value && typeof value === "object" \? \[value\] : \[\]/);
  assert.doesNotMatch(reader, /writeStoredProfiles|JSON\.stringify/);
});

test("OpenCode Go preset uses the logical gateway and mixed-protocol metadata", () => {
  assert.match(main, /"opencode-go"/);
  assert.match(main, /provider_type:\s*"opencode-go"/);
  assert.match(main, /https:\/\/opencode\.ai\/zen\/go/);
  assert.match(main, /default_model:\s*"deepseek-v4-flash"/);
});

test("usage accounts use safeStorage and browser sessions are isolated by account", () => {
  assert.match(main, /usage-accounts\.json/);
  assert.match(main, /encrypted_secrets:\s*safeStorage\.encryptString/);
  assert.doesNotMatch(preload, /encrypted_secrets|decryptString/);
  assert.match(main, /persist:mewcode-usage-/);
  assert.match(main, /session\.fromPartition/);
  assert.match(main, /webRequest\.onBeforeRequest/);
  assert.match(main, /Usage browser origin is not allowed/);
  assert.match(main, /record\.allowedOrigins = origins/);
  assert.match(main, /Usage browser is not on a declared origin/);
  assert.match(main, /setPermissionRequestHandler/);
  assert.match(main, /Cookie 不会发送给 Python 插件/);
});

test("usage browser secret references are resolved in main without renderer plaintext", () => {
  assert.match(main, /params\.secret_ref/);
  assert.match(main, /usageAccountSecrets\(accountId\)/);
  assert.match(preload, /runUsageBrowserCommand/);
  assert.match(main, /usage:browser-command/);
});

test("IPC sender and external URL are validated", () => {
  assert.match(main, /validateSender\(event\)/);
  assert.match(main, /new URL\(rawUrl\)\.origin !== backendUrl/);
  assert.match(main, /Only HTTP\(S\) URLs are allowed/);
  assert.match(main, /validateProviderInput\(input/);
  assert.match(main, /Unsupported provider protocol/);
});

test("startup restart IPC trusts loading.html even with a theme query", () => {
  const { isTrustedLoadingUrl } = require("../loading-sender.cjs");
  const loading = "file:///C:/MewCode/loading.html";
  assert.equal(isTrustedLoadingUrl(`${loading}?theme=dark`, loading), true);
  assert.equal(isTrustedLoadingUrl(`${loading}?theme=light`, loading), true);
  assert.equal(isTrustedLoadingUrl(loading, loading), true);
  assert.equal(isTrustedLoadingUrl("file:///C:/MewCode/index.html?theme=dark", loading), false);
  assert.equal(isTrustedLoadingUrl("http://127.0.0.1:7788/?theme=dark", loading), false);
  assert.match(main, /isTrustedLoadingUrl\(rawUrl, loadingUrl\)/);
  assert.doesNotMatch(main, /rawUrl !== loadingUrl/);
});

test("official OpenRouter saves a frozen account catalog instead of a 100-model slice", () => {
  assert.match(main, /function isOfficialOpenRouter\(brand, base_url\)/);
  assert.match(main, /OpenRouter account model catalog is required/);
  assert.match(main, /officialOpenRouter \? 2000 : 100/);
  assert.match(main, /forTest: isOfficialOpenRouter\(brand, base_url\)/);
});

test("project directories are opened through a validated desktop IPC boundary", () => {
  assert.match(preload, /openDirectory:.*shell:open-directory/);
  assert.match(main, /ipcMain\.handle\("shell:open-directory"/);
  assert.match(main, /path\.isAbsolute\(directoryPath\)/);
  assert.match(main, /stats = await fsp\.stat\(normalized\)/);
  assert.match(main, /stats\.isDirectory\(\)/);
  assert.match(main, /shell\.openPath\(normalized\)/);
});

test("Pipeline canvas opens in a constrained internal desktop window", () => {
  assert.match(preload, /openPipeline:.*window:open-pipeline/);
  assert.match(preload, /closePipeline:.*window:close-pipeline/);
  assert.match(main, /ipcMain\.handle\("window:open-pipeline"/);
  assert.match(main, /ipcMain\.handle\("window:close-pipeline"/);
  assert.match(main, /async function openPipelineWindow\(/);
  assert.match(main, /"\/expert-flow"\s*:\s*"\/pipeline"/);
  assert.match(main, /Invalid Pipeline graph id/);
  assert.match(main, /Only Pipeline workspace windows can close themselves/);
  assert.match(main, /target\.searchParams\.set\("desktop_theme", currentWindowTheme\)/);
  assert.match(main, /nodeIntegration:\s*false/);
});

test("packaged sidecar writes runtime state under Electron userData", () => {
  assert.match(main, /app\.getPath\("userData"\)/);
  assert.match(main, /MEWCODE_USER_DATA_DIR:\s*userDataDir/);
  assert.match(main, /if \(app\.isPackaged\) spec\.cwd = runtimeDir/);
});

test("sidecar startup tolerates slow project initialization and serializes restart", () => {
  assert.match(main, /function probeBackend\(url, timeoutMs = 2500\)/);
  assert.match(main, /waitForBackend\(url, processRef, timeoutMs = 120000\)/);
  assert.match(main, /await stopBackend\(\);\s*await bootApp\(\)/);
  assert.match(main, /async function bootApp\(\) \{\s*await startBackend\(\)/);
  assert.match(main, /if \(restartPromise\) return restartPromise/);
});

test("packaged Windows client executable is named liteagent", () => {
  const forge = fs.readFileSync(path.join(root, "forge.config.cjs"), "utf8");
  assert.match(forge, /executableName:\s*"liteagent"/);
  assert.match(forge, /config:\s*\{\s*name:\s*"liteagent"/);
});
