from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from threading import RLock


# Bounded storage, shared by readers and writers; collisions only serialize
# unrelated paths. These are process-local locks, not cross-process leases.
_PATH_LOCKS = tuple(RLock() for _ in range(128))
_REPLACE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)


def file_lock(path: Path):
    key = os.path.normcase(os.path.abspath(path))
    return _PATH_LOCKS[hash(key) % len(_PATH_LOCKS)]


def _replace_with_retry(source: Path, target: Path) -> None:
    for attempt in range(len(_REPLACE_RETRY_DELAYS) + 1):
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            # Windows readers/AV may temporarily deny replacement. Never retry
            # content generation, append, arbitrary I/O errors, or change ACLs.
            if (getattr(exc, "winerror", None) not in {5, 32, 33}
                    or attempt == len(_REPLACE_RETRY_DELAYS)):
                raise
            time.sleep(_REPLACE_RETRY_DELAYS[attempt])


def atomic_write_text(path: Path, text: str) -> None:
    """先写临时文件再 os.replace 原子替换（Windows 上同样原子）。

    团队的 config.json/tasks.json/邮箱消息都是跨协程共享的整文件状态，
    截断式覆写在进程中途崩溃时会留下半写文件，让所有读取方永久报错。
    """
    with file_lock(path):
        _atomic_write_text(path, text)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:6]}")
    try:
        tmp.write_text(text, encoding="utf-8")
        _replace_with_retry(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def quarantine_corrupt_file(path: Path) -> Path | None:
    """把损坏的 JSON 文件挪到 .corrupt-<ts> 备份，返回备份路径。"""
    try:
        backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        os.replace(path, backup)
        return backup
    except OSError:
        return None
