from __future__ import annotations

import re
from pathlib import Path

from mewcode.permissions.sensitive_paths import classify_sensitive_read_path

_DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"rm\s+-[a-z]*r[a-z]*f[a-z]*\s+/\s*$"), "递归强制删除根目录"),
    (re.compile(r"mkfs\."), "格式化磁盘"),
    (re.compile(r"dd\s+if=.*of=/dev/"), "直接写磁盘设备"),
    (re.compile(r"chmod\s+-R\s+777\s+/"), "递归修改根目录权限"),
    (re.compile(r":\(\)\{\s*:\|:&\s*\};:"), "fork bomb"),
    (re.compile(r">\s*/dev/sd"), "覆盖磁盘设备"),
    (re.compile(r"(?i)\b(format|format\.com)\s+[a-z]:"), "format a Windows volume"),
    (re.compile(r"(?i)\b(remove-item|del|erase)\b.*\b(recurse|/s)\b.*(?:[a-z]:\\|/)$"), "recursive device-level deletion"),
    (re.compile(r"(?i)\b(?:set-mppreference\s+-disablerealtimemonitoring|bcdedit\b.*disable|reg\s+add\b.*\\policies\\.*defender)"), "disable security controls"),
    (re.compile(r"(?i)(?:^|[;&|]\s*)\b(?:sudo|pkexec|runas)\b|\bstart-process\b.*\b-verb\s+runas\b"), "privilege escalation or elevation bypass"),
    (re.compile(r"(?i)\b(?:schtasks\s+/create|sc\s+create|new-service)\b.*(?:powershell|cmd|wscript|curl|wget)"), "suspicious persistence"),
    (re.compile(r"(?i)(?:rm|del|erase|remove-item|truncate)\b.*(?:\.mewcode[/\\]audit|decisions\.jsonl)"), "tamper with permission audit records"),
]


_AUTHORIZATION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\bgit\s+push\b.*(?:--force(?:-with-lease)?|-f)\b"), "force push rewrites remote history"),
    (re.compile(r"(?i)\b(?:terraform\s+destroy|pulumi\s+destroy|kubectl\s+delete\s+(?:namespace|cluster))\b"), "cloud resource destruction"),
    (re.compile(r"(?i)\b(?:aws\s+(?:cloudformation\s+delete-stack|ec2\s+terminate-instances)|gcloud\s+projects\s+delete|az\s+group\s+delete)\b"), "cloud resource destruction"),
    (re.compile(r"(?i)\b(?:drop\s+(?:database|schema)|truncate\s+table)\b"), "database-wide destructive operation"),
]

_REVIEW_PRIOR_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\b(?:delete|remove|remove-item)\b.*\bprod(?:uction)?\b"), "possible production deletion; verify actual target"),
    (re.compile(r"(?i)\b(?:curl|wget|invoke-webrequest|scp)\b.*(?:\.ssh\b|\bcredentials\b|\bid_rsa\b|\bapi[_-]?key\b|\btoken\b)"), "possible credential transfer; verify source, destination and payload"),
    (re.compile(r"(?i)\b(?:curl|wget)\b.*\|\s*(?:ba)?sh\b"), "remote script execution; inspect source and bounded effects"),
]


_SAFE_COMMANDS = frozenset({
    "ls", "dir", "pwd", "echo", "cat", "head", "tail", "wc",
    "which", "whereis", "whoami", "hostname", "uname",
    "date", "cal", "uptime", "df", "du", "free",
    "file", "stat", "readlink", "realpath", "basename", "dirname",
    "sort", "uniq", "tr", "cut", "grep", "egrep", "fgrep",
    "diff", "comm", "true", "false", "test",
    "git status", "git log", "git diff", "git show",
    "git rev-parse", "git ls-files", "git blame", "git stash list",
    "go version", "node -v", "npm -v", "python --version", "pip list",
    "cargo --version", "rustc --version", "java -version", "java --version",
})

# Auto mode deliberately keeps this list narrow. Commands outside it still
# need confirmation, so Auto never collapses into Bypass permissions.
_AUTO_LOCAL_COMMANDS = frozenset({
    "pytest", "python -m pytest", "py -m pytest", "uv run pytest",
    "python -m unittest", "py -m unittest",
    "npm test", "npm run test", "npm run lint", "npm run build", "npm run typecheck",
    "pnpm test", "pnpm run test", "pnpm run lint", "pnpm run build", "pnpm run typecheck",
    "yarn test", "yarn lint", "yarn build", "yarn typecheck",
    "ruff", "mypy", "eslint", "tsc", "python -m compileall",
    "go test", "go vet", "cargo test", "cargo check", "cargo build", "cargo clippy",
    "dotnet test", "dotnet build", "gradle test", "gradle build",
})


def is_safe_command(command: str) -> bool:
    trimmed = command.strip()
    if not trimmed:
        return False
    for ch in _SHELL_SMUGGLING:
        if ch in trimmed:
            return False
    for safe in _SAFE_COMMANDS:
        if trimmed == safe or trimmed.startswith(safe + " "):
            return True
    return False


def _normalize_workspace_python(command: str, project_root: str | Path | None) -> str:
    """Recognize only a Python executable inside this project's .venv."""
    if project_root is None:
        return command
    match = re.match(r"^(?P<executable>(?:\"[^\"]+\"|'[^']+'|\S+))\s+(?P<rest>.+)$", command)
    if not match:
        return command
    executable = match.group("executable").strip("\"'")
    normalized = executable.replace("\\", "/").lower()
    if not normalized.endswith("/.venv/scripts/python.exe"):
        return command
    if not _path_under_root(executable, Path(project_root).resolve()):
        return command
    return "python " + match.group("rest")


def is_auto_local_command(command: str, project_root: str | Path | None = None) -> bool:
    """Return whether ``command`` is a bounded local validation command.

    Shell chaining, redirects and substitutions are excluded before matching,
    which keeps an otherwise harmless test/build prefix from being used to
    smuggle in a second side-effecting command.
    """
    trimmed = command.strip()
    if not trimmed or any(
        token in trimmed for token in _SHELL_SMUGGLING
    ):
        return False
    trimmed = _normalize_workspace_python(trimmed, project_root)
    if project_root is not None and command_requires_auto_review(command, project_root)[0]:
        return False
    normalized = " ".join(trimmed.lower().split())
    return any(
        normalized == allowed or normalized.startswith(allowed + " ")
        for allowed in _AUTO_LOCAL_COMMANDS
    )


_SHELL_SMUGGLING = ("|", ";", "&", ">", "<", "$", "`", "\n", "\r", "%", "!", "(", ")", ",", "@", "{", "}")
_NETWORK_HINT = re.compile(
    r"(?i)\b(pip3?\s+install|uv\s+(?:pip\s+install|add)|conda\s+install|"
    r"curl|wget|invoke-webrequest|"
    r"https?://|ftp://|ssh\b|scp\b|npm\s+(?:i|install)|pnpm\s+(?:i|add|install)|"
    r"git\s+push)\b"
)
_UNBOUNDED_INTERPRETER = re.compile(
    r"(?i)\b(?:python|py|node|nodejs|bash|sh|zsh|pwsh|powershell|cmd)"
    r"(?:\d+(?:\.\d+)*)?\s+"
    r"(?:-c\b|--command\b|-command\b|-encodedcommand\b|--eval\b|-e\b|-file\b|-\s*$)"
)
_PATH_SUFFIXES = (
    ".py", ".txt", ".docx", ".md", ".json", ".xml", ".csv", ".html",
    ".pdf", ".yml", ".yaml",
)
_PYTHON_SCRIPT = re.compile(
    r"^(?:uv\s+run\s+)?(?:python|py)(?:\d+(?:\.\d+)*)?\s+(\S+\.py)(?:\s|$)",
    re.IGNORECASE,
)
_PANDOC_CMD = re.compile(r"^pandoc\b", re.IGNORECASE)


def _path_under_root(path_text: str, project_root: Path) -> bool:
    raw = path_text.strip().strip("\"'")
    if not raw or raw.startswith("-"):
        return False
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = project_root / candidate
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(project_root.resolve())
        return True
    except (OSError, ValueError):
        return False


def is_workspace_local_command(command: str, project_root: str | Path) -> bool:
    """Classify a workspace ``.py`` / local pandoc command by visible syntax.

    This is only a static *candidate* for a future Auto fast path. Script
    internals can read, write or connect elsewhere, so this predicate alone
    must never authorize execution or skip an unverified OS boundary.
    """
    trimmed = (command or "").strip()
    if not trimmed or any(token in trimmed for token in _SHELL_SMUGGLING):
        return False
    if _NETWORK_HINT.search(trimmed):
        return False
    root = Path(project_root).resolve()
    normalized = " ".join(_normalize_workspace_python(trimmed, root).split())

    script_match = _PYTHON_SCRIPT.match(normalized)
    if script_match:
        script = script_match.group(1)
        if not _path_under_root(script, root):
            return False
        script_path = Path(script.strip("\"'"))
        if not script_path.is_absolute():
            script_path = root / script_path
        if not script_path.is_file():
            return False
        # Remaining args that look like paths must stay in-workspace.
        remainder = normalized[script_match.end():].strip()
        for token in remainder.split():
            cleaned = token.strip("\"'")
            if not cleaned:
                continue
            if cleaned.startswith("-") and "=" in cleaned:
                cleaned = cleaned.split("=", 1)[1]
            elif cleaned.startswith("-"):
                continue
            if any(sep in cleaned for sep in ("/", "\\", ":")) or cleaned.endswith(
                (".py", ".txt", ".docx", ".md", ".json", ".xml", ".csv")
            ):
                if not _path_under_root(cleaned, root):
                    return False
        return True

    if _PANDOC_CMD.match(normalized):
        tokens = normalized.split()[1:]
        path_tokens = [
            token.strip("\"'")
            for token in tokens
            if token.strip("\"'")
            and not token.startswith("-")
            and token not in {"-o", "-t", "-f", "-s"}
        ]
        # Also treat "-o out.txt" style outputs.
        for index, token in enumerate(tokens):
            if token in {"-o", "--output"} and index + 1 < len(tokens):
                path_tokens.append(tokens[index + 1].strip("\"'"))
        if not path_tokens:
            return False
        return all(_path_under_root(token, root) for token in path_tokens)

    return False


def _looks_like_path_token(token: str) -> bool:
    cleaned = token.strip().strip("\"'")
    if not cleaned:
        return False
    if cleaned.startswith("-") and "=" in cleaned:
        cleaned = cleaned.split("=", 1)[1]
    elif cleaned.startswith("-"):
        return False
    if cleaned in {".", ".."}:
        return False
    if any(sep in cleaned for sep in ("/", "\\")):
        return True
    if len(cleaned) >= 2 and cleaned[1] == ":":
        return True
    return cleaned.lower().endswith(_PATH_SUFFIXES)


def command_requires_auto_review(command: str, project_root: str | Path) -> tuple[bool, str]:
    """Whether Auto must send this Bash line to the isolated reviewer.

    This is a command-string heuristic, not an OS sandbox. Hidden network or
    filesystem effects inside ``python -c`` / scripts can slip through; those
    unbounded forms are therefore reviewed rather than fast-pathed.
    """
    trimmed = (command or "").strip()
    if not trimmed:
        return True, "empty command"
    if any(token in trimmed for token in _SHELL_SMUGGLING):
        return True, "compound shell command"
    if _NETWORK_HINT.search(trimmed):
        return True, "network or package install"
    if _UNBOUNDED_INTERPRETER.search(trimmed):
        return True, "unbounded interpreter payload"
    root = Path(project_root)
    for token in trimmed.split():
        cleaned = token.strip("\"'")
        if not _looks_like_path_token(cleaned):
            continue
        if not _path_under_root(cleaned, root):
            return True, "path outside workspace"
    return False, ""


def command_sensitive_read_reason(command: str, project_root: str | Path) -> str:
    """Catch literal sensitive read targets before rules or model review.

    This is deliberately a literal guard, not a claim that arbitrary shell or
    script code has been sandboxed. Dynamic paths remain review-required until
    a native execution boundary exists.
    """
    if not re.search(r"(?i)(?:^|[\s;&|])(?:get-content|type|cat)\s", command or ""):
        return ""
    for token in re.findall(r"\"[^\"]*\"|'[^']*'|\S+", command):
        candidate = token.strip().strip("\"'(),;")
        if not candidate:
            continue
        if "=" in candidate and candidate.startswith("-"):
            candidate = candidate.split("=", 1)[1]
        elif candidate.startswith("-"):
            continue
        verdict = classify_sensitive_read_path(candidate, workspace_root=project_root)
        if verdict.sensitive:
            return verdict.reason
    return ""


def auto_hard_deny_reason(command: str) -> str:
    """Compatibility accessor for invariant hazards only, never heuristics."""
    return DangerousCommandDetector().detect(command)[1]


def command_authorization_reason(command: str) -> str:
    for pattern, reason in _AUTHORIZATION_PATTERNS:
        if pattern.search(command or ""):
            return reason
    return ""


def command_review_prior(command: str) -> str:
    return "; ".join(reason for pattern, reason in _REVIEW_PRIOR_PATTERNS
                     if pattern.search(command or ""))


class DangerousCommandDetector:


    def __init__(self, extra_patterns: list[tuple[str, str]] | None = None) -> None:
        self._patterns = list(_DANGEROUS_PATTERNS)
        if extra_patterns:
            for regex_str, reason in extra_patterns:
                self._patterns.append((re.compile(regex_str), reason))


    def detect(self, command: str) -> tuple[bool, str]:
        for pattern, reason in self._patterns:
            if pattern.search(command):
                return True, reason
        return False, ""
