# 用量追溯与统计

## 结论先行

MewCode 把“用量”拆成四种不能混为一谈的数据：

1. 请求响应中的 token 遥测；
2. OpenAI 官方组织 Usage/Costs；
3. 用户设置的组织/项目 hard spend limit 及其剩余量；
4. 预付费信用余额、OpenAI 批准的 usage-tier 上限和短时 rate limit。

前 3 类可以在满足凭证和绑定条件时追溯。第 4 类中的短时 rate limit 可以从响应头读取，但它不是账户余额；OpenAI 当前公开 Admin API 没有返回“预付费信用余额”或“获批 usage-tier 上限”的数值接口，因此系统必须显示“不可用”，不能拿 hard spend limit 或限流余量冒充。

对于中转站也有一个不可绕过的边界：如果中转站使用自己的托管 OpenAI 账号，又不披露上游账号、project/API-key 绑定和上游请求回执，客户端只看到中转站声明，无法从密码学或 OpenAI 官方侧证明真实扣费账号。要获得可核验结果，必须使用下文的 BYOK/独占项目方案，或要求中转站实现回执协议。

## 模块结构

```text
模型响应 ──> LLMClient 归一化 ──> RequestUsage ──> SQLite 请求账本
                                                    │
OpenAI Admin API ──> UsageBackend ──> Usage/Costs/Spend Limit 快照
                                                    │
                              /usage <── UsageService 对账
```

- `mewcode/usage/models.py`：统一数据模型、证据等级和归属状态。
- `mewcode/usage/backend.py`：provider 无关的账户用量接口。
- `mewcode/usage/ledger.py`：并发安全的 SQLite 请求账本。
- `mewcode/usage/openai.py`：OpenAI Usage、Costs 和 Spend Limit 的官方实现。
- `mewcode/usage/service.py`：客户端绑定、当前 Agent/Session/Trace 关联和对账。
- `mewcode/commands/handlers/usage.py`：`/usage` 查询入口。

账本默认位置是 `<workspace>/.mewcode/usage/ledger.sqlite3`。它只保存凭证的 SHA-256 截断指纹，不保存 API key 或原始响应正文。

## 统一接口

`UsageBackend` 提供两个账户级操作：

```python
async def fetch_usage(query: UsageQuery) -> AccountUsageSummary: ...

async def fetch_account_snapshot(
    query: UsageQuery, *, provider_name: str = ""
) -> AccountSnapshot: ...
```

`AccountSnapshot` 分开保存：

- `scope_cost`：project/API-key 过滤范围的官方实际费用；
- `organization_cost`：整个组织的官方实际费用；
- `organization_limit`、`project_limits`：hard spend limit、同期已用和可用；
- `prepaid_credit_balance`：当前 OpenAI 公共 API 不可读，保持 `None`；
- `approved_usage_limit`：当前 OpenAI 公共 API 不可读，保持 `None`；
- `evidence`、`attribution`、`attribution_reason`：数据来源和归属强度。

这样上层 UI 不会把“限流剩余 token”“支出上限剩余美元”和“预付费余额”错误地显示成同一个额度。

## 证据等级

| 等级 | 含义 | 可声称的结论 |
|---|---|---|
| `official_account` | 从 `api.openai.com` 的 Admin API 获取 | 该组织/过滤范围的官方聚合用量和费用真实 |
| `official_response` | 直连官方模型响应及响应头 | 该请求返回的 token、request ID 和限流信息真实 |
| `relay_attested` | 预留给已完成签名验证的中转回执 | 只能按回执覆盖范围声称 |
| `relay_reported` | 中转响应或未验证的签名声明 | 只能说“中转站报告”，不能说“OpenAI 官方核验” |
| `local_estimate` | 本地 tokenizer/价格估算 | 只能估算，不能作为账单 |
| `unavailable` | 没有数据源 | 明确显示不可用 |

归属状态与证据等级独立：

- `request_verified`：单请求归属有官方响应身份；
- `aggregate_verified`：官方聚合按独占 project/API-key 核验；
- `configured`：绑定来自配置，但缺少独立证明；
- `unverifiable`：无法证明中转扣费账号；
- `mismatch`：配置的 organization 与官方响应头不一致。

## OpenAI / GPT-5.6 实现

GPT-5.6 请求账本保存：

- 未缓存输入 token；
- cache read token；
- cache write token；
- 输出 token；
- 输出中的 reasoning token 子集；
- 响应模型（用于发现 `gpt-5.6` 别名实际路由结果）、response ID、request ID、service tier、system fingerprint；
- `x-ratelimit-*` 响应头。

账户级统计不在客户端硬编码 GPT-5.6 单价，而是读取 OpenAI Costs API 的实际金额。这可以覆盖缓存、长上下文、服务层级和未来价格变化。Usage API 用于按 model/project/API-key/service tier 对 token 和请求数分组。

OpenAI 官方文档要求组织 Usage/Costs 使用 Admin API key；普通项目调用 key 不具备组织账单权限。相关依据见 [Admin API keys](https://developers.openai.com/api/docs/guides/admin-apis)、[Usage API 示例](https://developers.openai.com/cookbook/examples/completions_usage_api) 和 [Spend limits](https://developers.openai.com/api/docs/guides/spend-limits)。GPT-5.6 模型与缓存字段见 [GPT-5.6 model guidance](https://developers.openai.com/api/docs/guides/model-guidance?model=gpt-5.6)。

## 直连 OpenAI 配置

建议为 MewCode 创建独占 OpenAI project 和 project API key，再用组织 Admin key 查询账单。Admin key 应通过环境变量注入，不要写入仓库。

```yaml
providers:
  - name: openai-gpt56
    protocol: openai
    base_url: https://api.openai.com/v1
    model: gpt-5.6
    api_key: ${OPENAI_API_KEY}
    usage:
      enabled: true
      account_tracking: true
      admin_api_key_env: OPENAI_ADMIN_KEY
      admin_base_url: https://api.openai.com/v1
      upstream_organization_id: org_xxx
      project_ids: [proj_mewcode]
      api_key_ids: [key_mewcode]
      attribution_mode: direct
```

`project_ids`/`api_key_ids` 是官方聚合过滤条件。若不使用独占绑定，官方组织总账仍是真实的，但其中可能包含其他应用，MewCode 会把归属降级为 `configured`。

## 中转站配置

### 推荐：BYOK + 独占 OpenAI project/API key

中转只负责转发，请求最终使用你自己的 OpenAI key；同时用该 OpenAI 组织的 Admin key读取官方账单：

```yaml
providers:
  - name: relay-gpt56-byok
    protocol: openai-compat
    base_url: https://relay.example/v1
    model: gpt-5.6
    api_key: ${RELAY_OR_BYOK_KEY}
    usage:
      enabled: true
      account_tracking: true
      admin_api_key_env: OPENAI_ADMIN_KEY
      admin_base_url: https://api.openai.com/v1
      upstream_organization_id: org_xxx
      project_ids: [proj_mewcode]
      api_key_ids: [key_mewcode]
      attribution_mode: byok-relay
```

此模式能核验“独占范围的官方聚合”，但 OpenAI 聚合 Usage API 不提供逐请求 ID，所以单请求关联还要依赖本地 `x-request-id` 和中转回执。

### 托管账号中转

若中转使用自己的上游 key，至少应回传：

```text
X-MewCode-Upstream-Request-Id
X-MewCode-Upstream-Organization-Id
X-MewCode-Upstream-Project-Id
X-MewCode-Upstream-Api-Key-Id
X-MewCode-Usage-Receipt
X-MewCode-Usage-Signature
```

当前实现会保存这些身份字段、回执哈希和“是否带签名”，但在没有受信公钥和签名验证实现时仍标记为 `relay_reported`，不会假装它是 OpenAI 官方证据。要达到独立核验，中转方还必须提供以下至少一种能力：

1. 把请求绑定到用户可查询的独占 OpenAI project/API key；
2. 让用户使用上游组织 Admin API 对该 project/API key 聚合对账；
3. 提供可验证签名的不可变回执，并允许用上游 request ID 审计；
4. 暴露上游真实 spend limit/credit 数据源及其证据来源。

如果这些条件都不满足，“真实调用账号的实际用量和剩余额度”在信息论上不可从客户端恢复，系统会显示 `unverifiable`。

## 查询

```text
/usage                 # 本地账本 + 官方账户快照
/usage local           # 只看本月本地请求
/usage account         # 只看 OpenAI 官方 Costs/Spend Limit
/usage reconcile       # 对比本地 token 与官方聚合 token
/quota                 # /usage 的别名
```

对账差额通常来自时间窗口边界、同 project 的其他调用、中转未回传 usage、流中断或官方聚合延迟。只有独占 project/API key 的官方聚合才能标记为 `aggregate_verified`；当前 OpenAI Usage API 没有 request-ID 分组维度，因此不会标记为逐请求精确核销。

## 验收规则

- 直连 OpenAI + Admin key + 独占 project/API key：官方聚合与 hard spend remaining 可核验。
- 直连 OpenAI但无独占绑定：官方总账可读，MewCode 请求归属不可隔离。
- BYOK 中转 + 上游 Admin key + 独占绑定：官方聚合可核验，逐请求需回执。
- 托管中转只有自报 token/余额：标记 `relay_reported`/`unverifiable`。
- 任何模式都不得把 `x-ratelimit-remaining-tokens` 当作账户剩余额度。
- OpenAI 未公开的 prepaid credit/approved usage-tier 数值必须显示“不可用”，不得估算成真实余额。
