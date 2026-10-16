import { describe, expect, it } from 'vitest';
import { projectionFromSnapshot, reduceRuntimeEvent } from '@/hooks/useRuntimeRun';
import type { RunSnapshot } from '@/types/runtime';

const snapshot: RunSnapshot = { run_id: 'r', session_id: 's', status: 'planning', run_version: 1,
  tasks: [], output_artifacts: [], final_artifact_ids: [], last_event_id: 0, last_seq: 0 };

describe('terminal planning diagnostic', () => {
  it('retains the exact rejection through status change and terminal events', () => {
    const diagnostic = { code: 'LLM_DRAFT_INVALID', message: '模型未返回有效任务计划，请重新提交。' };
    let state = projectionFromSnapshot(snapshot);
    state = reduceRuntimeEvent(state, { run_id: 'r', seq: 1, event_id: 1, type: 'plan.rejected', payload: { code: diagnostic.code, diagnostic } });
    state = reduceRuntimeEvent(state, { run_id: 'r', seq: 2, event_id: 2, type: 'run.status_changed', payload: { status: 'failed' } });
    state = reduceRuntimeEvent(state, { run_id: 'r', seq: 3, event_id: 3, type: 'run.failed', payload: { status: 'failed' } });
    expect(state.status).toBe('failed');
    expect(state.diagnostic).toEqual(diagnostic);
  });

  it('accepts a terminal diagnostic directly and clears it when a new run attempt queues', () => {
    const diagnostic = { code: 'RUN_OUTPUTS_MISSING', message: '任务未产生可交付成果。' };
    let state = reduceRuntimeEvent(projectionFromSnapshot(snapshot), { run_id: 'r', seq: 1, event_id: 1,
      type: 'run.failed', payload: { status: 'failed', diagnostic } });
    expect(state.diagnostic).toEqual(diagnostic);
    state = reduceRuntimeEvent(state, { run_id: 'r', seq: 2, event_id: 2,
      type: 'run.status_changed', payload: { status: 'queued' } });
    expect(state.diagnostic).toBeNull();
  });
});
