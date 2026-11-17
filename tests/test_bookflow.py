import asyncio
import copy
import json

import pytest

from mewcode.bookflow import project
from mewcode.bookflow.render import inline, safe_math
from mewcode.runtime_scheduler import RuntimeScheduler
from mewcode.workflow_core.bookflow import build
from mewcode.workflow_core.runtime import Core, MemoryBackend
from mewcode.workflow_core.state import fresh


@pytest.fixture
def book_project(tmp_path):
    source, root = tmp_path/'source', tmp_path/'project'
    directory = source/'books/circuits'
    directory.mkdir(parents=True)
    blocks = [dict(block_id=f'p{page}-b{order}', pdf_page=page, order=order,
                   type='Text', text=f'Source {page}/{order}', asset_ids=[])
              for page in range(1, 8) for order in range(2)]
    for name in project.RECORDS:
        content = (json.dumps({}) if name == 'book.json' else '' if name != 'blocks.jsonl'
                   else '\n'.join(json.dumps(b) for b in blocks))
        (directory/name).write_text(content, encoding='utf-8')
    catalog = project.prepare(root, source, books=['circuits'], lanes=2, packets=3)
    project.stage(root, 'circuits')
    for lane in range(2):
        payload = project.read(root/'lanes/circuits'/f'lane-{lane}.json')
        value = dict(book='circuits', lane=lane, source_sha256=payload['source_sha256'],
                     packets=[dict(id=p['id'], status='completed', headings=[], relations=[], issues=[])
                              for p in payload['packets']])
        project.write(root/'decisions/circuits'/f'lane-{lane}.json', value)
    return root, catalog, blocks


def test_packets_partition_whole_pages_without_omissions(book_project):
    root, catalog, blocks = book_project
    assigned = [identity for p in catalog['books']['circuits']['tasks'] for identity in p['block_ids']]
    assert assigned == [b['block_id'] for b in blocks]
    assert len(assigned) == len(set(assigned))
    for lane in range(2):
        project.validate_lane(root, 'circuits', lane)


def test_staged_images_are_reused_without_reading_the_old_disk(book_project):
    root, catalog, _ = book_project
    source = __import__('pathlib').Path(catalog['source'])
    original = source/'books/circuits/image.png'
    original.write_bytes(b'preserved immutable image')
    record = dict(asset_id='image',asset_kind='original_raster_crop',block_id='p1-b0',
                  original_path='books/circuits/image.png',sha256=project.sha(original))
    (source/'books/circuits/assets.jsonl').write_text(json.dumps(record),encoding='utf-8')
    project.prepare(root,source,books=['circuits'],lanes=2,packets=3)
    project.stage(root,'circuits')
    original.unlink()
    result = project.stage(root,'circuits')
    assert result['images'] == 1
    assert result['missing_images'] == []


@pytest.mark.parametrize('fault', ['revision', 'omit', 'duplicate', 'foreign_relation', 'foreign_heading'])
def test_results_cannot_change_source_revision_or_ownership(book_project, fault):
    root, _, _ = book_project
    path = root/'decisions/circuits/lane-0.json'
    value = project.read(path)
    packet = value['packets'][0]
    if fault == 'revision':
        value['source_sha256'] = 'wrong'
    elif fault == 'omit':
        packet['order'] = ['p1-b0']
    elif fault == 'duplicate':
        value['packets'] = [packet, copy.deepcopy(packet)]
    elif fault == 'foreign_relation':
        packet['relations'] = [dict(source='p7-b0', target='p1-b0')]
    else:
        packet['headings'] = [dict(id='p7-b0', level=0)]
    project.write(path, value)
    with pytest.raises(ValueError):
        project.validate_lane(root, 'circuits', 0)


def test_render_preserves_all_text_and_source_doubts(book_project, monkeypatch):
    root, _, blocks = book_project
    from mewcode.bookflow import render
    def render_stub(root, book, ordered, headings, destination, concerns, **kwargs):
        assert ordered == blocks
        assert concerns == [dict(packet_id='circuits-000', detail='Source answer is unreadable')]
        destination.mkdir(parents=True)
        return dict(concerns=concerns)
    monkeypatch.setattr(render, 'render_book', render_stub)
    path = root/'decisions/circuits/lane-0.json'
    value = project.read(path)
    value['packets'][0].update(status='needs_review', issues=['Source answer is unreadable'])
    project.write(path, value)
    result = project.assemble(root, 'circuits', compile_pdf=False)
    assert result['coverage']['omitted_blocks'] == 0
    assert result['coverage']['semantic_review_complete'] is False
    (root/'inputs/circuits/records/blocks.jsonl').write_text('changed', encoding='utf-8')
    with pytest.raises(ValueError, match='Frozen source'):
        project.assemble(root, 'circuits', compile_pdf=False)


def test_tex_commands_are_escaped_or_preserved_as_source_images():
    assert not safe_math(r'\input{private}')
    assert not safe_math(r'\begin{document}x\end{document}')
    assert inline(r'Value $\input{private}$') is None
    assert inline('Cost & tax 5%') == r'Cost \& tax 5\%'
    assert inline(r'Voltage $\frac{a}{b}$') == r'Voltage \(\frac{a}{b}\)'


def test_page_review_requires_current_images_and_preserves_source_unknown(book_project, monkeypatch):
    from mewcode.bookflow import render
    from mewcode.bookflow.review import finish_review
    root, _, _ = book_project
    pdf = root/'outputs/circuits/main.pdf'
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b'current PDF')
    digest = project.sha(pdf)
    review = root/'reviews/circuits'
    project.write(review/'manifest.json',dict(pdf_sha256=digest,unmapped_body_blocks=[]))
    monkeypatch.setattr(render,'package_book',lambda _:None)
    for lane in range(2):
        page = dict(page=lane+1,image_sha256=f'image-{lane}')
        project.write(review/f'lane-{lane}.json',dict(pages=[page]))
        project.write(review/f'result-{lane}.json',dict(book='circuits',lane=lane,pdf_sha256=digest,
            pages=[dict(**page,status='source_unknown' if lane else 'generated_defect',issues=['located issue'])]))
    assert finish_review(root,'circuits')['ready'] is False
    path = review/'result-0.json'
    value = project.read(path)
    value['pages'][0]['status'] = 'passed'
    project.write(path,value)
    result = finish_review(root,'circuits')
    assert result['ready'] is True
    assert result['counts']['source_unknown'] == 1
    value['pages'][0]['image_sha256'] = 'stale'
    project.write(path,value)
    with pytest.raises(ValueError,match='another rendered image'):
        finish_review(root,'circuits')


@pytest.mark.asyncio
async def test_book_lanes_run_concurrently_then_join(tmp_path):
    graph = build(tmp_path, python='python', script='bookflow.py', books=['circuits'])
    arrived, all_arrived = set(), asyncio.Event()
    async def execute(ctx):
        if '/book_lane_' in ctx.node_id:
            assert ctx.inputs['item'] == 'circuits'
            arrived.add(ctx.node_id)
            if len(arrived) == 8:
                all_arrived.set()
            await asyncio.wait_for(all_arrived.wait(), 3)
        elif ctx.node_id.endswith('/book_build'):
            assert len(arrived) == 8
        elif ctx.node_id.endswith('/review_finish'):
            return dict(text='all pages checked', value=True)
        return dict(text='delivered')
    result = await Core(MemoryBackend(fresh(graph)), execute, concurrency=8).run()
    assert result['status'] == 'completed'
    assert len(arrived) == 8
    assert graph['execution_profile'] == 'bookflow.parallel.v1'
    assert 'limits' not in graph['nodes'][0]['config']


def test_paused_loop_is_a_checkpoint_instead_of_an_active_worker():
    from mewcode.workflow_core.store import synchronize
    graph = build('project', python='python', script='bookflow.py', books=['circuits'])
    state = fresh(graph)
    state.update(status='paused', nodes={'fast_books':dict(status='running',loop={'round':0})})
    record = dict(workflow_core=state)
    synchronize(record)
    assert record['active_node_ids'] == []
    assert state['nodes']['fast_books']['loop']['round'] == 0
    state['status'] = 'running'
    synchronize(record)
    assert record['active_node_ids'] == ['fast_books']


@pytest.mark.asyncio
async def test_scheduler_reconfigure_admits_queued_work_without_dropping_leases():
    scheduler = RuntimeScheduler(limit=2, kind_limits={'model': 1})
    first = await scheduler.acquire(kind='model')
    pending = asyncio.create_task(scheduler.acquire(kind='model'))
    await asyncio.sleep(0)
    assert not pending.done()
    await scheduler.configure(limit=3, kind_limits={'model': 2})
    second = await asyncio.wait_for(pending, 1)
    assert scheduler.snapshot()['running'] == 2
    await scheduler.configure(limit=1, kind_limits={'model': 1})
    third_task = asyncio.create_task(scheduler.acquire(kind='model'))
    await asyncio.sleep(0)
    await scheduler.release(first)
    assert not third_task.done()
    await scheduler.release(second)
    third = await asyncio.wait_for(third_task, 1)
    await scheduler.release(third)
    assert scheduler.snapshot()['running'] == 0
