fn main() {
    println!("cargo:rerun-if-env-changed=MEWCODE_MOBILE_BACKEND_URL");
    // Explicitly register app commands so ACL permissions can allow them for
    // External(http://127.0.0.1:…) webviews that load the local Python UI.
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(
            tauri_build::AppManifest::new().commands(&[
                "set_theme",
                "select_directory",
                "open_directory",
                "open_external",
                "provider_presets",
                "provider_list",
                "provider_save",
                "provider_remove",
                "provider_test",
                "usage_list_accounts",
                "usage_save_account",
                "usage_remove_account",
                "usage_browser_command",
                "diagnostics",
                "open_pipeline",
                "close_pipeline",
                "restart_backend",
                "mobile_backend_url",
                "mobile_pairing_state",
                "mobile_discover_nodes",
                "mobile_probe_host",
                "mobile_request_pairing",
                "mobile_poll_pairing",
                "mobile_set_active_peer",
                "mobile_clear_pairing",
                "mobile_remote_request",
            ]),
        ),
    )
    .expect("failed to run tauri-build");
}
