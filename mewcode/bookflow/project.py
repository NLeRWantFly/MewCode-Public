from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil

BOOKS = {'circuits': '电路', 'machines': '电机学', 'accounting': '会计', 'mathematics': '数学'}
RECORDS = ('book.json', 'blocks.jsonl', 'assets.jsonl', 'pages.jsonl', 'questions.jsonl',
           'knowledge.jsonl', 'headings.jsonl', 'fulltext.md')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def rows(path):
    return [json.loads(line) for line in Path(path).read_text(encoding='utf-8-sig').splitlines() if line.strip()]


def write(path, value, *, compact=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.pending')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=None if compact else 2), encoding='utf-8')
    os.replace(temporary, path)


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def source_key(identity):
    return ':'.join(str(identity).split(':')[-2:])


def contained(root, relative):
    root = Path(root).resolve()
    result = (root / relative).resolve()
    if not result.is_relative_to(root):
        raise ValueError('Path escapes the book project')
    return result


def freeze_file(source, target, expected=None):
    source, target = Path(source), Path(target)
    digest = sha(source)
    if expected and expected != digest:
        raise ValueError(f'Source fingerprint changed: {source}')
    if not target.is_file() or sha(target) != digest:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + '.pending')
        shutil.copy2(source, temporary)
        if sha(temporary) != digest:
            raise OSError('Copy verification failed')
        os.replace(temporary, target)
    return dict(path=str(target), sha256=digest, size=target.stat().st_size)


def prepare(root, source, *, books=tuple(BOOKS), lanes=8, packets=32, template=None):
    root, source = Path(root).resolve(), Path(source).resolve()
    if not 1 <= lanes <= 16 or not 1 <= packets <= 128:
        raise ValueError('Invalid lane or packet count')
    catalog = dict(schema='mewcode.fastbook.v1', root=str(root), source=str(source), lanes=lanes,
                   packet_target=packets, template=str(template or ''), books={})
    for book in books:
        if book not in BOOKS:
            raise ValueError('Unknown source book')
        directory = root/'inputs'/book
        fingerprints = {}
        supplemental = sorted(path.name for path in (source/'books'/book).iterdir()
                              if path.is_file() and path.suffix in {'.json', '.jsonl', '.md'})
        for name in dict.fromkeys((*RECORDS, *supplemental)):
            candidate = source/'books'/book/name
            if not candidate.is_file():
                if name == 'headings.jsonl':
                    continue
                raise ValueError(f'Missing whole-book input: {candidate}')
            fingerprints[name] = freeze_file(candidate, directory/'records'/name)
        blocks = sorted(rows(directory/'records/blocks.jsonl'), key=lambda b: (b['pdf_page'], b['order']))
        ids = [block['block_id'] for block in blocks]
        if len(ids) != len(set(ids)):
            raise ValueError('Source block identities are not unique')
        pages = sorted({block['pdf_page'] for block in blocks})
        width = max(1, math.ceil(len(pages)/packets))
        tasks = []
        for offset in range(0, len(pages), width):
            selected = pages[offset:offset+width]
            owned = [block['block_id'] for block in blocks if block['pdf_page'] in selected]
            tasks.append(dict(id=f'{book}-{len(tasks):03}', lane=len(tasks) % lanes,
                              pages=selected, block_ids=owned))
        book_meta = dict(title=BOOKS[book], pages=len(pages), blocks=len(blocks), fingerprints=fingerprints,
                         tasks=tasks, source_sha256=fingerprints['blocks.jsonl']['sha256'])
        catalog['books'][book] = book_meta
    write(root/'catalog.json', catalog)
    return catalog


def stage(root, book):
    root = Path(root).resolve()
    catalog = read(root/'catalog.json')
    meta = catalog['books'][book]
    directory = root/'inputs'/book
    for name, record in meta['fingerprints'].items():
        if sha(directory/'records'/name) != record['sha256']:
            raise ValueError('Frozen source record changed')
    source = Path(catalog['source']).resolve()
    assets, missing, source_files = {}, [], {}
    def asset(relative, expected):
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or '..' in Path(relative).parts:
            return None
        if expected:
            cached = directory/'assets'/(expected + Path(relative).suffix.lower())
            if cached.is_file() and sha(cached) == expected:
                source_files[expected] = dict(path=str(cached),sha256=expected,size=cached.stat().st_size)
                return str(cached)
        original = contained(source, relative)
        if not original.is_file():
            missing.append(dict(path=relative, reason='source_file_missing'))
            return None
        digest = sha(original)
        if expected and digest != expected:
            raise ValueError(f'Image fingerprint changed: {relative}')
        target = directory/'assets'/(digest + original.suffix.lower())
        if digest not in source_files:
            source_files[digest] = freeze_file(original, target, expected=expected)
        return str(target)
    for row in rows(directory/'records/assets.jsonl'):
        assets[row['asset_id']] = dict(path=asset(row.get('original_path'), row.get('sha256')),
            kind=row.get('asset_kind'), block_id=row.get('block_id'), sha256=row.get('sha256'))
    pages = {}
    for row in rows(directory/'records/pages.jsonl'):
        pages[row['pdf_page']] = dict(printed_page=row.get('printed_page'),
            path=asset(row.get('page_image'), row.get('page_image_sha256')),
            width=row.get('width_px'), height=row.get('height_px'))
    write(directory/'asset-index.json', dict(assets=assets, pages=pages, missing=missing))
    blocks = rows(directory/'records/blocks.jsonl')
    questions = rows(directory/'records/questions.jsonl')
    by_id = {row['block_id']: row for row in blocks}
    for lane in range(catalog['lanes']):
        jobs = []
        for task in meta['tasks']:
            if task['lane'] != lane:
                continue
            selected = set(task['block_ids'])
            context_pages = {task['pages'][0]-1, task['pages'][-1]+1}
            context = [dict(block_id=source_key(b['block_id']), text=b.get('text'), type=b['type'])
                       for b in blocks if b['pdf_page'] in context_pages]
            data = []
            for identity in task['block_ids']:
                b = by_id[identity]
                data.append(dict(id=source_key(identity), page=b['pdf_page'], type=b['type'], text=b.get('text'),
                    latex=b.get('latex'), heading_path=b.get('heading_path'),
                    images=[assets[a] for a in b.get('asset_ids', []) if a in assets]))
            question_rows = []
            for q in questions:
                refs = {key: [source_key(ref['block_id']) for ref in q.get(key, [])
                              if isinstance(ref, dict) and ref.get('block_id') in by_id]
                        for key in ('source_refs', 'book_answer_source_refs')}
                if not any(ref.get('block_id') in selected for key in refs
                           for ref in q.get(key, []) if isinstance(ref, dict)):
                    continue
                question_rows.append(dict(id=q.get('question_id'), status=q.get('status'),
                    issues=q.get('issues') or q.get('flags') or [], **refs))
            jobs.append(dict(id=task['id'], pages=task['pages'], source_sha256=meta['source_sha256'],
                blocks=data, read_only_neighbor_context=context, questions=question_rows,
                page_images={str(p): pages.get(p) for p in task['pages']}))
        write(root/'lanes'/book/f'lane-{lane}.json', dict(book=book, lane=lane,
            source_sha256=meta['source_sha256'], packets=jobs,
            output=str(root/'decisions'/book/f'lane-{lane}.json'),
            result_schema=dict(book=book, lane=lane, source_sha256=meta['source_sha256'],
                packets=[dict(id=job['id'], status='completed or needs_review', headings=[],
                              relations=[], source_context=[], issues=[]) for job in jobs]),
            instructions='逐包处理所有正文，保持原始源块和题答来源。只输出层级、关联和疑点；'
                         '无需抄写正文或完整block_id顺序。未给order时程序沿用原顺序。'
                         '块ID采用源内简写，验证器按只读源记录还原完整身份；不得编造ID。'
                         'needs_review代表该包已处理且仍有疑点，可以正常交付，不要求补齐缺损才让其他包推进。'), compact=True)
    receipt = dict(book=book, pages=meta['pages'], blocks=meta['blocks'], packets=len(meta['tasks']),
                   lanes=catalog['lanes'], images=len(source_files), missing_images=missing)
    write(directory/'stage-result.json', receipt)
    return receipt


def validate_lane(root, book, lane):
    root = Path(root).resolve()
    catalog = read(root/'catalog.json')
    meta = catalog['books'][book]
    if not 0 <= lane < catalog['lanes']:
        raise ValueError('Unknown lane')
    value = read(root/'decisions'/book/f'lane-{lane}.json')
    if (value.get('book'), value.get('lane'), value.get('source_sha256')) != (book, lane, meta['source_sha256']):
        raise ValueError('The lane result is bound to another source revision')
    tasks = {task['id']: task for task in meta['tasks'] if task['lane'] == lane}
    results = value.get('packets')
    if (not isinstance(results, list) or any(not isinstance(r, dict) for r in results)
            or len(results) != len(tasks) or {r.get('id') for r in results} != set(tasks)):
        raise ValueError('Every assigned packet must have exactly one result')
    known = {identity for task in meta['tasks'] for identity in task['block_ids']}
    aliases = {source_key(identity): identity for identity in known}
    if len(aliases) != len(known):
        raise ValueError('Source shorthand identities are ambiguous')
    for result in results:
        task = tasks[result['id']]
        owned = set(task['block_ids'])
        if result.get('status') not in {'completed', 'needs_review'}:
            raise ValueError('Packet status must distinguish processed content and remaining concerns')
        if isinstance(result.get('order'), list):
            result['order'] = [aliases.get(identity, identity) for identity in result['order']]
        order = result.get('order', task['block_ids'])
        if not isinstance(order, list) or len(order) != len(owned) or set(order) != owned:
            raise ValueError('A packet cannot omit, duplicate or import source blocks')
        headings = result.get('headings', [])
        if (not isinstance(headings, list) or any(not isinstance(h, dict) for h in headings)
                or len({h.get('id') for h in headings}) != len(headings)):
            raise ValueError('Heading identities must be unique')
        for heading in headings:
            heading['id'] = aliases.get(heading.get('id'), heading.get('id'))
            if (heading.get('id') not in owned or type(heading.get('level')) is not int
                    or not 0 <= heading['level'] <= 3):
                raise ValueError('Heading must identify an owned source block and level 0-3')
        for relation in result.get('relations', []):
            for key in ('source', 'target'):
                relation[key] = aliases.get(relation.get(key), relation.get(key))
            if relation.get('source') not in owned or relation.get('target') not in known:
                raise ValueError('Relationship must retain exact source identities')
        if not isinstance(result.get('issues', []), list):
            raise ValueError('Concerns must remain a list')
        contexts = result.get('source_context', [])
        if not isinstance(contexts, list):
            raise ValueError('Source context must identify owned blocks')
        contexts = [aliases.get(identity, identity) for identity in contexts]
        if len(contexts) != len(set(contexts)) or not set(contexts) <= owned:
            raise ValueError('Source context must identify owned blocks')
        result['source_context'] = contexts
    return value


def assemble(root, book, *, compile_pdf=True):
    from .render import render_book, compile_book, package_book
    root = Path(root).resolve()
    catalog = read(root/'catalog.json')
    meta = catalog['books'][book]
    for name, record in meta['fingerprints'].items():
        if sha(root/'inputs'/book/'records'/name) != record['sha256']:
            raise ValueError('Frozen source record changed')
    decisions = {}
    for lane in range(catalog['lanes']):
        for result in validate_lane(root, book, lane)['packets']:
            decisions[result['id']] = result
    blocks = rows(root/'inputs'/book/'records/blocks.jsonl')
    by_id = {block['block_id']: block for block in blocks}
    ordered, headings, concerns, source_context = [], {}, [], []
    for task in meta['tasks']:
        result = decisions[task['id']]
        ordered.extend(by_id[identity] for identity in result.get('order', task['block_ids']))
        headings.update({h['id']: h['level'] for h in result.get('headings', [])})
        source_context.extend(result.get('source_context', []))
        concerns.extend(dict(packet_id=task['id'], detail=issue) for issue in result.get('issues', []))
        if result['status'] == 'needs_review' and not result.get('issues'):
            concerns.append(dict(packet_id=task['id'], detail='此包仍需复核；没有声明内容全部正确。'))
    if len(ordered) != len(blocks) or {b['block_id'] for b in ordered} != set(by_id):
        raise ValueError('Whole-book source coverage changed')
    destination = root/'outputs'/book
    info = render_book(root, book, ordered, headings, destination, concerns, source_context=source_context)
    write(destination/'packet-decisions.json', decisions)
    write(destination/'relationships.json', [relation for result in decisions.values()
                                           for relation in result.get('relations', [])])
    coverage = dict(book=book, source_sha256=meta['source_sha256'], source_blocks=len(blocks),
        mapped_blocks=len(ordered), omitted_blocks=0, duplicated_blocks=0, packets=len(decisions),
        source_files_preserved=True, semantic_review_complete=False, concerns=info['concerns'])
    write(destination/'coverage.json', coverage)
    result = dict(book=book, coverage=coverage, directory=str(destination))
    if compile_pdf:
        result['build'] = compile_book(root, book, destination)
        result['zip'] = package_book(destination)
    write(destination/'result.json', result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=('prepare', 'stage', 'validate', 'assemble', 'finish-review'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--book', choices=BOOKS)
    parser.add_argument('--lane', type=int)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--template', type=Path)
    args = parser.parse_args()
    if args.operation == 'prepare':
        value = prepare(args.root, args.source, template=args.template)
        value = dict(books={key:dict(pages=b['pages'],blocks=b['blocks'],packets=len(b['tasks']))
                           for key,b in value['books'].items()},root=str(args.root))
    elif args.operation == 'stage':
        value = stage(args.root, args.book)
    elif args.operation == 'validate':
        value = validate_lane(args.root, args.book, args.lane)
        value = dict(valid=True,book=args.book,lane=args.lane,packets=len(value['packets']))
    elif args.operation == 'finish-review':
        from .review import finish_review
        value = finish_review(args.root,args.book)
        value = {key:value[key] for key in ('book','ready','counts','unmapped_body_blocks')}
    else:
        value = assemble(args.root,args.book)
        value = dict(book=args.book,directory=value['directory'],pdf=value['build']['pdf'],zip=value['zip'],
            source_blocks=value['coverage']['source_blocks'],mapped_blocks=value['coverage']['mapped_blocks'],
            concerns=len(value['coverage']['concerns']),
            reports=['coverage.json','issues.json','source-map.json','packet-decisions.json'])
    print(json.dumps(value,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
