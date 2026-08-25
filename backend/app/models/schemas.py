"""Gismind 数据模型。所有 Pydantic 模型的统一定义，参考 docs/02_data_models.md。"""

from typing import List, Literal, Optional, Any
from pydantic import BaseModel, Field, field_validator


# === Agent 层模型 ===

class NeedClarification(BaseModel):
    """Planner 反问"""
    question: str = Field(description="反问用户的问题")


class PlannedToolCall(BaseModel):
    """One model-native function call normalized for deterministic execution."""

    id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class PlannerOutput(BaseModel):
    """Planner 的 LLM 输出包装结构"""
    thinking: str = Field(description="分析思路，1-2 句")
    tool_calls: List[PlannedToolCall] = Field(
        default_factory=list,
        description="schema-first 模式下一轮唯一的原生工具调用",
    )
    code: Optional[str] = Field(default=None, description="code-mode 下 LLM 输出的 Python 代码")
    summary: Optional[str] = Field(default=None, description="code-mode 下代码执行的预期输出摘要")
    need_clarification: Optional[NeedClarification] = Field(
        default=None,
        description="需要反问用户时的反问内容。LLM 偶尔会输出字符串而非对象，此处自动转换。",
    )

    @field_validator("need_clarification", mode="before")
    @classmethod
    def _coerce_clarification(cls, v):
        """将字符串自动转为 NeedClarification 对象。"""
        if v is None:
            return None
        if isinstance(v, str):
            return NeedClarification(question=v)
        return v


class ToolResult(BaseModel):
    """工具执行结果"""
    tool_call_id: str = Field(description="对应的工具调用 ID")
    tool_name: str = ""
    status: Literal["success", "empty", "error"]
    data: Optional[Any] = Field(default=None, description="成功时的结果数据")
    message: Optional[str] = Field(default=None, description="empty/error 时的说明")
    error_code: Optional[str] = None
    duration_ms: int = 0
    source: Optional[str] = Field(
        default=None,
        description="数据来源：Amap / OSM_CN / OSM_Global / Upload / inline / async / sandbox",
    )
    truncated: bool = False
    mode: Optional[str] = Field(
        default=None,
        description='"json" 或 "code"：JSON 工具调用 vs code-mode Python 执行',
    )


class JudgeDecision(BaseModel):
    """Judge 决策"""
    decision: Literal["CONTINUE", "RETRY", "FINISH", "AWAITING_INPUT"]
    reason: str


# === 工具层模型 ===

class POI(BaseModel):
    """POI 对象"""
    name: str
    address: Optional[str] = None
    tel: Optional[str] = None
    location: tuple[float, float]
    crs: Literal["GCJ02", "WGS84", "BD09", "CGCS2000"]
    source: Literal["Amap", "OSM_CN", "OSM_Global", "Upload"]
    category: Optional[str] = None
    poi_id: Optional[str] = None
    distance: Optional[float] = None


# === 前端契约层模型 ===

class MapLayerBase(BaseModel):
    type: str
    style: Optional[dict] = None


class PointLayer(MapLayerBase):
    type: Literal["point"] = "point"
    coordinates: List[List[float]]
    source: Literal["Amap", "OSM_CN", "OSM_Global", "Upload"]
    popup_fields: List[str] = []


class HeatmapLayer(MapLayerBase):
    type: Literal["heatmap"] = "heatmap"
    coordinates: List[List[float]]
    weights: Optional[List[float]] = None
    radius: int = 25
    gradient: Optional[dict] = None


class PolygonLayer(MapLayerBase):
    type: Literal["polygon"] = "polygon"
    coordinates: List[List[List[List[float]]]]
    fill_color: str = "#3388ff"
    fill_opacity: float = 0.3


class PolylineLayer(MapLayerBase):
    type: Literal["polyline"] = "polyline"
    coordinates: List[List[List[float]]]
    stroke_color: str = "#FF6B35"
    stroke_width: int = 4


class FeatureCollectionLayer(MapLayerBase):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: List[dict]


# === API 请求/响应模型 ===

class ChatRequest(BaseModel):
    """POST /api/chat 请求体"""
    session_id: str
    message: str
    upload_file_ids: List[str] = []

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, v: str) -> str:
        if not v or len(v) > 128:
            raise ValueError("session_id 长度须在 1-128 之间")
        if not v.replace("-", "").replace("_", "").isalnum():
            raise ValueError("session_id 只允许字母数字、连字符、下划线")
        return v

    @field_validator("message")
    @classmethod
    def validate_message(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("message 不能为空")
        if len(v) > 10000:
            raise ValueError("message 不能超过 10000 字符")
        return v.strip()


class UploadResponse(BaseModel):
    """POST /api/upload 响应"""
    file_id: str
    filename: str
    crs: str
    original_crs: str = ""
    feature_count: int
    geometry_type: str = ""
    preview: dict = {}
    warnings: List[str] = []


class TaskStatus(BaseModel):
    """GET /api/task/{task_id} 响应"""
    task_id: str
    status: Literal["pending", "running", "done", "failed"]
    progress: float = 0.0
    stage: str = ""
    result: Optional[dict] = None
    error: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""


class ErrorEnvelope(BaseModel):
    """统一错误响应"""
    code: str
    message: str
    detail: Optional[dict] = None
    trace_id: str = ""


class HealthResponse(BaseModel):
    """GET /api/health 响应"""
    status: Literal["ok", "degraded"]
    version: str = "1.6.0"
    checks: dict


# === Session 管理模型（Sprint 1 增量） ===

class SessionMeta(BaseModel):
    """会话列表用元信息（不带消息主体）。"""
    id: str
    title: str = Field(default="新会话", max_length=200)
    created_at: int = Field(description="epoch ms")
    updated_at: int = Field(description="epoch ms")
    message_count: int = 0
    tool_count: int = 0
    has_map: bool = False


class SessionListResponse(BaseModel):
    items: List[SessionMeta] = Field(default_factory=list)


class RenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class CreateSessionResponse(BaseModel):
    """POST /api/sessions 响应"""
    id: str
    title: str
    created_at: int
    updated_at: int


class SessionMessagesResponse(BaseModel):
    """GET /api/sessions/{id}/messages 响应"""
    messages: List[dict] = Field(default_factory=list)


# === Run Control 模型 ===

class RunStatus(BaseModel):
    """Run 执行状态"""
    run_id: str
    status: Literal["pending", "running", "paused", "cancelled", "completed", "failed"]
    created_at: float = 0.0
    updated_at: float = 0.0
