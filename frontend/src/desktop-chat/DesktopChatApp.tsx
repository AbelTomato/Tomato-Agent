import { useEffect, useRef, useState } from 'react';
import { listen } from '@tauri-apps/api/event';
import { invoke } from '@tauri-apps/api/core';
import type { FormEvent } from 'react';
import { ProjectNavigation } from './ProjectNavigation';
import type { ChatProject as Project, ChatSession as Session } from './ProjectNavigation';

type Message = { id: string; session_id: string; role: 'user' | 'assistant'; content: string; created_at: string };
type Delta = { session_id: string; delta: string };
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
  const [input, setInput] = useState('');
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

  useEffect(() => {
    let active = true;
    void invoke<{ configured: boolean }>('desktop_chat_status').then(value => { if (active) setConfigured(value.configured); })
      .catch(reason => { if (active) setError(String(reason)); });
    const unlisten = listen<Delta>('desktop_chat_delta', event => {
      if (event.payload.session_id !== activeSession.current) return;
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
    const completed = listen<{ session_id: string }>('desktop_chat_completed', event => {
      if (event.payload.session_id === activeSession.current) setBusy(false);
    });
    const cancelled = listen<{ session_id: string }>('desktop_chat_cancelled', event => {
      if (event.payload.session_id !== activeSession.current) return;
      setBusy(false);
      void refreshHistory(event.payload.session_id).catch(reason => setError(String(reason)));
    });
    const failed = listen<{ session_id: string; error: string }>('desktop_chat_failed', event => {
      if (event.payload.session_id !== activeSession.current) return;
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
      if (activeSession.current === id && historyRevision.current === revision) setMessages(history);
    } catch (reason) {
      if (activeSession.current === id && historyRevision.current === revision) throw reason;
    }
  }

  async function selectSession(id: string) {
    const revision = ++navigationRevision.current;
    if (activeSession.current !== id && busy) await invoke('desktop_chat_cancel').catch(() => undefined);
    if (revision !== navigationRevision.current) return;
    activeSession.current = id;
    setSessionId(id);
    setBusy(false);
    setError('');
    setMessages([]);
    try {
      await invoke('desktop_chat_select_session', { sessionId: id });
      if (activeSession.current === id && revision === navigationRevision.current) await refreshHistory(id);
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
    activeSendSession.current = requestSession;
    setInput(''); setError(''); setBusy(true);
    setMessages(current => [...current, { id: crypto.randomUUID(), session_id: sessionId, role: 'user', content, created_at: new Date().toISOString() }]);
    try {
      await invoke('desktop_chat_send', { input: { sessionId: requestSession, content } });
      if (activeSession.current === requestSession) await refreshHistory(requestSession);
    } catch (reason) {
      if (activeSession.current === requestSession) {
        setError(String(reason));
        await refreshHistory(requestSession).catch(() => undefined);
      }
    }
    finally {
      if (activeSendSession.current === requestSession && activeSession.current === requestSession) setBusy(false);
    }
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
          {messages.length === 0 && <div className="desktop-chat-empty"><h2>开始一段对话</h2><p>{configured ? '发送消息开始聊天' : '模型尚未配置'}</p></div>}
          {messages.map(message => <article className={`desktop-chat-message ${message.role}`} key={message.id}><strong>{message.role === 'user' ? '你' : 'Tomato Agent'}</strong><p>{message.content}</p></article>)}
        </div>
        <div className="desktop-chat-feedback">{notice && <p role="status">{notice}</p>}{error && <p className="desktop-chat-error" role="alert">{error}</p>}</div>
        <form className="desktop-chat-composer" onSubmit={send}>
          <textarea aria-label="消息" placeholder={configured ? '输入消息' : '请先配置模型'} value={input} onChange={event => setInput(event.target.value)} disabled={!configured || !sessionId || busy} />
          {busy ? <button type="button" onClick={() => void invoke('desktop_chat_cancel')}>停止</button> : <button type="submit" disabled={!configured || !sessionId || !input.trim()}>发送</button>}
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
