# Verifier 控制边界与成本刹车设计

> **历史文档 / 部分被替代（2026-08-09）**：普通角色已移除 Judge，由 `native_step_finalize_node` 接管确定性完成、局部重试及 PendingStore 持久化；Judge 仅保留给 coder。其余 Verifier/refinement 与 `/resume` 安全约束仍适用。当前架构以主技术文档 v2.1 为准。

**日期：** 2026-07-18  
**状态：** 已确认设计，待实施计划  
**关联：** Remediation Plan Phase 4（Task 4.4、4.5、4.7）与 Phase 5（Task 5.3、5.5）

## 目标

让 Gismind 的 Verifier 能受控地兜底请求用户输入，同时保持“缺槽与消歧由确定性规则决定”的边界；消除 Verifier 故障、低置信度与空泛拒绝导致的无效 refine 循环及额外 LLM 成本。

本设计不把暂停决策权交给 LLM。preflight 规则和 PendingTask 是用户输入暂停的主路径；Verifier 仅提供经代码验证的兜底信号。

## 非目标

- 不新增由 LLM 自由判断的澄清问答能力。
- 不改变工具 preflight 的规则语义或绕过 critical rule。
- 不在本任务实现 YAML 工具契约、完整 RulesGateway 重构或通用质量门细则；这些工作仍归属 Remediation Plan Phase 5。
- 不保留每个成功 outcome 额外启动完整 verifier sub-agent 的独立审查路径。

## 当前链路与问题

当前 sub-agent 链路为：

```text
planner -> tools -> observer -> verifier -> judge | refine_router -> planner
```

- `build_sub_agent._route_after_verifier_sub` 只按 `approved` 二分。
- `verifier_node._call_verifier` 在 LLM 或解析故障时构造 `approved=False`，导致无意义 refine。
- `refine_router` 将 `approved=True && confidence < 0.6` 也路由为 refine，并接受没有可执行 hint 的拒绝。
- dispatcher 的 `_verify_outcome_independent` 会为每个成功 outcome 再运行一个完整 verifier sub-agent，成本与延迟不成比例。
- `judge`、`PendingTask` 与 `PendingStore` 已存在，但 resume 流程与 PendingTask 字段尚未完整化。

## 设计决策

### 1. Preflight 是唯一的主暂停入口

当 preflight 规则发现用户可补的缺槽或地名/图层消歧选择时，规则层直接构造 `pending_task`。该状态由现有 Judge 路径：

```text
pending_task -> judge AWAITING_INPUT -> PendingStore -> judge.awaiting_input SSE
```

持久化并向前端发送。Verifier 不得覆盖、删除或降级 preflight 已生成的 pending task。

### 2. Verifier 使用受限的第三态兜底

`VerifierOutput` 新增字段：

```python
needs_input: bool = False
missing_slots: list[str] = []
choices: list[dict[str, Any]] = []
input_reason: str | None = None
verifier_unavailable: bool = False
invalid_rejection: bool = False
```

`approved` 保持兼容，`needs_input=True` 不是可自由使用的“暂停按钮”。Verifier prompt 只能在以下情形声明它：

- 用户专属参数缺失，例如距离、目标图层或输出格式；
- 多候选对象必须由用户选择；
- 无法从已有上下文、工具调用、重试或重新规划推断的信息。

路由代码必须通过确定性 `is_user_resolvable_input_request(output)` 验证，验证条件为：

1. `approved is False` 且 `needs_input is True`；
2. `missing_slots` 或 `choices` 至少一个非空；
3. 所有 slot 属于由工具契约/规则层提供的允许集合，或 choices 具有稳定候选标识；
4. `input_reason` 非空，且不属于工具错误、数据为空、模型不确定、计算失败等可由系统处理的原因；
5. 当前 state 不存在 preflight 已生成的 `pending_task`。

验证通过才将输出转成 PendingTask；否则它按常规 approve/reject 处理。该验证函数是 LLM 输出和状态机之间的唯一转换边界。

### 3. Verifier fail-open，但质量门 fail-closed

Verifier 的作用是建议 refine，不是系统正确性的唯一防线。

- LLM 调用、JSON 解析或 schema 验证失败时，构造 `approved=True`、`verifier_unavailable=True` 的输出，写 warning/metric，不触发 refine。
- 确定性结果质量门仍在 assemble 前检查客观错误（如声明成功但最终图层为 0 要素、宣称导出却无产物）。质量门可以要求一次受限的处理，不依赖 Verifier 的 fail-closed 行为。
- 仅失败或确定性质量门标记为低可信的 outcome 可以进入成本受控的 LLM 审查；成功 outcome 不再启动完整 verifier sub-agent。

### 4. Refine 的有效性门

普通 reject 只有同时满足以下条件才会进入 refine：

- `approved is False`；
- 不是已获确认的合法 `needs_input`；
- `refinement_hints` 中至少有一条非空且可执行；
- hint 不是泛化模板，例如“改进”“重新检查”“再试一次”。

不合格 reject 被规范化为 `approved=True` 并标记 `invalid_rejection=True`，供日志、指标和后续 prompt 调优使用。`confidence` 仅用于观测与触发质量门抽样，不能推翻 `approved=True`。

### 5. 统一迭代上限

- 唯一计数源是 `SubAgentState.iteration`。
- 仅 `_planner_node` 在每次 planner 执行后将其加一。
- `refine_router` 不再写 iteration；有效 refine 通过回到 planner 自然消耗下一次 iteration。
- `judge`、`refine_router` 与所有角色均读取 registry `spec.max_iterations`，不维护第二套阈值。
- 达到上限时 Judge 以统一的 termination cause 返回已有部分结果，不能由 Verifier 再触发新的 refine。

### 6. PendingTask 与 resume 完整化

PendingTask 增加：

- `slot_patch_schema`：用户答复可写入字段及类型/单位约束；
- `choices`：带稳定 id 的可选项；
- `correction_history`：每次答复、验证结果与时间戳；
- 现有 `missing_slots` 继续保留。

resume 顺序：

1. 加载并校验 PendingTask 与 `sub_agent_run_id`；
2. 先验证 checkpoint 是否存在，未找到时不得清除 pending；
3. 依据 `slot_patch_schema` 解析并验证用户答案；
4. 将有效 patch 合并到原 SubTask goal/上下文并重新规划；
5. 仅成功交接后清除 PendingStore，失败时保留 pending 并返回可修正说明。

## 状态机路由

```text
preflight user-resolvable issue
  -> pending_task
  -> judge AWAITING_INPUT
  -> PendingStore + SSE

observer
  -> verifier
  -> valid needs_input + deterministic validation
       -> pending_task -> judge AWAITING_INPUT
  -> approved / verifier unavailable / invalid rejection
       -> judge
  -> reject with actionable hints
       -> refine_router -> planner
```

若 verifier 产生无效 `needs_input`，不得暂停；若产生无效 reject，按 approve 进入 Judge。任何 preflight pending 都优先于 Verifier 输出。

## 可观测性

新增或统一记录以下事件/指标：

- `verifier.fail_open`：故障类型、run id、是否由质量门覆盖；
- `verifier.invalid_rejection`：拒绝原因与被拒绝的 hints 分类；
- `verifier.needs_input_accepted` / `verifier.needs_input_rejected`：确定性验证结果；
- `refine.applied`：iteration、有效 hint 数、统一上限；
- `pending.resume_validation_failed`：字段验证失败但 PendingTask 未清除。

不得记录用户答复中的敏感原文；事件只保留 slot 名、choice id 与验证状态。

## 测试与验收

### 单元测试

1. preflight 缺距离或缺必需图层直接生成 pending，不调用 Verifier。
2. Verifier 的合法缺槽和合法候选选择可转为 PendingTask。
3. 空 slot、未知 slot、无稳定 id 的 choices、工具错误/空数据类理由均不能让 Verifier 暂停。
4. LLM 异常、无效 JSON 与 schema 失败均生成 fail-open approval 标记。
5. `approved=True` 在任意 confidence 下不 refine。
6. 空 hint 或泛化 hint 的 reject 被标为 invalid rejection 并不 refine。
7. 带明确、可操作 hint 的 reject 正确回 planner。
8. iteration 只由 planner 递增，refine 不单独递增；达到 `spec.max_iterations` 后不再回 planner。
9. `_verify_outcome_independent` 不再对成功 outcome 调用 `run_sub_agent(agent_role="verifier")`。

### 集成测试

1. `preflight -> AWAITING_INPUT -> PendingStore -> SSE -> resume` 全链路保持可用。
2. verifier 兜底产生合法 needs_input 时走同一 PendingStore/SSE 流程。
3. checkpoint 缺失或用户 patch 无效时 pending 不被清除。
4. 成功 outcome 不产生额外 verifier sub-agent；确定性质量门的失败路径仍被记录。

## 文件级实施范围

- `backend/app/agents/schemas.py`：扩展 VerifierOutput、PendingTask。
- `backend/app/agents/verifier_node.py`：prompt、解析与 fail-open 输出。
- `backend/app/agents/refine_router.py`：合法 input 请求验证、拒绝质量门、统一迭代路由。
- `backend/app/agents/build_sub_agent.py`：三态 verifier 后路由与 pending 转接。
- `backend/app/agents/judge.py`：保持 pending 优先，统一上限语义。
- `backend/app/agents/pending.py`：扩展序列化与安全 async/sync 调度。
- `backend/app/agents/dispatcher.py`：删除成功 outcome 的嵌套 verifier，修复 resume 交接顺序。
- `backend/app/api/chat.py`：resume 保留 pending 直至 checkpoint 与 patch 都验证通过。
- 对应 `backend/tests/unit/` 与 `backend/tests/integration/`：新增上述覆盖。

## 设计自检

- 无 TBD、TODO 或未决实现分支。
- 暂停主权明确归属 preflight/规则代码，Verifier 只受限兜底。
- fail-open 仅适用于建议性 Verifier；客观质量仍由确定性质量门保障。
- 迭代计数、PendingTask 生命周期与调用成本均有单一责任边界。
