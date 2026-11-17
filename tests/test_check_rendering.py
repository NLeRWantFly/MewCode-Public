import json

from PIL import Image
import pytest

from mewcode.tools.check_rendering import CheckRendering, CheckRenderingParams, check_markdown_rendering


def page_dir(root):
    path = root / "stage5_candidates" / "handbook" / "page_0015"
    path.mkdir(parents=True)
    return path


@pytest.mark.asyncio
async def test_valid_bitmap_link_passes_with_stable_identity(tmp_path):
    page = page_dir(tmp_path)
    Image.new("RGB", (16, 12), "navy").save(page / "figure.png")
    (page / "candidate.md").write_text("正文\n\n![修订图](figure.png)\n", encoding="utf-8")
    tool = CheckRendering(tmp_path)
    params = CheckRenderingParams(markdown_path="stage5_candidates/handbook/page_0015/candidate.md")

    first = await tool.execute(params)
    second = await tool.execute(params)
    assert not first.is_error, first.output
    assert first.output == second.output
    result = json.loads(first.output)
    assert result["document"] == "handbook"
    assert result["page"] == 15
    assert result["images"][0]["format"] == "PNG"
    assert result["identity"]["object"] == "handbook/page_0015"


@pytest.mark.asyncio
async def test_bracket_only_placeholder_fails_with_document_page_and_line(tmp_path):
    page = page_dir(tmp_path)
    Image.new("RGB", (5, 5), "white").save(page / "figure.png")
    (page / "candidate.md").write_text("标题\n\n[此处替换为原图]\n\n![实际图](figure.png)\n", encoding="utf-8")
    result = await CheckRendering(tmp_path).execute(CheckRenderingParams(
        markdown_path="stage5_candidates/handbook/page_0015/candidate.md"))
    assert result.is_error
    error = json.loads(result.output)["errors"][0]
    assert (error["document"], error["page"], error["line"]) == ("handbook", 15, 3)
    assert error["code"] == "bracket_placeholder"


@pytest.mark.asyncio
async def test_missing_corrupt_and_escaping_image_links_fail_closed(tmp_path):
    page = page_dir(tmp_path)
    (page / "broken.png").write_bytes(b"not a bitmap")
    (page / "candidate.md").write_text(
        "![missing](missing.png)\n![corrupt](broken.png)\n![escape](../../../../outside.png)\n",
        encoding="utf-8")
    result = await CheckRendering(tmp_path).execute(CheckRenderingParams(
        markdown_path="stage5_candidates/handbook/page_0015/candidate.md"))
    assert result.is_error
    errors = json.loads(result.output)["errors"]
    assert len(errors) == 3
    assert [error["line"] for error in errors] == [1, 2, 3]
    assert all(error["document"] == "handbook" and error["page"] == 15 for error in errors)


def test_absolute_and_remote_image_links_are_rejected(tmp_path):
    page = page_dir(tmp_path)
    (page / "candidate.md").write_text(
        "![remote](https://example.com/image.png)\n![absolute](/outside.png)\n", encoding="utf-8")
    result = check_markdown_rendering(tmp_path,
        "stage5_candidates/handbook/page_0015/candidate.md")
    assert result["status"] == "FAIL"
    assert [item["code"] for item in result["errors"]] == ["image_unrenderable", "image_unrenderable"]


@pytest.mark.asyncio
async def test_markdown_path_cannot_escape_workspace(tmp_path):
    outside = tmp_path.parent / "outside.md"
    outside.write_text("![x](x.png)", encoding="utf-8")
    result = await CheckRendering(tmp_path).execute(CheckRenderingParams(markdown_path="../outside.md"))
    assert result.is_error
    payload = json.loads(result.output)
    assert payload["errors"][0]["code"] == "input_invalid"
