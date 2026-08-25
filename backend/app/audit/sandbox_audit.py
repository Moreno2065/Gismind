from __future__ import annotations
import hashlib
import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

log = logging.getLogger("gismind.sandbox_audit")

# 审计日志绝对路径：基于本文件所在目录向上三级（backend/app/audit -> backend）
_AUDIT_BASE = Path(__file__).resolve().parent.parent.parent
AUDIT_PATH = _AUDIT_BASE / ".gismind" / "sandbox_audit.log"
AUDIT_MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB 触发轮转


class _ForbiddenTracker:
    """滑动窗口记录每个 user 的 SANDBOX_FORBIDDEN_IMPORT 触发频率。"""

    _buckets: dict[str, deque[float]] = {}
    _lock: threading.Lock = threading.Lock()
    THRESHOLD_PER_MIN = 20

    @classmethod
    def record(cls, user_id: str) -> bool:
        now = time.time()
        with cls._lock:
            q = cls._buckets.setdefault(user_id, deque())
            q.append(now)
            while q and q[0] < now - 60:
                q.popleft()
            return len(q) >= cls.THRESHOLD_PER_MIN


def record(
    user_id: str,
    code: str,
    error_code: str | None = None,
    duration_ms: int = 0,
    returncode: int = 0,
) -> None:
    """写入一条 sandbox 调用审计日志。"""
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)

    # 简单日志轮转：文件超过 10MB 时重命名为 .1 备份
    try:
        if AUDIT_PATH.exists() and AUDIT_PATH.stat().st_size > AUDIT_MAX_SIZE_BYTES:
            backup = AUDIT_PATH.with_suffix(".log.1")
            if backup.exists():
                backup.unlink()
            os.rename(str(AUDIT_PATH), str(backup))
    except OSError:
        pass  # 轮转失败不影响审计写入

    entry = {
        "ts": datetime.now().isoformat(),
        "user_id": user_id,
        "code_hash": hashlib.sha256(code.encode()).hexdigest()[:16],
        "code_len": len(code),
        "error_code": error_code,
        "duration_ms": duration_ms,
        "returncode": returncode,
    }
    with AUDIT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    if error_code == "SANDBOX_FORBIDDEN_IMPORT":
        exceeded = _ForbiddenTracker.record(user_id)
        if exceeded:
            log.warning(
                "user %s hit SANDBOX_FORBIDDEN_IMPORT >= %d times/min; "
                "consider disabling sandbox for this user",
                user_id, _ForbiddenTracker.THRESHOLD_PER_MIN,
            )
