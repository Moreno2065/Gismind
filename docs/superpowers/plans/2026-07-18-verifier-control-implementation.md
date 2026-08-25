# Verifier 控制边界与成本刹车 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Verifier 收敛为受控建议层：缺槽暂停由确定性 preflight/PendingTask 主导，Verifier 故障与空泛拒绝不再制造 refine 循环或额外 verifier 子图成本。

**Architecture:** 保持现有 `planner -> tools -> observer -> verifier -> judge/refine_router` 图。`VerifierOutput` 扩展受限 `needs_input` 信号，`refine_router` 用纯函数验证该信号、筛除空泛 reject，并将合法请求转成现有 `judge` 消费的 `pending_task`。LLM Verifier fail-open；成功 outcome 的二次 verifier 子图移除，后续确定性质量门保留独立职责。

**Tech Stack:** Python 3、FastAPI、Pydantic v2、LangGraph、pytest、fakeredis。

---

## 文件结构与责任

| 文件 | 责任 |
|---|---|
| `backend/app/agents/schemas.py` | Verifier 与 PendingTask 的可序列化契约。 |
| `backend/app/agents/verifier_node.py` | Verifier prompt、LLM 调用与 fail-open fallback。 |
| `backend/app/agents/refine_router.py` | 输出规范化、受限 needs-input 验证、refine 路由。 |
| `backend/app/agents/build_sub_agent.py` | 将 verifier 产生的合法 pending 送入 Judge。 |
| `backend/app/agents/pending.py` | PendingTask Redis 序列化与安全同步桥接。 |
| `backend/app/agents/dispatcher.py` | resume 目标重建；去除成功 outcome 的嵌套 verifier。 |
| `backend/app/api/chat.py` | checkpoint/patch 验证完成后才清 pending。 |
| `backend/tests/unit/test_verifier_control.py` | Verifier schema、fail-open、拒绝质量门与路由纯函数测试。 |
| `backend/tests/integration/test_awaiting_input_e2e.py` | PendingTask 扩展字段、resume 生命周期及 SSE 集成回归。 |
| `backend/tests/unit/test_dispatcher.py` | 不再启动嵌套 verifier，以及 resume re-plan 行为。 |

本工作区根目录不是 Git repository，因此本计划不包含 Git 提交步骤；实施者不得通过初始化仓库或在未获授权时执行 Git 变更代替验证。

### Task 1: 扩展数据契约并锁定序列化行为

**Files:**
- Modify: `backend/app/agents/schemas.py:25-101`
- Test: `backend/tests/unit/test_verifier_control.py`

- [x] **Step 1: 写入 PendingTask 与 VerifierOutput 的失败测试**

```python
from app.agents.schemas import PendingTask, VerifierOutput


def test_verifier_output_defaults_to_non_pending_approval_metadata():
    output = VerifierOutput(approved=True, reason="ok")
    assert output.needs_input is False
    assert output.missing_slots == []
    assert output.choices == []
    assert output.verifier_unavailable is False
    assert output.invalid_rejection is False


def test_pending_task_round_trips_resume_contract_fields():
    task = PendingTask(
        sub_agent_run_id="run-1",
        original_request="在南京缓冲",
        missing_slots=["distance"],
        slot_patch_schema={"distance": {"type": "number", "unit": "m"}},
        choices=[{"id": "poi-a", "label": "夫子庙"}],
        correction_history=[{"answer": "500米", "valid": True}],
    )
    restored = PendingTask.from_dict(task.to_dict())
    assert restored.slot_patch_schema["distance"]["unit"] == "m"
    assert restored.choices[0]["id"] == "poi-a"
    assert restored.correction_history[0]["valid"] is True
```

- [x] **Step 2: 运行测试，确认当前契约缺字段而失败**

Run: `cd backend && python -m pytest tests/unit/test_verifier_control.py -q`

Expected: FAIL，指出 `VerifierOutput` 或 `PendingTask` 缺少新字段。

- [x] **Step 3: 以 Pydantic/dataclass 默认工厂扩展契约**

在 `VerifierOutput` 加入下列字段，列表/字典必须使用 `Field(default_factory=...)`：

```python
needs_input: bool = False
missing_slots: list[str] = Field(default_factory=list)
choices: list[dict[str, Any]] = Field(default_factory=list)
input_reason: Optional[str] = None
verifier_unavailable: bool = False
invalid_rejection: bool = False
```

在 `PendingTask` 加入：

```python
slot_patch_schema: dict[str, Any] = field(default_factory=dict)
choices: list[dict[str, Any]] = field(default_factory=list)
correction_history: list[dict[str, Any]] = field(default_factory=list)
```

同时让 `to_dict()` 与 `from_dict()` 保留所有这些字段；`candidates` 保持现有兼容字段，不在本任务删除或重命名。

- [x] **Step 4: 运行契约测试**

Run: `cd backend && python -m pytest tests/unit/test_verifier_control.py -q`

Expected: PASS。

### Task 2: Verifier fail-open 与拒绝质量门

**Files:**
- Modify: `backend/app/agents/verifier_node.py:22-31,118-144`
- Modify: `backend/app/agents/refine_router.py:22-124`
- Test: `backend/tests/unit/test_verifier_control.py`

- [x] **Step 1: 写入 fail-open、低置信度与有效 hint 的失败测试**

```python
from app.agents.refine_router import refine_router


def test_verifier_failure_is_approved_with_observability_marker(monkeypatch):
    monkeypatch.setattr("app.agents.verifier_node.create_llm", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    from app.agents.verifier_node import _call_verifier
    result = _call_verifier({"mode": "json"})["verifier_output"]
    assert result["approved"] is True
    assert result["verifier_unavailable"] is True


def test_approved_low_confidence_does_not_refine():
    state = {"iteration": 1, "max_iterations": 3, "verifier_output": {
        "approved": True, "reason": "complete", "confidence": 0.1,
    }}
    assert refine_router(state) == {"should_stop": False}


def test_generic_reject_is_normalized_to_approval():
    state = {"iteration": 1, "max_iterations": 3, "verifier_output": {
        "approved": False, "reason": "请改进", "refinement_hints": ["重新检查"],
    }}
    result = refine_router(state)
    assert result["should_stop"] is False
    assert result["verifier_output"]["approved"] is True
    assert result["verifier_output"]["invalid_rejection"] is True


def test_actionable_reject_returns_refinement_message():
    state = {"iteration": 1, "max_iterations": 3, "verifier_output": {
        "approved": False, "reason": "缓冲距离未使用米", "refinement_hints": ["将 distance=500 并使用单位 m"],
    }}
    result = refine_router(state)
    assert result["should_stop"] is False
    assert "distance=500" in result["messages"][0].content
```

- [x] **Step 2: 运行测试，确认当前实现 fail-closed 且低置信度会 refine**

Run: `cd backend && python -m pytest tests/unit/test_verifier_control.py -q`

Expected: FAIL，因 Verifier fallback 为 `approved=False` 且低 confidence 会进入 refine。

- [x] **Step 3: 令 `_call_verifier` 在异常时 fail-open**

将异常分支替换为：

```python
logger.warning("verifier LLM unavailable; fail-open: %s", e)
out = VerifierOutput(
    approved=True,
    reason="Verifier unavailable; deterministic quality gates remain active",
    confidence=0.0,
    verifier_unavailable=True,
)
```

更新 JSON 与 code-mode prompt：reject 必须给出可执行的 `refinement_hints`；只有不可由系统、工具或重试补足的必填参数/消歧候选才能设置 `needs_input=true`，并同时提供 `missing_slots` 或带 `id` 的 `choices`。

- [x] **Step 4: 在 refine_router 添加纯规范化函数并移除置信度分支**

新增：

```python
_GENERIC_HINTS = {"改进", "请改进", "重新检查", "再试一次", "重试"}


def _has_actionable_hints(hints: list[str]) -> bool:
    return any(hint.strip() and hint.strip() not in _GENERIC_HINTS for hint in hints)


def _approve_invalid_rejection(verifier: VerifierOutput) -> VerifierOutput:
    return verifier.model_copy(update={
        "approved": True,
        "invalid_rejection": True,
        "needs_input": False,
    })
```

`verifier is None` 也视为 fail-open，返回 `{"should_stop": False}`。当 `approved=True` 时立即进入 Judge，不再读取 `confidence`。当 reject 没有 `_has_actionable_hints(...)` 时，返回包含已规范化 `verifier_output` 的 approve delta；只有有效 hint 可构造 refine message。不要在此函数递增 `iteration`。

- [x] **Step 5: 运行 Task 2 测试**

Run: `cd backend && python -m pytest tests/unit/test_verifier_control.py -q`

Expected: PASS。

### Task 3: 受限 Verifier needs_input 转换为 PendingTask

**Files:**
- Modify: `backend/app/agents/refine_router.py`
- Modify: `backend/app/agents/build_sub_agent.py:235-245`
- Test: `backend/tests/unit/test_verifier_control.py`

- [x] **Step 1: 编写合法与非法 needs_input 的失败测试**

```python
from app.agents.refine_router import verifier_pending_task


def test_valid_choice_request_becomes_pending_task():
    pending = verifier_pending_task({
        "user_input": "查夫子庙附近咖啡", "run_id": "r1",
        "verifier_output": {
            "approved": False, "needs_input": True,
            "reason": "地名有多个候选", "input_reason": "请选择具体地点",
            "choices": [{"id": "amap:1", "label": "南京夫子庙"}],
        },
    })
    assert pending["sub_agent_run_id"] == "r1"
    assert pending["choices"][0]["id"] == "amap:1"


def test_unknown_slot_cannot_pause():
    assert verifier_pending_task({
        "verifier_output": {
            "approved": False, "needs_input": True,
            "missing_slots": ["invented_slot"], "input_reason": "需要信息",
        },
    }) is None


def test_system_error_reason_cannot_pause():
    assert verifier_pending_task({
        "verifier_output": {
            "approved": False, "needs_input": True,
            "missing_slots": ["distance"], "input_reason": "工具调用失败",
        },
    }) is None
```

- [x] **Step 2: 运行测试，确认 helper 尚不存在**

Run: `cd backend && python -m pytest tests/unit/test_verifier_control.py -q`

Expected: FAIL，导入 `verifier_pending_task` 失败。

- [x] **Step 3: 实现确定性转换边界**

在 `refine_router.py` 定义并测试 `verifier_pending_task(state) -> dict | None`。它必须：

```python
_ALLOWED_USER_SLOTS = {"distance", "output_path", "output_format", "target_layer"}
_SYSTEM_RESOLVABLE_TERMS = ("工具", "超时", "数据为空", "模型", "计算失败", "重试")
```

- 先拒绝 state 已有 `pending_task`、`approved=True`、`needs_input=False`、空 reason、reason 含系统可处理词、空 slots/choices。
- slots 必须全在 `_ALLOWED_USER_SLOTS`；choices 中每一项必须是 dict 且有非空 `id`。
- 返回 Judge 所需的 dict：`sub_agent_run_id`、`original_request`、`missing_slots`、`choices`、`message`、`issues=[]`、`slot_patch_schema={}`、`correction_history=[]`。
- 不写 Redis、不发 SSE、不调用 LLM。

在 `refine_router` 优先调用该 helper。成功时返回：

```python
{
    "should_stop": False,
    "pending_task": pending,
    "verifier_output": verifier.model_dump(),
}
```

在 `_route_after_refine` 中先检查 `state.get("pending_task")`，命中时路由到新图边 `judge`；否则维持 end/planner 行为。这样只由现有 `judge` 执行 PendingStore 写入及 SSE 发射。

- [x] **Step 4: 运行 needs_input 路由测试**

Run: `cd backend && python -m pytest tests/unit/test_verifier_control.py -q`

Expected: PASS。

### Task 4: 修复 PendingStore 同步桥接并保证 resume 不丢任务

**Files:**
- Modify: `backend/app/agents/pending.py:78-116`
- Modify: `backend/app/api/chat.py:636-659`
- Modify: `backend/app/agents/dispatcher.py:187-205`
- Test: `backend/tests/integration/test_awaiting_input_e2e.py`

- [x] **Step 1: 写入 checkpoint 缺失与非法 patch 保留 pending 的失败测试**

```python
async def test_resume_no_checkpoint_keeps_pending_store(client, fake_redis, sample_pending_task):
    from app.agents.pending import PendingStore
    await PendingStore().save("sess-1", sample_pending_task)
    with patch("app.agents.checkpointer.get_sqlite_checkpointer"), \
         patch("app.agents.dispatcher.build_dispatcher") as build:
        build.return_value.get_state.return_value = None
        response = client.post("/api/chat/sess-1/resume", json={
            "sub_agent_run_id": sample_pending_task.sub_agent_run_id, "answer": "500米",
        })
    assert response.json()["status"] == "no_checkpoint"
    assert await PendingStore().load("sess-1") is not None
```

补充 `slot_patch_schema={"distance": {"type": "number", "unit": "m"}}` 的 fixture，并断言无法解析的答复返回 `status="invalid_answer"` 且 PendingStore 仍存在；合法答复才由 dispatcher 消费。

- [x] **Step 2: 运行相关集成测试，确认当前先 clear 的行为失败**

Run: `cd backend && python -m pytest tests/integration/test_awaiting_input_e2e.py -q`

Expected: FAIL，`no_checkpoint` 后 pending 已被清除。

- [x] **Step 3: 替换 PendingStore 的死锁风险同步包装**

在 `pending.py` 添加私有 `_run_sync(coro)`：无 running loop 时用线程本地持久 event loop 的 `run_until_complete`；已有 running loop 时启动独立 daemon 线程并在其中创建/复用 loop，`join()` 后重新抛异常。`save_sync`、`load_sync`、`clear_sync` 全部只调用 `_run_sync`，不得调用当前 loop 的 `run_coroutine_threadsafe(...).result()`。

- [x] **Step 4: 在 API 端先校验再清除**

`resume_chat` 必须按顺序：load/match PendingTask -> 构建 dispatcher -> `get_state(config)` -> 若无 checkpoint 返回 `no_checkpoint`（不 clear） -> 用 `slot_patch_schema` 验证 answer -> 将 answer 作为恢复输入和 pending context 交给 dispatcher -> dispatcher 成功接管后 `await store.clear(session_id)`。

最小 patch parser：对 `type == "number"` 使用正则提取一个十进制数字，失败返回 `invalid_answer`；其余无 schema 的答复保持原样。将规范化 patch 以 `resume_patch` 写进 state，新增 `resume_patch: dict[str, object]` 到 `AgentRootState`。

`planner_router_node` 在检测 pending + `resume_patch` 时构建新 `user_input`：

```python
resume_context = json.dumps(state["resume_patch"], ensure_ascii=False)
user_input = f"{pending['original_request']}\n\n用户补充参数：{resume_context}"
```

随后走正常 planner LLM 分支获得新 TaskPlan；不要复用旧 plan，也不要在该节点清 PendingStore。

- [x] **Step 5: 运行 Pending/resume 集成测试**

Run: `cd backend && python -m pytest tests/integration/test_awaiting_input_e2e.py -q`

Expected: PASS。

### Task 5: 统一迭代边界并移除成功 outcome 嵌套 Verifier

**Files:**
- Modify: `backend/app/agents/refine_router.py`
- Modify: `backend/app/agents/dispatcher.py:101-123,673-688`
- Modify: `backend/tests/unit/test_dispatcher.py`
- Test: `backend/tests/unit/test_verifier_control.py`
- Test: `backend/tests/unit/test_dispatcher.py`

- [x] **Step 1: 写入不启动嵌套 verifier 的失败测试**

```python
import asyncio
from unittest.mock import MagicMock, patch

from app.agents.dispatcher import _dispatch_single
from app.agents.schemas import SubTask


def test_successful_poi_dispatch_skips_independent_verifier():
    results, dispatched, events = {}, {}, []
    legacy_verifier = MagicMock()
    raw_state = {
        "agent_role": "poi", "iteration": 1,
        "tool_results": [{"status": "success", "data": {"pois": []}}],
        "final_output": {"summary": "找到结果"},
    }
    with patch("app.agents.build_sub_agent.run_sub_agent", return_value=raw_state), \
         patch("app.agents.dispatcher._verify_outcome_independent", legacy_verifier):
        asyncio.run(_dispatch_single(
            {"task_plan": {"tasks": [{"id": "t1"}]}},
            SubTask(id="t1", agent_role="poi", goal="查询咖啡"),
            results, dispatched, events,
        ))
    legacy_verifier.assert_not_called()
```

另写测试：有效 refine 在 `iteration == max_iterations` 时返回 `REFINE_LIMIT_EXCEEDED`，低于上限时不返回或修改 `iteration` 字段。

- [x] **Step 2: 运行测试，确认当前成功 dispatch 会调用独立 verifier**

Run: `cd backend && python -m pytest tests/unit/test_dispatcher.py tests/unit/test_verifier_control.py -q`

Expected: FAIL，`legacy_verifier.assert_not_called()` 失败。

- [x] **Step 3: 删除嵌套 verifier 路径**

删除 `_verify_outcome_independent`（`dispatcher.py:101-123`）、`_dispatch_single` 中 `o.status == "success" and o.agent_role in {"poi", "geometer"}` 的独立 verifier 分支（`dispatcher.py:673-688`）以及旧的 `test_independent_verifier_calls_run_sub_agent`。同时移除只被该函数使用的 `VerifierOutput` import。

确定性质量门将在 Phase 5 Task 5.5 独立实现，本任务只消除重复 LLM 审查，不能用空实现或另一种 LLM 调用替换该分支。

确保 `refine_router` 仅比较 state 的 `iteration` 与 state 初始化时来自 `spec.max_iterations` 的 `max_iterations`；不要增加第二套常数，且不在任何 return delta 包含 `iteration`。

- [x] **Step 4: 运行调度与迭代边界测试**

Run: `cd backend && python -m pytest tests/unit/test_dispatcher.py tests/unit/test_verifier_control.py -q`

Expected: PASS。

### Task 6: 全链路回归与文档对齐

**Files:**
- Modify: `docs/superpowers/plans/2026-07-17-gismind-remediation-plan.md:149-155,169-172`（仅勾选/更新已完成任务时）
- Test: `backend/tests/unit/test_verifier_control.py`
- Test: `backend/tests/integration/test_awaiting_input_e2e.py`

- [x] **Step 1: 运行目标测试集**

Run: `cd backend && python -m pytest tests/unit/test_verifier_control.py tests/unit/test_dispatcher.py tests/integration/test_awaiting_input_e2e.py -q`

Expected: PASS。

- [x] **Step 2: 运行全量后端回归**

Run: `cd backend && python -m pytest tests/ -q`

Expected: PASS，允许项目现有明确标记的 skip；任何新增失败必须在继续前修复。

- [x] **Step 3: 更新总体计划状态**

只有上述测试全绿后，更新 Remediation Plan：Task 4.4、4.5、4.7 与 5.3 只勾选实际已完成的子项，并在对应条目附此计划路径。不要把尚未实现的质量门（Task 5.5）标为完成。

- [x] **Step 4: 复查变更边界**

Run: `cd backend && python -m pytest tests/unit/test_verifier_control.py tests/integration/test_awaiting_input_e2e.py -q`

Expected: PASS。确认实现未新增 LLM 控制的暂停路径、未恢复 confidence 阈值拒绝、未重新引入成功 outcome 的嵌套 verifier。

## 自检

- 规格中的每项要求均映射到 Task 1–6：受限第三态（1、3）、fail-open/拒绝质量门（2）、统一 iteration（5）、Pending/resume（4）、移除嵌套 verifier（5）、回归与计划状态（6）。
- 无 TBD/TODO/“类似前项”等占位表述。
- `needs_input`、`slot_patch_schema`、`choices`、`correction_history`、`resume_patch` 在所有任务中使用同一名称。
- 每项代码变更均先定义失败测试，再给出最小实现和精确验证命令。
