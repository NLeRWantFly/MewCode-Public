from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "frontend" / "static"


def _render(text: str) -> str:
    script = r"""
const fs = require("fs");
const { createContext, runInContext } = require("vm");
const markedSrc = fs.readFileSync(process.argv[1], "utf8");
const katexSrc = fs.readFileSync(process.argv[2], "utf8");
const renderSrc = fs.readFileSync(process.argv[3], "utf8");
const text = fs.readFileSync(0, "utf8");
const ctx = createContext({ console, globalThis: {} });
ctx.globalThis = ctx;
ctx.window = ctx;
runInContext(markedSrc, ctx);
runInContext(katexSrc, ctx);
runInContext(renderSrc, ctx);
process.stdout.write(ctx.MewMarkdown.renderMarkdown(text));
"""
    result = subprocess.run(
        [
            "node",
            "-e",
            script,
            str(STATIC / "vendor" / "marked.min.js"),
            str(STATIC / "vendor" / "katex.min.js"),
            str(STATIC / "markdown-render.js"),
        ],
        input=text,
        capture_output=True,
        text=True,
        check=True,
        cwd=str(ROOT),
    )
    return result.stdout


def test_autolink_does_not_swallow_following_chinese_text() -> None:
    html = _render("见 https://example.com/path后续说明")
    assert "后续说明" in html
    assert "path%E5%90%8E" not in html
    assert ">https://example.com/path<" in html or 'href="https://example.com/path"' in html


def test_autolink_stops_before_fullwidth_punctuation() -> None:
    html = _render("见 https://example.com/path。后续说明")
    assert "path%E3%80%82" not in html
    assert "后续说明" in html


def test_concatenated_continue_text_is_not_inside_error_url() -> None:
    html = _render("错误 https://api.github.com/repos/foo/bar继续执行输出")
    assert "bar%E7%BB%A7" not in html
    assert "继续执行输出" in html
    start = html.find("<a ")
    end = html.find("</a>")
    assert start != -1 and end != -1
    assert "继续执行输出" not in html[start:end]


def test_markdown_render_exports_isolate_helper() -> None:
    source = (STATIC / "markdown-render.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "function isolateAutolinks(" in source
    assert "isolateAutolinks(" in source.split("function renderMarkdown(", 1)[1][:400]
    script_refs = re.findall(
        r'<script\b[^>]*\bsrc=["\']([^"\']*markdown-render\.js[^"\']*)["\']',
        html,
    )
    assert len(script_refs) == 1
    resource, separator, query = script_refs[0].partition("?")
    assert resource == "/static/markdown-render.js"
    assert separator and any(
        parameter.startswith("v=") and len(parameter) > 2
        for parameter in query.split("&")
    )
    assert (STATIC / "markdown-render.js").is_file()


def test_live_stream_finishes_with_math_render() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    finish = app.split("function finishStream(", 1)[1][:1200]
    assert "closeMarkdownSegment" in finish or "enhanceMath" in finish


def test_inline_and_display_latex_render_to_katex() -> None:
    html = _render("能量 $E=mc^2$\n\n$$\\frac{a}{b}$$\n")
    assert "katex" in html
    assert "<em>" not in html
    assert "$E=mc^2$" not in html
    assert "$$" not in html


def test_latex_underscores_are_not_markdown_emphasis() -> None:
    html = _render(r"预测 $\hat{y}_i$ 与真值")
    assert "katex" in html
    assert "<em>" not in html.lower()


def test_latex_environments_render() -> None:
    html = _render("\\begin{align}\na &= b \\\\\nc &= d\n\\end{align}")
    assert "katex" in html
    assert "<em>" not in html.lower()


def test_code_fence_keeps_literal_dollar() -> None:
    html = _render("```bash\necho $HOME\n```")
    assert "katex" not in html
    assert "$HOME" in html


def test_bracket_display_math_renders() -> None:
    html = _render("\\[\\sum_{i=1}^{n} x_i\\]")
    assert "katex" in html
    assert "<em>" not in html.lower()
