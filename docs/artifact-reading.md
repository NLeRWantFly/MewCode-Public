# 审稿读取、缓存与本地读写用量

`ReadAuditArtifact` 可按页读取不超过 2 GiB 的 PDF，返回页文字或真实页面像素。原来的 64 MiB 门槛继续用于文本、图片、DOCX 和审稿清单；大 PDF 页面证据也能登记到 `ReviewWorkQueue`。图片仍须符合既有大小、像素、视觉能力和验收去重约束。

## 使用方式

以下为工具参数示例，路径须属于本工具允许的基线或候选目录。

```json
{"file_path":"book.pdf","page":18,"render_page":true,"criterion_id":"page-18"}
```

| 操作 | 参数 | 行为 |
| --- | --- | --- |
| 正常读取 | `cache_mode="use"`，默认 | 复用当前文件版本的指纹和页内容 |
| 强制刷新 | `cache_mode="refresh"` | 清理该文件的缓存，重新读取并缓存；其他页面下次重新解析 |
| 绕过缓存 | `cache_mode="bypass"` | 本次重新读取，不读取、修改或写入缓存 |
| 查看用量 | `action="cache_info"` | 返回允许目录中的缓存条目、容量及本工具累计 I/O |
| 清理缓存 | `action="clear_cache"` | 清理允许目录中的缓存，返回移除数量 |
| 单文件操作 | 缓存操作额外提供 `file_path` | 只查看或清理该文件；仍须属于允许目录 |
| 接续文字 | `offset`、`char_offset` | 使用返回的 `NEXT_OFFSET`、`NEXT_CHAR_OFFSET`，直到 `HAS_MORE=false` |

```json
{"action":"cache_info"}
{"action":"clear_cache","file_path":"book.pdf"}
{"file_path":"book.pdf","page":18,"cache_mode":"refresh"}
```

`offset` 从 0 开始，`page` 从 1 开始。长行返回字符游标，输出保留宿主截断所需空间，接续不会跳过被截断的内容。PDF 文字提取不代表图像和版式已核对，视觉验收须用 `render_page=true`。

## 缓存边界

缓存位于后端进程内存，默认最多 64 MiB 内容、256 条、单条 8 MiB，条目自载入后有效 600 秒。超限条目可读取但不缓存；LRU 淘汰、重启和清理均可能导致下次重新读取。这是缓存内容大小，不是整个进程的驻留内存预算。

指纹按规范路径和文件版本复用，页面文字和像素按页分别缓存。版本包含文件身份、大小、修改时间、状态变化时间；Windows 额外查询文件系统 ChangeTime，识别保留大小和修改时间的改写。缓存命中及解析前后均核对版本，源文件在读取期间变化会失败，不交付旧内容。需要明确重新扫描原字节时使用 `refresh` 或 `bypass`。

同一版本的完整原字节 SHA-256 首次仍须流式扫描全文件一次；后续页面和审稿 PDF 证据核对复用该指纹。页文字和渲染用 PDFium 可寻址输入，避免为访问一页把整本 PDF 复制到 Python 内存。并发的相同指纹或页读取共用一次加载，PDFium 对象生命周期保持串行。

缓存不授予额外目录权限，也不代替视觉证据。每次图片交付仍核对视觉能力、当前验收项、裁剪、像素去重及原页覆盖；清理或刷新缓存不会清除视觉验收台账。仅 PDF 页面来源及原字节指纹允许使用 2 GiB 门槛，清单和文本证据的整文件读取仍受原门槛约束。

本工具不写业务源文件，不把缓存写到磁盘。取消异步读取会停止交付；已启动的原生解析可能继续到结束，其实际 I/O 仍计入累计值，期间 `active_reads` 可大于 0。

## 用量口径

每次读取输出有 `READ_USAGE` 摘要，结构化指标在 `ToolResult.host_observation.artifact_io`。`cache_info.io_totals` 是当前工具实例的累计值，重启或新实例重新计数。

| 指标 | 含义 |
| --- | --- |
| `source_read_bytes` / `source_read_calls` | 原文件接口实际返回的字节和调用数；重复读取同一区间重复计数 |
| `hash_read_bytes` / `parser_read_bytes` | 用于完整指纹和内容解析的原文件字节 |
| `cache_read_bytes` / `cache_write_bytes` | 内存缓存读写内容字节，写缓存不等于写业务文件 |
| `hash_cache_hit` / `data_cache_hit` | 指纹及内容是否复用 |
| `coalesced_reads` | 复用并发在途加载的次数 |
| `source_write_bytes` | 本工具始终为 0 |
| `output_text_bytes` / `output_image_bytes` | 返回文字 UTF-8 字节和图片编码字节，不含 base64 膨胀 |
| `elapsed_ms` | 调用耗时，包含等待并发加载或 PDFium 的时间 |
| `read_calls` / `failed_calls` / `cancelled_calls` / `active_reads` | 累计调用、失败、取消及未结束的后台读取，仅见于累计值 |

计数排除路径/版本元数据读取，不代表 OS 物理磁盘 I/O、模型缓存 token、账单或费用；也不包含其他工具、审稿队列自身的独立 I/O 或整个应用的读取量。

## 本轮验证（2026-10-02）

有效合成 PDF 为 **67,109,787 字节**、两页，带未引用的大数据流。场景在同一缓存实例按下表顺序执行：

| 场景 | 指纹读取字节 | 解析读取字节 | 缓存状态 |
| --- | ---: | ---: | --- |
| 首次读取第 1 页文字 | 67,109,787 | 2,014 | miss |
| 再读第 1 页文字 | 0 | 0 | hit |
| 首次读取第 2 页文字 | 0 | 3,038 | partial |
| 首次渲染第 1 页 | 0 | 2,014 | partial |
| 同页像素、另一个验收项 | 0 | 0 | hit |
| 明确绕过缓存 | 67,109,787 | 2,014 | bypassed |
| 明确刷新缓存 | 67,109,787 | 2,014 | miss |
| 刷新后再读 | 0 | 0 | hit |

源版本前后一致，源写入均为 0；本轮验证未读取业务 PDF、未调用模型。报告：`.tmp/artifact-read-20261002/read-usage-report.json`。合成验证不承诺每份 PDF 的解析字节和耗时相同，首次完整指纹成本仍随源文件大小增长。

最终 10 文件联合回归 **120 passed / 56.63s**；启动检查 **33 passed / 55.96s**。范围重叠，不累加。覆盖大 PDF 文本/像素、台账接入、指纹共享、保留修改时间的改写、并发复用、取消计量、缓存操作、容量/期限/作用域、长行接续、编码及既有返修证据约束。

```powershell
$env:MEWCODE_USER_DATA_DIR = Join-Path $PWD '.tmp/artifact-read-20261002/userdata-final2'
$env:MEWCODE_DESKTOP_PROFILE_ISOLATED = '1'
.venv/Scripts/python.exe -m pytest tests/test_artifact_read_cache.py tests/test_read_audit_artifact.py tests/test_audit_unicode_repair.py tests/test_audit_evidence.py tests/test_review_work_queue.py tests/test_repair_work_batch.py tests/test_audit_protocol.py tests/test_artifact_inventory.py tests/test_serialization.py tests/test_verify_artifacts.py -q --tb=short -p no:cacheprovider --basetemp=.tmp/artifact-read-20261002/pytest-final2
.venv/Scripts/python.exe scripts/check_startup.py
.venv/Scripts/python.exe .tmp/artifact-read-20261002/measure_artifact_reads.py
```

日常应用最后只读观察为执行中、当前阶段 6/32、已完成 2、进行中 1，因此未在本轮重启；新实现将在下一次正常后端重启后加载。旧卡片的 64 MiB 失败属于历史回执，真实业务重试、隔离执行器准入及整个 N03 验收须分别取得成功证据。
