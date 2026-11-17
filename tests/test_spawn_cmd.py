from __future__ import annotations

import shlex

from mewcode.teams.spawn_tmux import build_cli_command


def test_build_cli_command_quotes_all_values():
    cmd = build_cli_command(
        team_name="team",
        teammate_name="mate; rm -rf ~",
        worktree_path="/path with space/wt",
        prompt="do 'quoted' things; echo $(pwd)",
        agent_type="general-purpose",
        model="sonnet",
        mailbox_dir="/mail box",
    )
    # 必须能被 POSIX shell 安全切分，且危险片段保持为单个 token
    tokens = shlex.split(cmd)
    assert "MEWCODE_TEAMMATE_NAME=mate; rm -rf ~" in tokens
    assert "/path with space/wt" in tokens
    assert "do 'quoted' things; echo $(pwd)" in tokens
    # 不允许出现裸的命令注入分隔
    assert ";" not in [t for t in tokens]


def test_build_cli_command_minimal():
    cmd = build_cli_command(
        team_name="t", teammate_name="m", worktree_path="/wt", prompt="hi",
    )
    tokens = shlex.split(cmd)
    assert tokens[-1] == "hi"
    assert "--agent-type" not in tokens
    assert "--model" not in tokens
