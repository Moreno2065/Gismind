// frontend/src/api/client.ts
// API 调用封装 — Runtime / artifacts / sessions / health

import type {
  CreateSessionResponse,
  SessionListResponse,
  SessionMessage,
  SessionMessagesResponse,
  SessionMeta,
} from '@/types/session';
import type {
  ArtifactSummary,
  CreateRunRequest,
  CreateRunResponse,
  RunCommandResponse,
  RunSnapshot,
  PendingConfirmation,
  SessionRun,
} from '@/types/runtime';

const BASE = (import.meta.env.VITE_API_BASE_URL ?? '/api').replace(/\/$/, '');

// ---------------------------------------------------------------------------
// X-User-Id — 临时认证方案，与后端 _get_user_id() 对齐
// ---------------------------------------------------------------------------
const USER_ID_KEY = 'gismind.user_id';

/** 获取或生成持久化用户 ID（临时方案，生产应替换为 JWT / OAuth2）。 */
export function getUserId(): string {
  try {
    let id = localStorage.getItem(USER_ID_KEY);
    if (!id) {
      id = `u_${crypto.randomUUID().replace(/-/g, '').slice(0, 12)}`;
      localStorage.setItem(USER_ID_KEY, id);
    }
    return id;
  } catch {
    return 'anonymous';
  }
}

/** 构造包含 X-User-Id 的请求头。 */
function authHeaders(extra?: Record<string, string>): Record<string, string> {
  return { 'X-User-Id': getUserId(), ...extra };
}

export interface HealthResponse {
  status: 'ok' | 'degraded';
  version: string;
  checks: Record<string, string>;
}

/** 健康检查 */
export async function getHealth(signal?: AbortSignal): Promise<HealthResponse> {
  const res = await fetch(`${BASE}/health`, { signal });
  if (!res.ok) {
    // 503 时后端仍返回 JSON
    try {
      return (await res.json()) as HealthResponse;
    } catch {
      throw new Error(`health check failed: HTTP ${res.status}`);
    }
  }
  return (await res.json()) as HealthResponse;
}

export interface UploadDeclaration {
  declared_crs?: Record<string, unknown>;
  declared_bbox?: readonly [number, number, number, number];
}

/** Upload directly to the durable Runtime CAS with only explicit user declarations. */
export async function uploadRuntimeArtifact(file: File, signal?: AbortSignal, options?: UploadDeclaration): Promise<ArtifactSummary> {
  const form = new FormData();
  form.append('file', file);
  if (options?.declared_crs) form.append('declared_crs', JSON.stringify(options.declared_crs));
  if (options?.declared_bbox) form.append('declared_bbox', JSON.stringify(options.declared_bbox));
  const res = await fetch(`${BASE}/artifacts/uploads`, { method: 'POST', headers: authHeaders(), body: form, signal });
  if (!res.ok) await readError(res, 'runtime upload failed');
  return (await res.json()) as ArtifactSummary;
}

/** Create a durable Runtime run. Retries are safe when client_request_id is stable. */
export async function createRuntimeRun(req: CreateRunRequest, signal?: AbortSignal): Promise<CreateRunResponse> {
  const res = await fetch(`${BASE}/runs`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(req),
    signal,
  });
  if (!res.ok) await readError(res, 'create run failed');
  return (await res.json()) as CreateRunResponse;
}

/** Read the authoritative Runtime snapshot before (re)subscribing to events. */
export async function getRuntimeRun(runId: string, signal?: AbortSignal): Promise<RunSnapshot> {
  const res = await fetch(`${BASE}/runs/${encodeURIComponent(runId)}`, { headers: authHeaders(), signal });
  if (!res.ok) await readError(res, 'get run failed');
  return (await res.json()) as RunSnapshot;
}

/** Open a replayable event stream. The cursor is sent as Last-Event-ID. */
export async function runtimeEventsStream(runId: string, lastEventId = 0, signal?: AbortSignal): Promise<Response> {
  const res = await fetch(`${BASE}/runs/${encodeURIComponent(runId)}/events`, {
    headers: authHeaders({ Accept: 'text/event-stream', ...(lastEventId > 0 ? { 'Last-Event-ID': String(lastEventId) } : {}) }),
    signal,
  });
  if (!res.ok) await readError(res, 'runtime event stream failed');
  return res;
}

export async function cancelRuntimeRun(runId: string, commandId: string, expectedRunVersion: number, signal?: AbortSignal): Promise<RunCommandResponse> {
  const res = await fetch(`${BASE}/runs/${encodeURIComponent(runId)}/cancel`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ command_id: commandId, expected_run_version: expectedRunVersion }),
    signal,
  });
  if (!res.ok) await readError(res, 'cancel run failed');
  return (await res.json()) as RunCommandResponse;
}

export async function resumeRuntimeRun(runId: string, commandId: string, patch: Record<string, unknown>, expectedRunVersion: number, signal?: AbortSignal): Promise<RunCommandResponse> {
  const res = await fetch(`${BASE}/runs/${encodeURIComponent(runId)}/resume`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ command_id: commandId, patch, expected_run_version: expectedRunVersion, action: 'resume' }),
    signal,
  });
  if (!res.ok) await readError(res, 'resume run failed');
  return (await res.json()) as RunCommandResponse;
}

export async function confirmRuntimeRun(runId: string, commandId: string, confirmation: PendingConfirmation, expectedRunVersion: number, signal?: AbortSignal): Promise<RunCommandResponse> {
  const res = await fetch(`${BASE}/runs/${encodeURIComponent(runId)}/confirm`, {
    method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }), signal,
    body: JSON.stringify({ command_id: commandId, expected_run_version: expectedRunVersion, task_id: confirmation.task_id, action_hash: confirmation.action_hash, confirmation: true }),
  });
  if (!res.ok) await readError(res, 'confirm run failed');
  return (await res.json()) as RunCommandResponse;
}

export async function getSessionRuns(sessionId: string, signal?: AbortSignal): Promise<SessionRun[]> {
  const res = await fetch(`${BASE}/sessions/${encodeURIComponent(sessionId)}/runs`, { headers: authHeaders(), signal });
  if (!res.ok) await readError(res, 'get session runs failed');
  return ((await res.json()) as { items: SessionRun[] }).items;
}

export async function getRuntimeArtifact(artifactId: string, signal?: AbortSignal): Promise<ArtifactSummary> {
  const res = await fetch(`${BASE}/artifacts/${encodeURIComponent(artifactId)}`, { headers: authHeaders(), signal });
  if (!res.ok) await readError(res, 'get artifact failed');
  return (await res.json()) as ArtifactSummary;
}

/** Content URL is generated by the server; storage_uri never crosses this boundary. */
export function runtimeArtifactContentUrl(artifact: Pick<ArtifactSummary, 'artifact_id'>): string {
  return `${BASE}/artifacts/${encodeURIComponent(artifact.artifact_id)}/content`;
}

export async function getRuntimeArtifactContent(artifactId: string, signal?: AbortSignal): Promise<unknown> {
  const res = await fetch(runtimeArtifactContentUrl({ artifact_id: artifactId }), { headers: authHeaders(), signal });
  if (!res.ok) await readError(res, 'get artifact content failed');
  return res.json();
}

// ---------------------------------------------------------------------------
// Sessions API
// ---------------------------------------------------------------------------

const ACTIVE_KEY = 'gismind.active_session';

/** 读取当前活动 session id（不自动创建）。 */
export function getActiveSessionId(): string | null {
  try {
    return localStorage.getItem(ACTIVE_KEY);
  } catch {
    return null;
  }
}

/** 设置当前活动 session id。 */
export function setActiveSessionId(id: string): void {
  try {
    localStorage.setItem(ACTIVE_KEY, id);
  } catch {
    // localStorage 不可用时静默忽略（隐私模式等）
  }
}

/** 清除当前活动 session id。 */
export function clearActiveSessionId(): void {
  try {
    localStorage.removeItem(ACTIVE_KEY);
  } catch {
    // 忽略
  }
}

export class RuntimeApiError extends Error {
  constructor(public readonly code: string, public readonly status: number, message: string) {
    super(`${code}: ${message}`);
    this.name = 'RuntimeApiError';
  }
}

/** 解析错误响应 envelope，保留可恢复的协议错误代码。 */
async function readError(res: Response, fallback: string): Promise<never> {
  const payload = await res.json().catch(() => ({}));
  const err = (payload as { error?: { message?: string } }).error;
  const msg = err?.message ?? `HTTP ${res.status}`;
  const code = (err as { code?: string } | undefined)?.code;
  if (code) throw new RuntimeApiError(code, res.status, msg);
  throw new Error(`${fallback}: ${msg}`);
}

/** 列出全部 session。 */
export async function listSessions(signal?: AbortSignal): Promise<SessionMeta[]> {
  const res = await fetch(`${BASE}/sessions`, { headers: authHeaders(), signal });
  if (!res.ok) await readError(res, 'list sessions failed');
  const payload = (await res.json()) as SessionListResponse;
  return payload.items;
}

/** 新建一个空 session。 */
export async function createSession(signal?: AbortSignal): Promise<CreateSessionResponse> {
  const res = await fetch(`${BASE}/sessions`, { method: 'POST', headers: authHeaders(), signal });
  if (!res.ok) await readError(res, 'create session failed');
  return (await res.json()) as CreateSessionResponse;
}

/** 获取单个 session 元信息。 */
export async function getSessionMeta(id: string, signal?: AbortSignal): Promise<SessionMeta> {
  const res = await fetch(`${BASE}/sessions/${encodeURIComponent(id)}`, { headers: authHeaders(), signal });
  if (!res.ok) await readError(res, 'get session failed');
  return (await res.json()) as SessionMeta;
}

/** 读取 session 全部消息。 */
export async function getSessionMessages(id: string, signal?: AbortSignal): Promise<SessionMessage[]> {
  const res = await fetch(`${BASE}/sessions/${encodeURIComponent(id)}/messages`, { headers: authHeaders(), signal });
  if (!res.ok) await readError(res, 'get messages failed');
  const payload = (await res.json()) as SessionMessagesResponse;
  return payload.messages;
}

/** 重命名 session。无返回体，仅确认 status。 */
export async function renameSession(id: string, title: string, signal?: AbortSignal): Promise<void> {
  const res = await fetch(`${BASE}/sessions/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ title }),
    signal,
  });
  if (!res.ok) await readError(res, 'rename session failed');
}

/** 删除 session。无返回体，仅确认 status。 */
export async function deleteSession(id: string, signal?: AbortSignal): Promise<void> {
  const res = await fetch(`${BASE}/sessions/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    headers: authHeaders(),
    signal,
  });
  if (!res.ok) await readError(res, 'delete session failed');
}

// ---------------------------------------------------------------------------
// Legacy Run Control API has been removed. Runtime commands above are versioned
// and persisted by the authoritative run ledger.
// ---------------------------------------------------------------------------
