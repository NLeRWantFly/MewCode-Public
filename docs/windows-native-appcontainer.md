# Windows 原生命令边界候选：AppContainer / LPAC

状态：生产 AUTO 仍未启用；新增宿主 stdin 适配器与局部实机结果见 [本轮接入回执](auto-native-integration-20260925.md)。Git 兼容和完整验收尚未通过。保持 PowerShell/EXE 原生兼容目标；不使用 Docker、WSL，也不将当前未合格的 Codex elevated 当作默认后备。

最新进展（2026-09-25）：上一批固定 COM 运行时依赖的显式对照已解决系统 PowerShell CLI 与 .NET 线程池退出卡住，报告为 `.tmp/mewcode-lpac-probe-medm9m81/report.json`（不需要 DOS 盘符映射）及 `.tmp/mewcode-lpac-probe-jw_bjoie/report.json`。默认 hosted runspace 的驱动初始化仍有限制，不能扩大为任意 PowerShell 脚本兼容。下文失败记录保留为诊断历史；生产状态仍未启用。

本地收尾批次依用户要求不开展网安相关测试，只验证项目工具链、取消、调度和界面。新本地兼容测试 `tests/test_local_toolchain_integration.py` 不运行原生探针、不创建监听器、不调用模型、不安装依赖；在独立测试目录建立无 pip 虚拟环境与空 Git 仓库，验证 Windows 路径、PowerShell 调用及校验进程取消。它不是生产 AUTO 验收。

## 为什么另做原生候选

当前 Codex elevated 的宿主 nonce 与同 ACL 文件对照均出现真实逃逸，参见 [现场记录](execution-boundary-windows.md)。不能通过模型审核、更大预算或文本规则补足 OS 边界。沿用 Codex 的审批/执行分离，但尝试 Windows 自身提供的另一种内核身份边界。

微软文档描述 AppContainer 使用用户身份与包身份的权限交集；LPAC 进一步去除普通 AppContainer 的环境权限。没有网络 capability 时应不可访问网络。这些仅是候选设计依据，仍以本机行为验证为准：[启动 AppContainer](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer)、[隔离范围](https://learn.microsoft.com/en-us/windows/win32/secauthz/appcontainer-isolation)。LPAC 缺少某些系统/注册表能力可能阻止 PowerShell 启动，不能为跑通而自动改成宿主或更宽权限。

## 本批试验边界

- 启动器仅接受一次全新、带合成标识的测试目录；标识是防误操作检查，**不是可信授权凭证**。
- 每次创建随机专属 AppContainer profile，不复用其他软件的沙箱账户/身份。不配置机器级防火墙，不添加联网能力。
- 只向本次合成目录内指定子路径临时添加本次专属 SID 的 ACE，结束只撤销自己的 ACE；不对系统目录、真实工作区或用户资料扩权。
- 子进程挂起创建，先加入禁止脱离且关闭即终止的 Job Object，再恢复执行。根进程结束不能被视作后代全部结束；Job 清理结果须单独记录。
- 宿主启动器输出结构化回执，子进程 stdout/stderr 只是独立字段。外层监督超时无法证明 profile/ACL 已清理时记录未知，不伪造通过。
- Python 构造器与 C# 启动器分别检查参数、目标和 reparse 路径；没有生产开关、自动安装、自动批准或主机执行回退。

## 验证顺序

1. 原生 `cmd.exe` 与系统 Windows PowerShell 的随机标记、合法工作区读写、原用户读取产物。
2. 外部合成目录给普通用户相同读写权限，验证包身份仍拒绝普通文件读取、秘密样式文件读取、越界写入；网络用宿主可达监听器及随机 nonce。
3. 启动兼容性和基础边界均可用后，再验证联接/符号链接、硬链接、进程派生、超时取消、IPv6/UDP/外网、环境变量、权限权威文件以及运行后新出现的敏感文件。
4. 在真实工具链的脱敏副本上验证 Python/EXE、依赖加载、产物提交与并发任务范围隔离。只通过合成烟测不能启用生产 AUTO。

LPAC 和普通 AppContainer 是两份独立候选。后者只能通过显式诊断参数运行对照，不能在前者失败时自动降级。最终后端选择、授权范围和审阅签认仍由宿主与用户决定。

## 复现入口

`native/windows/MewCode.AppContainerProbe.cs` 用本机 .NET Framework 编译器构建，无新增 NuGet 依赖。`scripts/probe_windows_appcontainer.py --helper <固定绝对路径> --execute-synthetic-probes` 只执行合成试验，在 `.tmp/mewcode-lpac-probe-*` 留存请求与报告。`--regular-appcontainer` 是显式对照候选。返回失败或有未验证项目时，`native_runtime_ready()` / `native_fastpath_ready()` 保持关闭。

## 2026-09-25 原生对照结果

普通 AppContainer 报告：`.tmp/mewcode-lpac-probe-_6kae8uv/report.json`。这不是生产验收回执。

| 项目 | 实测结果 |
| --- | --- |
| CMD 与 Windows PowerShell 启动 | 成功；修复每个 CMD 参数都强制引用导致的 `echo` 失败，PowerShell 使用 UTF-16LE 编码参数保存中文路径 |
| PowerShell 语言模式 | `ConstrainedLanguage`；cmdlet 读写通过，不代表任意 .NET 脚本兼容 |
| 独立 .NET EXE | 成功执行；输入读取、产物写入和宿主回读通过 |
| 外部普通文件 / 合成 `.env` / 越界写 | 相同普通用户 ACL 下均被拒绝；不把结果扩大为所有秘密路径已覆盖 |
| 环境 | 合成密钥变量未继承；配置了合成 USERPROFILE/APPDATA/LOCALAPPDATA，解决进程创建错误 203 |
| 根进程退出与超时 | Job 清理成功，宿主确认已报告的后代 PID 不再存活 |
| 临时资源 | 本次专属 SID ACL、profile 清理均成功 |
| 回环网络 | 宿主阳性对照可达；子进程连接超时、无数据到达。记为 `NETWORK_UNKNOWN_TIMEOUT`，不是已证实拒绝 |

启动器与 Python 请求构造器分别拒绝合成 marker、授权文件、夹具 EXE 的多硬链接；系统自带 EXE 的合法 WinSxS 硬链接不因此误拒。路径检查不是 TOCTOU 防护：生产前还须解决路径替换竞态、用户取消、依赖和权限权威文件保护、联接、IPv6/UDP/外网，以及新出现敏感文件的屏蔽。夹具和报告保留，没有删除用户资料。

LPAC 独立测试 `.tmp/mewcode-lpac-probe-gi6dgack/report.json` 发现本机 `TokenIsLessPrivilegedAppContainer` 查询返回错误 87；启动器在挂起状态拒绝恢复，并清理子进程。不能把不支持该查询解释为 LPAC 不存在，也不能直接跳过身份核验。后续采用 Chromium 官方 `CheckLpacToken` 的内核 `AccessCheck` 对照来兼容该查询缺失，仍须先验证实际令牌：[Chromium 测试源码](https://chromium.googlesource.com/chromium/src/+/4fc987ed69c8333f860e2ab455bbdd731cdf682c/sandbox/win/src/app_container_test.cc)。

已补此兼容核验，`.tmp/mewcode-lpac-probe-cgw16t8e/report.json` 实测 LPAC 的权限掩码为 2、CMD 成功执行。只有查询返回 87/50 才采用行为核验，其余查询失败仍拒绝；普通候选必须得到掩码 3，任何不匹配均不恢复子进程。新回执同时保留受上限约束的 stdout/stderr 原始字节，避免 Windows ANSI/OEM/UTF-16 错误文本被 UTF-8 显示解码破坏。

该零能力 LPAC 测试里 PowerShell 不能读取 `SOFTWARE\\Microsoft\\PowerShell`，独立 .NET EXE 初始化也失败。因此新增**显式合成对照** `--runtime-registry-read`：仅 LPAC 可声明固定 `registryRead` 能力，默认关闭，普通候选或非布尔请求拒绝；不得传任意能力名，不加入网络、COM 能力，不修改注册表/系统 ACL。微软说明 LPAC 的注册表访问需要该能力：[官方说明](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer)。它是否满足本项目运行时与安全要求仍须现场验证，不能将能力名称当成全局安全证明。

网络探针同时运行同一合成 EXE 的宿主阳性对照（固定 `network-control` 模式，不操作文件）。只有同一网络代码从宿主成功发送且监听器确认随机标记、沙箱代码执行了预期所有读写检查、明确网络连接超时且无沙箱数据送达时，才能记录“有界测试未送达”；与显式 AccessDenied 分开记录。其他故障、缺少阳性对照、非预期退出码或任意沙箱数据到达均不能通过。这仍不是全部协议/目标的网络验收。

`registryRead` 实测报告 `.tmp/mewcode-lpac-probe-rcqt9s3a/report.json`：独立 .NET EXE 的合法读写与回读通过，外部普通文件/合成 `.env`/junction 读取和外部写入均拒绝；联网返回明确 `SocketError.AccessDenied`，同 EXE 宿主阳性对照成功；根退出/超时后代清理以及 ACL/profile 清理通过。Windows PowerShell 仍因 `PSEtwLog` 初始化异常而失败，因此本次总烟测仍为失败，不启用 AUTO。

下一兼容对照仅显式增加固定 `lpacInstrumentation` 系统能力，前提为 LPAC + registryRead；不禁用 ETW/AMSI，不关闭日志，不接受任意能力名或扩为网络/COM 能力。该能力名称由 [Chromium 系统能力定义](https://chromium.googlesource.com/chromium/src/+/refs/tags/132.0.6832.1/sandbox/policy/win/lpac_capability.h)核实；它能否解决此 PowerShell 初始化问题是待验证假设，不是来源保证。

### 本轮最终结果与剩余工作

- 最终普通 AppContainer：`.tmp/mewcode-lpac-probe-8w7mg8j7/report.json`，身份行为掩码为 3，声明能力为空。CMD、Windows PowerShell cmdlet、独立 EXE、合成外部与 junction 读取拒绝、外部写拒绝、宿主回读、根退出/超时后代清理均通过。联网属于同 EXE 阳性对照支持的有界未送达，不是显式 AccessDenied。整体 `smoke_passed=true`，但 `production_authorized=false`。
- LPAC + 两项固定运行时能力：`.tmp/mewcode-lpac-probe-ltoi7pdl/report.json` 的 PowerShell 在 10 秒截止内未初始化；完全相同权限、仅延长观测的 `.tmp/mewcode-lpac-probe-i6u4qft9/report.json` 在 45 秒后仍未初始化。EXE、明确拒绝联网、读写和清理通过，整个 PowerShell 兼容烟测仍失败。没有加入更多能力去碰运气，也没有关闭日志、安全接口或改机器策略。
- 回执中的 `lpac_verified` 表示**请求的 LPAC 状态匹配已核验**，不是孤立断言“该进程是 LPAC”；需同时读取顶层 `lpac` 与 `lpac_access_mask`（LPAC 为 2、普通候选为 3）。
- 下一工程步骤是定位 Windows PowerShell 的原生初始化等待链、验证实际依赖最小集，而不是无限延长预算；随后补取消清理、junction 写逃逸、IPv6/UDP/外网、权限权威文件、新出现敏感文件、路径替换竞态与真实工具链隔离验收。普通 AppContainer 自身的环境访问也必须单独核验，不能自动顶替严格候选。
- 本轮定向回归：Python 策略/报告/原生门禁和权限集成 `217 passed, 10 skipped`；最新 C# 启动器的拒绝请求验证 `15 passed`，每条在创建 profile/子进程前拒绝。跳过项不是通过证据。总计 232 通过、10 跳过，非全库验收。

本批没有接入生产 AUTO，没有更改现有运行的合同、验收范围或阶段签认，也未提交/推送工作区里其他未完成改动。临时 profile 和本次添加的 SID ACL 均按报告清理；合成夹具、EXE 与报告留在 `.tmp` 供复查。

### 后续定位：等待链与 PowerShell 驱动初始化（2026-09-25）

以下实验权限均仍为 LPAC + registryRead/lpacInstrumentation，没有联网/COM 新能力，没有修改真实文件、系统 ACL 或安全配置。

- 合成 profile Local/Roaming 访问对照：`.tmp/mewcode-lpac-probe-sbxl133p/report.json`，PowerShell 25 秒仍超时，不能将配置目录权限视为已证实根因。
- 只读等待链：新增 `MewCode.WaitChainProbe.cs`，只查询显式 PID，最多 8 线程，每线程 2 秒、总预算 18.5 秒，有独立 watchdog。异步缓冲区与回调保留至关闭完成或诊断进程退出；不读取目标内存、不结束目标进程。`.tmp/mewcode-lpac-probe-7r9ygz3h/report.json` 中 7 线程查询 467 毫秒完成、未报告循环死锁、未定位具体等待对象，不能解释为目标没有阻塞。外层 Python 取消/超时也收束自己的 observer。
- 固定运行空间探针：`.tmp/mewcode-lpac-probe-qx6oanj5/report.json` 中 Windows PowerShell 程序集、CreateRunspace、Open 均完成。随后实际管道触发 `DriveNotFoundException`：名为 `Microsoft.PowerShell.Core\\FileSystem` 的驱动器不存在（`.tmp/mewcode-lpac-probe-p9fjms58/report.json`）。空运行空间能打开不是完整兼容证明。
- `-Mta` 对照 `.tmp/mewcode-lpac-probe-scl3q8wm/report.json` 仍超时，不改变生产调用方式。
- 会话内显式建立逻辑 PSDrive 不修改 OS 挂载或访问权。`.tmp/mewcode-lpac-probe-biqvx2c2/report.json` 中固定 PowerShell 文字管道可执行，但原生子进程标记缺失且进程未正常结束，因此此替代诊断仍失败。相同 EXE 的宿主 `powershell-init` 阳性对照成功输出引擎/原生子进程标记并退出 0。禁止把固定管道输出当成整个边界通过。

复现可选参数：`--waitchain-helper <绝对路径>`、`--writable-runtime-profile`、`--diagnose-powershell-init`、`--powershell-mta`。均只服务合成诊断；初始化诊断不接收用户脚本，其成功也不会替代原有 PowerShell 边界烟测或开启 `production_authorized`。两个 AUTO 生产门禁仍为 false；完整兼容、取消/竞态及剩余逃逸矩阵未验收。

本次合并回归：17 个定向 Python 测试文件共 **339 passed / 5 skipped**（含显式设置 `MEWCODE_APPCONTAINER_PROBE_EXE` 的拒绝请求验证）；随后补审批旧等待分位数排序回归，统计/集成子集 **21 passed**。前端格式化 **3 passed**，`node --check frontend/static/app.js` 与 `git diff --check` 通过。测试使用 `.tmp` 下独立 `MEWCODE_USER_DATA_DIR`，不写现场运行台账。上述不是端到端生产验收，跳过项和原生实测失败仍保留。

### 分阶段执行：原生子进程与清理阶段对照（2026-09-25）

新增固定诊断标记，不改变 LPAC 能力、ACL 范围或生产门禁。主 Agent 实测报告：`.tmp/mewcode-lpac-probe-dfkay55c/report.json`，使用既有 `registryRead` / `lpacInstrumentation` 两项能力；诊断仍为 20 秒截止。

- 直接由合成 EXE 启动系统 `cmd.exe`：退出 0，固定 stdout 标记存在、stderr 为空。可排除“该令牌完全不能创建原生子进程”这一过宽假设。
- 不补逻辑驱动的运行空间：Invoke 抛错，PipelineState 为 Failed，失败类型为 DriveNotFoundException；pipeline 与 runspace 的 Dispose 前后标记均出现，整个进程仍超时。
- 仅补运行空间内逻辑驱动的对照：Invoke 返回，PipelineState 为 Completed、Reason 为空，文字标记存在，但 PowerShell 原生子进程标记缺失；两处 Dispose 均返回，整个进程仍超时。下一步应调查 PowerShell 原生命令路径及运行空间释放后的存活线程/退出阶段，不再假定 Dispose 本身卡住。
- 相同新编译合成 EXE 的宿主阳性对照：原引擎/原生子进程标记、Dispose 完成与 PS_INIT_DONE 全部出现，正常退出 0。诊断标记不替代实际 PowerShell CLI 与脚本工具链验收。
- 此报告 `outside_write_host_observation=false` 是验收证明缺失，并非观测到越界写入：合成 `outside/escaped.txt` 实际不存在，PowerShell 未完成拒绝探针。报告计算器已新增 `outside_write_observation` 枚举区分“确有写入 / 拒绝核验通过 / 未见写入但探针未完成 / 观测不可用”，原通过条件不变。
- 所有 case 的 Job、专属 SID ACL 和 profile 清理均为成功；EXE 读写隔离与明确网络拒绝仍通过。PowerShell 兼容失败、取消/竞态等完整矩阵缺失，`production_authorized=false`，两个 AUTO 门禁继续关闭。

复现：项目虚拟环境运行 `scripts/probe_windows_appcontainer.py --helper <已编译 helper 绝对路径> --execute-synthetic-probes --runtime-registry-read --runtime-instrumentation --diagnose-powershell-init`。它只运行固定合成工作负载，不接收业务脚本。

### Sol 执行批次：环境、驱动器、取消三个独立对照（2026-09-25）

按最新授权使用 `gpt-6-sol / max` 执行限定子任务，主 Agent 合并并实测。未更改隔离能力、系统 ACL、安全接口或生产开关。

- `scripts/diagnose_powershell_runtime.py` 使用历史十键环境与只增加固定 `PATHEXT=.COM;.EXE;.BAT;.CMD` 的环境运行同一宿主合成 EXE。前者原生标记缺失、退出 2；后者原生标记与 PS_INIT_DONE 均出现、退出 0。修复只补固定扩展名，不继承任意宿主环境。此前普通交互 Shell 的 PATH/PATHEXT 对照不能替代这一精确环境对照。
- `.tmp/mewcode-lpac-probe-8gh8fiym/report.json`：受限手工 PSDrive 管道已有原生子命令 BEFORE/OK/AFTER、LASTEXITCODE_0、PS_INIT_DONE；无管道错误，两个 Dispose 均返回，但整个进程仍达到 20 秒截止。系统 PowerShell CLI 仍无输出超时。由此将“原生命令遗漏”与“进程退出卡住”明确拆开。
- `.tmp/mewcode-lpac-probe-t_iekpir/report.json`：以空闲本地 DOS 盘符临时指向**本次合成 workspace**，没有增授 ACL；默认 runspace 也能完成管道和原生子命令，支持卷根访问导致默认 PSDrive 缺失这一诊断。映射始终按完整目标精确删除，清理成功。系统 CLI/进程退出问题仍存在，不能把逻辑盘映射当作完整修复。上游有同类初始化检查问题：[PowerShell 官方仓库报告](https://github.com/PowerShell/PowerShell/issues/27253)。
- 两次报告的 `cancellation_descendants` 均通过：宿主创建随机 `MewCodeProbeCancel_` 事件，helper 只打开现有事件；收到信号后终止 Job，根和后代退出，专属 ACL/profile 清理成功。取消与超时分开记录。报告计算器要求实际信号、后代退出及所有清理证据，未知或不完整仍不通过。
- 新增第一机会异常观测仅保留最多 24 种去重类型名，错误类型链限深 8；不记录异常消息、变量、环境值。此观测不是安全判定，下一步只读线程栈定位剩余退出等待，不能强制成功退出掩盖故障。

新增显式诊断选项：`--diagnose-cancellation`、`--alias-workspace-drive`。DOS 别名仅为合成对照，不接入生产。所有原生网络、权限权威文件、后创建敏感文件、路径替换竞态与真实工具链验收仍保留；AUTO 两个生产门禁继续为 false。
