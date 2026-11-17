"""Complete, bounded-segment JSONL scans without modifying session history."""
from __future__ import annotations

import hashlib
import os


def _identity(stat):
    # Windows fstat/stat can expose different legacy ctime values for the same
    # file (creation vs metadata time). File ID, size and write time agree.
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def history_lines(path, *, limit, scan):
    """Yield complete records across segment boundaries, then any partial tail.

    A segment and an individual record may each consume at most ``limit``
    bytes. The caller must consume the iterator to completion before publishing
    any sources: final file-identity validation is part of this operation.
    """
    if type(limit) is not int or limit < 1:
        raise ValueError("历史读取分段上限无效")
    # Avoid buffered read-ahead and cached-prefix reuse during revalidation.
    with path.open("rb", buffering=0) as stream:
        original = os.fstat(stream.fileno())
        size = original.st_size
        scan.update(bytes=size, bytes_read=0, segments=0, max_segment_bytes=0, complete=False)
        remaining, segment_bytes = size, 0
        pending = bytearray()
        checksum = hashlib.sha256()
        while remaining:
            # IO buffers stay small even when a permitted segment is 100 MiB.
            count = min(1024 * 1024, limit - segment_bytes, remaining)
            chunk = stream.read(count)
            if not chunk:
                raise ValueError("历史在分段读取期间被截短；未采用不完整来源，请重新读取。")
            checksum.update(chunk)
            remaining -= len(chunk)
            segment_bytes += len(chunk)
            scan["bytes_read"] += len(chunk)
            if segment_bytes == limit or not remaining:
                scan["segments"] += 1
                scan["max_segment_bytes"] = max(scan["max_segment_bytes"], segment_bytes)
                segment_bytes = 0
            pieces = chunk.split(b"\n")
            for index, piece in enumerate(pieces):
                terminated = index < len(pieces) - 1
                if len(pending) + len(piece) + int(terminated) > limit:
                    raise ValueError("单条历史记录超过安全读取上限；未截断记录或提高上限，不能生成完整来源快照。")
                pending.extend(piece)
                if terminated:
                    pending.append(10)
                    yield bytes(pending)
                    pending.clear()
        if pending:
            yield bytes(pending)
        after = os.fstat(stream.fileno())
        current = path.stat()
        if _identity(original) != _identity(after) or _identity(original) != _identity(current):
            same_file = lambda stat: (stat.st_dev, stat.st_ino) == (original.st_dev, original.st_ino)
            if not (same_file(after) and same_file(current) and after.st_size > size and current.st_size > size):
                raise ValueError("历史在分段读取期间发生变化；未采用混合版本，请重新读取。")
            # Normal chat persistence may append while a scan is running. Size
            # growth alone does not prove the scanned prefix stayed unchanged:
            # verify every captured byte again, in bounded reads, before reuse.
            stream.seek(0)
            verified = hashlib.sha256()
            remaining = size
            while remaining:
                chunk = stream.read(min(1024 * 1024, limit, remaining))
                if not chunk:
                    raise ValueError("历史在校验期间被截短；未采用不完整来源，请重新读取。")
                verified.update(chunk)
                remaining -= len(chunk)
            after, current = os.fstat(stream.fileno()), path.stat()
            if (not same_file(after) or not same_file(current) or min(after.st_size, current.st_size) < size
                    or verified.digest() != checksum.digest()):
                raise ValueError("历史前文在分段读取期间发生变化；未采用混合版本，请重新读取。")
            scan.update(verified_prefix_bytes=size, appended_after_snapshot=True)
        scan.update(complete=True, history_digest=checksum.hexdigest())
