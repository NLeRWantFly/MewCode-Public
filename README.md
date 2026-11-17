# MewCode

MewCode 是包含终端界面和 Tauri 桌面客户端的 AI 编程助手，支持多 Provider、工具调用、MCP、Skill、任务检查点和可视化工作流。

## 当前工作流

首次任务由主对话探索和推进，图记录公开步骤、结果与产物。图支持顺序、条件分支、嵌套循环、子 Agent 并发和依赖汇合。任务完成后可提取可复用流程；明确复用并绑定新输入后，才按图执行。

详见 [工作流说明](docs/goal-workflow-v1.md) 与 [桌面开发说明](README.tauri.md)。

## 本地运行

需要 Python 3.11 或更新版本及 uv：

```sh
uv sync --group dev
uv run mewcode
```

网页客户端：

```sh
uv run python frontend/server.py --host 127.0.0.1 --port 8765
```

浏览器访问 `http://127.0.0.1:8765`。桌面端构建需要 Rust 和 Tauri，参见桌面开发说明。

## Provider 配置

此公开快照不携带任何可用密钥、个人 Provider 配置、账户记录或运行历史。请自行配置 Provider；示例位于 [config.example.yaml](config.example.yaml)，密钥从本机环境变量读取。运行时配置、会话、数据库和产物应保持在 Git 忽略目录中。

## 验证

```sh
uv run python scripts/check_startup.py
uv run pytest tests/workflow_core/test_trace.py tests/workflow_core/test_chat_tools.py
```

本仓库是独立脱敏快照；日常开发继续在原开发仓库进行。此仓库不自动同步开发历史，后续公开更新应重新生成并检查脱敏快照。

公开版不包含私人运行的回放捕获，依赖这些捕获的两项测试会跳过；TLS 测试使用 OpenSSL 在临时目录生成一次性证书。
