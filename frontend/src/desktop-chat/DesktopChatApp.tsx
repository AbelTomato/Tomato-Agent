import { useEffect, useRef, useState } from 'react';
import { listen } from '@tauri-apps/api/event';
import { invoke } from '@tauri-apps/api/core';
import type { FormEvent } from 'react';

type Message = { id: string; session_id: string; role: 'user' | 'assistant'; content: string; created_at: string };
type Delta = { session_id: string; delta: string };

export function DesktopChatApp() {
  const [configured, setConfigured] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [settings, setSettings] = useState(false);
  const [baseUrl, setBaseUrl] = useState('https://api.openai.com/v1');
  const [model, setModel] = useState('');
  const [apiKey, setApiKey] = useState('');
  const activeSession = useRef<string | null>(null);

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
      setBusy(false);
    });
    const cancelled = listen<{ session_id: string }>('desktop_chat_cancelled', event => {
      setBusy(false);
    });
    const failed = listen<{ session_id: string; error: string }>('desktop_chat_failed', event => {
      setBusy(false);
      setError(event.payload.error);
    });
    void invoke<string | null>('desktop_chat_latest_session').then(async id => {
      if (!active || !id) return;
      const history = await invoke<Message[]>('desktop_chat_history', { sessionId: id });
      if (active) { activeSession.current = id; setSessionId(id); setMessages(history); }
    }).catch(reason => { if (active) setError(String(reason)); });
    return () => { active = false; void completed.then(stop => stop()); void cancelled.then(stop => stop()); void failed.then(stop => stop()); };
  }, []);

  async function createSession() {
    setError('');
    try {
      const id = await invoke<string>('desktop_chat_create_session');
      activeSession.current = id;
      setSessionId(id);
      setMessages([]);
    } catch (reason) { setError(String(reason)); }
  }

  async function send(event: FormEvent) {
    event.preventDefault();
    if (!sessionId || !input.trim() || busy) return;
    const content = input.trim();
    setInput(''); setError(''); setBusy(true);
    setMessages(current => [...current, { id: crypto.randomUUID(), session_id: sessionId, role: 'user', content, created_at: new Date().toISOString() }]);
    try {
      await invoke('desktop_chat_send', { input: { sessionId, content } });
      const history = await invoke<Message[]>('desktop_chat_history', { sessionId });
      setMessages(history);
    } catch (reason) {
      setError(String(reason));
      const history = await invoke<Message[]>('desktop_chat_history', { sessionId }).catch(() => null);
      if (history) setMessages(history);
    }
    finally { setBusy(false); }
  }

  async function saveSettings(event: FormEvent) {
    event.preventDefault(); setError('');
    try {
      await invoke('desktop_chat_configure', { input: { baseUrl, model, apiKey: apiKey || null } });
      setApiKey(''); setConfigured(true); setSettings(false);
    } catch (reason) { setError(String(reason)); }
  }

  return (
    <main className="desktop-chat-shell" aria-label="普通聊天">
      <aside className="desktop-chat-sidebar">
        <header><h1>Tomato Agent</h1><button type="button" onClick={() => setSettings(true)} aria-label="模型设置">设置</button></header>
        <button type="button" onClick={() => void createSession()}>新建对话</button>
        {sessionId ? <button type="button" className="desktop-chat-session active">新对话</button> : <p>尚无对话</p>}
      </aside>
      <section className="desktop-chat-main" aria-label="聊天内容">
        <div className="desktop-chat-messages" aria-live="polite">
          {configured && <p className="desktop-chat-provider-note">阶段 1 验收模式：Fake Provider，不会请求真实模型</p>}
          {messages.length === 0 && <div className="desktop-chat-empty"><h2>开始一段对话</h2><p>{configured ? '发送消息开始聊天' : '模型尚未配置'}</p></div>}
          {messages.map(message => <article className={`desktop-chat-message ${message.role}`} key={message.id}><strong>{message.role === 'user' ? '你' : 'Tomato Agent'}</strong><p>{message.content}</p></article>)}
        </div>
        {error && <p className="desktop-chat-error" role="alert">{error}</p>}
        <form className="desktop-chat-composer" onSubmit={send}>
          <textarea aria-label="消息" placeholder={configured ? '输入消息' : '请先配置模型'} value={input} onChange={event => setInput(event.target.value)} disabled={!configured || !sessionId || busy} />
          {busy ? <button type="button" onClick={() => void invoke('desktop_chat_cancel')}>停止</button> : <button type="submit" disabled={!configured || !sessionId || !input.trim()}>发送</button>}
        </form>
      </section>
      {settings && <div className="desktop-chat-backdrop" role="presentation"><form className="desktop-chat-settings" onSubmit={saveSettings}>
        <header><h2>模型设置</h2><button type="button" onClick={() => setSettings(false)} aria-label="关闭">关闭</button></header>
        <label>API Base URL<input type="url" required value={baseUrl} onChange={event => setBaseUrl(event.target.value)} /></label>
        <label>模型名称<input required value={model} onChange={event => setModel(event.target.value)} /></label>
        <label>API Key<input type="password" autoComplete="new-password" value={apiKey} onChange={event => setApiKey(event.target.value)} /></label>
        <p>API Key 保存在 Windows 凭据库中。</p>
        <footer><button type="button" onClick={() => void invoke('desktop_chat_delete_credential').then(() => setConfigured(false)).catch(reason => setError(String(reason)))}>删除凭据</button><button type="submit">保存设置</button></footer>
      </form></div>}
    </main>
  );
}