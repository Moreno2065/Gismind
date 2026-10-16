import { runtimeArtifactContentUrl } from '@/api/client';
import type { RunTaskProjection, TaskArtifactReference } from '@/types/runtime';

export function runtimeTaskAnchor(taskId: string) {
  return `runtime-task-${encodeURIComponent(taskId)}`;
}

function ArtifactLink({ artifactId }: { artifactId: string }) {
  return <a href={runtimeArtifactContentUrl({ artifact_id: artifactId })} download
    className="break-all text-amber underline decoration-amber/40 underline-offset-2 focus-visible:outline focus-visible:outline-1 focus-visible:outline-amber">{artifactId}</a>;
}

function ArtifactRows({ items }: { items: TaskArtifactReference[] }) {
  return <ul className="space-y-1">{items.map((item) => <li key={`${item.name}:${item.ordinal}`}>
    <span className="font-mono text-ink-400">{item.name}[{item.ordinal}] → </span><ArtifactLink artifactId={item.artifact_id} />
  </li>)}</ul>;
}

function Timestamp({ value }: { value: string }) {
  const date = new Date(value);
  return <time dateTime={value} title={value}>{Number.isNaN(date.getTime()) ? value : date.toLocaleString()}</time>;
}

export function TaskExecutionDetails({ task }: { task: RunTaskProjection }) {
  const details = task.details;
  if (!details) return null;
  const latest = details.latest_attempt;
  const elapsed = latest?.finished_at ? (Date.parse(latest.finished_at) - Date.parse(latest.started_at)) / 1000 : null;
  const parameters = Object.entries(details.parameters ?? {});
  return <details className="mt-2 min-w-0 text-[11px] leading-5" data-testid="task-execution-details">
    <summary className="cursor-pointer text-ink-400 hover:text-amber focus-visible:outline focus-visible:outline-1 focus-visible:outline-amber">查看执行详情</summary>
    <div className="mt-2 space-y-3 border-l border-ink-600/70 pl-3 text-ink-300 [overflow-wrap:anywhere]">
      <p className="text-[10px] text-ink-500">来自已保存的计划与执行记录；敏感内容已隐藏。</p>
      {details.dependencies.length > 0 && <div><p className="mb-1 text-ink-500">前序步骤</p><ul className="flex flex-wrap gap-x-3 gap-y-1">{details.dependencies.map((dependency) =>
        <li key={dependency.task_id}><a href={`#${runtimeTaskAnchor(dependency.task_id)}`} className="text-ink-300 underline decoration-ink-600 underline-offset-2 hover:text-amber focus-visible:outline focus-visible:outline-1 focus-visible:outline-amber">{dependency.task_key}</a></li>)}</ul></div>}
      <div><p className="mb-1 text-ink-500">输入与命名端口</p>
        {details.inputs.length ? <ul className="space-y-1">{details.inputs.map((input) => <li key={`${input.name}:${input.ordinal}`}>
          <span className="font-mono text-ink-400">{input.name} → </span>
          {input.artifact_id ? <ArtifactLink artifactId={input.artifact_id} /> : <span className="font-mono">{`${input.source_task_key ?? input.source_task_id}.${input.source_port}[${input.selection === 'all' ? '*' : input.source_ordinal ?? 0}]`}</span>}
        </li>)}</ul> : <p className="text-ink-400">无绑定输入</p>}
      </div>
      {details.resolved_inputs.length > 0 && <div><p className="mb-1 text-ink-500">已解析输入</p><ArtifactRows items={details.resolved_inputs} /></div>}
      <div><p className="mb-1 text-ink-500">关键参数</p>{parameters.length ? <dl className="space-y-1">{parameters.map(([key, value]) => <div key={key} className="flex flex-wrap gap-x-3">
        <dt className="font-mono text-ink-400">{key}</dt><dd className="min-w-0 font-mono">{typeof value === 'string' ? value : JSON.stringify(value)}</dd>
      </div>)}</dl> : <p className="text-ink-400">未设置额外参数</p>}</div>
      <div><p className="mb-1 text-ink-500">输出成果 · 可下载</p>{details.outputs.length ? <ArtifactRows items={details.outputs} /> : <p className="text-ink-400">尚无已记录的输出成果</p>}</div>
      <div className="space-y-1 border-t border-ink-700/60 pt-2 text-ink-400">
        <p>{latest ? `第 ${latest.attempt_no} 次执行` : `已执行 ${task.attempts} 次`}{details.max_attempts ? ` · 最多 ${details.max_attempts} 次` : ''}</p>
        {latest && <p>开始：<Timestamp value={latest.started_at} /></p>}
        {latest?.finished_at && <p>结束：<Timestamp value={latest.finished_at} />{elapsed !== null && Number.isFinite(elapsed) && elapsed >= 0 ? ` · ${elapsed.toFixed(2)} 秒` : ''}</p>}
        {task.status === 'retry_wait' && details.next_retry_at && <p className="text-amber">计划重试：<Timestamp value={details.next_retry_at} /></p>}
      </div>
    </div>
  </details>;
}
