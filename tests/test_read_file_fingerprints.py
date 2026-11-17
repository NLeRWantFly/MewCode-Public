import hashlib
import json

import pytest

from mewcode.tools.read_file import Params, ReadFile


@pytest.mark.asyncio
async def test_read_file_reports_raw_and_normalized_text_fingerprints(tmp_path):
    path = tmp_path / "windows-lines.md"
    raw = b"\xef\xbb\xbfalpha\r\nbeta\r\n"
    path.write_bytes(raw)

    result = await ReadFile().execute(Params(
        file_path=str(path), limit=0, fingerprint_mode="both"
    ))

    assert not result.is_error
    payload = json.loads(result.output)
    fingerprints = payload["fingerprints"]
    assert payload["content"] == ""
    assert fingerprints["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert fingerprints["text_sha256"] == hashlib.sha256(b"alpha\nbeta\n").hexdigest()
    assert fingerprints["raw_sha256"] != fingerprints["text_sha256"]
    assert fingerprints["raw_digest_kind"] == "raw_file_sha256_v1"
    assert fingerprints["text_digest_kind"] == "utf8_text_universal_newline_lf_v1"


@pytest.mark.asyncio
async def test_read_file_default_output_stays_line_numbered(tmp_path):
    path = tmp_path / "plain.md"
    path.write_text("one\ntwo\n", encoding="utf-8")
    result = await ReadFile().execute(Params(file_path=str(path), limit=1))
    assert result.output == "1\tone"
    assert result.host_observation == {"file_state": {
        "absolute_path": str(path.resolve()), "exists": True, "is_file": True,
        "text_sha256": hashlib.sha256(b"one\ntwo\n").hexdigest()}}


@pytest.mark.asyncio
async def test_read_file_missing_path_keeps_structured_negative_observation(tmp_path):
    path = tmp_path / "not-created.txt"
    result = await ReadFile().execute(Params(file_path=str(path)))

    assert result.is_error
    assert "file not found" in result.output
    assert result.host_observation == {"file_state": {
        "absolute_path": str(path.resolve()), "exists": False, "is_file": False,
        "text_sha256": None}}


@pytest.mark.asyncio
async def test_raw_fingerprint_only_supports_binary_files(tmp_path):
    path = tmp_path / "pilot.png"
    raw = b"\x89PNG\r\n\x1a\n\x00\xff"
    path.write_bytes(raw)

    result = await ReadFile().execute(Params(
        file_path=str(path), limit=0, fingerprint_mode="raw"
    ))

    assert not result.is_error
    payload = json.loads(result.output)
    assert payload == {"fingerprints": {
        "path": str(path.resolve()), "bytes": len(raw),
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "raw_digest_kind": "raw_file_sha256_v1",
    }, "content": ""}
    assert result.host_observation == {"file_state": {
        "absolute_path": str(path.resolve()), "exists": True, "is_file": True,
        "text_sha256": None}}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,limit", [("none", 1), ("raw", 1), ("both", 0)])
async def test_binary_file_cannot_supply_text_evidence(tmp_path, mode, limit):
    path = tmp_path / "pilot.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n")

    result = await ReadFile().execute(Params(
        file_path=str(path), limit=limit, fingerprint_mode=mode
    ))

    assert result.is_error
    assert "codec can't decode" in result.output
    assert result.host_observation["file_state"]["text_sha256"] is None
