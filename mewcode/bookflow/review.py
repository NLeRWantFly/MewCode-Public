"""Prepare independent parallel page review without treating coverage as correctness."""
from pathlib import Path
import re

from .project import read, rows, sha, source_key, write
from .render import sha_of_id


def prepare_review(root, book, destination):
    import pymupdf as fitz
    root, destination = Path(root), Path(destination)
    catalog = read(root/'catalog.json')
    labels = {key: int(page) for key, page in re.findall(
        r'\\newlabel\{bf([^}]+)\}\{\{[^}]*\}\{(\d+)\}',
        (destination/'main.aux').read_text(encoding='utf-8', errors='replace'))}
    blocks = rows(root/'inputs'/book/'records/blocks.jsonl')
    source_map = read(destination/'source-map.json')
    included = {b['block_id'] for b in source_map}
    context = {b['block_id'] for b in source_map if b['classification'] == 'source_page_context'}
    blocks = [b for b in blocks if b['block_id'] in included]
    source_index = read(root/'inputs'/book/'asset-index.json')
    mapped = {}
    for block in blocks:
        output_page = labels.get(sha_of_id(block['block_id']))
        if output_page:
            mapped.setdefault(output_page, []).append(dict(id=source_key(block['block_id']),
                source_page=block['pdf_page'], type=block['type'], text=block.get('text')))
    review_root = root/'reviews'/book
    image_root = review_root/'pages'
    image_root.mkdir(parents=True, exist_ok=True)
    lanes = [[] for _ in range(catalog['lanes'])]
    pdf_sha = sha(destination/'main.pdf')
    with fitz.open(destination/'main.pdf') as document:
        count = len(document)
        for number, page in enumerate(document, 1):
            image = image_root/f'{number:04}.png'
            page.get_pixmap(dpi=140, alpha=False).save(image)
            source_pages = sorted({b['source_page'] for b in mapped.get(number, [])})
            lanes[(number-1) % len(lanes)].append(dict(page=number, image=str(image),
                image_sha256=sha(image), blocks=mapped.get(number, []),
                sources=[dict(page=p, **source_index['pages'][str(p)]) for p in source_pages]))
    for lane, pages in enumerate(lanes):
        write(review_root/f'lane-{lane}.json', dict(book=book, lane=lane, pdf_sha256=pdf_sha,
            pages=pages, output=str(review_root/f'result-{lane}.json'),
            schema=dict(book=book, lane=lane, pdf_sha256=pdf_sha, pages=[dict(page=p['page'],
                image_sha256=p['image_sha256'], status='passed/generated_defect/source_unknown/unobserved',
                issues=[]) for p in pages])), compact=True)
    receipt = dict(pdf_sha256=pdf_sha, pages=count, lanes=len(lanes),
        unmapped_body_blocks=[b['block_id'] for b in blocks if b['block_id'] not in context
                              and sha_of_id(b['block_id']) not in labels])
    write(review_root/'manifest.json', receipt)
    return receipt


def finish_review(root, book):
    root = Path(root)
    catalog = read(root/'catalog.json')
    review_root = root/'reviews'/book
    manifest = read(review_root/'manifest.json')
    pdf = root/'outputs'/book/'main.pdf'
    if sha(pdf) != manifest['pdf_sha256']:
        raise ValueError('PDF revision changed; regenerate review inputs')
    combined = []
    for lane in range(catalog['lanes']):
        assigned = read(review_root/f'lane-{lane}.json')
        result = read(review_root/f'result-{lane}.json')
        if (result.get('book'),result.get('lane'),result.get('pdf_sha256')) != (book,lane,manifest['pdf_sha256']):
            raise ValueError('Page review belongs to another PDF revision')
        expected = {p['page']: p['image_sha256'] for p in assigned['pages']}
        pages = result.get('pages') or []
        if (len(pages) != len(expected) or len({p['page'] for p in pages}) != len(pages)
                or {p['page'] for p in pages} != set(expected)):
            raise ValueError('Each assigned page needs exactly one recorded disposition')
        for page in pages:
            if page.get('image_sha256') != expected[page['page']]:
                raise ValueError('Page review is bound to another rendered image')
            if page.get('status') not in {'passed','generated_defect','source_unknown','unobserved'}:
                raise ValueError('Page status must distinguish actual evidence and unresolved defects')
            if not isinstance(page.get('issues'), list):
                raise ValueError('Page issues must be retained')
        combined.extend(pages)
    summary = dict(book=book, pdf_sha256=manifest['pdf_sha256'], pages=sorted(combined,key=lambda p:p['page']),
        counts={status:sum(p['status']==status for p in combined)
                for status in ('passed','generated_defect','source_unknown','unobserved')},
        unmapped_body_blocks=manifest['unmapped_body_blocks'])
    summary['ready'] = (not summary['counts']['generated_defect'] and not summary['counts']['unobserved']
                        and not summary['unmapped_body_blocks'])
    write(root/'outputs'/book/'page-review.json', summary)
    from .render import package_book
    package_book(root/'outputs'/book)
    return summary
