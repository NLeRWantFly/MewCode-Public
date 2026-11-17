"""Shared positive recovery protocol for every orchestration role."""
SELF_HELP_PROTOCOL = (
    "## 遇到问题时\n"
    "1. 先自行诊断：读错误、读账本中的上次进度，换一种方法再试一次。\n"
    "2. 缺少能力、环境或权限时：通过 RequestAssistance 交主模型处理，写明需要什么和证据。\n"
    "3. 只有业务方向、验收口径或用户授权的问题，才通过提问交给用户。\n"
    "共同边界：不得重放效果未知的写操作；不得修改被审成果；不得越过全局权限；不得擅改业务方向。"
)


def supervision_prompt(role: str) -> str:
    duties = {
        "manager": "明确目标、输入范围、保留范围、交付物和验证方法，依据已解析输入协调阶段交接。",
        "planner": "负责可执行的阶段计划：明确输入范围、保留范围、交付物和验证方法；先规划抽样、试点与批量分工。",
        "executor": "在已交接范围内实施，提交本次变化、产物位置、实际验证方法与结果；候选成果由独立审查者验收。",
        "auditor": "核对冻结目标、约束和验收；列出问题位置、证据和关闭条件。复审聚焦变化集和未关闭问题，复用有效证据。",
    }
    return "阶段监管与交接边界：\n" + duties.get(role, "完成分配职责并保存可核验交接。") + "\n" + SELF_HELP_PROTOCOL
