// frontend/src/components/ChatPanel.tsx
// 聊天面板 — 消息列表 + 底部输入框。
// 发送消息调 /api/chat，接收 SSE 流拼装消息。
// 自动触底 + 用户上滚时不强行触底。
// 消息按 sessionId 分桶持久化到 localStorage[gismind.messages.{sessionId}]。

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  chatStream,
  resumeChat,
  uploadFile,
  cancelRun,
  pauseRun,
  resumeRun,
  getSessionMessages,
  canApplyResumeResult,
  textBlocksFromResumeFinalOutput,
} from '@/api/client';
import { useSSE } from '@/hooks/useSSE';
import { useScrollAnchor } from '@/hooks/useScrollAnchor';
import { countFeatures } from '@/lib/utils';
import type { ChatMessage, MessageBlock, SSEEvent, ThinkingStep, StreamEvent } from '@/types/message';
import { MessageBubble } from './MessageBubble';

interface PendingUpload {
  file: File;
  file_id?: string;
  status: 'uploading' | 'ready' | 'error';
  error?: string;
}

interface AwaitingInput {
  subAgentRunId: string;
  message: string;
  missingSlots?: string[];
  error?: string;
}

interface ChatPanelProps {
  sessionId: string;
  /** SSE 流结束（done/error）后调用，用于触发会话列表刷新以更新 title/tool_count 等元信息 */
  onSessionUpdate?: () => void;
}

export function ChatPanel({ sessionId, onSessionUpdate }: ChatPanelProps) {
  const [messages, setMessages] = useState<ChatMessage[]>(() => loadMessages(sessionId));
  const [input, setInput] = useState('');
  const [isStreaming, setIsStreaming] = useState(false);
  const [isPaused, setIsPaused] = useState(false);
  const [awaitingInput, setAwaitingInput] = useState<AwaitingInput | null>(null);
  const [pendingUploads, setPendingUploads] = useState<PendingUpload[]>([]);
  const [dragOver, setDragOver] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const runIdRef = useRef<string | null>(null);
  const streamSessionIdRef = useRef<string | null>(null);
  const { start: startSSE } = useSSE();
  const scroll = useScrollAnchor<HTMLDivElement>();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const sessionIdRef = useRef(sessionId);
  sessionIdRef.current = sessionId;

  // 用 ref 读取最新值，避免频繁变化的状态破坏回调稳定
  const inputRef = useRef(input);
  inputRef.current = input;
  const isStreamingRef = useRef(isStreaming);
  isStreamingRef.current = isStreaming;
  const awaitingInputRef = useRef(awaitingInput);
  awaitingInputRef.current = awaitingInput;
  const pendingUploadsRef = useRef(pendingUploads);
  pendingUploadsRef.current = pendingUploads;

  const persistTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // sessionId 切换时：取消在飞流 + 从 localStorage 加载新 session 的消息 + 可选回填后端
  useEffect(() => {
    abortRef.current?.abort();
    runIdRef.current = null;
    isStreamingRef.current = false;
    setIsStreaming(false);
    setIsPaused(false);
    setAwaitingInput(null);
    setPendingUploads([]);
    setInput('');
    const localMessages = loadMessages(sessionId);
    setMessages(localMessages);
    if (localMessages.length === 0) {
      const ac = new AbortController();
      void getSessionMessages(sessionId, ac.signal)
        .then((remote) => {
          if (sessionIdRef.current !== sessionId || remote.length === 0) return;
          const restored = remote.flatMap((record, index) => {
            if (record.role !== 'user' && record.role !== 'assistant') return [];
            const trace = (record.executionTrace || record.execution_trace || []) as StreamEvent[];
            return [{
              id: `history_${sessionId}_${index}`,
              role: record.role,
              blocks: [{ type: 'text' as const, content: record.content || '' }],
              status: 'done' as const,
              executionTrace: trace,
              created_at: record.created_at ? Date.parse(record.created_at) : Date.now(),
            }];
          });
          setMessages((current) => current.length === 0 ? restored : current);
        })
        .catch(() => {
          // localStorage remains the offline source of truth when history is unavailable
        });
      return () => ac.abort();
    }
  }, [sessionId]);

  // 持久化消息（限最近 50 条，避免 localStorage 撑爆）— 500ms trailing debounce
  useEffect(() => {
    if (persistTimerRef.current) clearTimeout(persistTimerRef.current);
    persistTimerRef.current = setTimeout(() => {
      const trimmed = messages.slice(-50).map((m) => ({
        ...m,
        // 地图块体积大 — 持久化时保留元数据但清空图层数据，刷新后提示重新查询
        blocks: m.blocks.map((b) => {
          if (b.type === 'map') {
            return {
              ...b,
              layers: [],
              expired: true,
              featureCount: countFeatures(b.layers),
            } as MessageBlock;
          }
          return b;
        }) as MessageBlock[],
      }));
      try {
        localStorage.setItem(messagesKey(sessionIdRef.current), JSON.stringify(trimmed));
      } catch {
        /* quota — 静默 */
      }
    }, 500);
    return () => {
      if (persistTimerRef.current) clearTimeout(persistTimerRef.current);
    };
  }, [messages, sessionId]);

  // 流式输出时跟随底部
  useEffect(() => {
    scroll.maybeStick();
  }, [messages, scroll]);

  // 自适应输入框高度 — rAF 批处理避免强制同步布局（forced reflow）
  useEffect(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    requestAnimationFrame(() => {
      ta.style.height = 'auto';
      ta.style.height = `${Math.min(ta.scrollHeight, 200)}px`;
    });
  }, [input]);

  // 构造或更新最后一条 assistant 消息
  const updateLastAssistant = useCallback(
    (updater: (m: ChatMessage) => ChatMessage) => {
      setMessages((prev) => {
        if (prev.length === 0 || prev[prev.length - 1].role !== 'assistant') return prev;
        const last = prev[prev.length - 1];
        return [...prev.slice(0, -1), updater(last)];
      });
    },
    [],
  );

  // 追加 token 到最后一个 text block
  const appendToken = useCallback(
    (content: string) => {
      updateLastAssistant((m) => {
        const blocks = [...m.blocks];
        const last = blocks[blocks.length - 1];
        if (last && last.type === 'text') {
          blocks[blocks.length - 1] = { type: 'text', content: last.content + content };
        } else {
          blocks.push({ type: 'text', content });
        }
        return { ...m, blocks };
      });
    },
    [updateLastAssistant],
  );

  // 推入一个 map/chart block
  const pushBlock = useCallback(
    (block: MessageBlock) => {
      updateLastAssistant((m) => ({ ...m, blocks: [...m.blocks, block] }));
    },
    [updateLastAssistant],
  );

  const appendStreamEvent = useCallback(
    (event: StreamEvent) => {
      updateLastAssistant((m) => ({
        ...m,
        executionTrace: [...(m.executionTrace ?? []), event],
      }));
    },
    [updateLastAssistant],
  );

  // SSE 事件处理
  const handleSSEEvent = useCallback(
    (ev: SSEEvent) => {
      // 如果会话已切换，忽略旧会话的 SSE 事件
      if (streamSessionIdRef.current !== sessionIdRef.current) return;

      switch (ev.event) {
        case 'status':
          // Capture run_id from the first status event
          if (ev.data.run_id) {
            runIdRef.current = ev.data.run_id as string;
          }
          updateLastAssistant((m) => ({
            ...m,
            status: (ev.data.status as ChatMessage['status']) ?? m.status,
            statusMessage: ev.data.message,
          }));
          break;
        case 'token':
          appendToken(ev.data.content);
          break;
        case 'map':
          pushBlock({ type: 'map', layers: ev.data.layers, bbox: ev.data.bbox });
          break;
        case 'chart':
          pushBlock({ type: 'chart', config: ev.data.config });
          break;
        case 'react_trace': {
          const step: ThinkingStep = {
            kind: 'react_trace',
            round: ev.data.round,
            thinking: ev.data.thinking,
            tool_calls: ev.data.tool_calls,
            observer_summary: ev.data.observer_summary,
            tool_results: ev.data.tool_results,
            ts: Date.now(),
          };
          updateLastAssistant((m) => ({
            ...m,
            thinkingTrace: [...(m.thinkingTrace ?? []), step],
            status: 'thinking' as ChatMessage['status'],
            statusMessage: `第 ${ev.data.round} 轮：${ev.data.thinking?.slice(0, 40) || '分析中'}...`,
          }));
          break;
        }
        case 'error':
          updateLastAssistant((m) => ({
            ...m,
            status: 'error',
            error: { code: ev.data.code, message: ev.data.message },
            trace_id: ev.data.trace_id,
          }));
          runIdRef.current = null;
          setIsPaused(false);
          isStreamingRef.current = false;
          setIsStreaming(false);
          onSessionUpdate?.();
          break;
        case 'done':
          updateLastAssistant((m) => ({
            ...m,
            status: 'done',
            trace_id: ev.data.trace_id,
          }));
          runIdRef.current = null;
          setIsPaused(false);
          isStreamingRef.current = false;
          setIsStreaming(false);
          onSessionUpdate?.();
          break;
        // --- StreamEvent real-time trace events ---
        case 'run.completed':
          appendStreamEvent(ev as unknown as StreamEvent);
          break;
        case 'run.failed': {
          appendStreamEvent(ev as unknown as StreamEvent);
          const raw = ev as unknown as Record<string, unknown>;
          updateLastAssistant((m) => ({
            ...m,
            status: 'error',
            error: {
              // The following terminal `error` event normally repeats this
              // code. Preserve it here as well so a stream interruption after
              // `run.failed` cannot erase the actionable backend category.
              code: String(raw.error_code || 'RUN_FAILED'),
              message: String(raw.message || '任务执行失败'),
            },
          }));
          break;
        }
        case 'judge.awaiting_input': {
          // SSE payload is flat (sse_format(event, item)); tolerate nested .data too.
          const raw = ev as unknown as Record<string, unknown>;
          const nested = (raw.data && typeof raw.data === 'object')
            ? (raw.data as Record<string, unknown>)
            : undefined;
          const pendingTask = (
            (raw.pending_task as Record<string, unknown> | undefined)
            || (nested?.pending_task as Record<string, unknown> | undefined)
          );
          const message =
            (raw.message as string)
            || (nested?.message as string)
            || (pendingTask?.message as string)
            || '需要更多信息';
          const subAgentRunId =
            (pendingTask?.sub_agent_run_id as string)
            || runIdRef.current
            || '';
          const missingSlots = Array.isArray(pendingTask?.missing_slots)
            ? (pendingTask.missing_slots as string[])
            : undefined;
          updateLastAssistant((m) => ({
            ...m,
            status: 'fetching' as ChatMessage['status'],
            statusMessage: message,
          }));
          // 独立于流状态保存，避免随后 done / onComplete 清除等待提示
          setAwaitingInput({ subAgentRunId, message, missingSlots });
          appendStreamEvent(ev as unknown as StreamEvent);
          setIsPaused(false);
          isStreamingRef.current = false;
          setIsStreaming(false);
          break;
        }
        default:
          // Collect all other StreamEvent types for TraceTimeline
          if (ev.event && ev.event.includes('.')) {
            appendStreamEvent(ev as unknown as StreamEvent);
            // Update status message from workflow_step / progress events
            const d = (ev as unknown as { data?: { message?: string } }).data;
            if (d?.message) {
              updateLastAssistant((m) => ({
                ...m,
                statusMessage: d.message,
              }));
            }
          }
          break;
      }
    },
    [appendToken, pushBlock, appendStreamEvent, updateLastAssistant, onSessionUpdate],
  );

  const handleSend = useCallback(async () => {
    const text = inputRef.current.trim();
    if (!text || isStreamingRef.current) return;
    // Claim the in-flight lock synchronously so a second Enter/click cannot
    // pass the gate before React re-renders isStreaming=true.
    isStreamingRef.current = true;

    const pendingAnswer = awaitingInputRef.current;
    if (pendingAnswer) {
      // Snapshot the session that owns this resume so a mid-flight switch cannot
      // apply final_output / UI updates onto the newly active session.
      const resumeSessionId = sessionIdRef.current;
      abortRef.current?.abort();
      const ac = new AbortController();
      abortRef.current = ac;

      setIsStreaming(true);
      setAwaitingInput((prev) => (prev ? { ...prev, error: undefined } : prev));
      try {
        const result = await resumeChat(
          resumeSessionId,
          pendingAnswer.subAgentRunId,
          text,
          ac.signal,
        );
        // Drop stale responses after session switch or explicit abort.
        if (!canApplyResumeResult(resumeSessionId, sessionIdRef.current, ac.signal, result.session_id)) {
          return;
        }
        switch (result.status) {
          case 'resumed': {
            const textBlocks = textBlocksFromResumeFinalOutput(result.final_output);
            // When final_output has displayable text, put it in message blocks
            // (done status hides statusMessage). Placeholder only if no text.
            // Single state update: user answer + assistant final (or mark prior assistant done).
            // Avoid updateLastAssistant after appending user — last role would be user and no-op.
            setMessages((prev) => {
              const next = [...prev];
              // Close the prior awaiting assistant (if still last) so it is not left in fetching.
              // StatusStrip is hidden when status=done, so promote statusMessage into a text block.
              if (next.length > 0 && next[next.length - 1].role === 'assistant') {
                const last = next[next.length - 1];
                const keepBlocks =
                  last.blocks.length > 0
                    ? last.blocks
                    : last.statusMessage
                      ? [{ type: 'text' as const, content: last.statusMessage }]
                      : last.blocks;
                next[next.length - 1] = {
                  ...last,
                  blocks: keepBlocks,
                  status: 'done',
                  statusMessage: undefined,
                };
              }
              next.push(createUserMessage(text));
              // Real final_output becomes the next assistant message; fixed copy only if absent.
              next.push({
                id: `msg_${crypto.randomUUID()}`,
                role: 'assistant',
                blocks:
                  textBlocks.length > 0
                    ? textBlocks
                    : [{ type: 'text', content: '已提交回答并恢复执行' }],
                status: 'done',
                created_at: Date.now(),
              });
              return next;
            });
            setInput('');
            setAwaitingInput(null);
            onSessionUpdate?.();
            return;
          }
          case 'no_checkpoint':
            // 后端明确要求重新走普通 chat 流，由下方现有发送流程接管。
            setAwaitingInput(null);
            break;
          case 'mismatch':
            // 保留等待态，更新 expected run id，提示可重试
            setAwaitingInput((prev) => prev ? {
              ...prev,
              subAgentRunId: result.expected_sub_agent_run_id ?? prev.subAgentRunId,
              error: result.message ?? '等待任务已更新，请重新提交回答。',
            } : prev);
            return;
          case 'not_found':
            // 保留等待态 + 可重试错误（规格：mismatch/not_found 均不清除 awaiting）
            setAwaitingInput((prev) => prev ? {
              ...prev,
              error: result.message ?? '等待任务已失效，请重新提交或换一种描述。',
            } : prev);
            return;
          case 'in_progress':
          case 'invoke_noop':
          case 'invoke_failed':
            // These terminal HTTP responses have not consumed the pending
            // task. Never fall through into a fresh /api/chat request: that
            // would duplicate spatial work or lose the user's clarification.
            setAwaitingInput((prev) => prev ? {
              ...prev,
              error: result.message ?? '恢复尚未完成，请保留当前回答后重试。',
            } : prev);
            return;
        }
      } catch (err) {
        if ((err as Error).name === 'AbortError') return;
        // Ignore errors that race past a session switch (abort may surface as network error).
        if (!canApplyResumeResult(resumeSessionId, sessionIdRef.current, ac.signal)) {
          return;
        }
        setAwaitingInput((prev) => prev ? {
          ...prev,
          error: (err as Error).message || '提交回答失败，请重试。',
        } : prev);
        return;
      } finally {
        // Only clear streaming if this request still owns the active session/controller.
        // no_checkpoint falls through to normal send — re-claim the lock there.
        if (canApplyResumeResult(resumeSessionId, sessionIdRef.current, undefined) && abortRef.current === ac) {
          isStreamingRef.current = false;
          setIsStreaming(false);
          abortRef.current = null;
        }
      }
    }

    // 收集已上传完成的 file_id
    const uploadFileIds = pendingUploadsRef.current
      .filter((u) => u.status === 'ready' && u.file_id)
      .map((u) => u.file_id as string);

    const userMsg = createUserMessage(text);
    const assistantMsg: ChatMessage = {
      id: `msg_${crypto.randomUUID()}`,
      role: 'assistant',
      blocks: [],
      status: 'thinking',
      statusMessage: '分析中',
      thinkingTrace: [],
      executionTrace: [],
      created_at: Date.now(),
    };

    setMessages((prev) => [...prev, userMsg, assistantMsg]);
    setInput('');
    setPendingUploads([]);
    // Re-claim after resume finally (no_checkpoint) or keep claim from gate.
    isStreamingRef.current = true;
    setIsStreaming(true);
    scroll.forceBottom();

    const ac = new AbortController();
    abortRef.current = ac;

    try {
      const res = await chatStream(
        { session_id: sessionIdRef.current, message: text, upload_file_ids: uploadFileIds.length ? uploadFileIds : undefined },
        ac.signal,
      );
      if (!res.ok || !res.body) {
        const errBody = await res.json().catch(() => ({}));
        const errMsg = (errBody as { error?: { message?: string } }).error?.message ?? `HTTP ${res.status}`;
        updateLastAssistant((m) => ({
          ...m,
          status: 'error',
          error: { code: 'HTTP_ERROR', message: errMsg },
        }));
        isStreamingRef.current = false;
        setIsStreaming(false);
        return;
      }
      streamSessionIdRef.current = sessionIdRef.current;
      await startSSE(res, {
        onEvent: handleSSEEvent,
        onError: (err) => {
          streamSessionIdRef.current = null;
          updateLastAssistant((m) => ({
            ...m,
            status: 'error',
            error: { code: 'STREAM_ERROR', message: err.message || '连接中断' },
          }));
          isStreamingRef.current = false;
          setIsStreaming(false);
        },
        onComplete: () => {
          streamSessionIdRef.current = null;
          // 确保最终状态为 done 或保留 error
          updateLastAssistant((m) => (m.status === 'error' ? m : { ...m, status: 'done' }));
          isStreamingRef.current = false;
          setIsStreaming(false);
        },
      });
    } catch (err) {
      streamSessionIdRef.current = null;
      if ((err as Error).name === 'AbortError') {
        updateLastAssistant((m) => ({
          ...m,
          status: 'error',
          error: { code: 'CANCELLED', message: '已取消' },
        }));
      } else {
        updateLastAssistant((m) => ({
          ...m,
          status: 'error',
          error: { code: 'NETWORK', message: (err as Error).message || '网络错误' },
        }));
      }
      isStreamingRef.current = false;
      setIsStreaming(false);
    }
  }, [sessionId, scroll, startSSE, handleSSEEvent, updateLastAssistant, onSessionUpdate]);

  const handleStop = useCallback(() => {
    // Use run control API for graceful cancel, then abort SSE connection
    const runId = runIdRef.current;
    if (runId) {
      cancelRun(runId).catch(() => { /* best-effort */ });
    }
    abortRef.current?.abort();
    runIdRef.current = null;
    setIsPaused(false);
    // 取消是显式退出 awaiting 的合法路径（与 session 切换 / 成功 resume 并列）
    setAwaitingInput(null);
    isStreamingRef.current = false;
    setIsStreaming(false);
    updateLastAssistant((m) => ({
      ...m,
      status: 'error',
      error: { code: 'CANCELLED', message: '已取消' },
    }));
  }, [updateLastAssistant]);

  const handlePause = useCallback(() => {
    const runId = runIdRef.current;
    if (runId) {
      pauseRun(runId).then(() => setIsPaused(true)).catch(() => { /* best-effort */ });
    }
  }, []);

  const handleResume = useCallback(() => {
    const runId = runIdRef.current;
    if (runId) {
      resumeRun(runId).then(() => setIsPaused(false)).catch(() => { /* best-effort */ });
    }
  }, []);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void handleSend();
    }
  };

  // 文件上传 — 多个文件并发上传，保留单文件进度
  const handleFiles = useCallback(async (files: FileList | File[]) => {
    const arr = Array.from(files);
    setPendingUploads((prev) => [...prev, ...arr.map((file) => ({ file, status: 'uploading' as const }))]);
    await Promise.allSettled(
      arr.map(async (file) => {
        try {
          const res = await uploadFile(file);
          setPendingUploads((prev) =>
            prev.map((p) => (p.file === file ? { ...p, status: 'ready', file_id: res.file_id } : p)),
          );
        } catch (err) {
          setPendingUploads((prev) =>
            prev.map((p) => (p.file === file ? { ...p, status: 'error', error: (err as Error).message } : p)),
          );
        }
      }),
    );
  }, []);

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    if (e.dataTransfer.files.length) void handleFiles(e.dataTransfer.files);
  };

  const hasMessages = messages.length > 0;

  return (
    <div className="flex h-full flex-col">
      {/* 消息列表 */}
      <div
        ref={scroll.containerRef}
        className={
          hasMessages
            ? "flex-1 overflow-y-auto overscroll-contain scroll-smooth"
            : "flex-1 overflow-y-auto overscroll-contain scroll-smooth flex items-center justify-center"
        }
        style={{ scrollbarGutter: 'stable' }}
      >
        {hasMessages ? (
          <div className="mx-auto w-full max-w-3xl px-4 pb-6 pt-6">
            <div className="flex flex-col gap-6">
              {messages.map((m) => (
                <MessageBubble
                  key={m.id}
                  message={m}
                  onBeforeMapLoad={scroll.beforeMutation}
                  onAfterMapLoad={scroll.afterMutation}
                />
              ))}
            </div>
          </div>
        ) : (
          <WelcomeHero />
        )}
      </div>

      {/* 输入区 */}
      <div className="border-t border-ink-700 bg-ink-900/80 backdrop-blur-md">
        <div className="mx-auto w-full max-w-3xl px-4 py-3">
          {awaitingInput && (
            <div
              role="status"
              className="mb-2 rounded-lg border border-amber/40 bg-amber/10 px-3 py-2 text-sm text-ink-200"
            >
              <div className="font-medium text-amber">等待你的回答</div>
              <div className="mt-1 text-ink-300">{awaitingInput.message}</div>
              {awaitingInput.error && (
                <div className="mt-1 text-xs text-signal-error">{awaitingInput.error}</div>
              )}
            </div>
          )}

          {/* 待上传文件 chip */}
          {pendingUploads.length > 0 && (
            <div className="mb-2 flex flex-wrap gap-2">
              {pendingUploads.map((u, i) => (
                <span
                  key={i}
                  className="flex items-center gap-2 rounded-md border border-ink-600 bg-ink-800 px-2 py-1 font-mono text-[11px] text-ink-300"
                >
                  <FileIcon />
                  <span className="max-w-[160px] truncate">{u.file.name}</span>
                  {u.status === 'uploading' && <span className="text-signal-fetching">上传中…</span>}
                  {u.status === 'ready' && <span className="text-signal-done">就绪</span>}
                  {u.status === 'error' && <span className="text-signal-error" title={u.error}>失败</span>}
                  <button
                    type="button"
                    onClick={() => setPendingUploads((prev) => prev.filter((_, j) => j !== i))}
                    className="text-ink-500 hover:text-signal-error"
                    aria-label="移除"
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>
          )}

          <div
            className={`relative flex items-end gap-2 rounded-xl border bg-ink-800 px-3 py-2 transition ${
              dragOver ? 'border-amber shadow-glow' : 'border-ink-600 focus-within:border-ink-500'
            }`}
            onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
            onDragLeave={() => setDragOver(false)}
            onDrop={onDrop}
          >
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-ink-400 transition hover:bg-ink-700 hover:text-amber"
              title="上传文件（shp zip / geojson / kml）"
              aria-label="上传文件"
            >
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" />
              </svg>
            </button>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept=".zip,.geojson,.json,.kml"
              className="hidden"
              onChange={(e) => {
                if (e.target.files?.length) void handleFiles(e.target.files);
                e.target.value = '';
              }}
            />

            <textarea
              ref={textareaRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              rows={1}
              placeholder={
                awaitingInput
                  ? '请输入补充信息'
                  : '问 Gismind：南京新街口 500 米内有多少蜜雪冰城…'
              }
              className="max-h-[200px] flex-1 resize-none bg-transparent py-2 text-[15px] leading-relaxed text-ink-100 placeholder:text-ink-500 focus:outline-none"
              disabled={isStreaming}
            />

            {isStreaming ? (
              <div className="flex shrink-0 items-center gap-2">
                {isPaused ? (
                  <button
                    type="button"
                    onClick={handleResume}
                    className="flex h-9 items-center gap-2 rounded-lg border border-amber/40 bg-amber/10 px-3 text-sm text-amber transition hover:bg-amber/20"
                  >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z" /></svg>
                    继续
                  </button>
                ) : (
                  <button
                    type="button"
                    onClick={handlePause}
                    className="flex h-9 items-center gap-2 rounded-lg border border-ink-500/40 bg-ink-700/50 px-3 text-sm text-ink-300 transition hover:bg-ink-700"
                  >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16" /><rect x="14" y="4" width="4" height="16" /></svg>
                    暂停
                  </button>
                )}
                <button
                  type="button"
                  onClick={handleStop}
                  className="flex h-9 shrink-0 items-center gap-2 rounded-lg border border-signal-error/40 bg-signal-error/10 px-3 text-sm text-signal-error transition hover:bg-signal-error/20"
                >
                  <span className="h-3 w-3 rounded-sm bg-signal-error" />
                  停止
                </button>
              </div>
            ) : (
              <button
                type="button"
                onClick={handleSend}
                disabled={!input.trim()}
                className="flex h-9 shrink-0 items-center gap-2 rounded-lg bg-amber px-3 text-sm font-medium text-ink-950 transition hover:bg-amber-glow disabled:cursor-not-allowed disabled:bg-ink-600 disabled:text-ink-400"
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <path d="m22 2-7 20-4-9-9-4Z" />
                </svg>
                {awaitingInput ? '回答并继续' : '发送'}
              </button>
            )}
          </div>
          <div className="mt-1.5 flex items-center justify-between px-1">
            <span className="font-mono text-[10px] text-ink-500">
              Enter 发送 · Shift+Enter 换行 · 拖拽文件上传
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------

function WelcomeHero() {
  return (
    <div className="flex flex-col items-center text-center animate-fade-in">
      <div className="mb-5 flex h-16 w-16 items-center justify-center rounded-2xl border border-ink-700 bg-ink-800 shadow-glow">
        <svg width="36" height="36" viewBox="0 0 32 32" fill="none">
          <circle cx="16" cy="14" r="9" stroke="#ff7a1a" strokeWidth="2.5" />
          <path d="M16 5 L16 14 L22 16" stroke="#ff7a1a" strokeWidth="2.5" strokeLinecap="round" fill="none" />
          <path d="M4 26 Q16 18 28 26" stroke="#5b6776" strokeWidth="1.5" fill="none" strokeLinecap="round" />
        </svg>
      </div>
      <h1 className="font-display text-3xl text-ink-100">
        <span className="text-amber">Gismind</span> · 空间智能
      </h1>
      <p className="mt-2 max-w-md text-sm leading-relaxed text-ink-400">
        用自然语言完成 POI 查询、缓冲区、叠加分析、等时圈等空间任务。
        Agent 自动调度高德与 OpenStreetMap 数据源。
      </p>

      {/* 键盘提示行 — 替代原 examples grid 的视觉锚点 */}
      <div className="mt-7 flex items-center gap-1 font-mono text-[10px] uppercase tracking-[0.22em] text-ink-500">
        <Kbd>Enter</Kbd>
        <span>发送</span>
        <Sep />
        <span className="inline-flex items-center gap-1">
          <Kbd>Shift</Kbd>
          <span className="text-ink-600">+</span>
          <Kbd>Enter</Kbd>
        </span>
        <span>换行</span>
        <Sep />
        <Kbd>t</Kbd>
        <span>切换主题</span>
        <Sep />
        <span>拖拽文件上传</span>
      </div>
    </div>
  );
}

function Kbd({ children }: { children: React.ReactNode }) {
  return (
    <span className="inline-flex h-[18px] min-w-[18px] items-center justify-center rounded border border-ink-600 bg-ink-900/80 px-1 text-[9px] leading-none text-ink-300">
      {children}
    </span>
  );
}

function Sep() {
  return <span aria-hidden="true" className="mx-1.5 text-ink-700">·</span>;
}

function FileIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <path d="M14 2v6h6" />
    </svg>
  );
}

function createUserMessage(content: string): ChatMessage {
  return {
    id: `msg_${crypto.randomUUID()}`,
    role: 'user',
    blocks: [{ type: 'text', content }],
    status: 'done',
    created_at: Date.now(),
  };
}

function loadMessages(sid: string): ChatMessage[] {
  try {
    const raw = localStorage.getItem(messagesKey(sid));
    if (!raw) return [];
    const arr = JSON.parse(raw) as ChatMessage[];
    // 过滤掉历史中 status 异常的（如崩溃残留的 thinking）
    return arr.map((m) => (m.role === 'assistant' && (m.status === 'thinking' || m.status === 'fetching' || m.status === 'summarizing' || m.status === 'reviewing' || m.status === 'reflecting')
      ? { ...m, status: 'done' as const }
      : m));
  } catch {
    return [];
  }
}

function messagesKey(sid: string): string {
  return `gismind.messages.${sid}`;
}
