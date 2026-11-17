from __future__ import annotations

import tempfile
import os
from pathlib import Path

from mewcode.permissions.sensitive_paths import classify_sensitive_read_path

PATH_ESCAPE_REASON = "相对路径或目录联接逃逸了当前工作区；请明确指定普通外部路径。"


class PathSandbox:
    """Path allowlist for Read/Write tools. Not an OS/kernel command sandbox.

    Temp and the project root are writable/readable here. This does not
    isolate subprocesses; shell execution has its own approval and backend policy.
    """


    def __init__(
        self,
        project_root: str,
        extra_allowed: list[str] | None = None,
        file_grants: dict[str, bool] | None = None,
        dir_grants: dict[str, bool] | None = None,
    ) -> None:
        root = Path(project_root).resolve()
        # Owned by a trusted conversation runtime. Sharing these dictionaries lets
        # an explicit UI grant reach already-created nodes without widening roots.
        self.file_grants = file_grants if file_grants is not None else {}
        # Directory grants exist because a whole folder cannot be expressed as a
        # file grant: Glob/Grep/ReadFile over a directory outside the workspace
        # need the directory itself, and "grant a sibling directory" must stay
        # impossible. Values are writable flags; read-only is the default.
        self.dir_grants = dir_grants if dir_grants is not None else {}
        self._allowed_roots: list[Path] = [root, Path(tempfile.gettempdir()).resolve()]
        if extra_allowed:
            for p in extra_allowed:
                self._allowed_roots.append(Path(p).resolve())
        # SkillLoader advertises user skills outside the workspace. Their
        # instructions and supporting resources are readable, never implicitly
        # writable. Do not grant access to the rest of the user configuration.
        from mewcode.skills.loader import USER_SKILLS_DIR

        self._read_only_roots = [Path(USER_SKILLS_DIR).expanduser().resolve()]


    @property
    def project_root(self) -> Path:
        return self._allowed_roots[0]


    def check(self, path: str, *, read_only: bool = False) -> tuple[bool, str]:
        if read_only:
            sensitive = classify_sensitive_read_path(path, workspace_root=self.project_root)
            if sensitive.sensitive:
                return False, f"敏感位置读取被硬规则禁止：{sensitive.reason}。请使用脱敏副本或结构化宿主工具。"
        p = Path(path).expanduser()
        # Windows treats `/folder` as drive-rooted, not `is_absolute()`. It is
        # still an explicit external target, unlike `..\folder` traversal.
        relative_request = not p.is_absolute() and not p.anchor
        if not p.is_absolute():
            p = self.project_root / p
        abs_path = p.absolute()

        try:
            real_path = abs_path.resolve(strict=True)
        except OSError:
            ancestor = abs_path
            while True:
                try:
                    exists = ancestor.exists()
                except OSError:
                    return False, f"无法解析路径: {path}"
                if exists:
                    break
                parent = ancestor.parent
                if parent == ancestor:
                    return False, f"无法解析路径: {path}"
                ancestor = parent
            try:
                resolved_ancestor = ancestor.resolve(strict=True)
            except OSError:
                return False, f"无法解析路径: {path}"
            real_path = (resolved_ancestor / abs_path.relative_to(ancestor)).resolve()

        if read_only:
            # Full-disk *explicit* reads are allowed, but an apparent
            # workspace-relative read must not silently escape through `..`
            # or a junction. The caller can name an ordinary external path
            # explicitly if that is truly intended.
            if ((relative_request or abs_path.is_relative_to(self.project_root))
                    and not real_path.is_relative_to(self.project_root)):
                return False, PATH_ESCAPE_REASON
            # Re-evaluate the physical target after resolving a symlink/junction.
            sensitive = classify_sensitive_read_path(real_path, workspace_root=self.project_root)
            if sensitive.sensitive:
                return False, f"敏感位置读取被硬规则禁止：{sensitive.reason}。请使用脱敏副本或结构化宿主工具。"

            return True, ""

        grant = self.file_grants.get(os.path.normcase(str(real_path)))
        if grant is not None and (read_only or grant):
            return True, ""
        for granted_dir, writable in self.dir_grants.items():
            if not (read_only or writable):
                continue
            try:
                real_path.relative_to(Path(granted_dir))
                return True, ""
            except (ValueError, OSError):
                continue
        roots = self._allowed_roots + (self._read_only_roots if read_only else [])
        for root in roots:
            try:
                real_path.relative_to(root)
                return True, ""
            except ValueError:
                continue

        return False, (
            f"路径 {path} 超出沙箱当前写入范围。若需要修改具体文件，请通过 "
            "AskUserQuestion(purpose='authorization', file_access={file_path: 该文件绝对路径, "
            "access: 'read_write', reason: 具体用途}) 在对话中申请单文件授权；"
            "宿主会展示范围并记录用户确认，无需用户寻找设置。也可使用工作区内的目标。"
        )

    def check_external_write(self, path: str) -> tuple[bool, str]:
        """Validate an explicit external target before Auto review or full access.

        This does not create a grant. Relative traversal, credential targets,
        OS files and permission authority remain outside this convenience path.
        """
        ok, reason = self.check(path, read_only=True)
        if not ok:
            return ok, reason
        try:
            candidate = Path(path).expanduser()
            if not candidate.is_absolute():
                candidate = self.project_root / candidate
            target = candidate.resolve(strict=False)
            if target.name.lower() in {
                "permissions.grants.json", "permissions.yaml", "permissions.local.yaml",
            } or (target.parent.name.lower() == "rules" and target.suffix.lower() == ".rules"):
                return False, "权限规则文件需通过宿主授权入口修改。"
            protected_roots = (
                [Path(value).resolve() for key in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)")
                 if (value := os.environ.get(key))]
                if os.name == "nt" else
                [Path(value).resolve() for value in ("/etc", "/usr", "/bin", "/sbin", "/boot", "/dev", "/proc", "/sys")]
            )
            if target == Path(target.anchor) or any(target.is_relative_to(root) for root in protected_roots):
                return False, "系统目录写入不属于普通外部文件操作。"
        except (OSError, RuntimeError, ValueError):
            return False, "无法验证外部写入路径。"
        return True, ""

    def grant_file(self, path: str, *, writable: bool = False) -> str:
        """Host/UI-only exact existing-file grant, never derived from tool text."""
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            raise ValueError("文件授权需要绝对路径")
        resolved = candidate.resolve(strict=True)
        if not resolved.is_file():
            raise ValueError("请选择一个现有文件；文件授权不包含目录")
        self.file_grants[os.path.normcase(str(resolved))] = bool(writable)
        return str(resolved)

    def grant_approved_path(self, path: str, *, read_only: bool = True) -> str:
        """Record the access a user just approved for one tool call.

        Existing directory → directory grant. Existing file → file grant.
        A not-yet-existing target grants only that exact file path. It must
        never silently authorize the nearest existing parent directory.
        """
        candidate = Path(str(path or "")).expanduser()
        if not candidate.is_absolute():
            candidate = self.project_root / candidate
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError):
            return ""
        try:
            if resolved.is_dir():
                return self.grant_directory(str(resolved), writable=not read_only)
            if resolved.exists():
                return self.grant_file(str(resolved), writable=not read_only)
            if read_only:
                return ""
            self.file_grants[os.path.normcase(str(resolved))] = True
            return str(resolved)
        except (ValueError, OSError):
            return ""

    def grant_directory(self, path: str, *, writable: bool = False) -> str:
        """Host/UI-only directory grant, never derived from tool text.

        Covers the directory and everything under it; a sibling directory stays
        blocked. Read-only unless the caller explicitly asks for writes.
        """
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            raise ValueError("目录授权需要绝对路径")
        resolved = candidate.resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError("请选择一个现有目录；目录授权只接受目录")
        if resolved == Path(resolved.anchor):
            raise ValueError("不能把整个磁盘加为授权目录，请选择具体目录")
        self.dir_grants[os.path.normcase(str(resolved))] = bool(writable)
        return str(resolved)
