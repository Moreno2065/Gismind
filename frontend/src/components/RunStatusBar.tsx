import { useState } from 'react';
import type { RunProjectionState } from '@/types/runtime';
import { runtimeTaskAnchor, TaskExecutionDetails } from './TaskExecutionDetails';

interface RunStatusBarProps {
  projection: RunProjectionState | null;
  connectionState: string;
  terminalSettled: boolean;
}

const statusLabels: Record<RunProjectionState['status'], string> = {
  queued: '等待执行', planning: '正在规划', awaiting_input: '等待补充信息', ready: '准备执行',
  running: '运行中', cancelling: '正在停止', completed: '已完成', failed: '执行失败', cancelled: '已停止',
};

const connectionLabels: Record<string, string> = {
  idle: '尚未连接', connecting: '正在连接', live: '实时同步', reconnecting: '正在恢复连接', closed: '连接已关闭',
};

const taskLabels: Record<string, string> = {
  blocked: '等待前序', ready: '待执行', leased: '准备中', running: '执行中', validating: '正在校验',
  retry_wait: '等待重试', awaiting_input: '等待输入', succeeded: '完成', failed: '失败', cancelled: '已取消',
};

function stateColor(status: string) {
  return status === 'failed' ? 'text-signal-error' : status === 'succeeded' ? 'text-signal-done'
    : ['running', 'validating', 'leased', 'requested', 'retry_wait', 'awaiting_input'].includes(status) ? 'text-amber' : 'text-ink-400';
}

function FailureDetail({ code, message }: { code?: string | null; message?: string | null }) {
  if (!code && !message) return null;
  return <div className="mt-1.5 space-y-1 break-words text-[11px] leading-relaxed text-signal-error">
    {code && <div className="font-mono text-[10px]">{code}</div>}
    {message && <p className="whitespace-pre-wrap">{message}</p>}
  </div>;
}

function DiagnosticActions({ projection, path }: { projection: RunProjectionState; path: string | null }) {
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle');
  const code = String(projection.diagnostic?.code ?? '');
  const guidance = projection.status === 'awaiting_input' ? '请在下方补充所需信息，再继续执行。'
    : /CRS/.test(code) ? '请检查输入数据的坐标系，补充或更正坐标系后重新提交。'
    : /PARAM|PORT_MISMATCH|DRAFT_INVALID|GOAL_UNCOVERED/.test(code) ? '请检查下列位置对应的输入、参数或目标，修改请求后重新提交。'
    : '请检查输入数据与该步骤的参数，修改请求后重新提交；仍失败时可复制诊断以便排查。';
  const diagnostic = JSON.stringify({ run_id: projection.run_id, last_seq: projection.last_seq,
    diagnostic: projection.diagnostic,
    failed_tasks: projection.tasks.filter(task => task.status === 'failed').map(task => ({ task_id: task.task_id, task_key: task.task_key, op: task.op, error_code: task.error_code, error: task.error })),
  }, null, 2);
  const copy = async () => {
    try { await navigator.clipboard.writeText(diagnostic); setCopyState('copied'); }
    catch { setCopyState('failed'); }
  };
  return <div className="mt-2 space-y-2 text-[11px] leading-relaxed text-ink-300">
    {path && <p className="break-all"><span className="text-ink-500">问题位置：</span><code>{path}</code></p>}
    <p>{guidance}</p>
    <button type="button" onClick={() => { void copy(); }} className="min-h-8 rounded border border-ink-600 px-2.5 text-ink-300 hover:border-amber hover:text-amber focus-visible:outline focus-visible:outline-2 focus-visible:outline-amber">复制诊断</button>
    <span role="status" className="ml-2 text-ink-400">{copyState === 'copied' ? '已复制' : copyState === 'failed' ? '复制失败，请选中下方诊断文本。' : ''}</span>
    {copyState === 'failed' && <textarea aria-label="诊断文本" readOnly value={diagnostic} rows={5} className="w-full rounded border border-ink-600 bg-ink-950 p-2 font-mono text-[10px] text-ink-300" />}
  </div>;
}

export function RunStatusBar({ projection, connectionState, terminalSettled }: RunStatusBarProps) {
  if (!projection && connectionState === 'idle') return null;
  const tasks = projection?.tasks ?? [];
  const planning = projection?.planning_attempts ?? [];
  const latestPlanning = planning.at(-1);
  const completed = tasks.filter((task) => task.status === 'succeeded').length;
  const terminalStatus = !!projection && ['completed', 'failed', 'cancelled'].includes(projection.status);
  const syncingFinal = terminalStatus && !terminalSettled;
  const label = syncingFinal ? '正在同步最终结果'
    : projection?.status === 'planning' && latestPlanning?.phase === 'repair' && latestPlanning.status === 'requested' ? '正在修复计划'
    : projection ? statusLabels[projection.status] : '正在同步任务';
  const failed = projection?.status === 'failed' && terminalSettled;
  const settled = terminalSettled && terminalStatus;
  const dotColor = failed ? 'bg-signal-error' : settled ? 'bg-ink-400' : 'bg-amber';
  const diagnosticCode = typeof projection?.diagnostic?.code === 'string' ? projection.diagnostic.code : null;
  const diagnosticMessage = typeof projection?.diagnostic?.message === 'string' ? projection.diagnostic.message : null;
  const diagnosticPath = typeof projection?.diagnostic?.path === 'string' ? projection.diagnostic.path : null;
  const diagnosticInRow = [...tasks, ...planning].some((item) => item.error_code === diagnosticCode && item.error === diagnosticMessage);
  const showDiagnostic = ['failed', 'awaiting_input'].includes(projection?.status ?? '') && (diagnosticCode || diagnosticMessage || diagnosticPath);

  return <section className="runtime-status-bar shrink-0 overflow-hidden rounded-xl border border-ink-600/70 bg-ink-900/90" aria-label="任务运行状态" data-testid="runtime-progress" data-run-id={projection?.run_id}>
    <div className="flex flex-wrap items-center justify-between gap-x-5 gap-y-2 px-4 py-3">
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1" role="status" aria-live="polite" aria-atomic="true">
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${dotColor}`} aria-hidden="true" />
        <span data-testid="runtime-stage" className={`text-sm font-medium ${failed ? 'text-signal-error' : 'text-ink-100'}`}>
          <span>{label}</span>{projection?.status === 'running' && <span className="text-[11px] font-normal text-ink-400"> · 执行中</span>}
        </span>
        {tasks.length > 0 && <span className="text-[11px] tabular-nums text-ink-400">{`已完成 ${completed} / ${tasks.length} 个步骤`}</span>}
      </div>
      {!settled && <span className={`text-[10px] tracking-wide ${connectionState === 'reconnecting' || connectionState === 'closed' ? 'text-amber' : 'text-ink-400'}`}>
        {connectionLabels[connectionState] ?? '连接状态未知'}
      </span>}
    </div>
    {(planning.length > 0 || tasks.length > 0 || showDiagnostic) && <div
      className="max-h-[min(24vh,14rem)] overflow-y-auto overscroll-contain border-t border-ink-700/70 focus-visible:outline focus-visible:outline-1 focus-visible:outline-amber"
      data-testid="runtime-progress-list" role="region" aria-label="执行步骤" tabIndex={0}>
      {planning.length > 0 && <ol aria-label="规划过程" className="divide-y divide-ink-700/50">
        {planning.map((attempt) => <li key={attempt.attempt_id} data-planning-attempt-id={attempt.attempt_id}
          data-planning-phase={attempt.phase} data-planning-status={attempt.status} className="px-4 py-2">
          <div className="flex items-center justify-between gap-3 text-[11px]">
            <span className="text-ink-300">{attempt.phase === 'repair' ? '修复计划' : '生成计划'}</span>
            <span className={`shrink-0 ${stateColor(attempt.status)}`}>{attempt.status === 'requested' ? '等待模型响应' : attempt.status === 'succeeded' ? '完成' : '未通过'}</span>
          </div>
          {attempt.status === 'failed' && <FailureDetail code={attempt.error_code} message={attempt.error} />}
        </li>)}
      </ol>}
      {tasks.length > 0 && <ol aria-label="计划步骤" className="divide-y divide-ink-700/60 border-t border-ink-700/60">
        {tasks.map((task, index) => <li key={task.task_id} id={runtimeTaskAnchor(task.task_id)} tabIndex={-1} data-task-id={task.task_id} data-task-status={task.status}
          data-task-op={task.op ?? ''} data-task-order={task.display_order} className="flex gap-3 px-4 py-2.5" aria-current={['leased', 'running', 'validating'].includes(task.status) ? 'step' : undefined}>
          <span className={`mt-0.5 shrink-0 font-mono text-[10px] tabular-nums ${stateColor(task.status)}`}>{String(index + 1).padStart(2, '0')}</span>
          <div className="min-w-0 flex-1">
            <div className="flex items-start justify-between gap-3">
              <span className="break-words text-[12px] leading-relaxed text-ink-200">{task.task_key}</span>
              <span className={`shrink-0 text-[10px] leading-relaxed ${stateColor(task.status)}`}>{taskLabels[task.status] ?? task.status}</span>
            </div>
            {task.op && <div className="mt-0.5 break-all font-mono text-[10px] text-ink-400">{task.op}</div>}
            {['failed', 'retry_wait', 'awaiting_input'].includes(task.status) && <FailureDetail code={task.error_code} message={task.error} />}
            <TaskExecutionDetails task={task} />
          </div>
        </li>)}
      </ol>}
      {showDiagnostic && <div className="border-t border-signal-error/20 px-4 py-2" data-testid="runtime-diagnostic">
        {!diagnosticInRow && <FailureDetail code={diagnosticCode} message={diagnosticMessage} />}
        {projection && <DiagnosticActions key={`${projection.run_id}:${diagnosticCode}:${diagnosticPath}`} projection={projection} path={diagnosticPath} />}
      </div>}
      {projection && <details className="border-t border-ink-700/60 text-[10px] text-ink-400">
        <summary className="cursor-pointer px-4 py-2 hover:text-ink-200">运行标识与同步游标</summary>
        <dl className="space-y-1 break-all px-4 pb-3 font-mono"><div><dt className="inline">Run: </dt><dd className="inline">{projection.run_id}</dd></div>
          <div><dt className="inline">事件序号: </dt><dd className="inline">{projection.last_seq}</dd></div></dl>
      </details>}
    </div>}
  </section>;
}
