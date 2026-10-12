import { useCallback, useEffect, useRef, useState } from 'react';
import { getRuntimeArtifact, getRuntimeRun, RuntimeApiError, runtimeArtifactContentUrl, runtimeEventsStream } from '@/api/client';
import type { ArtifactSummary, PendingConfirmation, RunProjectionState, RunSnapshot, RuntimeEvent } from '@/types/runtime';

export class EventSequenceGapError extends Error {
  constructor(public readonly expected: number, public readonly received: number) {
    super(`runtime event sequence gap: expected ${expected}, received ${received}`);
    this.name = 'EventSequenceGapError';
  }
}

export function projectionFromSnapshot(snapshot: RunSnapshot): RunProjectionState {
  return {
    ...snapshot,
    seen_event_ids: new Set<number>(),
    events: [],
    map_layers: {},
  };
}

const taskStatusByEvent: Readonly<Record<string, string>> = {
  'task.ready': 'ready',
  'task.leased': 'leased',
  'task.started': 'running',
  'task.validating': 'validating',
  'task.retry_wait': 'retry_wait',
  'task.awaiting_input': 'awaiting_input',
  'task.completed': 'succeeded',
  'task.failed': 'failed',
  'task.cancelled': 'cancelled',
};

/** Pure reducer: event IDs are deduplicated and seq gaps fail closed. */
export function reduceRuntimeEvent(state: RunProjectionState, event: RuntimeEvent): RunProjectionState {
  // The endpoint is scoped by run_id, but keep the reducer fail-closed when a
  // proxy/reconnect accidentally delivers a frame from another stream.
  if (event.run_id !== state.run_id) return state;
  if (state.seen_event_ids.has(event.event_id)) return state;
  // A snapshot already includes its cursor and all preceding events. A proxy
  // may replay that boundary frame even though this subscription never saw it.
  if (event.seq <= state.last_seq && event.event_id <= state.last_event_id) return state;
  if (event.seq !== state.last_seq + 1) throw new EventSequenceGapError(state.last_seq + 1, event.seq);
  const seen = new Set(state.seen_event_ids);
  seen.add(event.event_id);
  const payload = event.payload ?? {};
  let tasks = state.tasks;
  if (event.type === 'task.created' && typeof payload.task_id === 'string' && !state.tasks.some((task) => task.task_id === payload.task_id)) {
    tasks = [...state.tasks, {
      task_id: payload.task_id,
      task_key: typeof payload.task_key === 'string' ? payload.task_key : payload.task_id,
      status: typeof payload.initial_status === 'string' ? payload.initial_status : 'blocked',
      attempts: 0,
      error: null,
    }];
  }
  tasks = tasks.map((task) => {
    if (event.task_id !== task.task_id && payload.task_id !== task.task_id) return task;
    const status = typeof payload.status === 'string' ? payload.status : taskStatusByEvent[event.type] ?? task.status;
    return { ...task, status, error: typeof payload.message === 'string' ? payload.message : task.error };
  });
  const runStatusEvent = event.type.startsWith('run.');
  const status = runStatusEvent && typeof payload.status === 'string' && ['queued', 'planning', 'awaiting_input', 'ready', 'running', 'cancelling', 'completed', 'failed', 'cancelled'].includes(payload.status)
    ? payload.status as RunProjectionState['status']
    : event.type === 'run.awaiting_input' ? 'awaiting_input' : event.type === 'run.completed' ? 'completed' : event.type === 'run.failed' ? 'failed' : event.type === 'run.cancelled' ? 'cancelled' : state.status;
  const awaitingInput = event.type === 'run.awaiting_input'
    ? payload
    : runStatusEvent && status !== 'awaiting_input'
      ? null
      : state.awaiting_input;
  const artifacts = event.artifact_id && !state.output_artifacts.some((item) => item.artifact_id === event.artifact_id)
    ? [...state.output_artifacts, { artifact_id: event.artifact_id, type: String(payload.artifact_type ?? 'Artifact'), producer_task_id: event.task_id ?? null }]
    : state.output_artifacts;
  let confirmations = state.pending_confirmations ?? [];
  if (event.type === 'run.confirmation_required' && ['confirmation_id', 'task_id', 'task_key', 'action_hash'].every((key) => typeof payload[key] === 'string')) {
    const confirmation = { confirmation_id: payload.confirmation_id, task_id: payload.task_id, task_key: payload.task_key, action_hash: payload.action_hash } as PendingConfirmation;
    if (typeof payload.action_summary === 'string') confirmation.action_summary = payload.action_summary;
    confirmations = [...confirmations.filter((item) => item.confirmation_id !== confirmation.confirmation_id), confirmation];
  }
  if (event.type === 'run.confirmed') confirmations = confirmations.filter((item) => item.confirmation_id !== payload.confirmation_id);
  if (['completed', 'cancelled', 'failed'].includes(status)) confirmations = [];
  return {
    ...state,
    status,
    run_version: typeof payload.run_version === 'number' ? payload.run_version : typeof payload.version === 'number' ? payload.version : state.run_version,
    last_event_id: event.event_id,
    last_seq: event.seq,
    seen_event_ids: seen,
    events: [...state.events, event],
    tasks,
    output_artifacts: artifacts,
    awaiting_input: awaitingInput,
    patch_schema: (awaitingInput?.patch_schema as Record<string, unknown> | undefined) ?? null,
    diagnostic: (awaitingInput?.diagnostic as Record<string, unknown> | undefined) ?? null,
    pending_confirmations: confirmations,
  };
}

function decodeEvent(frame: string): RuntimeEvent | null {
  const data = frame.split(/\r?\n/).filter((line) => line.startsWith('data:')).map((line) => line.slice(5).replace(/^\s/, '')).join('\n');
  if (!data) return null;
  try {
    return JSON.parse(data) as RuntimeEvent;
  } catch {
    return null;
  }
}

async function readRuntimeStream(response: Response, onEvent: (event: RuntimeEvent) => void, signal: AbortSignal): Promise<void> {
  if (!response.body) throw new Error('runtime event stream has no body');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  const cancel = () => { void reader.cancel().catch(() => { /* transport already closed */ }); };
  signal.addEventListener('abort', cancel, { once: true });
  try {
    while (!signal.aborted) {
      const part = await reader.read();
      if (part.done) break;
      buffer += decoder.decode(part.value, { stream: true });
      const frames = buffer.split(/\r?\n\r?\n/);
      buffer = frames.pop() ?? '';
      for (const frame of frames) {
        const event = decodeEvent(frame);
        if (event) onEvent(event);
      }
    }
    // Only the blank-line delimiter commits an SSE frame. EOF can interrupt a
    // frame after valid JSON; retain the last complete cursor for replay.
  } finally {
    signal.removeEventListener('abort', cancel);
    try { await reader.cancel(); } catch { /* stream closed or aborted */ }
    try { reader.releaseLock(); } catch { /* noop */ }
  }
}

export type RuntimeConnectionState = 'idle' | 'connecting' | 'live' | 'reconnecting' | 'closed';

export function useRuntimeRun(runId: string | null) {
  const [projection, setProjection] = useState<RunProjectionState | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [transport, setTransport] = useState<{ runId: string | null; terminalSettled: boolean; connectionState: RuntimeConnectionState }>({
    runId: null, terminalSettled: false, connectionState: 'idle',
  });
  const abortRef = useRef<AbortController | null>(null);

  const stop = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setTransport((current) => ({ ...current, connectionState: current.runId ? 'closed' : 'idle' }));
  }, []);

  const connect = useCallback(async () => {
    stop();
    setError(null);
    setTransport({ runId, terminalSettled: false, connectionState: runId ? 'connecting' : 'idle' });
    if (!runId) { setProjection(null); return; }
    const abort = new AbortController();
    abortRef.current = abort;
    try {
      const snapshot = await getRuntimeRun(runId, abort.signal);
      if (abort.signal.aborted || abortRef.current !== abort) return;
      let current = projectionFromSnapshot(snapshot);
      let terminalObserved = ['completed', 'failed', 'cancelled'].includes(snapshot.status);
      setProjection(current);
      setTransport({ runId, terminalSettled: terminalObserved, connectionState: terminalObserved ? 'closed' : 'connecting' });
      while (!abort.signal.aborted && !terminalObserved) {
        try {
          const response = await runtimeEventsStream(runId, current.last_event_id, abort.signal);
          if (abort.signal.aborted || abortRef.current !== abort) break;
          setTransport({ runId, terminalSettled: false, connectionState: 'live' });
          await readRuntimeStream(response, (event) => {
            if (abort.signal.aborted || abortRef.current !== abort) return;
            const next = reduceRuntimeEvent(current, event);
            // A generic status projection precedes the named terminal event.
            // Only a terminal snapshot or that accepted final frame ends replay.
            if (next !== current && ['run.completed', 'run.failed', 'run.cancelled'].includes(event.type)) terminalObserved = true;
            current = next;
            setError(null);
            setProjection(current);
            if (terminalObserved) {
              setTransport({ runId, terminalSettled: true, connectionState: 'closed' });
              abort.abort();
            }
          }, abort.signal);
        } catch (streamError) {
          if (abort.signal.aborted || abortRef.current !== abort) break;
          setError(streamError as Error);
          setTransport({ runId, terminalSettled: false, connectionState: 'reconnecting' });
          if (streamError instanceof EventSequenceGapError || (streamError instanceof RuntimeApiError && streamError.code === 'INVALID_EVENT_CURSOR')) {
            // A deleted/late frame cannot be repaired by retrying with the old
            // cursor. Rebase on the authoritative snapshot, then tail again.
            const rebased = await getRuntimeRun(runId, abort.signal);
            if (abort.signal.aborted || abortRef.current !== abort) break;
            current = projectionFromSnapshot(rebased);
            terminalObserved = ['completed', 'failed', 'cancelled'].includes(rebased.status);
            setError(null);
            setProjection(current);
            setTransport({ runId, terminalSettled: terminalObserved, connectionState: terminalObserved ? 'closed' : 'reconnecting' });
            continue;
          }
        }
        if (!abort.signal.aborted && !terminalObserved) {
          setTransport({ runId, terminalSettled: false, connectionState: 'reconnecting' });
          await new Promise<void>((resolve) => window.setTimeout(resolve, 500));
        }
      }
    } catch (requestError) {
      if (!abort.signal.aborted && abortRef.current === abort) {
        setError(requestError as Error);
        setTransport({ runId, terminalSettled: false, connectionState: 'closed' });
      }
    }
  }, [runId, stop]);

  useEffect(() => {
    void connect();
    return stop;
  }, [connect, stop]);

  const artifactUrl = useCallback((artifact: Pick<ArtifactSummary, 'artifact_id'>) => runtimeArtifactContentUrl(artifact), []);
  const loadArtifact = useCallback((artifactId: string, signal?: AbortSignal) => getRuntimeArtifact(artifactId, signal), []);
  return {
    projection: projection?.run_id === runId ? projection : null,
    terminalSettled: transport.runId === runId && !!runId && transport.terminalSettled,
    connectionState: transport.runId === runId ? transport.connectionState : runId ? 'connecting' as const : 'idle' as const,
    error, connect, stop, artifactUrl, loadArtifact,
  };
}
