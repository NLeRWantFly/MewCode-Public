# MewCode Skill 安装

MewCode 的 `FindSkills` 只检索本地目录，`LoadSkill` 只加载已存在的 Skill。Codex 宿主提供的 skill-installer 不会自动成为 MewCode 的工具；旧版本缺少安装入口时，模型可能自行选择运行 `npx skills add`。

现在主会话内置 `skill-installer`，对应 `InstallSkill(repo, path, ref="main")`。它下载公开 GitHub 仓库归档，只安装选定目录到当前项目 `.mewcode/skills/<name>`，不调用 npx/npm/PowerShell、不改变执行策略。安装是有网络和文件副作用的操作，仍经过正常工具权限；受限专家节点不获得安装能力。

成功后立即刷新 FindSkills / LoadSkill 的目录。资源、脚本原样保存，来源与文件 SHA256 写入 `.mewcode-install.json`；安装和加载均不会自动运行下载的脚本或注册其 Python 工具。已有名称（包括标准 `.agents/skills` 和内置技能）不覆盖。归档/解压大小、文件数、跨平台路径、重复路径和符号链接均检查，临时目录不进入技能目录，并发安装用独占锁保护。失败遗留锁需先核实原安装进程状态，不自动删除他人的锁。

目前仅支持公开 GitHub 仓库及精确目录，不支持私有仓库凭据、全局安装、自动更新或依赖安装。不要把安装 Skill 和执行其脚本视为同一次授权。

Windows 若原始错误明确指出 npx.ps1 / PSSecurityException / 禁止运行脚本，才可判定为 PowerShell 执行策略问题；没有错误原文时不能断言原因。新的 Skill 安装路径完全不经过该命令链；通用 npx 命令及系统执行策略保持不变。

重启更新后的 MewCode 后端以注册新工具。普通会话可直接请求“安装 owner/repo 中 path 目录的 Skill”；已运行的受限节点需回到主会话处理。
