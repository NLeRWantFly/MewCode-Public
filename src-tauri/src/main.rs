#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

#[cfg(target_os = "android")]
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::fs;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Manager, State, WebviewUrl, WebviewWindow, WebviewWindowBuilder};

struct RuntimeState {
    backend_url: Mutex<String>,
    desktop_token: Mutex<String>,
    backend: Mutex<Option<Child>>,
    diagnostics: Mutex<Vec<String>>,
    theme: Mutex<String>,
    app_data_dir: PathBuf,
    repo_root: PathBuf,
    providers: Mutex<Vec<Value>>,
    usage_accounts: Mutex<Vec<Value>>,
    /// Why the on-disk providers snapshot could not be loaded, if it could not.
    /// While this is set, provider writes are refused instead of clobbering a
    /// file the shell failed to read.
    providers_error: Mutex<Option<String>>,
    /// Same contract for usage-accounts.json.
    usage_accounts_error: Mutex<Option<String>>,
    startup: Mutex<StartupStatus>,
    starting: AtomicBool,
    shutting_down: AtomicBool,
}

#[derive(Clone, serde::Serialize)]
struct StartupStatus {
    phase: String,
    message: String,
    detail: String,
    log_dir: String,
}

impl RuntimeState {
    fn startup_status(&self, phase: &str, message: &str, detail: &str) {
        *self.startup.lock().expect("startup mutex poisoned") = StartupStatus {
            phase: phase.into(), message: message.into(), detail: detail.into(),
            log_dir: self.app_data_dir.to_string_lossy().into_owned(),
        };
    }

    fn remember(&self, message: impl Into<String>) {
        let mut items = self.diagnostics.lock().expect("diagnostics mutex poisoned");
        items.push(message.into());
        if items.len() > 80 {
            let excess = items.len() - 80;
            items.drain(0..excess);
        }
    }

    fn backend_url(&self) -> String {
        self.backend_url.lock().expect("backend url mutex poisoned").clone()
    }

    fn token(&self) -> String {
        self.desktop_token.lock().expect("desktop token mutex poisoned").clone()
    }

    fn stop_backend(&self) {
        let mut slot = self.backend.lock().expect("backend mutex poisoned");
        if let Some(mut child) = slot.take() {
            stop_child(&mut child);
        }
    }
}

fn hide_console(command: &mut Command) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000); // CREATE_NO_WINDOW, including Python's venv redirector.
    }
    #[cfg(not(windows))]
    let _ = command;
}

fn stop_child(child: &mut Child) {
    if matches!(child.try_wait(), Ok(Some(_))) { return; }
    #[cfg(windows)]
    {
        // The venv executable can own another Python process. Stop that tree too.
        let mut command = Command::new("taskkill");
        command.args(["/PID", &child.id().to_string(), "/T", "/F"])
            .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
        hide_console(&mut command);
        let _ = command.status();
    }
    let _ = child.kill();
    let _ = child.wait();
}

fn generated_token() -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    format!("mewcode-{:x}-{:x}", now, std::process::id())
}

fn free_port() -> Result<u16, String> {
    let listener = TcpListener::bind(("127.0.0.1", 0)).map_err(|e| e.to_string())?;
    listener.local_addr().map(|address| address.port()).map_err(|e| e.to_string())
}

/// Outcome of reading a JSON array file owned by the desktop shell.
#[derive(Debug, PartialEq)]
enum JsonArrayLoad {
    /// Parsed successfully. An empty array here is a real value, not a failure.
    Loaded(Vec<Value>),
    /// No file yet: an empty list is the correct starting state.
    Missing,
    /// The file exists but could not be used. Never collapse this into `Loaded(vec![])`:
    /// that turned "unreadable file" into "no providers at all", and the next save
    /// wrote the empty list back over a file that still held every profile.
    Failed(String),
}

/// Read a UTF-8 text file, tolerating the BOM that editors and PowerShell 5.1's
/// `Set-Content -Encoding utf8` prepend. serde_json rejects a leading BOM, so
/// without this the whole file looked unparseable.
fn read_text_without_bom(path: &Path) -> std::io::Result<String> {
    let bytes = fs::read(path)?;
    let body = bytes.strip_prefix(&[0xEF, 0xBB, 0xBF][..]).unwrap_or(&bytes);
    String::from_utf8(body.to_vec())
        .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))
}

fn load_json_vec(path: &Path) -> JsonArrayLoad {
    let text = match read_text_without_bom(path) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return JsonArrayLoad::Missing,
        Err(error) => return JsonArrayLoad::Failed(format!("unreadable: {error}")),
    };
    match serde_json::from_str::<Value>(&text) {
        Ok(Value::Array(rows)) => JsonArrayLoad::Loaded(rows),
        Ok(_) => JsonArrayLoad::Failed("top-level value is not an array".into()),
        Err(error) => JsonArrayLoad::Failed(format!("invalid JSON: {error}")),
    }
}

/// Refuse a write while the file backing it could not be loaded.
fn write_guard(error: &Mutex<Option<String>>, name: &str) -> Result<(), String> {
    match error.lock().expect("load error mutex poisoned").clone() {
        Some(reason) => Err(format!(
            "{name} could not be read ({reason}); refusing to overwrite it. \
             Repair or move the file, then restart the desktop app."
        )),
        None => Ok(()),
    }
}

fn clear_load_error(error: &Mutex<Option<String>>) {
    *error.lock().expect("load error mutex poisoned") = None;
}

fn read_theme(path: &Path) -> String {
    read_text_without_bom(path)
        .ok()
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .and_then(|value| value.get("theme").and_then(Value::as_str).map(str::to_owned))
        .filter(|theme| theme == "light" || theme == "dark")
        .unwrap_or_else(|| "dark".into())
}

fn write_json_vec(path: &Path, values: &[Value]) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let temporary = path.with_extension("json.tmp");
    let text = serde_json::to_string_pretty(values).map_err(|e| e.to_string())?;
    fs::write(&temporary, text).map_err(|e| e.to_string())?;
    fs::rename(&temporary, path).map_err(|e| e.to_string())
}

fn looks_like_repo_root(path: &Path) -> bool {
    path.join("frontend").join("server.py").is_file() && path.join("pyproject.toml").is_file()
}

fn explicit_repo_root(path: &Path, launch_dir: &Path) -> Option<PathBuf> {
    let root = if path.is_absolute() { path.to_path_buf() } else { launch_dir.join(path) };
    // Keep program/script arguments absolute when the sidecar cwd changes.
    if root.is_absolute() && looks_like_repo_root(&root) { Some(root) } else { None }
}

fn resolve_app_data_dir(default: PathBuf, override_path: Option<std::ffi::OsString>) -> Result<PathBuf, String> {
    match override_path {
        None => Ok(default),
        Some(raw) => {
            let path = PathBuf::from(raw);
            if !path.is_absolute() {
                return Err("MEWCODE_USER_DATA_DIR must be an absolute directory path".into());
            }
            Ok(path)
        }
    }
}

fn discover_repo_root() -> PathBuf {
    if let Some(explicit) = std::env::var_os("MEWCODE_REPO_ROOT") {
        let path = PathBuf::from(explicit);
        if let Some(root) = explicit_repo_root(&path, &std::env::current_dir().unwrap_or_default()) {
            return root;
        }
    }
    let mut starts = Vec::new();
    if let Ok(cwd) = std::env::current_dir() {
        starts.push(cwd);
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            starts.push(parent.to_path_buf());
        }
    }
    for start in starts {
        let mut ancestor = Some(start.as_path());
        while let Some(root) = ancestor {
            if looks_like_repo_root(root) {
                return root.to_path_buf();
            }
            ancestor = root.parent();
        }
    }
    std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))
}

fn venv_python(root: &Path) -> PathBuf {
    if cfg!(windows) {
        root.join(".venv").join("Scripts").join("python.exe")
    } else {
        root.join(".venv").join("bin").join("python")
    }
}

fn sidecar_spec(app: &AppHandle, state: &RuntimeState, port: u16) -> Result<(PathBuf, Vec<String>, PathBuf), String> {
    let args = |server: Option<PathBuf>| {
        let mut values = Vec::new();
        if let Some(path) = server { values.push(path.to_string_lossy().into_owned()); }
        values.extend(["--host".into(), "127.0.0.1".into(), "--port".into(), port.to_string()]);
        values
    };
    if let Ok(explicit) = std::env::var("MEWCODE_BACKEND") {
        return Ok((PathBuf::from(explicit), args(None), state.app_data_dir.clone()));
    }
    let resource_dir = app.path().resource_dir().map_err(|e| e.to_string())?;
    let executable = if cfg!(windows) { "mewcode-backend.exe" } else { "mewcode-backend" };
    for candidate in [resource_dir.join("backend").join(executable), resource_dir.join("binaries").join(executable)] {
        if candidate.exists() {
            return Ok((candidate, args(None), state.app_data_dir.clone()));
        }
    }
    let server = state.repo_root.join("frontend").join("server.py");
    if !server.is_file() {
        return Err(format!("MewCode frontend server is missing at {}", server.display()));
    }
    if let Ok(explicit) = std::env::var("MEWCODE_PYTHON") {
        let python = PathBuf::from(explicit);
        if python.exists() {
            return Ok((python, args(Some(server)), state.repo_root.clone()));
        }
    }
    let python = venv_python(&state.repo_root);
    if python.exists() {
        return Ok((python, args(Some(server.clone())), state.repo_root.clone()));
    }
    let mut ancestor = Some(state.repo_root.as_path());
    while let Some(root) = ancestor {
        let python_candidate = venv_python(root);
        if python_candidate.exists() {
            return Ok((python_candidate, args(Some(server.clone())), state.repo_root.clone()));
        }
        ancestor = root.parent();
    }
    let python = PathBuf::from(if cfg!(windows) { "python" } else { "python3" });
    Ok((python, args(Some(server)), state.repo_root.clone()))
}

fn probe_health(url: &str) -> bool {
    let parsed = match url::Url::parse(url) { Ok(value) => value, Err(_) => return false };
    let port = match parsed.port_or_known_default() { Some(value) => value, None => return false };
    let mut stream = match TcpStream::connect(("127.0.0.1", port)) { Ok(value) => value, Err(_) => return false };
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    let request = format!("GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nConnection: close\r\n\r\n", port);
    if stream.write_all(request.as_bytes()).is_err() { return false; }
    let mut response = String::new();
    if stream.read_to_string(&mut response).is_err() { return false; }
    response.starts_with("HTTP/1.1 2") || response.starts_with("HTTP/1.0 2")
}

fn backend_working_directory(default: PathBuf, data_root: &Path, isolated: bool) -> PathBuf {
    // Development Python sidecars otherwise use repo_root, whose relative
    // .mewcode/web_state.json and sessions may belong to a live user task.
    // Match the packaged sidecar's private cwd for explicit data profiles.
    if isolated { data_root.to_path_buf() } else { default }
}

fn wait_for_backend(mut inspect_exit: impl FnMut() -> Result<Option<std::process::ExitStatus>, String>,
                    mut healthy: impl FnMut() -> bool, timeout: Duration) -> Result<(), String> {
    let started = Instant::now();
    loop {
        if let Some(status) = inspect_exit()? {
            return Err(format!("MewCode backend exited before becoming ready ({status}); see backend.err.log"));
        }
        if healthy() { return Ok(()); }
        if started.elapsed() >= timeout {
            return Err(format!("timed out waiting for Python backend health after {} seconds", timeout.as_secs()));
        }
        thread::sleep(Duration::from_millis(250));
    }
}

fn report_startup_failure(data_dir: &Path, message: &str) {
    let text = format!("MewCode 启动失败：\n{message}\n\n日志目录：{}", data_dir.display());
    let _ = fs::write(data_dir.join("startup-error.log"), &text);
    eprintln!("{text}");
    #[cfg(target_os = "windows")]
    {
        // A GUI launch has no visible stderr. Show the startup failure without
        // invoking a shell or leaving a silent 120-second background process.
        #[link(name = "user32")]
        extern "system" { fn MessageBoxW(hwnd: isize, text: *const u16, caption: *const u16, flags: u32) -> i32; }
        let body: Vec<u16> = text.encode_utf16().chain(Some(0)).collect();
        let title: Vec<u16> = "MewCode".encode_utf16().chain(Some(0)).collect();
        unsafe { MessageBoxW(0, body.as_ptr(), title.as_ptr(), 0x10); }
    }
}

fn start_backend(app: &AppHandle, state: &RuntimeState) -> Result<(), String> {
    if state.shutting_down.load(Ordering::SeqCst) { return Err("Startup cancelled".into()); }
    state.stop_backend();
    let port = free_port()?;
    let token = generated_token();
    let url = format!("http://127.0.0.1:{}", port);
    let (program, args, cwd) = sidecar_spec(app, state, port)?;
    // Desktop profiles, WebView state and the sidecar must share the same
    // explicit data root. A backend-only override still reads real profiles.
    let backend_data_dir = state.app_data_dir.clone();
    fs::create_dir_all(&backend_data_dir)
        .map_err(|e| format!("unable to create MewCode data directory: {}", e))?;
    let cwd = backend_working_directory(cwd, &backend_data_dir,
        std::env::var_os("MEWCODE_USER_DATA_DIR").is_some());
    let backend_stdout = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(backend_data_dir.join("backend.out.log"))
        .map_err(|e| format!("unable to open backend stdout log: {}", e))?;
    let backend_stderr = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(backend_data_dir.join("backend.err.log"))
        .map_err(|e| format!("unable to open backend stderr log: {}", e))?;
    let mut command = Command::new(&program);
    hide_console(&mut command);
    command
        .args(args)
        .current_dir(cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::from(backend_stdout))
        .stderr(Stdio::from(backend_stderr));
    command.env("MEWCODE_DESKTOP_TOKEN", &token);
    command.env("MEWCODE_BACKEND_PORT", port.to_string());
    command.env("MEWCODE_USER_DATA_DIR", &backend_data_dir);
    // DATA_DIR is always passed to the sidecar, including normal production
    // launches. Do not mistake that mandatory path for an isolated profile.
    command.env("MEWCODE_DESKTOP_PROFILE_ISOLATED",
        if std::env::var_os("MEWCODE_USER_DATA_DIR").is_some() { "1" } else { "0" });
    command.env("PYTHONUNBUFFERED", "1");
    let mut child = command.spawn().map_err(|e| format!("unable to start MewCode backend: {}", e))?;
    {
        let mut slot = state.backend.lock().expect("backend mutex poisoned");
        if state.shutting_down.load(Ordering::SeqCst) {
            stop_child(&mut child);
            return Err("Startup cancelled".into());
        }
        *slot = Some(child);
    }
    *state.backend_url.lock().expect("backend url mutex poisoned") = url.clone();
    *state.desktop_token.lock().expect("desktop token mutex poisoned") = token;
    let ready = wait_for_backend(|| {
        if state.shutting_down.load(Ordering::SeqCst) { return Err("Startup cancelled".into()); }
        // Do not hold the process mutex while waiting for health. Closing the
        // loading window must be able to stop the child immediately.
        let mut slot = state.backend.lock().expect("backend mutex poisoned");
        slot.as_mut().ok_or("Backend stopped")?.try_wait().map_err(|e| e.to_string())
    }, || probe_health(&url), Duration::from_secs(120));
    if let Err(error) = ready {
        state.stop_backend();
        return Err(error);
    }
    state.remember(format!("backend healthy at {}", url));
    Ok(())
}

fn desktop_boot_url(state: &RuntimeState, next_path: &str) -> String {
    format!(
        "{}/desktop-boot?session={}&next={}",
        state.backend_url(),
        url_encode(&state.token()),
        url_encode(next_path)
    )
}

fn create_main_window(app: &AppHandle, state: &RuntimeState) -> Result<(), String> {
    let theme = state.theme.lock().expect("theme mutex poisoned").clone();
    // The bundled loading page paints before Python or the network is ready.
    let mut builder = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("startup.html".into()))
        .title("MewCode")
        .inner_size(1440.0, 940.0)
        .min_inner_size(900.0, 640.0)
        .background_color(if theme == "light" { tauri::window::Color(248, 249, 252, 255) }
                          else { tauri::window::Color(19, 22, 29, 255) })
        .initialization_script(format!("window.__MEWCODE_STARTUP_THEME__ = {};", json!(theme)))
        .visible(false);
    // Preserve existing installations' WebView storage. Explicit isolated
    // profiles must not share cookies or unsent drafts with that installation.
    if std::env::var_os("MEWCODE_USER_DATA_DIR").is_some() {
        builder = builder.data_directory(state.app_data_dir.join("webview"));
    }
    let window = builder.build()
        .map_err(|e| e.to_string())?;
    window.show().map_err(|e| e.to_string())?;
    Ok(())
}

fn launch_desktop_backend(app: AppHandle) -> Result<(), String> {
    let state = app.state::<RuntimeState>();
    if state.shutting_down.load(Ordering::SeqCst) { return Err("Startup cancelled".into()); }
    if state.starting.compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst).is_err() {
        return Ok(());
    }
    state.startup_status("starting", "正在启动本地服务", "");
    let worker_app = app.clone();
    thread::spawn(move || {
        let state = worker_app.state::<RuntimeState>();
        let result = start_backend(&worker_app, &state).and_then(|_| {
            if state.shutting_down.load(Ordering::SeqCst) { return Err("Startup cancelled".into()); }
            state.startup_status("opening", "正在打开工作区", "");
            let theme = state.theme.lock().expect("theme mutex poisoned").clone();
            let boot = desktop_boot_url(&state, &format!("/?desktop_theme={theme}"));
            let window = worker_app.get_webview_window("main").ok_or("Main window closed")?;
            // Native navigation retains the existing HttpOnly cookie handoff.
            window.navigate(boot.parse().map_err(|e: url::ParseError| e.to_string())?)
                .map_err(|e| e.to_string())
        });
        if let Err(error) = result {
            state.stop_backend();
            if !state.shutting_down.load(Ordering::SeqCst) {
                let _ = fs::write(state.app_data_dir.join("startup-error.log"), &error);
                state.remember(format!("startup failed: {error}"));
                state.startup_status("error", "启动未完成", &error);
            }
        } else {
            if let Err(error) = sync_profiles(&state) { state.remember(format!("provider sync deferred: {error}")); }
            if let Err(error) = sync_usage_accounts(&state) { state.remember(format!("usage account sync deferred: {error}")); }
        }
        state.starting.store(false, Ordering::SeqCst);
    });
    Ok(())
}

#[tauri::command]
fn startup_status(state: State<'_, RuntimeState>) -> StartupStatus {
    state.startup.lock().expect("startup mutex poisoned").clone()
}

#[tauri::command]
fn retry_startup(app: AppHandle) -> Result<(), String> {
    if app.state::<RuntimeState>().startup.lock().expect("startup mutex poisoned").phase != "error" {
        return Err("Startup is already in progress".into());
    }
    launch_desktop_backend(app)
}

fn reload_main(app: &AppHandle, state: &RuntimeState) -> Result<(), String> {
    let window = app.get_webview_window("main").ok_or("main window does not exist")?;
    let theme = state.theme.lock().expect("theme mutex poisoned").clone();
    let next = format!("/?desktop_theme={}", theme);
    let boot = desktop_boot_url(state, &next);
    window.eval(&format!("window.location.replace('{}');", boot.replace('\\', "\\\\").replace('\'', "\\'")))
        .map_err(|e| e.to_string())?;
    window.show().map_err(|e| e.to_string())?;
    Ok(())
}

fn backend_request(state: &RuntimeState, route: &str, body: Value) -> Result<Value, String> {
    let client = reqwest::blocking::Client::builder().timeout(Duration::from_secs(30)).build().map_err(|e| e.to_string())?;
    let response = client.post(format!("{}{}", state.backend_url(), route)).bearer_auth(state.token()).json(&body).send().map_err(|e| e.to_string())?;
    let status = response.status();
    let payload: Value = response.json().map_err(|e| e.to_string())?;
    if !status.is_success() {
        return Err(payload.get("error").and_then(Value::as_str).unwrap_or("backend request failed").to_string());
    }
    Ok(payload)
}

fn public_record(record: &Value) -> Value {
    let mut copy = record.clone();
    if let Some(object) = copy.as_object_mut() {
        object.remove("api_key");
        object.remove("secret_values");
        object.remove("encrypted_secrets");
        object.insert("has_secret".into(), Value::Bool(true));
    }
    copy
}

fn sync_profiles(state: &RuntimeState) -> Result<Value, String> {
    let profiles = state.providers.lock().expect("providers mutex poisoned").clone();
    backend_request(state, "/api/client/providers/sync", json!({"profiles": profiles}))
}

fn sync_usage_accounts(state: &RuntimeState) -> Result<Value, String> {
    let accounts = state.usage_accounts.lock().expect("usage accounts mutex poisoned").clone();
    backend_request(state, "/api/client/usage/accounts/sync", json!({"accounts": accounts}))
}

#[tauri::command]
fn set_theme(app: AppHandle, state: State<'_, RuntimeState>, theme: String) -> Result<bool, String> {
    let normalized = if theme == "light" { "light" } else { "dark" }.to_string();
    *state.theme.lock().expect("theme mutex poisoned") = normalized.clone();
    fs::create_dir_all(&state.app_data_dir).map_err(|e| e.to_string())?;
    fs::write(state.app_data_dir.join("ui-state.json"), json!({"theme": normalized}).to_string()).map_err(|e| e.to_string())?;
    for label in ["main", "pipeline", "expert-flow", "orch-canvas"] {
        if let Some(window) = app.get_webview_window(label) {
            let script = format!("document.documentElement.setAttribute('data-theme', '{}'); localStorage.setItem('mewcode-theme','{}');", normalized, normalized);
            let _ = window.eval(&script);
        }
    }
    Ok(true)
}

#[tauri::command]
fn select_directory() -> Result<String, String> {
    if cfg!(windows) {
        let script = "Add-Type -AssemblyName System.Windows.Forms; $d=New-Object System.Windows.Forms.FolderBrowserDialog; if($d.ShowDialog() -eq 'OK'){ [Console]::Write($d.SelectedPath) }";
        let output = Command::new("powershell").args(["-NoProfile", "-STA", "-Command", script]).output().map_err(|e| e.to_string())?;
        return Ok(String::from_utf8_lossy(&output.stdout).trim().to_string());
    }
    let command = if cfg!(target_os = "macos") { "osascript" } else { "zenity" };
    let args: Vec<&str> = if cfg!(target_os = "macos") { vec!["-e", "POSIX path of (choose folder)"] } else { vec!["--file-selection", "--directory"] };
    let output = Command::new(command).args(args).output().map_err(|e| e.to_string())?;
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

#[tauri::command]
fn open_directory(directory_path: String) -> Result<bool, String> {
    let path = PathBuf::from(directory_path);
    if !path.is_absolute() || !path.is_dir() { return Err("directory must be an existing absolute path".into()); }
    if cfg!(windows) { Command::new("explorer").arg(path).spawn().map_err(|e| e.to_string())?; }
    else if cfg!(target_os = "macos") { Command::new("open").arg(path).spawn().map_err(|e| e.to_string())?; }
    else { Command::new("xdg-open").arg(path).spawn().map_err(|e| e.to_string())?; }
    Ok(true)
}

#[tauri::command]
fn open_external(url: String) -> Result<bool, String> {
    let parsed = url::Url::parse(&url).map_err(|_| "only HTTP(S) URLs are allowed".to_string())?;
    if !matches!(parsed.scheme(), "http" | "https") { return Err("only HTTP(S) URLs are allowed".into()); }
    if cfg!(windows) { Command::new("cmd").args(["/C", "start", "", &url]).spawn().map_err(|e| e.to_string())?; }
    else if cfg!(target_os = "macos") { Command::new("open").arg(url).spawn().map_err(|e| e.to_string())?; }
    else { Command::new("xdg-open").arg(url).spawn().map_err(|e| e.to_string())?; }
    Ok(true)
}

#[tauri::command]
fn provider_presets() -> Value {
    json!({
        "typesafe": {"label":"desktop:example_provider_7（评审）","protocol":"typesafe","base_url":"https://api.typesafe.ai/v1","default_model":"jev-latest"},
        "openai": {"label":"OpenAI","protocol":"openai","base_url":"https://api.openai.com/v1"},
        "xai": {"label":"desktop:example_provider_2","protocol":"openai","base_url":"https://api.x.ai/v1","default_model":"grok-4.6"},
        "opencode-go": {"label":"OpenCode Go","provider_type":"opencode-go","protocol":"openai-compat","base_url":"https://opencode.ai/zen/go","default_model":"deepseek-v4-flash"},
        "anthropic": {"label":"Anthropic","protocol":"anthropic","base_url":"https://api.anthropic.com"},
        "kimi": {"label":"Moonshot / Kimi","protocol":"openai-compat","base_url":"https://api.moonshot.cn/v1"},
        "deepseek": {"label":"DeepSeek","protocol":"openai-compat","base_url":"https://api.deepseek.com/v1","default_model":"deepseek-chat"},
        "openrouter": {"label":"OpenRouter","protocol":"openai-compat","base_url":"https://openrouter.ai/api/v1"},
        "custom": {"label":"OpenAI Compatible","protocol":"openai-compat","base_url":""}
    })
}

#[tauri::command]
fn provider_list(state: State<'_, RuntimeState>) -> Vec<Value> {
    state.providers.lock().expect("providers mutex poisoned").iter().map(public_record).collect()
}

#[tauri::command]
fn provider_save(state: State<'_, RuntimeState>, profile: Value) -> Result<Value, String> {
    write_guard(&state.providers_error, "providers.json")?;
    if !profile.is_object() { return Err("provider profile must be an object".into()); }
    let mut profile = profile;
    let key = profile.get("provider_key").and_then(Value::as_str).filter(|value| !value.is_empty()).map(str::to_owned).unwrap_or_else(|| format!("desktop:{}", generated_token().replace('-', "")));
    if let Some(object) = profile.as_object_mut() {
        object.insert("provider_key".into(), Value::String(key.clone()));
        // Empty string must not wipe a previously saved secret. Treat it as
        // "keep existing key" the same way a missing field does.
        let api_key_empty = object.get("api_key").and_then(Value::as_str).map(|value| value.trim().is_empty()).unwrap_or(true);
        if api_key_empty {
            object.remove("api_key");
            if let Some(existing) = state.providers.lock().expect("providers mutex poisoned").iter().find(|item| item.get("provider_key").and_then(Value::as_str) == Some(key.as_str())) {
                if let Some(secret) = existing.get("api_key") {
                    object.insert("api_key".into(), secret.clone());
                }
            }
        }
        let name = object.get("name").and_then(Value::as_str).unwrap_or("").trim();
        let base_url = object.get("base_url").and_then(Value::as_str).unwrap_or("").trim();
        let default_model = object.get("default_model").and_then(Value::as_str).unwrap_or("").trim();
        let api_key = object.get("api_key").and_then(Value::as_str).unwrap_or("").trim();
        if name.is_empty() || base_url.is_empty() || default_model.is_empty() || api_key.is_empty() {
            return Err("name, base_url, default_model and api_key are required".into());
        }
    }
    let next = {
        let mut profiles = state.providers.lock().expect("providers mutex poisoned");
        profiles.retain(|item| item.get("provider_key").and_then(Value::as_str) != Some(key.as_str()));
        profiles.push(profile.clone());
        profiles.clone()
    };
    write_json_vec(&state.app_data_dir.join("providers.json"), &next)?;
    clear_load_error(&state.providers_error);
    sync_profiles(&state)?;
    let mut result = public_record(&profile);
    if let Some(object) = result.as_object_mut() {
        object.insert("persisted".into(), Value::Bool(true));
        object.insert("storage".into(), Value::String("app-data".into()));
    }
    Ok(result)
}

#[tauri::command]
fn provider_remove(state: State<'_, RuntimeState>, provider_key: String) -> Result<bool, String> {
    write_guard(&state.providers_error, "providers.json")?;
    let next = {
        let mut profiles = state.providers.lock().expect("providers mutex poisoned");
        profiles.retain(|item| item.get("provider_key").and_then(Value::as_str) != Some(provider_key.as_str()));
        profiles.clone()
    };
    write_json_vec(&state.app_data_dir.join("providers.json"), &next)?;
    clear_load_error(&state.providers_error);
    sync_profiles(&state)?;
    Ok(true)
}

#[tauri::command]
fn provider_test(state: State<'_, RuntimeState>, profile: Value) -> Result<Value, String> {
    let mut profile = profile;
    if let Some(object) = profile.as_object_mut() {
        let key = object.get("provider_key").and_then(Value::as_str).unwrap_or("").to_string();
        let api_key_empty = object.get("api_key").and_then(Value::as_str).map(|value| value.trim().is_empty()).unwrap_or(true);
        if api_key_empty {
            object.remove("api_key");
            if let Some(existing) = state.providers.lock().expect("providers mutex poisoned").iter().find(|item| item.get("provider_key").and_then(Value::as_str) == Some(key.as_str())) {
                if let Some(secret) = existing.get("api_key") {
                    object.insert("api_key".into(), secret.clone());
                }
            }
        }
    }
    // Provider probes can take longer than ordinary backend calls (stream + tools).
    let client = reqwest::blocking::Client::builder().timeout(Duration::from_secs(90)).build().map_err(|e| e.to_string())?;
    let response = client.post(format!("{}{}", state.backend_url(), "/api/client/providers/test")).bearer_auth(state.token()).json(&json!({"profile": profile})).send().map_err(|e| e.to_string())?;
    let status = response.status();
    let body = response.text().map_err(|e| e.to_string())?;
    let payload: Value = serde_json::from_str(&body).map_err(|_| {
        if body.trim().is_empty() {
            "provider test returned an empty response".to_string()
        } else {
            format!("provider test returned non-JSON (HTTP {}): {}", status.as_u16(), body.chars().take(180).collect::<String>())
        }
    })?;
    if !status.is_success() {
        return Err(payload.get("error").and_then(Value::as_str).unwrap_or("backend request failed").to_string());
    }
    Ok(payload)
}

#[tauri::command]
fn usage_list_accounts(state: State<'_, RuntimeState>) -> Vec<Value> {
    state.usage_accounts.lock().expect("usage accounts mutex poisoned").iter().map(public_record).collect()
}

#[tauri::command]
fn usage_save_account(state: State<'_, RuntimeState>, account: Value) -> Result<Value, String> {
    write_guard(&state.usage_accounts_error, "usage-accounts.json")?;
    let mut account = account;
    let id = account.get("id").and_then(Value::as_str).filter(|value| !value.is_empty()).map(str::to_owned).unwrap_or_else(|| format!("usage:{}", generated_token().replace('-', "")));
    if let Some(object) = account.as_object_mut() {
        object.insert("id".into(), Value::String(id.clone()));
    }
    let next = {
        let mut accounts = state.usage_accounts.lock().expect("usage accounts mutex poisoned");
        accounts.retain(|item| item.get("id").and_then(Value::as_str) != Some(id.as_str()));
        accounts.push(account.clone());
        accounts.clone()
    };
    write_json_vec(&state.app_data_dir.join("usage-accounts.json"), &next)?;
    clear_load_error(&state.usage_accounts_error);
    sync_usage_accounts(&state)?;
    let mut result = public_record(&account);
    if let Some(object) = result.as_object_mut() {
        object.insert("persisted".into(), Value::Bool(true));
        object.insert("storage".into(), Value::String("app-data".into()));
    }
    Ok(result)
}

#[tauri::command]
fn usage_remove_account(state: State<'_, RuntimeState>, account_id: String) -> Result<bool, String> {
    write_guard(&state.usage_accounts_error, "usage-accounts.json")?;
    let next = {
        let mut accounts = state.usage_accounts.lock().expect("usage accounts mutex poisoned");
        accounts.retain(|item| item.get("id").and_then(Value::as_str) != Some(account_id.as_str()));
        accounts.clone()
    };
    write_json_vec(&state.app_data_dir.join("usage-accounts.json"), &next)?;
    clear_load_error(&state.usage_accounts_error);
    sync_usage_accounts(&state)?;
    Ok(true)
}

#[tauri::command]
fn usage_browser_command(_command: Value) -> Result<Value, String> {
    Err("the Tauri shell does not provide an isolated Usage browser window yet".into())
}

#[tauri::command]
fn diagnostics(state: State<'_, RuntimeState>) -> Vec<String> {
    state.diagnostics.lock().expect("diagnostics mutex poisoned").clone()
}

#[tauri::command]
fn open_pipeline(app: AppHandle, state: State<'_, RuntimeState>, options: Value) -> Result<bool, String> {
    let graph_id = options.get("graphId").and_then(Value::as_str).unwrap_or("");
    let mode = options.get("mode").and_then(Value::as_str).unwrap_or("pipeline");
    if !matches!(mode, "pipeline" | "expert-flow" | "orch-canvas" | "orchestration")
        || graph_id.len() > 512
        || graph_id.contains('/')
        || graph_id.contains('\\')
    {
        return Err("invalid pipeline window options".into());
    }
    let path = if mode == "expert-flow" && graph_id.is_empty() {
        "/expert-flow"
    } else if matches!(mode, "orch-canvas" | "orchestration") {
        "/orch-canvas"
    } else {
        "/pipeline"
    };
    let label = if path == "/expert-flow" {
        "expert-flow"
    } else if path == "/orch-canvas" {
        "orch-canvas"
    } else {
        "pipeline"
    };
    let theme = state.theme.lock().expect("theme mutex poisoned").clone();
    let mut next = format!("{}?desktop_theme={}", path, theme);
    if !graph_id.is_empty() {
        next.push_str(&format!("&id={}", url_encode(graph_id)));
    }
    let boot = desktop_boot_url(&state, &next);
    // Never reuse a stuck/blank webview — destroy and recreate with a clean boot URL.
    if let Some(existing) = app.get_webview_window(label) {
        let _ = existing.destroy();
        thread::sleep(Duration::from_millis(150));
    }
    let title = if path == "/expert-flow" {
        "MewCode Expert Flow"
    } else if path == "/orch-canvas" {
        "MewCode 专家团画布"
    } else {
        "MewCode Pipeline"
    };
    let mut builder = WebviewWindowBuilder::new(
        &app,
        label,
        WebviewUrl::External(boot.parse().map_err(|e: url::ParseError| e.to_string())?),
    )
    .title(title)
    .inner_size(1280.0, 840.0)
    .min_inner_size(880.0, 620.0);
    if std::env::var_os("MEWCODE_USER_DATA_DIR").is_some() {
        builder = builder.data_directory(state.app_data_dir.join("webview"));
    }
    #[cfg(not(target_os = "android"))]
    {
        builder = builder
            .decorations(true)
            .closable(true)
            .maximizable(true)
            .minimizable(true);
    }
    let window = builder
        .build()
        .map_err(|e| e.to_string())?;
    window.show().map_err(|e| e.to_string())?;
    window.set_focus().map_err(|e| e.to_string())?;
    // If the first navigation stalls on a blank document, force the boot URL once more.
    let recovery = boot.replace('\\', "\\\\").replace('\'', "\\'");
    let _ = window.eval(&format!(
        "setTimeout(function(){{var t=(document.body&&document.body.innerText||'').trim();if(!t||t.length<2){{window.location.replace('{recovery}');}}}},1200);"
    ));
    Ok(true)
}

fn url_encode(value: &str) -> String {
    value.bytes().map(|byte| if byte.is_ascii_alphanumeric() || b"-._~".contains(&byte) { (byte as char).to_string() } else { format!("%{:02X}", byte) }).collect()
}

#[tauri::command]
fn close_pipeline(app: AppHandle, window: WebviewWindow) -> Result<bool, String> {
    let label = window.label().to_string();
    // destroy() is more reliable than close() for stuck WebView2 windows on Windows.
    if matches!(label.as_str(), "pipeline" | "expert-flow" | "orch-canvas") {
        window.destroy().map_err(|e| e.to_string())?;
        return Ok(true);
    }
    // Main window can dismiss any stuck secondary canvas if the child page lost IPC.
    if label == "main" {
        let mut closed = false;
        for name in ["pipeline", "expert-flow", "orch-canvas"] {
            if let Some(child) = app.get_webview_window(name) {
                let _ = child.destroy();
                closed = true;
            }
        }
        return Ok(closed);
    }
    Err("the current window is not a pipeline workspace".into())
}

#[tauri::command]
fn restart_backend(app: AppHandle, state: State<'_, RuntimeState>) -> Result<bool, String> {
    start_backend(&app, &state)?;
    sync_profiles(&state)?;
    sync_usage_accounts(&state)?;
    reload_main(&app, &state)?;
    Ok(true)
}

#[cfg(target_os = "android")]
#[tauri::command]
fn mobile_backend_url() -> Result<String, String> {
    let value = option_env!("MEWCODE_MOBILE_BACKEND_URL").unwrap_or("").trim();
    if value.is_empty() {
        return Err("MEWCODE_MOBILE_BACKEND_URL was not set at build time".into());
    }
    let url = url::Url::parse(value).map_err(|_| "invalid MEWCODE_MOBILE_BACKEND_URL".to_string())?;
    if url.scheme() != "https" {
        return Err("the Android backend URL must use HTTPS".into());
    }
    Ok(value.trim_end_matches('/').to_string())
}

#[cfg(target_os = "android")]
const MOBILE_PAIRING_FILE: &str = "mobile-pairing.json";
#[cfg(target_os = "android")]
const MAX_MOBILE_REMOTE_RESPONSE_BYTES: usize = 8 * 1024 * 1024;
#[cfg(target_os = "android")]
const MOBILE_PROBE_PORTS: [u16; 2] = [443, 8765];
#[cfg(target_os = "android")]
const MOBILE_PROBE_TIMEOUT: Duration = Duration::from_secs(8);
#[cfg(target_os = "android")]
const MOBILE_DISCOVER_TIMEOUT: Duration = Duration::from_secs(5);
#[cfg(target_os = "android")]
const MAX_MOBILE_PROBE_ATTEMPTS: usize = 16;
#[cfg(target_os = "android")]
const MAX_MOBILE_PROBE_NODES: usize = 64;

#[cfg(target_os = "android")]
#[derive(Clone, Deserialize, Serialize)]
struct MobilePairingPeer {
    endpoint: String,
    token: String,
    #[serde(default)]
    label: String,
    #[serde(default)]
    node_id: String,
}

#[cfg(target_os = "android")]
#[derive(Clone, Deserialize, Serialize)]
struct MobilePairingFile {
    device_id: String,
    #[serde(default)]
    active_endpoint: Option<String>,
    #[serde(default)]
    peers: Vec<MobilePairingPeer>,
}

#[cfg(target_os = "android")]
#[derive(Clone, Serialize)]
struct MobilePairingStatus {
    paired: bool,
    endpoint: Option<String>,
    device_id: Option<String>,
    peers: Vec<MobilePairingPeerPublic>,
}

#[cfg(target_os = "android")]
#[derive(Clone, Serialize)]
struct MobilePairingPeerPublic {
    endpoint: String,
    label: String,
    node_id: String,
}

#[cfg(target_os = "android")]
struct MobileRuntimeState {
    app_data_dir: PathBuf,
}

#[cfg(target_os = "android")]
fn mobile_pairing_path(state: &MobileRuntimeState) -> PathBuf {
    state.app_data_dir.join(MOBILE_PAIRING_FILE)
}

#[cfg(target_os = "android")]
fn is_mobile_device_id(value: &str) -> bool {
    let mut chars = value.chars();
    matches!(chars.next(), Some(first) if first.is_ascii_alphanumeric())
        && value.len() <= 128
        && chars.all(|item| item.is_ascii_alphanumeric() || matches!(item, '.' | '_' | ':' | '-'))
}

#[cfg(target_os = "android")]
fn is_tailnet_ip_str(host: &str) -> bool {
    if let Ok(address) = host.parse::<std::net::Ipv4Addr>() {
        let octets = address.octets();
        return octets[0] == 100 && (64..=127).contains(&octets[1]);
    }
    if let Ok(address) = host.parse::<std::net::Ipv6Addr>() {
        let segments = address.segments();
        return segments[0] == 0xfd7a && segments[1] == 0x115c && segments[2] == 0xa1e0;
    }
    false
}

#[cfg(target_os = "android")]
fn is_tailnet_host(url: &url::Url) -> bool {
    match url.host() {
        Some(url::Host::Domain(host)) => host == "ts.net" || host.ends_with(".ts.net"),
        Some(url::Host::Ipv4(address)) => {
            let octets = address.octets();
            octets[0] == 100 && (64..=127).contains(&octets[1])
        }
        Some(url::Host::Ipv6(address)) => {
            let segments = address.segments();
            segments[0] == 0xfd7a && segments[1] == 0x115c && segments[2] == 0xa1e0
        }
        None => false,
    }
}

#[cfg(target_os = "android")]
fn normalize_mobile_endpoint(value: &str) -> Result<String, String> {
    let mut parsed = url::Url::parse(value.trim()).map_err(|_| "invalid pairing endpoint".to_string())?;
    let https = parsed.scheme() == "https";
    let http = parsed.scheme() == "http";
    if !https && !http {
        return Err("pairing endpoint must use HTTP or HTTPS".into());
    }
    if !is_tailnet_host(&parsed) {
        return Err("pairing endpoint must be a Tailnet address".into());
    }
    if http && !matches!(parsed.host(), Some(url::Host::Ipv4(_)) | Some(url::Host::Ipv6(_))) {
        return Err("HTTP pairing endpoints must use a Tailscale IP".into());
    }
    if !parsed.username().is_empty() || parsed.password().is_some() || parsed.query().is_some() || parsed.fragment().is_some() {
        return Err("pairing endpoint contains unsupported URL fields".into());
    }
    if parsed.path() != "/" && !parsed.path().is_empty() {
        return Err("pairing endpoint cannot include a path".into());
    }
    parsed.set_path("");
    Ok(parsed.to_string().trim_end_matches('/').to_string())
}

#[cfg(target_os = "android")]
fn ensure_device_id(file: &mut MobilePairingFile) {
    if !is_mobile_device_id(&file.device_id) {
        file.device_id = format!(
            "android-{:x}-{:x}",
            SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_nanos(),
            std::process::id()
        );
    }
}

#[cfg(target_os = "android")]
fn read_mobile_pairing(state: &MobileRuntimeState) -> Result<MobilePairingFile, String> {
    let path = mobile_pairing_path(state);
    let text = match fs::read_to_string(path) {
        Ok(value) => value,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            let mut file = MobilePairingFile { device_id: String::new(), active_endpoint: None, peers: Vec::new() };
            ensure_device_id(&mut file);
            return Ok(file);
        }
        Err(_) => return Err("unable to read mobile pairing configuration".into()),
    };
    if let Ok(legacy) = serde_json::from_str::<Value>(&text) {
        if legacy.get("peers").is_none() {
            if let (Some(endpoint), Some(device_id), Some(token)) = (
                legacy.get("endpoint").and_then(Value::as_str),
                legacy.get("device_id").and_then(Value::as_str),
                legacy.get("token").and_then(Value::as_str),
            ) {
                let endpoint = normalize_mobile_endpoint(endpoint)?;
                if !is_mobile_device_id(device_id) || !(32..=256).contains(&token.len()) {
                    return Err("invalid mobile pairing configuration".into());
                }
                return Ok(MobilePairingFile {
                    device_id: device_id.to_string(),
                    active_endpoint: Some(endpoint.clone()),
                    peers: vec![MobilePairingPeer { endpoint, token: token.to_string(), label: String::new(), node_id: String::new() }],
                });
            }
        }
    }
    let mut file: MobilePairingFile = serde_json::from_str(&text).map_err(|_| "invalid mobile pairing configuration".to_string())?;
    ensure_device_id(&mut file);
    file.peers.retain(|peer| normalize_mobile_endpoint(&peer.endpoint).ok().as_deref() == Some(peer.endpoint.as_str()) && (32..=256).contains(&peer.token.len()));
    if let Some(active) = file.active_endpoint.clone() {
        if file.peers.iter().all(|peer| peer.endpoint != active) {
            file.active_endpoint = file.peers.first().map(|peer| peer.endpoint.clone());
        }
    }
    Ok(file)
}

#[cfg(target_os = "android")]
fn write_mobile_pairing(state: &MobileRuntimeState, file: &MobilePairingFile) -> Result<(), String> {
    fs::create_dir_all(&state.app_data_dir).map_err(|_| "unable to create mobile pairing storage".to_string())?;
    let path = mobile_pairing_path(state);
    let temporary = state.app_data_dir.join(format!(".{}-{}-{}.tmp", MOBILE_PAIRING_FILE, std::process::id(), SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_nanos()));
    let serialized = serde_json::to_vec(file).map_err(|_| "unable to save mobile pairing configuration".to_string())?;
    fs::write(&temporary, serialized).map_err(|_| "unable to save mobile pairing configuration".to_string())?;
    fs::rename(&temporary, &path).map_err(|_| "unable to save mobile pairing configuration".to_string())
}

#[cfg(target_os = "android")]
fn mobile_pairing_status_for(file: &MobilePairingFile) -> MobilePairingStatus {
    let active = file.active_endpoint.clone().and_then(|endpoint| file.peers.iter().find(|peer| peer.endpoint == endpoint).cloned());
    MobilePairingStatus {
        paired: active.is_some(),
        endpoint: active.as_ref().map(|peer| peer.endpoint.clone()),
        device_id: Some(file.device_id.clone()),
        peers: file.peers.iter().map(|peer| MobilePairingPeerPublic {
            endpoint: peer.endpoint.clone(),
            label: if peer.label.is_empty() { peer.endpoint.clone() } else { peer.label.clone() },
            node_id: peer.node_id.clone(),
        }).collect(),
    }
}

#[cfg(target_os = "android")]
fn upsert_peer(file: &mut MobilePairingFile, peer: MobilePairingPeer) {
    if let Some(existing) = file.peers.iter_mut().find(|item| item.endpoint == peer.endpoint) {
        *existing = peer.clone();
    } else {
        file.peers.push(peer.clone());
    }
    file.active_endpoint = Some(peer.endpoint);
}

#[cfg(target_os = "android")]
fn mobile_remote_path_is_allowed(path: &str, method: &str) -> bool {
    if path.len() > 600 || !path.starts_with("/api/remote/") || path.contains("..") || path.contains('#') {
        return false;
    }
    let base = path.split('?').next().unwrap_or("");
    match (method, base) {
        ("GET", "/api/remote/info" | "/api/remote/history" | "/api/remote/history/item" | "/api/remote/live") => true,
        ("POST", "/api/remote/conversations" | "/api/remote/conversations/activate" | "/api/remote/conversations/message" | "/api/remote/conversations/interrupt" | "/api/remote/conversations/resume" | "/api/remote/prompts") => true,
        ("GET", candidate) => candidate
            .strip_prefix("/api/remote/operations/")
            .is_some_and(|value| (8..=80).contains(&value.len()) && value.bytes().all(|item| item.is_ascii_alphanumeric() || item == b'-' || item == b'_')),
        _ => false,
    }
}

#[cfg(target_os = "android")]
fn mobile_remote_url(endpoint: &str, path: &str) -> Result<url::Url, String> {
    let url = url::Url::parse(&(endpoint.to_string() + path)).map_err(|_| "invalid remote API request".to_string())?;
    if normalize_mobile_endpoint(endpoint)? != endpoint || !is_tailnet_host(&url) {
        return Err("invalid remote API request".into());
    }
    Ok(url)
}

#[cfg(target_os = "android")]
fn mobile_http_client() -> Result<reqwest::blocking::Client, String> {
    reqwest::blocking::Client::builder()
        .http1_only()
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(Duration::from_secs(8))
        .timeout(Duration::from_secs(15))
        .build()
        .map_err(|_| "unable to initialize remote connection".to_string())
}

#[cfg(target_os = "android")]
fn pairing_network_error(error: reqwest::Error) -> String {
    if error.is_timeout() {
        return "连接超时：请确认手机和电脑都已打开 Tailscale，且电脑端 MewCode 保持运行。".into();
    }
    if error.is_connect() {
        return "连不上电脑端。请确认电脑已登录 Tailscale，且 MewCode 保持运行。".into();
    }
    let detail = error.to_string().to_ascii_lowercase();
    if detail.contains("dns") || detail.contains("resolve") || detail.contains("name or service") {
        return "无法解析电脑地址。请在手机上打开并连接 Tailscale。".into();
    }
    if detail.contains("certificate") || detail.contains("tls") || detail.contains("ssl") {
        return "电脑端 HTTPS 证书无效。请确认 Tailscale HTTPS 已开启后重试。".into();
    }
    "无法连接电脑端。请确认手机 Tailscale 已连接，电脑端保持打开。".into()
}

#[cfg(target_os = "android")]
fn fetch_tailscale_status() -> Result<Value, String> {
    let client = reqwest::blocking::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(2))
        .build()
        .map_err(|_| "Tailscale 不可用".to_string())?;
    let urls = ["http://100.100.100.100/status?json=1", "http://100.100.100.100/localapi/v0/status"];
    let mut last = "Tailscale 不可用".to_string();
    for url in urls {
        match client.get(url).header("Accept", "application/json").send() {
            Ok(response) if response.status().is_success() => {
                let value = response.json::<Value>().map_err(|_| "Tailscale 状态无效".to_string())?;
                if value.is_object() {
                    return Ok(value);
                }
                last = "Tailscale 状态无效".into();
            }
            Ok(_) => last = "Tailscale 未连接".into(),
            Err(error) if error.is_connect() => last = "Tailscale 不可用".into(),
            Err(_) => last = "Tailscale 不可用".into(),
        }
    }
    Err(last)
}

#[cfg(target_os = "android")]
fn probe_candidates(node: &Value) -> Vec<String> {
    let mut hosts: Vec<(String, bool)> = Vec::new();
    if let Some(dns) = node.get("DNSName").and_then(Value::as_str) {
        let host = dns.trim_end_matches('.').to_ascii_lowercase();
        if host == "ts.net" || host.ends_with(".ts.net") {
            hosts.push((host, false));
        }
    }
    if let Some(addresses) = node.get("TailscaleIPs").and_then(Value::as_array) {
        for address in addresses {
            let host = address.as_str().unwrap_or("").trim();
            if is_tailnet_ip_str(host) && hosts.iter().all(|(existing, _)| existing != host) {
                hosts.push((host.to_string(), true));
            }
        }
    }
    let mut urls = Vec::new();
    for (host, is_ip) in hosts {
        urls.extend(manual_host_candidates(&host, is_ip, None));
    }
    urls
}

#[cfg(target_os = "android")]
fn probe_endpoint(client: &reqwest::blocking::Client, base: &str) -> Option<String> {
    for path in ["/api/pairing/hello", "/api/remote/info"] {
        let url = format!("{base}{path}");
        match client.get(&url).header("Accept", "application/json").send() {
            Ok(response) => {
                let code = response.status().as_u16();
                if code == 401 || code == 403 {
                    return Some("auth_required".into());
                }
                if response.status().is_success() {
                    if path == "/api/pairing/hello" {
                        if let Ok(value) = response.json::<Value>() {
                            if value.get("pairing").and_then(Value::as_bool) == Some(true) {
                                return Some("reachable".into());
                            }
                        }
                    } else {
                        return Some("reachable".into());
                    }
                }
            }
            Err(error) if error.is_timeout() => return Some("timeout".into()),
            Err(_) => {}
        }
    }
    None
}

#[cfg(target_os = "android")]
fn parse_manual_tailscale_host(raw: &str) -> Result<(String, bool, Option<u16>), String> {
    let trimmed = raw.trim();
    if trimmed.is_empty() || trimmed.len() > 256 {
        return Err("主机地址无效".into());
    }
    if trimmed.contains('/') || trimmed.contains('?') || trimmed.contains('#') || trimmed.contains('@') {
        return Err("主机地址无效".into());
    }
    let candidate = if trimmed.contains("://") {
        trimmed.to_string()
    } else {
        format!("https://{trimmed}")
    };
    let parsed = url::Url::parse(&candidate).map_err(|_| "主机地址无效".to_string())?;
    let https = parsed.scheme() == "https";
    let http = parsed.scheme() == "http";
    if !https && !http {
        return Err("主机地址无效".into());
    }
    if !is_tailnet_host(&parsed) {
        return Err("不是 Tailscale 地址".into());
    }
    if http && !matches!(parsed.host(), Some(url::Host::Ipv4(_)) | Some(url::Host::Ipv6(_))) {
        return Err("不是 Tailscale 地址".into());
    }
    if !parsed.username().is_empty() || parsed.password().is_some() {
        return Err("主机地址无效".into());
    }
    let host = match parsed.host() {
        Some(url::Host::Domain(value)) => value.trim_end_matches('.').to_ascii_lowercase(),
        Some(url::Host::Ipv4(address)) => address.to_string(),
        Some(url::Host::Ipv6(address)) => address.to_string(),
        None => return Err("主机地址无效".into()),
    };
    let is_ip = matches!(parsed.host(), Some(url::Host::Ipv4(_)) | Some(url::Host::Ipv6(_)));
    let port = parsed.port();
    Ok((host, is_ip, port))
}

#[cfg(target_os = "android")]
fn manual_host_candidates(host: &str, is_ip: bool, preferred_port: Option<u16>) -> Vec<String> {
    let wrapped = if host.contains(':') && !host.starts_with('[') {
        format!("[{host}]")
    } else {
        host.to_string()
    };
    let mut ports: Vec<u16> = Vec::new();
    if let Some(port) = preferred_port {
        if port != 0 {
            ports.push(port);
        }
    }
    for port in MOBILE_PROBE_PORTS {
        if !ports.contains(&port) {
            ports.push(port);
        }
    }
    let mut urls = Vec::new();
    for port in ports {
        if port == 443 {
            // Serve publishes MagicDNS HTTPS. TLS against a 100.x IP hangs until
            // timeout because nothing speaks HTTPS on that address.
            if !is_ip {
                urls.push(format!("https://{wrapped}"));
            }
        } else if is_ip {
            // Desktop MewCode is HTTP on 8765; TLS against that port hangs until
            // timeout. Tailscale Serve is HTTPS on 443, so only probe HTTP here.
            urls.push(format!("http://{wrapped}:{port}"));
        }
    }
    urls
}

#[cfg(target_os = "android")]
fn probe_manual_host_blocking(raw_host: String) -> Result<Value, String> {
    let (host, is_ip, preferred_port) = parse_manual_tailscale_host(&raw_host)?;
    let client = reqwest::blocking::Client::builder()
        .http1_only()
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(MOBILE_PROBE_TIMEOUT)
        .timeout(MOBILE_PROBE_TIMEOUT)
        .build()
        .map_err(|_| "无法探测电脑".to_string())?;
    let mut status = "unreachable".to_string();
    let mut url = String::new();
    let mut attempts = 0usize;
    for candidate in manual_host_candidates(&host, is_ip, preferred_port) {
        attempts += 1;
        if attempts > MAX_MOBILE_PROBE_ATTEMPTS {
            break;
        }
        match probe_endpoint(&client, &candidate) {
            Some(found) if found == "reachable" || found == "auth_required" => {
                status = found;
                url = candidate;
                break;
            }
            Some(found) => status = found,
            None => {}
        }
    }
    let selectable = status == "reachable" || status == "auth_required";
    let error = if selectable {
        String::new()
    } else if is_ip {
        "请改用电脑的 MagicDNS 域名（以 .ts.net 结尾）。MewCode 只通过 Tailscale HTTPS 对外，100.x 地址连不上本机端口。".into()
    } else if status == "timeout" {
        "探测超时：请确认手机和电脑都已打开 Tailscale，电脑端 MewCode 保持运行，并填写设置里显示的 *.ts.net 地址。".into()
    } else {
        "未找到 MewCode 服务：请确认电脑地址正确且 MewCode 正在运行。".into()
    };
    Ok(json!({
        "id": host,
        "label": host,
        "hostname": host,
        "url": url,
        "status": status,
        "selectable": selectable,
        "error": error,
    }))
}

#[cfg(target_os = "android")]
fn discover_nodes_blocking() -> Result<Value, String> {
    // A phone whose Tailscale daemon cannot be reached is an expected state,
    // not a command failure. Degrade it into the structured payload the mobile
    // UI already knows how to render ("Tailscale 不可用") instead of aborting
    // the whole refresh, which used to hide the real cause behind a generic
    // fallback message.
    let payload = match fetch_tailscale_status() {
        Ok(value) => value,
        Err(error) => {
            return Ok(json!({
                "available": false,
                "connected": false,
                "error": error,
                "self_node_id": "",
                "self_hostname": "",
                "nodes": []
            }));
        }
    };
    let backend = payload.get("BackendState").and_then(Value::as_str).unwrap_or("");
    let connected = backend.eq_ignore_ascii_case("Running");
    let self_node = payload.get("Self").cloned().unwrap_or_else(|| json!({}));
    let self_id = self_node.get("ID").and_then(Value::as_str).unwrap_or("").to_string();
    let self_host = self_node.get("HostName").and_then(Value::as_str).unwrap_or("").to_string();
    if !connected {
        return Ok(json!({
            "available": true,
            "connected": false,
            "error": "Tailscale 未连接",
            "self_node_id": self_id,
            "self_hostname": self_host,
            "nodes": []
        }));
    }
    let client = reqwest::blocking::Client::builder()
        .http1_only()
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(MOBILE_DISCOVER_TIMEOUT)
        .timeout(MOBILE_DISCOVER_TIMEOUT)
        .build()
        .map_err(|_| "Tailscale 不可用".to_string())?;
    let mut selectable = Vec::new();
    let mut others = Vec::new();
    let mut attempts = 0usize;
    let peers = payload.get("Peer").and_then(Value::as_object).cloned().unwrap_or_default();
    for (key, node) in peers {
        if !node.is_object() {
            continue;
        }
        let node_id = node.get("ID").and_then(Value::as_str).filter(|value| !value.is_empty()).unwrap_or(key.as_str()).to_string();
        let hostname = node.get("HostName").and_then(Value::as_str).unwrap_or(&node_id).to_string();
        let candidates = probe_candidates(&node);
        if candidates.is_empty() {
            continue;
        }
        if selectable.len() + others.len() >= MAX_MOBILE_PROBE_NODES {
            break;
        }
        if node.get("Online").and_then(Value::as_bool) == Some(false) {
            others.push(json!({"id": node_id, "hostname": hostname, "url": "", "status": "offline", "selectable": false}));
            continue;
        }
        let mut status = "timeout".to_string();
        let mut url = String::new();
        for candidate in candidates {
            attempts += 1;
            if attempts > MAX_MOBILE_PROBE_ATTEMPTS {
                break;
            }
            if let Some(found) = probe_endpoint(&client, &candidate) {
                if found == "reachable" || found == "auth_required" {
                    status = found;
                    url = candidate;
                    break;
                }
                status = found;
            }
        }
        let selectable_flag = status == "reachable" || status == "auth_required";
        let item = json!({"id": node_id, "hostname": hostname, "url": url, "status": status, "selectable": selectable_flag});
        if selectable_flag { selectable.push(item); } else { others.push(item); }
        if attempts > MAX_MOBILE_PROBE_ATTEMPTS {
            break;
        }
    }
    selectable.extend(others);
    Ok(json!({
        "available": true,
        "connected": true,
        "error": "",
        "self_node_id": self_id,
        "self_hostname": self_host,
        "nodes": selectable
    }))
}

#[cfg(target_os = "android")]
fn save_approved_token(state: &MobileRuntimeState, endpoint: &str, token: &str, node_id: &str, label: &str) -> Result<MobilePairingStatus, String> {
    if !(32..=256).contains(&token.len()) || !token.bytes().all(|item| item.is_ascii_alphanumeric() || item == b'-' || item == b'_') {
        return Err("invalid pairing response".into());
    }
    let endpoint = normalize_mobile_endpoint(endpoint)?;
    let mut file = read_mobile_pairing(state)?;
    upsert_peer(&mut file, MobilePairingPeer {
        endpoint,
        token: token.to_string(),
        label: label.to_string(),
        node_id: node_id.to_string(),
    });
    write_mobile_pairing(state, &file)?;
    Ok(mobile_pairing_status_for(&file))
}

#[cfg(target_os = "android")]
fn request_pairing_blocking(app_data_dir: PathBuf, endpoint: String, node_id: String, label: String) -> Result<Value, String> {
    let endpoint = normalize_mobile_endpoint(&endpoint)?;
    let state = MobileRuntimeState { app_data_dir };
    let file = read_mobile_pairing(&state)?;
    let self_node_id = fetch_tailscale_status()
        .ok()
        .and_then(|payload| payload.get("Self").and_then(|node| node.get("ID")).and_then(Value::as_str).map(|value| value.to_string()))
        .filter(|value| is_mobile_device_id(value))
        .unwrap_or_else(|| file.device_id.clone());
    let _ = node_id;
    let response = mobile_http_client()?
        .post(endpoint.clone() + "/api/pairing/requests")
        .json(&json!({"device_id": file.device_id, "node_id": self_node_id, "label": label}))
        .send()
        .map_err(pairing_network_error)?;
    if !response.status().is_success() {
        return Err(format!("pairing was rejected (HTTP {})", response.status().as_u16()));
    }
    let mut payload: Value = response.json().map_err(|_| "invalid pairing response".to_string())?;
    if payload.get("status").and_then(Value::as_str) == Some("approved") {
        let token = payload.get("token").and_then(Value::as_str).ok_or("pairing response has no token")?;
        let status = save_approved_token(&state, &endpoint, token, payload.get("node_id").and_then(Value::as_str).unwrap_or(""), &label)?;
        payload["paired"] = json!(status.paired);
        payload["endpoint"] = json!(status.endpoint);
        payload["peers"] = json!(status.peers);
    }
    Ok(payload)
}

#[cfg(target_os = "android")]
fn poll_pairing_blocking(app_data_dir: PathBuf, endpoint: String, request_id: String) -> Result<Value, String> {
    let endpoint = normalize_mobile_endpoint(&endpoint)?;
    if !(8..=128).contains(&request_id.len()) || !request_id.bytes().all(|item| item.is_ascii_alphanumeric() || item == b'-' || item == b'_') {
        return Err("invalid pairing request".into());
    }
    let state = MobileRuntimeState { app_data_dir };
    let response = mobile_http_client()?
        .get(format!("{endpoint}/api/pairing/requests/{request_id}"))
        .send()
        .map_err(pairing_network_error)?;
    if !response.status().is_success() {
        return Err(format!("pairing was rejected (HTTP {})", response.status().as_u16()));
    }
    let mut payload: Value = response.json().map_err(|_| "invalid pairing response".to_string())?;
    if payload.get("status").and_then(Value::as_str) == Some("approved") {
        let token = payload.get("token").and_then(Value::as_str).ok_or("pairing response has no token")?;
        let file = read_mobile_pairing(&state)?;
        let status = save_approved_token(
            &state,
            &endpoint,
            token,
            payload.get("node_id").and_then(Value::as_str).unwrap_or(""),
            file.peers.iter().find(|peer| peer.endpoint == endpoint).map(|peer| peer.label.as_str()).unwrap_or(""),
        )?;
        payload["paired"] = json!(status.paired);
        payload["endpoint"] = json!(status.endpoint);
        payload["peers"] = json!(status.peers);
    }
    Ok(payload)
}

#[cfg(target_os = "android")]
#[tauri::command]
fn mobile_pairing_state(state: State<'_, MobileRuntimeState>) -> Result<MobilePairingStatus, String> {
    Ok(mobile_pairing_status_for(&read_mobile_pairing(&state)?))
}

#[cfg(target_os = "android")]
#[tauri::command]
async fn mobile_discover_nodes() -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(discover_nodes_blocking)
        .await
        .map_err(|_| "discovery task failed".to_string())?
}

#[cfg(target_os = "android")]
#[tauri::command]
async fn mobile_probe_host(host: String) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || probe_manual_host_blocking(host))
        .await
        .map_err(|_| "host probe task failed".to_string())?
}

#[cfg(target_os = "android")]
#[tauri::command]
async fn mobile_request_pairing(state: State<'_, MobileRuntimeState>, endpoint: String, node_id: String, label: String) -> Result<Value, String> {
    let app_data_dir = state.app_data_dir.clone();
    tauri::async_runtime::spawn_blocking(move || request_pairing_blocking(app_data_dir, endpoint, node_id, label))
        .await
        .map_err(|_| "pairing task failed".to_string())?
}

#[cfg(target_os = "android")]
#[tauri::command]
async fn mobile_poll_pairing(state: State<'_, MobileRuntimeState>, endpoint: String, request_id: String) -> Result<Value, String> {
    let app_data_dir = state.app_data_dir.clone();
    tauri::async_runtime::spawn_blocking(move || poll_pairing_blocking(app_data_dir, endpoint, request_id))
        .await
        .map_err(|_| "pairing task failed".to_string())?
}

#[cfg(target_os = "android")]
#[tauri::command]
fn mobile_set_active_peer(state: State<'_, MobileRuntimeState>, endpoint: String) -> Result<MobilePairingStatus, String> {
    let endpoint = normalize_mobile_endpoint(&endpoint)?;
    let mut file = read_mobile_pairing(&state)?;
    if file.peers.iter().all(|peer| peer.endpoint != endpoint) {
        return Err("that computer is not paired".into());
    }
    file.active_endpoint = Some(endpoint);
    write_mobile_pairing(&state, &file)?;
    Ok(mobile_pairing_status_for(&file))
}

#[cfg(target_os = "android")]
#[tauri::command]
fn mobile_clear_pairing(state: State<'_, MobileRuntimeState>, endpoint: Option<String>) -> Result<bool, String> {
    let mut file = read_mobile_pairing(&state)?;
    if file.peers.is_empty() {
        return Ok(false);
    }
    if let Some(value) = endpoint.filter(|item| !item.is_empty()) {
        let endpoint = normalize_mobile_endpoint(&value)?;
        let before = file.peers.len();
        file.peers.retain(|peer| peer.endpoint != endpoint);
        if file.active_endpoint.as_deref() == Some(endpoint.as_str()) {
            file.active_endpoint = file.peers.first().map(|peer| peer.endpoint.clone());
        }
        write_mobile_pairing(&state, &file)?;
        return Ok(before != file.peers.len());
    }
    file.peers.clear();
    file.active_endpoint = None;
    write_mobile_pairing(&state, &file)?;
    Ok(true)
}

#[cfg(target_os = "android")]
fn remote_request_blocking(app_data_dir: PathBuf, path: String, method: String, payload: Option<Value>) -> Result<Value, String> {
    let method = method.to_ascii_uppercase();
    if !mobile_remote_path_is_allowed(&path, &method) {
        return Err("remote API request is not allowed".into());
    }
    let state = MobileRuntimeState { app_data_dir };
    let file = read_mobile_pairing(&state)?;
    let peer = file.active_endpoint.as_ref()
        .and_then(|endpoint| file.peers.iter().find(|item| &item.endpoint == endpoint))
        .ok_or("MewCode has not been paired yet")?;
    let url = mobile_remote_url(&peer.endpoint, &path)?;
    let client = mobile_http_client()?;
    let request = match method.as_str() {
        "GET" => client.get(url),
        "POST" => client.post(url),
        _ => return Err("remote API request is not allowed".into()),
    }
    .bearer_auth(&peer.token);
    let response = if let Some(body) = payload { request.json(&body).send() } else { request.send() }
        .map_err(pairing_network_error)?;
    if response.content_length().is_some_and(|length| length > MAX_MOBILE_REMOTE_RESPONSE_BYTES as u64) {
        return Err("remote response is too large".into());
    }
    let status = response.status();
    let bytes = response.bytes().map_err(|_| "unable to read remote response".to_string())?;
    if bytes.len() > MAX_MOBILE_REMOTE_RESPONSE_BYTES {
        return Err("remote response is too large".into());
    }
    if !status.is_success() {
        if let Ok(value) = serde_json::from_slice::<Value>(&bytes) {
            if let Some(error) = value.get("error").and_then(Value::as_str).filter(|item| !item.is_empty()) {
                return Err(error.to_string());
            }
        }
        return Err(format!("remote request failed (HTTP {})", status.as_u16()));
    }
    serde_json::from_slice(&bytes).map_err(|_| "invalid remote response".to_string())
}

#[cfg(target_os = "android")]
#[tauri::command]
async fn mobile_remote_request(state: State<'_, MobileRuntimeState>, path: String, method: String, payload: Option<Value>) -> Result<Value, String> {
    let app_data_dir = state.app_data_dir.clone();
    tauri::async_runtime::spawn_blocking(move || remote_request_blocking(app_data_dir, path, method, payload))
        .await
        .map_err(|_| "remote request failed".to_string())?
}

#[cfg(target_os = "android")]
#[tauri::mobile_entry_point]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            let app_data_dir = app.path().app_data_dir().map_err(|e| e.to_string())?;
            fs::create_dir_all(&app_data_dir).map_err(|e| e.to_string())?;
            app.manage(MobileRuntimeState { app_data_dir });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            mobile_backend_url,
            mobile_pairing_state,
            mobile_discover_nodes,
            mobile_probe_host,
            mobile_request_pairing,
            mobile_poll_pairing,
            mobile_set_active_peer,
            mobile_clear_pairing,
            mobile_remote_request,
        ])
        .build(tauri::generate_context!())
        .expect("error while running MewCode Android")
        .run(|_app, _event| {});
}

#[cfg(not(target_os = "android"))]
pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            set_theme, select_directory, open_directory, open_external,
            provider_presets, provider_list, provider_save, provider_remove, provider_test,
            usage_list_accounts, usage_save_account, usage_remove_account, usage_browser_command,
            diagnostics, open_pipeline, close_pipeline, restart_backend,
            startup_status, retry_startup,
        ])
        .setup(|app| {
            let setup_error = |message: String| std::io::Error::new(std::io::ErrorKind::Other, message);
            let app_data_dir = resolve_app_data_dir(
                app.path().app_data_dir().map_err(|e| setup_error(e.to_string()))?,
                std::env::var_os("MEWCODE_USER_DATA_DIR"),
            ).map_err(setup_error)?;
            fs::create_dir_all(&app_data_dir).map_err(|e| e.to_string())?;
            let repo_root = discover_repo_root();
            let providers_path = app_data_dir.join("providers.json");
            let usage_accounts_path = app_data_dir.join("usage-accounts.json");
            let (providers, providers_error) = match load_json_vec(&providers_path) {
                JsonArrayLoad::Loaded(values) => (values, None),
                JsonArrayLoad::Missing => (Vec::new(), None),
                JsonArrayLoad::Failed(reason) => (Vec::new(), Some(reason)),
            };
            let (usage_accounts, usage_accounts_error) = match load_json_vec(&usage_accounts_path) {
                JsonArrayLoad::Loaded(values) => (values, None),
                JsonArrayLoad::Missing => (Vec::new(), None),
                JsonArrayLoad::Failed(reason) => (Vec::new(), Some(reason)),
            };
            if let Some(reason) = providers_error.as_deref() {
                // Loud, and never treated as "no providers": provider edits stay
                // refused until the file is repaired or moved.
                eprintln!(
                    "mewcode: {} is unusable ({reason}); provider edits are disabled until it is repaired",
                    providers_path.display()
                );
            }
            if let Some(reason) = usage_accounts_error.as_deref() {
                eprintln!(
                    "mewcode: {} is unusable ({reason}); usage-account edits are disabled until it is repaired",
                    usage_accounts_path.display()
                );
            }
            let state = RuntimeState {
                backend_url: Mutex::new(String::new()),
                desktop_token: Mutex::new(String::new()),
                backend: Mutex::new(None),
                diagnostics: Mutex::new(Vec::new()),
                theme: Mutex::new(read_theme(&app_data_dir.join("ui-state.json"))),
                app_data_dir: app_data_dir.clone(),
                repo_root,
                providers: Mutex::new(providers),
                usage_accounts: Mutex::new(usage_accounts),
                providers_error: Mutex::new(providers_error),
                usage_accounts_error: Mutex::new(usage_accounts_error),
                startup: Mutex::new(StartupStatus {
                    phase: "starting".into(), message: "正在启动本地服务".into(), detail: String::new(),
                    log_dir: app_data_dir.to_string_lossy().into_owned(),
                }),
                starting: AtomicBool::new(false),
                shutting_down: AtomicBool::new(false),
            };
            app.manage(state);
            let state = app.state::<RuntimeState>();
            let app_handle = app.handle().clone();
            if let Err(error) = create_main_window(&app_handle, &state) {
                state.stop_backend();
                report_startup_failure(&app_data_dir, &error);
                return Err(setup_error(error).into());
            }
            launch_desktop_backend(app_handle).map_err(setup_error)?;
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building MewCode Tauri application")
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                let state = app.state::<RuntimeState>();
                state.shutting_down.store(true, Ordering::SeqCst);
                state.stop_backend();
            }
        });
}

#[cfg(not(target_os = "android"))]
fn main() {
    run();
}

#[cfg(test)]
mod tests {
    //! Regressions for the 2026-09-24 incident: a UTF-8 BOM written by an
    //! external script made `read_json_vec` return an empty list, and the next
    //! save persisted that empty list over a file holding eight providers.

    use super::*;
    use std::io::Write;

    #[test]
    fn backend_exit_is_reported_before_health_or_timeout() {
        #[cfg(windows)]
        let mut command = { let mut command = Command::new("cmd.exe"); command.args(["/D", "/C", "exit 7"]); command };
        #[cfg(not(windows))]
        let mut command = { let mut command = Command::new("sh"); command.args(["-c", "exit 7"]); command };
        hide_console(&mut command);
        let mut child = command.spawn().unwrap();
        child.wait().unwrap();
        let started = Instant::now();
        let error = wait_for_backend(|| child.try_wait().map_err(|e| e.to_string()),
            || panic!("an exited child must not be healthy"), Duration::from_secs(120)).unwrap_err();
        assert!(error.contains("exited before becoming ready"));
        assert!(error.contains("backend.err.log"));
        assert!(started.elapsed() < Duration::from_secs(2));
    }

    #[test]
    fn cancelled_startup_does_not_wait_for_the_health_deadline() {
        let error = wait_for_backend(|| Err("Startup cancelled".into()),
            || panic!("cancelled startup must not probe health"), Duration::from_secs(120)).unwrap_err();
        assert_eq!(error, "Startup cancelled");
    }

    #[cfg(windows)]
    #[test]
    fn hidden_child_has_no_console_window() {
        if std::env::var_os("MEWCODE_TEST_CONSOLE_CHILD").is_some() {
            #[link(name = "kernel32")]
            extern "system" { fn GetConsoleWindow() -> isize; }
            assert_eq!(unsafe { GetConsoleWindow() }, 0);
            return;
        }
        let mut command = Command::new(std::env::current_exe().unwrap());
        command.args(["--exact", std::thread::current().name().unwrap(), "--nocapture"])
            .env("MEWCODE_TEST_CONSOLE_CHILD", "1");
        hide_console(&mut command);
        let result = command.output().unwrap();
        assert!(result.status.success(), "{}", String::from_utf8_lossy(&result.stdout));
    }

    #[test]
    fn data_root_keeps_the_default_without_an_override() {
        let root = std::env::temp_dir().join("mewcode-default-data");
        assert_eq!(resolve_app_data_dir(root.clone(), None).unwrap(), root);
    }

    #[test]
    fn data_root_uses_the_same_absolute_override_for_desktop_and_backend() {
        let root = std::env::temp_dir().join("MewCode isolated data");
        let other = std::env::temp_dir().join("mewcode-default-data");
        assert_eq!(resolve_app_data_dir(other, Some(root.clone().into_os_string())).unwrap(), root);
    }

    #[test]
    fn invalid_override_does_not_fall_back_to_real_user_data() {
        let root = std::env::temp_dir().join("mewcode-default-data");
        for raw in ["", "relative/data"] {
            assert!(resolve_app_data_dir(root.clone(), Some(raw.into())).is_err());
        }
    }

    #[test]
    fn explicit_data_profile_also_isolates_backend_working_directory() {
        let root = std::env::temp_dir().join("MewCode isolated data");
        let repo = std::env::temp_dir().join("MewCode live repo");
        assert_eq!(backend_working_directory(repo.clone(), &root, true), root);
        assert_eq!(backend_working_directory(repo.clone(), &root, false), repo);
    }

    #[test]
    fn relative_repo_override_keeps_server_argument_absolute_after_cwd_change() {
        let launch = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let root = explicit_repo_root(Path::new(".."), &launch).expect("source repository");
        let server = root.join("frontend").join("server.py");
        let data = std::env::temp_dir().join("MewCode isolated data");
        assert!(root.is_absolute() && server.is_absolute() && server.is_file());
        assert_eq!(backend_working_directory(root, &data, true), data);
        assert!(server.is_file());
        assert!(explicit_repo_root(Path::new("relative"), Path::new("")).is_none());
    }

    fn scratch(name: &str, bytes: &[u8]) -> PathBuf {
        let mut directory = std::env::temp_dir();
        directory.push(format!("mewcode-tauri-test-{}-{name}", std::process::id()));
        fs::create_dir_all(&directory).expect("scratch directory");
        let path = directory.join("providers.json");
        let mut file = fs::File::create(&path).expect("scratch file");
        file.write_all(bytes).expect("write scratch file");
        path
    }

    #[test]
    fn bom_prefixed_snapshot_still_loads() {
        let mut bytes = vec![0xEF, 0xBB, 0xBF];
        bytes.extend_from_slice(br#"[{"provider_key":"desktop:example_provider_7"}]"#);
        let path = scratch("bom", &bytes);
        match load_json_vec(&path) {
            JsonArrayLoad::Loaded(rows) => assert_eq!(rows.len(), 1),
            other => panic!("expected Loaded for a BOM-prefixed file, got {other:?}"),
        }
    }

    #[test]
    fn unreadable_snapshot_is_a_failure_not_an_empty_list() {
        let path = scratch("truncated", br#"[{"provider_key": "#);
        match load_json_vec(&path) {
            JsonArrayLoad::Failed(reason) => {
                assert!(reason.contains("invalid JSON"), "unexpected reason: {reason}");
            }
            other => panic!("expected Failed for a truncated file, got {other:?}"),
        }
    }

    #[test]
    fn non_array_snapshot_is_a_failure() {
        let path = scratch("object", br#"{"provider_key":"desktop:x"}"#);
        assert!(matches!(load_json_vec(&path), JsonArrayLoad::Failed(_)));
    }

    #[test]
    fn missing_snapshot_is_a_fresh_start() {
        let mut path = std::env::temp_dir();
        path.push(format!("mewcode-tauri-missing-{}.json", std::process::id()));
        let _ = fs::remove_file(&path);
        assert_eq!(load_json_vec(&path), JsonArrayLoad::Missing);
    }

    #[test]
    fn write_guard_refuses_after_a_failed_load_and_releases_after_a_good_save() {
        let error = Mutex::new(Some("invalid JSON: expected value".to_string()));
        let refusal = write_guard(&error, "providers.json").expect_err("must refuse to write");
        assert!(refusal.contains("refusing to overwrite"), "unexpected text: {refusal}");
        clear_load_error(&error);
        assert!(write_guard(&error, "providers.json").is_ok());
    }
}
