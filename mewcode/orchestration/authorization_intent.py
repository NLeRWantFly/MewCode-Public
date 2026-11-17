"""Detect a file-authorization intent in a user message.

Verbal authorization grants nothing by itself: the runner injects user
supplements with an explicit "不自动授权文件、命令或外发操作" note, and audit
criteria never contain an "authorized" item. So the host surfaces the intent as
a one-click, explicit grant instead of leaving a sub-model to guess.

Detection has to be conservative. A missed offer costs one manual grant; a
false offer looks like the system asking to widen its own permissions, which is
exactly what the surrounding design forbids. Rules:

* Only a clause that contains an authorization phrase counts, and a negation in
  the **same clause** cancels it ("这个文件不要改" must not offer a grant).
* "不用问我 / don't ask me" is about asking, not about file access — ignored.
* A path is only accepted when it looks like one (separator or known extension);
  otherwise the offer asks which file, rather than guessing.
* Explicit permission wording ("允许/可以/授权你…", "you may edit…") is enough on
  its own — it may ask which file.
* A bare instruction ("改一下", "请修改…", "please update…") only counts when the
  same message names a file. Otherwise ordinary task messages like "请修改论文的
  结论段" would each look like the system angling for more permissions.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

MAX_SCAN_CHARS = 4000
MAX_PATHS = 5

# Chinese inserts pronouns between the verb of permission and the action
# ("允许你修改"), and colloquial forms add 一下/一把. Patterns, not substrings.
_WRITE = (
    r"(?:允许|准许|批准|同意|授权)(?:我|你|您|它|他|她)?(?:去)?(?:改|修改|改写|重写|写|写入|写进|编辑|覆盖|替换|更新|生成|创建|新建|落盘|存|保存|提交|访问)"
    r"|(?:可以|能|能够|不必|无需)(?:我|你|您|它)?(?:去)?(?:改|修改|改写|重写|写|编辑|覆盖|替换|更新|生成|创建|保存)"
    r"|(?:帮|替)(?:我|我们|你)?(?:改|修改|写|重写|更新)"
    r"|(?:直接|顺手|尽管|随便)(?:改|修改|写|编辑|覆盖|替换|更新|动|碰)"
    r"|(?:改|修改|写|编辑|覆盖|替换|更新|重写)(?:一下|一把|一份|一遍|掉|进去)"
    r"|(?:你|您)(?:来)?(?:改|修改|写|重写|编辑|更新)(?:一下|一遍)?"
    r"|(?:这个|那个|该|这些|那些)?(?:文件|文档|稿子|论文|代码|脚本|目录|文件夹)?(?:你|您)(?:来)?(?:改|修改|写|重写|编辑|更新)"
    r"|(?:改|修改|写|编辑)(?:掉|完|好)?(?:就|再)?(?:可以|行|没问题)"
    r"|you (?:may|can|are allowed to|are permitted to|have permission to)(?: go ahead and)? (?:edit|modify|write|change|update|rewrite|save|create|overwrite)"
    r"|(?:go ahead and|feel free to|please|do)(?: go ahead and)? (?:edit|modify|write|change|update|rewrite|save|create|overwrite)"
    r"|(?:i )?(?:authorize|permit|allow) you to (?:edit|modify|write|change|update|rewrite|save|create|overwrite)"
    r"|permission to (?:edit|modify|write|change|update|rewrite|save|create|overwrite)"
)
_READ = (
    r"(?:允许|准许|批准|同意|授权)(?:我|你|您|它)?(?:去)?(?:读|阅读|查看|看|打开|检查|审阅|访问)"
    r"|(?:可以|能|能够)(?:我|你|您)?(?:去)?(?:读|阅读|查看|看|打开|检查|审阅)"
    r"|(?:读|看|检查|审阅)(?:一下|一遍)"
    r"|you (?:may|can|are allowed to|are permitted to) (?:read|open|inspect|review|check)"
    r"|(?:feel free to|please) (?:read|open|inspect|review|check)"
    r"|permission to (?:read|open|inspect|review|check)"
)
NEGATIONS = (
    r"(?:不|没|未|别|勿|禁止|严禁|不许|不准)(?:要|得|能|可以|准|允许|准许|同意|授权|允许你|许)?(?:我|你|您|它)?(?:去)?(?:改|修改|改写|重写|写|写入|编辑|覆盖|替换|更新|动|碰|碰它|访问|保存|提交)"
    r"|(?:先|暂时|暂时先|暂时别|先别)(?:改|修改|写|动|碰|覆盖)"
    r"|(?:不要|别|不准|不许|禁止|严禁)(?:你|您)?(?:动|碰|改|修改|写|编辑|覆盖|替换|更新)"
    r"|(?:还没|没有|未曾|未曾明确)(?:允许|授权|批准|同意)"
    r"|(?:do not|don't|never|must not|may not|should not|not allowed to|not permitted to)(?: go ahead and)? (?:edit|modify|write|change|update|rewrite|save|create|overwrite|touch)"
    r"|without (?:my )?permission"
    r"|no (?:edits|changes|writes)"
)

# "asking me" is not "letting me touch files".
IGNORED_PHRASES = ("不用问我", "不用问", "别问我", "don't ask me", "no need to ask")

# Each block above is one implicitly concatenated pattern (the first piece has no
# leading bar, the rest do), so compile it directly — joining it again would
# split it into characters.
_WRITE_RE = re.compile(_WRITE, re.IGNORECASE)
_READ_RE = re.compile(_READ, re.IGNORECASE)
_NEGATION_RE = re.compile(NEGATIONS, re.IGNORECASE)

CLAUSE_SPLIT_RE = re.compile(r"[。！？!?；;\n]+|(?<=[，,])")

PATH_TOKEN_RE = re.compile(
    r"""(?<![\w./-])(
        [`"'](?P<quoted>[^`"'\n]{2,240})[`"']
        |
        (?P<bare>[\\/]*(?:[A-Za-z]:)?[\\/]*(?:[\w.@+\-]+[\\/])*[\w.@+\-]+\.[A-Za-z0-9]{1,8})
        |
        (?P<dir>[\\/]*(?:[A-Za-z]:)?[\\/]*(?:[\w.@+\-]+[\\/]){0,8}[\w.@+\-]+[\\/])
    )""",
    re.VERBOSE,
)

KNOWN_EXTENSIONS = {
    "py", "js", "ts", "tsx", "jsx", "cjs", "mjs", "json", "jsonl", "yaml", "yml", "toml",
    "ini", "cfg", "md", "txt", "rst", "tex", "bib", "cls", "sty", "csv", "tsv", "html",
    "htm", "css", "scss", "sh", "ps1", "bat", "c", "h", "cpp", "hpp", "cc", "java", "go",
    "rs", "rb", "php", "sql", "xml", "docx", "doc", "pdf", "pptx", "ppt", "xlsx", "xls",
    "png", "jpg", "jpeg", "gif", "svg", "webp", "log", "env", "lock", "cfg",
}

URL_RE = re.compile(r"https?://", re.IGNORECASE)


@dataclass
class AuthorizationIntent:
    """What the user seems to be authorizing, and how strongly."""

    access: str  # "read" | "read_write"
    paths: list[str] = field(default_factory=list)
    phrase: str = ""
    clause: str = ""
    needs_path: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _looks_like_path(value: str) -> bool:
    token = value.strip().strip("`\"'")
    if not token or len(token) > 240 or URL_RE.search(token) or "://" in token:
        return False
    # A trailing separator is common in prose ("改 docs/sub/") and must not hide
    # a degenerate token like "../".
    trimmed = token.rstrip("/\\")
    if not trimmed or token.startswith("-") or trimmed in {".", ".."}:
        return False
    if "/" in token or "\\" in token:
        return True
    extension = token.rsplit(".", 1)[-1].lower() if "." in token else ""
    return extension in KNOWN_EXTENSIONS


def _normalize_path(value: str) -> str:
    return value.strip().strip("`\"'").replace("\\", "/")


def extract_paths(text: str) -> list[str]:
    """Path-like tokens from a message, in order, de-duplicated."""
    found: list[str] = []
    seen: set[str] = set()
    for match in PATH_TOKEN_RE.finditer(text):
        raw = match.group("quoted") or match.group("bare") or match.group("dir") or ""
        if not _looks_like_path(raw):
            continue
        # "https://host/docs/a.md" must not yield "//host/docs/a.md".
        if "://" in text[max(0, match.start() - 8):match.end()]:
            continue
        token = _normalize_path(raw)
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(token)
        if len(found) >= MAX_PATHS:
            break
    return found


def _clause_match(clause: str, pattern: "re.Pattern[str]") -> str:
    match = pattern.search(clause)
    return match.group(0) if match else ""


def detect_authorization_intent(
    text: str,
    *,
    known_paths: list[str] | None = None,
) -> AuthorizationIntent | None:
    """Return the authorization the user seems to be granting, or None.

    ``known_paths`` (files the run already touched) is only used to accept a
    bare mention such as "这个文件" when exactly one candidate is unambiguous.
    """
    if not text:
        return None
    scanned = str(text)[:MAX_SCAN_CHARS]
    # "不用问我" broadens asking, not file access: drop it before matching so it
    # can neither trigger nor cancel an offer.
    for phrase in IGNORED_PHRASES:
        scanned = re.sub(re.escape(phrase), " ", scanned, flags=re.IGNORECASE)

    for clause in CLAUSE_SPLIT_RE.split(scanned):
        if not clause.strip():
            continue
        write_phrase = _clause_match(clause, _WRITE_RE)
        read_phrase = "" if write_phrase else _clause_match(clause, _READ_RE)
        phrase = write_phrase or read_phrase
        if not phrase:
            continue
        if _NEGATION_RE.search(clause):
            continue
        access = "read_write" if write_phrase else "read"
        paths = extract_paths(clause)
        if not paths:
            # Fall back to the whole message, but never adopt a path that the
            # user just protected in another clause.
            protected = set()
            for other in CLAUSE_SPLIT_RE.split(scanned):
                if _NEGATION_RE.search(other):
                    protected.update(path.lower() for path in extract_paths(other))
            paths = [path for path in extract_paths(scanned) if path.lower() not in protected]
        if not paths and known_paths:
            candidates = {_normalize_path(item): item for item in known_paths if item}
            if len(candidates) == 1:
                paths = [next(iter(candidates))]
        return AuthorizationIntent(
            access=access,
            paths=paths,
            phrase=phrase,
            clause=clause.strip()[:200],
            needs_path=not paths,
        )
    return None


def merge_offer(offers: list[dict[str, Any]], intent: AuthorizationIntent) -> list[dict[str, Any]]:
    """Keep one offer per path, upgrading read -> read_write, newest first."""
    if intent.needs_path:
        return offers
    merged = [dict(item) for item in offers or []]
    for path in intent.paths:
        existing = next(
            (item for item in merged if str(item.get("path", "")).lower() == path.lower()),
            None,
        )
        if existing is None:
            merged.append({"path": path, "access": intent.access})
            continue
        if intent.access == "read_write":
            existing["access"] = "read_write"
    return merged[-MAX_PATHS:]
