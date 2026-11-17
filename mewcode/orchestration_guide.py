"""One packaged component guide shared by the canvas, planner and built-in skill."""

from functools import lru_cache
from importlib.resources import files


@lru_cache(maxsize=1)
def orchestration_component_guide() -> str:
    return files("mewcode").joinpath(
        "skills", "builtins", "orchestration-diagrams", "references", "components.md"
    ).read_text(encoding="utf-8").strip()


@lru_cache(maxsize=1)
def orchestration_build_experience() -> str:
    """Share the packaged skill's reusable lessons with graph model calls."""
    return files("mewcode").joinpath(
        "skills", "builtins", "orchestration-diagrams", "references", "execution-readiness.md"
    ).read_text(encoding="utf-8").strip()


@lru_cache(maxsize=1)
def native_workflow_guide() -> str:
    return files('mewcode').joinpath('skills', 'builtins', 'orchestration-diagrams',
                                   'references', 'native-workflow.md').read_text(encoding='utf-8').strip()
