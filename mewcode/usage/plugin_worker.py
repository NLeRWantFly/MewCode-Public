"""Isolated JSONL worker for trusted usage plugins.

This module is intentionally dependency-free.  The host starts it with
``python -I`` and sends one invocation object over stdin.  Browser and secret
operations are JSON-RPC requests routed back through the host.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import sys
import uuid
from pathlib import Path
from typing import Any


MAX_MESSAGE_BYTES = 1024 * 1024


def _read_message() -> dict[str, Any]:
    line = sys.stdin.buffer.readline(MAX_MESSAGE_BYTES + 1)
    if not line:
        raise RuntimeError("plugin host closed stdin")
    if len(line) > MAX_MESSAGE_BYTES:
        raise RuntimeError("plugin host message exceeded 1 MiB")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise RuntimeError("plugin host message must be a JSON object")
    return value


def _write_message(value: dict[str, Any]) -> None:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_MESSAGE_BYTES:
        raise RuntimeError("plugin output exceeded 1 MiB")
    sys.stdout.buffer.write(raw + b"\n")
    sys.stdout.buffer.flush()


class HostBridge:
    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        request_id = uuid.uuid4().hex
        _write_message({
            "type": "host.request",
            "id": request_id,
            "method": method,
            "params": params or {},
        })
        response = await asyncio.to_thread(_read_message)
        if response.get("type") != "host.response" or response.get("id") != request_id:
            raise RuntimeError("plugin host returned an invalid RPC response")
        if response.get("error"):
            raise RuntimeError(str(response["error"]))
        return response.get("result")

    async def open(self, url: str, *, visible: bool = False) -> Any:
        return await self.request("browser.open", {"url": url, "visible": visible})

    async def goto(self, url: str) -> Any:
        return await self.request("browser.goto", {"url": url})

    async def wait_for(self, selector: str, *, timeout_ms: int = 30000) -> Any:
        return await self.request(
            "browser.wait_for", {"selector": selector, "timeout_ms": timeout_ms}
        )

    async def click(self, selector: str) -> Any:
        return await self.request("browser.click", {"selector": selector})

    async def fill(
        self, selector: str, *, value: str | None = None, secret_ref: str | None = None
    ) -> Any:
        params: dict[str, Any] = {"selector": selector}
        if secret_ref:
            params["secret_ref"] = secret_ref
        else:
            params["value"] = value or ""
        return await self.request("browser.fill", params)

    async def text(self, selector: str) -> Any:
        return await self.request("browser.text", {"selector": selector})

    async def evaluate(self, script: str) -> Any:
        return await self.request("browser.evaluate", {"script": script})

    async def fetch(self, url: str, options: dict[str, Any] | None = None) -> Any:
        return await self.request("browser.fetch", {"url": url, "options": options or {}})

    async def pause_for_user(self, prompt: str) -> Any:
        return await self.request("browser.pause_for_user", {"prompt": prompt})

    async def close(self) -> Any:
        return await self.request("browser.close", {})

    async def secret(self, name: str) -> str:
        result = await self.request("secret.resolve", {"name": name})
        return str(result or "")


class PluginContext:
    def __init__(self, value: dict[str, Any]) -> None:
        self.account_id = str(value.get("account_id") or "")
        self.config = dict(value.get("config") or {})
        self.browser = HostBridge()

    async def secret(self, name: str) -> str:
        return await self.browser.secret(name)


async def _call(function: Any, *args: Any) -> Any:
    value = function(*args)
    return await value if inspect.isawaitable(value) else value


def _load(entrypoint: Path) -> Any:
    # ``-I`` deliberately removes the working directory and user site from the
    # import path.  Add only the installed plugin directory so bundled,
    # pure-Python dependencies remain importable without exposing the host.
    plugin_root = str(entrypoint.parent)
    if plugin_root not in sys.path:
        sys.path.insert(0, plugin_root)
    spec = importlib.util.spec_from_file_location("mewcode_usage_plugin", entrypoint)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load plugin entrypoint")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "collect", None)):
        raise RuntimeError("plugin entrypoint must define collect(query, context)")
    return module


async def run(entrypoint: Path) -> None:
    invocation = await asyncio.to_thread(_read_message)
    if invocation.get("type") != "invoke" or invocation.get("protocol_version") != "1":
        raise RuntimeError("unsupported plugin invocation protocol")
    module = _load(entrypoint)
    query = dict(invocation.get("query") or {})
    context = PluginContext(dict(invocation.get("context") or {}))
    artifact = await _call(module.collect, query, context)
    # Force both stages through the JSON contract.  This also rejects objects
    # that could smuggle live browser/session handles into normalize().
    artifact_json = json.dumps(
        artifact, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if len(artifact_json) > MAX_MESSAGE_BYTES:
        raise RuntimeError("plugin RawArtifact exceeded 1 MiB")
    artifact = json.loads(artifact_json)
    artifact_id = uuid.uuid4().hex
    _write_message({"type": "artifact", "id": artifact_id, "artifact": artifact})
    approval = await asyncio.to_thread(_read_message)
    if (
        approval.get("type") != "artifact.approved"
        or approval.get("id") != artifact_id
    ):
        raise RuntimeError("plugin host rejected the RawArtifact contract")
    normalize = getattr(module, "normalize", None)
    snapshot = await _call(normalize, query, artifact) if callable(normalize) else artifact
    if not isinstance(snapshot, dict):
        raise RuntimeError("plugin collect()/normalize() must return a JSON object")
    _write_message({"type": "result", "snapshot": snapshot})


def main(argv: list[str] | None = None) -> int:
    args = list(argv or sys.argv[1:])
    if len(args) != 1:
        sys.stderr.write("usage: plugin_worker.py <entrypoint>\n")
        return 2
    try:
        asyncio.run(run(Path(args[0]).resolve()))
    except Exception as exc:
        try:
            _write_message({"type": "error", "error": str(exc)[:2000]})
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
