"""code_mode 共享数据类型。

ExecutionResult: 单次代码执行的完整结果（success/stdout/result/error_code/executor_type）。
InspectionResult: AST 静态分析的输出（required_executor / reasons / call_graph）。
ASTBannedNodeError: outright banned 节点（Import / FunctionDef 等）抛的异常。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class ASTBannedNodeError(Exception):
    """outright banned 节点抛的异常（不是返回 required_executor="sandbox" 的路由信号）。

    与"路由到 sandbox"的区别：
    - 路由到 sandbox（While / AsyncFor / range 大字面量 / sandbox-tool call）：代码"可执行"
      但有风险，由 SandboxExecutor 在子进程跑。
    - outright banned（Import / FunctionDef / ClassDef / dunder / 危险 call）：代码**架构违规**，
      沙箱里执行也会失败，必须让 planner 改。

    Attributes:
        node_type: AST 节点类型名（如 "Import", "FunctionDef", "Attribute"）。
        snippet: 违规代码片段（首 80 字符），方便 verifier / 模型定位。
        lineno: 行号（可选）。
    """
    node_type: str = ""
    snippet: str = ""
    lineno: Optional[int] = None

    def __post_init__(self):
        if self.node_type:
            super().__init__(
                f"AST banned node: {self.node_type} at line {self.lineno}: {self.snippet!r}"
            )
        else:
            super().__init__("AST banned node")


@dataclass
class InspectionResult:
    """AST 静态分析结果。

    Attributes:
        required_executor: "inline"（主进程 exec）或 "sandbox"（子进程沙箱）。
                          单一字段、无歧义。executor 看到 "inline" 走主进程，
                          看到 "sandbox" 走子进程，看到 ASTBannedNodeError 就回 planner refine。
        reasons: 路由到 sandbox 的原因列表（inline 时为空）。每条 reason 是可读字符串。
                 outright banned 节点不进入 reasons，而是抛 ASTBannedNodeError。
        call_graph: 顶层函数名 → executor_type 的映射（"inline" / "async" / "sandbox"）。
                   用于 executor 二次判定（虽然 AST 没判 sandbox，但 call_graph 记录了真实类型）。
    """
    required_executor: str = "inline"
    reasons: list[str] = field(default_factory=list)
    call_graph: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExecutionResult:
    """HybridExecutor.execute(code) 的返回值。

    Attributes:
        success: True 表示执行成功（__result__ 已赋值或代码无副作用完成）。
        stdout: 代码 print() 累积输出。
        stderr: 代码执行中写入 stderr 的内容（含 traceback）。
        returncode: 子进程退出码（inline 路径恒为 0；sandbox 路径透传）。
        result: __result__ 变量的值（任意类型；to_dict 时走 JSON 兜底）。
        duration_ms: 墙钟耗时毫秒。
        error_code: 错误码（"SOFT_TIMEOUT" / "AST_BANNED_NODE" / "SANDBOX_TIMEOUT" /
                                "SANDBOX_OOM" / "SANDBOX_FORBIDDEN_IMPORT" /
                                "INLINE_TIMEOUT" / "EXECUTION_ERROR" / None）。
        required_executor: AST 静态分析得出的推荐执行路径（"inline" / "sandbox"）。
                          与实际 executor_type 可能不同（例如 AST 说 inline，但 runtime 失败转 sandbox）。
        executor_type: 实际走的执行路径（"inline" / "sandbox"）。失败回退后会更新。
        code: 原始代码字符串（用于 checkpoint + trace 回放）。
        traceback: 异常 traceback 字符串（仅失败态填充；execution_to_tool_result 内截断到 3000 字符）。
    """
    success: bool = False
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    result: Any = None
    duration_ms: int = 0
    error_code: Optional[str] = None
    required_executor: str = "sandbox"
    executor_type: str = "sandbox"
    code: str = ""
    traceback: Optional[str] = None

    def to_dict(self) -> dict:
        """JSON-可序列化字典（result 走 try/except 兜底）。"""
        d = asdict(self)
        try:
            d["result"] = json.loads(json.dumps(self.result, default=repr))
        except Exception:
            d["result"] = repr(self.result)
        return d