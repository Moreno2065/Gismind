// frontend/src/types/session.ts
// Session API projections from the durable Runtime store.

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
  id?: string;
  role: 'user' | 'assistant' | 'tool' | 'system';
  content: string;
  created_at?: string;
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
