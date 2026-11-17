"""Durable local request ledger for usage tracing.

SQLite is used instead of per-session JSON so concurrent agents and separate
processes can append safely.  Only normalized metadata and credential hashes
are stored; API keys and raw response bodies are never persisted.
"""

from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mewcode.usage.models import (
    AccountSnapshot,
    EvidenceLevel,
    LocalUsageSummary,
    RateLimitSnapshot,
    RequestUsage,
    TokenUsage,
    UsageAccount,
    UsageIdentities,
    UsageRun,
    UsageRunStatus,
    UsageSnapshot,
)


UTC = timezone.utc
DEFAULT_LEDGER_PATH = ".mewcode/usage/ledger.sqlite3"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS request_usage (
    event_id TEXT PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    protocol TEXT NOT NULL,
    base_url TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    configured_model TEXT NOT NULL,
    response_model TEXT NOT NULL,
    response_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    upstream_request_id TEXT NOT NULL,
    service_tier TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    api_key_id TEXT NOT NULL,
    credential_fingerprint TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    cached_input_tokens INTEGER NOT NULL,
    cache_write_input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    reasoning_tokens INTEGER NOT NULL,
    cache_usage_available INTEGER NOT NULL,
    rate_limits_json TEXT NOT NULL,
    evidence TEXT NOT NULL,
    attribution TEXT NOT NULL,
    receipt_sha256 TEXT NOT NULL,
    receipt_signature_present INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_request_usage_time
    ON request_usage(occurred_at);
CREATE INDEX IF NOT EXISTS idx_request_usage_provider_time
    ON request_usage(provider_name, occurred_at);
CREATE INDEX IF NOT EXISTS idx_request_usage_model_time
    ON request_usage(response_model, occurred_at);
CREATE INDEX IF NOT EXISTS idx_request_usage_session_time
    ON request_usage(session_id, occurred_at);

CREATE TABLE IF NOT EXISTS account_snapshots (
    snapshot_hash TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    account_ref TEXT NOT NULL,
    evidence TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_account_snapshots_provider_time
    ON account_snapshots(provider_name, fetched_at);

CREATE TABLE IF NOT EXISTS usage_accounts (
    account_id TEXT PRIMARY KEY,
    adapter_id TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_accounts_adapter
    ON usage_accounts(adapter_id, enabled);

CREATE TABLE IF NOT EXISTS usage_snapshots_v1 (
    snapshot_hash TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    status TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_snapshots_v1_account_time
    ON usage_snapshots_v1(account_id, fetched_at DESC);

CREATE TABLE IF NOT EXISTS usage_runs (
    run_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_runs_account_time
    ON usage_runs(account_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS usage_plugins (
    plugin_id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    installed_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_migrations (
    migration_id TEXT PRIMARY KEY,
    completed_at TEXT NOT NULL,
    details_json TEXT NOT NULL
);
"""


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


class UsageLedger:
    def __init__(self, work_dir: str | Path, path: str | Path | None = None) -> None:
        root = Path(work_dir)
        candidate = Path(path) if path is not None else Path(DEFAULT_LEDGER_PATH)
        self.path = candidate if candidate.is_absolute() else root / candidate
        self._schema_lock = threading.Lock()
        self._schema_ready = False

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            connection.executescript(_SCHEMA)
            connection.commit()
            self._schema_ready = True

    def append(self, record: RequestUsage) -> None:
        with self._connect() as connection:
            self._ensure_schema(connection)
            connection.execute(
                """
                INSERT OR IGNORE INTO request_usage VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    record.event_id,
                    _iso(record.occurred_at),
                    record.provider_name,
                    record.protocol,
                    record.base_url,
                    record.endpoint,
                    record.configured_model,
                    record.response_model,
                    record.response_id,
                    record.request_id,
                    record.upstream_request_id,
                    record.service_tier,
                    record.organization_id,
                    record.project_id,
                    record.api_key_id,
                    record.credential_fingerprint,
                    record.tokens.input_tokens,
                    record.tokens.cached_input_tokens,
                    record.tokens.cache_write_input_tokens,
                    record.tokens.output_tokens,
                    record.tokens.reasoning_tokens,
                    int(record.cache_usage_available),
                    json.dumps(
                        record.rate_limits.to_dict() if record.rate_limits else None,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    record.evidence.value,
                    record.attribution.value,
                    record.receipt_sha256,
                    int(record.receipt_signature_present),
                    record.session_id,
                    record.agent_id,
                    record.trace_id,
                    json.dumps(
                        record.metadata,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )
            connection.commit()

    def save_account_snapshot(self, snapshot: AccountSnapshot) -> str:
        digest = snapshot.canonical_hash()
        payload = json.dumps(
            snapshot.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._connect() as connection:
            self._ensure_schema(connection)
            connection.execute(
                """
                INSERT OR IGNORE INTO account_snapshots
                (snapshot_hash, fetched_at, provider_name, account_ref,
                 evidence, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    digest,
                    _iso(snapshot.fetched_at),
                    snapshot.provider_name,
                    snapshot.account_ref,
                    snapshot.evidence.value,
                    payload,
                ),
            )
            connection.commit()
        return digest

    def upsert_usage_account(self, account: UsageAccount) -> None:
        """Persist only public account metadata, never runtime secrets."""

        payload = json.dumps(
            account.to_dict(include_secrets=False),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._connect() as connection:
            self._ensure_schema(connection)
            connection.execute(
                """
                INSERT INTO usage_accounts
                    (account_id, adapter_id, enabled, updated_at, payload_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    adapter_id=excluded.adapter_id,
                    enabled=excluded.enabled,
                    updated_at=excluded.updated_at,
                    payload_json=excluded.payload_json
                """,
                (
                    account.id,
                    account.adapter_id,
                    int(account.enabled),
                    _iso(account.updated_at),
                    payload,
                ),
            )
            connection.commit()

    def delete_usage_account(self, account_id: str) -> bool:
        with self._connect() as connection:
            self._ensure_schema(connection)
            cursor = connection.execute(
                "DELETE FROM usage_accounts WHERE account_id = ?", (account_id,)
            )
            connection.commit()
            return cursor.rowcount > 0

    def get_usage_account(self, account_id: str) -> UsageAccount | None:
        with self._connect() as connection:
            self._ensure_schema(connection)
            row = connection.execute(
                "SELECT payload_json FROM usage_accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
        return UsageAccount.from_dict(json.loads(row[0])) if row else None

    def list_usage_accounts(self, *, enabled_only: bool = False) -> list[UsageAccount]:
        if not self.path.exists():
            return []
        query = "SELECT payload_json FROM usage_accounts"
        values: list[Any] = []
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY updated_at, account_id"
        with self._connect() as connection:
            self._ensure_schema(connection)
            rows = connection.execute(query, values).fetchall()
        return [UsageAccount.from_dict(json.loads(row[0])) for row in rows]

    def save_usage_snapshot(self, snapshot: UsageSnapshot) -> str:
        digest = snapshot.canonical_hash()
        payload = json.dumps(
            snapshot.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._connect() as connection:
            self._ensure_schema(connection)
            connection.execute(
                """
                INSERT OR IGNORE INTO usage_snapshots_v1
                    (snapshot_hash, account_id, status, fetched_at, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    digest,
                    snapshot.account_id,
                    snapshot.status.value,
                    _iso(snapshot.fetched_at),
                    payload,
                ),
            )
            connection.commit()
        return digest

    def latest_usage_snapshot(self, account_id: str) -> UsageSnapshot | None:
        with self._connect() as connection:
            self._ensure_schema(connection)
            row = connection.execute(
                """
                SELECT payload_json FROM usage_snapshots_v1
                WHERE account_id = ?
                ORDER BY fetched_at DESC LIMIT 1
                """,
                (account_id,),
            ).fetchone()
        return UsageSnapshot.from_dict(json.loads(row[0])) if row else None

    def latest_usage_snapshots(self) -> dict[str, UsageSnapshot]:
        with self._connect() as connection:
            self._ensure_schema(connection)
            rows = connection.execute(
                """
                SELECT snapshots.account_id, snapshots.payload_json
                FROM usage_snapshots_v1 AS snapshots
                JOIN (
                    SELECT account_id, MAX(fetched_at) AS fetched_at
                    FROM usage_snapshots_v1 GROUP BY account_id
                ) AS latest
                ON latest.account_id = snapshots.account_id
                AND latest.fetched_at = snapshots.fetched_at
                ORDER BY snapshots.account_id
                """
            ).fetchall()
        return {
            str(account_id): UsageSnapshot.from_dict(json.loads(payload))
            for account_id, payload in rows
        }

    def save_usage_run(self, run: UsageRun) -> None:
        payload = json.dumps(
            run.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with self._connect() as connection:
            self._ensure_schema(connection)
            connection.execute(
                """
                INSERT INTO usage_runs
                    (run_id, account_id, status, updated_at, payload_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    payload_json=excluded.payload_json
                """,
                (
                    run.id,
                    run.account_id,
                    run.status.value,
                    _iso(run.updated_at),
                    payload,
                ),
            )
            connection.commit()

    @staticmethod
    def _run_from_json(payload: str) -> UsageRun:
        value = json.loads(payload)

        def parsed(name: str) -> datetime:
            result = datetime.fromisoformat(str(value[name]))
            if result.tzinfo is None:
                result = result.replace(tzinfo=UTC)
            return result.astimezone(UTC)

        return UsageRun(
            id=str(value["id"]),
            account_id=str(value["account_id"]),
            status=UsageRunStatus(value["status"]),
            created_at=parsed("created_at"),
            updated_at=parsed("updated_at"),
            message=str(value.get("message") or ""),
            interaction_id=str(value.get("interaction_id") or ""),
            snapshot_hash=str(value.get("snapshot_hash") or ""),
            plugin_id=str(value.get("plugin_id") or ""),
            plugin_version=str(value.get("plugin_version") or ""),
            plugin_sha256=str(value.get("plugin_sha256") or ""),
        )

    def get_usage_run(self, run_id: str) -> UsageRun | None:
        with self._connect() as connection:
            self._ensure_schema(connection)
            row = connection.execute(
                "SELECT payload_json FROM usage_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return self._run_from_json(row[0]) if row else None

    def latest_active_run(self, account_id: str) -> UsageRun | None:
        active = (
            UsageRunStatus.QUEUED.value,
            UsageRunStatus.RUNNING.value,
            UsageRunStatus.ACTION_REQUIRED.value,
        )
        with self._connect() as connection:
            self._ensure_schema(connection)
            row = connection.execute(
                """
                SELECT payload_json FROM usage_runs
                WHERE account_id = ? AND status IN (?, ?, ?)
                ORDER BY updated_at DESC LIMIT 1
                """,
                (account_id, *active),
            ).fetchone()
        return self._run_from_json(row[0]) if row else None

    def latest_usage_run(self, account_id: str) -> UsageRun | None:
        if not self.path.exists():
            return None
        with self._connect() as connection:
            self._ensure_schema(connection)
            row = connection.execute(
                """
                SELECT payload_json FROM usage_runs
                WHERE account_id = ? ORDER BY updated_at DESC LIMIT 1
                """,
                (account_id,),
            ).fetchone()
        return self._run_from_json(row[0]) if row else None

    def upsert_usage_plugin(self, manifest: dict[str, Any]) -> None:
        plugin_id = str(manifest["id"])
        payload = json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with self._connect() as connection:
            self._ensure_schema(connection)
            connection.execute(
                """
                INSERT INTO usage_plugins
                    (plugin_id, version, installed_at, payload_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(plugin_id) DO UPDATE SET
                    version=excluded.version,
                    installed_at=excluded.installed_at,
                    payload_json=excluded.payload_json
                """,
                (
                    plugin_id,
                    str(manifest["version"]),
                    str(manifest["installed_at"]),
                    payload,
                ),
            )
            connection.commit()

    def list_usage_plugins(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            self._ensure_schema(connection)
            rows = connection.execute(
                "SELECT payload_json FROM usage_plugins ORDER BY plugin_id"
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def get_usage_plugin(self, plugin_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            self._ensure_schema(connection)
            row = connection.execute(
                "SELECT payload_json FROM usage_plugins WHERE plugin_id = ?",
                (plugin_id,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def delete_usage_plugin(self, plugin_id: str) -> bool:
        with self._connect() as connection:
            self._ensure_schema(connection)
            cursor = connection.execute(
                "DELETE FROM usage_plugins WHERE plugin_id = ?", (plugin_id,)
            )
            connection.commit()
            return cursor.rowcount > 0

    def import_legacy_jsonl(self, path: str | Path) -> int:
        """Idempotently import the old desktop usage.jsonl request ledger."""

        source = Path(path)
        migration_id = "desktop-usage-jsonl-v1:" + hashlib.sha256(
            str(source.resolve()).encode("utf-8")
        ).hexdigest()[:16]
        with self._connect() as connection:
            self._ensure_schema(connection)
            done = connection.execute(
                "SELECT 1 FROM usage_migrations WHERE migration_id = ?",
                (migration_id,),
            ).fetchone()
        if done or not source.is_file():
            return 0

        imported = 0
        for line_number, line in enumerate(
            source.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            try:
                value = json.loads(line)
                occurred = datetime.fromisoformat(str(value["ts"]))
                if occurred.tzinfo is None:
                    occurred = occurred.replace(tzinfo=UTC)
                record = RequestUsage(
                    event_id=str(uuid.uuid5(
                        uuid.NAMESPACE_URL, f"{source.resolve()}:{line_number}:{line}"
                    )),
                    occurred_at=occurred,
                    configured_model=str(value.get("model") or ""),
                    response_model=str(value.get("model") or ""),
                    tokens=TokenUsage(
                        input_tokens=max(int(value.get("in") or 0), 0),
                        output_tokens=max(int(value.get("out") or 0), 0),
                    ),
                    session_id=str(value.get("sid") or ""),
                    evidence=EvidenceLevel.LOCAL_ESTIMATE,
                    metadata={
                        "legacy_messages": max(int(value.get("msgs") or 1), 1),
                        "legacy_import": True,
                    },
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            self.append(record)
            imported += 1

        with self._connect() as connection:
            self._ensure_schema(connection)
            connection.execute(
                """
                INSERT OR REPLACE INTO usage_migrations
                    (migration_id, completed_at, details_json)
                VALUES (?, ?, ?)
                """,
                (
                    migration_id,
                    _iso(datetime.now(UTC)),
                    json.dumps({"path": str(source), "imported": imported}),
                ),
            )
            connection.commit()
        return imported

    @staticmethod
    def _where(
        *,
        start_time: datetime | None,
        end_time: datetime | None,
        provider_name: str,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if start_time is not None:
            clauses.append("occurred_at >= ?")
            values.append(_iso(start_time))
        if end_time is not None:
            clauses.append("occurred_at < ?")
            values.append(_iso(end_time))
        if provider_name:
            clauses.append("provider_name = ?")
            values.append(provider_name)
        return (" WHERE " + " AND ".join(clauses) if clauses else "", values)

    def summarize(
        self,
        *,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        provider_name: str = "",
    ) -> LocalUsageSummary:
        if not self.path.exists():
            return LocalUsageSummary()
        where, values = self._where(
            start_time=start_time,
            end_time=end_time,
            provider_name=provider_name,
        )
        with self._connect() as connection:
            self._ensure_schema(connection)
            row = connection.execute(
                """
                SELECT COUNT(*),
                       COALESCE(SUM(input_tokens), 0),
                       COALESCE(SUM(cached_input_tokens), 0),
                       COALESCE(SUM(cache_write_input_tokens), 0),
                       COALESCE(SUM(output_tokens), 0),
                       COALESCE(SUM(reasoning_tokens), 0)
                FROM request_usage
                """ + where,
                values,
            ).fetchone()
            summary = LocalUsageSummary(
                request_count=int(row[0]),
                tokens=TokenUsage(
                    input_tokens=int(row[1]),
                    cached_input_tokens=int(row[2]),
                    cache_write_input_tokens=int(row[3]),
                    output_tokens=int(row[4]),
                    reasoning_tokens=int(row[5]),
                ),
            )
            for model_row in connection.execute(
                """
                SELECT CASE WHEN response_model = '' THEN configured_model
                            ELSE response_model END AS effective_model,
                       COALESCE(SUM(input_tokens), 0),
                       COALESCE(SUM(cached_input_tokens), 0),
                       COALESCE(SUM(cache_write_input_tokens), 0),
                       COALESCE(SUM(output_tokens), 0),
                       COALESCE(SUM(reasoning_tokens), 0)
                FROM request_usage
                """ + where + " GROUP BY effective_model",
                values,
            ):
                summary.by_model[str(model_row[0])] = TokenUsage(
                    input_tokens=int(model_row[1]),
                    cached_input_tokens=int(model_row[2]),
                    cache_write_input_tokens=int(model_row[3]),
                    output_tokens=int(model_row[4]),
                    reasoning_tokens=int(model_row[5]),
                )
            for evidence, count in connection.execute(
                "SELECT evidence, COUNT(*) FROM request_usage"
                + where
                + " GROUP BY evidence",
                values,
            ):
                summary.evidence_counts[str(evidence)] = int(count)

            rate_where = (
                where + " AND rate_limits_json != 'null'"
                if where
                else " WHERE rate_limits_json != 'null'"
            )
            latest = connection.execute(
                "SELECT rate_limits_json FROM request_usage"
                + rate_where
                + " ORDER BY occurred_at DESC LIMIT 1",
                values,
            ).fetchone()
            if latest:
                raw = json.loads(latest[0])
                summary.latest_rate_limits = RateLimitSnapshot(**raw)
            return summary

    def identities(
        self,
        *,
        provider_name: str = "",
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> UsageIdentities:
        """Return official/configured account identities seen in request rows."""

        if not self.path.exists():
            return UsageIdentities()
        where, values = self._where(
            start_time=start_time,
            end_time=end_time,
            provider_name=provider_name,
        )
        identities = UsageIdentities()
        with self._connect() as connection:
            self._ensure_schema(connection)
            rows = connection.execute(
                "SELECT DISTINCT organization_id, project_id, api_key_id "
                "FROM request_usage" + where,
                values,
            )
            for organization_id, project_id, api_key_id in rows:
                if organization_id:
                    identities.organization_ids.add(str(organization_id))
                if project_id:
                    identities.project_ids.add(str(project_id))
                if api_key_id:
                    identities.api_key_ids.add(str(api_key_id))
        return identities

    def recent(
        self, *, provider_name: str = "", limit: int = 100
    ) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        where, values = self._where(
            start_time=None, end_time=None, provider_name=provider_name
        )
        with self._connect() as connection:
            self._ensure_schema(connection)
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM request_usage"
                + where
                + " ORDER BY occurred_at DESC LIMIT ?",
                [*values, max(1, min(limit, 1000))],
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                prompt_tokens = (
                    int(item.get("input_tokens", 0) or 0)
                    + int(item.get("cached_input_tokens", 0) or 0)
                    + int(item.get("cache_write_input_tokens", 0) or 0)
                )
                available = bool(item.get("cache_usage_available", 0))
                item["prompt_tokens"] = prompt_tokens
                item["cache_hit_rate"] = (
                    int(item.get("cached_input_tokens", 0) or 0) / prompt_tokens
                    if available and prompt_tokens
                    else (0.0 if available else None)
                )
                try:
                    metadata = json.loads(item.get("metadata_json") or "{}")
                except (json.JSONDecodeError, TypeError):
                    metadata = {}
                item["cache_slot"] = str(metadata.get("cache_slot") or "")
                item["cache_provider"] = str(
                    metadata.get("cache_provider") or ""
                )
                item["cache_type"] = str(metadata.get("cache_type") or "")
                item["estimated_cost_usd"] = metadata.get("estimated_cost_usd")
                item["cost_basis"] = str(metadata.get("cost_basis") or "")
                result.append(item)
            return result

    def conversation_summary(
        self, session_id: str, *, additional_paths: tuple[Path, ...] = (),
    ) -> dict[str, Any]:
        """Aggregate durable receipts, never guesses from chat text or UI events.

        An empty scope must NOT turn into a project/account-wide query. Reasoning
        tokens are an output subset and cached tokens an input subset. Old graph
        runtimes wrote project-local receipts while desktop chat wrote centrally;
        read both without migrating, caching, or changing any writer's database.
        The same event copied between ledgers counts once (primary route wins).
        """
        result: dict[str, Any] = {
            "conversation_id": session_id, "models": [], "request_count": 0,
            "tokens": TokenUsage().to_dict(), "unreported_token_requests": 0,
            "basis": "local_request_receipts", "updated_at": _iso(datetime.now(UTC)),
            "partial": False,
        }
        if not session_id:
            return result
        seen_paths: set[Path] = set()
        seen_events: set[str] = set()
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for path in (self.path, *additional_paths):
            try:
                path = path.resolve()
                if path in seen_paths or not path.exists():
                    continue
                seen_paths.add(path)
                # Read-only and bounded: never create a missing project ledger,
                # run a schema migration, or block a writer during UI polling.
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as connection:
                    rows = connection.execute("""
                        SELECT event_id, provider_name,
                               CASE WHEN response_model = '' THEN configured_model ELSE response_model END,
                               input_tokens, cached_input_tokens, cache_write_input_tokens,
                               output_tokens, reasoning_tokens, occurred_at, agent_id
                        FROM request_usage WHERE session_id = ?
                    """, (session_id,)).fetchall()
            except (OSError, sqlite3.Error):
                result["partial"] = True
                continue
            for event, provider, model, inp, cached, written, out, reasoning, occurred, agent in rows:
                if event in seen_events:
                    continue
                seen_events.add(event)
                tokens = TokenUsage(inp, cached, written, out, reasoning)
                group = groups.setdefault((provider, model), {
                    "tokens": TokenUsage(), "count": 0, "missing": 0,
                    "first": occurred, "last": occurred, "agents": set(),
                })
                group["tokens"] = group["tokens"].add(tokens)
                group["count"] += 1
                group["missing"] += int(tokens.total == 0)
                group["first"] = min(group["first"], occurred)
                group["last"] = max(group["last"], occurred)
                group["agents"].add(agent)
        total = TokenUsage()
        for (provider, model), group in sorted(
            groups.items(), key=lambda item: (-item[1]["count"], *item[0]),
        ):
            tokens = group["tokens"]
            total = total.add(tokens)
            result["models"].append({
                "provider": provider, "model": model or "未标识模型", "request_count": group["count"],
                "tokens": tokens.to_dict(), "unreported_token_requests": group["missing"],
                "first_used_at": group["first"], "last_used_at": group["last"],
                "agent_count": len(group["agents"]),
            })
            result["request_count"] += group["count"]
            result["unreported_token_requests"] += group["missing"]
        result["tokens"] = total.to_dict()
        return result

    def activity_events(self) -> list[dict[str, Any]]:
        """Return the compact event shape used by the desktop heatmap."""

        if not self.path.exists():
            return []
        with self._connect() as connection:
            self._ensure_schema(connection)
            rows = connection.execute(
                """
                SELECT occurred_at,
                       CASE WHEN response_model = '' THEN configured_model
                            ELSE response_model END,
                       input_tokens + cached_input_tokens
                           + cache_write_input_tokens,
                       output_tokens,
                       session_id,
                       metadata_json,
                       evidence
                FROM request_usage ORDER BY occurred_at
                """
            ).fetchall()
        events: list[dict[str, Any]] = []
        for occurred_at, model, input_tokens, output_tokens, session_id, metadata, evidence in rows:
            try:
                details = json.loads(metadata or "{}")
            except (TypeError, json.JSONDecodeError):
                details = {}
            events.append({
                "ts": datetime.fromisoformat(str(occurred_at)).astimezone(),
                "model": str(model or ""),
                "in": int(input_tokens or 0),
                "out": int(output_tokens or 0),
                "sid": str(session_id or ""),
                "msgs": max(int(details.get("legacy_messages") or 1), 1),
                "approx": evidence == EvidenceLevel.LOCAL_ESTIMATE.value,
            })
        return events

    def activity_aggregate(self, *, since: datetime | None = None) -> dict[str, Any]:
        """Aggregate heatmap data in SQLite instead of materializing every row."""

        if not self.path.exists():
            return {"days": {}, "hours": {}, "models": {}, "sessions": [], "totals": {}}
        where = " WHERE occurred_at >= ?" if since is not None else ""
        values = [_iso(since)] if since is not None else []
        token_expr = (
            "input_tokens + cached_input_tokens + cache_write_input_tokens + output_tokens"
        )
        message_expr = (
            "CASE WHEN json_valid(metadata_json) THEN "
            "MAX(COALESCE(CAST(json_extract(metadata_json, '$.legacy_messages') AS INTEGER), 1), 1) "
            "ELSE 1 END"
        )
        with self._connect() as connection:
            self._ensure_schema(connection)
            day_rows = connection.execute(f"""
                SELECT strftime('%Y-%m-%d', occurred_at, 'localtime') AS day,
                       SUM({token_expr}),
                       SUM(input_tokens + cached_input_tokens + cache_write_input_tokens),
                       SUM(output_tokens), SUM({message_expr}),
                       MAX(CASE WHEN evidence = ? THEN 1 ELSE 0 END)
                FROM request_usage{where}
                GROUP BY day
            """, [EvidenceLevel.LOCAL_ESTIMATE.value, *values]).fetchall()
            hour_rows = connection.execute(f"""
                SELECT CAST(strftime('%H', occurred_at, 'localtime') AS INTEGER),
                       SUM({token_expr})
                FROM request_usage{where} GROUP BY 1
            """, values).fetchall()
            model_rows = connection.execute(f"""
                SELECT CASE WHEN response_model = '' THEN configured_model ELSE response_model END,
                       SUM({token_expr})
                FROM request_usage{where} GROUP BY 1
            """, values).fetchall()
            session_rows = connection.execute(
                "SELECT DISTINCT session_id FROM request_usage" + where,
                values,
            ).fetchall()
        days = {
            str(day): {
                "tokens": int(tokens or 0), "in": int(input_tokens or 0),
                "out": int(output_tokens or 0), "messages": int(messages or 0),
                "approx": bool(approximate),
            }
            for day, tokens, input_tokens, output_tokens, messages, approximate in day_rows
            if day
        }
        totals = {
            "tokens": sum(item["tokens"] for item in days.values()),
            "in": sum(item["in"] for item in days.values()),
            "out": sum(item["out"] for item in days.values()),
            "messages": sum(item["messages"] for item in days.values()),
        }
        return {
            "days": days,
            "hours": {int(hour): int(tokens or 0) for hour, tokens in hour_rows if hour is not None},
            "models": {str(model): int(tokens or 0) for model, tokens in model_rows if model},
            "sessions": [str(row[0]) for row in session_rows if row[0]],
            "totals": totals,
        }
