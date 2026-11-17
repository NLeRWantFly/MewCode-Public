"""An executable two-subflow example, sharing the run's checkpoint scheduler."""


def nested_delivery(goal=""):
    def stage(identity, kind, label, purpose, steps, outputs, acceptance, **extra):
        return {"node_id": identity, "kind": kind, "label": label, "goal": purpose,
                "bind_later": True, "plan_brief": {"steps": steps, "outputs": outputs,
                "acceptance": acceptance}, **extra}

    return {
        "spec_id": "template.nested_delivery", "template_id": "nested_delivery",
        "label": "复杂交付 · 资料与制作子流程", "goal": goal or "形成可追溯的资料包和最终交付物",
        "require_confirm_gate": True, "finalizer_node_id": "delivery",
        "nodes": [
            stage("confirm", "gate", "确认范围", goal or "确认目标、来源范围和各节点模型", [], [], []),
            stage("sources", "plan", "资料子流程", "把大任务转成可核验的资料需求",
                  ["明确资料范围、允许读取的文件及事实清单"], ["资料需求与输入路径"], ["每个需求对应来源要求"]),
            stage("sources_collect", "implement", "提取资料", "保存可追溯的事实与引用",
                  ["读取授权来源，标注缺失与不确定项", "写入 process/sources.md"],
                  ["process/sources.md"], ["事实具有来源指针，不编造缺失项"]),
            stage("sources_normalize", "implement", "整理资料", "建立供后续制作读取的统一资料包",
                  ["消除重复并核对单位、时间与命名", "写入 process/evidence.json"],
                  ["process/evidence.json"], ["所有条目可追溯至原始来源"]),
            stage("sources_check", "audit", "资料验收", "确认资料包足以支撑制作",
                  ["只读抽查来源与结构", "记录缺口及修订要求"], ["资料验收结论"],
                  ["关键来源可核验，未解决缺口明确列出"], audit_target="sources_normalize", max_audit_retries=2),
            stage("sources_handoff", "reduce", "资料交付", "向制作子流程交接已核验的资料",
                  ["汇总文件指针和仍存在的限制"], ["资料交接清单"], ["不将待确认项表述为已确认事实"]),
            stage("production", "plan", "制作子流程", "用资料包制定具体交付结构",
                  ["读取资料交接，分配正文与配套素材的独立输出路径"], ["制作方案"], ["输出和验收与用户目标一致"]),
            stage("production_draft", "implement", "制作正文", "生成主体交付文件",
                  ["依据核验资料撰写正文", "写入 output/draft.md"], ["output/draft.md"], ["结论与来源一致"]),
            stage("production_assets", "implement", "制作配套素材", "生成正文需要的图表或配套文件",
                  ["根据资料生成所需素材，不覆盖正文", "用 ReportArtifact 登记命令生成的文件"],
                  ["output/assets/ 下的实际素材"], ["素材数据与资料包一致"]),
            stage("production_check", "audit", "交付验收", "联合检查正文与素材是否满足目标",
                  ["只读核对交付物与验收要求", "列明阻断项，不自行修改文件"], ["交付验收记录"],
                  ["产物齐全、来源准确、验收证据可读"], audit_target="production_draft", max_audit_retries=2),
            stage("delivery", "reduce", "最终交付", "交付成品、过程文件和未解决问题",
                  ["汇总两个子流程的已验证结果与文件链接"], ["最终交付清单"], ["明确区分已完成与待处理"]),
        ],
        "edges": [{"from": source, "to": target} for source, target in [
            ("confirm", "sources"), ("sources", "sources_collect"),
            ("sources_collect", "sources_normalize"), ("sources_normalize", "sources_check"),
            ("sources_check", "sources_handoff"), ("sources_handoff", "production"),
            ("production", "production_draft"), ("production", "production_assets"),
            ("production_draft", "production_check"), ("production_assets", "production_check"),
            ("production_check", "delivery"),
        ]],
        "meta": {"canvas_groups": {
            "sources": {"mode": "executable", "expanded": True,
                        "children": ["sources_collect", "sources_normalize", "sources_check", "sources_handoff"]},
            "production": {"mode": "executable", "expanded": True,
                           "children": ["production_draft", "production_assets", "production_check"]},
        }},
    }
