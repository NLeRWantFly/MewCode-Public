from __future__ import annotations

import os
import shutil

from mewcode.teams.models import BackendType


class BackendDetectionError(Exception):
    pass


def _in_tmux_session() -> bool:
    return bool(os.environ.get("TMUX"))


def _in_iterm2() -> bool:
    return os.environ.get("TERM_PROGRAM") == "iTerm.app"


def _it2_available() -> bool:
    return shutil.which("it2") is not None


def _tmux_installed() -> bool:
    return shutil.which("tmux") is not None


def detect_backend(
    teammate_mode: str = "",
    is_interactive: bool = True,
) -> BackendType:
    """当前唯一受支持的后端是 in-process。

    tmux/iTerm2 pane 后端的孵化端代码存在（spawn_tmux/spawn_iterm2），
    但消费端协议（mewcode CLI 的 --work-dir/--agent-type/--model 参数与
    MEWCODE_* 环境变量）尚未实现——显式请求 pane 模式时直接报错，
    而不是静默孵化一个无法按协议启动的进程。
    """
    if teammate_mode in ("tmux", "iterm2"):
        raise BackendDetectionError(
            f"Pane backend '{teammate_mode}' is experimental and not wired up "
            f"yet (the mewcode CLI does not consume the spawn protocol). "
            f"Use teammate_mode: \"in-process\"."
        )
    return BackendType.IN_PROCESS


def detect_pane_backend(
    teammate_mode: str = "",
    is_interactive: bool = True,
) -> BackendType:
    """[EXPERIMENTAL] pane 后端探测链，当前无调用方。

    保留以备 pane 协议补齐后接线；探测顺序：显式 in-process/非交互 →
    已在 tmux 会话内 → iTerm2+it2 → 本机装有 tmux。
    """
    if teammate_mode == "in-process" or not is_interactive:
        return BackendType.IN_PROCESS

    if _in_tmux_session():
        return BackendType.TMUX

    if _in_iterm2() and _it2_available():
        return BackendType.ITERM2

    if _tmux_installed():
        return BackendType.TMUX

    raise BackendDetectionError(
        "No suitable terminal backend found for Agent Team.\n"
        "Pane backends need tmux (Linux/macOS) or iTerm2 + it2 CLI (macOS); "
        "neither is available on Windows.\n"
        "Set 'teammate_mode: \"in-process\"' in config.yaml to use the "
        "in-process backend."
    )
