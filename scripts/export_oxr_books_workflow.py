"""Export the editable task without connecting to a server or running OCR.

Run from the repository: python -m scripts.export_oxr_books_workflow
  --source-dir <PDF directory> --output-dir <new task directory>
  --provider-key <configured provider offering gpt-6.1-sol / max>
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from pypdf import PdfReader

from mewcode.orchestration.convert import to_expert_flow
from mewcode.orchestration.oxr_books_template import BOOKS, oxr_books
from mewcode.orchestration.spec import OrchestrationSpec
from mewcode.orchestration.validate import validate_spec
from mewcode.workflow_core.migration import graph_from_flow


def export(source_dir, output_dir, provider_key):
    source, target = Path(source_dir).resolve(), Path(output_dir).resolve()
    spec = OrchestrationSpec.from_value(oxr_books(provider_key=provider_key, source_dir=str(source)))
    validate_spec(spec, require_bindings=True)
    flow = to_expert_flow(spec)
    graph = graph_from_flow(flow, goal=spec.goal)
    graph['layout'] = {node.node_id: node.position for node in spec.nodes if node.position}
    # Preserve the complete Core graph and runtime input contract in the portable flow.
    flow_value = flow.as_dict()
    flow_value['meta']['workflow_core'] = graph
    manifest = []
    for book_id, _, filename, _ in BOOKS:
        path = source / filename
        with path.open('rb') as stream:
            sha256 = hashlib.file_digest(stream, 'sha256').hexdigest()
        manifest.append(dict(book_id=book_id, filename=filename, path=str(path),
            bytes=path.stat().st_size, pages=len(PdfReader(path).pages), sha256=sha256))
    examples = {row['name']: row.get('default', '') for row in spec.goal_contract.run_inputs}
    files = {
        'orchestration.json': spec.as_dict(),
        'workflow.json': graph,
        'flow.json': flow_value,
        'run-inputs.example.json': examples,
        'materials.json': dict(books=manifest, total_pages=sum(row['pages'] for row in manifest)),
    }
    # Do not overwrite a user's edited task with a fresh export.
    if target.exists() and any((target / name).exists() for name in [*files, '使用说明.txt']):
        raise FileExistsError(f'输出目录已有任务流，请使用新目录：{target}')
    target.mkdir(parents=True, exist_ok=True)
    for name, value in files.items():
        (target / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    summary = '\n'.join(f"- {row['filename']}：{row['pages']} 页" for row in manifest)
    (target / '使用说明.txt').write_text(f'''sample-project四书 · OXR 结构化资料库

这是已完成设计的任务流，尚未连接服务器，也未开始识别。
材料共 {sum(row['pages'] for row in manifest)} 页：
{summary}

执行顺序
1. 连接服务器并记录硬件环境。
2. 并行部署 OXR、整理上传四本书并准备共享处理程序。
3. 每本书分别识别页与块、归档原图和增强图。
4. 每本书的题目答案解析、知识点凝练并行进行。
5. 汇总为 SQLite / JSONL / Markdown 与可浏览索引，回传本机。

启动时填写
服务器地址、SSH 用户名；默认端口 22，可选跳板机。
登录支持 SSH Agent、私钥路径、交互登录。密码不填入流程文件。
可改服务器工作目录、本机源目录、本机输出目录；已有图片外链基地址可选。
这些字段在 orchestration.json 的 goal_contract.run_inputs 中声明，由 MewCode 现有启动参数面板在执行时收集。
run-inputs.example.json 只是空白示例，未填写主机与用户名时启动校验会拒绝执行。

使用文件
- orchestration.json：MewCode 原生编排方案，含 16 个节点、26 条依赖与启动参数。
- workflow.json：新版 Core 原生画布图，可独立编辑节点与位置。
- flow.json：执行兼容包，包含同一 Core 图和输入合同。
- materials.json：实际文件路径、页数、大小及 SHA-256。

MewCode 模板库已添加 oxr_books（sample-project四书 · OXR 结构化资料库）。
模板先保存、运行时收集连接信息。导出方案节点绑定 {provider_key} / gpt-6.1-sol / max；导入其他机器时仅调整 Provider 到支持同一模型和 effort 的路由，不自动替换模型。
API 保存使用 POST /api/orchestration/confirm，JSON 请求为 {{"spec": orchestration.json 的对象, "activate": false, "conversation_id": 当前 MewCode 会话 ID}}。
该操作只保存到当前会话；运行仍从工作台启动并填写参数。不要对演示验收服务发起实际业务执行。

预期业务产出（将在执行时生成）
- pages / blocks：从 1 开始的 PDF 页码、印刷页码、块层级/阅读顺序、文本、公式、表格、坐标与来源。
- assets：原图、合理增强图、标题、尺寸、来源块、哈希及可选持久外链。
- questions：题干、选项、题图、书中原答案、生成答案、解析、答案来源与待复核状态。
- knowledge：定义、凝练摘要、公式、适用条件、易错点、相关题目与原书引用。
- guodian.sqlite3、JSONL、index.html、manifest-final.json、完整图片与可复现程序。

节点正常交付后推进；疑点记录并保留未确认状态。不会添加隐式模型验收或无限返修。
逐页 checkpoint 和稳定 ID 支持局部重跑，数据库幂等写入；四本书各自有独立结果目录。
卡片显示最近五条公开进展，节点详情保存说明和可点击产物。

OXR 官方说明：https://github.com/SoMarkAI/OXR
当前基线为 Linux、支持 BF16 的 NVIDIA GPU、CUDA 13.0 兼容驱动；24GB 用于低并发尝试，48GB 以上适合长文档。实际参数由部署节点结合服务器实况确定。
OXR 识别输出不能代替题解推导；最终报告会分别列明书中答案、模型推导和未解决项。
''', encoding='utf-8')
    return dict(directory=str(target), nodes=len(graph['nodes']), edges=len(graph['edges']),
                books=len(manifest), pages=sum(row['pages'] for row in manifest))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--provider-key', required=True)
    args = parser.parse_args()
    print(json.dumps(export(args.source_dir, args.output_dir, args.provider_key), ensure_ascii=False))
