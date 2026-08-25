// frontend/src/api/client.ts
// API 调用封装 — chat(SSE) / upload / health

import type { ChatRequest, UploadResponse } from '@/types/message';
import type {
  CreateSessionResponse,
  SessionListResponse,
  SessionMessage,
  SessionMessagesResponse,
  SessionMeta,
} from '@/types/session';

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

/** 文件上传 */
export async function uploadFile(file: File, signal?: AbortSignal): Promise<UploadResponse> {
  const form = new FormData();
  form.append('file', file);
  const res = await fetch(`${BASE}/upload`, { method: 'POST', headers: authHeaders(), body: form, signal });
  const payload = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = (payload as { error?: { message?: string } }).error;
    throw new Error(err?.message ?? `upload failed: HTTP ${res.status}`);
  }
  return payload as UploadResponse;
}

/**
 * 发起对话 — SSE 流式接收。
 * 返回 Response；调用方用 useSSE 解析 body 流。
 */
export function chatStream(req: ChatRequest, signal?: AbortSignal): Promise<Response> {
  return fetch(`${BASE}/chat`, {
    method: 'POST',
    headers: authHeaders({
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
    }),
    body: JSON.stringify(req),
    signal,
  });
}

export interface ResumeChatResponse {
  status: 'resumed' | 'no_checkpoint' | 'mismatch' | 'not_found';
  session_id: string;
  sub_agent_run_id?: string;
  expected_sub_agent_run_id?: string;
  message?: string;
  final_output?: unknown;
}

/**
 * Session snapshot guard for in-flight resume.
 * Resume responses must only apply when the requesting session is still active
 * and the request was not aborted (e.g. user switched session mid-resume).
 */
export function canApplyResumeResult(
  requestSessionId: string,
  activeSessionId: string,
  signal?: AbortSignal,
  responseSessionId?: string,
): boolean {
  if (signal?.aborted) return false;
  if (!requestSessionId || requestSessionId !== activeSessionId) return false;
  if (responseSessionId && responseSessionId !== requestSessionId) return false;
  return true;
}

/**
 * Extract human-readable text from resume `final_output`.
 * Backend chat path uses `text` or `summary` (see chat.py token emission).
 */
export function extractResumeFinalText(finalOutput: unknown): string | null {
  if (finalOutput == null) return null;
  if (typeof finalOutput === 'string') {
    const t = finalOutput.trim();
    return t.length > 0 ? t : null;
  }
  if (typeof finalOutput !== 'object') return null;
  const rec = finalOutput as Record<string, unknown>;
  for (const key of ['text', 'summary'] as const) {
    const v = rec[key];
    if (typeof v === 'string' && v.trim().length > 0) return v;
  }
  return null;
}

/** Build assistant text blocks from resume final_output (empty when no displayable text). */
export function textBlocksFromResumeFinalOutput(
  finalOutput: unknown,
): Array<{ type: 'text'; content: string }> {
  const text = extractResumeFinalText(finalOutput);
  return text ? [{ type: 'text' as const, content: text }] : [];
}

/** 提交等待中的用户回答，并恢复对应 session。 */
export async function resumeChat(
  sessionId: string,
  subAgentRunId: string,
  answer: string,
  signal?: AbortSignal,
): Promise<ResumeChatResponse> {
  const res = await fetch(`${BASE}/chat/${encodeURIComponent(sessionId)}/resume`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ sub_agent_run_id: subAgentRunId, answer }),
    signal,
  });
  if (!res.ok) await readError(res, 'resume chat failed');
  return (await res.json()) as ResumeChatResponse;
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

/** 解析错误响应 envelope，未知错误给出兜底文案。 */
async function readError(res: Response, fallback: string): Promise<never> {
  const payload = await res.json().catch(() => ({}));
  const err = (payload as { error?: { message?: string } }).error;
  const msg = err?.message ?? `HTTP ${res.status}`;
  if (res.status === 404) {
    throw new Error(`SESSION_NOT_FOUND: ${msg}`);
  }
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
// Run Control API — 暂停 / 取消 / 恢复 / 查询
// ---------------------------------------------------------------------------

export interface RunStatusResponse {
  run_id: string;
  status: 'pending' | 'running' | 'paused' | 'cancelled' | 'completed' | 'failed';
  created_at: number;
  updated_at: number;
}

/** 取消正在执行的 run。 */
export async function cancelRun(runId: string, signal?: AbortSignal): Promise<{ status: string; run_id: string }> {
  const res = await fetch(`${BASE}/runs/${encodeURIComponent(runId)}/cancel`, {
    method: 'POST',
    headers: authHeaders(),
    signal,
  });
  return (await res.json()) as { status: string; run_id: string };
}

/** 暂停正在执行的 run。 */
export async function pauseRun(runId: string, signal?: AbortSignal): Promise<{ status: string; run_id: string }> {
  const res = await fetch(`${BASE}/runs/${encodeURIComponent(runId)}/pause`, {
    method: 'POST',
    headers: authHeaders(),
    signal,
  });
  return (await res.json()) as { status: string; run_id: string };
}

/** 恢复已暂停的 run。 */
export async function resumeRun(runId: string, signal?: AbortSignal): Promise<{ status: string; run_id: string }> {
  const res = await fetch(`${BASE}/runs/${encodeURIComponent(runId)}/resume`, {
    method: 'POST',
    headers: authHeaders(),
    signal,
  });
  return (await res.json()) as { status: string; run_id: string };
}

/** 查询 run 状态。 */
export async function getRunStatus(runId: string, signal?: AbortSignal): Promise<RunStatusResponse> {
  const res = await fetch(`${BASE}/runs/${encodeURIComponent(runId)}`, {
    headers: authHeaders(),
    signal,
  });
  return (await res.json()) as RunStatusResponse;
}
