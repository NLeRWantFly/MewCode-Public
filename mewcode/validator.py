"""MewCode 的配置校验逻辑。"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

VALID_PROTOCOLS = {"anthropic", "openai", "openai-compat", "typesafe"}
VALID_PROVIDER_TYPES = {"standard", "opencode-go"}
VALID_CONTEXT_CHANNELS = {"auto", "deepseek", "generic"}

VALID_PERMISSION_MODES = {
    "default",
    "acceptEdits",
    "plan",
    "auto",
    "strictIsolation",
    "bypassPermissions",
    "custom",
    "dontAsk",
}

VALID_TEAMMATE_MODES = {"", "in-process"}

# "" = 未配置（运行时等效 dontAsk 全自动）
VALID_TEAMMATE_PERMISSION_MODES = {"", "inherit", "default", "acceptEdits", "dontAsk"}

VALID_USAGE_ATTRIBUTION_MODES = {
    "auto",
    "direct",
    "byok-relay",
    "managed-relay",
}

DEFAULT_CONTEXT_WINDOW = 200_000

# 内置的"模型名子串 -> context window（最大输入 token 数）"映射表，
# 是 context window 回退链的第 3 层（见 ProviderConfig.get_context_window）。
# 按从最具体到最通用排序，第一个子串命中即生效。值仅为合理起始点，
# 模型更新/重命名后可能过时。如果值不准确，在配置中设置 context_window 覆盖（最高优先级）。
MODEL_CONTEXT_WINDOWS: list[tuple[str, int]] = [
    ("gpt-6.1-sol", 1_050_000),
    ("gpt-6-astra", 1_050_000),
    ("gpt-6-sol", 1_050_000),
    ("gpt-6-luna", 1_050_000),
    ("kimi-k3-1", 300_000),   # kimi-k3-1 本地网关实际上下文 300k（用户确认）
    ("kimi-k3", 1_000_000),
    ("kimi-k2", 256_000),
    ("gemini-3.1-flash-image", 131_072),
    ("gemini-2.5", 1_048_576),
    ("gemini-3", 1_048_576),
    ("1m", 1_000_000),       # 也覆盖 "-1m" 后缀（如 claude-...-1m）
    ("gpt-5.6", 1_050_000),  # GPT-5.6 Sol/Terra/Luna 官方 window 均为 1.05M（须先于 gpt-5 命中）；
                             # 272k 只是输入超过 272K 后按 2 倍计费的阈值，不是窗口
    ("gpt-5", 400_000),    # GPT-5 系列（含 codex 变体）的 window 约 400k
    ("gpt-4.1", 1_000_000),  # GPT-4.1 系列的 window 为 1M
    ("gpt-4o", 128_000),
    ("gpt-4-turbo", 128_000),
    ("o1", 200_000),         # OpenAI 推理模型 o1 / o3 / o4
    ("o3", 200_000),
    ("o4", 200_000),
    ("gpt-3.5", 16_385),
    ("claude-sonnet-4-6", 1_000_000),
    ("claude-sonnet-5", 1_000_000),
    ("claude-opus-4-6", 1_000_000),
    ("claude-opus-4-7", 1_000_000),
    ("claude-opus-4-8", 1_000_000),
    ("claude-opus-5", 1_000_000),
    ("claude-fable", 1_000_000),
    ("claude-mythos", 1_000_000),
    ("claude", 200_000),
    ("glm-5.3", 1_000_000),
    ("glm-5.2", 1_000_000),
    ("glm-5v-turbo", 200_000),
    ("glm-5", 200_000),
    ("glm-4.6v", 128_000),
    ("minimax-m3", 1_000_000),
    ("minimax-m2", 204_800),
    ("qwen3", 1_000_000),
    ("mimo-v2-omni", 256_000),
    ("mimo-v2", 1_000_000),
    ("hy3", 256_000),
    ("grok-4", 500_000),
]


def lookup_model_context_window(model: str) -> int:
    """通过子串匹配（第 3 层），返回内置映射表中该模型对应的
    context window；没有匹配则返回 0。"""
    m = model.lower()
    for substr, window in MODEL_CONTEXT_WINDOWS:
        if substr in m:
            return window
    return 0


class ConfigError(Exception):
    pass


def validate_providers(raw_providers: list) -> list[dict]:
    """校验 providers 列表，返回清洗后的 provider 字典列表。"""
    # Desktop credentials and local CLI discovery can supply every provider.
    # An empty explicit list must not require an unusable placeholder route.
    if not isinstance(raw_providers, list):
        raise ConfigError("'providers' must be a list")

    providers: list[dict] = []
    for i, entry in enumerate(raw_providers):
        if not isinstance(entry, dict):
            raise ConfigError(f"Provider #{i + 1}: must be a mapping")

        missing = [f for f in ("name", "protocol", "base_url", "model") if f not in entry]
        if missing:
            raise ConfigError(f"Provider #{i + 1}: missing fields: {', '.join(missing)}")

        protocol = entry["protocol"]
        if protocol not in VALID_PROTOCOLS:
            raise ConfigError(
                f"Provider #{i + 1}: invalid protocol '{protocol}', "
                f"must be one of: {', '.join(sorted(VALID_PROTOCOLS))}"
            )

        provider_type = entry.get("provider_type", "standard")
        if provider_type not in VALID_PROVIDER_TYPES:
            raise ConfigError(
                f"Provider #{i + 1}: invalid provider_type '{provider_type}', "
                f"must be one of: {', '.join(sorted(VALID_PROVIDER_TYPES))}"
            )

        # 默认为 0（"未设置"）而非硬编码的 window 值：0 会让
        # ProviderConfig.get_context_window() 走四层回退链解析
        #（自动拉取 / 映射表 / 默认值）。配置中显式指定的值仍须为正整数，
        # 且作为最高优先级覆盖。
        context_window = entry.get("context_window", 0)
        if not isinstance(context_window, int) or isinstance(context_window, bool) or context_window < 0:
            raise ConfigError(
                f"Provider #{i + 1}: context_window must be a positive integer"
            )

        thinking = entry.get("thinking", False)
        if not isinstance(thinking, bool):
            raise ConfigError(f"Provider #{i + 1}: thinking must be a boolean")

        context_channel = entry.get("context_channel", "auto")
        if context_channel not in VALID_CONTEXT_CHANNELS:
            raise ConfigError(
                f"Provider #{i + 1}: context_channel must be one of: "
                + ", ".join(sorted(VALID_CONTEXT_CHANNELS))
            )

        max_output_tokens = entry.get("max_output_tokens", 0)
        if not isinstance(max_output_tokens, int) or max_output_tokens < 0:
            raise ConfigError(
                f"Provider #{i + 1}: max_output_tokens must be a non-negative integer"
            )

        service_tier = entry.get("service_tier", "")
        valid_service_tiers = {
            "",
            "auto",
            "default",
            "fast",
            "flex",
            "priority",
            "scale",
            "ultrafast",
        }
        if not isinstance(service_tier, str) or service_tier not in valid_service_tiers:
            raise ConfigError(
                f"Provider #{i + 1}: service_tier must be one of: "
                + ", ".join(sorted(tier for tier in valid_service_tiers if tier))
            )

        usage = validate_usage_config(entry.get("usage"), provider_index=i + 1)

        providers.append(
            {
                "name": entry["name"],
                "protocol": protocol,
                "provider_type": provider_type,
                "base_url": entry["base_url"],
                "model": entry["model"],
                "api_key": entry.get("api_key", ""),
                "thinking": thinking,
                "context_channel": context_channel,
                "context_window": context_window,
                "max_output_tokens": max_output_tokens,
                "service_tier": service_tier,
                "usage": usage,
            }
        )

    return providers


def validate_usage_config(raw: object, *, provider_index: int) -> dict:
    """Validate an optional provider-level usage tracking block."""

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"Provider #{provider_index}: usage must be a mapping")

    enabled = raw.get("enabled", True)
    account_tracking = raw.get("account_tracking", False)
    if not isinstance(enabled, bool):
        raise ConfigError(
            f"Provider #{provider_index}: usage.enabled must be a boolean"
        )
    if not isinstance(account_tracking, bool):
        raise ConfigError(
            f"Provider #{provider_index}: usage.account_tracking must be a boolean"
        )

    attribution_mode = raw.get("attribution_mode", "auto")
    if attribution_mode not in VALID_USAGE_ATTRIBUTION_MODES:
        raise ConfigError(
            f"Provider #{provider_index}: usage.attribution_mode must be one of: "
            + ", ".join(sorted(VALID_USAGE_ATTRIBUTION_MODES))
        )

    def string_field(name: str, default: str = "") -> str:
        value = raw.get(name, default)
        if not isinstance(value, str):
            raise ConfigError(
                f"Provider #{provider_index}: usage.{name} must be a string"
            )
        return value

    def string_list(name: str) -> list[str]:
        value = raw.get(name, [])
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item for item in value
        ):
            raise ConfigError(
                f"Provider #{provider_index}: usage.{name} must be a list of non-empty strings"
            )
        return list(value)

    admin_base_url = string_field(
        "admin_base_url", "https://api.openai.com/v1"
    )
    if not admin_base_url.startswith(("https://", "http://")):
        raise ConfigError(
            f"Provider #{provider_index}: usage.admin_base_url must be an HTTP(S) URL"
        )

    return {
        "enabled": enabled,
        "account_tracking": account_tracking,
        "admin_api_key": string_field("admin_api_key"),
        "admin_api_key_env": string_field(
            "admin_api_key_env", "OPENAI_ADMIN_KEY"
        ),
        "admin_base_url": admin_base_url,
        "upstream_organization_id": string_field("upstream_organization_id"),
        "project_ids": string_list("project_ids"),
        "api_key_ids": string_list("api_key_ids"),
        "attribution_mode": attribution_mode,
    }


def validate_permission_mode(mode: str) -> str:
    """校验 permission_mode 取值。"""
    if mode not in VALID_PERMISSION_MODES:
        raise ConfigError(
            f"Invalid permission_mode '{mode}', "
            f"must be one of: {', '.join(sorted(VALID_PERMISSION_MODES))}"
        )
    return mode


def validate_mcp_servers(raw_mcp: list | dict | None) -> list[dict]:
    """校验 mcp_servers 配置段，返回清洗后的 server 配置字典列表。"""
    if raw_mcp is None:
        return []

    # Accept named-server mappings as well as the canonical list syntax.
    if isinstance(raw_mcp, dict):
        entries = []
        for name, entry in raw_mcp.items():
            if not isinstance(entry, dict):
                raise ConfigError(f"MCP server '{name}': must be a mapping")
            if entry.get("name", name) != name:
                raise ConfigError(f"MCP server '{name}': conflicting name")
            entries.append({**entry, "name": name})
        raw_mcp = entries

    if not isinstance(raw_mcp, list):
        raise ConfigError("'mcp_servers' must be a list of server configs")

    servers: list[dict] = []
    for i, entry in enumerate(raw_mcp):
        if not isinstance(entry, dict):
            raise ConfigError(f"MCP server #{i + 1}: must be a mapping")
        name = entry.get("name")
        if not name:
            raise ConfigError(f"MCP server #{i + 1}: missing 'name'")
        has_command = "command" in entry
        has_url = "url" in entry
        if has_command and has_url:
            raise ConfigError(
                f"MCP server '{name}': cannot have both 'command' and 'url'"
            )
        if not has_command and not has_url:
            raise ConfigError(
                f"MCP server '{name}': must have either 'command' or 'url'"
            )
        servers.append(
            {
                "name": name,
                "command": entry.get("command"),
                "args": entry.get("args", []),
                "url": entry.get("url"),
                "headers": entry.get("headers", {}),
                "env": entry.get("env", {}),
            }
        )

    return servers


def validate_hooks(raw_hooks: list | None) -> list:
    """校验 hooks 配置段。"""
    if raw_hooks is None:
        return []
    if not isinstance(raw_hooks, list):
        raise ConfigError("'hooks' must be a list of hook definitions")
    return raw_hooks


def validate_bool_field(value: object, field_name: str) -> bool:
    """校验一个布尔类型的配置字段。"""
    if not isinstance(value, bool):
        raise ConfigError(f"'{field_name}' must be a boolean")
    return value


def validate_worktree(raw_wt: dict | None) -> dict:
    """校验 worktree 配置段，返回清洗后的配置字典。"""
    defaults = {
        "symlink_directories": ["node_modules", ".venv", "vendor"],
        "stale_cleanup_interval": 3600,
        "stale_cutoff_hours": 24,
    }

    if raw_wt is None:
        return defaults

    if not isinstance(raw_wt, dict):
        raise ConfigError("'worktree' must be a mapping")

    sym = raw_wt.get("symlink_directories", defaults["symlink_directories"])
    if not isinstance(sym, list) or not all(isinstance(s, str) for s in sym):
        raise ConfigError("'worktree.symlink_directories' must be a list of strings")

    interval = raw_wt.get("stale_cleanup_interval", defaults["stale_cleanup_interval"])
    if not isinstance(interval, int) or interval <= 0:
        raise ConfigError("'worktree.stale_cleanup_interval' must be a positive integer")

    cutoff = raw_wt.get("stale_cutoff_hours", defaults["stale_cutoff_hours"])
    if not isinstance(cutoff, int) or cutoff <= 0:
        raise ConfigError("'worktree.stale_cutoff_hours' must be a positive integer")

    return {
        "symlink_directories": sym,
        "stale_cleanup_interval": interval,
        "stale_cutoff_hours": cutoff,
    }


def validate_teammate_mode(mode: object) -> str:
    """校验 teammate_mode 取值。"""
    if not isinstance(mode, str) or mode not in VALID_TEAMMATE_MODES:
        raise ConfigError(
            f"Invalid teammate_mode '{mode}', "
            f"must be one of: {', '.join(repr(m) for m in sorted(VALID_TEAMMATE_MODES))}"
        )
    return mode


def validate_teammate_permission_mode(mode: object) -> str:
    """校验 teammate_permission_mode 取值。"""
    if not isinstance(mode, str) or mode not in VALID_TEAMMATE_PERMISSION_MODES:
        raise ConfigError(
            f"Invalid teammate_permission_mode '{mode}', "
            f"must be one of: "
            f"{', '.join(repr(m) for m in sorted(VALID_TEAMMATE_PERMISSION_MODES))}"
        )
    return mode


def validate_evolution(raw_evo: dict | None) -> dict:
    """校验 evolution 配置段，返回清洗后的配置字典。"""
    defaults: dict = {
        "enabled": False,
        "min_traces_trigger": 30,
        "max_traces_per_evolution": 50,
        "min_traces_per_evolution": 30,
        "min_failure_recurrence": 3,
        "token_increase_threshold": 0.15,
        "deprecation_task_threshold": 60,
        # 成功经验路径
        "success_enabled": False,
        "success_iteration_threshold": 8,
        "success_tool_call_threshold": 10,
        "success_promotion_recurrence": 2,
        "success_match_enabled": True,
        "success_match_timeout": 8.0,
        "success_baseline_samples": 5,
        "success_iteration_reduction_threshold": 0.20,
        "success_hit_failure_threshold": 3,
    }

    if raw_evo is None:
        return defaults

    if not isinstance(raw_evo, dict):
        raise ConfigError("'evolution' must be a mapping")

    # 兼容嵌套 success 子映射：evolution.success.<key> → evolution.success_<key>
    nested_success = raw_evo.get("success")
    if isinstance(nested_success, dict):
        for k, v in nested_success.items():
            flat_key = f"success_{k}"
            if flat_key in defaults and flat_key not in raw_evo:
                raw_evo = {**raw_evo, flat_key: v}

    result: dict = {}
    for key, default_val in defaults.items():
        val = raw_evo.get(key, default_val)
        if key in ("min_traces_trigger", "max_traces_per_evolution",
                    "min_traces_per_evolution", "min_failure_recurrence",
                    "deprecation_task_threshold",
                    "success_iteration_threshold", "success_tool_call_threshold",
                    "success_promotion_recurrence", "success_baseline_samples",
                    "success_hit_failure_threshold"):
            if not isinstance(val, int) or val <= 0:
                raise ConfigError(f"'evolution.{key}' must be a positive integer, got {val!r}")
        elif key in ("token_increase_threshold", "success_iteration_reduction_threshold"):
            if not isinstance(val, (int, float)) or val <= 0 or val > 1:
                raise ConfigError(
                    f"'evolution.{key}' must be a float between 0 and 1, got {val!r}"
                )
        elif key == "success_match_timeout":
            if not isinstance(val, (int, float)) or val <= 0:
                raise ConfigError(f"'evolution.{key}' must be a positive number, got {val!r}")
        elif key in ("enabled", "success_enabled", "success_match_enabled"):
            if not isinstance(val, bool):
                raise ConfigError(f"'evolution.{key}' must be a boolean, got {val!r}")
        result[key] = val

    return result


def validate_pipeline(raw_pipeline: dict | None) -> dict:
    """校验 pipeline 配置段（自动挡模型档位 + 重试/熔断参数），返回清洗后的配置字典。"""
    defaults: dict = {
        "auto_heavy_model": None,
        "auto_light_model": None,
    }
    # 整型参数：(默认值, 最小值, 最大值)。None = 未配置（用代码默认）
    int_params: dict[str, tuple[int, int]] = {
        "granule_retries": (0, 10),
        "network_breaker_threshold": (1, 50),
        "max_inline_output_chars": (1_000, 1_000_000),
    }

    if raw_pipeline is None:
        result = dict(defaults)
        for key in int_params:
            result[key] = None
        return result

    if not isinstance(raw_pipeline, dict):
        raise ConfigError("'pipeline' must be a mapping")

    result: dict = {}
    for key, default_val in defaults.items():
        val = raw_pipeline.get(key, default_val)
        if val is not None and (not isinstance(val, str) or not val.strip()):
            raise ConfigError(
                f"'pipeline.{key}' must be a non-empty string or null, got {val!r}"
            )
        result[key] = val

    for key, (lo, hi) in int_params.items():
        val = raw_pipeline.get(key)
        if val is None:
            result[key] = None
            continue
        if isinstance(val, bool) or not isinstance(val, int) or not (lo <= val <= hi):
            raise ConfigError(
                f"'pipeline.{key}' must be an integer in [{lo}, {hi}], got {val!r}"
            )
        result[key] = val

    return result


def validate_redis(raw_redis: dict | None) -> dict:
    defaults = {
        "enabled": False,
        "url": "redis://127.0.0.1:6379/0",
        "key_prefix": "mewcode",
        "context_ttl_seconds": 604_800,
        "recall_ttl_seconds": 300,
        "max_messages": 200,
        "socket_timeout_seconds": 0.5,
    }
    if raw_redis is None:
        return {**defaults, "configured": False}
    if not isinstance(raw_redis, dict):
        raise ConfigError("'redis' must be a mapping")

    result = dict(defaults)
    result.update(raw_redis)
    result["configured"] = True

    if not isinstance(result["enabled"], bool):
        raise ConfigError("'redis.enabled' must be a boolean")
    for key in ("url", "key_prefix"):
        if not isinstance(result[key], str) or not result[key].strip():
            raise ConfigError(f"'redis.{key}' must be a non-empty string")
    for key, lower, upper in (
        ("context_ttl_seconds", 60, 2_592_000),
        ("recall_ttl_seconds", 1, 86_400),
        ("max_messages", 10, 2_000),
    ):
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
            raise ConfigError(
                f"'redis.{key}' must be an integer in [{lower}, {upper}]"
            )
    timeout = result["socket_timeout_seconds"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 0.05 <= float(timeout) <= 10.0
    ):
        raise ConfigError(
            "'redis.socket_timeout_seconds' must be a number in [0.05, 10.0]"
        )
    result["socket_timeout_seconds"] = float(timeout)
    return result


def validate_tailscale(raw_tailscale: dict | None) -> dict:
    """Validate opt-in, read-only Tailscale discovery and peer settings."""
    defaults = {
        "enabled": True,
        "cli_path": "tailscale",
        "timeout_seconds": 2.0,
        "expose_addresses": False,
        "auto_discover": True,
        "probe_ports": [443, 8765],
        "probe_timeout_seconds": 1.5,
        "remote_nodes": [],
        "remote_token_env": "MEWCODE_REMOTE_TOKEN",
    }
    if raw_tailscale is None:
        return {**defaults, "configured": False}
    if not isinstance(raw_tailscale, dict):
        raise ConfigError("'tailscale' must be a mapping")

    result = dict(defaults)
    result.update(raw_tailscale)
    result["configured"] = True
    if not isinstance(result["enabled"], bool):
        raise ConfigError("'tailscale.enabled' must be a boolean")
    cli_path = result["cli_path"]
    if not isinstance(cli_path, str) or not cli_path.strip():
        raise ConfigError("'tailscale.cli_path' must be a non-empty string")
    if len(cli_path) > 260:
        raise ConfigError("'tailscale.cli_path' must be at most 260 characters")
    timeout = result["timeout_seconds"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 0.2 <= float(timeout) <= 10.0
    ):
        raise ConfigError("'tailscale.timeout_seconds' must be a number in [0.2, 10.0]")
    result["timeout_seconds"] = float(timeout)
    if not isinstance(result["expose_addresses"], bool):
        raise ConfigError("'tailscale.expose_addresses' must be a boolean")
    if not isinstance(result["auto_discover"], bool):
        raise ConfigError("'tailscale.auto_discover' must be a boolean")
    ports = result["probe_ports"]
    if not isinstance(ports, list) or not ports or len(ports) > 8:
        raise ConfigError("'tailscale.probe_ports' must contain 1-8 ports")
    normalized_ports: list[int] = []
    for port in ports:
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ConfigError("'tailscale.probe_ports' contains an invalid port")
        if port not in normalized_ports:
            normalized_ports.append(port)
    result["probe_ports"] = normalized_ports
    probe_timeout = result["probe_timeout_seconds"]
    if (
        isinstance(probe_timeout, bool)
        or not isinstance(probe_timeout, (int, float))
        or not 0.2 <= float(probe_timeout) <= 5.0
    ):
        raise ConfigError("'tailscale.probe_timeout_seconds' must be a number in [0.2, 5.0]")
    result["probe_timeout_seconds"] = float(probe_timeout)

    nodes = result["remote_nodes"]
    if not isinstance(nodes, list) or len(nodes) > 64:
        raise ConfigError("'tailscale.remote_nodes' must be a list with at most 64 entries")
    normalized_nodes: list[dict[str, str]] = []
    for item in nodes:
        if not isinstance(item, dict):
            raise ConfigError("each 'tailscale.remote_nodes' entry must be a mapping")
        node_id = item.get("id")
        url = item.get("url")
        token_env = item.get("token_env", result["remote_token_env"])
        if (
            not isinstance(node_id, str)
            or not node_id
            or len(node_id) > 120
            or not re.fullmatch(r"[A-Za-z0-9_.:-]+", node_id)
        ):
            raise ConfigError("remote node id is invalid")
        if (
            not isinstance(url, str)
            or len(url) > 300
            or not re.fullmatch(r"https?://[^\s/]+(?::\d+)?(?:/[^\s]*)?", url)
        ):
            raise ConfigError("remote node url must be an explicit http(s) URL")
        try:
            parsed_url = urlsplit(url)
            host = (parsed_url.hostname or "").rstrip(".").lower()
        except ValueError as exc:
            raise ConfigError("remote node url is malformed") from exc
        allowed_host = host.endswith(".ts.net")
        if not allowed_host:
            try:
                address = ipaddress.ip_address(host)
                allowed_host = (
                    address in ipaddress.ip_network("100.64.0.0/10")
                    or address in ipaddress.ip_network("fd7a:115c:a1e0::/48")
                )
            except ValueError:
                allowed_host = False
        if not allowed_host or (parsed_url.scheme != "https" and not host.startswith("100.")):
            raise ConfigError("remote node url must target a Tailscale *.ts.net name or tailnet IP")
        if not isinstance(token_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", token_env):
            raise ConfigError("remote node token_env is invalid")
        normalized_nodes.append({"id": node_id, "url": url.rstrip("/"), "token_env": token_env})
    result["remote_nodes"] = normalized_nodes
    token_env = result["remote_token_env"]
    if not isinstance(token_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", token_env):
        raise ConfigError("'tailscale.remote_token_env' is invalid")
    return result


def validate_config_structure(raw: object) -> dict:
    """校验的主入口。校验解析后的原始配置，返回清洗后的字典。

    返回的字典包含以下键：
        providers、permission_mode、mcp_servers、hooks、
        enable_fork、enable_verification_agent、worktree、
        teammate_mode、enable_coordinator_mode、
        allow_self_modification、allow_self_evolution、evolution、pipeline
    """
    if not isinstance(raw, dict) or "providers" not in raw:
        raise ConfigError("Config must contain a 'providers' list")

    return {
        "providers": validate_providers(raw["providers"]),
        "permission_mode": validate_permission_mode(raw.get("permission_mode", "default")),
        "mcp_servers": validate_mcp_servers(raw.get("mcp_servers")),
        "hooks": validate_hooks(raw.get("hooks")),
        "enable_fork": validate_bool_field(raw.get("enable_fork", False), "enable_fork"),
        "enable_verification_agent": validate_bool_field(
            raw.get("enable_verification_agent", False), "enable_verification_agent"
        ),
        "worktree": validate_worktree(raw.get("worktree")),
        "teammate_mode": validate_teammate_mode(raw.get("teammate_mode", "")),
        "teammate_permission_mode": validate_teammate_permission_mode(
            raw.get("teammate_permission_mode", "")
        ),
        "enable_coordinator_mode": validate_bool_field(
            raw.get("enable_coordinator_mode", False), "enable_coordinator_mode"
        ),
        "allow_self_modification": validate_bool_field(
            raw.get("allow_self_modification", False), "allow_self_modification"
        ),
        "allow_self_evolution": validate_bool_field(
            raw.get("allow_self_evolution", False), "allow_self_evolution"
        ),
        "evolution": validate_evolution(raw.get("evolution")),
        "pipeline": validate_pipeline(raw.get("pipeline")),
        "redis": validate_redis(raw.get("redis")),
        "tailscale": validate_tailscale(raw.get("tailscale")),
    }
