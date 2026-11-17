from __future__ import annotations

from mewcode.commands.registry import Command, CommandContext, CommandType


async def handle_do(ctx: CommandContext) -> None:
    """Exit planning through the same UI policy transition as the mode control."""
    ctx.ui.set_plan_mode(False)
    ctx.ui.add_system_message("已返回执行模式；沿用进入 Plan 前的权限设置，工具仍需遵守原审批与执行边界。")


DO_COMMAND = Command(
    name="do",
    description="退出 Plan 模式，恢复原权限档位",
    usage="/do",
    type=CommandType.LOCAL_UI,
    handler=handle_do,
)
