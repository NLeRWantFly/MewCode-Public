"""Bounded public GitHub skill installation; no shell or downloaded execution."""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import httpx

from mewcode.skills.parser import parse_skill_file

MAX_ARCHIVE = 20 * 1024 * 1024
MAX_FILES = 512
MAX_EXPANDED = 20 * 1024 * 1024
RECEIPT = ".mewcode-install.json"


def safe_parts(value):
    parts = value.split("/")
    for part in parts:
        if (not part or part in {".", ".."} or part.endswith((".", " "))
                or re.search(r'[\\:<>"|?*\x00-\x1f]', part)
                or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)):
            raise ValueError("Invalid portable path component")
    return parts


async def download(repo, ref):
    url = f"https://codeload.github.com/{repo}/zip/{quote(ref, safe='')}"
    data = bytearray()
    async with httpx.AsyncClient(timeout=45, follow_redirects=False, trust_env=False) as client:
        async with client.stream("GET", url) as response:
            if response.status_code != 200:
                raise ValueError(f"GitHub archive HTTP {response.status_code}; no shell fallback")
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_ARCHIVE:
                    raise ValueError("Archive exceeds download limit")
    return bytes(data)


def install_archive(work_dir, archive, *, repo, ref, path):
    if len(archive) > MAX_ARCHIVE:
        raise ValueError("Archive exceeds download limit")
    requested = safe_parts(path)
    root = Path(work_dir).resolve(strict=True)
    base = root / ".mewcode" / "skills"
    # Reject aliases/junctions rather than writing through them.
    for parent in (root / ".mewcode", base):
        if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)() or parent.resolve() != parent.absolute():
            raise ValueError("Skill destination must not be a linked directory")
    files = {}
    size = 0
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        if len(source.infolist()) > 20000:
            raise ValueError("Archive contains too many entries")
        for entry in source.infolist():
            parts = entry.filename.split("/")
            if parts[1:1 + len(requested)] != requested or len(parts) <= 1 + len(requested):
                continue
            if "\\" in entry.orig_filename:
                raise ValueError("Archive paths must use forward slashes")
            if entry.is_dir():
                continue
            relative = "/".join(parts[1 + len(requested):])
            safe_parts(relative)
            if stat.S_IFMT(entry.external_attr >> 16) not in {0, stat.S_IFREG}:
                raise ValueError("Skill archive contains a link or special file")
            key = relative.casefold()
            if key in {name.casefold() for name in files} or key == RECEIPT.casefold():
                raise ValueError("Duplicate or reserved skill path")
            size += entry.file_size
            if size > MAX_EXPANDED or len(files) >= MAX_FILES:
                raise ValueError("Skill exceeds extraction limits")
            files[relative] = source.read(entry)
    if "SKILL.md" not in files:
        raise ValueError("Selected directory has no SKILL.md")
    base.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=".install-", dir=base))
    try:
        for relative, data in files.items():
            target = temp.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        skill = parse_skill_file(temp / "SKILL.md")
        safe_parts(skill.name)
        if len(skill.name) > 64:
            raise ValueError("Skill name exceeds 64 characters")
        # Keep the source selection and resulting skill identity unambiguous.
        if skill.name != requested[-1]:
            raise ValueError("Skill name must match the selected directory")
        from mewcode.skills.loader import SkillLoader
        if skill.name in SkillLoader(str(root)).load_all():
            raise ValueError("Skill already exists; installation never replaces or shadows it")
        destination = base / skill.name
        if destination.exists() or destination.is_symlink():
            raise ValueError("Skill destination already exists")
        receipt = {"repo": repo, "ref": ref, "path": path, "name": skill.name,
                   "instruction_only": True, "archive_sha256": hashlib.sha256(archive).hexdigest(),
                   "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
        (temp / RECEIPT).write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        # Same filesystem publish; exclusive cross-process lock prevents races.
        lock = base / (".install-" + skill.name + ".lock")
        handle = lock.open("x")
        try:
            with handle:
                if destination.exists() or destination.is_symlink():
                    raise ValueError("Skill destination already exists")
                temp.rename(destination)
        finally:
            lock.unlink()
        return {"name": skill.name, "destination": str(destination),
                "archive_sha256": receipt["archive_sha256"], "files": len(files),
                "instruction_only": True}
    finally:
        if temp.exists():
            shutil.rmtree(temp)


async def install(work_dir, *, repo, ref, path):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("repo must be a public GitHub owner/repo")
    safe_parts(repo)
    safe_parts(path)
    if not re.fullmatch(r"[A-Za-z0-9_./-]{1,160}", ref):
        raise ValueError("Invalid GitHub ref")
    safe_parts(ref)
    archive = await download(repo, ref)
    return install_archive(work_dir, archive, repo=repo, ref=ref, path=path)
