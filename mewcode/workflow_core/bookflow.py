"""A bounded book queue with eight independent lanes and deterministic assembly."""
from __future__ import annotations

from pathlib import Path

from .schema import normalize

MODEL = dict(provider_key='codex:codex-custom', model_id='gpt-6.1-terra', effort='max')
TOOLS = ['ReadFile', 'WriteFile', 'EditFile', 'Bash', 'ReadAuditArtifact', 'ReportArtifact', 'report_step',
         'ToolSearch', 'InspectVisualEvidence']


def powershell(*arguments):
    return '& ' + ' '.join("'" + str(argument).replace("'", "''") + "'" for argument in arguments)


def build(root, *, python, script, books=('circuits', 'machines', 'accounting', 'mathematics'), lanes=8):
    if not 1 <= lanes <= 16:
        raise ValueError('Lane count must be between 1 and 16')
    root = Path(root)
    command = powershell(python, script)
    root_arg = "'" + str(root).replace("'", "''") + "'"
    common = ('只执行本节点职责。全书源文本、图片、题目和答案来源已由程序准备，原材料只读。'
              '一小时是吞吐目标，不是强制停止条件。保留来源疑点，普通交付即可推进；'
              '不要增加逐小批全书复核、隐式验收、模型间追问或完成证书。'
              '已完成的包直接复用，失败只修受影响的包。')
    def node(identity, label, task):
        return dict(id=identity,kind='work',label=label,task=common+'\n'+task,
                    model=MODEL.copy(),config={},inputs=[],outputs=[],tools=TOOLS.copy())
    setup = node('book_stage', '准备本书完整输入',
        '从循环输入 item 读取当前 book_id，只运行一次以下命令，将 <book_id> 替换成 item：\n'
        +command+' stage --root '+root_arg+' --book <book_id>\n'
        '命令完成后按 pages、blocks、packets 简短报告，标记保留的原始缺损。'
        '这一步按原记录生成整书输入，不等待旧节点补交。不要重复调查环境或读取全部原文。')
    workers = []
    edges = []
    for lane in range(lanes):
        identity = f'book_lane_{lane}'
        workers.append(node(identity, f'章节并行 {lane+1}/{lanes}',
            f'你负责 lane {lane}，只写自己的结果文件。当前 book_id 是循环输入 item。\n'
            f'完整读取 {root}/lanes/<book_id>/lane-{lane}.json（ReadFile limit=100000），它包含本 lane 全部包、原文、'
            '只读邻接上下文、题答及源图路径和结果格式。通读后一次处理全部包；仅疑点需要查看对应原页像素。'
            '只给出标题层级、跨块关联、必要顺序调整和未决疑点，不重新生成全部正文，不自行解题或改写原答案。'
            '默认按原文顺序，不必抄写全部 block_id。块ID可直接使用输入中的源内简写。headings 项必须是 {"id":"原块ID","level":0或1或2或3}，'
            'relations 项必须是 {"source":"本包源块ID","target":"原块ID","kind":"关系类型"}。'
            '截图时间戳、重复页眉等只有对照源像素确认为页面背景后，才将块ID放入source_context；'
            '程序仍保留其完整来源，不可将正文或未能判定的内容移出正文。'
            '每个包都返回 completed 或 needs_review；存在原始缺损时注明具体 issue，保留记录后正常交付，'
            '不将它变成阻止其他章节处理的条件。不要声称未实际核验的内容已正确。\n'
            f'将完整结果一次写到 {root}/decisions/<book_id>/lane-{lane}.json，随后执行：\n'
            +command+' validate --root '+root_arg+f' --book <book_id> --lane {lane}\n'
            '若反馈结构或归属错误，在当前对话修正该文件并复查。正常返回后直接简短交付文件路径。'))
        edges.append(dict(id=f'stage_{lane}',source='book_stage',target=identity,port='always'))
    merge = node('book_build', '程序合成、全量校验与编译',
        '当前 book_id 是循环输入 item。八路结果均已交付后，只运行：\n'
        +command+' assemble --root '+root_arg+' --book <book_id>\n'
        '程序保留源块全文、生成来源映射、完整性校验、疑点清单、ElegantBook TeX、PDF 和 ZIP。'
        '不要重新审核或重写所有章节。若是编译错误，读取对应日志，仅修正排版程序或不安全数学的显示，'
        '保留原文与问题记录，不重复派发已经完成的章节包。'
        f'成功后用 ReportArtifact 登记 {root}/outputs/<book_id>/main.pdf、'
        f'{root}/outputs/<book_id>-LaTeX.zip、coverage.json、issues.json。'
        '普通回复应区分源块覆盖、编译成功和仍待独立核实的内容。')
    for lane in range(lanes):
        edges.append(dict(id=f'lane_build_{lane}',source=f'book_lane_{lane}',target='book_build',port='always'))
    reviewers = []
    review_edges = []
    for lane in range(lanes):
        identity = f'page_review_{lane}'
        reviewers.append(node(identity,f'独立检查成书页面 {lane+1}/{lanes}',
            f'当前 book_id 是循环输入 item。完整读取 {root}/reviews/<book_id>/lane-{lane}.json。'
            '你是独立只读检查节点，只写该lane的result文件，不改正文或其他lane结果。'
            '按分配页逐页使用 InspectVisualEvidence 查看成书图像，并对照清单中的原文、原图，'
            '检查漏文、错序、公式/表格越界、缺字、裁切、题图与答案归属；不能用图像尺寸或编译成功替代实际像素。'
            '使用宿主已配置、已授权的图像路线；无法观察必须记unobserved，不能假称通过。'
            '原书疑点记source_unknown，本次生成缺陷记generated_defect，实际检查无问题记passed。'
            '已有同image_sha256的真实逐页裁决可复用，只复查变化页。'
            '按输入schema将全部页的结果和具体证据/疑点写到output指向的自己result文件，正常交付。'))
        review_edges.append(dict(id=f'review_join_{lane}',source=identity,target='review_finish',port='always'))
    finish = node('review_finish','汇总检查并定向修正',
        '当前 book_id 是循环输入 item。运行：\n'+command+' finish-review --root '+root_arg+' --book <book_id>\n'
        '查看汇总的ready、counts和定位问题。若有本次生成缺陷，定向修正当前书结构决策或排版程序，'
        '仅重新assemble当前书以更新PDF与页面清单，保留旧逐页记录供相同页面复用；然后返回false继续已声明的检查循环。'
        '没有生成缺陷、漏映射或未观察页时返回true；原书疑点独立保留，不作正确性PASS。'
        '最终只输出JSON：{"value":true或false,"reason":"实际结果"}。')
    finish['kind'] = 'judge'
    review_loop = dict(id='book_review',kind='loop',label='八路独立逐页检查与定向返修',task=common,
        model=MODEL.copy(),config=dict(mode='until',until={'path':'results.review_finish.value','equals':True},
            graph=dict(nodes=[*reviewers,finish],edges=review_edges,layout={})),inputs=[],outputs=[])
    edges.append(dict(id='build_review',source='book_build',target='book_review',port='always'))
    body = dict(label='每本书八路处理',nodes=[setup,*workers,merge,review_loop],edges=edges,layout={
        'book_stage':dict(x=20,y=300), 'book_build':dict(x=880,y=300),
        **{f'book_lane_{lane}':dict(x=440,y=lane*300) for lane in range(lanes)}})
    loop = dict(id='fast_books',kind='loop',label='整书重构 · 8 路章节并行',task=common,
        model=MODEL.copy(),config=dict(mode='foreach',items=list(books),graph=body),inputs=[],outputs=[])
    deliver = node('fast_deliver','展示四书 PDF 与 LaTeX 交付',
        f'汇总 {root}/outputs 各书已经成功生成的 PDF、LaTeX ZIP、覆盖报告和疑点清单，'
        '给出可点击路径、逐书源页/块覆盖与未决事项。保留旧运行与其有效成果；不要重跑已完成的构建。')
    return normalize(dict(format='mewcode.workflow.v2',revision=9,label='四书整书快速重构',concurrency=lanes,
        execution_profile='bookflow.parallel.v1',
        goal='在源文与图片完整保留、原有疑点可追溯的前提下，按整书准备、章节并行、程序合成方式生成可读 PDF 和可编辑 LaTeX。',
        nodes=[loop,deliver],edges=[dict(id='books_deliver',source='fast_books',target='fast_deliver',port='always')],
        layout={'fast_books':dict(x=40,y=80),'fast_deliver':dict(x=500,y=80)}))
