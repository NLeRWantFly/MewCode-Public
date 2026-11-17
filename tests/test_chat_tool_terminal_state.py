import json
from pathlib import Path
import subprocess


def test_finish_stream_settles_pending_tool_rows() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "frontend" / "static" / "app.js"
    ).read_text(encoding="utf-8")

    assert "function settlePendingToolRows(msg = {})" in source
    assert 'const label = interrupted ? "已中断" : failed ? "失败" : "结果未知";' in source
    assert "for (const toolId of pendingTurnToolIds)" in source
    finish_stream = source.split("function finishStream(msg) {", 1)[1].split(
        "// ---------- 输入与发送 ----------", 1
    )[0]
    assert "settlePendingToolRows(msg);" in finish_stream
    assert "pendingTurnToolIds.clear();" in source


def test_interrupted_tool_row_is_settled_by_actual_javascript() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["node", str(root / "tests" / "chat_tool_terminal_dom.cjs")],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report == {
        "done": True,
        "label": "已中断",
        "groupRunning": False,
        "groupStatus": "已中断",
        "hasExplanation": True,
        "pendingCount": 0,
    }


def test_tool_arguments_and_results_render_without_object_coercion() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["node", str(root / "tests" / "chat_tool_renderer_dom.cjs")],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)

    assert report["emptyObject"] == "{}"
    assert report["emptyArray"] == "[]"
    assert report["jsonString"] == '{"raw": true}'
    assert report["nullValue"] == ""
    assert report["undefinedValue"] == ""
    assert report["falseValue"] == "false"
    assert report["zeroValue"] == "0"
    assert report["arrayValue"] == '[\n  1,\n  {\n    "ok": true\n  }\n]'
    assert report["legacyObjectResult"] == '{}\n\n→ {\n  "count": 2\n}'

    assert "&lt;img src=x onerror=alert(1)&gt;" in report["escapedObject"]
    assert "<img" not in report["escapedObject"]
    assert report["namedSkill"] == "LoadSkill computer-use"
    assert report["emptySkill"] == "LoadSkill"

    assert report["liveSummary"] == "LoadSkill computer-use"
    assert report["liveBeforeResult"] == '{\n  "name": "computer-use"\n}'
    assert report["liveObjectResult"].endswith(
        '\n\n→ {\n  "loaded": true,\n  "html": "<script>bad()</script>"\n}'
    )
    assert "&lt;script&gt;" in report["liveObjectResultHTML"]
    assert "<script>" not in report["liveObjectResultHTML"]
    assert report["liveDone"] is True
    assert report["liveGroupStatus"] == "完成"
    assert report["liveFalseResult"] == "\n\n→ false"
    assert report["liveZeroResult"] == "\n\n→ 0"
    assert report["pendingCount"] == 0
