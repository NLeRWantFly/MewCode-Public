from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .validator import (
    ConfigError,
    DEFAULT_CONTEXT_WINDOW,
    VALID_PERMISSION_MODES,
    VALID_PROTOCOLS,
    VALID_TEAMMATE_MODES,
    lookup_model_context_window,
    validate_config_structure,
)


_ENV_KEY_MAP = {
    "typesafe": "TYPESAFE_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openai-compat": "OPENAI_API_KEY",
}

_PROVIDER_TYPE_ENV_KEY_MAP = {
    "opencode-go": "OPENCODE_API_KEY",
}

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


@dataclass
class UsageConfig:
    """Request-ledger and upstream account tracking settings."""

    enabled: bool = True
    account_tracking: bool = False
    admin_api_key: str = ""
    admin_api_key_env: str = "OPENAI_ADMIN_KEY"
    admin_base_url: str = "https://api.openai.com/v1"
    upstream_organization_id: str = ""
    project_ids: list[str] = field(default_factory=list)
    api_key_ids: list[str] = field(default_factory=list)
    attribution_mode: str = "auto"

    def resolve_admin_api_key(self) -> str:
        if self.admin_api_key:
            resolved = resolve_env_vars(self.admin_api_key)
            if not _ENV_VAR_RE.fullmatch(resolved):
                return resolved
        return os.environ.get(self.admin_api_key_env, "")


@dataclass
class ProviderConfig:
    name: str
    protocol: str
    base_url: str
    model: str
    api_key: str = ""
    thinking: bool = False
    context_channel: str = "auto"
    # 0 表示"未设置" — get_context_window() 通过四层 fallback 解析真实窗口大小。
    # 正数表示配置文件里显式指定的覆盖值。
    context_window: int = 0
    max_output_tokens: int = 0
    service_tier: str = ""
    default_headers: dict[str, str] = field(default_factory=dict)
    usage: UsageConfig = field(default_factory=UsageConfig)
    provider_type: str = "standard"
    # 运行时 cache，存放从 provider 的 /v1/models 端点自动拉取的 context window
    # （get_context_window 的第 2 层）。通过 set_fetched_context_window() 写入一次；
    # 0 表示"尚未拉取"。不会持久化。
    _fetched_context_window: int = field(default=0, repr=False)
    _route_context_window: int = field(default=0, repr=False)
    # Runtime-only binding supplied by Codex discovery. Never infer a credential
    # source for manually configured providers or relay endpoints.
    _codex_auth_path: str = field(default="", repr=False)

    def resolve_api_key(self) -> str:
        if self.api_key:
            resolved = resolve_env_vars(self.api_key)
            if not _ENV_VAR_RE.search(resolved):
                return resolved
        env_var = _PROVIDER_TYPE_ENV_KEY_MAP.get(
            self.provider_type, _ENV_KEY_MAP.get(self.protocol, "")
        )
        return os.environ.get(env_var, "")

    def set_fetched_context_window(self, window: int) -> None:
        """记录从 provider 自动拉取到的 context window（第 2 层）。

        非正数会被忽略，这样一次失败的拉取就不会污染 cache。在解析
        context window 时，每个 provider 只会调用一次。
        """
        if window > 0:
            self._fetched_context_window = window

    def get_context_window(self) -> int:
        """通过多层 fallback 解析模型的 context window，按优先级从高到低：

          1. 配置文件提供的 context_window（> 0）——显式覆盖，永远优先。
          2. OpenCode Go 等路由表写入的 _route_context_window。
          3. 从 provider 的 /v1/models 端点自动拉取并通过 set_fetched_context_window
             缓存的值（只有 anthropic 协议的 provider 才会设置它；拉取失败或缺失时
             保持为 0 并跳过）。
          4. 内置模型目录中已核对的官方窗口。
          5. DeepSeek / Grok 等能力表。
          6. 内置的「模型名 -> window」映射表（按子串匹配）。
          7. 保守的默认值（claude -> 200000，其他 -> 128000）。
        """
        if self.context_window > 0:
            return self.context_window
        if self.protocol == "typesafe":
            return 32_000  # State + longest question; total question fan-out is 64k.
        if self._route_context_window > 0:
            return self._route_context_window
        if self._fetched_context_window > 0:
            return self._fetched_context_window
        from .model_catalog import DEFAULT_MODEL_CATALOG

        descriptor = DEFAULT_MODEL_CATALOG.describe(self.model)
        if descriptor is not None and descriptor.context_window > 0:
            return descriptor.context_window
        from .model_capabilities import resolve_model_capabilities

        capabilities = resolve_model_capabilities(
            self.model, self.base_url, self.context_channel
        )
        if capabilities.context_window > 0:
            return capabilities.context_window
        window = lookup_model_context_window(self.model)
        if window > 0:
            return window
        if "claude" in self.model.lower():
            return DEFAULT_CONTEXT_WINDOW
        return 128_000

    def get_max_output_tokens(self) -> int:
        if self.max_output_tokens > 0:
            return self.max_output_tokens
        if self.thinking:
            return 64000
        # Always-reasoning models (deepseek v4 on relays, etc.) spend the same
        # output budget on reasoning_content even when `thinking` is unset; a
        # flat 8192 lets reasoning consume everything and returns empty text.
        from .model_capabilities import resolve_model_capabilities

        capabilities = resolve_model_capabilities(
            self.model, self.base_url, self.context_channel
        )
        if not capabilities.supports_thinking:
            return 8192
        if capabilities.max_output_tokens > 0:
            return min(64000, capabilities.max_output_tokens)
        return 64000


def resolve_env_vars(value: str) -> str:
    return _ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), value)


def build_child_env(declared_env: dict[str, str] | None) -> dict[str, str]:
    env: dict[str, str] = {}
    path = os.environ.get("PATH", "")
    if path:
        env["PATH"] = path
    for key, value in (declared_env or {}).items():
        env[key] = resolve_env_vars(value)
    return env


@dataclass
class MCPServerConfig:
    name: str
    command: str | None = None
    args: list[str] = field(default_factory=list)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)


    @property
    def is_stdio(self) -> bool:
        return self.command is not None


@dataclass
class WorktreeConfig:
    symlink_directories: list[str] = field(default_factory=lambda: ["node_modules", ".venv", "vendor"])
    stale_cleanup_interval: int = 3600
    stale_cutoff_hours: int = 24


@dataclass
class CompactConfig:
    utilization_threshold: float = 0.85
    min_keep_messages: int = 3


@dataclass
class CriticConfig:
    enabled: bool = False


@dataclass
class RateLimitConfig:
    enabled: bool = True
    default_max_per_minute: int = 30
    per_tool: dict[str, int] = field(default_factory=lambda: {"Bash": 10, "WriteFile": 20})


@dataclass
class EvolutionConfig:
    """自进化子系统配置。"""

    enabled: bool = False
    min_traces_trigger: int = 30
    max_traces_per_evolution: int = 50
    min_traces_per_evolution: int = 30
    min_failure_recurrence: int = 3
    token_increase_threshold: float = 0.15
    deprecation_task_threshold: int = 60
    backup_dir: str = "harness/backup"
    traces_dir: str = "harness/traces"
    skills_dir: str = "harness/skills"
    skill_meta_file: str = "harness/skills/skill_meta.json"
    # 成功经验路径配置
    success_enabled: bool = False
    success_iteration_threshold: int = 8  # 迭代数 ≥ 此值视为复杂
    success_tool_call_threshold: int = 10  # 工具调用数 ≥ 此值视为复杂
    success_promotion_recurrence: int = 2  # 同类成功复发达此值晋升正式
    success_match_enabled: bool = True  # 任务开始时是否做 Skill 注入匹配
    success_match_timeout: float = 8.0  # 匹配侧路调用超时（秒）
    success_baseline_samples: int = 5  # 降本评估的历史基线样本数
    success_iteration_reduction_threshold: float = 0.20  # 迭代降幅阈值
    success_hit_failure_threshold: int = 3  # 命中失败累计达此值降级


@dataclass
class PipelineConfig:
    """Pipeline 自动挡档位配置。

    重/轻两档模型名由用户在 config 的 pipeline 节指定；
    均未配置时由内置分档表兜底（模型名含 "opus" 归重档、
    含 "haiku" 归轻档，见 pipeline/models.py 的 BUILTIN_MODEL_TIERS）。
    """

    auto_heavy_model: str | None = None
    """重档模型名（复杂任务）。None = 未配置，走内置分档表兜底。"""
    auto_light_model: str | None = None
    """轻档模型名（简单任务与管线辅助调用）。None = 未配置。"""
    granule_retries: int | None = None
    """任务子步骤瞬时错误重试次数。None = 未配置（用代码默认 2）。"""
    network_breaker_threshold: int | None = None
    """连续网络失败熔断阈值：达到后 run 标记为 interrupted（可断点续跑）
    而非继续烧重试。None = 未配置（用代码默认 6）。"""
    max_inline_output_chars: int | None = None
    """Pipeline prompt 单变量最大内联字符数。None = 使用代码默认 8000；
    超限值以预览 + 落盘路径注入。"""


@dataclass
class RedisConfig:
    enabled: bool = False
    url: str = "redis://127.0.0.1:6379/0"
    key_prefix: str = "mewcode"
    context_ttl_seconds: int = 604_800
    recall_ttl_seconds: int = 300
    max_messages: int = 200
    socket_timeout_seconds: float = 0.5
    configured: bool = field(default=False, repr=False)


@dataclass
class TailscaleConfig:
    """Read-only Tailscale node discovery settings.

    The web service uses these values only for bounded Tailscale status and
    allow-listed peer requests; remote command execution is out of scope. If
    the CLI is not installed the collector reports ``available: false``.
    """

    # Read-only discovery is enabled by default; setting ``enabled: false``
    # restores the previous opt-out behavior.
    enabled: bool = True
    cli_path: str = "tailscale"
    timeout_seconds: float = 2.0
    expose_addresses: bool = False
    auto_discover: bool = True
    probe_ports: list[int] = field(default_factory=lambda: [443, 8765])
    probe_timeout_seconds: float = 1.5
    # Explicitly allow-listed MewCode peers. Each item is {id, url, token_env}.
    remote_nodes: list[dict[str, str]] = field(default_factory=list)
    remote_token_env: str = "MEWCODE_REMOTE_TOKEN"
    configured: bool = field(default=False, repr=False)


@dataclass
class AppConfig:
    providers: list[ProviderConfig]
    permission_mode: str = "default"
    permission_mode_explicit: bool = field(default=False, repr=False, kw_only=True)
    mcp_servers: list[MCPServerConfig] = field(default_factory=list)
    raw_hooks: list[dict] = field(default_factory=list)
    enable_fork: bool = False
    enable_verification_agent: bool = False
    worktree: WorktreeConfig = field(default_factory=WorktreeConfig)
    teammate_mode: str = ""
    # teammate 的权限模式。"" 表示未配置（等效 dontAsk 全自动）；可显式
    # 设为 "dontAsk" / "acceptEdits"（编辑放行、命令走规则否则安全拒绝）/
    # "default" / "inherit"（继承主会话）。用空值作未配置哨兵，显式写的
    # 值在多层配置合并时任意方向都能覆盖。teammate 是后台运行的，"ask"
    # 会被安全拒绝而非挂起；无论哪种模式，DangerousCommandDetector 与
    # 用户 deny 规则始终生效
    teammate_permission_mode: str = ""
    enable_coordinator_mode: bool = False
    # Harness Engineering 新增
    compact: CompactConfig = field(default_factory=CompactConfig)
    critic: CriticConfig = field(default_factory=CriticConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    allow_self_modification: bool = False
    # Evolution Engineering 新增
    allow_self_evolution: bool = False
    evolution: EvolutionConfig = field(default_factory=EvolutionConfig)
    # Pipeline 自动挡新增
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    tailscale: TailscaleConfig = field(default_factory=TailscaleConfig)


def _load_single_file(path: Path) -> AppConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"Failed to parse config {path}: {e}") from e

    validated = validate_config_structure(raw)

    providers = [
        ProviderConfig(
            name=p["name"],
            protocol=p["protocol"],
            base_url=p["base_url"],
            model=p["model"],
            provider_type=p["provider_type"],
            api_key=p["api_key"],
            thinking=p["thinking"],
            context_channel=p["context_channel"],
            context_window=p["context_window"],
            max_output_tokens=p["max_output_tokens"],
            service_tier=p["service_tier"],
            usage=UsageConfig(**p["usage"]),
        )
        for p in validated["providers"]
    ]

    mcp_servers = [
        MCPServerConfig(
            name=s["name"],
            command=s["command"],
            args=s["args"],
            url=s["url"],
            headers=s["headers"],
            env=s["env"],
        )
        for s in validated["mcp_servers"]
    ]

    wt = validated["worktree"]
    worktree_cfg = WorktreeConfig(
        symlink_directories=wt["symlink_directories"],
        stale_cleanup_interval=wt["stale_cleanup_interval"],
        stale_cutoff_hours=wt["stale_cutoff_hours"],
    )

    return AppConfig(
        providers=providers,
        permission_mode=validated["permission_mode"],
        permission_mode_explicit="permission_mode" in raw,
        mcp_servers=mcp_servers,
        raw_hooks=validated["hooks"],
        enable_fork=validated["enable_fork"],
        enable_verification_agent=validated["enable_verification_agent"],
        worktree=worktree_cfg,
        teammate_mode=validated["teammate_mode"],
        teammate_permission_mode=validated["teammate_permission_mode"],
        enable_coordinator_mode=validated["enable_coordinator_mode"],
        # Harness Engineering 新增
        compact=CompactConfig(
            utilization_threshold=validated.get("compact", {}).get("utilization_threshold", 0.85),
            min_keep_messages=validated.get("compact", {}).get("min_keep_messages", 3),
        ),
        critic=CriticConfig(
            enabled=validated.get("critic", {}).get("enabled", False),
        ),
        rate_limit=RateLimitConfig(
            enabled=validated.get("rate_limit", {}).get("enabled", True),
            default_max_per_minute=validated.get("rate_limit", {}).get("default_max_per_minute", 30),
            per_tool=validated.get("rate_limit", {}).get("per_tool", {"Bash": 10, "WriteFile": 20}),
        ),
        allow_self_modification=validated.get("allow_self_modification", False),
        allow_self_evolution=validated.get("allow_self_evolution", False),
        evolution=EvolutionConfig(
            enabled=validated.get("evolution", {}).get("enabled", False),
            min_traces_trigger=validated.get("evolution", {}).get("min_traces_trigger", 30),
            max_traces_per_evolution=validated.get("evolution", {}).get("max_traces_per_evolution", 50),
            min_traces_per_evolution=validated.get("evolution", {}).get("min_traces_per_evolution", 30),
            min_failure_recurrence=validated.get("evolution", {}).get("min_failure_recurrence", 3),
            token_increase_threshold=validated.get("evolution", {}).get("token_increase_threshold", 0.15),
            deprecation_task_threshold=validated.get("evolution", {}).get("deprecation_task_threshold", 60),
            success_enabled=validated.get("evolution", {}).get("success_enabled", False),
            success_iteration_threshold=validated.get("evolution", {}).get("success_iteration_threshold", 8),
            success_tool_call_threshold=validated.get("evolution", {}).get("success_tool_call_threshold", 10),
            success_promotion_recurrence=validated.get("evolution", {}).get("success_promotion_recurrence", 2),
            success_match_enabled=validated.get("evolution", {}).get("success_match_enabled", True),
            success_match_timeout=validated.get("evolution", {}).get("success_match_timeout", 8.0),
            success_baseline_samples=validated.get("evolution", {}).get("success_baseline_samples", 5),
            success_iteration_reduction_threshold=validated.get("evolution", {}).get("success_iteration_reduction_threshold", 0.20),
            success_hit_failure_threshold=validated.get("evolution", {}).get("success_hit_failure_threshold", 3),
        ),
        # Pipeline 自动挡新增
        pipeline=PipelineConfig(
            auto_heavy_model=validated.get("pipeline", {}).get("auto_heavy_model"),
            auto_light_model=validated.get("pipeline", {}).get("auto_light_model"),
            granule_retries=validated.get("pipeline", {}).get("granule_retries"),
            network_breaker_threshold=validated.get("pipeline", {}).get("network_breaker_threshold"),
            max_inline_output_chars=validated.get("pipeline", {}).get("max_inline_output_chars"),
        ),
        redis=RedisConfig(**validated["redis"]),
        # Older validators/config layers may not expose tailscale yet; use the
        # disabled default until the validation layer is migrated.
        tailscale=TailscaleConfig(**validated.get("tailscale", {})),
    )


def _merge_config(base: AppConfig, override: AppConfig) -> AppConfig:
    if override.providers:
        base.providers = override.providers
    if override.permission_mode_explicit or override.permission_mode != "default":
        base.permission_mode = override.permission_mode
        base.permission_mode_explicit = override.permission_mode_explicit

    if override.mcp_servers:
        by_name = {s.name: i for i, s in enumerate(base.mcp_servers)}
        for s in override.mcp_servers:
            if s.name in by_name:
                base.mcp_servers[by_name[s.name]] = s
            else:
                base.mcp_servers.append(s)
                by_name[s.name] = len(base.mcp_servers) - 1

    base.raw_hooks.extend(override.raw_hooks)
    if override.enable_fork:
        base.enable_fork = True
    if override.enable_verification_agent:
        base.enable_verification_agent = True
    if override.teammate_mode:
        base.teammate_mode = override.teammate_mode
    if override.teammate_permission_mode:
        base.teammate_permission_mode = override.teammate_permission_mode
    if override.enable_coordinator_mode:
        base.enable_coordinator_mode = True
    # pipeline 自动挡档位：项目层显式配置的模型名覆盖用户层
    if override.pipeline.auto_heavy_model is not None:
        base.pipeline.auto_heavy_model = override.pipeline.auto_heavy_model
    if override.pipeline.auto_light_model is not None:
        base.pipeline.auto_light_model = override.pipeline.auto_light_model
    if override.pipeline.granule_retries is not None:
        base.pipeline.granule_retries = override.pipeline.granule_retries
    if override.pipeline.network_breaker_threshold is not None:
        base.pipeline.network_breaker_threshold = override.pipeline.network_breaker_threshold
    if override.pipeline.max_inline_output_chars is not None:
        base.pipeline.max_inline_output_chars = override.pipeline.max_inline_output_chars
    if override.redis.configured:
        base.redis = override.redis
    if override.tailscale.configured:
        base.tailscale = override.tailscale
    return base


def load_config(path: Path | None = None) -> AppConfig:
    if path is not None:
        if not path.exists():
            raise ConfigError(f"Config file not found: {path}")
        return _load_single_file(path)

    cwd = Path.cwd()
    home = Path.home()
    candidates = [
        home / ".mewcode" / "config.yaml",
        cwd / ".mewcode" / "config.yaml",
        cwd / ".mewcode" / "config.local.yaml",
    ]

    merged: AppConfig | None = None
    for p in candidates:
        if not p.exists():
            continue
        layer = _load_single_file(p)
        if merged is None:
            merged = layer
        else:
            merged = _merge_config(merged, layer)

    if merged is None:
        raise ConfigError(
            "No config file found. Expected .mewcode/config.yaml "
            "in project or ~/.mewcode/config.yaml"
        )
    return merged
