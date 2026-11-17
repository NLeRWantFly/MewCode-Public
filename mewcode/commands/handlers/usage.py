from __future__ import annotations

from decimal import Decimal

from mewcode.commands.registry import Command, CommandContext, CommandType
from mewcode.usage.backend import UsageBackendError
from mewcode.usage.models import Money, SpendLimitSnapshot, TokenUsage
from mewcode.usage.service import current_month_query


def _money(value: Money) -> str:
    symbol = "$" if value.currency == "usd" else f"{value.currency.upper()} "
    return f"{symbol}{value.amount.quantize(Decimal('0.000001')):f}"


def _tokens(value: TokenUsage) -> str:
    return (
        f"输入 {value.input_tokens:,} + 缓存读 {value.cached_input_tokens:,}"
        f" + 缓存写 {value.cache_write_input_tokens:,}；"
        f"输出 {value.output_tokens:,}（推理 {value.reasoning_tokens:,}）"
    )


def _limit_line(limit: SpendLimitSnapshot) -> str:
    label = "组织" if limit.scope == "organization" else f"项目 {limit.scope_id}"
    status = f"，状态 {limit.enforcement_status}" if limit.enforcement_status else ""
    return (
        f"{label}硬支出上限：{_money(limit.threshold)}；"
        f"已用 {_money(limit.spent)}；可用 {_money(limit.remaining)}{status}"
    )


async def handle_usage(ctx: CommandContext) -> None:
    service = ctx.config.get("usage_service")
    provider = ctx.config.get("provider")
    if service is None or provider is None:
        ctx.ui.add_system_message("用量服务或当前 Provider 尚未初始化")
        return

    subcommand = (ctx.args.strip().lower() or "all").split()[0]
    if subcommand not in {"all", "local", "account", "reconcile"}:
        ctx.ui.add_system_message(
            "用法：/usage [all|local|account|reconcile]"
        )
        return

    query = current_month_query(
        project_ids=provider.usage.project_ids,
        api_key_ids=provider.usage.api_key_ids,
    )
    lines = [f"用量追溯 · {provider.name} · 本月"]

    if subcommand in {"all", "local"}:
        local = service.local_summary(
            provider_name=provider.name,
            start_time=query.start_time,
            end_time=query.end_time,
        )
        lines.extend(
            [
                "",
                f"本地请求账本：{local.request_count:,} 次",
                _tokens(local.tokens),
            ]
        )
        for model, tokens in sorted(local.by_model.items()):
            lines.append(f"  {model or '(未知模型)'}：{_tokens(tokens)}")
        if local.evidence_counts:
            evidence = "，".join(
                f"{name}={count}" for name, count in sorted(local.evidence_counts.items())
            )
            lines.append(f"证据等级：{evidence}")
        rate = local.latest_rate_limits
        if rate is not None:
            lines.append(
                "最近请求限流余量（不是账户余额）："
                f"requests={rate.requests_remaining}/{rate.request_limit}，"
                f"tokens={rate.tokens_remaining}/{rate.token_limit}"
            )

    if subcommand in {"all", "account"}:
        try:
            official_usage, snapshot = await service.account_report(provider)
        except UsageBackendError as exc:
            lines.extend(["", f"官方账户账单：不可用（{exc}）"])
        except Exception as exc:
            lines.extend(["", f"官方账户账单：查询失败（{exc}）"])
        else:
            lines.extend(
                [
                    "",
                    f"官方账户账单：{snapshot.evidence.value}",
                    f"官方聚合请求：{official_usage.request_count:,} 次",
                    f"官方聚合 token：{_tokens(official_usage.tokens)}",
                    f"筛选范围实际费用：{_money(snapshot.scope_cost)}",
                    f"组织本月实际费用：{_money(snapshot.organization_cost)}",
                    f"归属：{snapshot.attribution.value} — {snapshot.attribution_reason}",
                ]
            )
            official_by_model: dict[str, TokenUsage] = {}
            for group in official_usage.groups:
                model = group.model or "(未知模型)"
                official_by_model[model] = official_by_model.get(
                    model, TokenUsage()
                ).add(group.tokens)
            for model, tokens in sorted(official_by_model.items()):
                lines.append(f"  官方 {model}：{_tokens(tokens)}")
            if snapshot.organization_limit is not None:
                lines.append(_limit_line(snapshot.organization_limit))
            else:
                lines.append("组织硬支出上限：未配置或官方端点未返回")
            lines.extend(_limit_line(item) for item in snapshot.project_limits)
            lines.append(
                "预付费信用余额："
                + (
                    _money(snapshot.prepaid_credit_balance)
                    if snapshot.prepaid_credit_balance is not None
                    else "OpenAI 公共 Admin API 不提供，不能伪报"
                )
            )
            lines.append(
                "获批使用层级额度："
                + (
                    _money(snapshot.approved_usage_limit)
                    if snapshot.approved_usage_limit is not None
                    else "OpenAI 公共 Admin API 不提供，不能伪报"
                )
            )
            lines.extend(f"限制：{item}" for item in snapshot.limitations)

    if subcommand == "reconcile":
        try:
            report = await service.reconcile(provider)
        except UsageBackendError as exc:
            lines.extend(["", f"对账不可用：{exc}"])
        except Exception as exc:
            lines.extend(["", f"对账失败：{exc}"])
        else:
            lines.extend(
                [
                    "",
                    f"本地：{_tokens(report.local.tokens)}",
                    f"官方：{_tokens(report.official.tokens)}",
                    f"差额：输入 {report.input_token_delta:+,}，"
                    f"输出 {report.output_token_delta:+,}",
                    f"聚合核验：{'是' if report.aggregate_verified else '否'}；"
                    f"逐请求精确关联：{'是' if report.exact_request_linkage else '否'}",
                    report.explanation,
                ]
            )

    ctx.ui.add_system_message("\n".join(lines))


USAGE_COMMAND = Command(
    name="usage",
    aliases=["quota"],
    description="查看请求用量、OpenAI 官方账单、硬支出余量与对账证据",
    usage="/usage [all|local|account|reconcile]",
    type=CommandType.LOCAL,
    handler=handle_usage,
)
