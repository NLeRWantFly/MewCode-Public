# Windows P0 原生执行边界：当前实现与验收状态

非 Docker AUTO 的架构取舍、Codex 开源实现映射及后续门槛见 [AUTO 原生执行机制](auto-mode-codex-native-design.md)。本页保留本机探针的原始失败事实；旧的「Docker 仅用于显式严格隔离档」不代表 AUTO 会使用 Docker。

## 2026-09-25 对照复测：撤销旧敏感读通过结论

下文早期记录中的“临时 `.env` 读取被拒”**不能作为敏感路径隔离通过证据**：旧夹具没有普通文件可读的阳性对照，可能仅受临时目录原有 ACL 保护。修订后的探针在新建合成目录给两个文件相同的普通读取权限（只改合成目录，`finally` 撤销），沙箱必须先读到 `ordinary.txt`，再验证 `.env` 被拒。现场结果是**两个文件均读取成功、退出码 0**。固定 deny 清单没有覆盖任意外部秘密样式文件。

网络探针也改为宿主监听器实际接收随机 nonce，而非在整段输出中搜索字符串。现场宿主分别收到了阳性对照和沙箱子进程的 nonce，确认 **loopback 真实可达**。空命令启动探针通过。三项现场复测合计 **1 通过、2 失败**；没有通过原生边界验收。

`NativeExecutionReceipt` 现在分别保存执行状态、退出码、标准输出和标准错误；只有成功退出且标准输出含唯一精确标记才算探针输出证据。超时、启动失败、错误回显和重复标记不能被当作拦截成功。该回执仅是诊断数据，不能授权或解锁 AUTO。

该修订后的权限/前缀规则/原生边界/缓存/授权定向回归：`265 passed, 5 skipped`（12.97 秒）。这是普通回归结果，不包含上述显式启用且失败的现场逃逸验收。`git diff --check` 通过。

公开源代码提供了能力限制的解释，而不是补丁完成的证明：当前 Codex 的 [Windows deny-read 解析器（与本机版本对应）](https://github.com/openai/codex/blob/rust-v0.155.0-alpha.16/codex-rs/windows-sandbox-rs/src/deny_read_resolver.rs) 将 glob 展开成已有文件的 ACL 快照，不是对未来任意文件的内核级名称过滤；[WFP 过滤器列表](https://github.com/openai/codex/blob/main/codex-rs/windows-sandbox-rs/src/wfp/filter_specs.rs)也不能单独证明所有 TCP loopback 已隔离。因此不能用扩大一次性扫描或重复安装相同规则冒充完整修复。

宿主只读环境核查：WSL2 服务存在，但发行版列表只有 `docker-desktop`；没有独立 Linux 发行版可直接复用。本次未启动该发行版、未使用 Docker、未改变系统防火墙和账户，也未安装新发行版。后续若采用独立 WSL2 + Codex Linux 内核边界，需要先明确该执行环境变更；Windows 命令兼容、挂载、interop、敏感读和网络仍须重新验收。不能把“装了 WSL”算作沙箱通过。

2026-09-25 原 P0 试点：AUTO 的 Bash/RunVerification 尝试复用本机安装的 Codex Windows elevated 原生沙箱，不通过 `PATH` 查找执行器，也不在后端缺失时回退宿主 Shell。现场验收失败后，elevated 只保留为候选；后续按 [非 Docker AUTO 设计](auto-mode-codex-native-design.md)评估 MXC 与修复后的 elevated。显式严格隔离档仍使用原有 Docker 后端；它不是 AUTO 后备。选择原试点的依据是 elevated 实现使用低权限沙箱用户、文件系统权限边界与防火墙；本项目不在 P0 自行管理 Windows 用户、ACL 和防火墙。参见 [Windows sandbox](https://learn.chatgpt.com/docs/windows/windows-sandbox) 与 [Permissions](https://learn.chatgpt.com/docs/permissions)。

执行器候选强制 `windows.sandbox="elevated"`、`:workspace` 派生权限、`network.enabled=false`，并对固定凭据/浏览器资料路径和项目权限权威文件设置 `deny`；子进程继承的密钥环境变量经 `_child_env` 过滤。超时或取消时按本次启动的 PID 清理进程树。AUTO Bash/RunVerification 的宿主 Shell 回退已移除；包括显式 allow 规则在内，只有运行边界通过验收后才能到达执行器。当前运行门禁尚未通过，这些命令立即返回 `AUTO_BOUNDARY_DENIED`，不先消耗模型审查。

这仍**不是工作区脚本免审查的安全完成，也不是可上线的 AUTO 命令边界**。本机 Windows 10 19045 上，已安装后端接受固定敏感路径 `deny`，并在现场探针中拒绝工作区外写入；合成的临时目录 `.env` 读取也被拒绝。但尝试 `:root=deny` 加 `:minimal=read` 的默认不可读策略时，elevated 后端报 `requires effective :root read access`，因此一个通过的 `.env` 样例不能证明任意外部秘密文件均不可读。更关键的是，实际端到端探针用宿主可连通的 `127.0.0.1` 监听端口证实沙箱子进程也能连接；即使同时设置 `network.enabled=false` 与 `allow_local_binding=false` 仍如此。2026-09-25 的追加检查发现本机确有启用的 `codex_sandbox_offline_block_loopback_tcp` 规则，规则 SID 与实际 `CodexSandboxOffline` 子进程 SID 相同，不能简单归因于规则缺失。普通开发会话内的嵌套沙箱探针可能启动超时、`taskkill /T` 可能返回拒绝访问；改在宿主权限下运行相同合成测试后，空命令启动和进程树清理通过，但 loopback 连接依然成功。增加 CLI 的 `--sandbox-state-disable-network` 后，宿主侧 loopback 探针依然失败。这是原生边界的真实未通过证据，不能把嵌套超时当作断网成功，也不能拿宿主侧进程清理通过掩盖网络逃逸。

`native_runtime_ready()` 与 `native_fastpath_ready()` 均保持 `False`，AUTO 命令在宿主权限层和运行层双重失败关闭。仅带 `diagnostic_only=True` 的内部探针可调用候选后端，不是模型工具参数。诊断回执现在只写 `ISOLATION_CANDIDATE` 和 `network-requested=disabled`，不谎称网络已隔离；超时清理区分进程树清理命令返回成功与失败，且不把命令成功冒充为所有脱离后代的独立存活证明。审查不是 OS 隔离，不能把这些未通过的约束视为已满足。

P0 签收前须在可控合成夹具中实际验证：工作区写成功、工作区外写拒绝、敏感读拒绝（含任意外部秘密样式文件）、目录联接逃逸拒绝、默认联网拒绝、密钥环境变量不继承、超时/取消后没有存活后代；每项须记录后端标识、退出码和目标路径类别，不能记录密钥内容。任何一项失败或无法验证，脚本免审查开关继续关闭。P2 才增加终端、WSL、网络按主机授权和弱后端可用性；弱后端不得绕过 P0 的安全门槛。

当前的可复现门槛测试在 PowerShell 中为 `$env:MEWCODE_TEST_NATIVE_SANDBOX='1'; .\.venv\Scripts\python.exe -m pytest -q tests/test_native_command_boundary.py -k real_native`（需在允许启动 elevated Windows 沙箱的宿主环境运行）。2026-09-25 的结果为：后端启动与合成 `.env` 拒绝探针通过，loopback 拒绝探针失败；这项失败是**未通过证据**，不得跳过或改成成功断言来解锁 AUTO。普通单测不启用该环境变量，因此默认跳过真实宿主探针。

## 主机修复边界

只读核查确认：三个 Windows 防火墙配置档均启用并允许合并本地规则；`codex_sandbox_offline_block_loopback_tcp` 规则启用，其 SID 与真实子进程的 `CodexSandboxOffline` SID 相同。因而再次添加同形规则没有已证实的收益，且当前会话只有中等完整性、管理员组仅用于拒绝，不能自行修改防火墙。沙箱日志中的常规 `setup refresh` 显示 `errors=[]`，但这只说明本次写入根 ACL 刷新未报告错误，不证明 loopback 规则在本机生效。

官方 [Windows sandbox 故障排查](https://learn.chatgpt.com/docs/windows/windows-sandbox) 建议异常时重跑 elevated setup，并在仍异常时收集 `CODEX_HOME/.sandbox/sandbox.log`（**不得**收集 `.sandbox-secrets/`）。本次尝试调用 setup 的主机级路径被审批层拒绝，因为它需要启动长期运行的 app-server、影响范围尚未得到单独批准；不得通过其他命令或后台服务绕过该拒绝。待用户/管理员完成受支持的设置修复后，先在宿主权限下重跑全部 `real_native` 探针；只有网络、敏感读、写入范围、链接逃逸和进程树验收全部通过，才可单独讨论解除 `native_runtime_ready()` 的关闭状态。

## 备选后端：Anthropic srt 隔离试点（未启用）

`mewcode/sandbox/srt_windows.py` 已实现离线的逐次运行策略构造和显式可执行文件参数校验，**尚未接入 AUTO，也不代表后端验收通过**。策略必须声明工作区内的读目录和更窄的写目录；不得写整个工作区根；默认不联网、禁止弱隔离降级，并拒绝写 `.mewcode`、`.git`、`.env`。调用者用户资料目录与固定敏感路径列入 `denyRead`，再按声明路径开放工作区读取。Windows 上 `allowRead` 与广域 `denyRead` 的真实 ACL 效果必须由现场逃逸探针证明，不能只凭配置推断。每次调用应创建全新 srt 进程，避免其会话级读写授权跨任务复用。

[srt 官方 Windows alpha 文档](https://github.com/anthropics/sandbox-runtime#windows-alpha)说明：运行前需一次管理员级 `windows-install`，会创建专用账户、安装机器范围的 WFP 过滤器和目录 ACL；其 Windows 读写授权为会话级，不能把单条命令的配置覆盖当成独立授权。试点启动前，本机只确认 Node 可用、未发现 `srt`；授权后的实际安装与测试结果见下节。试点入口要求宿主明确给出 `node.exe`、包内 `dist/cli.js` 和本次设置文件的绝对路径；不通过模型控制的 `PATH` 或 `npx` 自动下载运行。

下一步须在**隔离的合成目录与明确获准的主机安装环境**运行：工作区指定输出写入成功；工作区外写入、用户资料任意秘密样式文件读取、`.mewcode` / `.git` 写入、目录联接逃逸、普通 loopback 端口和外网直连均拒绝；代理保留端口不得误判为普通 loopback 放行；超时与取消后没有存活后代。任何一项未通过，继续保持 AUTO 命令关闭，不自动回退 Codex 候选后端或宿主 Shell。

### 2026-09-25 本机合成验收结果：srt Windows 不满足 P0

在用户明确授权后，固定安装 `@anthropic-ai/sandbox-runtime@0.0.77`（npm 安装脚本关闭），执行官方 `windows-install`。专用 `srt-sandbox` 账户建立。普通权限的 `srt-win status` 报 WFP `cannot-read, 0 filters`，其帮助说明此处无法枚举是非管理员权限限制，**不是**零过滤器的证明。初次真实烟测在 WFP 行为核验前遇 `CreateProcessWithLogonW ... 0x80070005`；Secondary Logon 服务实际运行。该现象与[上游受保护安装目录问题 #396](https://github.com/anthropics/sandbox-runtime/issues/396)一致。仅对包内固定 `srt-win.exe` 给沙箱 SID 做临时读取/执行授权后，烟测通过；授权在同一操作的 `finally` 中撤销，事后 ACL 核查无该账户的残留 ACE。没有给 npm 包目录递归扩权。

`tests/test_srt_windows_escape.py` 使用合成工作区、合成用户资料、合成范围外目录和宿主可连通的临时 loopback 监听器。先前烟测 1/1 通过；真实边界复测 4 项中 **2 通过、2 失败**：

| 边界 | 现场结果 |
| --- | --- |
| 合成用户资料中的秘密样式文件 | 沙箱读取被拒，测试通过 |
| 非代理保留端口的 `127.0.0.1` | 宿主可连接，沙箱直连被拒，测试通过 |
| `allowWrite` 外、显式给 Authenticated Users `M` 权限的合成目录 | 沙箱成功创建范围外文件，测试失败；与[上游 #402](https://github.com/anthropics/sandbox-runtime/issues/402)同类 |
| 授权输出目录内指向上述范围外目录的 junction | 沙箱成功经联接写到范围外，测试失败 |

此外，授权输出目录内的文件虽由沙箱成功创建，原调用用户在 srt 会话结束后仍无法读取该文件；这是独立的产物交付问题。合成目录的临时 Authenticated Users 显式授权已由测试 `finally` 撤销；`srt-win.exe` 的临时 RX 授权也已撤销。安装本身（专用账户及 WFP 设置）仍在本机，未擅自卸载。未继续执行外网直连、超时/取消后的后代存活测试，因为两个写入逃逸已足以判定 P0 失败；这些项目不得标为通过。

结论：**srt 当前只保留为失败的隔离候选，不接入 AUTO，不解除 `native_runtime_ready()` / `native_fastpath_ready()`。** 不能靠审核模型、静态命令分类或列举少量 `denyWrite` 路径补足 Windows 上现有 ACL 对 Authenticated Users 的宽授权。下一后端必须在操作系统层证明“默认拒绝工作区外写入”，并重跑完整逃逸矩阵；原合同和用户签认门槛不变。
