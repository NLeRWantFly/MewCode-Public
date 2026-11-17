from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from mewcode.config import ConfigError, load_config
from mewcode.hooks import HookConfigError, HookEngine, load_hooks
from mewcode.permissions import PermissionMode
from mewcode.permissions.modes import PUBLIC_PERMISSION_MODES, public_permission_mode


def main() -> None:
    # 先确保 .mewcode/ 目录存在，否则下面写 debug.log 会因目录不存在而崩溃
    Path(".mewcode").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(message)s",
        filename=".mewcode/debug.log",
        filemode="w",
    )

    parser = argparse.ArgumentParser(prog="mewcode", description="MewCode AI coding assistant")
    parser.add_argument(
        "--mode",
        choices=[m.value for m in PUBLIC_PERMISSION_MODES],
        default=None,
        help="Permission mode (overrides config.yaml)",
    )
    parser.add_argument(
        "-p",
        metavar="PROMPT",
        default=None,
        help="Run non-interactively: execute the prompt and print the result to stdout",
    )
    args = parser.parse_args()

    try:
        config = load_config()
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    mode_str = args.mode if args.mode else config.permission_mode
    permission_mode = public_permission_mode(mode_str, allow_plan=True)

    try:
        hooks = load_hooks(config.raw_hooks)
    except HookConfigError as e:
        print(f"Hook config error: {e}", file=sys.stderr)
        sys.exit(1)

    hook_engine = HookEngine(hooks) if hooks else None

    if args.p is not None:
        asyncio.run(_run_prompt(config, permission_mode, hook_engine, args.p))
        return

    from mewcode.app import MewCodeApp
    from mewcode.driver import NoAltScreenDriver

    app = MewCodeApp(
        providers=config.providers,
        permission_mode=permission_mode,
        mcp_servers=config.mcp_servers,
        hook_engine=hook_engine,
        enable_fork=config.enable_fork,
        enable_verification_agent=config.enable_verification_agent,
        worktree_config=config.worktree,
        teammate_mode=config.teammate_mode,
        enable_coordinator_mode=config.enable_coordinator_mode,
        redis_config=config.redis,
        driver_class=NoAltScreenDriver,
    )
    # 注入 Harness 配置到 app 实例（app 在 _init_harness 中读取）
    app._teammate_permission_mode = config.teammate_permission_mode
    app._app_config = config
    app._compact_config = config.compact
    app._critic_config = config.critic
    app._rate_limit_config = config.rate_limit
    app._allow_self_modification = config.allow_self_modification
    # 注入 Evolution 配置
    app._allow_self_evolution = config.allow_self_evolution
    app._evolution_config = config.evolution
    app.run()


async def _run_prompt(config, permission_mode, hook_engine, prompt: str) -> None:
    from mewcode.client import resolve_context_window
    from mewcode.opencode_go import resolve_effective_provider_config
    from mewcode.plugins import ApplicationHost

    provider = resolve_effective_provider_config(config.providers[0])
    # 第 2 层：尽力从 provider 自动拉取模型的 context window（缓存在 provider 上）。
    # 不会抛异常或阻塞启动；失败则退化到映射表。
    await resolve_context_window(provider)
    work_dir = os.getcwd()
    plugin_host = ApplicationHost(
        work_dir,
        hook_engine=hook_engine,
        enable_verification_agent=config.enable_verification_agent,
    )
    await plugin_host.astart()
    try:
        await _run_prompt_with_host(
            config,
            permission_mode,
            hook_engine,
            prompt,
            provider,
            work_dir,
            plugin_host,
        )
    finally:
        await plugin_host.aclose()


async def _run_prompt_with_host(
    config,
    permission_mode,
    hook_engine,
    prompt: str,
    provider,
    work_dir: str,
    plugin_host,
) -> None:
    from mewcode.plugins.features import MultiAgentOptions, multi_agent_plugin
    from mewcode.plugins.runtime import AgentRuntimeRequest
    from mewcode.plugins.services import AGENT_FACTORY, TASKS, TEAMS

    factory = plugin_host.context.get(AGENT_FACTORY)
    runtime = await factory.acreate(AgentRuntimeRequest(
        provider=provider,
        work_dir=work_dir,
        config=config,
        permission_mode=permission_mode,
        hook_engine=hook_engine,
    ))
    runtime.install(multi_agent_plugin(MultiAgentOptions(
        provider=provider,
        enable_fork=config.enable_fork,
        worktree_config=config.worktree,
        teammate_permission_mode=config.teammate_permission_mode,
        teammate_mode="in-process",
        is_interactive=False,
        enable_coordinator_mode=config.enable_coordinator_mode,
    )))
    agent = runtime.agent
    conv = runtime.conversation
    task_manager = runtime.context.get(TASKS)
    team_manager = runtime.context.get(TEAMS)

    def drain_notifications() -> list[str]:
        notes: list[str] = []
        for t in task_manager.poll_completed():
            notes.append(
                f"<task-notification>\n<task_id>{t.id}</task_id>\n"
                f"<status>{t.status}</status>\n<result>{t.result}</result>\n"
                f"</task-notification>"
            )
        notes.extend(team_manager.drain_lead_mailbox())
        return notes

    try:
        last_result = await agent.run_to_completion(prompt, conv)
        print(last_result, flush=True)

        if not team_manager._teams:
            return

        import sys
        for i in range(90):
            await asyncio.sleep(2)
            running = {k: not t.done() for k, t in task_manager._async_tasks.items()}
            completed_ids = [t.id for t in task_manager._tasks.values() if t.status != "running"]
            print(f"[poll {i}] running={running} completed={completed_ids} teams={list(team_manager._teams.keys())} queue_size={task_manager._notify_queue.qsize()}", file=sys.stderr, flush=True)
            notes = drain_notifications()
            if not notes:
                has_running = any(v for v in running.values())
                if not has_running:
                    print(f"[poll {i}] no running tasks, breaking", file=sys.stderr, flush=True)
                    break
                continue
            for note in notes:
                conv.add_system_reminder(note)
            last_result = await agent.run_to_completion(
                "Teammate notifications received. Process them and continue.", conv
            )
            print(last_result, flush=True)
    finally:
        # -p 模式退出前兜底清理：不留下 is_active=True 的僵尸团队、
        # 孤儿 worktree 和无人消费的邮箱
        for name in list(team_manager._teams):
            try:
                team_manager.delete_team(name, force=True)
            except Exception:
                pass
        await runtime.aclose()


if __name__ == "__main__":
    main()
