// frontend/src/types/session.ts
// Session API 契约类型 — 与 backend/app/api/sessions.py 对齐

export interface SessionMeta {
  id: string;
  title: string;
  created_at: number;        // epoch ms
  updated_at: number;
  message_count: number;
  tool_count: number;
  has_map: boolean;
}

export interface SessionMessage {
  role: 'user' | 'assistant' | 'tool' | 'system';
  content: string;
  tool_call_id?: string;
  tool_calls?: unknown[];
  created_at?: string;       // iso
  // 前端兼容字段（来自 ChatMessage 持久化）
  blocks?: unknown[];
  status?: 'thinking' | 'fetching' | 'summarizing' | 'done' | 'error';
  thinkingTrace?: unknown[];
  execution_trace?: unknown[];
  executionTrace?: unknown[];
  trace_id?: string;
  error?: { code: string; message: string };
}

export interface CreateSessionResponse {
  id: string;
  title: string;
  created_at: number;
  updated_at: number;
}

export interface SessionListResponse {
  items: SessionMeta[];
}

export interface SessionMessagesResponse {
  messages: SessionMessage[];
}
