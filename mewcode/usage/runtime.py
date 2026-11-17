"""Desktop account orchestration for normalized usage collection."""

from __future__ import annotations

import asyncio
import copy
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from mewcode.usage.adapters import UsageAdapterRegistry
from mewcode.usage.deepseek import DeepSeekPortalUsageAdapter
from mewcode.usage.ledger import UsageLedger
from mewcode.usage.models import (
    LocalUsageSummary,
    TokenUsage,
    UsageAccount,
    UsagePeriod,
    UsageRun,
    UsageRunStatus,
    utc_now,
)
from mewcode.usage.plugins import (
    PluginUsageAdapter,
    UsagePluginManager,
    UsagePluginRunner,
    validate_host_request,
)
from mewcode.runtime_scheduler import (
    GLOBAL_RUNTIME_SCHEDULER,
    current_runtime_scheduler,
)


UTC = timezone.utc
EventCallback = Callable[[dict[str, Any]], Awaitable[None]]
ProviderNameResolver = Callable[[str], str]


def current_month_period(now: datetime | None = None) -> UsagePeriod:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    current = current.astimezone(UTC)
    return UsagePeriod(
        start=datetime(current.year, current.month, 1, tzinfo=UTC),
        end=current,
        timezone="UTC",
        policy="current_month",
    )


class BrowserCommandBroker:
    def __init__(self) -> None:
        self._event_callback: EventCallback | None = None
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._pending_runs: dict[str, str] = {}

    def set_event_callback(self, callback: EventCallback | None) -> None:
        self._event_callback = callback

    async def request(
        self,
        run: UsageRun,
        manifest: dict[str, Any],
        account: UsageAccount,
        method: str,
        params: dict[str, Any],
        *,
        on_status: Callable[[UsageRunStatus, str, str], Awaitable[None]],
    ) -> Any:
        validated = validate_host_request(manifest, account, method, params)
        if method == "secret.resolve":
            return validated["secret"]
        if self._event_callback is None:
            raise RuntimeError("桌面浏览器桥尚未连接")
        command_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[command_id] = future
        self._pending_runs[command_id] = run.id
        is_interaction = method == "browser.pause_for_user"
        if is_interaction:
            await on_status(
                UsageRunStatus.ACTION_REQUIRED,
                str(validated.get("prompt") or "请在登录窗口中完成验证"),
                command_id,
            )
        await self._event_callback({
            "type": "usage_browser_command",
            "run_id": run.id,
            "account_id": account.id,
            "command_id": command_id,
            "method": method,
            "params": validated,
            "allowed_origins": list((manifest.get("permissions") or {}).get("origins") or []),
            "bound_provider_key": str(manifest.get("bound_provider_key") or ""),
            "interactive": is_interaction,
        })
        try:
            result = await asyncio.wait_for(
                future, timeout=600.0 if is_interaction else 60.0
            )
        finally:
            self._pending.pop(command_id, None)
            self._pending_runs.pop(command_id, None)
        if is_interaction:
            await on_status(UsageRunStatus.RUNNING, "登录已完成，继续采集", "")
        return result

    def resolve(self, command_id: str, *, result: Any = None, error: str = "") -> bool:
        future = self._pending.get(command_id)
        if future is None or future.done():
            return False
        if error:
            future.set_exception(RuntimeError(error[:1000]))
        else:
            future.set_result(result)
        return True

    def cancel_run(self, run_id: str) -> None:
        for command_id, pending_run in list(self._pending_runs.items()):
            if pending_run != run_id:
                continue
            future = self._pending.get(command_id)
            if future is not None and not future.done():
                future.cancel()


class UsageRuntime:
    def __init__(
        self,
        data_root: str | Path,
        *,
        provider_name_resolver: ProviderNameResolver | None = None,
        cache_seconds: int = 60,
    ) -> None:
        self.data_root = Path(data_root)
        self.ledger = UsageLedger(
            self.data_root, self.data_root / "usage" / "ledger.sqlite3"
        )
        self.plugins = UsagePluginManager(
            self.data_root / "usage-plugins", self.ledger
        )
        self.plugin_runner = UsagePluginRunner()
        self.adapters = UsageAdapterRegistry.with_builtins()
        self.browser = BrowserCommandBroker()
        self.adapters.register(DeepSeekPortalUsageAdapter(self._portal_browser_request))
        self.adapters.register(PluginUsageAdapter(
            self.plugins,
            self.plugin_runner,
            host_handler_factory=self._plugin_host_handler,
        ))
        self.provider_name_resolver = provider_name_resolver or (lambda value: value)
        self.cache_seconds = max(0, cache_seconds)
        self._accounts: dict[str, UsageAccount] = {
            account.id: account for account in self.ledger.list_usage_accounts()
        }
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._account_runs: dict[str, UsageRun] = {}
        self._event_callback: EventCallback | None = None

    def set_event_callback(self, callback: EventCallback | None) -> None:
        self._event_callback = callback
        self.browser.set_event_callback(callback)

    async def _portal_browser_request(
        self, account: UsageAccount, method: str, params: dict[str, Any]
    ) -> Any:
        run = self._account_runs.get(account.id)
        if run is None:
            raise RuntimeError("usage portal run is unavailable")
        manifest = {
            "permissions": {
                "browser": True,
                "interactive_login": True,
                "origins": ["https://platform.deepseek.com"],
            },
            "secret_schema": {"type": "object", "properties": {}},
        }

        async def on_status(
            status: UsageRunStatus, message: str, interaction_id: str
        ) -> None:
            run.interaction_id = interaction_id
            await self._set_run_status(run, status, message)

        return await self.browser.request(
            run, manifest, account, method, params, on_status=on_status
        )

    def import_legacy_desktop_usage(self, path: str | Path) -> int:
        return self.ledger.import_legacy_jsonl(path)

    def list_accounts(self) -> list[UsageAccount]:
        return sorted(self._accounts.values(), key=lambda item: (item.name.lower(), item.id))

    def get_account(self, account_id: str) -> UsageAccount | None:
        return self._accounts.get(account_id)

    def upsert_managed_account(self, account: UsageAccount) -> UsageAccount:
        """Register an in-memory account whose credentials must never reach SQLite."""
        account.managed = True
        self._accounts[account.id] = account
        return account

    def save_account(
        self,
        payload: dict[str, Any],
        *,
        secret_values: dict[str, str] | None = None,
    ) -> UsageAccount:
        existing = self._accounts.get(str(payload.get("id") or ""))
        if existing is not None and existing.managed:
            raise ValueError("managed usage accounts are read-only")
        merged = dict(existing.to_dict() if existing else {})
        merged.update(payload)
        merged.setdefault("id", uuid.uuid4().hex)
        if existing:
            merged["created_at"] = existing.created_at.isoformat()
        merged["updated_at"] = utc_now().isoformat()
        secrets = dict(existing.secret_values if existing else {})
        if secret_values is not None:
            secrets.update({
                str(key): str(value)
                for key, value in secret_values.items()
                if str(value)
            })
        account = UsageAccount.from_dict(merged, secret_values=secrets)
        self._accounts[account.id] = account
        self.ledger.upsert_usage_account(account)
        return account

    def sync_accounts(self, payloads: Iterable[dict[str, Any]]) -> list[UsageAccount]:
        synced: list[UsageAccount] = []
        for payload in payloads:
            value = dict(payload)
            secrets = value.pop("secret_values", None)
            synced.append(self.save_account(value, secret_values=secrets))
        return synced

    def delete_account(self, account_id: str) -> bool:
        run = self._account_runs.get(account_id)
        if run is not None:
            self.cancel_run(run.id)
        account = self._accounts.pop(account_id, None)
        if account is not None and account.managed:
            return True
        return self.ledger.delete_usage_account(account_id)

    def _local_summary(self, account: UsageAccount, period: UsagePeriod) -> LocalUsageSummary:
        names = list(dict.fromkeys(
            self.provider_name_resolver(key) for key in account.linked_provider_keys
        ))
        names = [name for name in names if name]
        if not names:
            return LocalUsageSummary()
        combined = LocalUsageSummary()
        for name in names:
            item = self.ledger.summarize(
                provider_name=name, start_time=period.start, end_time=period.end
            )
            combined.request_count += item.request_count
            combined.tokens = combined.tokens.add(item.tokens)
            for model, tokens in item.by_model.items():
                combined.by_model[model] = combined.by_model.get(model, TokenUsage()).add(tokens)
            for evidence, count in item.evidence_counts.items():
                combined.evidence_counts[evidence] = combined.evidence_counts.get(evidence, 0) + count
            if item.latest_rate_limits is not None:
                combined.latest_rate_limits = item.latest_rate_limits
        return combined

    async def _emit(self, run: UsageRun) -> None:
        self.ledger.save_usage_run(run)
        if self._event_callback is not None:
            await self._event_callback({"type": "usage_run.updated", "run": run.to_dict()})

    async def _set_run_status(
        self,
        run: UsageRun,
        status: UsageRunStatus,
        message: str = "",
        interaction_id: str = "",
    ) -> None:
        run.status = status
        run.message = message[:2000]
        run.interaction_id = interaction_id
        run.updated_at = utc_now()
        await self._emit(run)

    def _plugin_host_handler(
        self, manifest: dict[str, Any], account: UsageAccount
    ) -> Callable[[str, dict[str, Any]], Awaitable[Any]]:
        async def handler(method: str, params: dict[str, Any]) -> Any:
            run = self._account_runs.get(account.id)
            if run is None:
                raise RuntimeError("usage run is no longer active")

            async def on_status(
                status: UsageRunStatus, message: str, interaction_id: str
            ) -> None:
                await self._set_run_status(
                    run, status, message=message, interaction_id=interaction_id
                )

            return await self.browser.request(
                run,
                manifest,
                account,
                method,
                params,
                on_status=on_status,
            )

        return handler

    async def start_refresh(self, account_id: str, *, force: bool = False) -> UsageRun:
        account = self._accounts.get(account_id)
        if account is None:
            raise KeyError(account_id)
        if not account.enabled:
            raise ValueError("usage account is disabled")
        active = self._account_runs.get(account_id)
        if active is not None and active.status in {
            UsageRunStatus.QUEUED,
            UsageRunStatus.RUNNING,
            UsageRunStatus.ACTION_REQUIRED,
        }:
            return active
        latest = self.ledger.latest_usage_snapshot(account_id)
        if (
            not force
            and latest is not None
            and (utc_now() - latest.fetched_at).total_seconds() < self.cache_seconds
        ):
            run = UsageRun(
                id=uuid.uuid4().hex,
                account_id=account_id,
                status=(
                    UsageRunStatus.FAILED
                    if latest.status.value == "error"
                    else UsageRunStatus.PARTIAL
                    if latest.status.value in {"partial", "action_required"}
                    else UsageRunStatus.SUCCEEDED
                ),
                message="使用一分钟内的缓存快照",
                snapshot_hash=latest.canonical_hash(),
            )
            await self._emit(run)
            return run
        run = UsageRun(id=uuid.uuid4().hex, account_id=account_id)
        self._account_runs[account_id] = run
        await self._emit(run)
        task = asyncio.create_task(self._execute(run, copy.deepcopy(account)))
        self._tasks[run.id] = task
        task.add_done_callback(lambda _task, run_id=run.id: self._tasks.pop(run_id, None))
        return run

    async def refresh_enabled(self, *, force: bool = False) -> list[UsageRun]:
        return [
            await self.start_refresh(account.id, force=force)
            for account in self.list_accounts()
            if account.enabled
        ]

    async def _execute(self, run: UsageRun, account: UsageAccount) -> None:
        try:
            if account.adapter_id == "plugin":
                manifest = self.plugins.get_plugin(account.plugin_id)
                if manifest is not None:
                    run.plugin_id = str(manifest.get("id") or account.plugin_id)
                    run.plugin_version = str(manifest.get("version") or "")
                    run.plugin_sha256 = str(manifest.get("sha256") or "")
            async with current_runtime_scheduler(GLOBAL_RUNTIME_SCHEDULER).slot(
                kind="usage", label=account.name, priority=100
            ):
                await self._set_run_status(
                    run, UsageRunStatus.RUNNING, "正在查询用量"
                )
                period = current_month_period()
                local = self._local_summary(account, period)
                adapter = self.adapters.get(account.adapter_id)
                snapshot = await adapter.fetch_snapshot(
                    account, period, local=local
                )
            digest = self.ledger.save_usage_snapshot(snapshot)
            run.snapshot_hash = digest
            status = (
                UsageRunStatus.FAILED
                if snapshot.status.value == "error"
                else UsageRunStatus.PARTIAL
                if snapshot.status.value in {"partial", "action_required"}
                else UsageRunStatus.SUCCEEDED
            )
            await self._set_run_status(run, status, "用量刷新完成")
        except asyncio.CancelledError:
            await self._set_run_status(run, UsageRunStatus.CANCELLED, "用量刷新已取消")
            raise
        except Exception as exc:
            await self._set_run_status(run, UsageRunStatus.FAILED, str(exc))
        finally:
            if self._account_runs.get(account.id) is run:
                self._account_runs.pop(account.id, None)

    def get_run(self, run_id: str) -> UsageRun | None:
        for run in self._account_runs.values():
            if run.id == run_id:
                return run
        return self.ledger.get_usage_run(run_id)

    def cancel_run(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        if task is None or task.done():
            return False
        self.browser.cancel_run(run_id)
        task.cancel()
        return True

    def resolve_browser_command(
        self, command_id: str, *, result: Any = None, error: str = ""
    ) -> bool:
        return self.browser.resolve(command_id, result=result, error=error)

    def summary(self) -> dict[str, Any]:
        snapshots = self.ledger.latest_usage_snapshots()
        accounts = []
        for account in self.list_accounts():
            run = (
                self._account_runs.get(account.id)
                or self.ledger.latest_usage_run(account.id)
            )
            accounts.append({
                "account": account.to_dict(),
                "snapshot": snapshots.get(account.id).to_dict()
                if account.id in snapshots else None,
                "run": run.to_dict() if run else None,
            })
        return {
            "period": current_month_period().to_dict(),
            "accounts": accounts,
            "adapters": self.adapters.ids(),
        }
