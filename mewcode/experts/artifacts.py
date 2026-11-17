"""Run-scoped skill artifacts for expert sub-agents.

Sub-agents can author a skill to solve their own task. Every artifact lives
under one run-scoped directory, passes a real loading gate (parse + validate
the SKILL.md) before it can be approved, and needs an explicit approval before
execution. Validation is a real load, not a shape check - the analog of DSH's
standingKeyFor.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mewcode.skills.parser import SkillDef, SkillParseError, parse_skill_file

SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SKILL_FILENAME = "SKILL.md"

# Artifact lifecycle: draft -> validated -> approved (executable) | rejected
STATE_DRAFT = "draft"
STATE_VALIDATED = "validated"
STATE_APPROVED = "approved"
STATE_REJECTED = "rejected"


class ArtifactError(ValueError):
    """Base error for the artifact layer."""


class ArtifactValidationError(ArtifactError):
    """The artifact failed the real loading gate."""


class ArtifactNotExecutable(ArtifactError):
    """The artifact was invoked without the required approval."""


@dataclass
class SkillArtifact:
    """One run-scoped skill plus its gate state.

    Lifecycle: write -> draft; validate -> validated (or rejected on failure);
    approve -> approved (executable). Execution is hard-gated on the approved
    state - there is no path that skips the gates.
    """

    name: str
    path: Path
    state: str = STATE_DRAFT
    definition: SkillDef | None = None
    approvals: list[dict[str, str]] = field(default_factory=list)
    escalation: str = ""

    @property
    def is_executable(self) -> bool:
        return self.state == STATE_APPROVED

    def validate(self) -> SkillDef:
        """Run the real loading gate: parse and validate the SKILL.md."""
        try:
            definition = parse_skill_file(self.path)
        except SkillParseError as exc:
            self.state = STATE_REJECTED
            raise ArtifactValidationError(
                f"skill {self.name} 校验失败: {exc}"
            ) from exc
        if definition.name != self.name:
            self.state = STATE_REJECTED
            raise ArtifactValidationError(
                f"skill {self.name} 校验失败: 名字与 frontmatter 不一致"
            )
        self.definition = definition
        self.state = STATE_VALIDATED
        return definition

    def request_approval(self, escalation: str = "") -> None:
        """Mark awaiting approval; records where the request may escalate to."""
        if self.state != STATE_VALIDATED:
            raise ArtifactError(f"skill {self.name} 未通过校验，不能请求批准")
        self.escalation = escalation

    def approve(self, by: str, reason: str = "") -> None:
        if self.state != STATE_VALIDATED:
            raise ArtifactError(f"skill {self.name} 未通过校验，不能批准")
        self.state = STATE_APPROVED
        self.approvals.append({"by": by, "reason": reason})

    def reject(self, by: str, reason: str = "") -> None:
        self.state = STATE_REJECTED
        self.approvals.append({"by": by, "reason": reason or "rejected"})

    def require_executable(self) -> None:
        """The hard gate: no approval, no execution."""
        if not self.is_executable:
            raise ArtifactNotExecutable(f"skill {self.name} 未经批准，不得执行")


class ExpertArtifactWorkspace:
    """Run-scoped store for sub-agent skill artifacts.

    All writes land under one root. Names are restricted to a safe slug
    pattern and resolved paths are verified to stay inside the root, so a
    sub-agent can never write outside the run scope. When the run ends the
    directory is recycled - artifacts never leak into global state.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    @classmethod
    def for_run(cls, base: str | Path, run_id: str) -> "ExpertArtifactWorkspace":
        """The default layout: <base>/experts/<run_id>/artifacts/."""
        return cls(Path(base) / "experts" / run_id / "artifacts")

    def artifact_path(self, name: str) -> Path:
        self._check_name(name)
        path = (self.root / name / SKILL_FILENAME).resolve()
        self._check_inside(path)
        return path

    def write_skill(self, name: str, content: str) -> SkillArtifact:
        path = self.artifact_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return SkillArtifact(name=name, path=path, state=STATE_DRAFT)

    def load_skill(self, name: str) -> SkillArtifact:
        path = self.artifact_path(name)
        if not path.is_file():
            raise ArtifactError(f"skill {name} 不存在")
        return SkillArtifact(name=name, path=path, state=STATE_DRAFT)

    def list_artifacts(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            entry.name
            for entry in self.root.iterdir()
            if entry.is_dir() and (entry / SKILL_FILENAME).is_file()
        )

    def remove_artifact(self, name: str) -> None:
        self._check_name(name)
        directory = (self.root / name).resolve()
        self._check_inside(directory)
        if directory.is_dir():
            shutil.rmtree(directory)

    def recycle(self) -> None:
        """Recycle the whole run-scoped directory; a no-op when absent."""
        if self.root.is_dir():
            shutil.rmtree(self.root)

    def verify_within_sandbox(self, sandbox: Any) -> None:
        """Hard check: the artifact root must lie inside the sandbox roots."""
        ok, reason = sandbox.check(str(self.root))
        if not ok:
            raise ArtifactError(f"skill 目录越出沙箱: {reason}")

    def _check_name(self, name: str) -> None:
        if not isinstance(name, str) or not SKILL_NAME_PATTERN.match(name):
            raise ArtifactError(f"非法 skill 名字: {name!r}")

    def _check_inside(self, path: Path) -> None:
        try:
            path.relative_to(self.root)
        except ValueError:
            raise ArtifactError("skill 路径越出运行级目录") from None
