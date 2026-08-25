"""SqliteSaver 单例工厂。

根据 Task 1.1 验证: langgraph 1.x + langgraph-checkpoint-sqlite 3.x
Import: from langgraph.checkpoint.sqlite import SqliteSaver

注意: SqliteSaver.from_conn_string() 在 v3.x 中是 context manager，
因此本模块直接创建 sqlite3.Connection 后传给构造函数，以支持单例模式。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


def checkpoint_path() -> Path:
    """返回 settings.APP_CHECKPOINT_DB 的绝对路径，自动创建父目录。"""
    p = Path(settings.APP_CHECKPOINT_DB)
    if not p.is_absolute():
        p = Path(os.getcwd()) / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _resolve_path(path: Path) -> Path:
    """确保路径的父目录存在，返回绝对路径。"""
    p = path.absolute() if not path.is_absolute() else path
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


_singleton_saver = None
_singleton_path: Optional[Path] = None
_singleton_lock = threading.Lock()


def get_sqlite_checkpointer(path: Optional[Path] = None):
    """返回 SqliteSaver 实例。单例避免重复开文件。

    Args:
        path: 可选，覆盖 settings.APP_CHECKPOINT_DB 指定的路径。

    Returns:
        SqliteSaver 实例（进程内单例）。

    Raises:
        ValueError: 单例已绑定到另一路径时又传入不同显式 path。
    """
    global _singleton_saver, _singleton_path
    requested = _resolve_path(path) if path is not None else None

    if _singleton_saver is not None:
        if requested is not None and _singleton_path is not None and requested != _singleton_path:
            raise ValueError(
                f"Sqlite checkpointer already bound to {_singleton_path}; "
                f"refusing different path {requested}. Call reset_sqlite_checkpointer() first."
            )
        return _singleton_saver

    with _singleton_lock:
        if _singleton_saver is not None:
            if requested is not None and _singleton_path is not None and requested != _singleton_path:
                raise ValueError(
                    f"Sqlite checkpointer already bound to {_singleton_path}; "
                    f"refusing different path {requested}. Call reset_sqlite_checkpointer() first."
                )
            return _singleton_saver

        p = requested if requested is not None else checkpoint_path()
        _resolve_path(p)
        from langgraph.checkpoint.sqlite import SqliteSaver

        conn = sqlite3.connect(str(p), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL;")
        _singleton_saver = SqliteSaver(conn)
        _singleton_path = p
        return _singleton_saver


def reset_sqlite_checkpointer() -> None:
    """关闭当前单例的数据库连接并重置单例。

    主要用于测试或优雅关闭场景。
    """
    global _singleton_saver, _singleton_path
    with _singleton_lock:
        if _singleton_saver is not None:
            try:
                _singleton_saver.conn.close()
            except (AttributeError, Exception):
                logger.warning("failed to close checkpointer connection", exc_info=True)
        _singleton_saver = None
        _singleton_path = None
