from __future__ import annotations

import re
from pathlib import Path

import yaml

from mewcode.skills.parser import VALID_CONTEXTS, VALID_MODES, VALID_NAME_RE

ALLOWED_PROPERTIES = {
    "name",
    "description",
    "license",
    "allowed-tools",
    "allowedTools",
    "metadata",
    "compatibility",
    "mode",
    "context",
    "model",
}

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)


def validate_skill(skill_path: str | Path) -> tuple[bool, str]:
    skill_path = Path(skill_path)
    skill_md = skill_path / "SKILL.md"
    if not skill_md.is_file():
        return False, "SKILL.md not found"

    content = skill_md.read_text(encoding="utf-8")
    if not content.lstrip().startswith("---"):
        return False, "No YAML frontmatter found"

    match = _FRONTMATTER_RE.match(content.lstrip())
    if not match:
        return False, "Invalid frontmatter format"

    try:
        frontmatter = yaml.safe_load(match.group(1))
        if not isinstance(frontmatter, dict):
            return False, "Frontmatter must be a YAML dictionary"
    except yaml.YAMLError as e:
        return False, f"Invalid YAML in frontmatter: {e}"

    unexpected = set(frontmatter.keys()) - ALLOWED_PROPERTIES
    if unexpected:
        return False, (
            "Unexpected key(s) in SKILL.md frontmatter: "
            f"{', '.join(sorted(str(k) for k in unexpected))}. "
            f"Allowed properties are: {', '.join(sorted(ALLOWED_PROPERTIES))}"
        )

    if "name" not in frontmatter:
        return False, "Missing 'name' in frontmatter"
    if "description" not in frontmatter:
        return False, "Missing 'description' in frontmatter"

    name = frontmatter.get("name", "")
    if not isinstance(name, str):
        return False, f"Name must be a string, got {type(name).__name__}"
    name = name.strip()
    if not VALID_NAME_RE.match(name):
        return False, (
            f"Name '{name}' must be lowercase letters, digits, and hyphens, "
            "starting with a letter"
        )
    if len(name) > 64:
        return False, f"Name is too long ({len(name)} characters). Maximum is 64 characters."

    description = frontmatter.get("description", "")
    if not isinstance(description, str):
        return False, f"Description must be a string, got {type(description).__name__}"
    description = description.strip()
    if "<" in description or ">" in description:
        return False, "Description cannot contain angle brackets (< or >)"
    if len(description) > 1024:
        return False, (
            f"Description is too long ({len(description)} characters). "
            "Maximum is 1024 characters."
        )

    mode = frontmatter.get("mode", "inline")
    if mode not in VALID_MODES:
        return False, f"Invalid mode '{mode}': must be one of {sorted(VALID_MODES)}"

    context = frontmatter.get("context", "full")
    if context not in VALID_CONTEXTS:
        return False, (
            f"Invalid context '{context}': must be one of {sorted(VALID_CONTEXTS)}"
        )

    compatibility = frontmatter.get("compatibility", "")
    if compatibility:
        if not isinstance(compatibility, str):
            return False, (
                f"Compatibility must be a string, got {type(compatibility).__name__}"
            )
        if len(compatibility) > 500:
            return False, (
                f"Compatibility is too long ({len(compatibility)} characters). "
                "Maximum is 500 characters."
            )

    return True, "Skill is valid!"
