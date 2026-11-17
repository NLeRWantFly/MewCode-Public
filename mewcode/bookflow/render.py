"""Deterministic, source-preserving TeX assembly; models never regenerate body text."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import zipfile

from .project import BOOKS, freeze_file, read, rows, sha, write

ESCAPE = {'\\': r'\textbackslash{}', '{': r'\{', '}': r'\}', '$': r'\$', '&': r'\&',
          '#': r'\#', '%': r'\%', '_': r'\_', '~': r'\textasciitilde{}', '^': r'\textasciicircum{}'}
MATH_COMMANDS = set(('frac dfrac tfrac sqrt text mathrm mathbf mathit mathcal mathbb operatorname '
    'overline underline vec hat bar dot ddot tilde widehat widetilde left right big Big bigg Bigg '
    'begin end displaystyle textstyle scriptstyle limits nolimits sum prod int iint iiint oint '
    'lim max min sin cos tan cot sec csc arcsin arccos arctan sinh cosh tanh log ln exp det '
    'alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa lambda mu nu '
    'xi pi varpi rho varrho sigma varsigma tau upsilon phi varphi chi psi omega Gamma Delta '
    'Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega cdot times div pm mp le leq ge geq ne neq '
    'approx equiv sim simeq propto in notin subset supset subseteq supseteq cup cap emptyset '
    'infty partial nabla forall exists neg land lor to rightarrow leftarrow leftrightarrow '
    'Rightarrow Leftarrow Leftrightarrow longrightarrow therefore because quad qquad hspace '
    'ldots cdots vdots ddots circ angle triangle parallel perp bot top ell hbar Re Im '
    'langle rangle lvert rvert vert Vert lVert rVert braces underbrace overbrace').split())
MATH_ENVS = {'aligned', 'array', 'cases', 'matrix', 'pmatrix', 'bmatrix', 'vmatrix', 'Vmatrix', 'gathered'}


def escape(text):
    return ''.join(ESCAPE.get(char, char) for char in str(text) if char >= ' ' or char in '\n\t')


def safe_math(text):
    if any(command not in MATH_COMMANDS for command in re.findall(r'\\([A-Za-z]+)', text)):
        return False
    if any(env not in MATH_ENVS for env in re.findall(r'\\(?:begin|end)\{([^}]+)\}', text)):
        return False
    if '&' in text and not re.search(r'\\begin\{(?:array|aligned|cases|.*matrix)\}', text):
        return False
    depth = 0
    for index, char in enumerate(text):
        if index and text[index-1] == '\\':
            continue
        depth += (char == '{') - (char == '}')
        if depth < 0:
            return False
    return depth == 0 and '%' not in text and '$' not in text


def inline(text):
    pieces, cursor = [], 0
    for match in re.finditer(r'(?<!\\)\$([^$]+)\$', text):
        if not safe_math(match[1]):
            return None
        pieces.extend([escape(text[cursor:match.start()]), r'\(' + match[1] + r'\)'])
        cursor = match.end()
    pieces.append(escape(text[cursor:]))
    return ''.join(pieces)


def render_book(root, book, blocks, headings, destination, concerns, *, source_context=()):
    from PIL import Image
    root, destination = Path(root), Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    catalog = read(root/'catalog.json')
    frozen = root/'inputs'/book
    index = read(frozen/'asset-index.json')
    assets, pages = index['assets'], index['pages']
    concerns = list(concerns) + [dict(kind='missing_source_asset', **entry) for entry in index['missing']]
    provenance = destination/'provenance'
    shutil.copytree(frozen/'records', provenance/'records', dirs_exist_ok=True)
    template = Path(catalog['template'])
    cls = next(template.rglob('elegantbook.cls'), None)
    if cls is None:
        raise ValueError('The pinned ElegantBook class is unavailable')
    freeze_file(cls, destination/'elegantbook.cls')
    license_path = next(template.rglob('License'), None)
    if license_path:
        freeze_file(license_path, destination/'License')
    dependency = next((parent/'environment/texmf' for parent in [template.parent, template.parent.parent]
                       if (parent/'environment/texmf').is_dir()), None)
    if dependency:
        shutil.copytree(dependency, destination/'texmf', dirs_exist_ok=True)
    source_map, rendered = [], []
    source_context = set(source_context)

    def picture(block, *, crop=False):
        path = None
        candidates = [assets[key] for key in block.get('asset_ids', []) if key in assets and assets[key]['path']]
        candidates.sort(key=lambda value: ('original' not in str(value.get('kind')), str(value.get('kind'))))
        if candidates and not crop:
            original = Path(candidates[0]['path'])
            target = destination/'assets'/original.name
            freeze_file(original, target)
            path = target.relative_to(destination).as_posix()
        else:
            page = pages.get(str(block['pdf_page'])) or {}
            bbox = block.get('bbox')
            if page.get('path') and isinstance(bbox, list) and len(bbox) == 4:
                target = destination/'assets'/('crop-' + sha_of_id(block['block_id']) + '.png')
                target.parent.mkdir(exist_ok=True)
                with Image.open(page['path']) as source:
                    frame = block.get('oxr_page_size') or {}
                    scale_x = source.width / (frame.get('w') or source.width)
                    scale_y = source.height / (frame.get('h') or source.height)
                    rectangle = [max(0, round(bbox[0]*scale_x)), max(0, round(bbox[1]*scale_y)),
                                 min(source.width, round(bbox[2]*scale_x)), min(source.height, round(bbox[3]*scale_y))]
                    if rectangle[2] > rectangle[0] and rectangle[3] > rectangle[1]:
                        source.crop(rectangle).convert('RGB').save(target)
                        path = target.relative_to(destination).as_posix()
        if not path:
            return None
        height = '3.2cm' if block['type'] == 'Formula' else r'.55\textheight'
        return (r'\par\begingroup\centering\includegraphics[width=.82\linewidth,height='
                + height + r',keepaspectratio]{' + path + r'}\par\endgroup')

    for block in blocks:
        identity, kind = block['block_id'], block['type']
        text = str(block.get('corrected_text') or block.get('text') or block.get('latex') or '')
        text = re.sub(r'!\[[^\]]*\]\([^\n)]+\)', '', text).strip()
        classification = 'body'
        output = ''
        if kind in {'Page-header', 'Page-footer'} or identity in source_context:
            classification = 'source_page_context'
        elif kind == 'Title' or identity in headings:
            level = headings.get(identity)
            if level is None:
                level = (0 if re.match(r'^第[一二三四五六七八九十百零\d]+章', text) else
                         2 if re.match(r'^\d+\.\d+\.\d+', text) else 1 if re.match(r'^\d+\.\d+', text) else 3)
            command = ('chapter', 'section', 'subsection', 'subsubsection')[level]
            output = '\\' + command + '*{' + escape(text) + '}\n'
            if level < 3:
                output += r'\addcontentsline{toc}{' + command + '}{' + escape(text) + '}'
            if level == 0:
                output += r'\markboth{' + escape(text) + '}{' + escape(text) + '}'
            elif level == 1:
                output += r'\markright{' + escape(text) + '}'
        elif kind in {'Picture', 'Table', 'Formula'}:
            output = picture(block)
            if output is None:
                output = escape(text or '[原始图像缺失，待复核]') + r'\par'
                concerns.append(dict(block_id=identity, kind='source_image_missing'))
        else:
            output = inline(text)
            if output is None:
                output = picture(block, crop=True) or escape(text)
                concerns.append(dict(block_id=identity, kind='math_preserved_as_source_image'))
            output += '\n\\par'
        rendered.append('% source-block: ' + identity + '\n' + output)
        if output:
            rendered.append(r'\label{bf' + sha_of_id(identity) + '}')
        source_map.append(dict(block_id=identity, pdf_page=block['pdf_page'], original_text=block.get('text'),
                               classification=classification, source_refs=block.get('source_refs') or []))
    write(destination/'source-map.json', source_map)
    write(destination/'issues.json', concerns)
    main = (r'\documentclass[lang=cn,11pt,math=cm,scheme=chinese]{elegantbook}' + '\n'
            r'\hypersetup{pdftitle={' + escape(BOOKS[book]) + '}}\n'
            r'\setlength{\emergencystretch}{3em}' + '\n'
            r'\begin{document}' + '\n' + r'\tableofcontents\clearpage' + '\n' + '\n\n'.join(rendered)
            + '\n' + r'\end{document}' + '\n')
    (destination/'main.tex').write_text(main, encoding='utf-8')
    (destination/'README.md').write_text('# ' + BOOKS[book] + '\n\n'
        'main.tex 为可编辑书稿，原文、题目和答案来源记录保存在 provenance/records。'
        'source-map.json 逐块列明正文与页眉页脚的去向，issues.json 保留尚待核实事项。'
        '编译成功和源块覆盖完整不代表所有原始内容已经通过独立语义复核。\n\n'
        '使用 XeLaTeX 编译，关闭 shell escape；如有 texmf 目录，将 TEXMFHOME 指向该目录。\n', encoding='utf-8')
    return dict(concerns=concerns, blocks=len(blocks), source_map=str(destination/'source-map.json'))


def sha_of_id(value):
    import hashlib
    return hashlib.sha256(value.encode()).hexdigest()[:20]


def compile_book(root, book, destination):
    destination = Path(destination)
    compiler = Path('C:/Users/example-user/texlive/bin/windows/xelatex.exe')
    if not compiler.is_file():
        raise ValueError('The configured XeLaTeX engine is unavailable')
    env = dict(os.environ)
    env['PATH'] = str(compiler.parent) + os.pathsep + env.get('PATH', '')
    if (destination/'texmf').is_dir():
        env['TEXMFHOME'] = str(destination/'texmf')
    env['TEXMFVAR'] = str(destination/'build-cache/var')
    env['TEXMFCONFIG'] = str(destination/'build-cache/config')
    commands = []
    for number in (1, 2):
        command = [str(compiler), '-no-shell-escape', '-interaction=nonstopmode', '-halt-on-error',
                   '-file-line-error', 'main.tex']
        process = subprocess.run(command, cwd=destination, env=env, capture_output=True)
        (destination/f'compile-{number}.log').write_bytes(process.stdout + process.stderr)
        commands.append(dict(returncode=process.returncode, shell_escape=False))
        if process.returncode:
            write(destination/'compile-result.json', dict(success=False,commands=commands))
            raise ValueError('XeLaTeX failed; inspect compile-' + str(number) + '.log without regenerating completed packets')
    pdf = destination/'main.pdf'
    receipt = dict(success=pdf.is_file(), commands=commands, pdf=str(pdf), sha256=sha(pdf))
    write(destination/'compile-result.json', receipt)
    from .review import prepare_review
    receipt['review'] = prepare_review(root, book, destination)
    return receipt


def package_book(destination):
    destination = Path(destination)
    archive = destination.parent/(destination.name + '-LaTeX.zip')
    excluded = {'.aux', '.log', '.toc', '.out', '.xdv', '.fls', '.fdb_latexmk'}
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for path in destination.rglob('*'):
            if path.is_file() and 'build-cache' not in path.parts and path.suffix not in excluded:
                bundle.write(path, path.relative_to(destination))
    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip() is not None:
            raise OSError('LaTeX package verification failed')
    return str(archive)
