"""Validate maintenance model edits against the host's configured routes."""
from __future__ import annotations


def patch_node_model(node, patch, routes):
    if node.get("kind") == "gate":
        raise ValueError("人工确认门禁无需模型绑定：" + node["node_id"])
    if not isinstance(patch, dict) or not patch or set(patch) - {"provider_key", "model_id", "effort"}:
        raise ValueError("model 只允许 provider_key、model_id、effort，且必须有实际修改字段。")
    binding = {**(node.get("model") or {}), **patch}
    for key in ("provider_key", "model_id", "effort"):
        value = binding.get(key, "")
        if not isinstance(value, str) or len(value) > 200:
            raise ValueError("模型绑定字段必须是有效文本：" + key)
        binding[key] = value.strip()
    if not binding["provider_key"] or not binding["model_id"]:
        raise ValueError("请提供完整 provider_key 和 model_id，不能清空模型绑定。")
    if binding["effort"] == "auto":
        binding["effort"] = ""
    if not isinstance(routes, list):
        raise ValueError("当前模型配置未就绪，无法校验节点模型修改。")
    matches = [model for route in routes if isinstance(route, dict) and route.get("provider_key") == binding["provider_key"]
               for model in route.get("models", []) if isinstance(model, dict) and model.get("model_id") == binding["model_id"]]
    if not matches:
        raise ValueError("节点模型未在当前可用配置中：" + binding["model_id"])
    if binding["effort"] and not any(binding["effort"] in model.get("efforts", []) for model in matches):
        raise ValueError("节点模型不支持指定 effort：" + binding["model_id"] + " / " + binding["effort"])
    return {key: binding[key] for key in ("provider_key", "model_id", "effort")}


def validate_changed_models(before, after, routes):
    old = before.as_dict() if hasattr(before, "as_dict") else before
    new = after.as_dict() if hasattr(after, "as_dict") else after
    original = {node["node_id"]: node for node in old["nodes"]}
    for node in new["nodes"]:
        if node.get("model") != original.get(node["node_id"], {}).get("model"):
            patch_node_model(node, node.get("model"), routes)
