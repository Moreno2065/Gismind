import type { MapLayer } from '@/types/message';

export type RunStatus = 'queued' | 'planning' | 'awaiting_input' | 'ready' | 'running' | 'cancelling' | 'completed' | 'failed' | 'cancelled';

export interface ArtifactSummary {
  artifact_id: string;
  origin?: 'upload' | 'task';
  producer_task_id?: string | null;
  type: 'Vector' | 'Raster' | 'POISet' | 'Coordinate' | 'Table' | 'MapLayer' | 'File' | string;
  crs?: Record<string, unknown> | null;
  schema?: Record<string, unknown>;
  media_type?: string | null;
  content_hash?: string;
  byte_size?: number;
  size_bytes?: number;
  validation_status?: string;
  content_url?: string;
}

export interface RunTaskProjection {
  task_id: string;
  task_key: string;
  status: string;
  attempts: number;
  op?: string | null;
  display_order?: number;
  error_code?: string | null;
  error?: string | null;
  details?: TaskExecutionDetails;
}

export interface TaskArtifactReference {
  name: string;
  ordinal: number;
  artifact_id: string;
}

export interface TaskExecutionDetails {
  parameters: Record<string, unknown>;
  inputs: Array<{
    name: string;
    ordinal: number;
    artifact_id?: string | null;
    source_task_id?: string | null;
    source_task_key?: string | null;
    source_port?: string | null;
    source_ordinal?: number | null;
    selection?: 'one' | 'all';
  }>;
  resolved_inputs: TaskArtifactReference[];
  dependencies: Array<{ task_id: string; task_key: string }>;
  outputs: TaskArtifactReference[];
  latest_attempt?: {
    attempt_no: number;
    status: string;
    started_at: string;
    finished_at?: string | null;
    error_code?: string | null;
  } | null;
  max_attempts?: number | null;
  next_retry_at?: string | null;
}

export interface PlanningAttemptProjection {
  attempt_id: string;
  attempt_no: number;
  phase: 'initial' | 'repair';
  status: 'requested' | 'succeeded' | 'failed';
  error_code?: string | null;
  error?: string | null;
  created_at?: string;
  finished_at?: string | null;
}

export interface PendingConfirmation {
  confirmation_id: string;
  task_id: string;
  task_key: string;
  action_hash: string;
  action_summary?: string;
}

export interface SessionRun {
  run_id: string;
  session_id: string;
  status: RunStatus;
  run_version: number;
}

export interface RunSnapshot {
  run_id: string;
  session_id: string;
  status: RunStatus;
  run_version: number;
  active_input_revision?: { input_revision_id?: string; revision: number } | null;
  active_plan?: { plan_id: string; revision: number; plan_hash?: string | null } | null;
  tasks: RunTaskProjection[];
  planning_attempts?: PlanningAttemptProjection[];
  output_artifacts: ArtifactSummary[];
  final_artifact_ids?: string[];
  last_event_id: number;
  last_seq: number;
  awaiting_input?: Record<string, unknown> | null;
  pending_confirmations?: PendingConfirmation[];
  diagnostic?: Record<string, unknown> | null;
  patch_schema?: Record<string, unknown> | null;
}

export interface RuntimeEvent {
  event_id: number;
  run_id: string;
  seq: number;
  type: string;
  occurred_at?: string;
  schema_version?: string;
  task_id?: string;
  artifact_id?: string;
  payload: Record<string, unknown>;
}

export interface RunProjectionState extends RunSnapshot {
  seen_event_ids: Set<number>;
  events: RuntimeEvent[];
  map_layers: Record<string, MapLayer>;
}

export interface CreateRunRequest {
  session_id: string;
  message: string;
  input_artifact_ids?: string[];
  client_request_id: string;
}

export interface CreateRunResponse {
  run_id: string;
  status: RunStatus;
  run_version: number;
  events_url: string;
}

export interface RunCommandResponse {
  run_id: string;
  status: RunStatus;
  run_version: number;
}

export interface RuntimeErrorEnvelope {
  error: { code: string; message: string; details?: Record<string, unknown>; trace_id?: string };
}
