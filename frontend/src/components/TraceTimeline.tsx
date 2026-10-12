/** Projection of the durable Runtime event log for one assistant response. */
import { useMemo, useState } from 'react';
import type { RuntimeEvent } from '@/types/runtime';

interface TraceTimelineProps {
  events: RuntimeEvent[];
  active?: boolean;
}

type TaskStatus = 'pending' | 'running' | 'succeeded' | 'failed' | 'awaiting_input' | 'cancelled';
interface TaskView {
  taskId: string;
  operation: string;
  status: TaskStatus;
  events: RuntimeEvent[];
}

function statusFor(event: RuntimeEvent, current: TaskStatus): TaskStatus {
  const eventStatus = typeof event.payload.status === 'string' ? event.payload.status : undefined;
  if (eventStatus === 'leased' || eventStatus === 'running' || eventStatus === 'validating') return 'running';
  if (eventStatus === 'succeeded') return 'succeeded';
  if (eventStatus === 'failed') return 'failed';
  if (eventStatus === 'cancelled') return 'cancelled';
  if (event.type === 'task.leased' || event.type === 'task.started') return 'running';
  if (event.type === 'task.completed' || event.type === 'task.empty') return 'succeeded';
  if (event.type === 'task.failed' || event.type === 'task.worker_lost' || event.type === 'task.resource_limit_exceeded') return 'failed';
  if (event.type === 'task.cancelled') return 'cancelled';
  if (event.type === 'run.awaiting_input') return 'awaiting_input';
  return current;
}

function taskViews(events: RuntimeEvent[]): TaskView[] {
  const tasks = new Map<string, TaskView>();
  for (const event of events) {
    const taskId = event.task_id ?? (typeof event.payload.task_id === 'string' ? event.payload.task_id : undefined);
    if (!taskId) continue;
    const view = tasks.get(taskId) ?? {
      taskId,
      operation: typeof event.payload.op === 'string' ? event.payload.op : 'GIS task',
      status: 'pending' as TaskStatus,
      events: [],
    };
    view.events.push(event);
    if (typeof event.payload.op === 'string') view.operation = event.payload.op;
    view.status = statusFor(event, view.status);
    tasks.set(taskId, view);
  }
  return [...tasks.values()];
}

function messageFor(event: RuntimeEvent): string {
  return typeof event.payload.message === 'string' ? event.payload.message : event.type;
}

function safeJson(value: unknown): string {
  try {
    const text = JSON.stringify(value, null, 2);
    return text.length > 1800 ? `${text.slice(0, 1800)}\n… truncated` : text;
  } catch {
    return String(value);
  }
}

const STATUS_STYLE: Record<TaskStatus, { icon: string; label: string; color: string }> = {
  pending: { icon: '○', label: '等待', color: 'text-ink-500 border-ink-600' },
  running: { icon: '◌', label: '执行中', color: 'text-amber border-amber/50' },
  succeeded: { icon: '✓', label: '完成', color: 'text-signal-done border-signal-done/50' },
  failed: { icon: '×', label: '失败', color: 'text-signal-error border-signal-error/50' },
  cancelled: { icon: '■', label: '已取消', color: 'text-ink-500 border-ink-600' },
  awaiting_input: { icon: '?', label: '等待输入', color: 'text-amber border-amber/50' },
};

export default function TraceTimeline({ events, active = false }: TraceTimelineProps) {
  const [showAll, setShowAll] = useState(false);
  const tasks = useMemo(() => taskViews(events), [events]);
  const rootEvents = events.filter((event) => !event.task_id && typeof event.payload.task_id !== 'string');
  if (events.length === 0) return null;
  return (
    <section className="trace-timeline mb-3 overflow-hidden rounded-lg border border-ink-600/80 bg-ink-950/55" aria-label="Runtime event log">
      <header className="flex items-center justify-between border-b border-ink-700 px-3 py-2">
        <div className="flex items-center gap-2"><span className={`h-1.5 w-1.5 rounded-full ${active ? 'animate-pulse bg-amber' : 'bg-signal-done'}`} /><span className="font-mono text-[10px] uppercase tracking-[0.2em] text-ink-300">Runtime Event Log</span></div>
        <span className="font-mono text-[10px] text-ink-500">{tasks.length ? `${tasks.length} tasks` : `${events.length} events`}</span>
      </header>
      {rootEvents.slice(-3).map((event) => <div key={event.event_id} className="border-b border-ink-700/70 px-3 py-1.5 font-mono text-[10px] text-ink-400">› {messageFor(event)}</div>)}
      <div className="divide-y divide-ink-700/70">
        {tasks.map((task, index) => <TaskRow key={task.taskId} task={task} index={index} showAll={showAll} />)}
      </div>
      {events.length > 3 && <button type="button" onClick={() => setShowAll((value) => !value)} className="w-full border-t border-ink-700 px-3 py-1.5 text-left font-mono text-[9px] uppercase tracking-[0.14em] text-ink-500 hover:bg-ink-800/50 hover:text-ink-300">{showAll ? '▾ 收起事件详情' : '▸ 展开事件详情'}</button>}
    </section>
  );
}

function TaskRow({ task, index, showAll }: { task: TaskView; index: number; showAll: boolean }) {
  const style = STATUS_STYLE[task.status];
  const detailEvents = showAll ? task.events : task.events.slice(-2);
  return <article className="px-3 py-2.5"><div className="flex items-start gap-2.5"><span className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full border font-mono text-[11px] ${style.color} ${task.status === 'running' ? 'animate-pulse' : ''}`}>{style.icon}</span><div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><span className="font-mono text-[9px] text-ink-500">{String(index + 1).padStart(2, '0')}</span><span className="font-mono text-[10px] text-amber/90">{task.operation}</span><span className={`font-mono text-[9px] ${style.color.split(' ')[0]}`}>{style.label}</span></div><div className="mt-0.5 font-mono text-[9px] text-ink-500">{task.taskId}</div>{detailEvents.map((event) => <details key={event.event_id} className="mt-1.5 rounded border border-ink-700/80 bg-ink-900/50"><summary className="cursor-pointer px-2 py-1 font-mono text-[9px] text-ink-400">{event.type} · {messageFor(event)}</summary><pre className="max-h-52 overflow-auto border-t border-ink-700 p-2 whitespace-pre-wrap break-all font-mono text-[9px] leading-relaxed text-ink-300">{safeJson(event.payload)}</pre></details>)}</div></div></article>;
}
