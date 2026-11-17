from __future__ import annotations

from pathlib import Path

from scripts.install_coord_bridge import CODEX_INSTRUCTIONS, SERVER_NAME, TRIGGER_WORD, install_one, update_named_server


def test_named_server_update_preserves_unrelated_toml() -> None:
    root = Path("D:/projfiles/MewCode")
    token = root / ".mewcode/coordination/.bridge-token"
    original = '[misc]\nkeep = true\n\n[mcp_servers.other]\ncommand = "echo"\n'
    updated = update_named_server(original, root=root, token_file=token)
    assert '[misc]\nkeep = true\n' in updated
    assert '[mcp_servers.other]\ncommand = "echo"\n' in updated
    assert f"[mcp_servers.{SERVER_NAME}]" in updated
    assert "grok-4.6" not in updated
    assert TRIGGER_WORD in CODEX_INSTRUCTIONS
    assert "gpt-5.6-luna" not in CODEX_INSTRUCTIONS


def test_named_server_update_is_idempotent() -> None:
    root = Path("D:/projfiles/MewCode")
    token = root / ".mewcode/coordination/.bridge-token"
    first = update_named_server("", root=root, token_file=token)
    assert update_named_server(first, root=root, token_file=token) == first


def test_grok_server_omits_codex_only_options() -> None:
    root = Path("D:/projfiles/MewCode")
    token = root / ".mewcode/coordination/.bridge-token"
    updated = update_named_server("", root=root, token_file=token, target="grok")
    assert "enabled_tools" not in updated
    assert "instructions" not in updated
    assert "command = \"uv\"" in updated


def test_nested_env_table_is_replaced_with_server_block() -> None:
    root = Path("D:/projfiles/MewCode")
    token = root / ".mewcode/coordination/.bridge-token"
    original = (
        '[mcp_servers.mewcode-coordination]\n'
        'command = "uv"\n'
        'env = { OLD = "inline" }\n\n'
        '  [mcp_servers.mewcode-coordination.env]\n'
        '  OLD = "nested"\n\n'
        '[marketplace]\n'
        'enabled = true\n'
    )
    updated = update_named_server(original, root=root, token_file=token, target="grok")
    assert updated.count("[mcp_servers.mewcode-coordination]") == 1
    assert "mcp_servers.mewcode-coordination.env" not in updated
    assert "[marketplace]" in updated


def test_codex_server_declares_single_word_trigger() -> None:
    root = Path("D:/projfiles/MewCode")
    token = root / ".mewcode/coordination/.bridge-token"
    updated = update_named_server("", root=root, token_file=token, target="codex")
    assert f"Single-word trigger: when the user sends exactly {TRIGGER_WORD}" in updated
    assert "codex-auto-review" in CODEX_INSTRUCTIONS
    assert "never fall back to a Codex provider model" in CODEX_INSTRUCTIONS


def test_install_one_backs_up_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[misc]\nkeep = true\n", encoding="utf-8")
    root = Path("D:/projfiles/MewCode")
    changed = install_one(path, root=root, token_file=root / ".mewcode/coordination/.bridge-token")
    assert changed is True
    assert list(tmp_path.glob("config.toml.bak.*"))
    assert "keep = true" in path.read_text(encoding="utf-8")
