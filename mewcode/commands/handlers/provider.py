from __future__ import annotations

from mewcode.commands.registry import Command, CommandContext, CommandType
from mewcode.provider_sources import ProviderSlot, validate_slot


def _get_slots(ctx: CommandContext, refresh: bool = False) -> list[ProviderSlot]:
    getter = getattr(ctx.ui, "get_provider_slots", None)
    if getter is None:
        return []
    return getter(refresh=refresh)


def _current_key(ctx: CommandContext) -> str:
    current = getattr(ctx.ui, "_selected_provider", None)
    slots = _get_slots(ctx)
    for slot in slots:
        if slot.provider is current:
            return slot.key
    return ""


def _format_slot_list(ctx: CommandContext, slots: list[ProviderSlot]) -> str:
    current_key = _current_key(ctx)
    lines = ["可用 Provider 插槽："]
    for i, slot in enumerate(slots, start=1):
        marker = "*" if slot.key == current_key else " "
        err = validate_slot(slot)
        status = "" if err is None else f"  [!] {err}"
        lines.append(f" {marker} {i}. {slot.label}{status}")
    lines.append("")
    lines.append("切换: /provider <编号|名称>    重新扫描: /provider reload")
    return "\n".join(lines)


def _find_slot(slots: list[ProviderSlot], token: str) -> ProviderSlot | None:
    if token.isdigit():
        idx = int(token) - 1
        if 0 <= idx < len(slots):
            return slots[idx]
        return None
    for slot in slots:
        if slot.key == token or slot.provider.name == token:
            return slot
    return None


async def handle_provider(ctx: CommandContext) -> None:
    arg = ctx.args.strip()

    if getattr(ctx.ui, "get_provider_slots", None) is None:
        ctx.ui.add_system_message("当前界面不支持 Provider 切换")
        return

    # /provider —— 调出选择菜单（无菜单能力时降级为文本列表）
    if arg == "":
        show_menu = getattr(ctx.ui, "show_provider_menu", None)
        if show_menu is not None:
            show_menu()
            return
        arg = "list"

    if arg in ("list", "ls"):
        slots = _get_slots(ctx)
        if not slots:
            ctx.ui.add_system_message("未发现任何 Provider 配置源")
            return
        ctx.ui.add_system_message(_format_slot_list(ctx, slots))
        return

    if arg == "reload":
        slots = _get_slots(ctx, refresh=True)
        ctx.ui.add_system_message(
            f"已重新扫描配置源，共发现 {len(slots)} 个插槽\n\n"
            + _format_slot_list(ctx, slots)
        )
        return

    # /provider <编号|名称> —— 直接切换
    slots = _get_slots(ctx)
    slot = _find_slot(slots, arg)
    if slot is None:
        ctx.ui.add_system_message(
            f"未找到 Provider: {arg}\n\n" + _format_slot_list(ctx, slots)
        )
        return

    switch = getattr(ctx.ui, "switch_provider", None)
    if switch is None:
        ctx.ui.add_system_message("当前界面不支持 Provider 切换")
        return
    error = switch(slot)
    if error:
        ctx.ui.add_system_message(f"切换失败: {error}")


PROVIDER_COMMAND = Command(
    name="provider",
    description="查看/切换 LLM Provider（支持 codex、Claude Code、项目 .env 配置源）",
    usage="/provider [list | reload | <编号|名称>]",
    type=CommandType.LOCAL,
    handler=handle_provider,
    aliases=["providers"],
)
