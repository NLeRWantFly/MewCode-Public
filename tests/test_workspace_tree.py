from mewcode.workspace_tree import (
    MAX_IMPORT_CHARS,
    MAX_IMPORT_MESSAGES,
    PURE_CHAT_ID,
    build_import_text,
    collect_workspace_tree,
    local_machine,
    sort_history_items,
)


def test_local_machine_nests_mewcode_under_projects_and_normal():
    tree = local_machine(
        {
            "projects": [{
                "dir": "D:/work/demo",
                "label": "demo",
                "conversations": [{"id": "s1", "title": "修登录", "message_count": 3, "last_active": "1", "active": True}],
            }],
            "normal": [{"id": "n1", "title": "随手问", "message_count": 1, "last_active": "2"}],
            "work_dir": "D:/work/demo",
        },
        [
            {
                "source": "codex",
                "external_id": "c1",
                "title": "Codex 会话",
                "parent_folder": "demo",
                "project_path": "D:/work/demo",
                "is_project": True,
                "message_count": 4,
                "updated_at": "3",
            },
            {
                "source": "claude-code",
                "external_id": "a1",
                "title": "Claude 会话",
                "parent_folder": "app",
                "project_path": "C:/other/app",
                "is_project": True,
                "message_count": 2,
                "updated_at": "4",
            },
        ],
    )
    assert tree["label"] == "本机"
    labels = [source["label"] for source in tree["sources"]]
    assert labels == ["MewCode", "Codex", "Claude Code"]
    mew = tree["sources"][0]["projects"]
    assert mew[0]["label"] == "普通对话"
    assert mew[0]["conversations"][0]["id"] == "n1"
    assert mew[1]["label"] == "demo"
    assert tree["sources"][1]["projects"][0]["label"] == "demo"
    assert tree["sources"][2]["projects"][0]["label"] == "app"


def test_sort_history_items_puts_mewcode_before_codex():
    ordered = sort_history_items([
        {"source": "codex", "title": "old-codex", "updated_at": "2026-01-01"},
        {"source": "mewcode", "title": "old-mew", "updated_at": "2026-01-01"},
        {"source": "codex", "title": "new-codex", "updated_at": "2026-08-01"},
        {"source": "mewcode", "title": "new-mew", "updated_at": "2026-08-01"},
        {"source": "claude-code", "title": "claude", "updated_at": "2026-07-01"},
    ])
    assert [item["title"] for item in ordered] == [
        "new-mew", "old-mew", "new-codex", "old-codex", "claude",
    ]


def test_local_machine_keeps_same_leaf_names_apart_by_project_path():
    tree = local_machine(
        {"projects": [], "normal": [], "work_dir": ""},
        [
            {
                "source": "codex",
                "external_id": "c1",
                "title": "A 仓会话",
                "parent_folder": "demo",
                "project_path": "D:/a/demo",
                "is_project": True,
                "message_count": 1,
                "updated_at": "1",
            },
            {
                "source": "codex",
                "external_id": "c2",
                "title": "B 仓会话",
                "parent_folder": "demo",
                "project_path": "D:/b/demo",
                "is_project": True,
                "message_count": 1,
                "updated_at": "2",
            },
        ],
    )
    projects = tree["sources"][1]["projects"]
    assert len(projects) == 2
    assert {item["label"] for item in projects} == {"demo"}
    assert {item["id"] for item in projects} == {"D:/a/demo", "D:/b/demo"}


def test_local_machine_puts_unbound_external_into_pure_chats():
    tree = local_machine(
        {"projects": [], "normal": [], "work_dir": ""},
        [
            {
                "source": "codex",
                "external_id": "c1",
                "title": "23人玩游戏",
                "parent_folder": "未关联项目 · 08-28",
                "project_path": r"C:\Users\example-user\Documents\Codex\2026-08-28\slug",
                "is_project": False,
                "message_count": 2,
                "updated_at": "2026-08-28T12:00:00Z",
            },
            {
                "source": "codex",
                "external_id": "c2",
                "title": "更早的纯对话",
                "parent_folder": "",
                "project_path": "",
                "is_project": False,
                "message_count": 1,
                "updated_at": "2026-08-27T12:00:00Z",
            },
            {
                "source": "codex",
                "external_id": "hidden",
                "title": "噪声",
                "sidebar_visible": False,
                "is_project": False,
                "message_count": 0,
                "updated_at": "2026-08-29T12:00:00Z",
            },
        ],
    )
    projects = tree["sources"][1]["projects"]
    assert len(projects) == 1
    assert projects[0]["id"] == PURE_CHAT_ID
    assert projects[0]["label"] == "纯对话"
    assert projects[0]["kind"] == "chats"
    titles = [item["title"] for item in projects[0]["conversations"]]
    assert titles == ["23人玩游戏", "更早的纯对话"]


def test_collect_workspace_tree_keeps_local_when_remote_fails():
    tree = collect_workspace_tree(
        sidebar={"projects": [], "normal": [], "work_dir": "D:/work"},
        external_items=[],
        nodes=[{"id": "peer-a", "hostname": "office-a", "token_configured": True}],
        fetch_history=lambda _node: (_ for _ in ()).throw(ValueError("remote node is not configured")),
    )
    assert tree["machines"][0]["label"] == "本机"
    remote = tree["machines"][1]
    assert remote["label"] == "office-a"
    assert remote["error"] == "remote node is not configured"
    assert [source["label"] for source in remote["sources"]] == ["MewCode", "Codex", "Claude Code"]


def test_import_text_truncates_and_marks():
    messages = [{"role": "user", "text": "x" * 800} for _ in range(MAX_IMPORT_MESSAGES + 5)]
    text, truncated = build_import_text("codex", "长对话", messages)
    assert truncated is True
    assert "从 Codex 对话「长对话」导入的只读上下文：" in text
    assert "已截断" in text
    assert text.count("[USER]") <= MAX_IMPORT_MESSAGES
    assert len(text) <= MAX_IMPORT_CHARS + 80


def test_import_text_skips_structured_tool_blocks():
    messages = [
        {"role": "user", "kind": "message", "text": "修登录"},
        {"role": "assistant", "kind": "tool_result", "text": "total 41261\n./"},
        {"role": "assistant", "kind": "message", "text": "先看目录"},
    ]
    text, truncated = build_import_text("claude-code", "修登录", messages)
    assert truncated is False
    assert "修登录" in text
    assert "先看目录" in text
    assert "total 41261" not in text
