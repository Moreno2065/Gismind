"""SandboxExecutor — 包装 `run_in_sandbox`，跨进程 session_vars / __result__ / tool RPC。

核心 IPC 设计：
- session_vars 注入：pickle → tempfile → 路径字面量写入 wrapper（不 import os）
- __result__ 回捞：子进程 exec 完成后 inject UUID sentinel 到 stderr
- 工具调用：子进程 → 127.0.0.1 TCP JSON-lines RPC → 主进程 registry tool_fns
  （LLM 代码始终在 sandbox 执行，绝不在主进程 exec 任意 LLM 代码）
"""
from __future__ import annotations

import json
import logging
import os
import pickle
import re
import socket
import tempfile
import threading
import traceback
import uuid
from typing import Any, Callable, Optional

from app.agents.code_mode.types import ExecutionResult
from app.sandbox.runner import run_in_sandbox, SandboxResult

logger = logging.getLogger(__name__)


# ============================================================
# Sandbox 内建工具的标准库实现（子进程用 python -S，无第三方库）
# ============================================================

SANDBOX_TOOL_IMPLS = {
    "parse_zip": """def parse_zip(raw_bytes):
    '''解压 zip bytes，提取 GeoJSON 文件和文件列表。纯 stdlib 实现。

    Args:
        raw_bytes: zip 文件的原始 bytes。
    Returns:
        dict: {files: [文件名], geojson: GeoJSON dict 或 None, unsupported: [不支持的文件名]}
    '''
    import zipfile, io, json as _json
    z = zipfile.ZipFile(io.BytesIO(raw_bytes))
    names = z.namelist()
    files = []
    geojson = None
    unsupported = []
    for name in names:
        files.append(name)
        if name.endswith('.geojson') or name.endswith('.json'):
            try:
                data = _json.loads(z.read(name))
                geojson = data
            except Exception:
                pass
        elif name.endswith('.shp'):
            unsupported.append(name)
    return {'files': files, 'geojson': geojson, 'unsupported': unsupported}
""",
}


def _build_sandbox_tool_prefix() -> str:
    """构建 sandbox 子进程 namespace 中可用的工具函数前缀代码。"""
    parts = ["# Sandbox 内建工具（纯 stdlib 实现）"]
    for name, impl in SANDBOX_TOOL_IMPLS.items():
        parts.append(f"\n# ---- {name} ----")
        parts.append(impl)
    return "\n".join(parts)


# ============================================================
# UUID sentinel 生成（碰撞概率为零）
# ============================================================

def _GEN_STATE_SENTINEL() -> tuple[str, str, str]:
    """生成会话级的 UUID sentinel token。"""
    sid = uuid.uuid4().hex
    start = f"__GISMIND_STATE_{sid}__START__"
    end = f"__GISMIND_STATE_{sid}__END__"
    return sid, start, end


# ============================================================
# session_vars → tempfile（写入）
# ============================================================

def _write_session_vars(vars_dict: dict) -> dict[str, str]:
    """把 session_vars pickle 写入 tempfile，返回 env var dict。"""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".svars")
    with open(tmp.name, "wb") as f:
        pickle.dump(vars_dict, f)
    return {"APP_SANDBOX_VARS_PATH": tmp.name}


# ============================================================
# UUID sentinel → stderr（写入 wrapper 代码）
# ============================================================

def _build_result_wrapper_code(sentinel_id: str, start_token: str, end_token: str) -> str:
    """构造子进程 exec 后注入的收尾代码（输出到 stderr，加 UUID guard）。"""
    return (
        "\nimport json as _gismind_json, sys as _gismind_sys\n"
        f"_gismind_result = globals().get('__result__')\n"
        "if _gismind_result is not None:\n"
        f"    _gismind_sys.stderr.write(\n"
        f"        f\"{start_token}\"\n"
        f"        f\"{{_gismind_json.dumps(_gismind_result, default=str)}}\"\n"
        f"        f\"{end_token}\\n\"\n"
        f"    )\n"
        f"    _gismind_sys.stderr.flush()\n"
        "else:\n"
        f"    _gismind_sys.stderr.write(f'{start_token}{{{{}}}}{end_token}\\n')\n"
        f"    _gismind_sys.stderr.flush()\n"
    )


# ============================================================
# UUID sentinel → stderr（主进程解析）
# ============================================================

def _parse_result_sentinel(stderr: str) -> Optional[dict]:
    """从 stderr 解析 UUID sentinel 回传的 __result__。"""
    pattern = r"__GISMIND_STATE_(\w+)__START__(.*?)__GISMIND_STATE_\1__END__"
    match = re.search(pattern, stderr, re.DOTALL)
    if not match:
        return None
    payload = match.group(2).strip()
    try:
        result = json.loads(payload)
        return result
    except json.JSONDecodeError:
        return None


def _strip_sentinel_from_stderr(stderr: str) -> str:
    """从 stderr 中移除 UUID sentinel 内容，保留真正的错误消息。"""
    pattern = r"__GISMIND_STATE_(\w+)__START__.*?__GISMIND_STATE_\1__END__"
    return re.sub(pattern, "", stderr, flags=re.DOTALL).strip()


# ============================================================
# Sandbox → Host registry RPC (TCP JSON-lines)
# ============================================================

_RPC_END = b"\n"


def _build_sandbox_rpc_proxies(port: int, tool_names: list[str]) -> str:
    """生成 sandbox 内工具代理：经 TCP 回主进程调用 registry tool_fns。

    仅对需要主进程 handler 的工具生成代理（非 SANDBOX_TOOL_IMPLS 本地实现）。
    """
    if not tool_names:
        return "# no RPC tool proxies\n"

    names_literal = repr(list(tool_names))
    return (
        "# Sandbox → host registry RPC proxies\n"
        "import json as _rpc_json\n"
        "import socket as _rpc_socket\n"
        f"_RPC_PORT = {int(port)}\n"
        f"_RPC_TOOLS = {names_literal}\n"
        "\n"
        "def _rpc_call_tool(_tool_name, *args, **kwargs):\n"
        "    _payload = _rpc_json.dumps({\n"
        "        'tool': _tool_name,\n"
        "        'args': list(args),\n"
        "        'kwargs': dict(kwargs),\n"
        "    }, default=str)\n"
        "    _sock = _rpc_socket.create_connection(('127.0.0.1', _RPC_PORT), timeout=120)\n"
        "    try:\n"
        "        _sock.sendall((_payload + '\\n').encode('utf-8'))\n"
        "        _buf = b''\n"
        "        while b'\\n' not in _buf:\n"
        "            _chunk = _sock.recv(65536)\n"
        "            if not _chunk:\n"
        "                break\n"
        "            _buf += _chunk\n"
        "        _line = _buf.split(b'\\n', 1)[0].decode('utf-8', errors='replace').strip()\n"
        "        if not _line:\n"
        "            raise RuntimeError(f'RPC empty response for tool {_tool_name!r}')\n"
        "        _resp = _rpc_json.loads(_line)\n"
        "    finally:\n"
        "        try:\n"
        "            _sock.close()\n"
        "        except Exception:\n"
        "            pass\n"
        "    if not isinstance(_resp, dict):\n"
        "        raise RuntimeError(f'RPC invalid response for tool {_tool_name!r}')\n"
        "    if _resp.get('ok') is True:\n"
        "        return _resp.get('result')\n"
        "    raise RuntimeError(str(_resp.get('error') or f'RPC tool {_tool_name!r} failed'))\n"
        "\n"
        "def _make_rpc_proxy(_name):\n"
        "    def _proxy(*args, **kwargs):\n"
        "        return _rpc_call_tool(_name, *args, **kwargs)\n"
        "    _proxy.__name__ = _name\n"
        "    return _proxy\n"
        "\n"
        "for _tn in _RPC_TOOLS:\n"
        "    globals()[_tn] = _make_rpc_proxy(_tn)\n"
        "del _tn\n"
    )


def _build_sandbox_rpc_wrapper(main_port: int) -> str:
    """兼容旧 API：保留函数名；真实代理由 `_build_sandbox_rpc_proxies` 注入。"""
    _ = main_port
    return "# RPC loop deprecated: tools use per-call TCP proxies\n"


class _HostToolRpcServer:
    """主进程侧 TCP 服务：接收 sandbox 工具调用，派发到 registry tool_fns。"""

    def __init__(self, tool_fns: dict[str, Callable]):
        self._tool_fns = tool_fns or {}
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(32)
        self._sock.settimeout(0.5)
        self.port = int(self._sock.getsockname()[1])
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._errors: list[str] = []

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._serve_loop,
            name=f"sandbox-rpc-{self.port}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _serve_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                continue
            try:
                self._handle_conn(conn)
            except Exception as exc:  # noqa: BLE001
                self._errors.append(str(exc))
                logger.exception("sandbox RPC connection failed")
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _handle_conn(self, conn: socket.socket) -> None:
        conn.settimeout(120)
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
        line = buf.split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
        if not line:
            conn.sendall(b'{"ok":false,"error":"empty request"}\n')
            return
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            conn.sendall(b'{"ok":false,"error":"invalid JSON"}\n')
            return
        tool = str((req or {}).get("tool") or "")
        args = list((req or {}).get("args") or [])
        kwargs = dict((req or {}).get("kwargs") or {})
        fn = self._tool_fns.get(tool)
        if fn is None:
            resp = {"ok": False, "error": f"tool {tool!r} not available via RPC"}
            conn.sendall((json.dumps(resp, default=str) + "\n").encode("utf-8"))
            return
        try:
            result = fn(*args, **kwargs)
            # ToolResult-like objects already unwrapped by _build_code_mode_tool_fns
            resp = {"ok": True, "result": result}
        except Exception as exc:  # noqa: BLE001
            resp = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc()[-2000:],
            }
        try:
            payload = json.dumps(resp, default=str) + "\n"
        except (TypeError, ValueError):
            payload = json.dumps(
                {"ok": False, "error": f"tool {tool!r} returned non-JSON result"},
            ) + "\n"
        conn.sendall(payload.encode("utf-8"))


# ============================================================
# SandboxExecutor
# ============================================================

class SandboxExecutor:
    """包装 `run_in_sandbox`，增加 session_vars 注入 + __result__ 回传 + tool RPC。

    用法：
        executor = SandboxExecutor()
        code = "pois = query_poi(...); __result__ = {'pois': pois}"
        result = executor.execute(code, session_vars={...}, tool_fns={...})

    注意：
        - 不负责 session_vars 的更新逻辑（由 HybridExecutor 做 update）
        - 只负责 IPC：注入 → 执行 → 回传
        - LLM 代码始终在子进程执行；主进程只跑 registry 工具 handler
    """

    def __init__(self, timeout_s: Optional[int] = None, memory_mb: Optional[int] = None):
        self.timeout_s = timeout_s
        self.memory_mb = memory_mb
        self._tempfile_path: Optional[str] = None

    def execute(
        self,
        code: str,
        session_vars: Optional[dict] = None,
        call_graph: Optional[dict[str, str]] = None,
        tool_fns: Optional[dict[str, Callable]] = None,
        known_tools: Optional[dict[str, str]] = None,
    ) -> ExecutionResult:
        """在子进程沙箱中执行 code。

        1. pickle session_vars → tempfile（路径字面量写入 wrapper）
        2. 启动 host RPC server（供 sandbox 回调 registry tools）
        3. 拼装 wrapper code（unpickle + RPC proxies + exec + sentinel stderr）
        4. 调用 run_in_sandbox
        5. 解析 stderr 回传 __result__
        6. 删除 tempfile / 停 RPC
        """
        import time
        from app.config import settings

        start = time.time()
        timeout_s = (
            self.timeout_s
            if self.timeout_s is not None
            else settings.APP_SANDBOX_TIMEOUT_S
        )
        memory_mb = (
            self.memory_mb
            if self.memory_mb is not None
            else settings.APP_SANDBOX_MEMORY_MB
        )

        # 1. session_vars → tempfile
        sv = session_vars or {}
        env_from_vars = _write_session_vars(sv)
        self._tempfile_path = env_from_vars["APP_SANDBOX_VARS_PATH"]
        sv_path_literal = repr(self._tempfile_path)

        rpc_server: Optional[_HostToolRpcServer] = None
        try:
            # 2. 生成 UUID sentinel
            sentinel_id, start_token, end_token = _GEN_STATE_SENTINEL()

            # 3. 决定哪些工具走 RPC（有 host tool_fn 且非本地 sandbox impl 优先）
            fns = tool_fns or {}
            tools_meta = known_tools or {}
            call_graph = call_graph or {}

            local_impl_names = set(SANDBOX_TOOL_IMPLS)
            # RPC 工具：主进程提供了 callable 的全部工具（含 sandbox 类型如 code_executor）
            rpc_tool_names = sorted(
                name for name, fn in fns.items() if callable(fn) and name not in local_impl_names
            )
            # 若 call_graph 里点名了本地 sandbox 工具，注入本地实现
            tool_impls_code = ""
            for tool_name in set(list(call_graph.keys()) + list(tools_meta.keys())):
                if tool_name in SANDBOX_TOOL_IMPLS and tool_name not in rpc_tool_names:
                    tool_impls_code += (
                        f"\n# sandbox tool: {tool_name}\n{SANDBOX_TOOL_IMPLS[tool_name]}\n"
                    )
            if not tool_impls_code and not rpc_tool_names:
                # 兼容路径：无工具时仍注入本地 parse_zip 实现
                tool_impls_code = "\n".join(SANDBOX_TOOL_IMPLS.values()) + "\n"

            # 4. 启动 RPC server（仅当有需要回调的工具）
            rpc_port = 0
            env_overrides: dict[str, str] = dict(env_from_vars)
            if rpc_tool_names:
                rpc_server = _HostToolRpcServer(fns)
                rpc_server.start()
                rpc_port = rpc_server.port
                # 允许 sandbox 连回本机 RPC 端口
                allow = settings.APP_SANDBOX_NETWORK_ALLOWLIST or ""
                entries = [e.strip() for e in allow.split(",") if e.strip()]
                entries.append(f"127.0.0.1:{rpc_port}")
                env_overrides["APP_SANDBOX_NETWORK_ALLOWLIST"] = ",".join(entries)

            wrapper_result = _build_result_wrapper_code(sentinel_id, start_token, end_token)
            rpc_proxies = (
                _build_sandbox_rpc_proxies(rpc_port, rpc_tool_names)
                if rpc_tool_names
                else "# no RPC tools\n"
            )

            # 路径字面量注入：避免子进程 `import os`（被黑名单拦截）
            wrapped_code = (
                "import sys as _sys\n"
                "import sitecustomize_gismind\n"
                f"{tool_impls_code}"
                f"{rpc_proxies}"
                "# unpickle session_vars (path embedded; no os import)\n"
                "import pickle as _pickle\n"
                f"_sv_path = {sv_path_literal}\n"
                "if _sv_path:\n"
                "    with open(_sv_path, 'rb') as _f:\n"
                "        _sv = _pickle.load(_f)\n"
                "    for _k, _v in list(_sv.items()):\n"
                "        globals()[_k] = _v\n"
                "    del _sv\n"
                "del _sv_path\n"
                "del _pickle\n"
                f"\n{code}\n"
                f"{wrapper_result}\n"
            )

            # 5. 调用 run_in_sandbox
            sandbox_result = run_in_sandbox(
                wrapped_code,
                timeout_s=timeout_s,
                memory_mb=memory_mb,
                env_overrides=env_overrides,
            )
            duration_ms = int((time.time() - start) * 1000)

            # 6. 解析 sentinel
            stderr_sentinel_result = _parse_result_sentinel(sandbox_result.stderr)

            # 7. 判断返回
            error_code = sandbox_result.error_code
            fatal_codes = {
                "SANDBOX_TIMEOUT",
                "SANDBOX_OOM",
                "SANDBOX_FORBIDDEN_IMPORT",
                "SANDBOX_NETWORK_DENIED",
            }
            if error_code in fatal_codes:
                success = False
            elif sandbox_result.returncode != 0:
                success = False
                if not error_code:
                    error_code = "EXECUTION_ERROR"
            else:
                # returncode==0：有 __result__ 回传视为成功（即使 stderr 有噪声）
                success = True
                # 若无 sentinel 且 stderr 含 traceback，仍标失败
                if stderr_sentinel_result is None and "Traceback" in (sandbox_result.stderr or ""):
                    success = False
                    if not error_code:
                        error_code = "EXECUTION_ERROR"

            traceback_str = sandbox_result.stderr if not success else None
            if not success and not error_code:
                error_code = "EXECUTION_ERROR"
            cleaned_stderr = _strip_sentinel_from_stderr(sandbox_result.stderr)

            return ExecutionResult(
                success=success,
                stdout=sandbox_result.stdout,
                stderr=cleaned_stderr,
                returncode=sandbox_result.returncode,
                result=stderr_sentinel_result if stderr_sentinel_result is not None else {},
                duration_ms=duration_ms,
                error_code=error_code,
                executor_type="sandbox",
                required_executor="sandbox",
                code=code,
                traceback=traceback_str[:3000] if traceback_str else None,
            )
        finally:
            if rpc_server is not None:
                rpc_server.stop()
            self._cleanup_tempfile()

    def _cleanup_tempfile(self):
        """显式清理 tempfile（在 finally 块中调用，不依赖 __del__）。"""
        if self._tempfile_path:
            try:
                if os.path.exists(self._tempfile_path):
                    os.unlink(self._tempfile_path)
            except OSError:
                pass
            self._tempfile_path = None

    def __del__(self):
        """临时文件清理（兜底）。"""
        if self._tempfile_path and os.path.exists(self._tempfile_path):
            try:
                os.unlink(self._tempfile_path)
            except OSError:
                pass
