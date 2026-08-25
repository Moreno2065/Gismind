"""Preflight 模块公开接口。

用法::

    from app.agents.preflight import run_with_preflight, PreflightError, ValidationIssue
"""
from __future__ import annotations

from app.agents.preflight.validation import PreflightError, ValidationIssue
from app.agents.preflight.runner import run_with_preflight

# 注册 preflight 规则（import 时装饰器自动注册到 _RULES）
from . import (  # noqa: F401
    rules_buffer,
    rules_vector,
    rules_io,
    rules_layer,
    rules_overlay,
    rules_overwrite,
    rules_raster,
)

__all__ = [
    "run_with_preflight",
    "PreflightError",
    "ValidationIssue",
]
