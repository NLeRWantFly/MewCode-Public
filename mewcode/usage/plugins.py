"""Trusted usage-plugin package management and isolated execution."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
import zipfile
from contextlib import suppress
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from mewcode.usage.backend import UsageAdapter, UsageBackendError, UsageUnavailable
from mewcode.usage.ledger import UsageLedger
from mewcode.usage.models import (
    EvidenceLevel,
    FinancialUsage,
    LocalUsageSummary,
    MetricProvenance,
    MetricValue,
    UnavailableMetric,
    UsageAccount,
    UsagePeriod,
    UsageSnapshot,
    UsageSnapshotStatus,
    utc_now,
)


MAX_PLUGIN_BYTES = 20 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 40 * 1024 * 1024
MAX_PLUGIN_FILES = 500
MAX_PROTOCOL_BYTES = 1024 * 1024
PLUGIN_PROTOCOL_VERSION = "1"
PLUGIN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
FORBIDDEN_SUFFIXES = {".exe", ".dll", ".pyd", ".so", ".dylib", ".bat", ".cmd", ".ps1"}
FORBIDDEN_NAMES = {"setup.py", "setup.cfg", "pyproject.toml", "requirements.txt"}
BROWSER_METHODS = {
    "browser.open", "browser.goto", "browser.wait_for", "browser.click",
    "browser.fill", "browser.text", "browser.evaluate", "browser.fetch",
    "browser.pause_for_user", "browser.close",
}


HostRequestHandler = Callable[[str, dict[str, Any]], Awaitable[Any]]


def _redact_log(value: str, secrets: dict[str, str]) -> str:
    text = str(value or "")
    for secret in secrets.values():
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(
        r"(?i)(authorization\s*[:=]\s*bearer\s+|api[_ -]?key\s*[:=]\s*|password\s*[:=]\s*)\S+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", text)
    return text


def _safe_member(name: str) -> PurePosixPath:
    candidate = PurePosixPath(name.replace("\\", "/"))
    if candidate.is_absolute() or not candidate.parts or any(
        part in {"", ".", ".."} for part in candidate.parts
    ):
        raise ValueError(f"unsafe plugin archive path: {name}")
    if ":" in candidate.parts[0]:
        raise ValueError(f"unsafe plugin archive path: {name}")
    return candidate


def _origin(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError(f"plugin origin must be HTTP(S): {value}")
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname.lower()}{port}"


def _reject_external_schema_references(value: Any) -> None:
    """Keep manifest validation local to the installed ZIP.

    jsonschema supports remote and file references through ``$ref``.  A
    manifest is untrusted at inspection time, so v1 accepts only JSON Pointer
    references into the schema document itself.
    """

    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"$ref", "$dynamicRef"} and (
                not isinstance(item, str) or not item.startswith("#")
            ):
                raise ValueError("plugin JSON Schema references must be local")
            _reject_external_schema_references(item)
    elif isinstance(value, list):
        for item in value:
            _reject_external_schema_references(item)


def validate_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("usage-plugin.json must contain an object")
    plugin_id = str(value.get("id") or "").strip().lower()
    version = str(value.get("version") or "").strip()
    entrypoint = str(value.get("entrypoint") or "plugin.py").strip()
    protocol = str(value.get("protocol_version") or "")
    if not PLUGIN_ID_RE.fullmatch(plugin_id):
        raise ValueError("plugin id must use 3-128 lowercase letters, digits, '.', '_' or '-'")
    if not VERSION_RE.fullmatch(version):
        raise ValueError("plugin version must be semantic x.y.z")
    if protocol != PLUGIN_PROTOCOL_VERSION:
        raise ValueError("plugin protocol_version must be '1'")
    entry = _safe_member(entrypoint)
    if entry.suffix.lower() != ".py":
        raise ValueError("plugin entrypoint must be a Python file")
    operations = value.get("operations") or ["collect", "normalize"]
    if not isinstance(operations, list) or set(operations) != {"collect", "normalize"}:
        raise ValueError("plugin operations must contain collect and normalize")
    permissions = value.get("permissions") or {}
    if not isinstance(permissions, dict):
        raise ValueError("plugin permissions must be an object")
    origins = permissions.get("origins") or []
    if not isinstance(origins, list):
        raise ValueError("plugin permissions.origins must be an array")
    config_schema = value.get("config_schema") or {"type": "object"}
    secret_schema = value.get("secret_schema") or {"type": "object", "properties": {}}
    artifact_schema = value.get("artifact_schema") or {}
    if not all(isinstance(item, dict) for item in (
        config_schema, secret_schema, artifact_schema
    )):
        raise ValueError("plugin config_schema, secret_schema and artifact_schema must be objects")
    try:
        for schema in (config_schema, secret_schema, artifact_schema):
            _reject_external_schema_references(schema)
            Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError(f"plugin contains an invalid JSON Schema: {exc.message}") from exc
    secret_properties = secret_schema.get("properties") or {}
    if not isinstance(secret_properties, dict):
        raise ValueError("plugin secret_schema.properties must be an object")
    return {
        "id": plugin_id,
        "name": str(value.get("name") or plugin_id)[:120],
        "version": version,
        "protocol_version": protocol,
        "entrypoint": entry.as_posix(),
        "operations": ["collect", "normalize"],
        "config_schema": config_schema,
        "secret_schema": secret_schema,
        "artifact_schema": artifact_schema,
        "permissions": {
            "browser": bool(permissions.get("browser", False)),
            "interactive_login": bool(permissions.get("interactive_login", False)),
            "origins": list(dict.fromkeys(_origin(str(item)) for item in origins)),
        },
        "bound_provider_key": str(value.get("bound_provider_key") or "")[:180],
        "bound_slot": dict(value.get("bound_slot") or {}),
        "script_sha256": str(value.get("script_sha256") or "")[:64],
        "source_kind": str(value.get("source_kind") or "zip")[:40],
    }


class UsagePluginManager:
    def __init__(self, root: str | Path, ledger: UsageLedger) -> None:
        self.root = Path(root)
        self.ledger = ledger

    def inspect_package(self, archive: bytes) -> dict[str, Any]:
        if len(archive) > MAX_PLUGIN_BYTES:
            raise ValueError("plugin archive exceeds 20 MiB")
        digest = hashlib.sha256(archive).hexdigest()
        with tempfile.TemporaryDirectory(prefix="mewcode-usage-inspect-") as temporary:
            source = Path(temporary) / "plugin.zip"
            source.write_bytes(archive)
            try:
                bundle_handle = zipfile.ZipFile(source)
            except zipfile.BadZipFile as exc:
                raise ValueError("plugin package is not a valid ZIP archive") from exc
            with bundle_handle as bundle:
                infos = bundle.infolist()
                if len(infos) > MAX_PLUGIN_FILES:
                    raise ValueError("plugin archive contains too many files")
                if sum(item.file_size for item in infos) > MAX_UNCOMPRESSED_BYTES:
                    raise ValueError("plugin archive expands beyond 40 MiB")
                names: set[str] = set()
                for info in infos:
                    member = _safe_member(info.filename)
                    normalized = member.as_posix()
                    if normalized in names:
                        raise ValueError(f"duplicate plugin archive path: {normalized}")
                    names.add(normalized)
                    mode = (info.external_attr >> 16) & 0xFFFF
                    if mode and stat.S_ISLNK(mode):
                        raise ValueError("plugin archives cannot contain symbolic links")
                    if member.name.lower() in FORBIDDEN_NAMES or member.suffix.lower() in FORBIDDEN_SUFFIXES:
                        raise ValueError(f"plugin contains forbidden install/native file: {normalized}")
                if "usage-plugin.json" not in names:
                    raise ValueError("plugin archive is missing usage-plugin.json")
                try:
                    manifest_value = json.loads(bundle.read("usage-plugin.json"))
                except (RuntimeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError("usage-plugin.json is unreadable or invalid") from exc
                manifest = validate_manifest(manifest_value)
                if manifest["entrypoint"] not in names:
                    raise ValueError("plugin entrypoint is missing from archive")
        return {**manifest, "sha256": digest, "trusted": False}

    def install_package(self, archive: bytes, *, trusted: bool) -> dict[str, Any]:
        manifest = self.inspect_package(archive)
        if not trusted:
            raise PermissionError(
                "安装第三方 Python 插件前必须确认其 SHA-256 与权限；子进程隔离不抵御恶意代码"
            )
        plugin_dir = self.root / manifest["id"] / manifest["version"]
        temporary = self.root / f".install-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True, exist_ok=True)
        temporary.mkdir(parents=True)
        try:
            archive_path = temporary / "plugin.zip"
            archive_path.write_bytes(archive)
            extract_root = temporary / "content"
            extract_root.mkdir()
            with zipfile.ZipFile(archive_path) as bundle:
                for info in bundle.infolist():
                    member = _safe_member(info.filename)
                    target = extract_root.joinpath(*member.parts)
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(info) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
            if plugin_dir.exists():
                shutil.rmtree(plugin_dir)
            plugin_dir.parent.mkdir(parents=True, exist_ok=True)
            extract_root.replace(plugin_dir)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
        installed = {
            **manifest,
            "trusted": True,
            "installed_at": utc_now().isoformat(),
            "install_path": str(plugin_dir),
        }
        self.ledger.upsert_usage_plugin(installed)
        return installed

    @staticmethod
    def public_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in manifest.items() if key != "install_path"}


    def resync_installed(self) -> list[str]:
        """Register plugin folders copied into the usage-plugins root."""
        if not self.root.is_dir():
            return []
        discovered: list[str] = []
        known = {str(item.get("id") or "") for item in self.ledger.list_usage_plugins()}
        for plugin_id_dir in sorted(self.root.iterdir()):
            if not plugin_id_dir.is_dir() or plugin_id_dir.name.startswith("."):
                continue
            for version_dir in sorted(plugin_id_dir.iterdir()):
                if not version_dir.is_dir():
                    continue
                manifest_path = version_dir / "usage-plugin.json"
                if not manifest_path.is_file():
                    continue
                try:
                    manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
                    manifest = validate_manifest(manifest_value)
                except (OSError, TypeError, ValueError, ValidationError, SchemaError) as exc:
                    continue
                plugin_id = str(manifest.get("id") or plugin_id_dir.name)
                if plugin_id in known:
                    continue
                installed = {
                    **manifest,
                    "trusted": True,
                    "installed_at": utc_now().isoformat(),
                    "install_path": str(version_dir),
                }
                self.ledger.upsert_usage_plugin(installed)
                known.add(plugin_id)
                discovered.append(plugin_id)
        # Drop ledger rows whose install path vanished (hot-unplug).
        for item in list(self.ledger.list_usage_plugins()):
            install_path = Path(str(item.get("install_path") or ""))
            if str(item.get("id") or "") and install_path and not install_path.exists():
                try:
                    self.ledger.delete_usage_plugin(str(item.get("id")))
                except Exception:
                    pass
        return discovered

    def list_plugins(self) -> list[dict[str, Any]]:

        try:
            self.resync_installed()
        except Exception:
            pass
        return [self.public_manifest(item) for item in self.ledger.list_usage_plugins()]

    def get_plugin(self, plugin_id: str) -> dict[str, Any] | None:
        return self.ledger.get_usage_plugin(plugin_id)

    def delete_plugin(self, plugin_id: str) -> bool:
        manifest = self.ledger.get_usage_plugin(plugin_id)
        if manifest is None:
            return False
        install_path = Path(str(manifest.get("install_path") or ""))
        try:
            relative = install_path.resolve().relative_to(self.root.resolve())
        except (OSError, ValueError):
            raise ValueError("refusing to delete plugin outside the plugin root")
        if len(relative.parts) != 2 or relative.parts[0] != plugin_id:
            raise ValueError("refusing to delete an invalid plugin install path")
        if install_path.exists():
            shutil.rmtree(install_path)
        return self.ledger.delete_usage_plugin(plugin_id)


class UsagePluginRunner:
    def __init__(self, *, timeout: float = 120.0) -> None:
        self.timeout = timeout

    @staticmethod
    def _command(entrypoint: Path) -> list[str]:
        if getattr(sys, "frozen", False):
            return [sys.executable, "--usage-plugin-worker", str(entrypoint)]
        worker = Path(__file__).with_name("plugin_worker.py")
        return [sys.executable, "-I", str(worker), str(entrypoint)]

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        if process.stdin is not None:
            process.stdin.close()
        if os.name == "nt":
            result = await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
            )
            if result.returncode != 0 and process.returncode is None:
                process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except asyncio.TimeoutError:
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=10)

    async def run(
        self,
        manifest: dict[str, Any],
        account: UsageAccount,
        period: UsagePeriod,
        *,
        host_handler: HostRequestHandler,
    ) -> UsageSnapshot:
        install_path = Path(str(manifest.get("install_path") or "")).resolve()
        entrypoint = (install_path / str(manifest["entrypoint"])).resolve()
        try:
            entrypoint.relative_to(install_path)
        except ValueError as exc:
            raise UsageUnavailable("plugin entrypoint escaped its install directory") from exc
        if not entrypoint.is_file():
            raise UsageUnavailable("plugin entrypoint is unavailable")
        env = {
            key: value for key, value in os.environ.items()
            if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "LANG"}
        }
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        with tempfile.TemporaryDirectory(
            prefix="mewcode-usage-run-", ignore_cleanup_errors=True
        ) as work_dir:
            process = await asyncio.create_subprocess_exec(
                *self._command(entrypoint),
                cwd=work_dir,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
                start_new_session=os.name != "nt",
                limit=MAX_PROTOCOL_BYTES + 1,
            )
            assert process.stdin is not None and process.stdout is not None
            stderr_bytes = bytearray()

            async def drain_stderr() -> None:
                if process.stderr is None:
                    return
                while True:
                    chunk = await process.stderr.read(8192)
                    if not chunk:
                        return
                    remaining = MAX_PROTOCOL_BYTES - len(stderr_bytes)
                    if remaining > 0:
                        stderr_bytes.extend(chunk[:remaining])

            stderr_task = asyncio.create_task(drain_stderr())
            invocation = {
                "type": "invoke",
                "protocol_version": PLUGIN_PROTOCOL_VERSION,
                "query": {"period": period.to_dict()},
                "context": {"account_id": account.id, "config": account.config},
            }
            process.stdin.write(
                json.dumps(invocation, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
            )
            await process.stdin.drain()
            total_bytes = 0
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.timeout
            try:
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise UsageBackendError("usage plugin collection timed out")
                    try:
                        line = await asyncio.wait_for(
                            process.stdout.readline(), timeout=remaining
                        )
                    except ValueError as exc:
                        raise UsageBackendError(
                            "usage plugin output exceeded 1 MiB"
                        ) from exc
                    if not line:
                        error_log = _redact_log(
                            bytes(stderr_bytes).decode("utf-8", "replace")[:500],
                            account.secret_values,
                        )
                        raise UsageBackendError(
                            "usage plugin exited without a result"
                            + (f": {error_log}" if error_log else "")
                        )
                    total_bytes += len(line)
                    if total_bytes > MAX_PROTOCOL_BYTES:
                        raise UsageBackendError("usage plugin output exceeded 1 MiB")
                    try:
                        message = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise UsageBackendError("usage plugin wrote non-JSON output") from exc
                    if not isinstance(message, dict):
                        raise UsageBackendError("usage plugin message was not an object")
                    if message.get("type") == "host.request":
                        request_id = str(message.get("id") or "")
                        method = str(message.get("method") or "")
                        params = dict(message.get("params") or {})
                        try:
                            host_started = loop.time()
                            result = await host_handler(method, params)
                            if method == "browser.pause_for_user":
                                deadline += loop.time() - host_started
                            response = {"type": "host.response", "id": request_id, "result": result}
                        except Exception as exc:
                            response = {"type": "host.response", "id": request_id, "error": str(exc)[:1000]}
                        process.stdin.write(
                            json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
                        )
                        await process.stdin.drain()
                        continue
                    if message.get("type") == "artifact":
                        artifact = message.get("artifact")
                        try:
                            Draft202012Validator(
                                manifest.get("artifact_schema") or {}
                            ).validate(artifact)
                        except ValidationError as exc:
                            raise UsageBackendError(
                                f"usage plugin RawArtifact failed JSON Schema validation: {exc.message}"
                            ) from exc
                        process.stdin.write(json.dumps({
                            "type": "artifact.approved",
                            "id": str(message.get("id") or ""),
                        }, separators=(",", ":")).encode("utf-8") + b"\n")
                        await process.stdin.drain()
                        continue
                    if message.get("type") == "error":
                        raise UsageBackendError(_redact_log(
                            str(message.get("error") or "plugin failed"),
                            account.secret_values,
                        ))
                    if message.get("type") != "result":
                        raise UsageBackendError("usage plugin returned an unknown message type")
                    snapshot_payload = dict(message.get("snapshot") or {})
                    snapshot_payload.setdefault("schema_version", "1.0")
                    snapshot_payload["account_id"] = account.id
                    snapshot_payload["period"] = period.to_dict()
                    snapshot_payload.setdefault("status", "ok")
                    snapshot_payload.setdefault("financials", [])
                    snapshot_payload.setdefault("evidence", "relay_reported")
                    snapshot = UsageSnapshot.from_dict(snapshot_payload)
                    await asyncio.wait_for(process.wait(), timeout=10)
                    await stderr_task
                    if process.returncode != 0:
                        error_log = _redact_log(
                            bytes(stderr_bytes).decode("utf-8", "replace")[:500],
                            account.secret_values,
                        )
                        raise UsageBackendError(
                            "usage plugin exited with a non-zero status"
                            + (f": {error_log}" if error_log else "")
                        )
                    return snapshot
            except asyncio.TimeoutError as exc:
                raise UsageBackendError("usage plugin collection timed out") from exc
            finally:
                await self._terminate(process)
                if not stderr_task.done():
                    stderr_task.cancel()
                with suppress(asyncio.CancelledError):
                    await stderr_task


class PluginUsageAdapter(UsageAdapter):
    def __init__(
        self,
        manager: UsagePluginManager,
        runner: UsagePluginRunner,
        *,
        host_handler_factory: Callable[[dict[str, Any], UsageAccount], HostRequestHandler],
    ) -> None:
        self.manager = manager
        self.runner = runner
        self.host_handler_factory = host_handler_factory

    @property
    def adapter_id(self) -> str:
        return "plugin"

    async def fetch_snapshot(
        self,
        account: UsageAccount,
        period: UsagePeriod,
        *,
        local: LocalUsageSummary | None = None,
    ) -> UsageSnapshot:
        if not account.plugin_id:
            raise UsageUnavailable("用量账户没有配置 plugin_id")
        manifest = self.manager.get_plugin(account.plugin_id)
        if manifest is None:
            raise UsageUnavailable(f"用量插件未安装：{account.plugin_id}")
        account = copy.deepcopy(account)
        bound_slot = manifest.get("bound_slot")
        if isinstance(bound_slot, dict) and bound_slot:
            account.config.setdefault("provider_slot", bound_slot)
        try:
            Draft202012Validator(
                manifest.get("config_schema") or {"type": "object"}
            ).validate(account.config)
            Draft202012Validator(
                manifest.get("secret_schema") or {"type": "object"}
            ).validate(account.secret_values)
        except ValidationError as exc:
            raise UsageUnavailable(
                f"用量插件账户配置不符合 Manifest JSON Schema：{exc.message}"
            ) from exc
        handler = self.host_handler_factory(manifest, account)
        snapshot = await self.runner.run(
            manifest, account, period, host_handler=handler
        )
        snapshot.account_id = account.id
        snapshot.period = period
        snapshot.evidence = EvidenceLevel.RELAY_REPORTED
        snapshot.fetched_at = utc_now()
        snapshot.stale_at = snapshot.fetched_at + timedelta(minutes=5)
        # A portal scraper cannot promote itself to official API evidence.
        for row in snapshot.financials:
            for name in ("used", "limit", "remaining", "balance"):
                metric = getattr(row, name)
                if metric is not None:
                    setattr(row, name, MetricValue.from_value(
                        metric.value,
                        provenance=MetricProvenance.PROVIDER_PORTAL,
                        quality=metric.quality,
                    ))
            override = account.budget_override.get(row.currency)
            if override is not None and row.limit is None:
                row.limit = MetricValue.from_value(
                    override, provenance=MetricProvenance.CONFIGURED
                )
                row.derive_remaining()
        snapshot.warnings = [
            re.sub(r"<[^>]{0,500}>", " ", warning).strip()[:1000]
            for warning in snapshot.warnings[:50]
        ]
        snapshot.unavailable = [
            UnavailableMetric(
                field=item.field,
                code=item.code,
                message=re.sub(r"<[^>]{0,500}>", " ", item.message).strip()[:1000]
                or "插件未提供结构化不可用说明",
            )
            for item in snapshot.unavailable[:100]
        ]
        if local is not None and snapshot.tokens is None:
            snapshot.tokens = local.tokens
            snapshot.request_count = local.request_count
        return snapshot


def validate_host_request(
    manifest: dict[str, Any], account: UsageAccount, method: str, params: dict[str, Any]
) -> dict[str, Any]:
    permissions = manifest.get("permissions") or {}
    if method == "secret.resolve":
        name = str(params.get("name") or "")
        allowed = set(((manifest.get("secret_schema") or {}).get("properties") or {}).keys())
        if name not in allowed:
            raise PermissionError(f"plugin did not declare secret: {name}")
        # Bound Provider keys are intentionally resolvable only as an opaque
        # renderer-side secret_ref. Plaintext never crosses Python/WebSocket.
        if name == "provider_api_key" and manifest.get("bound_provider_key"):
            raise PermissionError(
                "provider_api_key may only be used as browser.fill secret_ref"
            )
        return {"secret": account.secret_values.get(name, "")}
    if method not in BROWSER_METHODS or not permissions.get("browser"):
        raise PermissionError(f"plugin browser method is not permitted: {method}")
    if method == "browser.pause_for_user" and not permissions.get("interactive_login"):
        raise PermissionError("plugin did not declare interactive_login")
    if method in {"browser.open", "browser.goto", "browser.fetch"}:
        url = str(params.get("url") or "")
        requested = _origin(url)
        if requested not in set(permissions.get("origins") or []):
            raise PermissionError(f"plugin URL origin is not declared: {requested}")
    if method == "browser.evaluate":
        script = str(params.get("script") or "")
        if not script or len(script) > 200_000:
            raise ValueError("browser.evaluate requires a script <= 200000 characters")
        if re.search(
            r"(?i)\b(cookie|localStorage|sessionStorage|indexedDB)\b", script
        ):
            raise PermissionError(
                "browser.evaluate cannot read browser credential/session storage"
            )
        if re.search(r"(?i)(?:\.\s*value\b|getAttribute\s*\(\s*['\"]value)", script):
            raise PermissionError(
                "browser.evaluate cannot read form values populated from secret_ref"
            )
    clean = dict(params)
    if method == "browser.fill" and clean.get("secret_ref"):
        name = str(clean.get("secret_ref"))
        allowed = set(((manifest.get("secret_schema") or {}).get("properties") or {}).keys())
        if name not in allowed:
            raise PermissionError(f"plugin did not declare secret: {name}")
        # Electron resolves this reference directly from safeStorage.  Do not
        # put the plaintext secret into a WebSocket message or renderer log.
        clean.pop("value", None)
    return clean
