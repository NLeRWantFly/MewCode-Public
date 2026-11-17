# 用量账户与自动化插件协议 v1

MewCode 把模型调用 Provider 与用量账户分开管理。一个用量账户可以关联多个
`provider_key`，财务数据来自官方 API 或 Provider 门户，本地请求账本只补充
Token 与请求数。无法取得的财务字段必须写入 `unavailable`，不能用 `0` 代替。

## 统一触发器

```http
POST /api/usage/accounts/{account_id}/refresh
```

成功接受后返回 HTTP `202`：

```json
{
  "run_id": "…",
  "run": { "status": "queued" }
}
```

调用方通过 `GET /api/usage/runs/{run_id}` 或 WebSocket
`usage_run.updated` 观察 `queued / running / action_required / succeeded /
partial / failed / cancelled`。同一账户同时只运行一个任务；一分钟内的非强制
刷新复用缓存。桌面端会在打开用量面板时先显示缓存，再触发所有启用账户。

## ZIP 结构

插件包最大 20 MiB，解压后最大 40 MiB、500 个文件：

```text
usage-plugin.json
plugin.py
可选的纯 Python vendored 依赖
```

v1 拒绝路径穿越、符号链接、原生库、可执行文件、安装脚本、
`requirements.txt` 与在线 `pip install`。安装前 UI 会展示 SHA-256、Origin 和
浏览器权限，并要求用户明确确认信任。

Manifest 示例：

```json
{
  "id": "example.relay",
  "name": "Example Relay",
  "version": "1.0.0",
  "protocol_version": "1",
  "entrypoint": "plugin.py",
  "operations": ["collect", "normalize"],
  "config_schema": {
    "type": "object",
    "properties": { "tenant": { "type": "string" } },
    "required": ["tenant"],
    "additionalProperties": false
  },
  "secret_schema": {
    "type": "object",
    "properties": {
      "username": { "type": "string", "title": "用户名" },
      "password": { "type": "string", "title": "密码" }
    },
    "required": ["username", "password"],
    "additionalProperties": false
  },
  "artifact_schema": {
    "type": "object",
    "properties": { "billing": { "type": "object" } },
    "required": ["billing"]
  },
  "permissions": {
    "browser": true,
    "interactive_login": true,
    "origins": ["https://console.example.com"]
  }
}
```

所有 Schema 按 JSON Schema 2020-12 校验。`config_schema` 和
`secret_schema` 在运行前校验；`RawArtifact` 在 `collect` 与 `normalize` 之间由
宿主进行大小与 `artifact_schema` 校验。v1 的 `$ref` / `$dynamicRef` 只允许
引用同一 Schema 内的 `#` JSON Pointer，不加载网络或本地文件引用。

## Python 接口

入口模块必须导出以下两个函数；同步函数与 `async def` 都可使用：

```python
async def collect(query, context):
    await context.browser.open("https://console.example.com/billing")
    await context.browser.fill("#username", secret_ref="username")
    await context.browser.fill("#password", secret_ref="password")
    await context.browser.click("button[type=submit]")
    if await context.browser.text("body") == "需要验证码":
        await context.browser.pause_for_user("请完成验证码后点击继续")
    billing = await context.browser.evaluate(
        "document.querySelector('#billing-json').textContent"
    )
    return {"billing": billing}


async def normalize(query, artifact):
    return {
        "status": "ok",
        "financials": [{
            "currency": "USD",
            "used": {
                "value": "12.34",
                "provenance": "provider_portal",
                "quality": "exact"
            },
            "limit": None,
            "remaining": None,
            "balance": None
        }],
        "unavailable": [{
            "field": "financials.USD.limit",
            "code": "not_exposed",
            "message": "门户没有展示本周期额度"
        }],
        "warnings": []
    }
```

`query.period` 使用 UTC 自然月。`context.config` 是账户公开配置。建议密码只通过
`browser.fill(..., secret_ref="password")` 使用：引用由 Electron 主进程直接从
safeStorage 解析，明文不会进入 WebSocket 或 Python 插件。插件也可显式调用
`await context.secret(name)`，但此时该声明过的密钥会进入可信插件进程。

## 浏览器桥

`context.browser` 提供：

- `open(url, visible=False)`、`goto(url)`、`wait_for(selector, timeout_ms=…)`
- `click(selector)`、`fill(selector, value=…|secret_ref=…)`、`text(selector)`
- `evaluate(script)`、`fetch(url, options)`
- `pause_for_user(prompt)`、`close()`

每个账户使用独立的持久化 Electron session partition。导航与网络请求只能访问
Manifest 声明的 Origin；权限请求默认拒绝。Cookie 保留在 Electron session 中，
协议没有读取或导出 Cookie 的方法，`evaluate` 也禁止访问 Cookie 与浏览器存储。
验证码、扫码或 2FA 会把任务置为
`action_required`，人工窗口最长等待 10 分钟。

## 标准快照约束

金额一律是十进制字符串。每个金额字段必须携带：

```json
{
  "value": "12.3400",
  "provenance": "official_api | provider_portal | local_ledger | configured | derived",
  "quality": "exact | estimate"
}
```

插件输出的财务来源会由宿主强制降为 `provider_portal`，快照证据强制为
`relay_reported`，插件不能自行提升为官方证据。只有同一货币、同一周期的
`limit - used` 可以派生 `remaining`；禁止通过 `used + balance` 反推总额度，也
不执行汇率换算。

## 运行边界

插件以 `python -I`、净化环境和临时工作目录运行。普通采集总时限 120 秒，
标准输出总上限 1 MiB；超时会终止进程树。只持久化标准快照、插件版本、哈希和
脱敏错误，不保存 RawArtifact、HTML、Cookie、密码或完整插件输入。

这是针对稳定性和误操作的隔离，不是恶意 Python 代码沙箱。插件仍是用户确认后
信任的本机代码；只能安装来源可信、SHA-256 可核对的包。
