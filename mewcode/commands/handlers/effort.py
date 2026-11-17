from __future__ import annotations

from mewcode.commands.registry import Command, CommandContext, CommandType


async def handle_effort(ctx: CommandContext) -> None:
    arg = ctx.args.strip().lower()

    if getattr(ctx.ui, "get_effort_levels", None) is None:
        ctx.ui.add_system_message("当前界面不支持 effort 调整")
        return

    levels = ctx.ui.get_effort_levels()
    if not levels:
        ctx.ui.add_system_message("当前 Provider 未就绪，无法设置 effort")
        return

    # /effort —— 调出选择菜单
    if arg == "":
        show_menu = getattr(ctx.ui, "show_effort_menu", None)
        if show_menu is not None:
            show_menu()
            return
        arg = "list"

    if arg in ("list", "ls"):
        current = getattr(ctx.ui, "_effort", "") or ""
        lines = ["可用 effort 级别："]
        for lv in levels:
            marker = "*" if lv == current else " "
            lines.append(f" {marker} {lv}")
        lines.append("")
        lines.append("设置: /effort <级别>")
        ctx.ui.add_system_message("\n".join(lines))
        return

    # /effort <级别> —— 直接设置
    error = ctx.ui.set_effort(arg)
    if error:
        ctx.ui.add_system_message(error)
    else:
        ctx.ui.add_system_message(f"Effort 已设置为: {arg}")


EFFORT_COMMAND = Command(
    name="effort",
    description="调整模型推理强度（级别取决于当前协议，如 off/low/medium/high）",
    usage="/effort [list | <级别>]",
    type=CommandType.LOCAL,
    handler=handle_effort,
)
