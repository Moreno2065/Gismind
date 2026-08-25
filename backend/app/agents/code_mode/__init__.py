"""code_mode package — LLM 写 Python 代码替代 JSON 工具调用的统一执行层。

模块划分：
- ast_guard.py: AST 静态分析，决定走 inline（主进程 exec）还是 sandbox（子进程）
- namespace.py: 干净 namespace 构造（无 os/sys/__import__）
- executor.py: HybridExecutor 主入口，AST 分流 → sandbox 统一执行
- sandbox_runner.py: SandboxExecutor 包装现有 run_in_sandbox
- types.py: ExecutionResult + InspectionResult dataclass
"""