# Tauri desktop shell

`src-tauri` is the first Tauri port of MewCode. It keeps the existing FastAPI
agent runtime and static renderer, replacing only the Electron window and
sidecar process manager.

## Runtime model

- The Rust shell first displays a bundled loading page in the main window,
  then starts `mewcode-backend` (or the repository Python fallback) on a
  background thread. Windows children use `CREATE_NO_WINDOW`; stdout/stderr
  remain in the existing backend logs. It allocates a loopback port, injects
  `MEWCODE_DESKTOP_TOKEN`, and waits for `/api/health` without blocking the UI.
- Once healthy, the same window navigates to the workspace. Startup failures
  stay visible on the loading page with retry and log-directory actions.
  Closing during startup cancels readiness checks and stops the backend tree.
- The shell opens `/desktop-boot`, which sets an HttpOnly session cookie and
  redirects to the authenticated chat page. HTTP and WebSocket requests use
  the same session checks.
- The sidebar's **在浏览器打开** action opens the same backend and history in a
  browser using a single-use link that expires after 90 seconds. It never puts
  the desktop credential in the browser launch URL. Local launchers can set
  `MEWCODE_BROWSER_LAUNCH_FILE` to a new file path before starting the desktop;
  the backend writes `{url, expires_in}` once it finishes startup. Open the URL
  immediately and remove the handoff file after consumption. No authentication
  exception or second backend is needed.
- Android does not start the Python sidecar. It loads the self-contained
  `frontend/static/mobile.html` remote workspace over the HTTPS custom
  protocol (`useHttpsScheme: true`) so styles and pairing logic do not depend
  on `/static/...` FastAPI paths.
- `frontend/static/tauri-bridge.js` exposes the same `window.mewcodeDesktop`
  surface used by the Electron renderer, so normal browser mode remains
  unchanged.
- Provider and usage account records are stored under the Tauri app-data
  directory and synchronized to the running Python backend. The first port
  does not yet use Electron's `safeStorage`; production packaging should add an
  OS keyring implementation before persisting long-lived provider secrets.
- Usage portal browser commands intentionally reject with a clear error. Tauri
  can add an isolated webview for that feature later without changing the
  Python usage protocol.

## Development

### Open cited local files

Local paths cited in chat open without navigating away from the conversation.
Directory links use the desktop file manager. Files inside the current project
open a text preview with download and folder actions; unsupported binary types
can be downloaded. Missing files display an error in the preview. Windows drive
paths, `/C:/...` citations, `file:///...` links, and relative project files are
recognized. Web links keep their normal behavior.

### Connect an existing workflow

Open **工作台 → 连接运行 → 打开当前工作流** to display a separately running
local workflow. **Ctrl+Shift+O** opens the only available connection directly,
or the connection list when there is more than one. The connected canvas, node dialogue, artifacts, and execution
status come from the original runtime. Graph edits and node controls go to that
same runtime; the desktop does not copy its run store or start another executor.

Local launchers register the runtime with
`mewcode.workflow_core.connections.register(url=..., run_id=...,
conversation_id=..., project=..., title=..., profile=...)`, using the desktop's
app-data directory as `profile`. The registry accepts explicit `127.0.0.1` HTTP
origins and verifies the run identity before offering a connection. Each client
page is bound to one run, conversation, and project. It uses the desktop's
authenticated origin and forwards only the supported workflow API requests.
Desktop credentials are not forwarded. Closing the connection returns to the
local workbench; it leaves the original runtime running.

Install Rust and Tauri CLI, then from this directory run:

```powershell
cargo install tauri-cli --version "^2"
$env:MEWCODE_REPO_ROOT = (Get-Location).Path
cargo tauri dev --manifest-path src-tauri/Cargo.toml
```

To use a packaged sidecar, set `MEWCODE_BACKEND` to the executable path, or
place `mewcode-backend(.exe)` under the packaged resource `backend` directory.
The default development fallback uses `.venv/Scripts/python.exe
frontend/server.py` on Windows and the corresponding `.venv/bin/python` path
on Unix. The Tauri bundle intentionally does not reference a missing external
binary, so packaging can provide the sidecar from its own CI artifact.

Rust/Cargo is required to rebuild the desktop executable. Startup process
regressions can be checked with:

```powershell
cargo test --manifest-path src-tauri/Cargo.toml --bin mewcode-tauri --release
cargo build --manifest-path src-tauri/Cargo.toml --bin mewcode-tauri --release
```

`tests/DesktopStartupBackend.cs` provides an isolated backend whose health and
first failure can be controlled with fixture files. Compile it as a console
executable with the Windows C# compiler, then run
`python tests/desktop_startup_smoke.py OUTPUT --backend FIXTURE.exe` to verify
the actual native loading page, failure, retry and navigation over WebView2
CDP. `--theme dark --cancel` leaves a loading window for a close/cancellation
check. Use a fresh output directory each time; fixture profiles are isolated
and no model requests are sent. Close the test window after the check.

## Android deployment

Android is configured as a Tauri 2 mobile target. The reproducible setup stores
the SDK and NDK under `D:\enviorments\mewcode-tauri` and does not modify the
system Android installation:

```powershell
.\scripts\setup-android.ps1
.\scripts\build-android.ps1 -DebugBuild
```

The build script defaults to the `aarch64` (`arm64-v8a`) target to keep test
APKs small. Pass `-Target universal` only when one APK must support emulators
and multiple device architectures.

The setup installs Android platform 35, build tools 35.0.0 and NDK
27.0.12077973. `cargo tauri android build` produces Android APK/AAB artifacts.
The Python FastAPI sidecar is desktop-only. Android configuration is created
at pairing time: the desktop discovers its own Tailscale MagicDNS endpoint,
then the phone lists nearby computers or enters a MagicDNS / Tailscale IP.
The one-time pairing code is exchanged for an app-private device credential;
no remote token or API key is embedded in the APK or exposed to WebView
JavaScript. An optional `-BackendUrl` can be used only for preconfiguration
with a real HTTPS Tailnet endpoint.
Release signing remains a deployment secret and is provided through the
standard Android/Gradle signing configuration. The build script keeps its
Gradle cache under the local Tauri project directory by default.

## Remote node configuration

Run MewCode behind a reachable HTTPS Tailscale Serve endpoint. In the desktop
app, approve the phone's pairing request. The remote proxy permits only
MewCode history, conversation create/send, expert-team live progress,
pause/resume, interactive prompts, and operation polling. Codex and
Claude Code transcripts are not shown on the phone; the shared thread is the
active MewCode conversation on the computer. Use the computer's MagicDNS
name (`*.ts.net`), not a Tailscale `100.x` address.
