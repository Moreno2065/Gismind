// frontend/src/components/MessageBubble.tsx
// 混合渲染消息气泡 — blocks 数组：text(react-markdown) / map(LazyMapView) / chart(ChartView)。
// 顶部状态条（thinking/fetching/error），底部思考折叠。

import { memo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { ChatMessage, MessageStatus } from '@/types/message';
import { LazyMapView } from './LazyMapView';
import { ChartView } from './ChartView';
import { ThinkingCollapse } from './ThinkingCollapse';
import TraceTimeline from './TraceTimeline';

interface MessageBubbleProps {
  message: ChatMessage;
  onBeforeMapLoad?: () => void;
  onAfterMapLoad?: () => void;
}

function MessageBubbleImpl({ message, onBeforeMapLoad, onAfterMapLoad }: MessageBubbleProps) {
  const isUser = message.role === 'user';

  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'} animate-fade-in-up`}>
      <div className={`flex w-full max-w-3xl flex-col ${isUser ? 'items-end' : 'items-start'}`}>
        {/* 角色标签 */}
        <div className="mb-1.5 flex items-center gap-2 px-1">
          <RoleTag role={message.role} />
          {message.trace_id && (
            <span className="font-mono text-[10px] text-ink-500">
              {message.trace_id.slice(0, 12)}
            </span>
          )}
        </div>

        {/* 气泡主体 */}
        <div
          className={
            isUser
              ? 'w-full max-w-[85%] rounded-2xl rounded-tr-sm border border-amber/30 bg-amber/10 px-4 py-3 text-ink-100 shadow-bubble'
              : 'w-full rounded-2xl rounded-tl-sm border border-ink-700 bg-ink-800 px-4 py-3 text-ink-200 shadow-bubble'
          }
        >
          {/* 状态条 */}
          {message.status !== 'done' && message.status !== 'error' && (
            <StatusStrip status={message.status} message={message.statusMessage} />
          )}

          {/* 错误条 */}
          {message.status === 'error' && message.error && (
            <div className="mb-3 flex items-start gap-2 rounded-md border border-signal-error/30 bg-signal-error/10 px-3 py-2">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="mt-0.5 shrink-0 text-signal-error">
                <circle cx="12" cy="12" r="10" />
                <path d="M12 8v4M12 16h.01" />
              </svg>
              <div>
                <div className="font-mono text-[11px] text-signal-error">{message.error.code}</div>
                <div className="text-sm text-ink-200">{message.error.message}</div>
              </div>
            </div>
          )}

          {/* 思考折叠（仅 assistant 且有思考链） */}
          {!isUser && message.thinkingTrace && message.thinkingTrace.length > 0 && (
            <ThinkingCollapse steps={message.thinkingTrace} />
          )}

          {!isUser && message.executionTrace && message.executionTrace.length > 0 && (
            <TraceTimeline
              events={message.executionTrace}
              active={message.status !== 'done' && message.status !== 'error'}
            />
          )}

          {/* blocks 混合渲染 */}
          <div className="space-y-1">
            {message.blocks.map((block, i) => {
              if (block.type === 'text') {
                return (
                  <div key={i} className="prose-chat">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{block.content}</ReactMarkdown>
                    {/* 流式光标 — 仅当仍在生成且为最后一个 block */}
                    {message.status !== 'done' && message.status !== 'error' && i === message.blocks.length - 1 && (
                      <span className="ml-0.5 inline-block h-4 w-1.5 translate-y-0.5 animate-blink bg-amber" />
                    )}
                  </div>
                );
              }
              if (block.type === 'map') {
                return (
                  <LazyMapView
                    key={i}
                    layers={block.layers}
                    bbox={block.bbox}
                    expired={block.expired}
                    featureCount={block.featureCount}
                    onBeforeLoad={onBeforeMapLoad}
                    onAfterLoad={onAfterMapLoad}
                  />
                );
              }
              if (block.type === 'chart') {
                return <ChartView key={i} config={block.config} />;
              }
              return null;
            })}

            {/* 纯思考态（无任何 block）的骨架 */}
            {message.blocks.length === 0 && message.status !== 'done' && message.status !== 'error' && (
              <ThinkingSkeleton />
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

/**
 * memo with shallow comparison prevents re-renders of non-last messages
 * when only the last (streaming) message updates. Note: during streaming the
 * last message itself always receives a new reference on every token, so its
 * memo guard is inert — this is expected and acceptable.
 */
export const MessageBubble = memo(MessageBubbleImpl);

// ---------------------------------------------------------------------------

function RoleTag({ role }: { role: 'user' | 'assistant' }) {
  if (role === 'user') {
    return (
      <span className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-wider text-amber">
        <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor">
          <circle cx="12" cy="8" r="4" />
          <path d="M4 20c0-4 4-6 8-6s8 2 8 6" />
        </svg>
        You
      </span>
    );
  }
  return (
    <span className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-wider text-ink-300">
      <GismindMark />
      Gismind
    </span>
  );
}

function GismindMark() {
  return (
    <svg width="11" height="11" viewBox="0 0 32 32" fill="none">
      <circle cx="16" cy="14" r="7" stroke="#ff7a1a" strokeWidth="2.5" />
      <path d="M16 7 L16 14 L21 16" stroke="#ff7a1a" strokeWidth="2.5" strokeLinecap="round" fill="none" />
    </svg>
  );
}

function StatusStrip({ status, message }: { status: MessageStatus; message?: string }) {
  const color =
    status === 'thinking' ? 'text-signal-thinking'
    : status === 'fetching' ? 'text-signal-fetching'
    : status === 'summarizing' ? 'text-signal-thinking'
    : status === 'reviewing' ? 'text-signal-reviewing'
    : status === 'reflecting' ? 'text-signal-reflecting'
    : 'text-ink-400';

  const pingBg =
    status === 'fetching' ? 'bg-signal-fetching'
    : status === 'reviewing' ? 'bg-signal-reviewing'
    : status === 'reflecting' ? 'bg-signal-reflecting'
    : 'bg-signal-thinking';

  return (
    <div className={`mb-2 flex items-center gap-2 ${color}`}>
      <span className="relative flex h-2 w-2">
        <span className={`absolute inline-flex h-full w-full animate-ping rounded-full opacity-60 ${pingBg}`} />
        <span className={`relative inline-flex h-2 w-2 rounded-full ${pingBg}`} />
      </span>
      <span className="font-mono text-[11px] uppercase tracking-wider">
        {message || statusLabel(status)}
      </span>
    </div>
  );
}

function statusLabel(status: string): string {
  switch (status) {
    case 'thinking': return '分析中';
    case 'fetching': return '获取数据';
    case 'summarizing': return '汇总中';
    case 'reviewing': return '审查中';
    case 'reflecting': return '反思中';
    default: return status;
  }
}

function ThinkingSkeleton() {
  return (
    <div className="space-y-2 py-1">
      <div className="h-3 w-2/3 animate-pulse rounded bg-ink-600/60" />
      <div className="h-3 w-full animate-pulse rounded bg-ink-600/40" />
      <div className="h-3 w-5/6 animate-pulse rounded bg-ink-600/40" />
      <div
        className="mt-3 h-32 w-full animate-shimmer rounded-md border border-ink-700"
        style={{
          backgroundImage:
            'linear-gradient(90deg, transparent, rgba(255,122,26,0.06), transparent)',
          backgroundSize: '200% 100%',
        }}
      />
    </div>
  );
}
