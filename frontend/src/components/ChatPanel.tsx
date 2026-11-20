// Runtime chat projection. The durable Run/Event ledger is authoritative.
import { forwardRef, memo, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from 'react';
import {
  cancelRuntimeRun,
  confirmRuntimeRun,
  createRuntimeRun,
  getRuntimeArtifact,
  getRuntimeArtifactContent,
  getRuntimeRun,
  getSessionMessages,
  getSessionRuns,
  resumeRuntimeRun,
  RuntimeApiError,
  runtimeArtifactContentUrl,
  uploadRuntimeArtifact,
  type UploadDeclaration,
} from '@/api/client';
import { projectionFromSnapshot, useRuntimeRun } from '@/hooks/useRuntimeRun';
import { useScrollAnchor } from '@/hooks/useScrollAnchor';
import { countFeatures } from '@/lib/utils';
import { projectRuntimeMapLayer } from '@/lib/runtimeMapProjection';
import { mergeMessageHistory } from '@/lib/mergeMessageHistory';
import { runtimeWorkflowExamples } from '@/lib/runtimeWorkflowExamples';
import type { ChatMessage, MessageBlock } from '@/types/message';
import type { ArtifactSummary, CreateRunRequest, PendingConfirmation, RunCommandResponse, RunProjectionState } from '@/types/runtime';
import { MessageBubble } from './MessageBubble';
import { hasStructuredFields, RuntimeInputForm } from './RuntimeInputForm';
import { UploadTray, type PendingUpload } from './UploadTray';
import { ArtifactWorkspace } from './ArtifactWorkspace';
import { RunStatusBar } from './RunStatusBar';

interface ActiveRun { runId: string; messageId: string }
interface PendingCreate { request: CreateRunRequest; messageId: string }
interface PendingCommand { key: string; commandId: string }
interface ChatPanelProps { sessionId: string; onSessionUpdate?: () => void }
interface RuntimeComposerHandle {
  setDraft: (text: string) => void;
  clearDraft: () => void;
  openFilePicker: () => void;
}

const terminalRunStatuses = new Set(['completed', 'failed', 'cancelled']);

// A session owns its requests, message bindings and local projection lifecycle.
export function ChatPanel(props: ChatPanelProps) { return <RuntimeChatPanel key={props.sessionId} {...props} />; }

function RuntimeChatPanel({ sessionId, onSessionUpdate }: ChatPanelProps) {
  const [activeRun, setActiveRun] = useState<ActiveRun | null>(() => loadActiveRun(sessionId));
  const activeRunRef = useRef(activeRun);
  const [messages, setMessages] = useState<ChatMessage[]>(() => {
    const cached = loadMessages(sessionId); const active = loadActiveRun(sessionId);
    return active ? ensureAssistant(cached, active) : cached;
  });
  const [recovering, setRecovering] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [commandError, setCommandError] = useState<string | null>(null);
  const [pendingUploads, setPendingUploads] = useState<PendingUpload[]>([]);
  const [artifacts, setArtifacts] = useState<ArtifactSummary[]>([]);
  const [selectedInputs, setSelectedInputs] = useState<ArtifactSummary[]>([]);
  const [lastProjection, setLastProjection] = useState<RunProjectionState | null>(null);
  const [mobilePane, setMobilePane] = useState<'chat' | 'data'>('chat');
  const [recoveryError, setRecoveryError] = useState<string | null>(null);
  const [recoveryRevision, setRecoveryRevision] = useState(0);
  const uploadsBlocked = pendingUploads.some((upload) => upload.status !== 'ready');
  const runtime = useRuntimeRun(activeRun?.runId ?? null);
  const projection = runtime.projection?.session_id === sessionId ? runtime.projection : null;
  const awaitingInput = projection?.status === 'awaiting_input' ? projection.awaiting_input : null;
  const cancellationDecision = awaitingInput?.kind === 'cancellation_decision_required';
  const schema = projection?.patch_schema ?? null;
  const structuredInput = !!awaitingInput && !cancellationDecision && hasStructuredFields(schema);
  const confirmations = projection?.pending_confirmations ?? [];
  const runBusy = !!activeRun && (!projection || projection.status !== 'awaiting_input');
  const isRunning = recovering || submitting || runBusy;
  const scroll = useScrollAnchor<HTMLDivElement>();
  const requestRef = useRef<AbortController | null>(null);
  const pendingCreateRef = useRef<PendingCreate | null>(null);
  const pendingCommandRef = useRef<PendingCommand | null>(null);
  const lifecycleRef = useRef<AbortController | null>(null);
  const busyRef = useRef(false);
  const artifactIdsRef = useRef(new Set<string>());
  const settlingRef = useRef(new Set<string>());
  const composerRef = useRef<RuntimeComposerHandle | null>(null);

  const updateAssistant = useCallback((messageId: string, update: (message: ChatMessage) => ChatMessage) => {
    setMessages((current) => current.map((message) => message.id === messageId ? update(message) : message));
  }, []);

  const clearActiveRunBinding = useCallback((expectedRunId?: string) => {
    if (expectedRunId && activeRunRef.current?.runId !== expectedRunId) return false;
    activeRunRef.current = null;
    pendingCommandRef.current = null;
    saveActiveRun(sessionId, null);
    setActiveRun(null);
    return true;
  }, [sessionId]);

  const attachRun = useCallback((binding: ActiveRun) => {
    activeRunRef.current = binding;
    pendingCommandRef.current = null;
    saveActiveRun(sessionId, binding);
    setMessages((current) => ensureAssistant(current, binding));
    setActiveRun(binding);
  }, [sessionId]);

  const hydrateMapArtifact = useCallback((
    runId: string,
    messageId: string,
    artifact: ArtifactSummary,
    signal: AbortSignal | undefined,
    fallbackExpiredOnError = false,
  ) => {
    const key = runId + ':' + artifact.artifact_id;
    if (artifactIdsRef.current.has(key)) return;
    artifactIdsRef.current.add(key);
    const appendBlock = (block: MessageBlock) => {
      if (signal?.aborted) return;
      updateAssistant(messageId, (message) => ({
        ...message,
        blocks: [
          ...message.blocks.filter((existing) => !('artifact_id' in existing) || existing.artifact_id !== ('artifact_id' in block ? block.artifact_id : undefined)),
          block,
        ],
      }));
    };
    void (async () => {
      const metadata = artifact.crs ? artifact : await getRuntimeArtifact(artifact.artifact_id, signal);
      if (metadata.artifact_id !== artifact.artifact_id || metadata.type !== 'MapLayer') {
        throw new Error('地图成果标识或类型与服务端响应不一致');
      }
      const content = await getRuntimeArtifactContent(artifact.artifact_id, signal);
      const map = projectRuntimeMapLayer(content, metadata.crs);
      appendBlock({ type: 'map', artifact_id: artifact.artifact_id, layers: map.layers, bbox: map.bbox });
      if (!signal?.aborted) setCommandError((current) => current?.startsWith('地图加载失败：') ? null : current);
    })().catch((error) => {
      artifactIdsRef.current.delete(key);
      if (signal?.aborted) return;
      if (fallbackExpiredOnError) {
        appendBlock({ type: 'map', artifact_id: artifact.artifact_id, layers: [], bbox: [0, 0, 0, 0], expired: true });
      }
      setCommandError('地图加载失败：' + (error as Error).message);
    });
  }, [updateAssistant]);

  useEffect(() => {
    const controller = new AbortController(); lifecycleRef.current = controller;
    return () => { controller.abort(); requestRef.current?.abort(); };
  }, [sessionId]);

  // Unlock stale localStorage activeRun if the backend confirms 404 RUN_NOT_FOUND
  // or if the fetched run belongs to a different session.
  useEffect(() => {
    if (!activeRun) return;
    const staleMissing = runtime.error instanceof RuntimeApiError
      && (runtime.error.status === 404 || runtime.error.code === 'RUN_NOT_FOUND');
    const wrongSession = !!runtime.projection
      && runtime.projection.run_id === activeRun.runId
      && runtime.projection.session_id !== sessionId;
    if (!staleMissing && !wrongSession) return;
    const { runId, messageId } = activeRun;
    if (!clearActiveRunBinding(runId)) return;
    const notice = staleMissing
      ? '上次运行记录已不存在，已恢复输入。'
      : '本地缓存的运行记录与当前会话不匹配，已恢复输入。';
    setCommandError(notice);
    updateAssistant(messageId, (message) => {
      if (message.status === 'done' || message.status === 'error') return message;
      return {
        ...message,
        status: 'error',
        error: { code: staleMissing ? runtime.error instanceof RuntimeApiError ? runtime.error.code : 'RUN_NOT_FOUND' : 'SESSION_MISMATCH', message: notice },
      };
    });
  }, [activeRun, clearActiveRunBinding, runtime.error, runtime.projection, sessionId, updateAssistant]);

  useEffect(() => {
    const controller = new AbortController();
    setRecovering(true); setRecoveryError(null);
    void Promise.allSettled([getSessionMessages(sessionId, controller.signal), getSessionRuns(sessionId, controller.signal)]).then(async ([history, runs]) => {
      if (controller.signal.aborted) return;
      if (history.status === 'fulfilled') {
        const records: ChatMessage[] = history.value.flatMap((record, index) => record.role !== 'user' && record.role !== 'assistant' ? [] : [{
          id: record.id ?? 'history_' + sessionId + '_' + index, role: record.role,
          blocks: record.content ? [{ type: 'text' as const, content: record.content }] : [], status: 'done' as const,
          created_at: record.created_at ? Date.parse(record.created_at) : Date.now(),
        }]);
        setMessages((current) => mergeMessageHistory(records, current));
      }
      if (runs.status === 'fulfilled') {
        const active = runs.value.find((run) => run.session_id === sessionId && !terminalRunStatuses.has(run.status));
        const currentBinding = activeRunRef.current;
        const confirmedTerminalActive = !active && currentBinding
          ? runs.value.find((run) => run.session_id === sessionId && run.run_id === currentBinding.runId && terminalRunStatuses.has(run.status))
          : undefined;
        if (active) {
          const saved = loadActiveRun(sessionId);
          attachRun(saved?.runId === active.run_id ? saved : { runId: active.run_id, messageId: 'assistant_' + active.run_id });
        }
        // Snapshot reads restore actual outputs, independently of the text cache.
        // Bound I/O concurrency so a long session cannot flood the local API.
        const completed = runs.value.filter((run) => run.session_id === sessionId && terminalRunStatuses.has(run.status));
        let index = 0;
        await Promise.all(Array.from({ length: Math.min(4, completed.length) }, async () => {
          while (index < completed.length && !controller.signal.aborted) {
            const item = completed[index++];
            try {
              const snapshot = await getRuntimeRun(item.run_id, controller.signal);
              if (controller.signal.aborted || snapshot.session_id !== sessionId || snapshot.run_id !== item.run_id) continue;
              setArtifacts((current) => mergeArtifacts(current, snapshot.output_artifacts));
              if (!active && item.run_id === completed[0]?.run_id) setLastProjection(projectionFromSnapshot(snapshot));
            } catch (error) {
              if (!controller.signal.aborted) setRecoveryError('部分历史成果读取失败：' + (error as Error).message);
            }
          }
        }));
        // If server run listing confirmed the cached activeRun is already terminal
        // and no newer active run replaced it, release the composer lock.
        if (!controller.signal.aborted && confirmedTerminalActive && currentBinding
            && activeRunRef.current?.runId === confirmedTerminalActive.run_id
            && !settlingRef.current.has(confirmedTerminalActive.run_id)) {
          clearActiveRunBinding(confirmedTerminalActive.run_id);
          updateAssistant(currentBinding.messageId, (message) => {
            if (message.status === 'done' || message.status === 'error') return message;
            if (confirmedTerminalActive.status === 'completed') return { ...message, status: 'done' };
            return {
              ...message,
              status: 'error',
              error: confirmedTerminalActive.status === 'cancelled'
                ? { code: 'RUN_CANCELLED', message: '任务已取消' }
                : { code: 'RUN_FAILED', message: '任务执行失败' },
            };
          });
        }
      }
      if (!controller.signal.aborted) {
        if (history.status === 'rejected' || runs.status === 'rejected') setRecoveryError('会话恢复未完成，请重试；本地缓存不是完整运行记录。');
        setRecovering(false);
      }
    });
    return () => controller.abort();
  }, [attachRun, clearActiveRunBinding, sessionId, recoveryRevision, updateAssistant]);

  useEffect(() => {
    const timeout = window.setTimeout(() => {
      const stored = messages.slice(-50).map((message) => ({ ...message, blocks: message.blocks.map((block) => block.type === 'map'
        ? { ...block, layers: [], expired: true, featureCount: block.expired ? block.featureCount : countFeatures(block.layers) } : block) }));
      try { localStorage.setItem(messagesKey(sessionId), JSON.stringify(stored)); } catch { /* presentation cache */ }
    }, 300);
    return () => window.clearTimeout(timeout);
  }, [messages, sessionId]);

  // Auto-stick scroll to bottom when new messages, blocks, or trace events arrive
  const lastMessage = messages.at(-1);
  const scrollSignal = `${messages.length}:${lastMessage?.id ?? ''}:${lastMessage?.status ?? ''}:${lastMessage?.blocks.length ?? 0}:${lastMessage?.executionTrace?.length ?? 0}`;
  useEffect(() => {
    if (messages.length === 0) return;
    const raf = window.requestAnimationFrame(() => scroll.maybeStick());
    return () => window.cancelAnimationFrame(raf);
  }, [scrollSignal, messages.length, scroll.maybeStick]);

  useEffect(() => {
    if (!activeRun || !projection || projection.run_id !== activeRun.runId) return;
    const { messageId, runId } = activeRun;
    setArtifacts((current) => mergeArtifacts(current, projection.output_artifacts));
    setLastProjection(projection);
    const statusMap: Record<string, ChatMessage['status']> = { queued: 'thinking', planning: 'thinking', ready: 'thinking', awaiting_input: 'fetching', running: 'fetching', cancelling: 'fetching', completed: 'done', failed: 'error', cancelled: 'error' };
    updateAssistant(messageId, (message) => ({ ...message, status: statusMap[projection.status] ?? 'thinking',
      statusMessage: projection.status === 'awaiting_input' ? String(projection.awaiting_input?.message ?? '需要补充输入') : 'Run ' + projection.status,
      error: projection.status === 'failed'
        ? { code: String(projection.diagnostic?.code ?? 'RUN_FAILED'), message: String(projection.diagnostic?.message ?? '任务执行失败') }
        : projection.status === 'cancelled'
          ? { code: String(projection.diagnostic?.code ?? 'RUN_CANCELLED'), message: String(projection.diagnostic?.message ?? '任务已取消') }
          : undefined,
      executionTrace: projection.events.length ? projection.events : message.executionTrace,
    }));
    const signal = lifecycleRef.current?.signal;
    const appendBlock = (block: MessageBlock) => {
      if (signal?.aborted) return;
      updateAssistant(messageId, (message) => ({ ...message, blocks: [...message.blocks.filter((existing) => !('artifact_id' in existing) || existing.artifact_id !== ('artifact_id' in block ? block.artifact_id : undefined)), block] }));
    };
    for (const artifact of projection.output_artifacts) {
      if (artifact.type === 'MapLayer') {
        hydrateMapArtifact(runId, messageId, artifact, signal, false);
      } else {
        const key = runId + ':' + artifact.artifact_id;
        if (artifactIdsRef.current.has(key)) continue;
        artifactIdsRef.current.add(key);
        appendBlock({ type: 'text', artifact_id: artifact.artifact_id, content: '[下载 ' + artifact.type + ' Artifact](' + runtimeArtifactContentUrl(artifact) + ')' });
      }
    }
    if (terminalRunStatuses.has(projection.status) && runtime.terminalSettled) {
      if (settlingRef.current.has(runId)) return;
      settlingRef.current.add(runId);
      const isCurrentRun = () => activeRunRef.current?.runId === runId && activeRunRef.current?.messageId === messageId;
      void getRuntimeRun(runId, signal).then((snapshot) => {
        if (signal?.aborted || snapshot.session_id !== sessionId || snapshot.run_id !== runId) return;
        if (!terminalRunStatuses.has(snapshot.status)) { settlingRef.current.delete(runId); if (isCurrentRun()) void runtime.connect(); return; }
        setArtifacts((current) => mergeArtifacts(current, snapshot.output_artifacts));
        if (isCurrentRun()) setLastProjection(projectionFromSnapshot(snapshot));
        const nonMapOutputs = snapshot.output_artifacts.filter((artifact) => artifact.type !== 'MapLayer');
        if (nonMapOutputs.length > 0) {
          updateAssistant(messageId, (message) => ({ ...message, blocks: [
            ...message.blocks,
            ...nonMapOutputs
              .filter((artifact) => !message.blocks.some((block) => 'artifact_id' in block && block.artifact_id === artifact.artifact_id))
              .map((artifact): MessageBlock => ({ type: 'text', artifact_id: artifact.artifact_id, content: '[下载 ' + artifact.type + ' Artifact](' + runtimeArtifactContentUrl(artifact) + ')' })),
          ] }));
        }
        // Hydrate any MapLayer first discovered at terminal settlement using
        // the session lifecycle signal so clearing activeRun does not strand it.
        for (const artifact of snapshot.output_artifacts) {
          if (artifact.type === 'MapLayer') {
            hydrateMapArtifact(runId, messageId, artifact, signal, true);
          }
        }
        // Session recovery may have attached another Run while this read waited.
        // Preserve this Run's results without detaching that newer binding.
        if (isCurrentRun()) {
          clearActiveRunBinding(runId);
          onSessionUpdate?.();
        }
      }).catch((error) => {
        settlingRef.current.delete(runId);
        if (!signal?.aborted && isCurrentRun()) setCommandError('最终结果同步失败，请重新连接：' + (error as Error).message);
      });
    }
  }, [activeRun, clearActiveRunBinding, hydrateMapArtifact, onSessionUpdate, projection, runtime.terminalSettled, runtime.connect, sessionId, updateAssistant]);

  const commandIdFor = useCallback((key: string): string => {
    if (pendingCommandRef.current?.key === key) return pendingCommandRef.current.commandId;
    const commandId = 'cmd_' + crypto.randomUUID();
    pendingCommandRef.current = { key, commandId };
    return commandId;
  }, []);

  const runCommand = useCallback(async (action: (signal: AbortSignal) => Promise<RunCommandResponse>, onAccepted?: () => void) => {
    if (busyRef.current) return;
    busyRef.current = true; setSubmitting(true); setCommandError(null);
    const controller = new AbortController(); requestRef.current?.abort(); requestRef.current = controller;
    try {
      await action(controller.signal);
      if (!controller.signal.aborted) {
        pendingCommandRef.current = null;
        onAccepted?.();
        void runtime.connect();
      }
    } catch (error) {
      if (!controller.signal.aborted) { setCommandError((error as Error).message); void runtime.connect(); }
    } finally {
      if (!controller.signal.aborted) { busyRef.current = false; setSubmitting(false); }
    }
  }, [runtime.connect]);

  const resume = useCallback((patch: Record<string, unknown>) => {
    if (!activeRun || !projection || projection.status !== 'awaiting_input') return;
    const runId = activeRun.runId;
    const commandId = commandIdFor(JSON.stringify({ kind: 'resume', runId, runVersion: projection.run_version, patch }));
    void runCommand((signal) => resumeRuntimeRun(runId, commandId, patch, projection.run_version, signal), () => {
      composerRef.current?.clearDraft();
      setMessages((current) => [...current, createUserMessage(typeof patch.answer === 'string' ? patch.answer : typeof patch.clarification === 'string' ? patch.clarification : JSON.stringify(patch))]);
    });
  }, [activeRun, commandIdFor, projection, runCommand]);

  const send = useCallback(async (rawText: string) => {
    const text = rawText.trim();
    if (!text || isRunning || structuredInput || uploadsBlocked || busyRef.current) return;
    if (awaitingInput) {
      const taskIds = Array.isArray(awaitingInput.task_ids) ? awaitingInput.task_ids.filter((item): item is string => typeof item === 'string') : [];
      const retry = cancellationDecision && /^(retry|重试)$/i.test(text);
      resume(retry ? { answer: text, task_decisions: Object.fromEntries(taskIds.map((id) => [id, 'retry'])) } : { answer: text });
      return;
    }
    busyRef.current = true; setSubmitting(true); setCommandError(null);
    const controller = new AbortController(); requestRef.current?.abort(); requestRef.current = controller;
    const inputArtifactIds = [...new Set([...pendingUploads.filter((upload) => upload.status === 'ready' && upload.artifactId).map((upload) => upload.artifactId as string), ...selectedInputs.map((artifact) => artifact.artifact_id)])];
    const submittedFiles = new Set(pendingUploads.map((upload) => upload.file));
    const previous = pendingCreateRef.current;
    // A lost response does not mean creation failed. Retry the identical
    // payload with its original key; changed input is an intentional new Run.
    const retrying = previous?.request.session_id === sessionId && previous.request.message === text
      && JSON.stringify(previous.request.input_artifact_ids ?? []) === JSON.stringify(inputArtifactIds);
    const createRequest: CreateRunRequest = retrying
      ? previous.request
      : { session_id: sessionId, message: text, input_artifact_ids: inputArtifactIds, client_request_id: 'client_' + crypto.randomUUID() };
    const messageId = retrying ? previous.messageId : 'msg_' + crypto.randomUUID();
    const pendingCreate = { request: createRequest, messageId };
    pendingCreateRef.current = pendingCreate;
    if (retrying) {
      // One idempotent creation owns one local turn, including transport retries.
      updateAssistant(messageId, (message) => ({ ...message, status: 'thinking', statusMessage: '提交 Runtime…', error: undefined }));
    } else {
      setMessages((current) => [...current, createUserMessage(text), { id: messageId, role: 'assistant', blocks: [], status: 'thinking', statusMessage: '提交 Runtime…', executionTrace: [], created_at: Date.now() }]);
    }
    window.requestAnimationFrame(() => scroll.forceBottom());
    try {
      const created = await createRuntimeRun(createRequest, controller.signal);
      if (!controller.signal.aborted) {
        if (pendingCreateRef.current === pendingCreate) pendingCreateRef.current = null;
        composerRef.current?.clearDraft();
        setSelectedInputs([]);
        setPendingUploads((current) => current.filter((upload) => !submittedFiles.has(upload.file)));
        attachRun({ runId: created.run_id, messageId });
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        updateAssistant(messageId, (message) => ({ ...message, status: 'error', error: { code: 'RUN_CREATE_FAILED', message: (error as Error).message } }));
      }
    } finally { if (!controller.signal.aborted) { busyRef.current = false; setSubmitting(false); } }
  }, [isRunning, structuredInput, uploadsBlocked, awaitingInput, cancellationDecision, resume, pendingUploads, selectedInputs, scroll, sessionId, attachRun, updateAssistant]);

  const stop = useCallback(() => {
    if (!activeRun || !projection) return;
    const commandId = commandIdFor(JSON.stringify({ kind: 'cancel', runId: activeRun.runId, runVersion: projection.run_version }));
    void runCommand((signal) => cancelRuntimeRun(activeRun.runId, commandId, projection.run_version, signal));
  }, [activeRun, commandIdFor, projection, runCommand]);

  const confirm = useCallback((confirmation: PendingConfirmation) => {
    if (!activeRun || !projection) return;
    const commandId = commandIdFor(JSON.stringify({
      kind: 'confirm',
      runId: activeRun.runId,
      runVersion: projection.run_version,
      confirmationId: confirmation.confirmation_id,
      taskId: confirmation.task_id,
      actionHash: confirmation.action_hash,
    }));
    void runCommand((signal) => confirmRuntimeRun(activeRun.runId, commandId, confirmation, projection.run_version, signal));
  }, [activeRun, commandIdFor, projection, runCommand]);

  const uploadFile = useCallback(async (file: File, declaration?: UploadDeclaration) => {
    const signal = lifecycleRef.current?.signal;
    setPendingUploads((current) => current.map((item) => item.file === file ? { ...item, declaration, status: 'uploading', error: undefined } : item));
    try {
      const artifact = await uploadRuntimeArtifact(file, signal, declaration);
      if (!signal?.aborted) setPendingUploads((current) => current.map((item) => item.file === file ? { ...item, status: 'ready', artifactId: artifact.artifact_id, artifact } : item));
    } catch (error) {
      if (!signal?.aborted) setPendingUploads((current) => current.map((item) => item.file === file ? { ...item, status: 'error', error: (error as Error).message } : item));
    }
  }, []);

  const upload = useCallback(async (files: FileList | File[]) => {
    const queued = Array.from(files);
    setPendingUploads((current) => [...current, ...queued.map((file) => ({ file, status: 'uploading' as const }))]);
    let index = 0;
    await Promise.all(Array.from({ length: Math.min(3, queued.length) }, async () => {
      while (index < queued.length && !lifecycleRef.current?.signal.aborted) await uploadFile(queued[index++]);
    }));
  }, [uploadFile]);

  const handleUseArtifact = useCallback((artifact: ArtifactSummary) => {
    setSelectedInputs((current) => mergeArtifacts(current, [artifact]));
    setMobilePane('chat');
  }, []);

  const handleWelcomeExample = useCallback((text: string) => {
    composerRef.current?.setDraft(text);
  }, []);

  const handleWelcomeUpload = useCallback(() => {
    composerRef.current?.openFilePicker();
  }, []);

  const hasMessages = messages.length > 0;
  const visibleArtifacts = useMemo(
    () => mergeArtifacts(artifacts, pendingUploads.flatMap((upload) => upload.artifact ? [upload.artifact] : [])),
    [artifacts, pendingUploads],
  );
  const workflowArtifacts = useMemo(
    () => [...pendingUploads.flatMap((upload) => upload.artifact ? [upload.artifact] : []), ...selectedInputs],
    [pendingUploads, selectedInputs],
  );
  const finalProjection = lastProjection?.status === 'completed' ? lastProjection : null;
  const finalArtifacts = finalProjection?.output_artifacts.filter((artifact) => finalProjection.final_artifact_ids?.includes(artifact.artifact_id)) ?? [];
  const preferredArtifact = finalArtifacts.find((artifact) => artifact.type === 'MapLayer')
    ?? finalArtifacts.find((artifact) => artifact.type === 'File') ?? finalArtifacts[0];
  const displayProjection = projection ?? lastProjection;

  return <div className="runtime-workbench">
    <div className="workbench-toolbar">
      <div><span className="workbench-kicker">SPATIAL WORKSPACE</span><h1 className="mt-1 text-sm font-medium text-ink-100">空间分析工作台</h1></div>
      <div className="flex items-center gap-3"><span className="hidden font-mono text-[10px] text-ink-400 sm:inline">{visibleArtifacts.length} 个成果 · 持久化运行</span><div className="workbench-tabs" aria-label="工作区视图">
        <button type="button" aria-pressed={mobilePane === 'chat'} onClick={() => setMobilePane('chat')}>对话</button>
        <button type="button" aria-pressed={mobilePane === 'data'} onClick={() => setMobilePane('data')}>成果 <span>{visibleArtifacts.length}</span></button>
      </div></div>
    </div>
    {recoveryError && <div role="alert" className="mx-4 mt-2 rounded border border-amber/40 bg-amber/10 p-2 text-xs text-amber">{recoveryError}<button type="button" onClick={() => setRecoveryRevision((value) => value + 1)} className="ml-3 underline">重试恢复</button></div>}
    <div className="workbench-body" data-mobile-pane={mobilePane}>
    <aside className="workbench-artifacts" aria-label="空间成果工作区">
      <ArtifactWorkspace key={sessionId} artifacts={visibleArtifacts} tasks={displayProjection?.tasks} preferredArtifactId={preferredArtifact?.artifact_id} resultRunId={finalProjection?.run_id} disabled={isRunning || !!awaitingInput} onUse={handleUseArtifact} />
    </aside>
    <section className="workbench-chat" aria-label="Runtime 对话">
    <div ref={scroll.containerRef} className={hasMessages ? 'min-h-0 flex-1 overflow-y-auto overscroll-contain' : 'flex min-h-0 flex-1 items-center justify-center overflow-y-auto'}>
      {hasMessages ? <div className="mx-auto flex w-full max-w-3xl flex-col gap-6 px-4 py-6">{messages.map((message) => <MessageBubble key={message.id} message={message} onBeforeMapLoad={scroll.beforeMutation} onAfterMapLoad={scroll.afterMutation} />)}</div> : <WelcomeHero onExample={handleWelcomeExample} onUpload={handleWelcomeUpload} />}
    </div>
    <div className="mx-auto w-full max-w-3xl shrink-0 px-4">
      <RunStatusBar projection={displayProjection} connectionState={activeRun ? runtime.connectionState : lastProjection ? 'closed' : 'idle'} terminalSettled={!activeRun && !!lastProjection && terminalRunStatuses.has(lastProjection.status)} />
    </div>
    <div className="workbench-composer"><div className="mx-auto w-full max-w-3xl px-4 py-3">
      {(commandError || runtime.error) && <div role="alert" className="mb-2 text-sm text-signal-error">{commandError ?? runtime.error?.message}{activeRun && <button type="button" onClick={() => { setCommandError(null); void runtime.connect(); }} className="ml-2 underline">重新连接</button>}</div>}
      {awaitingInput && <div className="mb-2 rounded-lg border border-amber/40 bg-amber/10 px-3 py-2 text-sm text-ink-200"><div className="font-medium text-amber">等待你的回答</div><div className="mt-1 text-ink-300">{cancellationDecision ? '无法确认上次取消是否已执行。输入“重试”可明确重新执行这些任务；输入其他内容将保持暂停。' : String(awaitingInput.message ?? '需要补充输入')}</div>
        {structuredInput && schema && <RuntimeInputForm key={activeRun?.runId + ':' + projection?.run_version} schema={schema} disabled={submitting} onSubmit={resume} />}
      </div>}
      {confirmations.map((confirmation) => <div key={confirmation.confirmation_id} className="mb-2 rounded-lg border border-amber/40 bg-amber/10 px-3 py-2 text-sm text-ink-200"><p>{confirmation.action_summary ?? '任务 ' + confirmation.task_key + ' 需要你的确认后才能执行。'}</p><button type="button" disabled={submitting} onClick={() => confirm(confirmation)} className="mt-2 rounded bg-amber px-3 py-1 text-ink-950 disabled:opacity-50">确认执行 {confirmation.task_key}</button></div>)}
      <UploadTray uploads={pendingUploads} disabled={submitting} onRetry={(file, declaration) => void uploadFile(file, declaration)} onRemove={(file) => setPendingUploads((current) => current.filter((upload) => upload.file !== file))} />
      {selectedInputs.length > 0 && <div aria-label="本轮选用的成果" className="mb-2 flex flex-wrap gap-2">{selectedInputs.map((artifact) => <span key={artifact.artifact_id} className="max-w-full rounded border border-amber/30 bg-amber/10 px-2 py-1 text-xs text-amber"><span className="break-all">{artifact.type} · {artifact.artifact_id}</span><button type="button" disabled={submitting} aria-label={'取消选用 ' + artifact.artifact_id} onClick={() => setSelectedInputs((current) => current.filter((item) => item.artifact_id !== artifact.artifact_id))} className="ml-2">×</button></span>)}</div>}
      <RuntimeComposer
        ref={composerRef}
        isRunning={isRunning}
        submitting={submitting}
        canStop={!!projection && !submitting}
        awaitingInput={!!awaitingInput}
        structuredInput={structuredInput}
        uploadsBlocked={uploadsBlocked}
        workflowArtifacts={workflowArtifacts}
        onSend={send}
        onStop={stop}
        onUpload={upload}
      />
      <div className="mt-2 flex items-center justify-between text-[10px] text-ink-400"><span>明确输入 · 可验证成果</span><span>Enter 发送 / Shift + Enter 换行</span></div>
    </div></div>
    </section>
    </div>
  </div>;
}

interface RuntimeComposerProps {
  isRunning: boolean;
  submitting: boolean;
  canStop: boolean;
  awaitingInput: boolean;
  structuredInput: boolean;
  uploadsBlocked: boolean;
  workflowArtifacts: ArtifactSummary[];
  onSend: (text: string) => Promise<void>;
  onStop: () => void;
  onUpload: (files: FileList | File[]) => Promise<void>;
}

const RuntimeComposer = memo(forwardRef<RuntimeComposerHandle, RuntimeComposerProps>(function RuntimeComposer({
  isRunning,
  canStop,
  awaitingInput,
  structuredInput,
  uploadsBlocked,
  workflowArtifacts,
  onSend,
  onStop,
  onUpload,
}, ref) {
  const [input, setInput] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  useImperativeHandle(ref, () => ({
    setDraft(text: string) {
      setInput(text);
    },
    clearDraft() {
      setInput('');
    },
    openFilePicker() {
      fileInputRef.current?.click();
    },
  }), []);

  // Reset or size textarea inline height whenever input changes (typed, cleared, or set via example).
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    if (input) {
      el.style.height = Math.min(el.scrollHeight, 160) + 'px';
    }
  }, [input]);

  const examples = useMemo(
    () => (isRunning || awaitingInput || uploadsBlocked || input.trim() ? [] : runtimeWorkflowExamples(workflowArtifacts)),
    [isRunning, awaitingInput, uploadsBlocked, input, workflowArtifacts],
  );

  return (
    <>
      {examples.length > 0 && <div role="group" aria-label="工作流示例" className="mb-2 flex flex-wrap items-center gap-2">
        <span className="text-xs text-ink-400">工作流示例</span>
        {examples.map((example) => <button key={example.title} type="button" onClick={() => setInput(example.prompt)}
          className="min-h-8 rounded-md border border-ink-600 px-2.5 text-xs text-ink-200 hover:border-amber hover:text-amber focus-visible:outline focus-visible:outline-amber">
          {example.title}
        </button>)}
      </div>}
      <div className="composer-field flex items-end gap-2 rounded-xl border border-ink-600 bg-ink-800 px-3 py-2">
        <button type="button" onClick={() => fileInputRef.current?.click()} className="h-9 text-ink-400 hover:text-amber" aria-label="上传文件">＋</button>
        <input ref={fileInputRef} className="hidden" type="file" multiple onChange={(event) => { if (event.target.files?.length) void onUpload(event.target.files); event.target.value = ''; }} />
        <textarea
          ref={textareaRef}
          aria-label="分析需求"
          value={input}
          onChange={(event) => setInput(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              void onSend(input);
            }
          }}
          rows={1}
          disabled={isRunning || structuredInput}
          placeholder={awaitingInput ? '请输入补充信息' : '描述你的空间问题…'}
          className="max-h-[160px] min-w-0 flex-1 resize-none bg-transparent py-2 text-sm leading-6 text-ink-100 placeholder:text-ink-400 focus:outline-none"
        />
        {isRunning ? (
          <button type="button" disabled={!canStop} onClick={onStop} className="h-9 rounded-lg border border-signal-error/40 px-3 text-sm text-signal-error">停止</button>
        ) : (
          <button type="button" onClick={() => void onSend(input)} disabled={!input.trim() || structuredInput || uploadsBlocked} className="h-9 rounded-lg bg-amber px-3 text-sm font-medium text-ink-950 disabled:bg-ink-600 disabled:text-ink-400">{awaitingInput ? '回答并继续' : '发送'}</button>
        )}
      </div>
    </>
  );
}));

function WelcomeHero({ onExample, onUpload }: { onExample: (text: string) => void; onUpload: () => void }) {
  return <div className="welcome-atelier">
    <div className="workbench-kicker"><span className="mr-2 inline-block h-1.5 w-1.5 rounded-full bg-amber" /> FROM QUESTION TO GEOGRAPHY</div>
    <h2>让空间问题，<br /><span>有迹可循。</span></h2>
    <p className="welcome-description">从一份数据、一个问题开始。让 Gismind 规划分析，<br className="hidden xl:block" />把每一步变成可检查、可复用的空间成果。</p>
    <div className="welcome-actions">
      <button type="button" onClick={onUpload}><span className="welcome-action-icon">↥</span><span><strong>从数据开始</strong><small>上传矢量、栅格或属性表</small></span><span className="text-amber">↗</span></button>
      <button type="button" onClick={() => onExample('对上传的矢量数据创建 500 米缓冲区，展示地图并导出 GeoJSON。')}><span className="welcome-action-icon">◎</span><span><strong>构建分析流程</strong><small>缓冲区 → 地图 → 导出</small></span><span className="text-amber">↗</span></button>
    </div>
    <div className="welcome-footnote"><span>01 / 描述问题</span><span>02 / 验证执行</span><span>03 / 检查成果</span></div>
  </div>;
}
function mergeArtifacts(current: ArtifactSummary[], incoming: ArtifactSummary[]): ArtifactSummary[] {
  if (!incoming.length) return current;
  const merged = new Map(current.map((artifact) => [artifact.artifact_id, artifact]));
  incoming.forEach((artifact) => merged.set(artifact.artifact_id, { ...merged.get(artifact.artifact_id), ...artifact }));
  return [...merged.values()];
}
function createUserMessage(content: string): ChatMessage { return { id: 'msg_' + crypto.randomUUID(), role: 'user', blocks: [{ type: 'text', content }], status: 'done', created_at: Date.now() }; }
function ensureAssistant(messages: ChatMessage[], active: ActiveRun): ChatMessage[] {
  if (messages.some((message) => message.id === active.messageId)) return messages.map((message) => message.id === active.messageId ? { ...message, run_id: active.runId } : message);
  return [...messages, { id: active.messageId, run_id: active.runId, role: 'assistant', blocks: [], status: 'thinking', statusMessage: '恢复 Runtime…', created_at: Date.now() }];
}
function messagesKey(sessionId: string) { return 'gismind.messages.' + sessionId; }
function loadMessages(sessionId: string): ChatMessage[] {
  try { const parsed = JSON.parse(localStorage.getItem(messagesKey(sessionId)) ?? '[]'); return Array.isArray(parsed) ? parsed as ChatMessage[] : []; } catch { return []; }
}
function loadActiveRun(sessionId: string): ActiveRun | null {
  try { const value = JSON.parse(localStorage.getItem('gismind.active_run.' + sessionId) ?? 'null'); return value && typeof value.runId === 'string' && typeof value.messageId === 'string' ? value : null; } catch { return null; }
}
function saveActiveRun(sessionId: string, active: ActiveRun | null) {
  try { if (active) localStorage.setItem('gismind.active_run.' + sessionId, JSON.stringify(active)); else localStorage.removeItem('gismind.active_run.' + sessionId); } catch { /* server recovery remains available */ }
}
