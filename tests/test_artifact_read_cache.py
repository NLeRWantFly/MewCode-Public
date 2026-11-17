from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from mewcode.tools.read_audit_artifact import ReadAuditArtifact, ReadAuditArtifactParams


def write_pdf(path, *, text="Page one", second_text=None, padding=0):
    """A valid PDF with an unreferenced, optionally sparse large stream."""
    offsets = [0]
    with path.open("wb") as stream:
        stream.write(b"%PDF-1.4\n")
        kids = b"[3 0 R] /Count 1" if second_text is None else b"[3 0 R 6 0 R] /Count 2"
        objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
                   b"<< /Type /Pages /Kids " + kids + b" >>",
                   b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 180 240] "
                   b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"]
        content = f"BT /F1 12 Tf 20 40 Td ({text}) Tj ET".encode("ascii")
        objects += [b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
                    b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
        if second_text is not None:
            content = f"BT /F1 12 Tf 20 40 Td ({second_text}) Tj ET".encode("ascii")
            objects += [b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 180 240] "
                        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 7 0 R >>",
                        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream"]
        for index, value in enumerate(objects, 1):
            offsets.append(stream.tell())
            stream.write(f"{index} 0 obj\n".encode() + value + b"\nendobj\n")
        if padding:
            offsets.append(stream.tell())
            stream.write(f"{len(offsets) - 1} 0 obj\n<< /Length {padding} >>\nstream\n".encode())
            stream.seek(padding, 1)
            stream.write(b"\nendstream\nendobj\n")
        xref = stream.tell()
        stream.write(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
        for offset in offsets[1:]:
            stream.write(f"{offset:010d} 00000 n \n".encode())
        stream.write(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())


def io_usage(result):
    return result.host_observation["artifact_io"]


@pytest.mark.asyncio
@pytest.mark.parametrize("visual", [False, True])
async def test_pdf_over_64_mib_is_read_by_page_and_reuses_cached_result(tmp_path, visual):
    pdf = tmp_path / "large.pdf"
    write_pdf(pdf, padding=64 * 1024 * 1024)
    assert pdf.stat().st_size > 64 * 1024 * 1024
    tool = ReadAuditArtifact([str(tmp_path)])
    first = await tool.execute(ReadAuditArtifactParams(file_path=str(pdf), render_page=visual, criterion_id="first"))
    assert not first.is_error, first.output
    assert "PAGE: 1/1" in first.output
    second = await tool.execute(ReadAuditArtifactParams(file_path=str(pdf), render_page=visual, criterion_id="second"))
    assert not second.is_error, second.output
    assert io_usage(first)["hash_read_bytes"] == pdf.stat().st_size
    assert io_usage(first)["parser_read_bytes"] < 1024 * 1024
    assert io_usage(second)["source_read_bytes"] == 0
    assert io_usage(second)["data_cache_hit"] and io_usage(second)["hash_cache_hit"]
    assert io_usage(second)["source_write_bytes"] == 0
    if visual:
        assert first.content_blocks[0]["data"] == second.content_blocks[0]["data"]
    else:
        assert "Page one" in second.output


@pytest.mark.asyncio
async def test_cache_invalidates_when_source_changes_and_keeps_exact_source_hash(tmp_path):
    pdf = tmp_path / "changing.pdf"
    write_pdf(pdf, text="Before")
    tool = ReadAuditArtifact([str(tmp_path)])
    first = await tool.execute(ReadAuditArtifactParams(file_path=str(pdf)))
    assert not first.is_error
    write_pdf(pdf, text="After is a new version")
    second = await tool.execute(ReadAuditArtifactParams(file_path=str(pdf)))
    assert not second.is_error, second.output
    assert "After is a new version" in second.output and "Before" not in second.output
    assert hashlib.sha256(pdf.read_bytes()).hexdigest() in second.output
    assert not io_usage(second)["hash_cache_hit"] and not io_usage(second)["data_cache_hit"]


@pytest.mark.asyncio
async def test_pdf_rewrite_with_preserved_size_and_mtime_invalidates_review_fingerprint(tmp_path):
    import os
    from tests.test_review_work_queue import setup_queue
    pdf = tmp_path / "preserved.pdf"
    write_pdf(pdf, text="Before")
    original = pdf.stat()
    reader = ReadAuditArtifact([str(tmp_path)])
    first = await reader.execute(ReadAuditArtifactParams(file_path=str(pdf)))
    assert not first.is_error
    reviewer, _, _, _ = setup_queue(tmp_path)
    previous = reviewer._digest("preserved.pdf")
    write_pdf(pdf, text="After!")
    os.utime(pdf, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert pdf.stat().st_size == original.st_size
    assert reviewer._digest("preserved.pdf") != previous
    second = await reader.execute(ReadAuditArtifactParams(file_path=str(pdf)))
    assert not second.is_error and "After!" in second.output and "Before" not in second.output


@pytest.mark.asyncio
async def test_refresh_bypass_and_cache_controls_do_not_touch_source_or_evidence_ledger(tmp_path):
    pdf = tmp_path / "page.pdf"
    write_pdf(pdf)
    original, names = pdf.read_bytes(), set(tmp_path.iterdir())
    tool = ReadAuditArtifact([str(tmp_path)])
    for mode in ("use", "bypass", "refresh", "use"):
        result = await tool.execute(ReadAuditArtifactParams(file_path=str(pdf), cache_mode=mode))
        assert not result.is_error
        if mode == "bypass":
            assert io_usage(result)["cache_write_bytes"] == 0 and not io_usage(result)["data_cache_hit"]
        if mode == "refresh":
            assert io_usage(result)["source_read_bytes"] > 0
    assert io_usage(result)["source_read_bytes"] == 0
    pixels = ReadAuditArtifactParams(file_path=str(pdf), render_page=True, criterion_id="same")
    assert not (await tool.execute(pixels)).is_error
    info = await tool.execute(ReadAuditArtifactParams(action="cache_info"))
    payload = json.loads(info.output)
    assert payload["cache"]["entries"] > 0 and payload["io_totals"]["source_write_bytes"] == 0
    cleared = await tool.execute(ReadAuditArtifactParams(action="clear_cache", file_path=str(pdf)))
    assert not cleared.is_error
    assert (await tool.execute(pixels)).is_error  # Clearing read cache never clears acceptance evidence.
    assert pdf.read_bytes() == original and set(tmp_path.iterdir()) == names


@pytest.mark.asyncio
async def test_parallel_same_page_reads_share_one_load(tmp_path):
    pdf = tmp_path / "parallel.pdf"
    write_pdf(pdf)
    tool = ReadAuditArtifact([str(tmp_path)])
    results = await asyncio.gather(*(tool.execute(ReadAuditArtifactParams(
        file_path=str(pdf), render_page=True, criterion_id=str(index))) for index in range(4)))
    assert all(not result.is_error for result in results)
    assert sum(io_usage(result)["hash_read_bytes"] for result in results) == pdf.stat().st_size
    assert sum(not io_usage(result)["data_cache_hit"] for result in results) == 1


@pytest.mark.asyncio
async def test_long_line_cursor_can_read_all_text_without_skipping_a_fragment(tmp_path):
    text = "甲" * 30000 + "乙" * 8000
    path = tmp_path / "long.txt"
    path.write_text(text, encoding="utf-8")
    tool = ReadAuditArtifact([str(tmp_path)])
    offset = char_offset = 0
    chunks = []
    for _ in range(5):
        result = await tool.execute(ReadAuditArtifactParams(file_path=str(path), offset=offset, char_offset=char_offset))
        assert not result.is_error
        from mewcode.tools.base import MAX_OUTPUT_CHARS
        assert len(result.output) <= MAX_OUTPUT_CHARS
        chunks.extend(line.split("\t", 1)[1] for line in result.output.splitlines() if line.startswith("1\t"))
        cursor = result.host_observation["read_cursor"]
        if not cursor["has_more"]:
            break
        offset, char_offset = cursor["next_offset"], cursor["next_char_offset"]
    assert "".join(chunks) == text
    assert io_usage(result)["data_cache_hit"]


@pytest.mark.asyncio
async def test_cache_controls_cannot_reveal_or_clear_outside_allowed_roots(tmp_path):
    from mewcode.tools.artifact_read_cache import ArtifactReadCache
    cache = ArtifactReadCache()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    owner = ReadAuditArtifact([str(tmp_path)], cache=cache)
    assert not (await owner.execute(ReadAuditArtifactParams(file_path=str(outside)))).is_error
    for action in ("read", "cache_info", "clear_cache"):
        result = await ReadAuditArtifact([str(allowed)], cache=cache).execute(
            ReadAuditArtifactParams(action=action, file_path=str(outside)))
        assert result.is_error and "本轮基线或候选目录" in result.output
    assert io_usage(await owner.execute(ReadAuditArtifactParams(file_path=str(outside))))["source_read_bytes"] == 0


@pytest.mark.asyncio
async def test_pages_share_source_hash_but_have_distinct_cached_content(tmp_path):
    pdf = tmp_path / "two_pages.pdf"
    write_pdf(pdf, text="First page", second_text="Second page")
    tool = ReadAuditArtifact([str(tmp_path)])
    first = await tool.execute(ReadAuditArtifactParams(file_path=str(pdf), page=1))
    second = await tool.execute(ReadAuditArtifactParams(file_path=str(pdf), page=2))
    again = await tool.execute(ReadAuditArtifactParams(file_path=str(pdf), page=1))
    assert all(not result.is_error for result in (first, second, again))
    assert "First page" in first.output and "Second page" not in first.output
    assert "Second page" in second.output and "First page" not in second.output
    assert "PAGE: 2/2" in second.output
    assert io_usage(second)["hash_cache_hit"] and not io_usage(second)["data_cache_hit"]
    assert io_usage(second)["hash_read_bytes"] == 0 and io_usage(second)["parser_read_bytes"] > 0
    assert io_usage(again)["source_read_bytes"] == 0


def test_cache_is_bounded_lru_and_does_not_store_oversized_values(tmp_path):
    from mewcode.tools.artifact_read_cache import ArtifactPayload, ArtifactReadCache, ReadUsage, SourceVersion
    path = tmp_path / "source.txt"
    path.write_text("source", encoding="utf-8")
    source = SourceVersion.read(path)
    cache = ArtifactReadCache(max_bytes=5, max_entry_bytes=3, max_entries=2)
    usage = ReadUsage()
    for key, value in (("a", "aa"), ("b", "bbb"), ("a", "wrong"), ("c", "ccc")):
        result, hit = cache.get_or_load(source, (key,), lambda: ArtifactPayload(value), usage)
        if key == "a" and value == "wrong":
            assert hit and result.data == "aa"
    info = cache.info([tmp_path])
    assert info["entries"] == 2 and info["bytes"] == 5
    assert not cache.get_or_load(source, ("large",), lambda: ArtifactPayload("large"), usage)[1]
    assert not cache.get_or_load(source, ("large",), lambda: ArtifactPayload("large"), usage)[1]
    assert cache.info([tmp_path])["bytes"] == 5


def test_cache_expiry_and_clear_are_scoped_to_allowed_directories(tmp_path, monkeypatch):
    from mewcode.tools.artifact_read_cache import ArtifactPayload, ArtifactReadCache, ReadUsage, SourceVersion
    clock = [100]
    monkeypatch.setattr("mewcode.tools.artifact_read_cache.time.monotonic", lambda: clock[0])
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    cache, usage = ArtifactReadCache(ttl_seconds=5), ReadUsage()
    for root in (first, second):
        path = root / "source.txt"
        path.write_text("source", encoding="utf-8")
        cache.get_or_load(SourceVersion.read(path), ("text",), lambda: ArtifactPayload("cached"), usage)
    assert cache.info([first])["entries"] == 1
    assert cache.clear([first]) == 1
    assert cache.info([second])["entries"] == 1
    clock[0] += 6
    assert cache.info([second])["entries"] == 0


@pytest.mark.asyncio
async def test_changed_during_parse_is_not_cached_or_delivered(tmp_path, monkeypatch):
    from PIL import Image
    import io
    pdf = tmp_path / "changed.pdf"
    write_pdf(pdf)
    pixels = io.BytesIO()
    Image.new("RGB", (10, 10), "blue").save(pixels, format="PNG")
    def changed_read(path, page, **kwargs):
        write_pdf(pdf, text="Concurrent replacement")
        return pixels.getvalue(), 1
    monkeypatch.setattr("mewcode.tools.pdf_pixels.render_page", changed_read)
    tool = ReadAuditArtifact([str(tmp_path)])
    result = await tool.execute(ReadAuditArtifactParams(file_path=str(pdf), render_page=True))
    assert result.is_error and "读取过程中发生变化" in result.output
    assert not result.content_blocks and not tool.ledger.as_list()


@pytest.mark.asyncio
async def test_bad_pdf_then_fixed_source_does_not_reuse_failure(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"not a pdf")
    tool = ReadAuditArtifact([str(tmp_path)])
    assert (await tool.execute(ReadAuditArtifactParams(file_path=str(path)))).is_error
    write_pdf(path, text="Repaired")
    fixed = await tool.execute(ReadAuditArtifactParams(file_path=str(path)))
    assert not fixed.is_error and "Repaired" in fixed.output
    info = json.loads((await tool.execute(ReadAuditArtifactParams(action="cache_info"))).output)
    assert info["io_totals"]["failed_calls"] == 1 and info["io_totals"]["read_calls"] == 2


@pytest.mark.asyncio
async def test_non_pdf_size_guard_rejects_before_reading_and_cache_is_not_a_vision_grant(tmp_path):
    path = tmp_path / "large.txt"
    with path.open("wb") as stream:
        stream.seek(64 * 1024 * 1024)
        stream.write(b"x")
    result = await ReadAuditArtifact([str(tmp_path)]).execute(ReadAuditArtifactParams(file_path=str(path)))
    assert result.is_error and io_usage(result)["source_read_bytes"] == 0
    pdf = tmp_path / "page.pdf"
    write_pdf(pdf)
    assert not (await ReadAuditArtifact([str(tmp_path)]).execute(ReadAuditArtifactParams(
        file_path=str(pdf), render_page=True))).is_error
    denied = await ReadAuditArtifact([str(tmp_path)], vision_ok=False).execute(ReadAuditArtifactParams(
        file_path=str(pdf), render_page=True))
    assert denied.is_error and not denied.content_blocks and io_usage(denied)["source_read_bytes"] == 0


@pytest.mark.asyncio
async def test_cancelled_read_counts_eventual_io_without_delivering_content(tmp_path, monkeypatch):
    import threading
    from mewcode.tools.artifact_read_cache import CountedReader
    pdf = tmp_path / "cancel.pdf"
    write_pdf(pdf)
    started, release = threading.Event(), threading.Event()
    def slow_read(path, page, *, read_meter):
        started.set()
        assert release.wait(5)
        with CountedReader(path, read_meter) as stream:
            stream.read(16)
        return "finished after cancel", 1
    monkeypatch.setattr("mewcode.tools.pdf_pixels.extract_page_text", slow_read)
    tool = ReadAuditArtifact([str(tmp_path)])
    pending = asyncio.create_task(tool.execute(ReadAuditArtifactParams(file_path=str(pdf))))
    assert await asyncio.to_thread(started.wait, 5)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    release.set()
    for _ in range(100):
        totals = json.loads((await tool.execute(ReadAuditArtifactParams(action="cache_info"))).output)["io_totals"]
        if not totals["active_reads"]:
            break
        await asyncio.sleep(0.01)
    assert totals["source_read_bytes"] == pdf.stat().st_size + 16
    assert totals["cancelled_calls"] == 1 and totals["read_calls"] == 1
    assert totals["output_text_bytes"] == totals["output_image_bytes"] == 0
    assert not tool.ledger.as_list()


@pytest.mark.asyncio
async def test_large_pdf_pixels_register_in_review_queue_and_source_checks_reuse_hash(tmp_path, monkeypatch):
    from tests.test_review_work_queue import setup_queue
    pdf = tmp_path / "large.pdf"
    write_pdf(pdf, second_text="Page two", padding=64 * 1024 * 1024)
    reviewer, init, store, run_id = setup_queue(tmp_path)
    init.image_path_template = "large.pdf"
    assert not (await reviewer.execute(init)).is_error
    items = store.load(run_id)["review_queues"]["check"]["items"]
    reader = ReadAuditArtifact([str(tmp_path)])
    reader.image_observer = reviewer.observe_image
    first = await reader.execute(ReadAuditArtifactParams(
        file_path=str(pdf), page=1, render_page=True, criterion_id=items[0]["item_id"]))
    assert not first.is_error, first.output
    second = await reader.execute(ReadAuditArtifactParams(
        file_path=str(pdf), page=2, render_page=True, criterion_id=items[1]["item_id"]))
    assert not second.is_error, second.output
    queue = store.load(run_id)["review_queues"]["check"]
    def forbidden_read(*args, **kwargs):
        raise AssertionError("A warm PDF fingerprint must not read source bytes again")
    with monkeypatch.context() as cached:
        cached.setattr("mewcode.tools.artifact_read_cache.CountedReader", forbidden_read)
        for item in items:
            receipt = queue["image_receipts"][item["item_id"]][0]
            assert receipt["page"] == item["identity"]["pdf_page"]
            assert reviewer._digest(receipt["path"]) == receipt["sha256"]
    previous = reviewer._digest("large.pdf")
    write_pdf(pdf, text="New source")
    assert reviewer._digest("large.pdf") != previous
    assert io_usage(second)["hash_read_bytes"] == 0


def test_large_pdf_review_limit_only_applies_to_page_sources(tmp_path, monkeypatch):
    import os
    from pathlib import Path
    from tests.test_review_work_queue import setup_queue
    reviewer, _, _, _ = setup_queue(tmp_path)
    original_stat = Path.stat
    def large_stat(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if path.name == "huge.pdf":
            values = list(result)
            values[6] = 2 * 1024 * 1024 * 1024 + 1
            return os.stat_result(values)
        return result
    monkeypatch.setattr(Path, "stat", large_stat)
    for name, size in (("large.pdf", 64 * 1024 * 1024 + 1),
                       ("large.txt", 64 * 1024 * 1024 + 1),
                       ("huge.pdf", 1)):
        path = tmp_path / name
        with path.open("wb") as stream:
            stream.seek(size - 1)
            stream.write(b"x")
        with pytest.raises(ValueError, match="超过读取上限"):
            reviewer._path(name)
        if name == "large.pdf":
            assert reviewer._path(name, pdf_source=True) == path
        else:
            with pytest.raises(ValueError, match="超过读取上限"):
                reviewer._path(name, pdf_source=True)
            with pytest.raises(ValueError, match="超过读取上限"):
                reviewer._digest(name)


def test_bypass_does_not_inspect_or_mutate_existing_cache(tmp_path):
    from mewcode.tools.artifact_read_cache import ArtifactPayload, ArtifactReadCache, ReadUsage, SourceVersion
    path = tmp_path / "source.txt"
    path.write_text("Before", encoding="utf-8")
    cache = ArtifactReadCache()
    cache.get_or_load(SourceVersion.read(path), ("text",), lambda: ArtifactPayload("old"), ReadUsage())
    before = cache.info([tmp_path])
    path.write_text("After", encoding="utf-8")
    usage = ReadUsage()
    payload, hit = cache.get_or_load(SourceVersion.read(path), ("text",),
        lambda: ArtifactPayload("new"), usage, mode="bypass")
    assert payload.data == "new" and not hit
    assert cache.info([tmp_path]) == before
    assert usage.cache_read_bytes == usage.cache_write_bytes == usage.cache_hits == usage.cache_misses == 0
