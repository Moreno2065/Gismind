"""trace 级 token 估算与封顶。"""
from __future__ import annotations


class CostExceeded(Exception):
    """APP_MAX_COST_TOKENS 被本次 LLM 调用突破。"""


class CostTracker:
    """轻量 token 计数器，task_id 级别的 LLM 开销聚合。

    每个 AgentRootState / SubAgent 全程持一个实例，
    每次 LLM 调用后估算 token 消耗并累加。
    """

    def __init__(self, max_tokens: int = 100000):
        self.max_tokens = max_tokens
        self.by_node: dict[str, int] = {}

    @property
    def total(self) -> int:
        return sum(self.by_node.values())

    @property
    def remaining(self) -> int:
        return max(0, self.max_tokens - self.total)

    def add(self, node: str, tokens: int) -> None:
        """累加 token 计数。若超出上限则抛出 CostExceeded。"""
        next_total = self.total + tokens
        if next_total > self.max_tokens:
            raise CostExceeded(
                f"trace exceeded max_tokens={self.max_tokens} "
                f"(would be {next_total}); failed at {node}"
            )
        self.by_node[node] = self.by_node.get(node, 0) + tokens


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数: 每 4 个字符 = 1 token。"""
    return max(1, len(text) // 4)
