"""Stable fingerprint algorithms shared by read tools and review evidence."""
from __future__ import annotations

import hashlib


TEXT_DIGEST_KIND = "utf8_text_universal_newline_lf_v1"
RAW_FILE_DIGEST_KIND = "raw_file_sha256_v1"


def content_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalized_text_digest(path) -> str:
    # utf-8-sig removes a BOM and Python text I/O normalizes CRLF/CR to LF.
    return content_digest(path.read_text(encoding="utf-8-sig"))


def file_digest(path) -> str:
    """Hash the exact source bytes, without text newline normalization."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()
