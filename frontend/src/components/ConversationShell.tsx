import { useEffect, useRef, useState } from 'react';
import {
  createSession, listSessions, listSessionTasks, submitTask, getTask, listTaskEvents,
  actOnTask, listWorkspaces, registerWorkspace, setSessionWorkspace, loadKnowledgeCapabilities, loadDocument,
  listSessionQueue, enqueueTask, controlSessionQueue, steerTask, type SessionQueue,
  type Session, type Workspace, type UnifiedTask, type TaskKind, type TaskAction,
  type RetrievalMode, type Citation, type KnowledgeDocument, type CodeTaskEvent,
} from '../api';
import { TaskCard, taskLabels } from './TaskCard';
import { WorkspacePicker } from './WorkspacePicker';

const storageKey = 'tomato-agent-session-id';
const terminal = (task: UnifiedTask) => ['completed', 'failed', 'cancelled', 'timed_out'].includes(task.status) || task.legacy;
const message = (reason: unknown) => reason instanceof Error ? (reason.message === 'assistant_disabled' ? '助手入口未启用，请配置 [assistant].enabled=true 与 [worker].enabled=true，或选择高级模式。' : reason.message) : '请求失败。';
function storedId() {
  try { return localStorage.getItem(storageKey) ?? ''; } catch { return ''; }
}
function remember(id: string) {
  try { localStorage.setItem(storageKey, id); } catch { /* 存储不可用时仍可操作。 */ }
}

export function ConversationShell() {
  const [sessionId, setSessionId] = useState(storedId);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [tasks, setTasks] = useState<UnifiedTask[]>([]);
  const [events, setEvents] = useState<Record<string, CodeTaskEvent[]>>({});
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [workspaceId, setWorkspaceId] = useState('');
  const [kind, setKind] = useState<TaskKind>('assistant');
  const [input, setInput] = useState('');
  const [retrievalMode, setRetrievalMode] = useState<RetrievalMode>('keyword');
  const [modes, setModes] = useState<RetrievalMode[]>(['keyword']);
  const [capabilityNotice, setCapabilityNotice] = useState('正在读取检索能力…');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [pollError, setPollError] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [document, setDocument] = useState<KnowledgeDocument | null>(null);
  const [documentError, setDocumentError] = useState('');
  const generation = useRef(0);
  const actionController = useRef<AbortController | null>(null);
  const documentController = useRef(0);
  const submission = useRef<{ fingerprint: string; id: string } | null>(null);
  const actionRequest = useRef<{ fingerprint: string; id: string } | null>(null);
  // 动作检查是一次观察；只在同一 Run 版本内保留，轮询不能把它丢弃。
  const recoveryObservations = useRef(new Map<string, UnifiedTask['recovery']>());
  const active = tasks.some((task) => !terminal(task));
  const [queue, setQueue] = useState<SessionQueue>({ paused: false, requests: [] });
  const [steerNotice, setSteerNotice] = useState('');

  useEffect(() => {
    const controller = new AbortController();
    void loadKnowledgeCapabilities().then((capabilities) => {
      if (controller.signal.aborted) return;
      setModes(capabilities.retrieval_modes);
      setCapabilityNotice(capabilities.embedding_configured ? '检索来源由服务端返回，可查看引用原文。' : 'Embedding 未配置，仅支持关键词检索。');
    }).catch((reason) => { if (!controller.signal.aborted) setCapabilityNotice(`检索能力读取失败：${message(reason)}`); });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const currentGeneration = ++generation.current;
    actionController.current?.abort();
    setBusy(false); setLoading(true); setTasks([]); setEvents({}); setWorkspaceId('');
    setDocument(null); setDocumentError(''); documentController.current += 1;
    setPollError('');
    setQueue({ paused: false, requests: [] });
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;
    const cursors = new Map<string, number>();
    const current = () => !controller.signal.aborted && generation.current === currentGeneration;
    async function history() {
      const collected: UnifiedTask[] = [];
      for (let offset = 0; ; offset += 200) {
        const page = await listSessionTasks(sessionId, 200, offset, controller.signal);
        if (!current()) return [];
        collected.push(...page.tasks);
        if (page.tasks.length < 200) return collected;
      }
    }
    async function poll() {
      try {
        const historyTasks = await history();
        const queued = sessionId ? await listSessionQueue(sessionId, controller.signal) : { paused: false, requests: [] };
        if (!current()) return;
        setQueue(queued);
        let moreEvents = false;
        const loaded = await Promise.all(historyTasks.map(async (task) => {
          if (task.legacy) return task;
          const detail = !terminal(task) ? await getTask(task.run_id, controller.signal) : task;
          const after = cursors.get(task.run_id) ?? 0;
          const page = await listTaskEvents(task.run_id, after, controller.signal);
          if (!current()) return task;
          cursors.set(task.run_id, page.next_cursor);
          if (page.events.length === 200) moreEvents = true;
          setEvents((previous) => ({ ...previous, [task.run_id]: [...new Map([
            ...(previous[task.run_id] ?? []), ...page.events,
          ].map((event) => [event.sequence, event])).values()].sort((a, b) => a.sequence - b.sequence) }));
          const observed = recoveryObservations.current.get(task.run_id);
          if (observed?.version !== detail.version) recoveryObservations.current.delete(task.run_id);
          return { ...task, ...detail, messages: task.messages,
            recovery: detail.recovery ?? (observed?.version === detail.version ? observed : undefined) };
        }));
        if (!current()) return;
        setTasks(loaded); setLoading(false); setPollError(''); failures = 0;
        if (moreEvents || loaded.some((task) => !terminal(task)) || queued.requests.some((item) => item.status === 'pending')) timer = setTimeout(() => void poll(), 1000);
      } catch (reason) {
        if (!current()) return;
        setLoading(false); setPollError(message(reason));
        timer = setTimeout(() => void poll(), Math.min(1000 * 2 ** failures++, 10000));
      }
    }
    async function initialize() {
      try {
        const allSessions: Session[] = [];
        for (let offset = 0; ; offset += 200) {
          const page = await listSessions(200, offset, controller.signal);
          if (!current()) return;
          allSessions.push(...page.sessions);
          if (page.sessions.length < 200) break;
        }
        const available = await listWorkspaces(controller.signal);
        if (!current()) return;
        setSessions(allSessions); setWorkspaces(available.workspaces);
        if (!sessionId) { setLoading(false); return; }
        const selected = allSessions.find((session) => session.id === sessionId);
        if (!selected) { setLoading(false); setError('保存的会话已失效，请选择或新建会话。'); return; }
        setWorkspaceId(selected.default_workspace_id ?? '');
        await poll();
      } catch (reason) {
        if (current()) { setLoading(false); setPollError(message(reason)); }
      }
    }
    void initialize();
    return () => { controller.abort(); actionController.current?.abort(); documentController.current += 1; if (timer !== undefined) clearTimeout(timer); };
  }, [sessionId, refresh]);

  function selectSession(id: string) {
    setSteerNotice('');
    if (id === sessionId) return;
    generation.current += 1; actionController.current?.abort(); documentController.current += 1;
    submission.current = null; actionRequest.current = null;
    recoveryObservations.current.clear();
    remember(id); setSessionId(id); setInput(''); setTasks([]); setEvents({}); setDocument(null); setError(''); setBusy(false); setLoading(true);
  }
  async function operate(operation: (signal: AbortSignal, current: () => boolean) => Promise<void>) {
    if (busy) return;
    actionController.current?.abort();
    const controller = new AbortController(); actionController.current = controller;
    const captured = generation.current;
    const current = () => !controller.signal.aborted && captured === generation.current;
    setBusy(true); setError('');
    try { await operation(controller.signal, current); }
    catch (reason) { if (current()) setError(message(reason)); }
    finally { if (current()) setBusy(false); }
  }
  async function newSession() {
    await operate(async (signal, current) => {
      const id = await createSession(signal);
      if (current()) selectSession(id);
    });
  }
  async function send() {
    if (!sessionId || !input.trim() || loading || (kind === 'code' && !workspaces.some((item) => item.workspace_id === workspaceId && item.available !== false))) return;
    await operate(async (signal, current) => {
      const body = { kind, message: input.trim(), ...((kind === 'code' || kind === 'assistant' && workspaceId) ? { workspace_id: workspaceId } : {}),
        ...(kind === 'knowledge' ? { options: { retrieval_mode: retrievalMode } } : kind === 'assistant' ? { options: {} } : {}) };
      const queued = active || queue.requests.some((item) => item.status === 'pending');
      const fingerprint = JSON.stringify({ sessionId, queued, ...body });
      if (submission.current?.fingerprint !== fingerprint) submission.current = { fingerprint, id: crypto.randomUUID() };
      if (queued) {
        await enqueueTask(sessionId, { ...body, client_request_id: submission.current!.id }, signal);
        if (current()) { submission.current = null; setInput(''); setRefresh((value) => value + 1); }
        return;
      }
      const task = await submitTask(sessionId, { ...body, client_request_id: submission.current!.id }, signal);
      if (!current()) return;
      submission.current = null; setInput(''); setTasks((previous) => [...previous.filter((item) => item.run_id !== task.run_id), task]);
      setRefresh((value) => value + 1);
    });
  }
  async function steer() {
    const task = tasks.find((item) => item.kind === 'assistant' && !terminal(item));
    if (!task || !input.trim()) return;
    await operate(async (signal, current) => {
      const body = { message: input.trim() };
      const fingerprint = JSON.stringify({ steer: task.run_id, ...body });
      if (submission.current?.fingerprint !== fingerprint) submission.current = { fingerprint, id: crypto.randomUUID() };
      await steerTask(task.run_id, { ...body, client_request_id: submission.current!.id }, signal);
      if (current()) { submission.current = null; setInput(''); setSteerNotice('调整请求已接收，等待服务端安全点生效；未决动作仍需单独处理。'); setRefresh((value) => value + 1); }
    });
  }
  async function queueControl(action: 'resume' | 'clear') {
    await operate(async (signal, current) => {
      const next = await controlSessionQueue(sessionId, action, signal);
      if (current()) { setQueue(next); setRefresh((value) => value + 1); }
    });
  }
  async function action(task: UnifiedTask, action: TaskAction, payload: Record<string, unknown>) {
    await operate(async (signal, current) => {
      const body = { action_id: task.pending_action?.action_id ?? `${task.run_id}:${task.version}:cancel`,
        action, expected_version: task.version, payload };
      const fingerprint = JSON.stringify({ run: task.run_id, ...body });
      if (actionRequest.current?.fingerprint !== fingerprint) actionRequest.current = { fingerprint, id: crypto.randomUUID() };
      let updated: UnifiedTask;
      try {
        updated = await actOnTask(task.run_id, { ...body, client_request_id: actionRequest.current!.id }, signal);
      } catch (reason) {
        if (current() && reason instanceof Error && ['stale_action', 'version_conflict', 'action_conflict'].includes(reason.message)) {
          actionRequest.current = null;
          setRefresh((value) => value + 1);
        }
        throw reason;
      }
      if (!current()) return;
      actionRequest.current = null;
      if (updated.recovery) recoveryObservations.current.set(task.run_id, updated.recovery);
      else recoveryObservations.current.delete(task.run_id);
      setTasks((previous) => previous.map((item) => item.run_id === task.run_id ? { ...item, ...updated } : item));
      // 重启轮询，但保留操作错误；错误只在下一次明确操作时清除。
      setRefresh((value) => value + 1);
    });
  }
  async function bind(id: string) {
    await operate(async (signal, current) => {
      const updated = await setSessionWorkspace(sessionId, id || null, signal);
      if (current()) { setWorkspaceId(updated.default_workspace_id ?? ''); setSessions((previous) => previous.map((item) => item.id === sessionId ? updated : item)); }
    });
  }
  async function register(path: string, targets: string[]) {
    await operate(async (signal, current) => {
      const workspace = await registerWorkspace(path, targets, signal);
      if (!current()) return;
      setWorkspaces((previous) => [...previous.filter((item) => item.workspace_id !== workspace.workspace_id), workspace]);
      const updated = await setSessionWorkspace(sessionId, workspace.workspace_id, signal);
      if (current()) { setWorkspaceId(updated.default_workspace_id ?? ''); setSessions((previous) => previous.map((item) => item.id === sessionId ? updated : item)); }
    });
  }
  async function openDocument(citation: Citation) {
    const captured = ++documentController.current;
    setDocument(null); setDocumentError('');
    try { const loaded = await loadDocument(citation.document_id); if (captured === documentController.current) setDocument(loaded); }
    catch (reason) { if (captured === documentController.current) setDocumentError(message(reason)); }
  }

  return <main className="conversation-layout">
    <aside className="session-sidebar"><h1>Tomato Agent</h1><button type="button" disabled={busy} onClick={() => void newSession()}>新建会话</button>
      <nav aria-label="会话列表">{sessions.map((session) => <button className="secondary-button" type="button" key={session.id} aria-current={session.id === sessionId ? 'true' : undefined} onClick={() => selectSession(session.id)}>{String(session.metadata.title ?? session.id)}</button>)}</nav>
    </aside>
    <section className="card conversation-shell" aria-label="统一会话">
      <header><div><h2>统一助手</h2><p>直接描述目标，助手在同一任务中检索资料、读取项目并解释；当前助手能力为只读。</p></div><span>{loading ? '恢复中' : active ? '有活动任务' : '就绪'}</span></header>
      <div className="controls"><label>入口（其他为高级模式）<select value={kind} onChange={(event) => setKind(event.target.value as TaskKind)}>{Object.entries(taskLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        {kind === 'knowledge' && <label>检索方式<select value={retrievalMode} onChange={(event) => setRetrievalMode(event.target.value as RetrievalMode)}>{modes.map((mode) => <option key={mode} value={mode}>{mode}</option>)}</select></label>}
        <button type="button" className="secondary-button" disabled={busy} onClick={() => setRefresh((value) => value + 1)}>刷新会话</button>
      </div>
      {kind === 'knowledge' && <p className="capability-note">{capabilityNotice}</p>}
      {kind === 'assistant' && <p className="capability-note">工作区可选；需要读取项目时可按任务请求绑定一次，已绑定任务不能切换项目。</p>}
      <WorkspacePicker workspaces={workspaces} value={workspaceId} disabled={busy || loading || active || !sessionId} onSelect={(id) => void bind(id)} onRegister={register} />
      {error && <p className="notice error" role="alert">{error}</p>}
      {pollError && <p className="notice error" role="alert">读取失败：{pollError}</p>}
      <div className="task-timeline" aria-live="polite">{tasks.length === 0 && <p className="empty-state">{sessionId ? '当前会话暂无任务。' : '请新建或选择会话。'}</p>}
        {tasks.map((task) => <div key={task.run_id}><TaskCard task={task} workspaces={workspaces} busy={busy} onAction={action} onOpenDocument={(citation) => void openDocument(citation)} />
          {!task.legacy && <details className="task-events"><summary>事件（{events[task.run_id]?.length ?? 0}）</summary><ol>{events[task.run_id]?.map((event) => <li key={event.sequence}><b>#{event.sequence} {event.event_type}</b><pre>{JSON.stringify(event.payload, null, 2)}</pre></li>)}</ol></details>}
        </div>)}
      </div>
      {active && <p className="notice">后续消息进入队列；回答、审批、绑定和核查请使用任务动作。</p>}
      {steerNotice && <p role="status">{steerNotice}</p>}
      {tasks.some((task) => events[task.run_id]?.some((event) => event.event_type === 'assistant.steer_applied')) && <p role="status">当前任务调整已生效（详见事件）。</p>}
      {queue.requests.some((item) => item.status === 'pending') && <section aria-label="后续消息队列">
        <p>{queue.paused ? '队列已暂停' : '队列等待启动'}</p>
        <ol>{queue.requests.filter((item) => item.status === 'pending').map((item) => <li key={item.request_id}>{item.request.message}</li>)}</ol>
        {queue.paused && <button type="button" disabled={busy || loading} onClick={() => void queueControl('resume')}>继续队列</button>}
        <button type="button" disabled={busy || loading} onClick={() => void queueControl('clear')}>清空待启动队列</button>
      </section>}
      {kind === 'code' && !workspaces.some((item) => item.workspace_id === workspaceId && item.available !== false) && <p className="notice">代码任务需要绑定可用原目录工作区。</p>}
      <form className="composer" onSubmit={(event) => { event.preventDefault(); void send(); }}><textarea aria-label="任务输入" value={input} rows={3} onChange={(event) => setInput(event.target.value)} disabled={busy || loading || !sessionId} placeholder="输入本轮任务…" /><button type="submit" disabled={busy || loading || !sessionId || !input.trim() || (kind === 'code' && !workspaces.some((item) => item.workspace_id === workspaceId && item.available !== false))}>{active || queue.requests.some((item) => item.status === 'pending') ? '排队发送' : '提交任务'}</button>
        {tasks.some((task) => task.kind === 'assistant' && !terminal(task)) && <button type="button" disabled={busy || loading || !input.trim()} onClick={() => void steer()}>调整当前任务</button>}
      </form>
      <small>当前 Session：{sessionId || '未选择'} · 新任务通过单 Worker 后台执行。</small>
    </section>
    {(document || documentError) && <section className="document-panel"><button className="close-button" type="button" onClick={() => { documentController.current += 1; setDocument(null); setDocumentError(''); }}>关闭</button>
      {documentError ? <p className="notice error">{documentError}</p> : document && <><h2>{document.document.title}</h2><p className="document-meta">{document.document.source_path} · {document.document.document_version}</p>{document.chunks.map((chunk) => <article className="document-chunk" key={chunk.chunk_id}><h3>{chunk.heading_path}</h3><span>第 {chunk.start_line}–{chunk.end_line} 行</span><p>{chunk.text}</p></article>)}</>}
    </section>}
  </main>;
}