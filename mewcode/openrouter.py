"""Official OpenRouter account catalog and explicit bearer auth."""

from __future__ import annotations

from urllib.parse import urlparse

import httpx

OPENROUTER_BRAND = "openrouter"
OPENROUTER_HOSTS = frozenset({"openrouter.ai", "www.openrouter.ai"})
OPENROUTER_ROOT = "https://openrouter.ai/api/v1"
OPENROUTER_APP_REFERER = "https://github.com/mewcode/mewcode"
OPENROUTER_APP_TITLE = "MewCode"
OPENROUTER_AUTH_ERROR = "OpenRouter 鉴权失败：缺少有效的 Authentication 头。"
OPENROUTER_NETWORK_ERROR = "无法连接 OpenRouter 账号模型目录。"
OPENROUTER_EMPTY_ERROR = "OpenRouter 账号模型列表为空，无法保存。"
OPENROUTER_CATALOG_REQUIRED = "OpenRouter 需要先拉取账号模型列表"
OPENROUTER_DEFAULT_NOT_IN_CATALOG = "OpenRouter 默认模型必须在账号目录中"


class OpenRouterCatalogError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        super().__init__(message)


class OpenRouterBearerAuth(httpx.Auth):
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = f"Bearer {self.api_key}"
        yield request


def normalize_openrouter_api_key(api_key: str) -> str:
    key = (api_key or "").strip()
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    if len(key) >= 2 and key[0] == key[-1] and key[0] in {"'", '"'}:
        key = key[1:-1].strip()
    return "".join(key.split())


def openrouter_http_error(response: httpx.Response) -> OpenRouterCatalogError:
    message = ""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "").strip()
        elif isinstance(error, str):
            message = error.strip()
    lowered = message.lower()
    if response.status_code in {401, 403}:
        if "missing" in lowered and "authentication" in lowered:
            return OpenRouterCatalogError("auth", OPENROUTER_AUTH_ERROR)
        if message:
            return OpenRouterCatalogError("auth", f"OpenRouter 鉴权失败：{message}")
        return OpenRouterCatalogError("auth", OPENROUTER_AUTH_ERROR)
    suffix = f"（{response.status_code}）"
    if message:
        return OpenRouterCatalogError("http", f"{OPENROUTER_NETWORK_ERROR}{suffix} {message}")
    return OpenRouterCatalogError("http", f"{OPENROUTER_NETWORK_ERROR}{suffix}")


def is_openrouter_endpoint(base_url: str) -> bool:
    host = (urlparse((base_url or "").strip()).hostname or "").lower()
    return host in OPENROUTER_HOSTS


def is_official_openrouter(*, brand: str = "", base_url: str = "") -> bool:
    if (brand or "").strip().lower() != OPENROUTER_BRAND:
        return False
    return is_openrouter_endpoint(base_url)


def openrouter_api_root(base_url: str) -> str:
    if is_openrouter_endpoint(base_url):
        return OPENROUTER_ROOT
    value = (base_url or OPENROUTER_ROOT).strip().rstrip("/")
    return value or OPENROUTER_ROOT


def openrouter_request_headers(
    api_key: str, existing: dict[str, str] | None = None
) -> dict[str, str]:
    headers = {str(key): str(value) for key, value in (existing or {}).items()}
    key = normalize_openrouter_api_key(api_key)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    headers.setdefault("HTTP-Referer", OPENROUTER_APP_REFERER)
    headers.setdefault("X-Title", OPENROUTER_APP_TITLE)
    headers.setdefault("X-OpenRouter-Title", OPENROUTER_APP_TITLE)
    return headers


def merge_llm_headers(
    base_url: str, api_key: str, existing: dict[str, str] | None = None
) -> dict[str, str] | None:
    headers = {str(key): str(value) for key, value in (existing or {}).items()}
    if is_openrouter_endpoint(base_url):
        headers = openrouter_request_headers(api_key, headers)
    return headers or None


async def fetch_openrouter_user_models(
    *,
    api_key: str,
    base_url: str,
    timeout: float = 8.0,
    client: httpx.AsyncClient | None = None,
) -> list[str]:
    key = normalize_openrouter_api_key(api_key)
    if not key:
        raise OpenRouterCatalogError("auth", OPENROUTER_AUTH_ERROR)
    root = openrouter_api_root(base_url)
    headers = openrouter_request_headers(key)
    auth = OpenRouterBearerAuth(key)

    async def get(url: str) -> httpx.Response:
        if client is not None:
            return await client.get(url, headers=headers, auth=auth)
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True
        ) as http:
            return await http.get(url, headers=headers, auth=auth)

    try:
        # OpenRouter 没有按账号区分的目录端点（/models/user 不存在：认证
        # 通过后返回 400）。正确做法：用 /key 校验 key 有效性，用公开的
        # /models 拉取模型目录。
        key_response = await get(f"{root}/key")
        if key_response.status_code >= 400:
            raise openrouter_http_error(key_response)
        models_response = await get(f"{root}/models")
        if models_response.status_code >= 400:
            raise openrouter_http_error(models_response)
    except httpx.TimeoutException as exc:
        raise OpenRouterCatalogError("network", OPENROUTER_NETWORK_ERROR) from exc
    except httpx.HTTPError as exc:
        raise OpenRouterCatalogError("network", OPENROUTER_NETWORK_ERROR) from exc

    try:
        payload = models_response.json()
    except ValueError as exc:
        raise OpenRouterCatalogError("http", OPENROUTER_NETWORK_ERROR) from exc

    raw = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        raw = []
    models: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            model_id = str(item.get("id") or "").strip()
        else:
            model_id = str(item).strip()
        if model_id:
            models.append(model_id)
    models = list(dict.fromkeys(models))
    if not models:
        raise OpenRouterCatalogError("empty", OPENROUTER_EMPTY_ERROR)
    return models
