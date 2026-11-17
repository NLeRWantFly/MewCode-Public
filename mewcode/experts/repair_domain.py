"""Choose repair guidance from the task and deliverables, not one past incident."""
import re


def repair_domain(task="", files=()):
    text = str(task).casefold()
    paths = " ".join(str(path).casefold() for path in files)
    if re.search(r"论文|图注|排版|\b(?:docx|latex|manuscript)\b", text):
        return "document"
    if re.search(r"\b(?:pytest|unittest|npm|bug|code|backend|frontend|api|python|react)\b|代码|单元测试|失败测试|前端|后端", text):
        return "code"
    if re.search(r"(?:^|[ /\\])(?:pyproject\.toml|package\.json|cargo\.toml|go\.mod|pom\.xml)(?:\b|$)", paths):
        return "code"
    if re.search(r"\.(?:docx|tex|pdf)(?:\b|$)", paths):
        return "document"
    if re.search(r"\.(?:py|js|jsx|ts|tsx|go|rs|java|cs|cpp|c)(?:\b|$)|pyproject\.toml|package\.json|cargo\.toml", paths):
        return "code"
    if re.search(r"\.(?:png|jpg|jpeg|svg|webp)(?:\b|$)|图片|图像|视觉", paths + " " + text):
        return "visual"
    return "general"
