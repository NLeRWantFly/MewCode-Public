from __future__ import annotations

import logging
import shlex
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class TmuxPaneInfo:
    pane_id: str
    session: str


class TmuxSpawnError(Exception):
    pass


def _run_tmux(*args: str) -> str:
    result = subprocess.run(
        ["tmux", *args],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise TmuxSpawnError(f"tmux {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def build_cli_command(
    team_name: str,
    teammate_name: str,
    worktree_path: str,
    prompt: str,
    agent_type: str = "",
    model: str = "",
    mailbox_dir: str = "",
) -> str:
    # 所有值统一 shlex.quote：team_name/teammate_name/model 可由 LLM 参数
    # 直接控制，worktree_path 可能含空格——裸拼进 shell 行等于注入点
    parts = ["mewcode", "-p"]
    parts.extend(["--work-dir", shlex.quote(worktree_path)])
    if agent_type:
        parts.extend(["--agent-type", shlex.quote(agent_type)])
    if model:
        parts.extend(["--model", shlex.quote(model)])
    env_parts = [
        f"MEWCODE_TEAM_NAME={shlex.quote(team_name)}",
        f"MEWCODE_TEAMMATE_NAME={shlex.quote(teammate_name)}",
    ]
    if mailbox_dir:
        env_parts.append(f"MEWCODE_MAILBOX_DIR={shlex.quote(mailbox_dir)}")
    env_prefix = " ".join(env_parts)
    cmd = " ".join(parts)
    return f"{env_prefix} {cmd} {shlex.quote(prompt)}"


def spawn_tmux_teammate(
    team_name: str,
    teammate_name: str,
    worktree_path: str,
    prompt: str,
    agent_type: str = "",
    model: str = "",
    mailbox_dir: str = "",
) -> TmuxPaneInfo:
    window_name = f"{team_name}-{teammate_name}"

    try:
        pane_id = _run_tmux(
            "split-window",
            "-h",
            "-P",
            "-F", "#{pane_id}",
            "-t", f"{team_name}",
        )
    except TmuxSpawnError:
        try:
            _run_tmux("new-window", "-t", f"{team_name}", "-n", window_name, "-P", "-F", "#{pane_id}")
            pane_id = _run_tmux(
                "split-window",
                "-h",
                "-P",
                "-F", "#{pane_id}",
                "-t", f"{team_name}:{window_name}",
            )
        except TmuxSpawnError:
            _run_tmux("new-session", "-d", "-s", team_name, "-n", window_name)
            pane_id = _run_tmux(
                "list-panes",
                "-t", f"{team_name}:{window_name}",
                "-F", "#{pane_id}",
            ).split("\n")[0]

    cli_cmd = build_cli_command(
        team_name=team_name,
        teammate_name=teammate_name,
        worktree_path=worktree_path,
        prompt=prompt,
        agent_type=agent_type,
        model=model,
        mailbox_dir=mailbox_dir,
    )
    _run_tmux("send-keys", "-t", pane_id, cli_cmd, "Enter")

    log.info("Spawned tmux teammate %s in pane %s", teammate_name, pane_id)
    return TmuxPaneInfo(pane_id=pane_id, session=team_name)


def send_keys_to_pane(pane_id: str, keys: str = "") -> None:
    try:
        _run_tmux("send-keys", "-t", pane_id, keys, "Enter")
    except TmuxSpawnError:
        log.warning("Failed to send keys to tmux pane %s", pane_id)


def kill_pane(pane_id: str) -> None:
    try:
        _run_tmux("kill-pane", "-t", pane_id)
    except TmuxSpawnError:
        pass
