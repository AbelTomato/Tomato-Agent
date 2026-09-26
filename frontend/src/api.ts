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
  constructor(message: string, readonly status: number) {
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
    try {
      const body: unknown = await response.json();
      if (typeof body === 'object' && body !== null && 'detail' in body && typeof body.detail === 'string') {
        detail = body.detail;
      }
    } catch {
      // 非 JSON 错误响应仍使用 HTTP 状态展示。
    }
    throw new ApiError(detail, response.status);
  }
  return response.json() as Promise<T>;
}

const jsonRequest = (body: unknown): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

export async function createSession(): Promise<string> {
  const result = await request<{ session_id: string }>('/api/sessions', jsonRequest({}));
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