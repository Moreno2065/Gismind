// frontend/src/hooks/useSSE.ts
// 用 fetch + ReadableStream 接收 SSE（不用 EventSource，因需 POST）。
// 解析标准 SSE 文本帧：`event:` / `data:` 行，空行分隔。
// 心跳行（`: heartbeat`）忽略。

import { useCallback } from 'react';
import type { SSEEvent } from '@/types/message';

interface UseSSEOptions {
  onEvent: (ev: SSEEvent) => void;
  onError?: (err: Error) => void;
  onComplete?: () => void;
}

interface SSEFrame {
  event: string;
  data: string;
}

/** 解析一段 SSE 文本块为帧（按 \n\n 分隔的事件单元） */
function parseSSEChunk(chunk: string): SSEFrame[] {
  const frames: SSEFrame[] = [];
  const blocks = chunk.split(/\r?\n\r?\n/);
  for (const block of blocks) {
    if (!block.trim()) continue;
    let event = 'message'; // SSE 默认 event 名
    const dataLines: string[] = [];
    for (const line of block.split(/\r?\n/)) {
      if (line.startsWith(':')) continue; // 心跳注释行
      if (line.startsWith('event:')) {
        event = line.slice(6).trim();
      } else if (line.startsWith('data:')) {
        dataLines.push(line.slice(5).replace(/^\s/, ''));
      }
    }
    if (dataLines.length === 0) continue;
    frames.push({ event, data: dataLines.join('\n') });
  }
  return frames;
}

/** 把帧解析为 SSEEvent（按 event 名分发） */
function decodeFrame(frame: SSEFrame): SSEEvent | null {
  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(frame.data) as Record<string, unknown>;
  } catch {
    console.warn('[useSSE] JSON parse failed for event:', frame.event, 'data[:100]=', frame.data.slice(0, 100));
    return null;
  }

  // Build event from the parsed payload (event name from frame, data from JSON).
  // StreamEvent types carry their own "event" field; legacy types use frame.event.
  if (payload.event && typeof payload.event === 'string') {
    return { event: payload.event, data: payload } as unknown as SSEEvent;
  }

  // Legacy events — normalize to the shape expected by the union type
  const known = ['status', 'token', 'map', 'chart', 'error', 'done', 'react_trace'];
  if (!known.includes(frame.event)) {
    console.warn('[useSSE] unknown event ignored:', frame.event);
    return null;
  }
  return { event: frame.event, data: payload } as unknown as SSEEvent;
}

/** 找到 buffer 中第一个完整帧的结束位置（\n\n 或 \r\n\r\n 的起始） */
function findFrameEnd(buf: string): number {
  const lf = buf.indexOf('\n\n');
  const crlf = buf.indexOf('\r\n\r\n');
  if (lf === -1 && crlf === -1) return -1;
  if (lf === -1) return crlf;
  if (crlf === -1) return lf;
  return Math.min(lf, crlf);
}

export function useSSE() {
  const start = useCallback(async (response: Response, opts: UseSSEOptions) => {
    if (!response.body) throw new Error('SSE response has no body');
    const reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';
    let terminal = false;

    try {
      while (!terminal) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        let sepIdx: number;
        while ((sepIdx = findFrameEnd(buffer)) !== -1) {
          const raw = buffer.slice(0, sepIdx);
          buffer = buffer.slice(sepIdx).replace(/^\r?\n\r?\n/, '');
          const frames = parseSSEChunk(raw + '\n\n');
          for (const frame of frames) {
            const ev = decodeFrame(frame);
            if (ev) {
              opts.onEvent(ev);
              // POST /chat 以 done/error 作为唯一传输终态；run.* 只是可视化轨迹。
              if (
                ev.event === 'done' ||
                ev.event === 'error'
              ) {
                terminal = true;
              }
            }
          }
        }
      }
      if (buffer.trim()) {
        const frames = parseSSEChunk(buffer + '\n\n');
        for (const frame of frames) {
          const ev = decodeFrame(frame);
          if (ev) opts.onEvent(ev);
        }
      }
      // If stream ended without a terminal event, treat as error (F3 fix)
      if (!terminal) {
        opts.onError?.(new Error('SSE stream ended without terminal event (done/error)'));
        return;
      }
      opts.onComplete?.();
    } catch (err) {
      if ((err as Error).name === 'AbortError') return;
      opts.onError?.(err as Error);
    } finally {
      // 终端事件后主动 cancel，避免服务端继续向已放弃的连接推数据
      if (terminal) {
        try {
          await reader.cancel();
        } catch {
          /* noop */
        }
      }
      try {
        reader.releaseLock();
      } catch {
        /* noop */
      }
    }
  }, []);

  return { start };
}
