const RETRIEVAL_MODES = ['keyword', 'vector', 'hybrid'] as const;
export type RetrievalMode = (typeof RETRIEVAL_MODES)[number];


export type Citation = {
  citation_id: string;
  chunk_id: string;
  document_id: string;
  document_version: string;
  title: string;
  source_path: string;
  source_url: string | null;
  heading_path: string;
  start_line: number;
  end_line: number;
  text: string;
};

export type ConversationMessage = {
  role: 'user' | 'assistant';
  content: string;
  citations?: Citation[];
  evidence_status?: 'supported' | 'insufficient' | 'no_results';
  retrieval_mode?: RetrievalMode;
};

export type KnowledgeRunResult = ConversationMessage & {
  answer: string;
  run_id: string;
  session_id: string;
  status: string;
  trace_id: string;
  citations: Citation[];
  evidence_status: 'supported' | 'insufficient' | 'no_results';
  retrieval_mode: RetrievalMode;
};

export type KnowledgeCapabilities = {
  retrieval_modes: RetrievalMode[];
  embedding_configured: boolean;
  embedding_model: string | null;
  embedding_dimensions: number | null;
};

export type KnowledgeDocument = {
  document: {
    document_id: string;
    source_path: string;
    source_url: string | null;
    title: string;
    document_version: string;
  };
  chunks: Array<{
    chunk_id: string;
    document_version: string;
    heading_path: string;
    start_line: number;
    end_line: number;
    text: string;
  }>;
};

export type WritingStatus =
  | 'researching'
  | 'awaiting_outline_confirmation'
  | 'drafting'
  | 'awaiting_save_confirmation'
  | 'saved'
  | 'failed';

export type WritingTask = {
  task_id: string;
  session_id: string;
  topic: string;
  status: WritingStatus;
  version: number;
  outline: Record<string, unknown>;
  draft: string | null;
  citations: Citation[];
  research_run_id: string | null;
  drafting_run_id: string | null;
  saved_path: string | null;
  failed_stage: string | null;
  created_at: string;
  updated_at: string;
};

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000';

export class ApiError extends Error {
  constructor(message: string, readonly status: number, readonly code?: string) {
    super(message);
    this.name = 'ApiError';
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, init);
  } catch {
    throw new ApiError('无法连接后端服务，请确认后端正在运行。', 0);
  }

  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    let code: string | undefined;
    try {
      const body: unknown = await response.json();
      if (typeof body === 'object' && body !== null && 'detail' in body) {
        const responseDetail = body.detail;
        if (typeof responseDetail === 'string') detail = responseDetail;
        if (typeof responseDetail === 'object' && responseDetail !== null && 'code' in responseDetail && typeof responseDetail.code === 'string') {
          code = responseDetail.code;
          detail = code;
        }
      }
    } catch {
      // 非 JSON 错误响应仍使用 HTTP 状态展示。
    }
    throw new ApiError(detail, response.status, code);
  }
  return response.json() as Promise<T>;
}

const jsonRequest = (body: unknown): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

export async function createSession(signal?: AbortSignal): Promise<string> {
  const result = await request<{ session_id: string }>('/api/sessions', { ...jsonRequest({}), signal });
  if (!result.session_id) throw new ApiError('后端未返回 session_id。', 200);
  return result.session_id;
}

export function loadMessages(sessionId: string): Promise<{ messages: ConversationMessage[] }> {
  return request(`/api/sessions/${encodeURIComponent(sessionId)}/messages`);
}

export async function loadKnowledgeCapabilities(): Promise<KnowledgeCapabilities> {
  const result = await request<KnowledgeCapabilities>('/api/knowledge/capabilities');
  const modes = result.retrieval_modes.filter(
    (mode): mode is RetrievalMode => (RETRIEVAL_MODES as readonly string[]).includes(mode),
  );
  return {
    ...result,
    retrieval_modes: modes.length > 0 ? modes : ['keyword'],
  };
}

export function sendKnowledgeMessage(sessionId: string, message: string, retrievalMode: RetrievalMode): Promise<KnowledgeRunResult> {
  return request(
    `/api/sessions/${encodeURIComponent(sessionId)}/knowledge-runs`,
    jsonRequest({ message, retrieval_mode: retrievalMode }),
  );
}

export function sendBasicMessage(sessionId: string, message: string): Promise<{ answer?: string; message?: string; status?: string }> {
  return request(`/api/sessions/${encodeURIComponent(sessionId)}/runs`, jsonRequest({ message }));
}

export function loadDocument(documentId: string): Promise<KnowledgeDocument> {
  return request(`/api/knowledge/documents/${encodeURIComponent(documentId)}`);
}

export function createWritingTask(sessionId: string, topic: string): Promise<WritingTask> {
  return request(`/api/sessions/${encodeURIComponent(sessionId)}/writing-tasks`, jsonRequest({ topic }));
}

export function loadWritingTask(taskId: string): Promise<WritingTask> {
  return request(`/api/writing-tasks/${encodeURIComponent(taskId)}`);
}

export function researchWritingTask(taskId: string, version: number): Promise<WritingTask> {
  return request(
    `/api/writing-tasks/${encodeURIComponent(taskId)}/research`,
    jsonRequest({ version }),
  );
}

export function confirmWritingOutline(
  taskId: string,
  version: number,
  outline: Record<string, unknown>,
): Promise<WritingTask> {
  return request(
    `/api/writing-tasks/${encodeURIComponent(taskId)}/confirm-outline`,
    jsonRequest({ version, outline }),
  );
}

export function generateWritingDraft(taskId: string, version: number): Promise<WritingTask> {
  return request(
    `/api/writing-tasks/${encodeURIComponent(taskId)}/draft`,
    jsonRequest({ version }),
  );
}

export type WritingAttempt = {
  status: 'running' | 'completed' | 'failed' | 'conflicted';
  error_code: string | null;
};

export function loadDraftAttempt(taskId: string): Promise<WritingAttempt | null> {
  return request(`/api/writing-tasks/${encodeURIComponent(taskId)}/draft-attempt`);
}

export function saveWritingTask(taskId: string, version: number, idempotencyKey: string): Promise<WritingTask> {
  return request(
    `/api/writing-tasks/${encodeURIComponent(taskId)}/save`,
    jsonRequest({ version, idempotency_key: idempotencyKey }),
  );
}

export function retryWritingTask(taskId: string, version: number): Promise<WritingTask> {
  return request(
    `/api/writing-tasks/${encodeURIComponent(taskId)}/retry`,
    jsonRequest({ version }),
  );
}

export function isSafeExternalUrl(value: string | null): value is string {
  if (!value) return false;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' || url.protocol === 'http:';
  } catch {
    return false;
  }
}

export type CodeTaskStatus = 'queued' | 'running' | 'waiting' | 'completed' | 'failed' | 'cancelled' | 'timed_out';
export type CodeTaskEvent = { id: string; sequence: number; event_type: string; payload: Record<string, unknown>; created_at: string };
export type CodeTaskArtifactRef = {
  artifact_id: string; run_id: string; kind: string; size_bytes: number;
  sha256: string; created_at: string; relative_path: string | null;
};
export type CodeTaskSnapshotSummary = {
  sequence: number; created_at: string;
  protocol_version: number | null; schema_version: number | null;
  checkpoint_revision: number | null; last_event_sequence: number | null;
  phase: 'ready_model' | 'pending_tool' | 'ready_finish' | 'waiting' | null;
  logical_index: number | null;
};
export type CodeTask = {
  run_id: string; workspace_id: string; status: CodeTaskStatus; task_type: string;
  dispatch_status: 'worker_enabled' | 'worker_disabled'; version: number;
  error: Record<string, unknown> | null; artifacts: CodeTaskArtifactRef[];
  test_results: Array<{ passed: boolean; exit_code: number | null; [key: string]: unknown }>;
  changed_files: string[]; diff_artifact: CodeTaskArtifactRef | null;
  snapshot_summary: CodeTaskSnapshotSummary | null; event_cursor: number;
};
export type CodeTaskSubmission = Pick<CodeTask, 'run_id' | 'workspace_id' | 'status' | 'task_type' | 'dispatch_status'>;
export type CodeTaskRecovery = { status: CodeTaskStatus; recovery_reason?: string; recovery_action?: string };
export type CodeTaskArtifact = { artifact: CodeTaskArtifactRef; content: string };

const codeTaskPath = (runId: string) => `/api/code-tasks/${encodeURIComponent(runId)}`;

export function createCodeTask(task: string, signal?: AbortSignal): Promise<CodeTaskSubmission> {
  return request('/api/code-tasks', { ...jsonRequest({ task }), signal });
}
export function getCodeTask(runId: string, signal?: AbortSignal): Promise<CodeTask> {
  return request(codeTaskPath(runId), { signal });
}
export function listCodeTaskEvents(runId: string, after = 0, signal?: AbortSignal): Promise<{ events: CodeTaskEvent[] }> {
  return request(`${codeTaskPath(runId)}/events?after=${after}`, { signal });
}
export function executeCodeTask(runId: string, signal?: AbortSignal): Promise<CodeTask> {
  return request(`${codeTaskPath(runId)}/execute`, { ...jsonRequest({}), signal });
}
export function cancelCodeTask(runId: string, signal?: AbortSignal): Promise<Pick<CodeTask, 'run_id' | 'status' | 'error'>> {
  return request(`${codeTaskPath(runId)}/cancel`, { ...jsonRequest({ reason: 'user' }), signal });
}
export function recoverCodeTask(runId: string, expectedVersion: number, action: 'inspect' | 'continue', confirm = false, signal?: AbortSignal): Promise<CodeTaskRecovery> {
  return request(`${codeTaskPath(runId)}/recover`, { ...jsonRequest({ expected_version: expectedVersion, action, confirm }), signal });
}
export function loadCodeTaskArtifact(runId: string, artifactId: string, signal?: AbortSignal): Promise<CodeTaskArtifact> {
  return request(`${codeTaskPath(runId)}/artifacts/${encodeURIComponent(artifactId)}`, { signal });
}

export type TaskKind = 'assistant' | 'chat' | 'knowledge' | 'writing' | 'code';
export type TaskAction = 'provide_input' | 'approve_action' | 'reject_action' | 'bind_workspace' | 'cancel' | 'confirm_outline' | 'save_draft' | 'inspect_recovery' | 'continue_recovery';
export type TaskRecoveryObservation = {
  version: number; reason: string;
  action: 'reuse' | 'retry' | 'reconcile' | 'manual_review' | 'reject';
  allowed_actions: Extract<TaskAction, 'cancel' | 'inspect_recovery' | 'continue_recovery'>[];
};
export type Session = { id: string; metadata: Record<string, unknown>; default_workspace_id: string | null; created_at: string };
export type Workspace = { workspace_id: string; repo_path?: string; pytest_paths?: string[]; available?: boolean };
export type UnifiedResult = Partial<WritingTask> & {
  answer?: string; content?: string; evidence_status?: string;
  test_results?: CodeTask['test_results']; artifacts?: CodeTaskArtifactRef[];
  changed_files?: string[]; diff_artifact?: CodeTaskArtifactRef | null;
  schema_version?: number; validation_status?: 'not_required' | 'passed' | 'failed' | 'unavailable';
};
export type UnifiedTask = {
  run_id: string; session_id: string; kind: TaskKind; status: string; phase: string | null;
  version: number; workspace_id: string | null; created_at: string; legacy: boolean;
  result: UnifiedResult | null; error?: Record<string, unknown> | null;
  recovery?: TaskRecoveryObservation;
  messages?: ConversationMessage[]; capabilities: TaskAction[];
  pending_action: { action_id: string; reason: string; business_version: number;
    action_type?: string; target_capability?: string | null; frozen_parameters?: Record<string, unknown>;
    allowed_actions: TaskAction[]; display_data: UnifiedResult & Record<string, unknown> } | null;
};
export type UnifiedTaskRequest = { kind: TaskKind; message: string; client_request_id: string;
  workspace_id?: string | null; options?: { retrieval_mode?: RetrievalMode; limit?: number } };
export type TaskActionRequest = { action_id: string; action: TaskAction; expected_version: number;
  client_request_id: string; payload: Record<string, unknown> };
const sessionPath = (id: string) => `/api/sessions/${encodeURIComponent(id)}`;
const taskPath = (id: string) => `/api/tasks/${encodeURIComponent(id)}`;
export type SessionQueue = { paused: boolean; requests: Array<{ request_id: string; status: string;
  run_id: string | null; request: UnifiedTaskRequest }> };
export function listSessionQueue(id: string, signal?: AbortSignal): Promise<SessionQueue> {
  return request(`${sessionPath(id)}/queue`, { signal });
}
export function enqueueTask(id: string, body: UnifiedTaskRequest, signal?: AbortSignal): Promise<{ request_id: string }> {
  return request(`${sessionPath(id)}/queue`, { ...jsonRequest(body), signal });
}
export function controlSessionQueue(id: string, action: 'resume' | 'clear', signal?: AbortSignal): Promise<SessionQueue> {
  return request(`${sessionPath(id)}/queue/${action}`, { ...jsonRequest({}), signal });
}
export function steerTask(id: string, body: { client_request_id: string; message: string }, signal?: AbortSignal): Promise<{ request_id: string; status: string }> {
  return request(`${taskPath(id)}/steer`, { ...jsonRequest(body), signal });
}
export function listSessions(limit = 200, offset = 0, signal?: AbortSignal): Promise<{ sessions: Session[] }> {
  return request(`/api/sessions?limit=${limit}&offset=${offset}`, { signal });
}
export function listSessionTasks(id: string, limit = 200, offset = 0, signal?: AbortSignal): Promise<{ tasks: UnifiedTask[] }> {
  return request(`${sessionPath(id)}/tasks?limit=${limit}&offset=${offset}`, { signal });
}
export function submitTask(id: string, body: UnifiedTaskRequest, signal?: AbortSignal): Promise<UnifiedTask> {
  return request(`${sessionPath(id)}/tasks`, { ...jsonRequest(body), signal });
}
export function getTask(id: string, signal?: AbortSignal): Promise<UnifiedTask> {
  return request(taskPath(id), { signal });
}
export function listTaskEvents(id: string, after = 0, signal?: AbortSignal): Promise<{ events: CodeTaskEvent[]; next_cursor: number }> {
  return request(`${taskPath(id)}/events?after=${after}&limit=200`, { signal });
}
export function actOnTask(id: string, body: TaskActionRequest, signal?: AbortSignal): Promise<UnifiedTask> {
  return request(`${taskPath(id)}/actions`, { ...jsonRequest(body), signal });
}
export function listWorkspaces(signal?: AbortSignal): Promise<{ workspaces: Workspace[] }> {
  return request('/api/workspaces', { signal });
}
export function registerWorkspace(path: string, pytest_paths: string[], signal?: AbortSignal): Promise<Workspace> {
  return request('/api/workspaces', { ...jsonRequest({ path, pytest_paths }), signal });
}
export function setSessionWorkspace(id: string, workspace_id: string | null, signal?: AbortSignal): Promise<Session> {
  return request(`${sessionPath(id)}/workspace`, { ...jsonRequest({ workspace_id }), signal });
}