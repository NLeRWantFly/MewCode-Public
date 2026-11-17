"""Keep decoded evidence representable in UTF-8 without dropping characters."""
from __future__ import annotations

import re
from typing import Any


_SURROGATES = re.compile(r"[\ud800-\udbff][\udc00-\udfff]|[\ud800-\udfff]")


def utf8_safe_text(text: str) -> str:
    """Join valid UTF-16 pairs; visibly escape isolated surrogate code units.

    Broken PDF font maps and imported JSON can contain lone surrogates. They
    are not Unicode scalar values, so neither UTF-8 requests nor cache hashes
    can encode them. Keep a visible, reversible code-unit marker rather than
    silently deleting evidence or inventing the missing glyph.
    """
    def repair(match: re.Match[str]) -> str:
        value = match.group()
        if len(value) == 2:
            return chr(0x10000 + ((ord(value[0]) - 0xD800) << 10) + ord(value[1]) - 0xDC00)
        return f"\\u{ord(value):04x}"

    return _SURROGATES.sub(repair, text)


def utf8_safe_json(value: Any) -> Any:
    """Copy a JSON-shaped transport value, leaving the source history intact."""
    if isinstance(value, str):
        return utf8_safe_text(value)
    if isinstance(value, list):
        return [utf8_safe_json(item) for item in value]
    if isinstance(value, dict):
        return {utf8_safe_text(key) if isinstance(key, str) else key: utf8_safe_json(item)
                for key, item in value.items()}
    return value
