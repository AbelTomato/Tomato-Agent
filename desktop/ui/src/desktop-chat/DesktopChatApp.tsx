import { useEffect, useRef, useState } from 'react';
import { desktopClient } from '../desktop/client';
const { invoke, listen } = desktopClient;
import type { FormEvent } from 'react';
import { ProjectNavigation } from './ProjectNavigation';
import type { ChatProject as Project, ChatSession as Session } from './ProjectNavigation';

type Message = { id: string; session_id: string; role: 'user' | 'assistant'; content: string; created_at: string };
type Delta = { request_id: string; session_id: string; delta: string };
type TaskEvent = { sequence: number; event_type: string; summary?: string; payload: Record<string, unknown> };
type PendingAction = { action_id: string; reason: string; action_type?: string; allowed_actions: string[]; display_data: Record<string, unknown> };
type AgentTask = { run_id: string; session_id: string; kind: string; status: string; phase: string | null; version: number;
  result: { answer?: string; draft?: string; outline?: Record<string, unknown>; citations?: unknown[]; artifacts?: unknown[] } | null; error: { code?: string } | null;
  pending_action: PendingAction | null; capabilities: string[]; events?: TaskEvent[] };
type EngineWorkspace = { workspace_id: string; repo_path?: string; available?: boolean };
type EditDialog = { kind: 'create-project'; rootPath: string } | { kind: 'rename-project'; project: Project }
  | { kind: 'delete-project'; project: Project } | { kind: 'rename-session'; session: Session }
  | { kind: 'delete-session'; session: Session };

export function DesktopChatApp() {
  const [configured, setConfigured] = useState(false);
  const [initializing, setInitializing] = useState(true);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [messages, setMessages] = useState<Message[]>([]);
  const [agentTasks, setAgentTasks] = useState<AgentTask[]>([]);
  const [engineWorkspaces, setEngineWorkspaces] = useState<EngineWorkspace[]>([]);
  const [selectedWorkspace, setSelectedWorkspace] = useState('');
  const [input, setInput] = useState('');
  const [taskReply, setTaskReply] = useState('');
  const [mode, setMode] = useState<'chat' | 'assistant' | 'writing'>('chat');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [settings, setSettings] = useState(false);
  const [baseUrl, setBaseUrl] = useState('https://api.openai.com/v1');
  const [model, setModel] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [testingConnection, setTestingConnection] = useState(false);
  const [dialog, setDialog] = useState<EditDialog | null>(null);
  const [dialogValue, setDialogValue] = useState('');
  const [dialogError, setDialogError] = useState('');
  const [working, setWorking] = useState(false);
  const [notice, setNotice] = useState('');
  const mutationPending = useRef(false);
  const navigationRevision = useRef(0);
  const historyRevision = useRef(0);
  const activeSession = useRef<string | null>(null);
  const activeSendSession = useRef<string | null>(null);
  const activeGeneration = useRef<string | null>(null);
  const [hasOlder, setHasOlder] = useState(false);
  const activeTaskKey = agentTasks.filter(task => ['queued', 'running', 'waiting'].includes(task.status)).map(task => task.run_id).join(',');
  const syncedTaskReplies = useRef(new Set<string>());

  useEffect(() => {
    let active = true;
    void invoke<{ configured: boolean }>('desktop_chat_status').then(value => { if (active) setConfigured(value.configured); })
      .catch(reason => { if (active) setError(String(reason)); });
    const unlisten = listen<Delta>('desktop_chat_delta', event => {
      if (event.payload.session_id !== activeSession.current || event.payload.request_id !== activeGeneration.current) return;
      setMessages(current => {
        const last = current.at(-1);
        if (last?.role === 'assistant' && last.id === 'streaming') {
          return [...current.slice(0, -1), { ...last, content: last.content + event.payload.delta }];
        }
        return [...current, { id: 'streaming', session_id: event.payload.session_id, role: 'assistant', content: event.payload.delta, created_at: new Date().toISOString() }];
      });
    });
    return () => { active = false; void unlisten.then(stop => stop()); };
  }, [sessionId]);

  useEffect(() => {
    let active = true;
    const completed = listen<{ session_id: string; request_id: string }>('desktop_chat_completed', event => {
      if (event.payload.session_id === activeSession.current && event.payload.request_id === activeGeneration.current) setBusy(false);
    });
    const cancelled = listen<{ session_id: string; request_id: string }>('desktop_chat_cancelled', event => {
      if (event.payload.session_id !== activeSession.current || event.payload.request_id !== activeGeneration.current) return;
      setBusy(false);
      void refreshHistory(event.payload.session_id).catch(reason => setError(String(reason)));
    });
    const failed = listen<{ session_id: string; request_id: string; error: string }>('desktop_chat_failed', event => {
      if (event.payload.session_id !== activeSession.current || event.payload.request_id !== activeGeneration.current) return;
      setBusy(false);
      setError(event.payload.error);
      void refreshHistory(event.payload.session_id).catch(reason => setError(String(reason)));
    });
    void Promise.all([invoke<Session[]>('desktop_chat_sessions'), invoke<Project[]>('desktop_chat_projects')]).then(async ([list, projectList]) => {
      if (!active) return;
      setSessions(list);
      setProjects(projectList);
      const id = await invoke<string | null>('desktop_chat_latest_session');
      if (!active || !id) return;
      await selectSession(id);
    }).catch(reason => { if (active) setError(String(reason)); }).finally(() => { if (active) setInitializing(false); });
    return () => { active = false; void completed.then(stop => stop()); void cancelled.then(stop => stop()); void failed.then(stop => stop()); };
  }, []);

  async function refreshNavigation() {
    const [list, projectList] = await Promise.all([
      invoke<Session[]>('desktop_chat_sessions'),
      invoke<Project[]>('desktop_chat_projects'),
    ]);
    setSessions(list);
    setProjects(projectList);
    return list;
  }

  async function refreshHistory(id: string) {
    const revision = ++historyRevision.current;
    try {
      const history = await invoke<Message[]>('desktop_chat_history', { sessionId: id });
      if (activeSession.current === id && historyRevision.current === revision) { setMessages(history); setHasOlder(history.length >= 100); }
    } catch (reason) {
      if (activeSession.current === id && historyRevision.current === revision) throw reason;
    }
  }

  async function refreshAgentTasks(id: string) {
    const revision = navigationRevision.current;
    const tasks = await invoke<AgentTask[]>('desktop_agent_session_tasks', { sessionId: id });
    if (activeSession.current !== id || navigationRevision.current !== revision) return;
    setAgentTasks(tasks.slice(0, 10));
    if (tasks.some(task => task.capabilities.includes('bind_workspace'))) {
      const workspaces = await invoke<EngineWorkspace[]>('desktop_agent_workspaces');
      if (activeSession.current === id && navigationRevision.current === revision) {
        setEngineWorkspaces(workspaces.filter(workspace => workspace.available !== false));
      }
    }
    for (const task of tasks.slice(0, 10)) {
      if (task.status === 'completed' && !syncedTaskReplies.current.has(task.run_id)) {
        await invoke('desktop_agent_task_status', { sessionId: id, runId: task.run_id });
        syncedTaskReplies.current.add(task.run_id);
      }
    }
    if (activeSession.current === id && navigationRevision.current === revision) await refreshHistory(id);
  }

  async function selectSession(id: string) {
    const revision = ++navigationRevision.current;
    if (activeSession.current !== id && busy) await invoke('desktop_chat_cancel').catch(() => undefined);
    if (revision !== navigationRevision.current) return;
    activeSession.current = id;
    setSessionId(id);
    activeGeneration.current = null;
    setBusy(false);
    setError('');
    setMessages([]);
    setAgentTasks([]);
    setSelectedWorkspace('');
    try {
      await invoke('desktop_chat_select_session', { sessionId: id });
      if (activeSession.current === id && revision === navigationRevision.current) {
        await refreshAgentTasks(id);
      }
    } catch (reason) { if (activeSession.current === id && revision === navigationRevision.current) setError(String(reason)); }
  }

  async function createSession(projectId?: string) {
    if (mutationPending.current) return;
    mutationPending.current = true; setWorking(true); setError('');
    try {
      if (busy) await invoke('desktop_chat_cancel');
      const id = await invoke<string>('desktop_chat_create_session', { projectId: projectId ?? null });
      await refreshNavigation();
      ++navigationRevision.current;
      ++historyRevision.current;
      activeSession.current = id;
      setSessionId(id);
      setMessages([]);
      setAgentTasks([]);
      setBusy(false);
    } catch (reason) { setError(String(reason)); }
    finally { mutationPending.current = false; setWorking(false); }
  }

  function openDialog(value: EditDialog, initial = '') {
    setDialog(value); setDialogValue(initial); setDialogError(''); setNotice('');
  }

  async function createProject() {
    if (mutationPending.current) return;
    mutationPending.current = true;
    setError(''); setNotice(''); setWorking(true);
    try {
      const rootPath = await invoke<string | null>('desktop_chat_pick_project_directory');
      if (!rootPath) { setNotice('已取消目录选择'); return; }
      const suggested = rootPath.split(/[\\/]/).filter(Boolean).at(-1) || '新项目';
      openDialog({ kind: 'create-project', rootPath }, suggested);
    } catch (reason) { setError(String(reason)); }
    finally { mutationPending.current = false; setWorking(false); }
  }

  async function setSessionProject(session: Session, projectId: string) {
    if (mutationPending.current) return;
    mutationPending.current = true;
    setError(''); setWorking(true);
    try {
      await invoke('desktop_chat_set_session_project', { input: { sessionId: session.id, projectId: projectId || null } });
      await refreshNavigation();
    } catch (reason) { setError(String(reason)); }
    finally { mutationPending.current = false; setWorking(false); }
  }

  async function submitDialog(event: FormEvent) {
    event.preventDefault();
    if (!dialog || mutationPending.current) return;
    mutationPending.current = true; setWorking(true); setDialogError('');
    try {
      switch (dialog.kind) {
        case 'create-project':
          await invoke('desktop_chat_create_project', { input: { rootPath: dialog.rootPath, name: dialogValue } }); break;
        case 'rename-project':
          await invoke('desktop_chat_rename_project', { projectId: dialog.project.id, name: dialogValue }); break;
        case 'delete-project':
          await invoke('desktop_chat_delete_project', { projectId: dialog.project.id }); break;
        case 'rename-session':
          await invoke('desktop_chat_rename_session', { sessionId: dialog.session.id, title: dialogValue }); break;
        case 'delete-session':
          if (activeSession.current === dialog.session.id && busy) await invoke('desktop_chat_cancel');
          await invoke('desktop_chat_delete_session', { sessionId: dialog.session.id });
          if (activeSession.current === dialog.session.id) {
            ++navigationRevision.current; ++historyRevision.current;
            activeSession.current = null; setSessionId(null); setMessages([]); setBusy(false);
          }
          break;
      }
      const list = await refreshNavigation();
      if (!activeSession.current && list[0]) await selectSession(list[0].id);
      setDialog(null);
    } catch (reason) { setDialogError(String(reason)); }
    finally { mutationPending.current = false; setWorking(false); }
  }

  async function send(event: FormEvent) {
    event.preventDefault();
    if (!sessionId || !input.trim() || busy) return;
    const content = input.trim();
    const requestSession = sessionId;
    const requestId = crypto.randomUUID();
    activeGeneration.current = requestId;
    activeSendSession.current = requestSession;
    setInput(''); setError(''); setBusy(true);
    try {
      if (mode !== 'chat') {
        const task = await invoke<AgentTask>('desktop_agent_submit_task', {
          input: { sessionId: requestSession, content, requestId, kind: mode },
        });
        if (activeSession.current === requestSession && activeGeneration.current === requestId) {
          setAgentTasks(current => [task, ...current.filter(item => item.run_id !== task.run_id)].slice(0, 10));
          await refreshHistory(requestSession);
        }
      } else {
        setMessages(current => [...current, { id: crypto.randomUUID(), session_id: requestSession, role: 'user', content, created_at: new Date().toISOString() }]);
        await invoke('desktop_chat_send', { input: { sessionId: requestSession, content, requestId } });
        if (activeSession.current === requestSession && activeGeneration.current === requestId) await refreshHistory(requestSession);
      }
    } catch (reason) {
      if (activeSession.current === requestSession && activeGeneration.current === requestId) {
        setError(String(reason));
        await refreshHistory(requestSession).catch(() => undefined);
      }
    }
    finally {
      if (activeGeneration.current === requestId && activeSendSession.current === requestSession && activeSession.current === requestSession) setBusy(false);
    }
  }

  useEffect(() => {
    if (!sessionId || !activeTaskKey) return;
    let active = true;
    const runIds = activeTaskKey.split(',');
    const refresh = async () => {
      for (const runId of runIds) {
        try {
          const task = await invoke<AgentTask>('desktop_agent_task_status', { sessionId, runId });
          const eventsResponse = await invoke<{ body: { events?: TaskEvent[] } }>('desktop_agent_task_events', {
            sessionId, runId, after: 0,
          });
          task.events = eventsResponse.body.events ?? [];
          if (!active || activeSession.current !== sessionId) return;
          setAgentTasks(current => current.some(item => item.run_id === runId)
            ? current.map(item => item.run_id === runId ? task : item)
            : [task, ...current].slice(0, 10));
          if (task.status === 'completed' && !syncedTaskReplies.current.has(runId)) {
            syncedTaskReplies.current.add(runId);
            await refreshHistory(sessionId);
          }
          if (task.status === 'queued' || task.status === 'running') setBusy(true);
          else setBusy(false);
        } catch (reason) {
          if (active && activeSession.current === sessionId) setError(String(reason));
        }
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 1500);
    return () => { active = false; window.clearInterval(timer); };
  }, [sessionId, activeTaskKey]);

  async function actOnTask(task: AgentTask, action: string, payload: Record<string, unknown> = {}) {
    setError(''); setBusy(true);
    try {
      const response = await invoke<{ body: AgentTask }>('desktop_agent_task_action', {
        sessionId: activeSession.current,
        runId: task.run_id,
        input: { action_id: task.pending_action?.action_id ?? `${task.run_id}:${task.version}:${action}`,
          action, expected_version: task.version, client_request_id: crypto.randomUUID(), payload },
      });
      setAgentTasks(current => [response.body, ...current.filter(item => item.run_id !== task.run_id)]);
      setBusy(response.body.status === 'queued' || response.body.status === 'running');
      setTaskReply('');
    } catch (reason) { setError(String(reason)); setBusy(false); }
  }

  async function loadOlder() {
    const id = activeSession.current;
    const before = messages[0]?.id;
    if (!id || !before || busy) return;
    const revision = historyRevision.current;
    try {
      const page = await invoke<{ messages: Message[]; has_more: boolean }>('desktop_chat_older_history', { sessionId: id, before });
      if (activeSession.current !== id || historyRevision.current !== revision) return;
      setMessages(current => [...page.messages, ...current.filter(message => !page.messages.some(older => older.id === message.id))]);
      setHasOlder(page.has_more);
    } catch (reason) { if (activeSession.current === id) setError(String(reason)); }
  }

  async function saveSettings(event: FormEvent) {
    event.preventDefault(); setError('');
    try {
      await invoke('desktop_chat_configure', { input: { baseUrl, model, apiKey: apiKey || null } });
      setApiKey(''); setConfigured(true); setSettings(false);
    } catch (reason) { setError(String(reason)); }
  }

  async function testConnection() {
    setTestingConnection(true); setError('');
    try { await invoke('desktop_chat_test_connection', { input: { baseUrl, model, apiKey: apiKey || null } }); }
    catch (reason) { setError(String(reason)); }
    finally { setTestingConnection(false); }
  }

  return (
    <main className="desktop-chat-shell" aria-label="普通聊天">
      <ProjectNavigation sessions={sessions} projects={projects} sessionId={sessionId} disabled={initializing || working || dialog !== null}
        onSettings={() => setSettings(true)} onNewSession={projectId => void createSession(projectId)}
        onNewProject={() => void createProject()} onSelectSession={id => void selectSession(id)}
        onRenameSession={session => openDialog({ kind: 'rename-session', session }, session.title)}
        onDeleteSession={session => openDialog({ kind: 'delete-session', session })}
        onMoveSession={(session, projectId) => void setSessionProject(session, projectId)}
        onRenameProject={project => openDialog({ kind: 'rename-project', project }, project.name)}
        onDeleteProject={project => openDialog({ kind: 'delete-project', project })} />
      <section className="desktop-chat-main" aria-label="聊天内容">
        <header className="desktop-chat-context"><span>{projects.find(project => project.id === sessions.find(session => session.id === sessionId)?.project_id)?.name ?? '独立会话'}</span></header>
        <div className="desktop-chat-messages" aria-live="polite">
          {hasOlder && <button type="button" disabled={busy} onClick={() => void loadOlder()}>加载更早消息</button>}
          {messages.length === 0 && <div className="desktop-chat-empty"><h2>开始一段对话</h2><p>{configured ? '发送消息开始聊天' : '模型尚未配置'}</p></div>}
          {messages.map(message => <article className={`desktop-chat-message ${message.role}`} key={message.id}><strong>{message.role === 'user' ? '你' : 'Tomato Agent'}</strong><p>{message.content}</p></article>)}
          {agentTasks.map(task => {
            const clarification = task.pending_action?.action_type === 'clarification';
            const choices = Array.isArray(task.pending_action?.display_data.choices)
              ? task.pending_action.display_data.choices.filter((item): item is string => typeof item === 'string') : [];
            const answer = messages.some(message => message.id === `agent-assistant-${task.run_id}`)
              ? undefined : task.result?.answer;
            const outlineValue = task.pending_action?.display_data.outline;
            const writingOutline = outlineValue && typeof outlineValue === 'object'
              ? outlineValue as Record<string, unknown> : null;
            return <article className="desktop-agent-task" key={task.run_id} aria-label={task.kind === 'writing' ? '写作任务' : '助手任务'}>
              <header><strong>{task.kind === 'writing' ? '写作任务' : '助手任务'}</strong><span>{task.status === 'queued' ? '排队中' : task.status === 'running' ? '处理中' : task.status === 'waiting' ? '等待操作' : task.status === 'completed' ? '已完成' : task.status === 'cancelled' ? '已取消' : '未完成'}</span></header>
              {task.phase && <p className="desktop-agent-task-phase">{task.phase}</p>}
              {task.events?.slice(-4).map(item => <p className="desktop-agent-task-event" key={item.sequence}>{item.summary ?? item.event_type}</p>)}
              {task.pending_action && <div className="desktop-agent-task-action">
                <p>{task.pending_action.reason}</p>
                {clarification && <form onSubmit={event => { event.preventDefault(); if (taskReply.trim()) void actOnTask(task, 'provide_input', { message: taskReply.trim() }); }}>
                  {choices.length > 0 && <label>可选回答<select value={taskReply} onChange={event => setTaskReply(event.target.value)}><option value="">选择一项</option>{choices.map(choice => <option key={choice} value={choice}>{choice}</option>)}</select></label>}
                  <label>补充信息<textarea value={taskReply} onChange={event => setTaskReply(event.target.value)} rows={2} /></label>
                  <button type="submit" disabled={!taskReply.trim()}>提交回答</button>
                </form>}
                {task.kind === 'writing' && task.pending_action.reason === 'confirm_outline' && writingOutline &&
                  <button type="button" onClick={() => void actOnTask(task, 'confirm_outline', { outline: writingOutline })}>确认提纲并生成草稿</button>}
                {task.kind === 'writing' && task.pending_action.reason === 'save_draft' &&
                  <button type="button" onClick={() => void actOnTask(task, 'save_draft')}>确认保存草稿</button>}
                {task.capabilities.includes('bind_workspace') && <form onSubmit={event => {
                  event.preventDefault(); if (selectedWorkspace) void actOnTask(task, 'bind_workspace', { workspace_id: selectedWorkspace });
                }}>
                  <label>授权项目目录<select value={selectedWorkspace} onChange={event => setSelectedWorkspace(event.target.value)}>
                    <option value="">选择已授权目录</option>{engineWorkspaces.map(workspace => <option key={workspace.workspace_id} value={workspace.workspace_id}>
                      {workspace.repo_path ?? workspace.workspace_id}</option>)}
                  </select></label>
                  <button type="submit" disabled={!selectedWorkspace}>绑定并继续</button>
                  <button type="button" onClick={() => void (async () => {
                    try {
                      const path = await invoke<string | null>('desktop_chat_pick_project_directory');
                      if (!path) return;
                      const workspace = await invoke<EngineWorkspace>('desktop_agent_register_workspace', { path });
                      setEngineWorkspaces(current => [...current.filter(item => item.workspace_id !== workspace.workspace_id), workspace]);
                      setSelectedWorkspace(workspace.workspace_id);
                    } catch (reason) { setError(String(reason)); }
                  })()}>选择并授权新目录</button>
                </form>}
              </div>}
              {task.result?.draft && <div className="desktop-agent-task-answer"><p>{task.result.draft}</p></div>}
              {answer && <div className="desktop-agent-task-answer"><p>{answer}</p>
                {!!task.result?.citations?.length && <div className="desktop-agent-task-meta"><strong>参考资料</strong>{task.result.citations.map((citation, index) => {
                  const item = citation && typeof citation === 'object' ? citation as Record<string, unknown> : {};
                  return <p key={String(item.citation_id ?? index)}>{String(item.title ?? '资料')} · {String(item.heading_path ?? item.source_path ?? '')}</p>;
                })}</div>}
                {!!task.result?.artifacts?.length && <div className="desktop-agent-task-meta"><strong>产物</strong>{task.result.artifacts.map((artifact, index) => {
                  const item = artifact && typeof artifact === 'object' ? artifact as Record<string, unknown> : {};
                  return <p key={String(item.artifact_id ?? index)}>{String(item.relative_path ?? item.kind ?? '文件')}</p>;
                })}</div>}
              </div>}
              {task.error?.code && <p className="desktop-agent-task-error">任务未完成（{task.error.code}）</p>}
              {task.capabilities.includes('cancel') && <button type="button" className="secondary-button" onClick={() => void actOnTask(task, 'cancel')}>取消任务</button>}
            </article>;
          })}
        </div>
        <div className="desktop-chat-feedback">{notice && <p role="status">{notice}</p>}{error && <p className="desktop-chat-error" role="alert">{error}</p>}</div>
        <div className="desktop-chat-mode" role="group" aria-label="对话模式">
          <button type="button" aria-pressed={mode === 'chat'} disabled={busy} onClick={() => setMode('chat')}>普通聊天</button>
          <button type="button" aria-pressed={mode === 'assistant'} disabled={busy} onClick={() => setMode('assistant')}>助手任务</button>
          <button type="button" aria-pressed={mode === 'writing'} disabled={busy} onClick={() => setMode('writing')}>写作</button>
        </div>
        <form className="desktop-chat-composer" onSubmit={send}>
          <textarea aria-label={mode === 'chat' ? '消息' : mode === 'writing' ? '写作主题' : '任务内容'} placeholder={!configured ? '请先配置模型' : mode === 'chat' ? '输入消息' : mode === 'writing' ? '描述写作主题' : '描述需要完成的任务'} value={input} onChange={event => setInput(event.target.value)} disabled={!configured || !sessionId || busy} />
          {busy ? <button type="button" onClick={() => mode !== 'chat'
            ? (() => { const task = agentTasks.find(item => ['queued', 'running'].includes(item.status) && item.capabilities.includes('cancel')); if (task) void actOnTask(task, 'cancel'); })()
            : void invoke('desktop_chat_cancel')}>{mode !== 'chat' ? '取消任务' : '停止'}</button> : <button type="submit" disabled={!configured || !sessionId || !input.trim()}>{mode === 'chat' ? '发送' : mode === 'writing' ? '开始写作' : '开始任务'}</button>}
        </form>
      </section>
      {dialog && <div className="desktop-chat-backdrop"><form className="desktop-chat-settings" role="dialog" aria-modal="true"
        aria-labelledby="chat-project-dialog-title" onSubmit={submitDialog} onKeyDown={event => { if (event.key === 'Escape' && !working) setDialog(null); }}>
        <header><h2 id="chat-project-dialog-title">{dialog.kind === 'create-project' ? '新建项目' : dialog.kind === 'rename-project' ? '重命名项目' : dialog.kind === 'delete-project' ? '删除项目' : dialog.kind === 'rename-session' ? '重命名对话' : '删除对话'}</h2></header>
        {dialog.kind === 'create-project' && <p className="desktop-chat-project-path">{dialog.rootPath}</p>}
        {dialog.kind === 'delete-project' ? <p>删除“{dialog.project.name}”？其中的会话会回到独立会话列表，磁盘目录将保留。</p>
          : dialog.kind === 'delete-session' ? <p>删除“{dialog.session.title}”及其全部消息？此操作无法撤销。</p>
          : <label>{dialog.kind === 'rename-session' ? '对话名称' : '项目名称'}<input autoFocus required maxLength={120} value={dialogValue} disabled={working} onChange={event => setDialogValue(event.target.value)} /></label>}
        {dialogError && <p role="alert" className="desktop-chat-error">{dialogError}</p>}
        <footer><button type="button" className="secondary-button" disabled={working} onClick={() => setDialog(null)}>取消</button>
          <button type="submit" disabled={working || (!dialog.kind.startsWith('delete') && !dialogValue.trim())}>{working ? '正在保存…' : dialog.kind.startsWith('delete') ? '确认删除' : '保存'}</button></footer>
      </form></div>}
      {settings && <div className="desktop-chat-backdrop" role="presentation"><form className="desktop-chat-settings" onSubmit={saveSettings}>
        <header><h2>模型设置</h2><button type="button" onClick={() => setSettings(false)} aria-label="关闭">关闭</button></header>
        <label>API Base URL<input type="url" required value={baseUrl} onChange={event => setBaseUrl(event.target.value)} /></label>
        <label>模型名称<input required value={model} onChange={event => setModel(event.target.value)} /></label>
        <label>API Key<input type="password" autoComplete="new-password" value={apiKey} onChange={event => setApiKey(event.target.value)} /></label>
        <p>API Key 保存在 Windows 凭据库中。</p>
        <footer>{testingConnection ? <button type="button" onClick={() => void invoke('desktop_chat_cancel_connection_test')}>取消测试</button> : <button type="button" onClick={() => void testConnection()}>测试连接</button>}<button type="button" onClick={() => void invoke('desktop_chat_delete_credential').then(() => setConfigured(false)).catch(reason => setError(String(reason)))}>删除凭据</button><button type="submit">保存设置</button></footer>
      </form></div>}
    </main>
  );
}
