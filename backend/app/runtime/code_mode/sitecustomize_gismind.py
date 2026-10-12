"""通过 PYTHONPATH 注入子进程, 在 import 时拦截黑名单 + 默认 deny 网络.

调用方式:
    PYTHONPATH=backend/app/sandbox python -S -c "import sitecustomize_gismind; ..."

自动导入机制:
    把本文件目录加入 PYTHONPATH 后, Python 启动时会尝试 import sitecustomize,
    所以本文件必须命名为 sitecustomize_gismind.py; 调用方必须:
        PYTHONPATH=backend/app/sandbox:$PYTHONPATH python -S
    并确保子进程代码第一行是: import sitecustomize_gismind

----
限制说明：
  子进程使用 `python -S` 启动，阻止 site-packages 自动加载。
  这意味着 shapely、geopandas、numpy 等第三方地理/数值库在沙箱中不可用。
  如需在沙箱中支持这些库，需实现 --allowed-packages 机制显式白名单导入。
  TODO: 增加 SANDBOX_ALLOWED_PACKAGES 配置，允许按需加载指定第三方包。
----
"""
import builtins
import sys

# 在替换 __import__ 之前导入基础设施模块，让内部依赖(os/sys/io等)
# 正常进入 sys.modules, 不会被后续的 sandbox hook 误拦。
# pickle/zipfile/io 必须在 hook 前完成初始化，否则 wrapper / parse_zip 的
# `import pickle` / `import zipfile` 会间接触发被拦的 `import os`。
import socket as _socket_mod
import os as _os_mod
import pickle as _pickle_mod  # noqa: F401
import zipfile as _zipfile_mod  # noqa: F401
import io as _io_mod  # noqa: F401
# 预导入 linecache / traceback，确保 sandbox runner 的异常回报链可用
# （traceback 依赖 contextlib → os，必须在 hook 安装前完成导入）
import linecache as _linecache_mod  # noqa: F401
import traceback as _traceback_mod  # noqa: F401

BLOCKED_IMPORTS = {
    "os", "subprocess", "shutil", "ctypes", "_ctypes",
    "multiprocessing", "pty", "fcntl", "resource",
}

# 拦截 import — 用闭包捕获 _real_import 引用
_real_import = builtins.__import__

def _make_sandbox_import(real_import):
    def _sandbox_import(name, globals=None, locals=None, fromlist=(), level=0, **kwargs):
        top = name.split(".")[0] if name else ""
        if top in BLOCKED_IMPORTS:
            # 用 sys._getframe（不触发 linecache→import os）判断是否用户代码。
            # 用户从 <sandbox>/<string> 导入黑名单 → 拒绝。
            # stdlib 内部二次 import（如 linecache 拉 os）→ 返回已预加载模块。
            user_frame = False
            try:
                f = sys._getframe(1)
                for _ in range(8):
                    if f is None:
                        break
                    fn = f.f_code.co_filename or ""
                    if fn in ("<sandbox>", "<string>") or fn.endswith(
                        ("<sandbox>", "<string>")
                    ):
                        user_frame = True
                        break
                    f = f.f_back
            except ValueError:
                pass
            if user_frame:
                print("__SANDBOX_ERROR__:SANDBOX_FORBIDDEN_IMPORT", file=sys.stderr)
                raise ImportError(
                    f"[SANDBOX_FORBIDDEN_IMPORT] import {name!r} is blocked "
                    f"in code_executor sandbox"
                )
            if top in sys.modules:
                return sys.modules[top]
            if name in sys.modules:
                return sys.modules[name]
            # 未预加载的黑名单模块：仍拒绝
            print("__SANDBOX_ERROR__:SANDBOX_FORBIDDEN_IMPORT", file=sys.stderr)
            raise ImportError(
                f"[SANDBOX_FORBIDDEN_IMPORT] import {name!r} is blocked "
                f"in code_executor sandbox"
            )
        return real_import(name, globals, locals, fromlist, level, **kwargs)
    return _sandbox_import

builtins.__import__ = _make_sandbox_import(_real_import)

# 网络白名单: 空=全部 deny, 非空则只允许 host:port 列表
_WHITE_LIST = set()
_allow_env = _os_mod.environ.get("APP_SANDBOX_NETWORK_ALLOWLIST", "")
if _allow_env.strip():
    for entry in _allow_env.split(","):
        entry = entry.strip()
        if entry:
            _WHITE_LIST.add(entry)

_real_socket = _socket_mod.socket

def _make_sandbox_socket(real_socket):
    class _WrappedSocket:
        """包裹真实 socket 对象，拦截 .connect() / .connect_ex() 做网络白名单校验。"""

        def __init__(self, real_sock):
            self._sock = real_sock
            for _attr in ("family", "type", "proto", "getsockname", "getpeername",
                           "getsockopt", "setsockopt", "setblocking", "settimeout",
                           "gettimeout", "fileno", "close", "shutdown",
                           "bind", "listen", "accept", "send", "sendall",
                           "sendto", "recv", "recvfrom", "recv_into",
                           "makefile", "detach", "dup"):
                if hasattr(real_sock, _attr):
                    setattr(self, _attr, getattr(real_sock, _attr))

        def connect(self, address):
            host, port = address[0], address[1]
            key = f"{host}:{port}"
            if key in _WHITE_LIST:
                return self._sock.connect(address)
            raise PermissionError(
                "[SANDBOX_NETWORK_DENIED] outbound network connections blocked"
            )

        def connect_ex(self, address):
            host, port = address[0], address[1]
            key = f"{host}:{port}"
            if key in _WHITE_LIST:
                return self._sock.connect_ex(address)
            raise PermissionError(
                "[SANDBOX_NETWORK_DENIED] outbound network connections blocked"
            )

    def _maybe_allowed_socket(*args, **kwargs):
        real = real_socket(*args, **kwargs)
        return _WrappedSocket(real)

    return _maybe_allowed_socket

_socket_mod.socket = _make_sandbox_socket(_real_socket)

_real_create_connection = _socket_mod.create_connection

def _make_sandbox_create_connection(real_create_connection):
    def _controlled_create_connection(address, *args, **kwargs):
        host, port = address
        key = f"{host}:{port}"
        if key in _WHITE_LIST:
            return real_create_connection(address, *args, **kwargs)
        raise PermissionError(
            "[SANDBOX_NETWORK_DENIED] outbound network connections blocked"
        )
    return _controlled_create_connection

_socket_mod.create_connection = _make_sandbox_create_connection(_real_create_connection)

# Hook 安装完毕：删除 _real_* 引用，防止子进程代码通过 sitecustomize_gismind
# 模块属性直接访问原始函数绕过沙箱防护。
del _real_import, _real_socket, _real_create_connection

# 清理 os 模块：读完 env 配置后从 sys.modules 移除，防止注入代码通过
# sys.modules['os'] 取回 os 模块绕过 import 黑名单。
sys.modules.pop("os", None)
