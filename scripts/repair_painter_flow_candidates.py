"""Build offline, source-linked repair candidates; NEVER mutate a live project/run.

Run from the repository root with --project and a NEW --output directory.
The paper document deliberately remains non-executable until its paragraph
queue, current baseline and model bindings have been reviewed.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

from mewcode.orchestration import from_pipeline_graph, to_pipeline_graph, to_expert_flow, validate_spec
from mewcode.orchestration.draft_sources import public_text
from mewcode.orchestration.plan_integrity import require_resolved_plan_inputs
from mewcode.orchestration.spec import OrchestrationSpec
from mewcode.pipeline.models import PipelineGraph

DRAWING = "session_20260910_181625_tore"
PAPER = "session_20260910_183532_aglb"
PASS_EDGES = {("quality_audit", "figure_review_gate"), ("figure_review_gate", "material_extract"),
              ("material_audit", "experience_draft"), ("experience_audit", "migration_validation")}


def repair_drawing(raw):
    result = copy.deepcopy(raw)
    nodes = {n["node_id"]: n for n in result["nodes"]}
    replacements = {
        "topic_plan": [
            "用户指定 Topic：RGB-T 多模态目标检测，重点学习流程图与模块图；CCF-A/CCF-B 期刊或会议。",
            "用户允许纳入近期优秀、高引用的 CV 工作作为扩展参考；具体主辅层配额交 G1 冻结，不自行放宽 CCF 门槛。",
            "用户选择经验库加小型原创迁移验证；使用通用组件/模板，不要求真实实验数据。",
            "修复候选口径：CCF-A 优先、不足补 B，运行时依据官方目录逐项核验并记录版本和来源；时间窗 2023-01-01 至本次检索截止日（不晚于执行日）。",
            "接受 arXiv 图例版本不等于放宽发表门槛；正式样本须核验已在 CCF-A/B venue 发表或正式接收。",
        ],
        "scope_gate": [
            "G1 集中确认主辅层配额（建议合计 15–20 篇且不重复计数）、展示用途、成品宽高、检查缩放比例和可读性标准。未确认前不得当作冻结值。",
            "G1 由运行器保存用户确认记录；tool_preflight 负责将确认记录及验收快照归档到 01-topic/。",
        ],
        "tool_preflight": [
            "用户授权在当前项目内分类建子目录，使用 draw.io，自助检查并补齐工具能力；系统级安装或额外目录权限仍需单独授权。",
            "必须实测 .drawio 保存、重开、SVG/PNG 导出及视觉查看；历史能力声明不代替本次预检证据。",
        ],
        "figure_review_gate": ["按修复候选展示口径：首批 8–10 篇可每次展示 3–4 篇，逐篇给出名称、工作、创新和图例学习价值，由用户选择核心参照。"],
        "material_extract": [nodes["material_extract"]["goal"]],
        "migration_validation": ["仅用通用组件/模板图做小型原创迁移验证，不索要真实实验数据，不编造性能结果；没有数值需求不生成示例数据。"],
        "experience_enable": ["用户指定 draw.io；经验库应版本化保存候选 pending 与启用 active，只有用户确认且实际验证对应版本的经验可启用；不得自行写入长期记忆。"],
    }
    changes = []
    for id, values in replacements.items():
        brief = nodes[id]["plan_brief"]
        old = list(brief.get("known_inputs", []))
        retained = [s for s in old if not s.startswith(("既有阶段 ", "最新修复方案"))]
        brief["known_inputs"] = list(dict.fromkeys(retained + values))
        changes.append({"node_id": id, "field": "plan_brief.known_inputs", "before": old, "after": brief["known_inputs"]})
    changed_edges = set()
    for edge in result["edges"]:
        pair = (edge["from_node"], edge["to_node"])
        if pair in PASS_EDGES:
            changes.append({"edge": list(pair), "field": "condition", "before": edge["condition"], "after": "pass"})
            edge["condition"] = "pass"
            changed_edges.add(pair)
    if changed_edges != PASS_EDGES:
        raise ValueError("Drawing graph topology changed; review the new baseline first")
    result["version"] = raw["version"] + 1
    result["require_confirm_gate"] = True
    return result, changes


def paper_recovery_document(source_refs, original):
    # Definitions, not a fabricated runnable queue. No historical completion is
    # copied, and no active provider key is inferred from a model nickname.
    stages = [
        ("prepare_latex", "建立 LaTeX 工作副本", "读取原始 LaTeX 与实验材料，保留原稿，建立工作副本和可逆备份；读取适用写作 skill。"),
        ("segment_visualize", "分段并可视化", "区分正文、列表、公式、图表和伪代码，以可视化结构向用户展示分段。"),
        ("segment_gate", "确认分段", "由用户确认分段与改写范围；不能从历史进度直接推断确认。"),
        ("reference_search", "检索 20 篇候选", "依据方向契合、CCF-A/B、引用影响力检索并核验来源；分批展示名称、主要工作和创新点。"),
        ("reference_gate", "选择核心参照", "由用户从候选中选择约 10 篇，收集对应章节/段落的全文级学习材料。"),
        ("analyze", "分析本文段落", "对当前段及其上下文分析句法、段落功能、伪代码、宏、上下标和排版锚点。"),
        ("learn", "学习参照段落", "学习核心参照在对应章节和同类段落中的表达结构，不仅复述技术结论。"),
        ("decide", "综合差异并决策", "同时读取分析与学习结果，统计差异和共性，明确保留项和修改项，不改变事实与研究主张。"),
        ("en_rewrite", "形成英文修改稿", "按决策在英文语料环境中改写，不自由增删实验和结论。"),
        ("zh_back", "中文回译", "从英文修改稿回译中文，形成供老师阅读的对应稿，不独立另写。"),
        ("review", "审查监督双稿", "对照原文、参照学习、决策和双稿，核对事实、术语、宏、公式、排版与上下文遗漏。"),
        ("compile_verify", "本机编译并核验", "候选本机编译、检查日志与 PDF 排版，缺少实测证据不得声称通过。"),
        ("archive_unit", "归档通过单元", "验收通过后留存双稿、差异、编译证据和台账，再处理下一段；失败不自动进入下一段。"),
        ("final_summary", "汇总双稿与未结项", "汇总全部已审查单元的中英稿、验收证据与未解决事项，不将句长治理进度等同于整篇六角色流程完成。"),
    ]
    edges = [(stages[i][0], stages[i+1][0]) for i in range(4)]
    edges += [("reference_gate", "analyze"), ("reference_gate", "learn"), ("analyze", "decide"), ("learn", "decide")]
    edges += [("decide", "en_rewrite"), ("en_rewrite", "zh_back"), ("zh_back", "review"),
              ("review", "compile_verify"), ("compile_verify", "archive_unit"), ("archive_unit", "final_summary")]
    return {
        "artifact_type": "workflow_recovery_review", "executable": False, "conversation_id": PAPER,
        "replaces_graph_id": "g_85504ab8", "goal": "参照论文驱动的逐段六角色中英双稿改写与 LaTeX 验收",
        "original_user_request": original, "sources": source_refs,
        "nodes": [{"node_id": id, "label": title, "goal": goal, "binding_status": "待核对现用 provider/model，不沿用元流程绑定"} for id, title, goal in stages],
        "dependencies": [{"from": a, "to": b, "requires": "上游验收通过；两路分析与学习均就绪后才决策"} for a,b in edges],
        "iteration": {"semantics": "foreach：每一已确认段落完整执行 analyze 至 archive_unit，队列全部通过后才 final_summary",
                      "queue": None, "status": "必须核对现用源码与分段队列后冻结，不用旧行号或台账编号伪造队列"},
        "subflows": [{"name": "句长治理/跨段一致性/工程验收", "relation": "服务主流程；不得替代参照学习和双稿职责；历史报告单独保存"}],
        "blocking_review": ["核对当前工作副本、已经完成与仍需处理的段落，冻结 foreach 队列，避免重做已落稿内容。",
                            "核对当前六角色 provider/model 绑定及审查失败回退规则；不把六角色说成六种模型。",
                            "先核对差异，再将恢复后的业务流程应用为新版本；不从候选启动或覆盖当前会话工作。"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    project, output = args.project.resolve(), args.output.resolve()
    if output == project or project in output.parents or output in project.parents or output.exists():
        raise ValueError("Output must be a NEW directory outside the live project")
    tracked = {}
    def read(path):
        data = path.read_bytes()
        tracked[path] = hashlib.sha256(data).hexdigest()
        return data.decode("utf-8-sig")
    graph_path = project / ".mewcode/pipelines/g_0060428c.json"
    graph = PipelineGraph.model_validate_json(read(graph_path))
    before = from_pipeline_graph(graph).as_dict()
    if before["meta"].get("conversation_id") != DRAWING:
        raise ValueError("Drawing graph owner changed")
    paper_path = project / ".mewcode/pipelines/g_85504ab8.json"
    paper_before = from_pipeline_graph(PipelineGraph.model_validate_json(read(paper_path))).as_dict()
    if paper_before["meta"].get("conversation_id") != PAPER:
        raise ValueError("Paper graph owner changed")
    source_refs = {}
    for cid, lines in ((DRAWING, [1,9,13,20,55,66,135,138]), (PAPER, [1,1136,1311,1392,6003,6033])):
        path = project / ".mewcode/sessions" / (cid + ".jsonl")
        rows = read(path).splitlines()
        refs = []
        for n in lines:
            row = json.loads(rows[n-1])
            refs.append({"path": str(path), "line": n, "role": row.get("type"), "text": public_text(row.get("content"))})
        source_refs[cid] = refs
    # If history shifted, fail rather than cite unrelated line numbers.
    if "RGB-T" not in source_refs[DRAWING][4]["text"] or "每一段" not in source_refs[PAPER][0]["text"]:
        raise ValueError("Source anchors changed")
    repaired, changes = repair_drawing(before)
    spec = OrchestrationSpec.from_value(repaired)
    require_resolved_plan_inputs(spec)
    warnings = validate_spec(spec)
    to_expert_flow(spec)  # compilation only; no run or provider call
    canvas = to_pipeline_graph(spec, graph_id=graph.graph_id, version=graph.version + 1)
    roundtrip = from_pipeline_graph(canvas)
    assert roundtrip.edges == spec.edges
    assert [(n.node_id,n.model,n.plan_brief) for n in roundtrip.nodes] == [(n.node_id,n.model,n.plan_brief) for n in spec.nodes]
    assert repaired["meta"].get("loops") == before["meta"].get("loops")
    assert [n["model"] for n in repaired["nodes"]] == [n["model"] for n in before["nodes"]]
    artifacts = {
        "drawing.spec.candidate.json": repaired,
        "drawing.graph.candidate.json": canvas.model_dump(mode="json", by_alias=True),
        "drawing.diff.json": {"changes": changes, "sources": source_refs[DRAWING], "warnings": warnings},
        "paper.recovery.review.json": paper_recovery_document(source_refs[PAPER], source_refs[PAPER][0]["text"]),
        "baseline.sha256.json": {str(p): h for p,h in tracked.items()},
    }
    # Verify input files did not change while preparing the snapshot. A running
    # conversation may append; in that case retry against the new baseline.
    for path, checksum in tracked.items():
        if hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
            raise ValueError(f"Input changed while preparing candidate: {path}")
    output.mkdir(parents=True, exist_ok=False)
    for name, value in artifacts.items():
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "README.md").write_text(
        "# painter-flow 修复候选（尚未应用）\n\n"
        "绘图流：保留 24 个业务节点、26 条边、3 个循环及模型绑定；补全 7 个节点的未解析输入，将 4 条放行边改为 pass。"
        "具体变化与逐字来源见 drawing.diff.json。助手修复建议仍是候选口径，G1 配额/尺寸需冻结；不把外部网络不可用伪装成核验成功。\n\n"
        "论文流：paper.recovery.review.json 恢复原始业务主线、六角色依赖、逐段语义和本机编译约束。"
        "它不是可运行 spec：当前源码/段落队列、已完成范围和模型绑定尚待核对，不能用历史行号猜测或重新执行全部论文。\n\n"
        "本工具仅导出候选，未写入 painter-flow，未更改暂停运行的 locked_flow，未启动任务。"
        "应用前核对 baseline.sha256.json 和差异；如已有新对话/图修改须重新核对，不能覆盖新版本。\n",
        encoding="utf-8")
    print(json.dumps({"output": str(output), "drawing_nodes": len(spec.nodes), "drawing_edges": len(spec.edges),
                      "changes": len(changes), "live_project_modified": False, "paper_executable": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
