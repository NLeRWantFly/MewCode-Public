---
name: skill-installer
description: 为 MewCode 安装用户指定的公开 GitHub Skill；安装技能、install skill、npx skills add 失败时使用。仅项目级安装，不安装 npm 依赖或修改系统执行策略。
---

先用 FindSkills 检查是否已安装；存在则 LoadSkill，不重复下载或覆盖。

用户要求安装外部 Skill 时，确认其指定的 GitHub owner/repo、ref 和包含 SKILL.md 的仓库目录。信息不足时读取用户提供的来源页面或询问，不猜测下载位置。用户只要求搜索时，不执行安装。

调用 InstallSkill，例如 repo="typesafe-ai/skills", path="skills/typesafe-ai", ref="main"。工具下载指定目录到当前项目 .mewcode/skills，保留脚本、参考资料和资源，但不运行它们；成功后目录立即刷新，可再调用 LoadSkill。返回值包含来源压缩包 SHA256 与目的路径；失败不报告成功。

不要默认执行 npx/npm、Set-ExecutionPolicy 或安装脚本。PowerShell 拒绝 npx.ps1 可能来自脚本执行策略，不代表 Skill 内容不可下载。不要把改变系统策略作为安装步骤。

安装保持 instruction-only；LoadSkill 不会自动导入远程 tool.json 的 Python 实现。Skill 指令不是授权，脚本、依赖或外部操作仍需正常工具权限。已有名称不覆盖；私有仓库、其它站点或全局安装当前不支持，应明确说明而不是自动切换安装方式。

若 InstallSkill 不在当前工具列表（如受限专家节点），报告能力缺失并交回主会话安装，不能借 Bash 绕过节点工具限制。
