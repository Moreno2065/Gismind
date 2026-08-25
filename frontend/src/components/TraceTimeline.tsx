/** Persistent execution ledger for one assistant response. */
import { useMemo, useState } from 'react';
import type { StreamDisplayKind, StreamEvent, WorkflowPlanTask } from '@/types/message';

interface TraceTimelineProps {
  events: StreamEvent[];
  active?: boolean;
}

type EventPayload = Record<string, unknown>;
type TaskStatus = 'pending' | 'running' | 'success' | 'failed' | 'awaiting_input';

interface TaskView extends WorkflowPlanTask {
  status: TaskStatus;
  events: StreamEvent[];
}

interface ToolExecution {
  id: string;
  name: string;
  params?: unknown;
  result?: unknown;
  status?: string;
  durationMs?: number;
}

function payloadOf(event: StreamEvent | undefined): EventPayload {
  if (!event) return {};
  const raw = event as unknown as EventPayload;
  return raw.data && typeof raw.data === 'object'
    ? raw.data as EventPayload
    : raw;
}

function nameOf(event: StreamEvent): string {
  const payload = payloadOf(event);
  return String(payload.event || (event as unknown as EventPayload).event || '');
}

function messageOf(event: StreamEvent): string {
  const payload = payloadOf(event);
  return String(payload.message || nameOf(event));
}

function displayKindOf(event: StreamEvent): StreamDisplayKind {
  const value = payloadOf(event).display_kind;
  return typeof value === 'string' ? value as StreamDisplayKind : 'debug';
}

function taskIdOf(event: StreamEvent): string {
  const taskId = payloadOf(event).task_id;
  return typeof taskId === 'string' ? taskId : '';
}

function normalizeStatus(value: unknown): TaskStatus {
  const status = String(value || '').toLowerCase();
  if (status === 'success' || status === 'refined' || status === 'complete' || status === 'completed') return 'success';
  if (status === 'failed' || status === 'error') return 'failed';
  if (status === 'awaiting_input') return 'awaiting_input';
  if (status === 'running') return 'running';
  return 'pending';
}

function buildTasks(events: StreamEvent[]): TaskView[] {
  const plan = events.find((event) => nameOf(event) === 'run.plan');
  const rawTasks = payloadOf(plan).tasks;
  const planned = Array.isArray(rawTasks) ? rawTasks as unknown as WorkflowPlanTask[] : [];
  const taskMap = new Map<string, TaskView>();

  for (const task of planned) {
    taskMap.set(task.id, {
      ...task,
      depends_on: Array.isArray(task.depends_on) ? task.depends_on : [],
      status: normalizeStatus(task.status),
      events: [],
    });
  }

  for (const event of events) {
    const payload = payloadOf(event);
    const taskId = taskIdOf(event);
    if (!taskId) continue;
    if (!taskMap.has(taskId)) {
      taskMap.set(taskId, {
        id: taskId,
        agent_role: String(payload.agent_role || '?'),
        goal: String(payload.goal || ''),
        depends_on: [],
        status: 'pending',
        events: [],
      });
    }
    const task = taskMap.get(taskId)!;
    task.events.push(event);
    if (nameOf(event) === 'run.task.start') task.status = 'running';
    if (nameOf(event) === 'run.task.complete') task.status = normalizeStatus(payload.status);
    if (nameOf(event) === 'judge.awaiting_input') task.status = 'awaiting_input';
  }
  return Array.from(taskMap.values());
}

function buildToolExecutions(events: StreamEvent[]): ToolExecution[] {
  const tools = new Map<string, ToolExecution>();
  for (const event of events) {
    const name = nameOf(event);
    if (name !== 'tool.call.start' && name !== 'tool.call.complete') continue;
    const payload = payloadOf(event);
    const id = String(payload.tool_call_id || `${payload.tool_name || 'tool'}-${tools.size}`);
    const existing = tools.get(id) || { id, name: String(payload.tool_name || 'tool') };
    if (name === 'tool.call.start') existing.params = payload.params;
    if (name === 'tool.call.complete') {
      existing.result = payload.result;
      existing.status = String(payload.status || 'success');
      if (typeof payload.duration_ms === 'number') existing.durationMs = payload.duration_ms;
    }
    tools.set(id, existing);
  }
  return Array.from(tools.values());
}

function safeJson(value: unknown): string {
  if (value === undefined) return '—';
  try {
    const text = JSON.stringify(value, null, 2);
    return text.length > 2400 ? `${text.slice(0, 2400)}\n… truncated` : text;
  } catch {
    return String(value).slice(0, 2400);
  }
}

const STATUS_STYLE: Record<TaskStatus, { icon: string; label: string; color: string }> = {
  pending: { icon: '○', label: '等待', color: 'text-ink-500 border-ink-600' },
  running: { icon: '◌', label: '执行中', color: 'text-amber border-amber/50' },
  success: { icon: '✓', label: '完成', color: 'text-signal-done border-signal-done/50' },
  failed: { icon: '×', label: '失败', color: 'text-signal-error border-signal-error/50' },
  awaiting_input: { icon: '?', label: '等待输入', color: 'text-amber border-amber/50' },
};

export default function TraceTimeline({ events, active = false }: TraceTimelineProps) {
  const [showDebug, setShowDebug] = useState(false);
  const tasks = useMemo(() => buildTasks(events), [events]);
  const planEvent = events.find((event) => nameOf(event) === 'run.plan');
  const instructions = planEvent && Array.isArray(payloadOf(planEvent).instructions)
    ? payloadOf(planEvent).instructions as Array<{ id: string; text: string }>
    : [];
  const rawPlannerSource = planEvent ? payloadOf(planEvent).planner_source : undefined;
  const plannerSource = rawPlannerSource === 'root_llm'
    || rawPlannerSource === 'guardrail'
    || rawPlannerSource === 'fallback'
    ? rawPlannerSource
    : '';
  const plannerSourceLabels = {
    root_llm: 'Root LLM',
    guardrail: 'Guardrail',
    fallback: 'Fallback',
  };
  const plannerSourceLabel = plannerSource ? plannerSourceLabels[plannerSource] : '';
  const instructionText = new Map(instructions.map((item) => [item.id, item.text]));
  const completeCount = tasks.filter((task) => task.status === 'success').length;
  const debugEvents = events.filter((event) => displayKindOf(event) === 'debug');
  const rootEvents = events.filter((event) => {
    const name = nameOf(event);
    return !taskIdOf(event) && name !== 'run.plan' && displayKindOf(event) !== 'debug';
  });

  if (events.length === 0) return null;

  return (
    <section className="trace-timeline mb-3 overflow-hidden rounded-lg border border-ink-600/80 bg-ink-950/55" aria-label="执行过程">
      <header className="flex items-center justify-between border-b border-ink-700 px-3 py-2">
        <div className="flex items-center gap-2">
          <span className={`h-1.5 w-1.5 rounded-full ${active ? 'animate-pulse bg-amber' : 'bg-signal-done'}`} />
          <span className="font-mono text-[10px] uppercase tracking-[0.2em] text-ink-300">Execution DAG</span>
        </div>
        <span className="font-mono text-[10px] text-ink-500">
          {tasks.length > 0 ? `${completeCount}/${tasks.length} steps` : `${events.length} events`}
        </span>
        {plannerSourceLabel && (
          <span className="font-mono text-[9px] uppercase tracking-[0.12em] text-ink-400">
            规划来源 · {plannerSourceLabel}
          </span>
        )}
      </header>

      {rootEvents.length > 0 && (
        <div className="border-b border-ink-700/70 px-3 py-2">
          {rootEvents.slice(-3).map((event, index) => (
            <div key={`${nameOf(event)}-${index}`} className="flex gap-2 py-0.5 font-mono text-[10px] text-ink-400">
              <span className="text-amber/70">›</span>
              <span>{messageOf(event)}</span>
            </div>
          ))}
        </div>
      )}

      <div className="divide-y divide-ink-700/70">
        {tasks.map((task, index) => (
          <TaskLedgerRow
            key={task.id}
            task={task}
            index={index}
            instruction={task.instruction_id ? instructionText.get(task.instruction_id) : undefined}
            showDebug={showDebug}
          />
        ))}
      </div>

      {debugEvents.length > 0 && (
        <button
          type="button"
          onClick={() => setShowDebug((value) => !value)}
          className="w-full border-t border-ink-700 px-3 py-1.5 text-left font-mono text-[9px] uppercase tracking-[0.14em] text-ink-500 transition hover:bg-ink-800/50 hover:text-ink-300"
        >
          {showDebug ? '▾ 隐藏调试事件' : `▸ 显示调试事件 (${debugEvents.length})`}
        </button>
      )}
    </section>
  );
}

function TaskLedgerRow({
  task,
  index,
  instruction,
  showDebug,
}: {
  task: TaskView;
  index: number;
  instruction?: string;
  showDebug: boolean;
}) {
  const status = STATUS_STYLE[task.status];
  const tools = buildToolExecutions(task.events);
  const detailEvents = task.events.filter((event) => {
    const name = nameOf(event);
    if (name === 'run.task.start' || name === 'run.task.complete' || name.startsWith('tool.call.')) return false;
    return displayKindOf(event) !== 'debug' || showDebug;
  });

  return (
    <article className="px-3 py-2.5">
      <div className="flex items-start gap-2.5">
        <span className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full border font-mono text-[11px] ${status.color} ${task.status === 'running' ? 'animate-pulse' : ''}`}>
          {status.icon}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="font-mono text-[9px] text-ink-500">{String(index + 1).padStart(2, '0')}</span>
            <span className="font-mono text-[10px] uppercase tracking-wider text-amber/90">{roleIcon(task.agent_role)} {task.agent_role}</span>
            {task.tool_name && (
              <span className="rounded border border-ink-600 bg-ink-900 px-1 font-mono text-[9px] text-ink-300">{task.tool_name}</span>
            )}
            <span className={`font-mono text-[9px] ${status.color.split(' ')[0]}`}>{status.label}</span>
          </div>
          <div className="mt-0.5 text-xs leading-relaxed text-ink-200">{task.goal}</div>
          {instruction && instruction !== task.goal && (
            <div className="mt-1 font-mono text-[9px] text-ink-500">指令 · {instruction}</div>
          )}
          {task.depends_on.length > 0 && (
            <div className="mt-1 font-mono text-[9px] text-ink-500">依赖 · {task.depends_on.join(' → ')}</div>
          )}

          {tools.map((tool) => (
            <details key={tool.id} className="group mt-2 rounded border border-ink-700 bg-ink-900/70">
              <summary className="flex cursor-pointer list-none items-center gap-2 px-2 py-1.5 font-mono text-[10px] text-ink-300 hover:text-ink-100">
                <span className={tool.status === 'success' ? 'text-signal-done' : tool.status ? 'text-signal-error' : 'text-amber'}>
                  {tool.status === 'success' ? '✓' : tool.status ? '×' : '◌'}
                </span>
                <span className="text-amber/90">{tool.name}</span>
                {tool.durationMs !== undefined && <span className="ml-auto text-ink-500">{tool.durationMs} ms</span>}
                <span className="text-ink-500 group-open:rotate-90">›</span>
              </summary>
              <div className="grid gap-px border-t border-ink-700 bg-ink-700 md:grid-cols-2">
                <TraceData title="INPUT" value={tool.params} />
                <TraceData title="OUTPUT" value={tool.result} />
              </div>
            </details>
          ))}

          {detailEvents.map((event, eventIndex) => {
            const payload = payloadOf(event);
            const code = typeof payload.code === 'string' ? payload.code : undefined;
            const detailValue = code
              ?? payload.stdout
              ?? payload.stderr
              ?? payload.traceback
              ?? payload.result;
            return (
              <details key={`${nameOf(event)}-${eventIndex}`} className="mt-1.5 rounded border border-ink-700/80 bg-ink-900/50">
                <summary className="cursor-pointer px-2 py-1 font-mono text-[9px] text-ink-400">
                  {eventMark(displayKindOf(event))} {messageOf(event)}
                </summary>
                {detailValue !== undefined && (
                  <div className="border-t border-ink-700">
                    <TraceData title={nameOf(event).toUpperCase()} value={detailValue} />
                  </div>
                )}
              </details>
            );
          })}
        </div>
      </div>
    </article>
  );
}

function TraceData({ title, value }: { title: string; value: unknown }) {
  return (
    <div className="min-w-0 bg-ink-950/90 p-2">
      <div className="mb-1 font-mono text-[8px] tracking-[0.18em] text-ink-500">{title}</div>
      <pre className="max-h-52 overflow-auto whitespace-pre-wrap break-all font-mono text-[9px] leading-relaxed text-ink-300">{safeJson(value)}</pre>
    </div>
  );
}

function eventMark(kind: StreamDisplayKind): string {
  if (kind === 'warning') return '⚠';
  if (kind === 'confirmation') return '?';
  if (kind === 'result' || kind === 'workflow_step') return '✓';
  return '·';
}

function roleIcon(role: string): string {
  return ({ geo: '⌖', poi: '⌕', geometer: '△', viz: '◇', coder: 'λ', verifier: '◎' } as Record<string, string>)[role] || '·';
}
