"""别名解析 — 支持 latest / final 特殊引用与多键名解析。

_LATEST_REFS 包含 latest / latest_layer / latest_result / 上一步结果 等别名。
_FINAL_REFS 包含 final / final_result / final_output / 最终结果 等别名。
"""

from __future__ import annotations

from typing import Any

#: 指向"最新图层"的别名集合
_LATEST_REFS = frozenset({
    "latest", "latest_layer", "latest_result",
    "上一步结果", "上个结果", "最新结果", "最新输出",
})

#: 指向"最终结果"的别名集合
_FINAL_REFS = frozenset({
    "final", "final_result", "final_output",
    "最终结果", "最终输出", "输出结果",
})


def resolve_ref(ref: str, layers: dict[str, Any], aliases: dict[str, str]) -> str | None:
    """解析 ref → layer_id；返回 None 表示未找到。

    解析顺序：
    1. 检查是否为 _LATEST_REFS 成员 → 返回 layers 的最后一个 key
    2. 检查是否为 _FINAL_REFS 成员 → 返回 role=final 的最后一个图层
    3. 在 aliases 中精确匹配（原始 + 小写）
    4. 在 aliases 中做大小写不敏感的 key 遍历匹配
    """
    lowered = ref.lower().strip()
    if lowered in _LATEST_REFS:
        if layers:
            return list(layers.keys())[-1]
        return None
    if lowered in _FINAL_REFS:
        for lid in reversed(list(layers.keys())):
            record = layers[lid]
            if hasattr(record, "metadata") and record.metadata.get("role") == "final":
                return lid
        return None
    # 直接匹配
    result = aliases.get(ref) or aliases.get(lowered)
    if result is not None:
        return result
    # 大小写不敏感的 key 遍历匹配
    for alias_key, alias_val in aliases.items():
        if alias_key.lower() == lowered:
            return alias_val
    return None
