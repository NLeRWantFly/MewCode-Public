# 字段与转换核对

## 最小结构

OrchestrationSpec 使用 spec_id、label、goal、nodes、edges、finalizer_node_id、meta。节点使用 node_id、kind、label、goal、model、bind_later、plan_brief；边使用 from_node、to_node、condition。稳定 ID 与显示名称分开。模型绑定以当前可用配置为准，禁止照抄示例中的虚构模型。

下面是未绑定模型的合法草稿，用于表达“实现 → 检查 → 有界返修 / 汇总”，不是执行授权：

```json
{
  "spec_id": "bounded-review",
  "label": "实现与证据检查",
  "goal": "交付通过已确认测试的改动",
  "finalizer_node_id": "done",
  "nodes": [
    {"node_id": "work", "kind": "implement", "label": "实现", "goal": "按已确认范围完成改动与测试", "bind_later": true,
     "plan_brief": {"summary": "完成候选改动", "known_inputs": ["用户确认的变更范围和验收命令"], "open_questions": [], "steps": ["修改范围内文件", "运行验收命令"], "outputs": ["候选改动与测试日志"], "acceptance": ["指定测试全部通过"]}},
    {"node_id": "check", "kind": "decision", "label": "检查", "goal": "核验测试证据并提交循环判定", "bind_later": true},
    {"node_id": "done", "kind": "reduce", "label": "汇总", "goal": "交付改动说明及验证证据", "bind_later": true}
  ],
  "edges": [
    {"from_node": "work", "to_node": "check", "condition": "always"},
    {"from_node": "check", "to_node": "work", "condition": "loop"},
    {"from_node": "check", "to_node": "done", "condition": "pass"}
  ],
  "meta": {"loops": [{
    "loop_id": "verify", "entry_node_id": "work", "check_node_id": "check",
    "node_ids": ["work", "check"], "reviewer_node_ids": ["check"],
    "exit_condition": "指定测试全部通过且有可定位日志",
    "criteria": [{"criterion_id": "tests", "acceptance_test": "用户确认的验收命令通过，日志含命令、退出码和结果"}],
    "max_iterations": 3, "token_budget": 300000, "timeout_seconds": 900, "max_stalled_rounds": 2
  }]}
}
```

## 循环与审计约束

循环范围内的前向路径必须从入口可达检查点；检查点必须有 pass / always 退出边。嵌套循环不可交叉，入口和检查点须独立。预算范围：max_iterations 1–20、token_budget 1000–5000000、timeout_seconds 1–7200、max_stalled_rounds 1–5；node_max_iterations 与 reviewer_max_iterations 均 4–200，默认 40 / 12，默认值省略序列化以兼容旧合同哈希。应按任务设置，不把示例值当全局标准。

节点可选 tools / deny_tools 为工具名称数组；tools 缺省沿用角色，[] 表示无可选工具。meta.parallel 缺省 false。新增字段在 spec、flow 与画布往返时保留；kind 扩展仅来自可信角色注册表。

审查节点可声明 review_channels（text、pixel、command 字符串数组，始终包含 text）；缺省按步骤和验收推断，需要像素证据时由 L12 检查节点视觉能力或已配置的看图能力。同一审查对象只设一个 audit 节点，L13 拒绝仅按视觉和文本拆分审查；通道在 spec、flow 与画布往返时保留。

运行时判定须使用当前 contract_hash，逐条返回 criterion_id、state（PASS / PARTIAL / FAIL / UNKNOWN）、evidence、location。全 PASS 对应 EXIT；team 下普通缺证据 UNKNOWN 可 CONTINUE 交执行成员补齐，确需用户选择/材料/权限才 BLOCKED；strict 下 UNKNOWN 对应 BLOCKED。循环预算不应与节点自动重试叠乘。

需要业务返修的循环声明 `requires_repair: true`、`repair_node_ids: ["work"]`。这些 ID 必须是循环成员、有写入能力的 implement，且不能兼任 reviewer。旧合同省略新字段时哈希不变；不把旧运行静默升级成新合同。仅有审查者而声称能自行返修的声明会在编译时拒绝。

普通 audit 通过 audit_target 与 max_audit_retries 表达有限回查，不必同时另建通用循环。独立一次审计与循环决策职责不同；审计员不能直接改动被验收成果或降低已确认标准。

## 源码定位与本地验证

- mewcode/orchestration/spec.py、validate.py：字段、绑定检查、引用和无环校验。
- mewcode/orchestration/convert.py：plan → planner、implement → executor、reduce / gate → manager、audit / decision → auditor；门禁实际转换、有限回查、子阶段折叠。
- mewcode/orchestration/loops.py、mewcode/experts/loop_runtime.py：循环契约、证据判定与看门狗。
- mewcode/orchestration/pipeline_bridge.py、plan_session.py：画布往返与当前计划；mewcode/expert_checkpoints.py：运行进度。
- mewcode/expert_group.py、mewcode/expert_web.py：调度和实际工具边界；角色名称本身不授予权限。
- mewcode/experts/attempt_memory.py、candidate_verification.py：重试证据摘要与独立候选验证；VerifyCandidate 是候选执行者的命令工具，参数为 runner（pytest / unittest / npm_test）、targets（候选相对路径数组，最多 20 项）、timeout（1–600 秒，含依赖准备）。缺省 timeout=120；pytest 允许路径后的 :: 测试选择器，不接收任意 shell 命令。

代码环境中用 OrchestrationSpec.from_value(raw)、validate_spec(spec) 验证草稿，并用 to_expert_flow(spec) 检查编译；确认前再 validate_spec(spec, require_bindings=True)。这些校验不是启动运行。画布往返用 to_pipeline_graph / from_pipeline_graph 检查 ID、依赖和循环契约未丢失。
