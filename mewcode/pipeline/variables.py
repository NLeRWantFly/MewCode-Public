"""Pipeline 变量系统和表达式求值。

支持：
- {{input.xxx}} — 触发器输入
- {{node_id.output}} — 上游节点输出
- {{env.xxx}} — 环境变量
- {{loop.index}} / {{loop.item}} — 循环上下文
- 简单条件表达式求值
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable

log = logging.getLogger(__name__)

# 变量引用正则：{{ variable.path }}
VAR_PATTERN = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


class VariableContext:
    """变量上下文，管理 pipeline 执行过程中的所有变量。"""

    def __init__(
        self,
        trigger_input: dict[str, Any] | None = None,
        *,
        on_missing: Callable[[str], None] | None = None,
    ) -> None:
        self._trigger_input = trigger_input or {}
        self._outputs: dict[str, Any] = {}
        self._loop_context: dict[str, Any] = {}
        self._custom: dict[str, Any] = {}
        # 插值时遇到无法解析的引用的回调（收到引用路径，如 "n_xxx.output"）。
        # 由执行器挂接以发 warning 事件；为 None 时仅记日志。
        self.on_missing = on_missing

    # ------------------------------------------------------------------
    # 变量设置
    # ------------------------------------------------------------------

    def set_output(self, node_id: str, value: Any) -> None:
        """设置节点输出。"""
        self._outputs[node_id] = value

    def set_loop_context(self, index: int, item: Any) -> None:
        """设置循环上下文。"""
        self._loop_context = {"index": index, "item": item}

    def clear_loop_context(self) -> None:
        """清除循环上下文。"""
        self._loop_context = {}

    def set_custom(self, key: str, value: Any) -> None:
        """设置自定义变量。"""
        self._custom[key] = value

    # ------------------------------------------------------------------
    # 变量获取
    # ------------------------------------------------------------------

    def get(self, path: str) -> Any:
        """根据路径获取变量值。

        路径格式：
        - input.xxx — 触发器输入
        - env.xxx — 环境变量
        - loop.index / loop.item — 循环上下文
        - node_id.xxx — 节点输出（支持嵌套属性）
        - xxx — 自定义变量或节点输出
        """
        parts = path.split(".")
        root = parts[0]

        if root == "input":
            return self._get_nested(self._trigger_input, parts[1:])
        elif root == "env":
            if len(parts) > 1:
                return os.environ.get(parts[1], "")
            return ""
        elif root == "loop":
            if len(parts) > 1:
                return self._loop_context.get(parts[1])
            return self._loop_context
        elif root in self._custom:
            return self._get_nested(self._custom[root], parts[1:])
        elif root in self._outputs:
            return self._get_nested(self._outputs[root], parts[1:])
        else:
            # 尝试作为节点 ID 查找
            return None

    def _get_nested(self, obj: Any, parts: list[str]) -> Any:
        """递归获取嵌套属性。"""
        current = obj
        for part in parts:
            if current is None:
                return None
            if isinstance(current, dict):
                current = current.get(part)
            elif hasattr(current, part):
                current = getattr(current, part)
            else:
                return None
        return current

    # ------------------------------------------------------------------
    # 模板插值
    # ------------------------------------------------------------------

    @staticmethod
    def collect_asset_refs(value: Any) -> list[dict[str, str]]:
        refs: list[dict[str, str]] = []
        if isinstance(value, dict):
            asset = value.get("asset")
            mime = value.get("mime")
            if isinstance(asset, str) and asset.startswith("sha256:") and isinstance(mime, str):
                refs.append({
                    "asset": asset, "mime": mime,
                    "name": str(value.get("name", "")),
                })
            else:
                for child in value.values():
                    refs.extend(VariableContext.collect_asset_refs(child))
        elif isinstance(value, list):
            for child in value:
                refs.extend(VariableContext.collect_asset_refs(child))
        return refs

    def extract_asset_refs(self, *templates: str) -> list[dict[str, str]]:
        """Resolve asset-valued variable references outside text interpolation."""
        refs: list[dict[str, str]] = []
        seen: set[str] = set()
        for template in templates:
            for match in VAR_PATTERN.finditer(template or ""):
                for ref in self.collect_asset_refs(self.get(match.group(1))):
                    if ref["asset"] not in seen:
                        refs.append(ref)
                        seen.add(ref["asset"])
        return refs

    def interpolate(
        self,
        template: str,
        *,
        value_formatter: Callable[[str, Any], str] | None = None,
    ) -> str:
        """对模板字符串进行变量插值。

        无法解析的引用替换为空串（不把 {{...}} 原样拼进结果），
        并通过 on_missing 回调上报引用路径。

        Args:
            template: 包含 {{variable}} 占位符的字符串。

        Returns:
            插值后的字符串。
        """
        def replace_var(match: re.Match) -> str:
            path = match.group(1)
            value = self.get(path)
            if value is None:
                log.warning("Variable '%s' unresolved, replaced with empty string", path)
                if self.on_missing is not None:
                    try:
                        self.on_missing(path)
                    except Exception as e:
                        log.debug("on_missing callback failed: %s", e)
                return ""
            if self.collect_asset_refs(value):
                return ""
            if value_formatter is not None:
                try:
                    return value_formatter(path, value)
                except Exception as e:
                    log.warning(
                        "Variable formatter failed for '%s', using inline value: %s",
                        path, e,
                    )
            if isinstance(value, (dict, list)):
                try:
                    return json.dumps(value, ensure_ascii=False)
                except Exception:
                    return str(value)
            return str(value)

        return VAR_PATTERN.sub(replace_var, template)

    def interpolate_dict(self, data: dict[str, Any]) -> dict[str, Any]:
        """对字典中的所有字符串值进行插值。"""
        result = {}
        for key, value in data.items():
            if isinstance(value, str):
                result[key] = self.interpolate(value)
            elif isinstance(value, dict):
                result[key] = self.interpolate_dict(value)
            else:
                result[key] = value
        return result


# ---------------------------------------------------------------------------
# 条件表达式求值
# ---------------------------------------------------------------------------


class ExpressionEvaluator:
    """简单条件表达式求值器。

    支持的语法：
    - {{var}} == 'value'
    - {{var}} != 'value'
    - {{var}} > 10
    - {{var}} >= 10
    - {{var}} < 10
    - {{var}} <= 10
    - {{var}} contains 'substring'
    - expr1 and expr2
    - expr1 or expr2
    - not expr
    """

    def __init__(self, ctx: VariableContext) -> None:
        self._ctx = ctx

    def evaluate(self, expression: str) -> bool:
        """求值条件表达式。

        Args:
            expression: 条件表达式字符串。

        Returns:
            布尔结果。
        """
        # 先进行变量插值
        interpolated = self._ctx.interpolate(expression)
        log.debug("Evaluating expression: %s -> %s", expression, interpolated)

        try:
            return self._eval_or(interpolated.strip())
        except Exception as e:
            log.warning("Expression evaluation failed: %s, error: %s", expression, e)
            return False

    def _eval_or(self, expr: str) -> bool:
        """处理 or 运算。"""
        # 简单分割（不处理括号内的 or）
        parts = self._split_by_keyword(expr, " or ")
        if len(parts) > 1:
            return any(self._eval_and(p.strip()) for p in parts)
        return self._eval_and(expr)

    def _eval_and(self, expr: str) -> bool:
        """处理 and 运算。"""
        parts = self._split_by_keyword(expr, " and ")
        if len(parts) > 1:
            return all(self._eval_not(p.strip()) for p in parts)
        return self._eval_not(expr)

    def _eval_not(self, expr: str) -> bool:
        """处理 not 运算。"""
        if expr.startswith("not "):
            return not self._eval_comparison(expr[4:].strip())
        return self._eval_comparison(expr)

    def _eval_comparison(self, expr: str) -> bool:
        """处理比较运算。"""
        # 尝试各种比较运算符
        operators = [
            ("==", lambda a, b: a == b),
            ("!=", lambda a, b: a != b),
            (">=", lambda a, b: self._to_num(a) >= self._to_num(b)),
            ("<=", lambda a, b: self._to_num(a) <= self._to_num(b)),
            (">", lambda a, b: self._to_num(a) > self._to_num(b)),
            ("<", lambda a, b: self._to_num(a) < self._to_num(b)),
            (" contains ", lambda a, b: str(b) in str(a)),
            (" in ", lambda a, b: str(a) in str(b)),
        ]

        for op, func in operators:
            if op in expr:
                parts = expr.split(op, 1)
                if len(parts) == 2:
                    left = self._parse_value(parts[0].strip())
                    right = self._parse_value(parts[1].strip())
                    try:
                        return func(left, right)
                    except Exception:
                        return False

        # 没有比较运算符，尝试作为布尔值
        return self._parse_bool(expr)

    def _parse_value(self, s: str) -> Any:
        """解析值（字符串、数字、布尔）。"""
        s = s.strip()

        # 字符串字面量
        if (s.startswith("'") and s.endswith("'")) or (
            s.startswith('"') and s.endswith('"')
        ):
            return s[1:-1]

        # 布尔值
        if s.lower() == "true":
            return True
        if s.lower() == "false":
            return False
        if s.lower() == "none" or s.lower() == "null":
            return None

        # 数字
        try:
            if "." in s:
                return float(s)
            return int(s)
        except ValueError:
            pass

        # 原样返回
        return s

    def _parse_bool(self, s: str) -> bool:
        """解析布尔值。"""
        s = s.strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off", "", "none", "null"):
            return False
        # 非空字符串视为 True
        return bool(s)

    def _to_num(self, v: Any) -> float:
        """转换为数字。"""
        if isinstance(v, (int, float)):
            return float(v)
        try:
            return float(str(v))
        except ValueError:
            return 0.0

    def _split_by_keyword(self, expr: str, keyword: str) -> list[str]:
        """按关键字分割表达式（简单实现，不处理引号内的关键字）。"""
        # 简单实现：直接分割
        return expr.split(keyword)
