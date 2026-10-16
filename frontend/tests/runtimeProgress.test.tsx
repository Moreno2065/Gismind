import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { RunStatusBar } from '@/components/RunStatusBar';
import TraceTimeline from '@/components/TraceTimeline';
import { projectionFromSnapshot, reduceRuntimeEvent } from '@/hooks/useRuntimeRun';
import type { RuntimeEvent, RunSnapshot } from '@/types/runtime';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const snapshot: RunSnapshot = {
  run_id: 'progress-run', session_id: 's', status: 'planning', run_version: 1,
  tasks: [], output_artifacts: [], last_event_id: 0, last_seq: 0,
};
const event = (seq: number, type: string, payload: Record<string, unknown>, task_id?: string): RuntimeEvent => ({
  event_id: seq, seq, run_id: snapshot.run_id, type, payload, task_id,
});

describe('Durable runtime progress', () => {
  const details = {
    parameters: { distance_m: 500 },
    inputs: [{ name: 'vector', ordinal: 0, source_task_id: 'read', source_task_key: '读取数据', source_port: 'vector', source_ordinal: 0, selection: 'one' as const }],
    resolved_inputs: [{ name: 'vector', ordinal: 0, artifact_id: 'input-artifact' }],
    dependencies: [{ task_id: 'read', task_key: '读取数据' }],
    outputs: [{ name: 'vector', ordinal: 0, artifact_id: 'result/artifact' }],
    latest_attempt: { attempt_no: 2, status: 'failed', started_at: '2026-10-13T01:00:00Z', finished_at: '2026-10-13T01:00:02Z' },
    max_attempts: 3, next_retry_at: null,
  };

  it('explains persisted inputs, parameters, dependencies, outputs and attempt timing in a compact disclosure', () => {
    const task = { task_id: 'buffer', task_key: '缓冲区', op: 'vector.buffer', status: 'succeeded', attempts: 2, details };
    render(<RunStatusBar projection={projectionFromSnapshot({ ...snapshot, tasks: [task] })} connectionState="live" terminalSettled={false} />);
    const disclosure = screen.getByText('查看执行详情').closest('details');
    expect(disclosure?.open).toBe(false);
    fireEvent.click(screen.getByText('查看执行详情'));
    expect(screen.getByText('distance_m')).toBeTruthy();
    expect(screen.getByText('500')).toBeTruthy();
    expect(screen.getByText('读取数据.vector[0]')).toBeTruthy();
    expect(screen.getByRole('link', { name: /result\/artifact/ }).getAttribute('href')).toBe('/api/artifacts/result%2Fartifact/content');
    expect(screen.getByText(/第 2 次执行/)).toBeTruthy();
    expect(screen.getByText(/2.00 秒/)).toBeTruthy();
  });

  it('preserves detailed facts when task status events merge and accepts task-created details', () => {
    let state = reduceRuntimeEvent(projectionFromSnapshot(snapshot), event(1, 'task.created', { task_id: 't', task_key: '缓冲区', details }));
    state = reduceRuntimeEvent(state, event(2, 'task.started', { task_id: 't' }));
    expect(state.tasks[0].details).toEqual(details);
  });

  it('keeps a diagnostic path visible even when its error also appears in a task and copies exact context', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    render(<RunStatusBar projection={projectionFromSnapshot({ ...snapshot, status: 'failed',
      tasks: [{ task_id: 't', task_key: '导出', status: 'failed', attempts: 1, error_code: 'POSTCONDITION_FAILED', error: '未生成有效成果' }],
      diagnostic: { code: 'POSTCONDITION_FAILED', message: '未生成有效成果', path: 'tasks.export.outputs.file' },
    })} connectionState="closed" terminalSettled />);
    expect(screen.getByText('tasks.export.outputs.file')).toBeTruthy();
    expect(screen.getByText(/修改请求后重新提交/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '复制诊断' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledOnce());
    const copied = JSON.parse(writeText.mock.calls[0][0]);
    expect(copied).toMatchObject({ run_id: snapshot.run_id, diagnostic: { path: 'tasks.export.outputs.file' } });
    expect(screen.queryByRole('button', { name: /恢复执行|重新执行/ })).toBeNull();
  });

  it('shows a real planning repair request and restores it without event history', () => {
    let state = projectionFromSnapshot(snapshot);
    state = reduceRuntimeEvent(state, event(1, 'planner.requested', { attempt_id: 'initial', attempt_no: 1, phase: 'initial' }));
    state = reduceRuntimeEvent(state, event(2, 'planner.failed', { attempt_id: 'initial', status: 'failed', error_code: 'LLM_DRAFT_INVALID', message: '计划缺少任务' }));
    state = reduceRuntimeEvent(state, event(3, 'planner.requested', { attempt_id: 'repair', attempt_no: 2, phase: 'repair' }));
    expect(state.planning_attempts).toEqual([
      expect.objectContaining({ attempt_id: 'initial', phase: 'initial', status: 'failed', error_code: 'LLM_DRAFT_INVALID', error: '计划缺少任务' }),
      expect.objectContaining({ attempt_id: 'repair', phase: 'repair', status: 'requested' }),
    ]);
    const restored = projectionFromSnapshot({ ...state });
    render(<RunStatusBar projection={restored} connectionState="live" terminalSettled={false} />);
    expect(screen.getByTestId('runtime-stage').textContent).toBe('正在修复计划');
    expect(screen.getByTestId('runtime-progress').getAttribute('data-run-id')).toBe(snapshot.run_id);
    expect(screen.getByText('LLM_DRAFT_INVALID')).toBeTruthy();
    expect(screen.queryByRole('progressbar')).toBeNull();
    expect(screen.getByText('计划缺少任务')).toBeTruthy();
  });

  it('renders every planned step with operation, factual state, and exact failure after refresh', () => {
    const restored = projectionFromSnapshot({ ...snapshot, status: 'failed', tasks: [
      { task_id: 'read', task_key: '读取上传', op: 'vector.read', status: 'succeeded', attempts: 1 },
      { task_id: 'buffer', task_key: '缓冲区', op: 'vector.buffer', status: 'failed', attempts: 2, error_code: 'INVALID_CRS', error: '图层缺少坐标参考系' },
      { task_id: 'export', task_key: '导出文件', op: 'file.export', status: 'cancelled', attempts: 0 },
    ] });
    const { container } = render(<RunStatusBar projection={restored} connectionState="closed" terminalSettled />);
    const rows = container.querySelectorAll('[data-task-id]');
    expect(rows).toHaveLength(3);
    expect(rows[0].getAttribute('data-task-op')).toBe('vector.read');
    expect(rows[1].getAttribute('data-task-status')).toBe('failed');
    expect(within(rows[1] as HTMLElement).getByText('INVALID_CRS')).toBeTruthy();
    expect(within(rows[1] as HTMLElement).getByText('图层缺少坐标参考系')).toBeTruthy();
    expect(screen.getByText('file.export')).toBeTruthy();
    expect(screen.getByText('已取消')).toBeTruthy();
    expect(screen.getByTestId('runtime-progress-list').className).toContain('overflow-y-auto');
    expect(screen.getByTestId('runtime-progress-list').className).toContain('24vh');
  });

  it('preserves task metadata from SSE and does not treat forbidden empty results as success', () => {
    let state = projectionFromSnapshot({ ...snapshot, status: 'running' });
    state = reduceRuntimeEvent(state, event(1, 'task.created', { task_id: 't', task_key: '导出', op: 'file.export', initial_status: 'ready' }));
    state = reduceRuntimeEvent(state, event(2, 'task.empty', { allowed: false, error_code: 'EMPTY_RESULT', message: '未生成文件' }, 't'));
    expect(state.tasks[0]).toMatchObject({ op: 'file.export', status: 'failed', error_code: 'EMPTY_RESULT', error: '未生成文件' });
    render(<TraceTimeline events={state.events} />);
    expect(screen.getByText('失败')).toBeTruthy();
    expect(screen.queryByText('完成')).toBeNull();
  });

  it('shows a planning rejection even when no executable task exists', () => {
    render(<RunStatusBar projection={projectionFromSnapshot({ ...snapshot, status: 'failed',
      diagnostic: { code: 'PLAN_GOAL_UNCOVERED', message: '计划遗漏导出步骤' },
    })} connectionState="closed" terminalSettled />);
    expect(screen.getByText('PLAN_GOAL_UNCOVERED')).toBeTruthy();
    expect(screen.getByText('计划遗漏导出步骤')).toBeTruthy();
  });

  it('clears earlier generation planning attempts when a new planning lease starts', () => {
    let state = reduceRuntimeEvent(projectionFromSnapshot({ ...snapshot, status: 'queued' }), event(1, 'planner.requested', { attempt_id: 'old', phase: 'initial' }));
    state = reduceRuntimeEvent(state, event(2, 'run.planning_claimed', { generation: 2 }));
    expect(state.planning_attempts).toEqual([]);
    expect(state.status).toBe('planning');
  });

  it('replaces old plan steps and deliveries when a resumed run activates a new plan', () => {
    let state = projectionFromSnapshot({ ...snapshot,
      active_plan: { plan_id: 'old-plan', revision: 1 },
      tasks: [{ task_id: 'old-task', task_key: '旧步骤', op: 'vector.read', status: 'succeeded', attempts: 1 }],
      output_artifacts: [{ artifact_id: 'old-output', type: 'Vector' }], final_artifact_ids: ['old-output'],
    });
    state = reduceRuntimeEvent(state, event(1, 'plan.created', { plan_id: 'new-plan', plan_version: 2, task_count: 1 }));
    state = reduceRuntimeEvent(state, event(2, 'task.created', { task_id: 'new-task', task_key: '重新导出', op: 'file.export' }));
    expect(state.tasks.map((task) => task.task_id)).toEqual(['new-task']);
    expect(state.active_plan).toMatchObject({ plan_id: 'new-plan', revision: 2 });
    expect(state.output_artifacts).toEqual([]);
    expect(state.final_artifact_ids).toEqual([]);
  });

  it('keeps dependency display order identical for streamed tasks and restored snapshots', () => {
    let state = projectionFromSnapshot(snapshot);
    const orders = [['export', 2], ['buffer', 1], ['load', 0], ['map', 3]] as const;
    orders.forEach(([key, display_order], index) => {
      state = reduceRuntimeEvent(state, event(index + 1, 'task.created', { task_id: key, task_key: key, display_order }));
    });
    expect(state.tasks.map((task) => task.task_key)).toEqual(['load', 'buffer', 'export', 'map']);
    const restored = projectionFromSnapshot({ ...snapshot, tasks: [...state.tasks].reverse() });
    expect(restored.tasks).toEqual(state.tasks);
    const { container } = render(<RunStatusBar projection={restored} connectionState="live" terminalSettled={false} />);
    expect([...container.querySelectorAll('[data-task-id]')].map((row) => row.getAttribute('data-task-id'))).toEqual(['load', 'buffer', 'export', 'map']);
  });
});
