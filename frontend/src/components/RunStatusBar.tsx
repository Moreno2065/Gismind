import type { RunProjectionState } from '@/types/runtime';

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

export function RunStatusBar({ projection, connectionState, terminalSettled }: RunStatusBarProps) {
  if (!projection && connectionState === 'idle') return null;
  const tasks = projection?.tasks ?? [];
  const completed = tasks.filter((task) => task.status === 'succeeded').length;
  const activeTask = tasks.find((task) => ['running', 'validating', 'leased'].includes(task.status));
  const terminalStatus = !!projection && ['completed', 'failed', 'cancelled'].includes(projection.status);
  const syncingFinal = terminalStatus && !terminalSettled;
  const label = syncingFinal ? '正在同步最终结果' : projection ? statusLabels[projection.status] : '正在同步任务';
  const failed = projection?.status === 'failed' && terminalSettled;
  const settled = terminalSettled && terminalStatus;
  const dotColor = failed ? 'bg-signal-error' : settled ? 'bg-ink-400' : 'bg-amber';

  return <section className="runtime-status-bar overflow-hidden rounded-xl border border-ink-600/70 bg-ink-900/90" aria-label="任务运行状态" role="status" aria-live="polite">
    <div className="flex flex-wrap items-center justify-between gap-x-5 gap-y-2 px-4 py-3">
      <div className="flex min-w-0 items-center gap-3">
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${dotColor}`} aria-hidden="true" />
        <span className={`text-sm font-medium ${failed ? 'text-signal-error' : 'text-ink-100'}`}>{label}</span>
        {tasks.length > 0 && <span className="text-[11px] tabular-nums text-ink-400">{`已完成 ${completed} / ${tasks.length} 个步骤`}</span>}
      </div>
      {!settled && <span className={`text-[10px] tracking-wide ${connectionState === 'reconnecting' || connectionState === 'closed' ? 'text-amber' : 'text-ink-400'}`}>
        {connectionLabels[connectionState] ?? '连接状态未知'}
      </span>}
    </div>
    {activeTask && !terminalStatus && <div className="flex min-w-0 items-center gap-3 border-t border-ink-700/70 px-4 py-2 text-[11px]">
      <span className="shrink-0 text-ink-400">当前步骤</span>
      <span className="truncate text-ink-200" title={activeTask.task_key}>{activeTask.task_key}</span>
      {activeTask.status === 'validating' && <span className="shrink-0 text-amber">正在校验</span>}
    </div>}
  </section>;
}
