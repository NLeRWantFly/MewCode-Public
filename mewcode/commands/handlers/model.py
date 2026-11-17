from __future__ import annotations

from mewcode.commands.registry import Command, CommandContext, CommandType


async def handle_model(ctx: CommandContext) -> None:
    arg = ctx.args.strip()

    if getattr(ctx.ui, "switch_model", None) is None:
        ctx.ui.add_system_message("当前界面不支持模型切换")
        return

    provider = getattr(ctx.ui, "_selected_provider", None)
    if provider is None:
        ctx.ui.add_system_message(
            "当前 Provider 未就绪，先用 /provider 选择可用配置"
        )
        return

    # /model —— 拉取模型列表并调出选择菜单
    if arg == "":
        ctx.ui.add_system_message("正在获取模型列表…")
        lister = getattr(ctx.ui, "list_available_models", None)
        models = await lister() if lister is not None else None
        if models:
            ctx.ui.show_model_menu(models)
        else:
            ctx.ui.add_system_message(
                f"当前模型: {provider.model}\n"
                "未能从 Provider 获取模型列表，可用 /model <名称> 直接指定"
            )
        return

    if arg in ("list", "ls"):
        ctx.ui.add_system_message("正在获取模型列表…")
        lister = getattr(ctx.ui, "list_available_models", None)
        models = await lister() if lister is not None else None
        if not models:
            ctx.ui.add_system_message(
                f"当前模型: {provider.model}\n未能获取模型列表"
            )
            return
        lines = [f"当前模型: {provider.model}", "可用模型："]
        for m in models:
            marker = "*" if m == provider.model else " "
            lines.append(f" {marker} {m}")
        lines.append("")
        lines.append("切换: /model <名称>")
        ctx.ui.add_system_message("\n".join(lines))
        return

    # /model <名称> —— 直接切换
    error = ctx.ui.switch_model(arg)
    if error:
        ctx.ui.add_system_message(f"切换失败: {error}")


MODEL_COMMAND = Command(
    name="model",
    description="切换当前 Provider 使用的模型（从 Provider 拉取可选列表）",
    usage="/model [list | <名称>]",
    type=CommandType.LOCAL,
    handler=handle_model,
)
