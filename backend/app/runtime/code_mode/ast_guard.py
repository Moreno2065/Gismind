"""Static policy for Runtime code tasks."""

from __future__ import annotations

import ast
from dataclasses import dataclass


class ASTBannedNodeError(ValueError):
    pass


@dataclass(frozen=True)
class InspectionResult:
    required_executor: str = "sandbox"


def inspect(code: str) -> InspectionResult:
    tree = ast.parse(code, mode="exec")
    banned_names = {"socket", "tool_fns", "rpc", "eval", "exec", "compile", "__import__"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise ASTBannedNodeError("imports are not allowed")
        if isinstance(node, ast.Name) and node.id in banned_names:
            raise ASTBannedNodeError(f"forbidden name: {node.id}")
        if isinstance(node, ast.Attribute) and (node.attr.startswith("__") or node.attr in {"execute", "request", "post", "get"}):
            raise ASTBannedNodeError(f"forbidden attribute: {node.attr}")
    return InspectionResult()
