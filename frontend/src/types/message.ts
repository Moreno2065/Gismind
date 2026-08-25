// frontend/src/types/message.ts
// 前端契约层类型 — 与 docs/02_data_models.md §4 对齐

// ---------------------------------------------------------------------------
// MapLayer union (后端 MapLayerBuilder 生成，前端统一渲染)
// ---------------------------------------------------------------------------

/** 数据来源标记 — 决定视觉表达（Amap 橙色 / OSM 灰色 / Upload 中性） */
export type POISource = 'Amap' | 'OSM_CN' | 'OSM_Global' | 'Upload';

export interface PointLayer {
  type: 'point';
  coordinates: [number, number][]; // [[lng, lat], ...] GCJ02
  source: POISource;
  popup_fields?: string[];
  style?: Record<string, unknown>;
}

export interface HeatmapLayer {
  type: 'heatmap';
  coordinates: [number, number][];
  weights?: number[];
  radius?: number;
  gradient?: Record<string, string>;
  style?: Record<string, unknown>;
}

export interface PolygonLayer {
  type: 'polygon';
  coordinates: number[][][][]; // 多面，每面含环（外环 + 孔洞）
  fill_color?: string;
  fill_opacity?: number;
  style?: Record<string, unknown>;
}

export interface PolylineLayer {
  type: 'polyline';
  coordinates: number[][][]; // 多线
  stroke_color?: string;
  stroke_width?: number;
  style?: Record<string, unknown>;
}

/** 推荐使用：标准 GeoJSON，支持孔洞与多维坐标 */
export interface FeatureCollectionLayer {
  type: 'FeatureCollection';
  features: GeoJSONFeature[];
  style?: Record<string, unknown>;
}

/** 栅格瓦片层 — base64 PNG + bbox，使用 AMap ImageOverlay 渲染 */
export interface RasterLayer {
  type: 'raster';
  png_b64: string;        // base64 encoded PNG
  bbox: [number, number, number, number];  // [minLng, minLat, maxLng, maxLat]
  width: number;
  height: number;
  value_range?: [number, number];
  colormap?: string;
  value_kind?: string;
  opacity?: number;
}

export type MapLayer =
  | PointLayer
  | HeatmapLayer
  | PolygonLayer
  | PolylineLayer
  | FeatureCollectionLayer
  | RasterLayer;

// ---------------------------------------------------------------------------
// GeoJSON subset (RFC 7946)
// ---------------------------------------------------------------------------

export type GeoJSONGeometryType =
  | 'Point'
  | 'MultiPoint'
  | 'LineString'
  | 'MultiLineString'
  | 'Polygon'
  | 'MultiPolygon'
  | 'GeometryCollection';

export interface GeoJSONGeometry {
  type: GeoJSONGeometryType;
  coordinates: number[] | number[][] | number[][][] | number[][][][];
}

export interface GeoJSONFeature {
  type: 'Feature';
  geometry: GeoJSONGeometry;
  properties?: Record<string, unknown> & { _source?: POISource };
  id?: string | number;
}

// ---------------------------------------------------------------------------
// MessageBlock — 消息内嵌块
// ---------------------------------------------------------------------------

export interface TextBlock {
  type: 'text';
  content: string; // Markdown
}

export interface MapBlock {
  type: 'map';
  layers: MapLayer[];
  bbox: [number, number, number, number]; // [minLng, minLat, maxLng, maxLat] GCJ02
  expired?: boolean; // 本地持久化后标记数据已过期
  featureCount?: number; // 过期前保留的要素数量
}

export interface ChartBlock {
  type: 'chart';
  config: Record<string, unknown>; // ECharts option
}

export type MessageBlock = TextBlock | MapBlock | ChartBlock;

// ---------------------------------------------------------------------------
// ChatMessage
// ---------------------------------------------------------------------------

export type ChatRole = 'user' | 'assistant';
export type MessageStatus = 'thinking' | 'fetching' | 'summarizing' | 'reviewing' | 'reflecting' | 'done' | 'error';

export interface ChatMessage {
  id: string;
  role: ChatRole;
  blocks: MessageBlock[];
  status: MessageStatus;
  statusMessage?: string; // 来自 status 事件的展示文案
  thinkingTrace?: ThinkingStep[]; // ReAct 推理链（可选，折叠展示）
  executionTrace?: StreamEvent[]; // 结构化 DAG / 工具执行账本，可持久化回放
  trace_id?: string;
  error?: { code: string; message: string };
  created_at: number; // epoch ms
}

/** ReAct 推理链条目 — 来自 tool_call 事件或 Markdown > 引用 */
export interface ThinkingStep {
  kind: 'tool_call' | 'note' | 'react_trace';
  tool?: string;
  args?: Record<string, unknown>;
  note?: string;
  ts: number;
  // react_trace 专用字段
  round?: number;
  thinking?: string;
  tool_calls?: { tool_name: string; params: Record<string, unknown> }[];
  observer_summary?: string;
  tool_results?: { tool_name: string; status: string }[];
}

// ---------------------------------------------------------------------------
// Dispatcher SSE event payloads (Phase 2 / Task 2.4)
// ---------------------------------------------------------------------------

export type SubTaskData = { task_id: string; agent_role: string; status: string; error?: string };
export type VerifyData = { task_id: string; approved: boolean; reason: string; confidence: number };
export type ReflectData = { task_id: string; reason: string; iteration: number };

// ---------------------------------------------------------------------------
// SSEEvent — 后端推送的事件 payload
// ---------------------------------------------------------------------------

export interface ReactTraceStep {
  round: number;
  thinking: string;
  tool_calls: { tool_name: string; params: Record<string, unknown> }[];
  observer_summary?: string;
  tool_results?: { tool_name: string; status: string }[];
  // code-mode 字段（可选，仅 code-mode 步骤有值）
  code?: string;
  stdout?: string;
  result?: unknown;
  executor_type?: "inline" | "async" | "sandbox";
  error?: string;
}

export type SSEEvent =
  // Modern event stream (EVENT_CONTRACTS-based, from POST /api/chat real-time bridge)
  | StreamEvent
  // Legacy event types (from POST /api/chat pre-streaming or deprecated endpoints)
  | { event: 'status'; data: { status: MessageStatus | string; message: string; run_id?: string } }
  | { event: 'token'; data: { content: string } }
  | { event: 'map'; data: { layers: MapLayer[]; bbox: [number, number, number, number] } }
  | { event: 'chart'; data: { config: Record<string, unknown> } }
  | { event: 'error'; data: { code: string; message: string; trace_id: string; run_id?: string } }
  | { event: 'done'; data: { trace_id: string; run_id?: string } }
  // Deprecated: react_trace is now replaced by real-time StreamEvent flow.
  // Kept for backward compatibility with sessions recorded before the upgrade.
  | { event: 'react_trace'; data: ReactTraceStep };

// ---------------------------------------------------------------------------
// API 请求体
// ---------------------------------------------------------------------------

export interface ChatRequest {
  session_id: string;
  message: string;
  upload_file_ids?: string[];
}

export interface UploadResponse {
  file_id: string;
  filename: string;
  crs: string;
  original_crs?: string;
  feature_count: number;
  geometry_type: string;
  preview?: {
    bbox: [number, number, number, number];
    sample_features: GeoJSONFeature[];
  };
  warnings?: string[];
}

// ---------------------------------------------------------------------------
// StreamEvent — SSE event stream from GET /api/chat/{session_id}/events
// ---------------------------------------------------------------------------

/** display_kind 枚举，控制前端 UI 渲染方式 */
export type StreamDisplayKind =
  | 'progress'
  | 'workflow_step'
  | 'warning'
  | 'confirmation'
  | 'debug'
  | 'result';

/** 事件类型枚举，对应 EVENT_CONTRACTS */
export type StreamEventType =
  // Run-level
  | 'run.session'
  | 'run.thought'
  | 'run.summary'
  | 'run.completed'
  | 'run.failed'
  | 'run.paused'
  | 'run.plan'
  // Sub-task lifecycle
  | 'run.task.start'
  | 'run.task.complete'
  // Step-level
  | 'code.generation'
  | 'code.execution.start'
  | 'code.execution.stdout'
  | 'code.execution.stderr'
  | 'code.execution.complete'
  | 'code.execution.error'
  // Tool-call level
  | 'tool.call.start'
  | 'tool.preflight.warning'
  | 'tool.preflight.blocked'
  | 'tool.call.complete'
  | 'tool.postflight.warning'
  | 'tool.postflight.empty_result'
  // Risk events
  | 'tool.risk.detected'
  | 'tool.risk.auto_repair'
  | 'tool.risk.blocked'
  // Judge / pending
  | 'judge.decision'
  | 'judge.awaiting_input';

/** Generic stream event payload */
export interface StreamEventBase {
  event: StreamEventType;
  event_type: StreamEventType;
  display_kind: StreamDisplayKind;
  message: string;
  timestamp: string; // ISO8601
  session_id?: string;
  step_index?: number;
  step_total?: number;
  attempt_no?: number;
  trace_id?: string;
}

/** Run-level events */
export interface RunSessionEvent extends StreamEventBase {
  event: 'run.session';
  event_type: 'run.session';
}

export interface RunCompletedEvent extends StreamEventBase {
  event: 'run.completed';
  event_type: 'run.completed';
}

export interface RunFailedEvent extends StreamEventBase {
  event: 'run.failed';
  event_type: 'run.failed';
  error?: string;
  error_code?: string;
}

export interface WorkflowPlanTask {
  id: string;
  agent_role: string;
  tool_name?: string;
  goal: string;
  depends_on: string[];
  instruction_id?: string;
  status: string;
}

export interface RunPlanEvent extends StreamEventBase {
  event: 'run.plan';
  event_type: 'run.plan';
  /** How the dispatcher obtained this DAG; never infer LLM coverage from a fallback. */
  planner_source?: 'guardrail' | 'root_llm' | 'fallback';
  instructions?: Array<{ id: string; text: string }>;
  tasks?: WorkflowPlanTask[];
}

/** Sub-task lifecycle events */
export interface RunTaskStartEvent extends StreamEventBase {
  event: 'run.task.start';
  event_type: 'run.task.start';
  task_id?: string;
  agent_role?: string;
  goal?: string;
  task_index?: number;
  task_total?: number;
}

export interface RunTaskCompleteEvent extends StreamEventBase {
  event: 'run.task.complete';
  event_type: 'run.task.complete';
  task_id?: string;
  status?: string;
  error_code?: string;
  duration_ms?: number;
}

/** Code generation / execution events */
export interface CodeGenerationEvent extends StreamEventBase {
  event: 'code.generation';
  event_type: 'code.generation';
  code?: string;
  role?: string;
}

export interface CodeExecutionStartEvent extends StreamEventBase {
  event: 'code.execution.start';
  event_type: 'code.execution.start';
  executor_type?: 'inline' | 'async' | 'sandbox';
}

export interface CodeExecutionStdoutEvent extends StreamEventBase {
  event: 'code.execution.stdout';
  event_type: 'code.execution.stdout';
}

export interface CodeExecutionStderrEvent extends StreamEventBase {
  event: 'code.execution.stderr';
  event_type: 'code.execution.stderr';
}

export interface CodeExecutionCompleteEvent extends StreamEventBase {
  event: 'code.execution.complete';
  event_type: 'code.execution.complete';
  result?: unknown;
  execution_time_ms?: number;
}

export interface CodeExecutionErrorEvent extends StreamEventBase {
  event: 'code.execution.error';
  event_type: 'code.execution.error';
  error_code?: string;
  traceback?: string;
}

/** Tool-call events */
export interface ToolCallStartEvent extends StreamEventBase {
  event: 'tool.call.start';
  event_type: 'tool.call.start';
  tool_name?: string;
  params?: Record<string, unknown>;
}

export interface ToolPreflightWarningEvent extends StreamEventBase {
  event: 'tool.preflight.warning';
  event_type: 'tool.preflight.warning';
  tool_name?: string;
  code?: string;
  stage?: string;
  issues?: Array<{ code: string; severity: string; message: string }>;
}

export interface ToolPreflightBlockedEvent extends StreamEventBase {
  event: 'tool.preflight.blocked';
  event_type: 'tool.preflight.blocked';
  tool_name?: string;
  code?: string;
  stage?: string;
  issues?: Array<{ code: string; severity: string; message: string }>;
}

export interface ToolCallCompleteEvent extends StreamEventBase {
  event: 'tool.call.complete';
  event_type: 'tool.call.complete';
  tool_name?: string;
  result?: unknown;
  status?: string;
  duration_ms?: number;
}

export interface ToolPostflightWarningEvent extends StreamEventBase {
  event: 'tool.postflight.warning';
  event_type: 'tool.postflight.warning';
  tool_name?: string;
  code?: string;
  warning_message?: string;
}

export interface ToolPostflightEmptyResultEvent extends StreamEventBase {
  event: 'tool.postflight.empty_result';
  event_type: 'tool.postflight.empty_result';
  tool_name?: string;
}

/** Judge events */
export interface JudgeDecisionEvent extends StreamEventBase {
  event: 'judge.decision';
  event_type: 'judge.decision';
  decision?: string;
  confidence?: number;
}

export interface JudgeAwaitingInputEvent extends StreamEventBase {
  event: 'judge.awaiting_input';
  event_type: 'judge.awaiting_input';
  pending_task?: {
    sub_agent_run_id: string;
    original_request: string;
    missing_slots: string[];
    candidates: Array<Record<string, unknown>>;
    message: string;
    issues: Array<Record<string, unknown>>;
    created_at: string;
  };
}

/** Risk events */
export interface ToolRiskDetectedEvent extends StreamEventBase {
  event: 'tool.risk.detected';
  event_type: 'tool.risk.detected';
  risk_code?: string;
  category?: string;
  severity?: string;
  tool_name?: string;
}

export interface ToolRiskAutoRepairEvent extends StreamEventBase {
  event: 'tool.risk.auto_repair';
  event_type: 'tool.risk.auto_repair';
  risk_code?: string;
  repair_action?: Record<string, unknown>;
}

export interface ToolRiskBlockedEvent extends StreamEventBase {
  event: 'tool.risk.blocked';
  event_type: 'tool.risk.blocked';
  risk_code?: string;
  category?: string;
  tool_name?: string;
}

/** Union of all stream event types */
export type StreamEvent =
  | RunSessionEvent
  | RunCompletedEvent
  | RunFailedEvent
  | RunPlanEvent
  | RunTaskStartEvent
  | RunTaskCompleteEvent
  | CodeGenerationEvent
  | CodeExecutionStartEvent
  | CodeExecutionStdoutEvent
  | CodeExecutionStderrEvent
  | CodeExecutionCompleteEvent
  | CodeExecutionErrorEvent
  | ToolCallStartEvent
  | ToolPreflightWarningEvent
  | ToolPreflightBlockedEvent
  | ToolCallCompleteEvent
  | ToolPostflightWarningEvent
  | ToolPostflightEmptyResultEvent
  | ToolRiskDetectedEvent
  | ToolRiskAutoRepairEvent
  | ToolRiskBlockedEvent
  | JudgeDecisionEvent
  | JudgeAwaitingInputEvent;
