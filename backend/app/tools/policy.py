"""Tool 重试/熔断策略框架（§8.2 Tool 鲁棒性）。

提供：
- ToolPolicy dataclass：每工具的 max_retries / fallback_chain / breaker 参数
- POLICIES dict：各工具的默认策略
- BreakerState：进程级熔断器状态（连续失败 N 次后在窗口内阻止调用）

用法示例：

    policy = POLICIES.get(tool_name)
    if policy and BreakerState.is_open(tool_name, policy):
        return error("tool is circuit-broken")

    for attempt in range((policy.max_retries if policy else 0) + 1):
        try:
            result = handler(ctx)
            if result.status != "error":
                break
        except Exception:
            if attempt < policy.max_retries:
                time.sleep(policy.retry_delay_s * (policy.retry_backoff ** attempt))
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from app.agents.errors import ErrorCode


@dataclass
class ToolPolicy:
    """工具重试/熔断策略。

    Attributes:
        max_retries: 最大重试次数（0 = 不重试）。
        retry_delay_s: 首次重试延迟秒数。
        retry_backoff: 退避因子（第 n 次重试延迟 = retry_delay_s * retry_backoff ** n）。
        retry_on: 可重试的错误码元组；空元组表示所有 error 都重试。
        fallback_chain: 备用工具名列表（预留，当前未实现自动 fallback）。
        breaker_threshold: 连续失败多少次后熔断。
        breaker_window_s: 熔断观察窗口秒数（窗口内达到阈值则熔断）。
        breaker_half_open_timeout_s: 半开超时秒数后自动重置。
        per_call_timeout_s: 单次调用超时秒数（预留，当前未实现 per-call timeout）。
    """
    max_retries: int = 2
    retry_delay_s: float = 1.0
    retry_backoff: float = 2.0
    retry_on: tuple[ErrorCode, ...] = ()
    fallback_chain: list[str] = field(default_factory=list)
    breaker_threshold: int = 5
    breaker_window_s: int = 60
    breaker_half_open_timeout_s: float = 30.0
    per_call_timeout_s: float = 30.0


# ============================================================
# 工具默认策略
# ============================================================

POLICIES: dict[str, ToolPolicy] = {
    "query_poi": ToolPolicy(
        max_retries=1,
        # POI 查询依赖外部 HTTP API（高德/OSM），重试应针对网络相关错误。
        # NOTE: 理想情况下应使用 ErrorCode.NETWORK_ERROR，当前 ErrorCode 枚举中
        # 无此值，暂用 TOOL_EXECUTION_ERROR 代替（覆盖外部调用失败场景）。
        retry_on=(ErrorCode.TOOL_EXECUTION_ERROR,),
        # fallback_chain 为空：自动 fallback 尚未实现，填写 ["query_poi"] 会导致
        # 无限递归。OSM 兜底已在 POIQuery.search_poi_tool 内部完成，不走此框架。
        fallback_chain=[],
        breaker_threshold=10,
    ),
    "isochrone": ToolPolicy(
        max_retries=2,
        retry_on=(ErrorCode.LLM_UNAVAILABLE, ErrorCode.TOOL_EXECUTION_ERROR),
        breaker_threshold=3,
    ),
    "geo_code": ToolPolicy(max_retries=1),
    "data_io_read": ToolPolicy(max_retries=0),      # 确定性操作不重试
    "code_executor": ToolPolicy(
        max_retries=0,                                # 沙箱不重试
        per_call_timeout_s=60.0,
    ),
    "buffer": ToolPolicy(max_retries=0),             # 计算本地，不重试
    "overlay": ToolPolicy(max_retries=0),
    "voronoi": ToolPolicy(max_retries=0),
    "map_layer_build": ToolPolicy(max_retries=0),
}


# ============================================================
# 进程级熔断器
# ============================================================

_NO_POLICY = ToolPolicy()


class BreakerState:
    """熔断器状态（进程级字典）。

    NOTE: _failures 和 _last_failure 为类级字典，无显式锁。
    在 ASGI 单进程模式下（uvicorn 默认单 worker），所有请求共享同一事件循环，
    不存在真正的并发写竞争。若部署多 worker，每个 worker 拥有独立的进程级状态，
    熔断计数不跨 worker 共享，属于可接受的降级行为。若未来引入 threading 模型
    （如 gunicorn + 多线程），需要在此处添加 threading.Lock。

    用法：
        BreakerState.record_failure("query_poi")
        if BreakerState.is_open("query_poi", policy):
            # 跳过该工具
    """
    _failures: dict[str, int] = {}
    _last_failure: dict[str, float] = {}

    @classmethod
    def record_failure(cls, tool_name: str, now: float | None = None) -> None:
        """记录工具的一次失败。"""
        t = now or time.time()
        cls._failures[tool_name] = cls._failures.get(tool_name, 0) + 1
        cls._last_failure[tool_name] = t

    @classmethod
    def is_open(
        cls,
        tool_name: str,
        policy: Optional[ToolPolicy] = None,
        now: float | None = None,
    ) -> bool:
        """检查工具是否已熔断。

        连续失败次数 >= breaker_threshold 且在窗口期内 → 熔断（返回 True）。
        窗口期过后自动转为半开（计数器归零，下次调用可继续）。
        """
        p = policy or _NO_POLICY
        t = now or time.time()
        failures = cls._failures.get(tool_name, 0)
        last = cls._last_failure.get(tool_name, 0.0)
        if failures >= p.breaker_threshold:
            if t - last < p.breaker_window_s:
                return True
            # 窗口期已过 → half-open reset
            cls._failures[tool_name] = 0
        return False

    @classmethod
    def reset(cls, tool_name: str) -> None:
        """手动重置熔断计数器。"""
        cls._failures.pop(tool_name, None)
        cls._last_failure.pop(tool_name, None)
