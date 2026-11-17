"""Bounded, human-readable choices shared by construction and stage inputs."""
from __future__ import annotations

import copy


def validate_options(value):
    if value is None:
        return None
    if not isinstance(value, list) or not 2 <= len(value) <= 6:
        raise ValueError("选择项须为 2–6 个清楚的方案")
    seen, recommended = set(), 0
    for option in value:
        if (not isinstance(option, dict) or not {"value", "label"}.issubset(option)
                or set(option) - {"value", "label", "description", "recommended"}):
            raise ValueError("选择项仅允许 value、label、description、recommended")
        for key, maximum in (("value", 500), ("label", 100), ("description", 300)):
            text = option.get(key, "")
            if (not isinstance(text, str) or len(text) > maximum or "\x00" in text
                    or key != "description" and (not text.strip() or text != text.strip())):
                raise ValueError("选择项的名称、含义或说明无效")
        if option["value"] in seen:
            raise ValueError("选择项的值不能重复")
        seen.add(option["value"])
        if type(option.get("recommended", False)) is not bool:
            raise ValueError("推荐标记须为布尔值")
        recommended += int(option.get("recommended", False))
    if recommended > 1:
        raise ValueError("同一问题最多推荐一个方案")
    return copy.deepcopy(value)


def validate_input_presentation(item):
    if "label" in item and (not isinstance(item["label"], str)
            or not 1 <= len(item["label"].strip()) <= 100 or "\x00" in item["label"]):
        raise ValueError("运行参数的显示名称须为 1–100 字")
    options = validate_options(item.get("options"))
    if options and item.get("default") not in (None, ""):
        if not isinstance(item["default"], str) or item["default"] not in {row["value"] for row in options}:
            raise ValueError("运行参数默认值不属于已声明的选择项")
    return options
