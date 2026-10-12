# Gismind 修复·改进·优化总体方案（Remediation Plan）

> **版本:** v1.1 | **最后更新:** 2026-07-17 | **初始版本:** v1.0 (2026-07-17)
> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development 或 superpowers:executing-plans，逐 Phase 执行。Steps 用 `- [ ]` 追踪。
> 每个 Phase 自包含，可在新会话中独立执行。执行任何 Task 前先读该 Task 列出的文件。

**Goal:** 修复 2026-07-17 全仓库审查发现的 16+ 项 Critical（主链路当前不可用），完成 `2026-07-17-code-mode-upgrade.md`（下称"升级计划"）遗留的子系统接线，吸收 PineFlow 的可校验性/可审计性机制，解决投影带硬编码问题，并把 code_executor 收敛到安全可维护的形态。

**定位:** 本计划不是重写，是**接线 + 收敛 + 加固**。升级计划产出的 8 个子系统（workspace/preflight/toolkit/skill/events/pending/hooks/risks）代码已存在但大面积未接线——本计划把它们接到主链路上，并补 CI 防回归。

---

## 当前状态（v1.1）

| 阶段 | 状态 | 关键产出 |
|------|------|---------|
| **Phase 0** 环境地基 | ✅ 已完成 | 虚拟环境锁定、pywin32 接入、基线测试通过 |
| **Phase 1** P0 接线修复 | ✅ 已完成 | preflight 规则库注册、SessionMemory async 化、toolkit 可见性修复、sandbox wrapper 重写、data_io 白名单 |
| **Phase 2** code_executor 收敛 | ✅ 已完成 | inline 路径废弃、沙箱黑名单不可还原化、session_vars JSON-safe 化、SystemExit 透传、AST guard 补漏、Job Object 保活 |
| **Phase 3** GIS 正确性 | 🔴 待执行 | 动态投影带选择、CRS 语义类型化、POI 去重修正 |
| **Phase 4** 编排收敛 | 🟡 部分完成 | Multi-Sub-Agent 已运行；空成功判定、resume 修复、成本刹车待执行 |
| **Phase 5** PineFlow 机制 | 🔴 待执行 | YAML 工具契约、规则网关、结果质量门 |
| **Phase 6** 前端 Critical | 🔴 待执行 | SSE 守卫、停止按钮收尾、渲染容错 |
| **Phase 7** 防回归 | 🔴 待执行 | 契约测试进 CI、反模式巡检、文档对齐 |

**已实现的核心能力:**
- ✅ **code-mode**：code executor 从 inline+AST 收敛到子进程沙箱 + 白名单 RPC，安全边界收缩到位
- ✅ **preflight / hooks / risk**：规则库注册完成，BEFORE_TOOL_CALL hook 已接线，risk 评估链路贯通
- ✅ **Multi-Sub-Agent**：dispatcher 多 sub-agent 并行编排已运行，支持 geometer/coder 等多角色协同

**决策前置（D1–D5，开工前需确认，见 §0）：** 每个决策影响特定 Phase，方案内标注了推荐默认值；若全部接受推荐值可直接开工。

---

## §0 决策点（Blocking Questions）

| # | 问题 | 推荐默认 | 影响 |
|---|------|---------|------|
| D1 | 部署形态：本地单人（软著演示）还是多用户服务？ | 本地单人 | Phase 2 沙箱加固等级、Phase 4 并发修复优先级 |
| D2 | inline（主进程 exec）路径去留？ | **废弃**，全部代码走子进程沙箱（代价：每次 +0.5~2s 启动） | Phase 2 整体范围。若保留 inline，则需额外做资源护栏（不推荐） |
| D3 | Ensemble（多跑投票）功能：修好启用 / 保留但默认关闭 / 移除？ | 保留但默认关闭（修 runner 类型 bug，voter 语义修正后置） | Phase 4 Task 4.1 |
| D4 | 业务范围：仅中国境内 / 含境外？ | 动态选带 + UTM fallback 都做（增量成本约 1 天） | Phase 3 Task 3.1 |
| D5 | YAML 工具契约化范围：核心 12 工具试点 / 全量 60 handler？ | 核心 12 工具试点，模式跑通后滚动推广 | Phase 5 Task 5.1 |

另需确认（不阻塞）：生产是否 Windows（pywin32 在 requirements.txt:34 被注释为 "Phase 3"，无 pywin32 = 沙箱无内存硬限）；软著材料的时间节点（影响 Phase 5/6 取舍）。

---

## Phase 0: 环境地基（所有验证的前提）

审查发现：仓库无锁定虚拟环境，按 README 跑 `pytest` 必败；pywin32 未装 → 沙箱内存硬限静默失效。

- [x] **Task 0.1** 创建 `backend/.venv` 并锁定依赖。把 `pywin32>=306; sys_platform == "win32"` 取消注释（requirements.txt:34），新增 `dev-requirements.txt`（pytest/pytest-asyncio/pytest-cov/fakeredis/httpx）或合并进主文件。补 `pyyaml>=6.0`（**当前缺失导致 toolkit 二态行为**，见 Phase 1 Task 1.5）。
- [x] **Task 0.2** 跑通基线：`python -m pytest tests/ -q` 全绿（允许 skip）。记录基线耗时。
- [x] **Task 0.3** 启动校验增强：`app/main.py` 启动时对"无 pywin32 的 Windows"打印显眼 warning："沙箱内存上限未生效"。

**验证：** 干净 shell 里 `pip install -r requirements.txt && pytest` 一条命令可复现全绿。

---

## Phase 1: P0 接线修复（修完主链路才端到端可跑）

> 全部为接线级小改动，合计约 50 行。每项附审查证据锚点。

- [x] **Task 1.1 修 preflight kwargs 契约（B1）**
  `app/agents/preflight/runner.py:76` 的 `fn(*args, **kwargs)` 改为 `fn(*args)`（kwargs 仅供 preflight 规则读取上下文，不透传给 handler）。证据：60 个 `_handle_*` 全是 `(ctx)` 单参（tool_execution.py:304-…）。
  反模式守卫：不要改 60 个 handler 签名去迁就 runner。
- [x] **Task 1.2 注册 preflight 规则库（B10）**
  `app/agents/preflight/__init__.py` 显式 `from . import rules_buffer, rules_vector, rules_io, rules_layer, rules_overlay, rules_overwrite, rules_raster`。验证：`preflight_for('geo_code', ...)` 后 `_RULES` 非空。
- [x] **Task 1.3 修 ensemble outcome 类型（B9）**
  `app/agents/ensemble/runner.py:41` 把 `SubAgentOutcome.model_validate(res)` 改为复用 `dispatcher.py:421` 的 `_subagent_state_to_outcome(res, task_id, run_id)`（提升到 `schemas.py` 或公共 helpers，两处共用）。
  反模式守卫：不要改 `run_sub_agent` 的返回类型去迁就 runner——dispatcher 也消费同一返回。
- [x] **Task 1.4 SessionMemory async 化（B11）**
  `app/agents/session_memory.py:43-158` 全部方法改 `async def` 并 await；调用点（build_sub_agent.py:114-120、hooks/builtins.py:143-158）位于 worker 线程，复用 `tool_execution.py:52-114` 的 `_run_async` thread-local loop 方案。**删除**注入点的 `except Exception: pass`，改为记 warning。
- [x] **Task 1.5 修 toolkit 可见性与 kernel 工具（B12）**
  `tool_execution.py:1739-1756`：注入循环从 `spec.tool_names` 改为遍历 `visible` 集合；kernel 工具（select_toolkit/load_skill/suggest_skill/inspect_workspace/proactive_clarification）强制并入 `visible`；`enabled_toolkits` 为空时默认并集 = 角色 spec 覆盖的全部 toolkits（参照 PineFlow `src/pineflow_agent/tools/registry/toolkits.py:134-138` 的"默认 data_io + kernel"策略，但我们默认全开更接近现状）。pyyaml 已在 Task 0.1 补入依赖。
- [x] **Task 1.6 修 sandbox wrapper（B2+B3）**
  `app/agents/code_mode/sandbox_runner.py:107-125`：重写 `_build_result_wrapper_code`——改为单行 `stderr.write(start + json.dumps(...) + end)` 拼装（当前 f-string 生成未闭合字符串，100% SyntaxError）；wrapper 内**不 import os**（被自家黑名单拦）。`app/sandbox/sitecustomize_gismind.py` 顶部预导入 `linecache`/`traceback`（runner.py:81-84 的异常回报链依赖它们，否则真实错误被误标 SANDBOX_FORBIDDEN_IMPORT）。
  **必须补一个零 mock 端到端测试**：trivial code → HybridExecutor → sandbox → `__result__` 回捞成功（现有测试全 mock 了 `run_in_sandbox`，这是盲区根因）。
- [x] **Task 1.7 inline 超时重建线程池（B6 的止损半）**
  `app/agents/code_mode/executor.py:359-370`：超时后 `shutdown(wait=False)` 旧 `_inline_pool` 并重建（`close()` 方法已存在，接上即可）。注意：若 D2 接受废弃 inline，此 Task 降级为"过渡期内止损"。
- [x] **Task 1.8 data_io 路径白名单（B8）**
  新增装饰器 `_require_allowed_path(param_name)`：`Path(p).resolve().is_relative_to(allowed_root)`，allowed_root = 上传目录 + workspace 输出目录（config 新增 `APP_WORKSPACE_DIR`）。应用到 `tool_execution.py:1544-1606` 的 load_csv/load_vector/load_raster/export_result 四个 handler。

**验证（Phase 1 出口标准）：**
1. 新增契约测试：对 `_TOOL_REGISTRY` 每个 handler 断言签名为 `(ctx)` 单参；对 `run_sub_agent` 真实返回跑 `_subagent_state_to_outcome` 断言 schema 通过。
2. 新增 wiring 冒烟测试：断言 `_RULES` 非空、kernel 工具在 geometer/coder 的 namespace 可达、SessionMemory 写入后可读回。
3. 端到端：mock 最底层 LLM，跑"code-mode 下 geometer 调 buffer"全链路成功。
4. 零 mock sandbox 回捞测试通过。

---

## Phase 2: code_executor 收敛（是否改进 code executor → 改，方向是"表达力保留 + 安全边界收死"）

**结论先行：不回到纯 JSON call，保留 code-mode 作为产品差异点，但把 PineFlow 的"模型不可绕过的确定性校验层"嫁接进来，并把执行边界收缩到子进程。**

- [x] **Task 2.1 废弃 inline 路径（按 D2）**
  `ast_guard.required_executor` 只保留 `"sandbox"` 与 banned 两类（`"inline"` 仅保留给无工具调用、无循环、纯表达式求值的 micro-snippet，或直接全量 sandbox）。删除/旁路 `executor.py` 的 ThreadPoolExecutor 分支。工具函数在 sandbox 内以 RPC 回主进程执行（复用 `app/sandbox/tools.py` 白名单通道）——这是本 Phase 最大的一块工作，设计参照 PineFlow `api/entrypoints/worker.py:112-135` 的**固定 operation 白名单 JSON-lines RPC**（8 个固定 op，不传任意代码）。
- [x] **Task 2.2 沙箱黑名单不可还原化**
  `sitecustomize_gismond.py`：hook 安装后 `del` 全部 `_real_import/_real_socket/_real_create_connection` 模块属性；读完 env 后 `sys.modules.pop('os')`；真实函数藏进闭包。统一空名单网络策略为 `if _WHITE_LIST and key in _WHITE_LIST: allow else deny`（当前 :82-86 与 :105-112 自相矛盾）。回归测试：`sys.modules['os']`、`importlib.import_module('os')` 各一条拒绝用例。
- [x] **Task 2.3 session_vars JSON-safe 化**
  `tool_execution.py:1954-1958` 的过滤改为**不带 default=str** 的 `json.dumps(v)` 探测，失败则拒绝入库（LLM 收到明确提示"结果需为 JSON 可序列化"）。GeoDataFrame 在工具出口统一转 GeoJSON dict 入库。这同时解决跨进程 pickle 崩塌隐患（sandbox_runner.py:222）。
- [x] **Task 2.4 SystemExit/退出码透传**
  `app/sandbox/runner.py:79-80`：`except SystemExit` 后以 `e.code or 0` 退出；`returncode==1` 且无 sentinel 时 error_code 必填，不得误报 success。
- [x] **Task 2.5 AST guard 补漏（防御纵深，非安全边界）**
  `ast_guard.py`：`ast.comprehension` 纳入 range 炸弹检查；检查 `range()` 全部参数；`List/Tuple/Str × Const` 乘法加阈值；`_try_eval_const` 对 `Pow` 结果位数设限；`FunctionDef`/`TryStar` 要么进 `_BANNED_NODES` 要么改文档（types.py:21）。定位说明：**AST 只做 DX 与路由，资源安全由子进程 + Job Object 兜底**——把这句话写进模块 docstring。
- [x] **Task 2.6 Job Object 句柄保活验证（Windows 必做）**
  在装 pywin32 的机器上跑 `run_in_sandbox("print(1)")`：若进程秒死，把 `runner.py:221-225` 的 job 句柄挂到 `proc` 对象上保活到 `communicate` 之后（KILL_ON_JOB_CLOSE 经典坑）。
- [x] **Task 2.7 inline stdout 捕获**（若 inline 全废弃则跳过）：否则 `run_inline` 加 `redirect_stdout`，让 LLM 能看到自己的 print 与 namespace 冲突 warning（namespace.py:174-178 的原设计意图）。

**验证：** PoC 回归集（comprehension 炸弹路由、`.env` 读取拒绝、`df.to_csv` 拒绝、fence 残缺输入、`sys.exit(3)` 报失败、sandbox 端到端回捞）；`APP_SANDBOX_NETWORK_ALLOWLIST` 空名单下 socket 两种调用均拒绝。

---

## Phase 3: GIS 正确性（投影问题 + 坐标系语义固化）

- [ ] **Task 3.1 动态投影带选择（核心）**
  现状：`spatial_analysis.py:44` 固定 `_PROJ_EPSG = 4548`（CM 117°E），4549 零引用，全国/全球计算系统性失真。
  做法：新增 `_resolve_projected_crs(gdf_wgs84) -> int`：
  ```
  lng0 = gdf 质心经度
  若在中国范围 (73.5~135.5): epsg = 4534 + round((round(lng0/3)*3 - 75)/3)
      # CGCS2000 3度带 CM 系列：4534=CM75E … 4548=CM117E … 4554=CM135E
  境外: UTM zone = floor((lng0+180)/6)+1; epsg = 32600+zone (北半球) / 32700+zone (南半球)
  ```
  替换全部 `to_crs(epsg=_PROJ_EPSG)` 调用点（buffer:209-229、overlay:235-257、voronoi、isochrone、clip:770-797 等）；删除 `_PROJ_EPSG` 常量与"4548/4549 双带"过时注释（4548 注释里的"中央经线 118°"也写错了，实为 117°E）。
  验证：乌鲁木齐（87°E）500m buffer 与 Haversine 逐点距离偏差 <1%；伦敦 POI buffer 落 UTM 30N。
  反模式守卫：不要按"输入城市名"选带——一律用数据质心经度；跨省大跨度数据取质心即可，不追求逐要素选带。
- [ ] **Task 3.2 统一国内/国外判定常量**
  `poi_query.py:281-288` 的 `is_china_bbox` (73,3)-(135,54) 与 `geo_transform.py:179-197` 的 `out_of_china` (73.66,3.86)-(135.05,53.55) 统一为同一常量（建议采用 geo_transform 的精确边界，提到 `geo_transform.py` 导出共用）；POI 落点后 crs 标注按**每个点的实际转换结果**而非 bbox 预判。
- [ ] **Task 3.3 CRS 语义领域类型化**
  新增轻量 `GeoLayer` dataclass（`gdf` + `crs_label: Literal["WGS84","GCJ02","PROJECTED"]`），`_ensure_wgs84` 入口对未标注输入显式失败（当前默认 WGS84 会把 GCJ02 数据二次偏转）。先覆盖 spatial_analysis.py 全部入口，data_io/map_layer 后置。测试：构造 attrs 丢失的 concat 场景，断言入口报错而非静默双偏转。
- [ ] **Task 3.4 requests 异常捕获修正**
  `spatial_analysis.py:499,621`：`except (TimeoutError, ConnectionError)` 改为 `except requests.exceptions.RequestException`（实测 `requests.Timeout` 非 builtin `TimeoutError` 子类，当前是死代码）；isochrone 的 empty message 区分"路网稀疏"与"路径服务超时"。同时统一 `geo_code.py:167-174`：解析高德 `infocode`，key 无效/限流返回 `status="error"` + error_code，真空值才 empty。
- [ ] **Task 3.5 POI 去重窗口高纬度修正 + 双源语义定版**
  `poi_query.py:397-399` 经度窗口乘 `cos(lat)`；明确"高德 OR OSM 互斥 fallback"语义并删掉空转的 `_deduplicate` 设施，**或**在高德结果不足时真正融合（二选一，建议先互斥+删设施）。
- [ ] **Task 3.6 raster 治理**
  临时文件统一进 `APP_WORKSPACE_DIR/tmp` 并挂请求生命周期清理（当前十余处 `delete=False` 从不清理）；`_safe_eval`（raster_analysis.py:636-655）下沉到 sandbox 执行或改 AST 白名单（只允许运算符 + np 函数名表）；RasterLayer bbox 出口统一转 WGS84→GCJ02；`raster_sampling` 前把点 `to_crs(src.crs)`；`_focal_window` 双重循环换 `scipy.ndimage.generic_filter`。

**验证：** 黄金坐标用例不变红；新增乌鲁木齐/伦敦 buffer 精度用例；高纬度（45°N）去重用例；高德 infocode 分类用例。

---

## Phase 4: 编排收敛（刹车、成本、上下文、resume）

- [ ] **Task 4.1 Ensemble 按 D3 处置**：默认关闭 = dispatcher prompt 不暴露 `parallel_redundant`（现状已是）+ registry 的 `enable_ensemble` 标 deprecated 注释；Task 1.3 的类型修复保留（防未来启用时踩雷）。voter 语义修正（vote_poi 改集合重叠多数派，voter.py:50-61）与 ensemble 入口接通**后置**到决定启用时再做。
- [ ] **Task 4.2 空成功判定**（dispatcher.py:432-443）：无 tool_results 且无 final_output 的终态判 `failed`（error_code=`EMPTY_RUN`）；工具因参数契约返回 empty 不再走 success 通道。
- [ ] **Task 4.3 `_resolve_ref` 契约对齐**（tool_execution.py:136-141 vs planner_factory.py:102-126）：改为**支持直传数据对象**（非 int 值透传给 handler），同时保留 int 索引兼容；prompt 里列出当前可用 session_vars 索引表。这修复约 40 个矢量 handler 在 code-mode 下不可用的问题。
- [x] **Task 4.4 迭代上限统一**（部分完成，见 `docs/superpowers/plans/2026-07-18-verifier-control-implementation.md`）：judge / judge_node / refine_router 均读 `state.max_iterations`（由 `spec.max_iterations` 初始化）；iteration 只在 `_planner_node` +1，refine_router 不再 +1。**未做**：删除/接线 `APP_SUB_AGENT_MAX_ITERATIONS` 文档字段。
- [x] **Task 4.5 resume 修复**（完成，见 `docs/superpowers/plans/2026-07-18-verifier-control-implementation.md`）：① resume 先 load/match pending → 校验 checkpoint 存在 → 校验 answer/patch，成功接管后才 `store.clear`；no_checkpoint / invalid_answer 保留 PendingTask。② resume 将 `original_request` + `resume_patch` 并入 user_input 后 re-plan，不复用旧 plan。根 checkpoint 使用空 namespace（不再错误使用 `checkpoint_ns="_root"`）。
- [ ] **Task 4.6 多轮上下文接通**：`run_react_loop(history=...)`（tool_execution.py:1996-2002 当前明确忽略）改为注入 root planner 的消息列表（截断到 APP_CONTEXT_WINDOW）；`_enrich_goal_from_deps` 不再 fallback 到 failed outcome，dep 失败的下游任务标 `DEPENDENCY_FAILED` 跳过。
- [x] **Task 4.7 成本刹车**（部分完成，见 `docs/superpowers/plans/2026-07-18-verifier-control-implementation.md`）：**已砍** `_verify_outcome_independent`（成功 poi/geometer outcome 不再 spawn 嵌套 verifier sub-agent）。Verifier LLM 故障 fail-open；空泛 reject 归一 approve。**未做**：CostTracker per-run 单例与 `APP_MAX_COST_TOKENS` 硬限；确定性质量门留给 Task 5.5。
- [ ] **Task 4.8 体系外状态收编**：judge 全局计数器（judge.py:28-39）移入 SubAgentState；`ALWAYS_VISIBLE_TOOLS` 模块级改写（toolkit/registry.py:313-323）改为构造时返回新集合；metrics 无界 list 加环形上限。
- [ ] **Task 4.9 SSE 断连取消**（chat.py:396-415）：generator 取消时调 `run_ctrl.request_cancel()`，dispatch 在 batch 边界（已有检查点 dispatcher.py:315-322）+ sub-agent 迭代边界检查取消标志；ensemble 路径补控制信号检查。
- [ ] **Task 4.10 事件链接线（B13）**：`run_react_loop` 增加 `on_event` 参数，用 **contextvar** 传入节点（callable 不可 pickle 进 SqliteSaver checkpoint，禁止写入 state 通道）；`EventCollector.clear_dedup` 挂到 run 结束钩子。前端真流式 react_trace 随之打通（替换当前事后补发）。

**验证：** 黄金回归集 + 新增：follow-up"它附近的咖啡店"可解析；超成本上限运行被中止；断连后 LLM 调用在 1 个迭代边界内停止；wiring 冒烟断言 hooks 四个 emit 点全部触发。

---

## Phase 5: 吸收 PineFlow 机制（可校验性 × 可审计性）

> 原则：**抄机制不抄代码**；每个机制落到 Gismind 已有模块上，不新建平行系统。

- [ ] **Task 5.1 YAML 工具契约（单一事实源）**（按 D5 先 12 个核心工具）
  参照 PineFlow `tools/contracts/defs/semantic/*.yaml` + `tool_definitions.py:514-531`：每个工具一份 YAML（name/语义描述/槽位定义含类型与单位/executor_type/prompt 文案/校验规则挂点/display 模板）。新建 `backend/resources/tools/*.yaml` + `app/agents/contracts/loader.py`，由其**派生**：① TOOL_SPECS 注册；② `_build_code_mode_prompt` 的工具文档段；③ 校验规则。这根治"prompt 写了但 namespace 没有"类脱节（B12 的结构性根因）。
  反模式守卫：不要手写第二份工具清单——Python 侧只读 YAML；`additionalProperties` 保持 False（PineFlow 的 True 是其弱点，schema 宽=校验全靠规则层）。
- [ ] **Task 5.2 规则网关 critical 化**：Phase 1 修好注册后，把 `BEFORE_TOOL_CALL` hook 真正接线（hooks/builtins.py:25-48 当前从不触发），preflight hard issue = 阻断执行返回 LLM 可读的 RepairProposal（critical=True 不可绕过，参照 PineFlow builtins.py:29-37）。删除 `_planner_node` 与 proxy 里的内联重复实现（双轨制合并到 hook 轨）。
- [x] **Task 5.3 PendingTask 完整化**（完成，见 `docs/superpowers/plans/2026-07-18-verifier-control-implementation.md`）：PendingTask 已有 `missing_slots` / `slot_patch_schema` / `choices` / `correction_history`（保留 `candidates` 兼容）；`to_dict`/`from_dict` 往返；sync 桥接改为 `_run_sync`（无 running loop 用线程本地 loop；有 running loop 时 daemon 线程），避免 `run_coroutine_threadsafe` 自阻塞。受限 `needs_input` 经 `verifier_pending_task` → judge AWAITING_INPUT 写入 PendingStore。
- [ ] **Task 5.4 图层引用解析升级**：`workspace/resolver.py` 对齐 PineFlow `core/state_tree.py:123-143` + `layer_reference_resolver.py`：支持别名、"latest/最新的/最终的"中英文指代、模糊短语；与 Task 4.3 的直传数据对象共存（解析优先级：int 索引 > 别名/指代 > 数据对象透传）。
- [ ] **Task 5.5 结果质量门**：assemble 前新增确定性检查（最终图层 0 要素、声明了导出但无导出产物、POI 结果全为空却宣称成功），命中则打回 refine 一次（参照 PineFlow `result_quality_gate.py:34-89`，"只放行一次"防循环）。
- [ ] **Task 5.6 结构化事件与审计**：事件链接通（Task 4.10）后，新增 `app/audit/run_report.py` 从事件流（而非人读文本）还原审计事实：调用了哪些工具、参数、产物路径、耗时、成本（参照 PineFlow `report_audit.py:44-77`）。**这项对软著材料直接有用**（操作手册可附运行审计样例）。
- [ ] **Task 5.7 ExecutionMemory 对齐**：`duplicate_guard.py` 的指纹字段改为白名单制（参照 PineFlow `execution_memory.py:13-26` 的 SIGNATURE_FIELDS），当前排除法易漏。
- [ ] **Task 5.8（可选演进，需单独决策）**：sub-agent 内循环增加第三种模式 `tool_call_mode`（PineFlow 式单步 native tool call），与 code_mode/JSON 模式并存，按角色灰度。这是"JSON call 骨架 + code 逃生舱"终态——工作量大（prompt 体系、observer/judge 全要分叉），建议软著材料稳定后再评估。

**验证：** YAML 增删一个工具不改 Python 即可用；规则网关阻断用例（地理 CRS 下米制缓冲被拒）；审计报告与事件流一致。

---

## Phase 6: 前端 Critical + 契约落地

- [ ] **Task 6.1** 停止按钮收尾（ChatPanel.tsx:314 + useSSE.ts:103）：handleStop 直接置 `status:'error', code:'CANCELLED'`。
- [ ] **Task 6.2** SSE 事件 session 守卫（ChatPanel.tsx:56-64）：handleSend 快照 sid，回调内 `if (sid !== sessionIdRef.current) return`。
- [ ] **Task 6.3** `renderLayersOnMap` per-feature try/catch（mapRenderers.ts:144-231），坏图层 console.warn 跳过。
- [ ] **Task 6.4** useSSE 断流判定：未收到 done/error 终端事件时走 onError；SSE switch 加 `default: console.warn`；`awaiting_input` 渲染为提示块并解除输入禁用（这是 judge/verifier 流程的前端最后一公里）。
- [ ] **Task 6.5** raster 渲染收敛到 `renderLayersOnMap` 单路径（删 RasterOverlay 的 setFitView）；heatmap 要么接 AMap.HeatMap 插件要么地图中央明确占位（契约 docs/02_data_models.md:376-382 有 HeatmapLayer）。
- [ ] **Task 6.6** 文档回写：docs/02_data_models.md §4 补齐 RasterLayer、status 枚举（voting/reviewing/reflecting）、react_trace/sub_task 事件——前端 types/message.ts 已领先文档。

**验证：** `npm run typecheck` 全绿；手动回归 MANUAL_TESTING.md 的 SSE 相关用例 + 新增"停止按钮""快速切换会话""畸形图层"三项。

---

## Phase 7: 防回归与文档对齐（Final Verification）

- [ ] **Task 7.1 契约测试进 CI**：handler 签名断言、`run_sub_agent` 返回 schema 断言、wiring 冒烟（hooks emit 点 ×4、preflight `_RULES` 非空、kernel 工具可达、事件链端到端）。
- [ ] **Task 7.2 反模式 grep 巡检**：`_PROJ_EPSG` 零引用；`except (TimeoutError, ConnectionError)` 零匹配；`default=str` 不出现在 session_vars 过滤；`_real_import` 不出现在 sitecustomize 模块属性。
- [ ] **Task 7.3 全量测试 + MANUAL_TESTING.md 手动回归**。
- [ ] **Task 7.4 文档对齐**：CLAUDE.md（APP_SUB_AGENT_MAX_ITERATIONS、root CONTINUE 描述、ensemble 状态、本计划新增的 APP_WORKSPACE_DIR）；docs/06_security.md 写明沙箱真实边界（"进程内 hook 只防误操作，资源隔离靠子进程 + Job Object"）；升级计划 `2026-07-17-code-mode-upgrade.md` 标注完成状态。

---

## 决策依据摘要（为什么这样排）

1. **Phase 1 先于一切**：当前主链路不可用（B1 让所有 code-mode 工具调用必炸），其余工作在坏地基上没有意义。
2. **code executor 改进而非废弃**：它是产品差异点，且 DeepSeek 级模型代码能力不弱；但安全边界必须从"AST+namespace 祈祷"收缩到"子进程+白名单 RPC"，这是 PineFlow worker 架构（worker.py:112-135）证明可行的形态。
3. **PineFlow 机制吸收排序按"修复已有接线"优先于"新增能力"**：规则网关/Task 5.2 是 B10 的自然延伸；YAML 契约放 Phase 5 因为它依赖 Phase 1-4 的稳定接口。
4. **投影问题用动态选带而非修 4549**：双带硬编码的设计本身错误——中国跨 35 个经度，任何常量组合都覆盖不了，质心动态选带 + UTM fallback 是一次性根治。
