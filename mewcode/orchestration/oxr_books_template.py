"""The Guodian books task, with server details collected for each execution."""
from __future__ import annotations

from pathlib import Path


BOOKS = (
    ('circuits', '电路', '第一章 电路的模型及其基本规律.pdf', 192),
    ('machines', '电机学', '电机学.pdf', 234),
    ('accounting', '电力会计', '前言 电力会计专项总结.pdf', 282),
    ('mathematics', '数学习题', '数学习题.pdf', 365),
)
MODEL = {'model_id': 'gpt-6.1-sol', 'effort': 'max'}
OXR_REPOSITORY = 'https://github.com/SoMarkAI/OXR'

CONTEXT = '''将sample-project目录中的四本 PDF 用自部署 OXR 转为可追溯的结构化资料库：页码、阅读顺序、识别块内容与坐标、高清图片、题目答案与解析、凝练的知识点。
全流程执行约定：先按启动参数连接用户服务器，完成环境部署、材料准备，再按依赖处理四本书，最后入库和回传。各节点正常交付即推进，不追加隐式质量评审或以完美为目标的返修循环。真实连接故障或缺必要输入时才请求用户补充；个别不可辨认页、题目缺条件等登记为 pending/failed，保存已有结果，继续其他页并在交付中明确未完成范围。
输入 PDF 只读；产物只写本次 remote_root 和 local_output 目录。部署目录、模型缓存、程序代码由前置共享节点准备；四书执行节点只写各自 books/<book_id>，题解只写 questions，知识节点只写 knowledge，不相互覆盖。重新运行复用文件校验和、模型版本、解析参数一致的完成页，修改参数时使用新 revision，最终数据库按稳定 ID 幂等更新，不把旧内容误当新结果。
所有业务工作节点使用 gpt-6.1-sol / max，OXR 仅负责文档识别。不使用旧模型、其他 effort 或未声明的自动替代路由；codex-auto-review 是宿主审批路由而非模型。解析与题解可以编写分批处理程序，但不能暗中改用其他推理模型。
统一来源引用为 book_id、源文件 sha256、pdf_page（从 1 开始）、printed_page（书内印刷页码，无法确认时为 null）、block_id；跨页题目保留全部来源块。书名仅凭文件名记录，真实封面/目录名称另存，不据文件名虚构内容。
结构化字段契约：pages 保存 page_id、book_id、pdf_page、printed_page、width_px、height_px、render_dpi、rotation、page_image、status；blocks 保存 block_id、page_id、parent_id、order、type、bbox=[x0,y0,x1,y1]、text、latex、table_html、asset_ids、confidence、source_refs。坐标以原始渲染页左上角为原点，明确像素尺寸；保留 OXR 原坐标、单位和向原始页坐标的变换，纠偏/裁剪后用逆变换回映。OXR 不提供置信度时写 null，不造数。
assets 保存 asset_id、book_id、page_id、block_id、原图路径、增强图路径、可选外链、caption、sha256、尺寸与 enhancement_operations；原图永不被增强图覆盖，文字、公式、电路线条不做生成式补画。外链基地址未给出时使用随包的相对路径，不构造假链接；OXR 临时下载链接须及时归档为本次数据集的持久文件。
questions 保存 question_id、题号、题干、选项、题图引用、source_refs、book_answer、answer、solution、answer_origin、status、uncertainties、knowledge_ids；书中答案保留原文，模型推导明确标记 model_generated，缺图/缺条件导致不确定时不编答案。解析给出可复核的解题步骤、关键公式与必要计算。
knowledge 保存 knowledge_id、title、summary、definition、formulas、conditions、common_errors、chapter_path、source_refs、related_question_ids、related_knowledge_ids；同义知识可合并但保留各来源与不同适用条件。
节点以简短公开进展说明重要发现与结果，供卡片显示最近五条；不贴成串工具流水账。每个节点用 ReportArtifact 登记本地可访问的索引、结果与摘要；远程大文件提供索引与下载位置，最终回传完整结果。'''


def startup_inputs(source_dir: str | None = None, local_output: str | None = None):
    source_dir = source_dir or str(Path.home() / 'Desktop' / 'sample-project')
    local_output = local_output or str(Path(source_dir) / 'OXR结构化成果')
    def field(name, label, *, required=True, default=None, **extra):
        result = dict(name=name, label=label, question=label, type='text',
                      required=required, collect_at='start', **extra)
        if default is not None:
            result['default'] = default
        return result
    return [
        field('ssh_host', '服务器地址或 SSH 配置别名'),
        field('ssh_user', 'SSH 用户名'),
        field('ssh_port', 'SSH 端口', default='22'),
        field('ssh_auth', 'SSH 登录方式', default='agent', options=[
            dict(value='agent', label='SSH Agent / 已配置身份', recommended=True),
            dict(value='key_file', label='本机私钥文件'),
            dict(value='interactive', label='交互登录', description='密码只在实际 SSH 登录时交互输入，不保存进任务参数'),
        ]),
        field('ssh_key_path', '本机私钥文件路径（只填路径）',
              required_when={'input': 'ssh_auth', 'equals': 'key_file'}),
        field('ssh_jump', '跳板机或 ProxyJump（可留空）', required=False),
        field('remote_root', '服务器工作目录', default='~/guodian-oxr'),
        field('source_dir', '本机四本 PDF 所在目录', default=source_dir),
        field('local_output', '本机成果保存目录', default=local_output),
        field('assets_base_url', '已有图片外链基地址（可留空）', required=False),
    ]


def oxr_books(goal: str = '', *, provider_key: str = '', source_dir: str | None = None,
              local_output: str | None = None):
    """Return a native editable spec. No network, model calls or data writes."""
    model = dict(provider_key=provider_key, **MODEL)
    nodes, edges = [], []

    def node(key, label, task, inputs, outputs, x, y):
        nodes.append(dict(node_id=key, kind='implement', label=label, goal=task,
            bind_later=not bool(provider_key), model=dict(model), position=dict(x=x, y=y),
            plan_brief=dict(summary=task, known_inputs=inputs, outputs=outputs)))

    def edge(source, target):
        edges.append({'from': source, 'to': target, 'condition': 'always'})

    node('connect', '连接服务器', '''读取本次启动参数，解析并验证 SSH 主机、用户名、端口、登录方式、跳板机与工作目录，建立连接。
SSH 参数用参数数组传递，不把用户字段拼成可执行 shell 命令；远端路径正确引用。保留正常主机身份核验。私钥只读取指定文件，不复制私钥内容到服务器或报告；交互登录时使用现有用户接管/提问工具，不把密码加入命令行、流程 JSON 或公开日志。
检测 OS、CPU、内存、磁盘空闲、GPU/显存、驱动、CUDA、Docker/GPU runtime、网络与目录写权限。选择可写工作目录并记录其实际绝对路径；创建本任务目录，不调整现有服务。将无秘密的环境事实保存为 connection.json，注明本次使用的连接参数引用和远端路径；不要把密钥或密码写入文件。''',
         ['本次启动参数'], ['local_output/connection.json（无密码、密钥内容）'], 40, 610)

    node('deploy', '部署 OXR 服务', '''按 connection.json 的服务器实况部署 https://github.com/SoMarkAI/OXR，自部署而不是将整本材料发送到公共演示 API。
执行时重新读官方 README 与配置，记录代码 commit、模型 revision、镜像 digest 和依赖版本。当前官方基线：Linux、支持 BF16 的 NVIDIA GPU、CUDA 13.0 兼容驱动，单卡 24GB 可低并发尝试，48GB 以上适合长文档；以实际硬件和启动测试决定参数，不宣称低配置必定可用。
优先官方 Docker all-in-one：docker/Dockerfile.allinone 与 SoMarkAI/OXR-1.0（含 layout_analysis）。可从官方 Hugging Face 或 ModelScope 镜像下载。无法用 Docker 时按官方方式分离 OXR/vLLM、版面模型、解析 API 三个进程和环境。已有匹配服务可复用。
API 绑定服务器回环地址，通过 SSH 隧道访问，默认不开放公网端口。以 /v1/parse/sync 的 file、api_key=dummy、output_formats=json/markdown 做小样本烟测并保存结果。配置共享请求队列/并发信号量，四本书共用 GPU 限额，显存不足时缩小批次与并发。保存启动/停止脚本及 runtime.json，包含服务端点、版本、实际并发和恢复命令。''',
         ['connect：connection.json', OXR_REPOSITORY],
         ['local_output/runtime.json', 'remote_root/service/ 启停脚本与 smoke-test.json'], 420, 410)
    edge('connect', 'deploy')

    node('prepare', '整理四书与处理程序', '''读取 source_dir 中清单指定的四本 PDF，核对文件存在、真实页数、大小与 sha256；清单页数是已观察的基线，若文件更新则记录新版本，不用旧页数截断。
分配四个 book_id：circuits、machines、accounting、mathematics。保留原 PDF，传输到 remote_root/inputs，核对远端 sha256，支持断点传输。准备 books/<book_id> 的独立结果目录与逐页状态清单。
编写并交付共享处理程序：按页/小批次调用 OXR、保存原始响应与 Markdown/JSON、建立页块表、提取图片、坐标回映、异常页登记、结果 JSONL 验证与 SQLite 入库。处理程序不依赖此节点已启动 OXR；实际烟测由部署节点完成。把统一字段契约保存为 schemas/，保存 CLI 用法、版本参数和可重复执行的命令；后继只调用已准备好的程序。
输出 manifest.json，包括四书绝对路径、book_id、sha256、页数、页范围与目标目录，并回传本机同名索引。''',
         ['connect：connection.json', 'source_dir 中四本 PDF（名称见任务材料清单）'],
         ['local_output/manifest.json', 'remote_root/pipeline/ 与 schemas/'], 420, 810)
    edge('connect', 'prepare')

    for index, (book_id, title, filename, pages) in enumerate(BOOKS):
        y = 40 + index * 390
        source = f'{filename}；已观察 {pages} 页；book_id={book_id}'
        ocr, questions, knowledge = [f'{stage}_{book_id}' for stage in ('ocr', 'questions', 'knowledge')]
        node(ocr, f'{title} · OCR 与图片', f'''仅处理 {source}。等待部署和材料准备均完成，读取 runtime.json 与 manifest.json 后调用共享处理程序。
先用本书包含文字、题目、公式/表格或图片的代表页确定 DPI 与版面参数，再覆盖 manifest 中本书全部实际页。典型从 300 DPI 渲染，细小字/线图可局部 400–600 DPI 重渲染；按 OXR 输入限制缩放并记录坐标变换，不盲目超分。
每页保存原始 OXR 响应、Markdown、页图、printed_page 映射以及规范化 pages/blocks。保留层级、阅读顺序、标题、表格、LaTeX、图题与跨页引用；表格跨页合并时保留原始块。原文与纠错字段分开。
提取本书图表/题图的原始嵌入图或高分辨率裁剪，轻度去噪、纠偏、对比度增强；电路符号、文字、公式与表格线不能通过生成式补全。生成原图/增强图配对和 assets.jsonl。assets_base_url 有值且对应位置可写时按授权路径存放并验证链接；否则保存相对路径，不额外申请云存储。
用稳定 ID 和逐页 checkpoint 支持断点；记录不可识别和真实请求失败页，保留可用数据并交付覆盖情况。不要因个别疑点反复重跑整本书。''',
             ['deploy：runtime.json', f'prepare：manifest.json 中 {book_id}', source],
             [f'books/{book_id}/pages.jsonl、blocks.jsonl', f'books/{book_id}/raw/、assets/、assets.jsonl',
              f'books/{book_id}/ocr-progress.json、issues.jsonl'], 800, y + 90)
        edge('deploy', ocr)
        edge('prepare', ocr)
        node(questions, f'{title} · 题目答案解析', f'''只处理 books/{book_id} 的已归档 OCR 数据和题图。
依据题号、结构与上下文识别例题、练习题、选择题、填空题和解答题，拼接跨页题干与选项，匹配书后答案并保留 book_answer 原文及来源。题干含图、表或公式时读取对应原图，不能仅凭破碎 OCR 猜题。
逐题给出 answer 与可复核的 solution，注明所用公式、条件、单位和关键计算；可用符号/数值程序核算。书中答案与推导冲突时并列保留并说明，不能覆盖原答案。会计题按书中适用口径解题；需要外部现行规则时单独注明来源与日期。
answer_origin 使用 book/model_generated/unresolved；缺条件、图无法辨认或推导未确认时设置 status=needs_review 并写 uncertainties，仍保存题目和可用部分。没有题目的章节明确记录数量为 0，不造题凑数。
按页段分批落盘并保存处理游标。只写 books/{book_id}/questions.jsonl、questions-progress.json 和 questions-summary.md；读取共享文件但不修改 OCR/knowledge 数据。最终列出题目数量、答案来源和待复核项。''',
             [f'{ocr}：blocks.jsonl、pages.jsonl、assets.jsonl 和原图'],
             [f'books/{book_id}/questions.jsonl', f'books/{book_id}/questions-summary.md'], 1190, y)
        node(knowledge, f'{title} · 知识点凝练', f'''只读取 books/{book_id} 已归档 OCR 资料，依据目录、标题和内容建立 chapter_path。
提取概念、定义、定理、公式、适用条件、常见误区及典型应用；每项凝练为可独立检索的知识记录，保留原始术语与 source_refs。公式与电路/电机物理量解释清楚，数学结论保留成立条件，会计口径保留书中上下文。
相近条目按含义和适用条件合并，同时保留所有页/块引用；不要把原文不确定内容润色成确定事实。建立相关知识之间的引用。本节点和题解并行，不假定 questions.jsonl 已存在；最终跨表关联由入库节点完成。
逐章节增量保存，只写 books/{book_id}/knowledge.jsonl、knowledge-progress.json 和 study-notes.md，不修改题解或 OCR 文件。''',
             [f'{ocr}：blocks.jsonl、pages.jsonl、原文与目录'],
             [f'books/{book_id}/knowledge.jsonl', f'books/{book_id}/study-notes.md'], 1190, y + 190)
        edge(ocr, questions)
        edge(ocr, knowledge)

    node('deliver', '汇总入库与交付', '''收齐四书本轮已产出的文件，生成 guodian.sqlite3 和同内容的 JSONL/Markdown 导出，不覆盖用户已有数据库。
建立 books、pages、blocks、assets、questions、knowledge、question_knowledge、knowledge_relations 表及全文检索索引；主键使用稳定 ID，引用使用外键，所有坐标/页码保留原始单位和来源。题目与知识点建立关联，跨书同名概念保留各来源及适用条件。
执行确定性的入库检查：清单总页数与 completed/pending/failed 之和、页码范围、外键、重复 ID、bbox 边界、JSONL 可读性、图片可访问性，以及若干按页/题号/知识点查询。这是数据导入职责，不再调用模型做完美性评审。具体错误写入 issues.jsonl，修正本次装载问题；业务疑点原样保留状态，不擅自宣布全部答案已验证。
以事务和幂等 upsert 写入本次数据库，生成便携 index.html 阅读入口（书→章节→页→块；题目→解析→知识点；原图/增强图），引用可随包打开的本地相对资产或已验证外链。
把数据库、全部结构化数据、图片资产、阅读入口、可复现处理程序及摘要回传 local_output；校验回传 sha256。manifest-final.json 明确每书页覆盖、失败页、题目/知识点数量、答案来源、模型版本、参数和文件位置；DELIVERY.md 说明如何浏览、查询与仅重跑某书/某页。节点报告登记可点击的本地交付文件。''',
         ['四书的 pages/blocks/assets/questions/knowledge 与问题清单', 'prepare：manifest.json'],
         ['local_output/guodian.sqlite3', 'local_output/index.html',
          'local_output/manifest-final.json、DELIVERY.md、books/ 与 pipeline/'], 1570, 610)
    for book_id, *_ in BOOKS:
        edge(f'questions_{book_id}', 'deliver')
        edge(f'knowledge_{book_id}', 'deliver')

    materials = '\n'.join(f'{key}: {filename}（已观察 {pages} 页）' for key, _, filename, pages in BOOKS)
    return dict(spec_id='guodian.oxr.books', template_id='oxr_books', label='sample-project四书 · OXR 结构化资料库',
        version=1, goal=CONTEXT + '\n材料清单：\n' + materials + ('\n本次补充目标：' + goal if goal else ''),
        require_confirm_gate=False, nodes=nodes, edges=edges, finalizer_node_id='deliver',
        goal_contract=dict(objective='四本书按页与识别块结构化，保存图片、题目答案解析和凝练知识点并入库',
                           run_inputs=startup_inputs(source_dir, local_output)),
        meta=dict(orchestration_mode='classic', parallel=True,
                  oxr_repository=OXR_REPOSITORY,
                  execution_model=dict(MODEL),
                  materials=[dict(book_id=k, filename=f, observed_pages=p) for k, _, f, p in BOOKS]))
