"""Per-stage change explanations: which file changed, where, and how.

Attribution comes from ProcessFile receipts captured at real tool-result
boundaries (``node_id`` + ``path``).  The *content* of a change comes from git,
never from model prose.  A receipt proves a node wrote a path; git proves what
the file became.  Neither implies the stage was accepted.

Everything here is read-only and bounded.  A non-git project, a missing git
binary, or a broken work tree yields an explicit unavailable state instead of
an exception or an invented summary.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

GIT_TIMEOUT_SECONDS = 8.0
MAX_FILES_PER_NODE = 12
MAX_HUNKS_PER_FILE = 10
MAX_LINES_PER_HUNK = 60
MAX_UNATTRIBUTED_FILES = 20
UNTRACKED_PREVIEW_LINES = 80
FIELD_SEP = "\x1f"

STATUS_LABEL = {
    "A": "新增",
    "M": "修改",
    "D": "删除",
    "R": "重命名",
    "C": "复制",
    "T": "类型变更",
    "U": "冲突",
    "?": "未跟踪",
}

LINE_KIND = {"+": "add", "-": "del", " ": "ctx"}

MAX_CONFLICT_FILES = 12
MAX_CONFLICT_SECTIONS = 8
MAX_CONFLICT_LINES = 40
# git writes side markers as exactly seven chars plus an optional " label",
# and the middle separator as exactly seven "=". Matching loosely would treat a
# content line such as a "========" Markdown underline as a separator.
CONFLICT_MARKER_RE = re.compile(
    r"^(?:(?P<mark><{7}|\|{7}|>{7})(?: (?P<label>.*))?|(?P<sep>={7}))$"
)

HUNK_HEADER_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@[ \t]?(?P<section>.*)$"
)
NAME_STATUS_RE = re.compile(r"^(?P<status>[AMDRCTUX?])\d*\t(?P<path>.+)$")
PATCH_NOISE_PREFIXES = (
    "diff --git ",
    "index ",
    "--- ",
    "+++ ",
    "new file mode",
    "deleted file mode",
    "old mode",
    "new mode",
    "similarity index",
    "dissimilarity index",
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
    "Binary files ",
)


def parse_conflict_markers(lines: Sequence[str]) -> list[dict[str, Any]]:
    """Split git conflict markers into side-by-side sections.

    Each section keeps both sides (and the common ancestor when diff3 recorded
    one) with the line where the conflict starts, so the canvas can show what
    the two writers actually disagreed about instead of just "文件冲突".
    """
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    side = ""
    for number, line in enumerate(lines, 1):
        matched = CONFLICT_MARKER_RE.match(line)
        marker = (matched.group("mark") or ("=" * 7 if matched.group("sep") else "")) if matched else ""
        label = (matched.group("label") or "").strip() if matched else ""
        if marker == "<" * 7:
            if current is not None:
                sections.append(current)
            current = {
                "start_line": number,
                "ours_label": label,
                "theirs_label": "",
                "base_label": "",
                "ours": [],
                "base": [],
                "theirs": [],
                "truncated": False,
            }
            side = "ours"
            continue
        if current is None:
            continue
        if marker == "|" * 7:
            current["base_label"] = label
            side = "base"
            continue
        if marker == "=" * 7:
            side = "theirs"
            continue
        if marker == ">" * 7:
            current["theirs_label"] = label
            sections.append(current)
            current = None
            side = ""
            continue
        if not side:
            continue
        bucket = current[side]
        if len(bucket) >= MAX_CONFLICT_LINES:
            current["truncated"] = True
            continue
        bucket.append(line)
    if current is not None:
        sections.append(current)
    return sections[:MAX_CONFLICT_SECTIONS]


def _is_unmerged(porcelain: str) -> bool:
    """UU / AA / DU / UD / AU / UA — any unmerged combination."""
    return len(porcelain) >= 2 and "U" in porcelain[:2]


def read_text_lines(path: Path, limit: int = 4000) -> tuple[list[str], bool]:
    """Read a bounded text file; empty for binary, huge, or unreadable files."""
    try:
        with path.open("rb") as handle:
            data = handle.read(2 * 1024 * 1024)
    except OSError:
        return [], False
    if b"\x00" in data[:4096]:
        return [], False
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError:
        return [], False
    lines = text.splitlines()
    return lines[:limit], len(lines) > limit


async def _git(
    work_dir: str,
    args: Sequence[str],
    *,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> tuple[int, str]:
    """Run a read-only git command and return ``(exit_code, combined_output)``."""
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=work_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as exc:  # git missing, cwd removed, permission denied
        return -1, str(exc)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        return -1, "git 命令超时"
    except Exception as exc:
        return -1, str(exc)
    output = (stdout or b"") + (stderr or b"")
    return process.returncode if process.returncode is not None else -1, output.decode(
        errors="replace"
    )


def relative_path(repo: str, raw: Any) -> str:
    """Return a repo-relative POSIX path, or ``""`` when it escapes the repo."""
    text = str(raw or "").strip()
    if not text:
        return ""
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = Path(repo) / candidate
    try:
        resolved = candidate.resolve()
        repo_root = Path(repo).resolve()
        relative = resolved.relative_to(repo_root)
    except (OSError, ValueError):
        return ""
    parts = relative.parts
    if not parts or any(part == ".." for part in parts):
        return ""
    return relative.as_posix()


def parse_unified_diff(text: str) -> dict[str, Any]:
    """Turn one single-file unified diff into display-ready hunks.

    Each hunk keeps its line ranges, its enclosing-section context (the "where"
    inside the file), and the +/- lines with their own kind, so the canvas can
    show exactly what changed without re-parsing or guessing.
    """
    status = ""
    hunks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    additions = 0
    deletions = 0
    lines_dropped = 0
    for line in text.splitlines():
        if not status:
            matched = NAME_STATUS_RE.match(line)
            if matched:
                status = matched.group("status")
                continue
        if line.startswith(PATCH_NOISE_PREFIXES):
            continue
        header = HUNK_HEADER_RE.match(line)
        if header:
            old_start = int(header.group("old_start"))
            new_start = int(header.group("new_start"))
            old_count = int(header.group("old_count") or 1)
            new_count = int(header.group("new_count") or 1)
            current = {
                "old_start": old_start,
                "old_count": old_count,
                "new_start": new_start,
                "new_count": new_count,
                "old_range": _range_label(old_start, old_count),
                "new_range": _range_label(new_start, new_count),
                "section": (header.group("section") or "").strip(),
                "changed": 0,
                "truncated": False,
                "lines": [],
            }
            hunks.append(current)
            continue
        if current is None:
            continue
        marker = line[:1]
        if marker == "\\":
            current["lines"].append({"kind": "note", "text": line[1:].strip()})
            continue
        kind = LINE_KIND.get(marker)
        if kind is None:
            continue
        if kind == "add":
            additions += 1
            current["changed"] += 1
        elif kind == "del":
            deletions += 1
            current["changed"] += 1
        if len(current["lines"]) >= MAX_LINES_PER_HUNK:
            current["truncated"] = True
            lines_dropped += 1
            continue
        current["lines"].append({"kind": kind, "text": line[1:]})
    visible = hunks[:MAX_HUNKS_PER_FILE]
    return {
        "status": status if status in STATUS_LABEL else "",
        "additions": additions,
        "deletions": deletions,
        "hunks": visible,
        "hunk_total": len(hunks),
        "truncated": len(hunks) > len(visible) or lines_dropped > 0,
    }


def _range_label(start: int, count: int) -> str:
    if count <= 0:
        return ""
    if count == 1:
        return f"{start}"
    return f"{start}–{start + count - 1}"


def _preview_lines(path: Path, limit: int = UNTRACKED_PREVIEW_LINES) -> tuple[list[str], bool]:
    """Read the head of a not-yet-tracked file so new files still show content."""
    try:
        data = path.read_bytes()[: 64 * 1024]
    except OSError:
        return [], False
    if b"\x00" in data[:4096]:
        return [], False
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError:
        return [], False
    lines = text.splitlines()
    return lines[:limit], len(lines) > limit


def _synthetic_added_hunks(lines: Sequence[str], *, label: str) -> list[dict[str, Any]]:
    if not lines:
        return []
    return [
        {
            "old_start": 0,
            "old_count": 0,
            "new_start": 1,
            "new_count": len(lines),
            "old_range": "",
            "new_range": _range_label(1, len(lines)),
            "section": label,
            "changed": len(lines),
            "truncated": False,
            "lines": [{"kind": "add", "text": line} for line in lines],
        }
    ]


async def _commit_info(repo: str, ref: str, *args: str) -> dict[str, Any] | None:
    fmt = FIELD_SEP.join(["%H", "%h", "%an", "%aI", "%s"])
    code, output = await _git(repo, ["log", "-1", f"--format={fmt}", ref or "HEAD", "--", *args])
    if code != 0:
        return None
    parts = output.strip("\n").split(FIELD_SEP)
    if len(parts) < 5:
        return None
    sha, short, author, date, subject = parts[:5]
    if not sha:
        return None
    return {
        "sha": sha,
        "short": short,
        "author": author,
        "date": date,
        "subject": subject,
        "ref": ref or "HEAD",
    }


def _receipt_rank(receipt: dict[str, Any]) -> tuple[int, float]:
    try:
        attempt = int(receipt.get("node_attempt") or 0)
    except (TypeError, ValueError):
        attempt = 0
    try:
        created = float(receipt.get("created_at") or 0)
    except (TypeError, ValueError):
        created = 0.0
    return attempt, created


def _dirty_state(porcelain: str) -> str:
    code = porcelain[:2]
    if not porcelain.strip():
        return "clean"
    if code == "??":
        return "untracked"
    return "dirty"


async def _file_change(
    repo: str,
    path: str,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    """Explain one file attributed to one node: commit, status, hunks, content."""
    entry: dict[str, Any] = {
        "path": path,
        "name": Path(path).name,
        "attempt": int(receipt.get("node_attempt") or 0),
        "member_id": str(receipt.get("member_id") or ""),
        "source": str(receipt.get("source") or ""),
        "source_label": "工具写入成功"
        if str(receipt.get("source") or "") == "successful_write"
        else "登记的已有文件",
        "captured_at": receipt.get("created_at"),
        "bytes": int(receipt.get("size") or 0),
        "status": "",
        "status_label": "",
        "additions": 0,
        "deletions": 0,
        "hunk_total": 0,
        "commit": None,
        "commit_ref": "",
        "dirty": "clean",
        "unmerged": False,
        "conflicts": [],
        "conflict_total": 0,
        "hunks": [],
        "truncated": False,
        "error": "",
        "note": "",
    }

    code, porcelain = await _git(repo, ["status", "--porcelain", "--", path])
    if code == 0:
        entry["dirty"] = _dirty_state(porcelain)
        entry["unmerged"] = _is_unmerged(porcelain)

    # An unmerged file is not a diff: the disagreement itself is the content the
    # canvas has to show, so read the marked-up file and split the sections.
    if entry["unmerged"]:
        lines, _truncated = read_text_lines(Path(repo) / path)
        sections = parse_conflict_markers(lines)
        entry["conflicts"] = sections
        entry["conflict_total"] = len(sections)
        entry["status"] = "U"
        entry["status_label"] = STATUS_LABEL["U"]
        if not sections:
            entry["note"] = "文件处于未合并状态，但没有读到冲突标记（可能已被手工编辑）。"
        return entry

    owner = await _commit_info(repo, "", path)
    diff_text = ""
    status_code = ""
    if owner is not None:
        entry["commit"] = owner
        # --name-status and --patch are mutually exclusive diff formats, so the
        # status code and the patch must be requested separately.
        code, status_out = await _git(
            repo,
            [
                "diff-tree",
                "--no-commit-id",
                "--root",
                "--name-status",
                "-r",
                owner["sha"],
                "--",
                path,
            ],
        )
        if code == 0:
            for line in status_out.splitlines():
                matched = NAME_STATUS_RE.match(line.strip())
                if matched:
                    status_code = matched.group("status")
                    break
        # --diff-merges=first-parent keeps a merge commit readable as an
        # ordinary diff against its first parent. Without it git emits a
        # combined diff ("@@@") which carries no parsable hunks, so a write that
        # landed in a merge would look like "no changes at all".
        show_args = [
            "show",
            "--format=",
            "--unified=3",
            "--no-color",
            "--no-ext-diff",
            "--find-renames",
            "--diff-merges=first-parent",
            owner["sha"],
            "--",
            path,
        ]
        code, diff_text = await _git(repo, show_args)
        if code != 0:
            # Older git builds may not know the flag; fall back rather than
            # losing the diff entirely.
            code, diff_text = await _git(repo, [item for item in show_args if item != "--diff-merges=first-parent"])
        if code != 0:
            diff_text = ""
        elif "@@@" in diff_text and "@@" not in diff_text.replace("@@@", ""):
            diff_text = ""
    parsed = parse_unified_diff(diff_text) if diff_text.strip() else {
        "status": "",
        "additions": 0,
        "deletions": 0,
        "hunks": [],
        "hunk_total": 0,
        "truncated": False,
    }
    if status_code:
        parsed["status"] = status_code

    absolute = Path(repo) / path
    if entry["dirty"] == "untracked" or (not parsed["hunks"] and not owner):
        lines, more = _preview_lines(absolute)
        if lines:
            parsed["hunks"] = _synthetic_added_hunks(lines, label="新文件内容（未提交）")
            parsed["additions"] = len(lines)
            parsed["status"] = "?"
            parsed["truncated"] = more
            entry["note"] = "工作区尚未提交此文件，展示的是文件当前开头内容。"

    entry.update(
        status=parsed["status"],
        status_label=STATUS_LABEL.get(parsed["status"], ""),
        additions=parsed["additions"],
        deletions=parsed["deletions"],
        hunks=parsed["hunks"],
        hunk_total=parsed["hunk_total"],
        truncated=parsed["truncated"],
    )
    if not entry["hunks"] and not entry["note"]:
        if entry["dirty"] == "dirty":
            entry["note"] = "文件在工作区有改动，但 git 未给出可比对的内容（可能是二进制文件）。"
        else:
            entry["note"] = "该文件在当前提交中没有可展示的差异（内容与提交一致）。"
        entry["status"] = entry["status"] or "M"
        entry["status_label"] = STATUS_LABEL.get(entry["status"], "")
    if entry["hunks"] and not entry["status"]:
        entry["status"] = "M"
        entry["status_label"] = STATUS_LABEL["M"]
    return entry


def _summarize(files: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {
        "files": len(files),
        "additions": sum(int(item.get("additions") or 0) for item in files),
        "deletions": sum(int(item.get("deletions") or 0) for item in files),
        "committed": sum(1 for item in files if item.get("commit")),
        "uncommitted": sum(1 for item in files if not item.get("commit")),
        "hunks": sum(int(item.get("hunk_total") or 0) for item in files),
    }


async def _head_commit_files(repo: str, head: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Files touched by the explained commit, regardless of node attribution."""
    if not head:
        return []
    code, output = await _git(
        repo,
        ["diff-tree", "--no-commit-id", "--root", "--name-status", "-r", head["sha"]],
    )
    if code != 0:
        return []
    rows: list[dict[str, Any]] = []
    for line in output.splitlines():
        matched = NAME_STATUS_RE.match(line.strip())
        if not matched:
            continue
        status = matched.group("status")
        rows.append(
            {
                "path": matched.group("path").strip().strip('"'),
                "status": status,
                "status_label": STATUS_LABEL.get(status, ""),
            }
        )
    return rows


async def _worktree_conflicts(repo: str) -> dict[str, Any]:
    """Unmerged files with their conflict sections, whoever caused them.

    A merge conflict is worktree state, not a commit, so it is reported
    independently of per-stage attribution — otherwise a conflict raised by a
    loop's merge step would be invisible whenever no write receipt exists.
    """
    code, output = await _git(repo, ["diff", "--name-only", "--diff-filter=U"])
    if code != 0:
        return {"files": [], "total": 0, "truncated": False}
    names = [line.strip().strip('"') for line in output.splitlines()]
    names = [name for name in names if name]
    rows: list[dict[str, Any]] = []
    for path in names[:MAX_CONFLICT_FILES]:
        lines, truncated = read_text_lines(Path(repo) / path)
        sections = parse_conflict_markers(lines)
        rows.append(
            {
                "path": path,
                "name": Path(path).name,
                "status": "U",
                "status_label": STATUS_LABEL["U"],
                "conflict_total": len(sections),
                "conflicts": sections,
                "truncated": truncated,
            }
        )
    return {
        "files": rows,
        "total": len(names),
        "truncated": len(names) > MAX_CONFLICT_FILES,
    }


async def node_change_report(
    work_dir: str,
    receipts: Iterable[dict[str, Any]] | None,
    *,
    commit: str = "",
    max_files: int = MAX_FILES_PER_NODE,
) -> dict[str, Any]:
    """Build the per-node change explanation the canvas renders.

    ``receipts`` are ProcessFile receipts; only their ``node_id`` / ``path`` /
    ``node_attempt`` fields are trusted.  ``commit`` selects which commit to
    explain; empty means HEAD.
    """
    report: dict[str, Any] = {
        "project": str(work_dir),
        "git_available": False,
        "is_repo": False,
        "head": None,
        "commit_files": [],
        "conflicts": [],
        "conflicts_total": 0,
        "conflicts_truncated": False,
        "nodes": {},
        "unattributed": [],
        "errors": [],
    }
    code, output = await _git(str(work_dir), ["rev-parse", "--is-inside-work-tree"])
    if code != 0 or output.strip() != "true":
        report["errors"].append("当前项目不是 git 仓库，无法展示提交与差异。")
        return report
    report["is_repo"] = True
    report["git_available"] = True

    code, output = await _git(str(work_dir), ["rev-parse", "--show-toplevel"])
    repo = output.strip() if code == 0 and output.strip() else str(work_dir)

    head = await _commit_info(repo, commit or "HEAD")
    if head is None:
        report["errors"].append(f"读不到提交 {commit or 'HEAD'} 的信息。")
    else:
        report["head"] = head
    report["commit_files"] = await _head_commit_files(repo, head)
    conflicts = await _worktree_conflicts(repo)
    report["conflicts"] = conflicts["files"]
    report["conflicts_total"] = conflicts["total"]
    report["conflicts_truncated"] = conflicts["truncated"]

    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    order: list[str] = []
    for receipt in receipts or []:
        if not isinstance(receipt, dict):
            continue
        node_id = str(receipt.get("node_id") or "").strip()
        path = relative_path(repo, receipt.get("path"))
        if not node_id or not path:
            continue
        if node_id not in grouped:
            grouped[node_id] = {}
            order.append(node_id)
        previous = grouped[node_id].get(path)
        if previous is None or _receipt_rank(receipt) >= _receipt_rank(previous):
            grouped[node_id][path] = receipt

    attributed: set[str] = set()
    for node_id in order:
        items = sorted(grouped[node_id].items(), key=lambda item: item[0])
        entries = items[:max_files]
        files = [await _file_change(repo, path, receipt) for path, receipt in entries]
        attributed.update(path for path, _ in entries)
        report["nodes"][node_id] = {
            "node_id": node_id,
            "files": files,
            "omitted": max(0, len(items) - len(entries)),
            "summary": _summarize(files),
        }

    report["unattributed"] = [
        {**row, "reason": "本次提交改动了这个文件，但没有节点写入回执把它归属到某个阶段。"}
        for row in report["commit_files"][:MAX_UNATTRIBUTED_FILES]
        if row["path"] not in attributed
    ]
    return report
