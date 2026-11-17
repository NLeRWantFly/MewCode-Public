---
name: orchestration-diagrams
description: 设计、保存、编辑和复用 MewCode 原生工作流；使用执行、判断、分支、循环组件与自由画布。
---

# MewCode 工作流

读取 [原生工作流](references/native-workflow.md)，使设计、画布和 Core 执行语义一致。

先读取已有节点与来源，保持仍有效的 ID、职责、产物和精确模型绑定。根据任务使用执行、判断、分支、循环四类组件，按真实依赖组织并行和串行；不自动添加验收门禁、最终模型检查或返修循环。

使用 ReadWorkflowBlocks、SubmitWorkflowBlocks、AdvanceTaskDesign 保存当前规格。构图直接编译已明确的节点，不额外抽取或审查整段历史。正常节点交付推进下游；真实错误留给节点恢复，保存已有成果。图结构与对话摘要可提取为下一个任务的构造标准。

仅保存编排不代表允许执行业务。原有工具权限与用户限制继续生效。旧版 plan/implement/audit、回边和审核协议只用于历史资料兼容，不用于设计新任务。
