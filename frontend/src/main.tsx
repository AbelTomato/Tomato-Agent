import { StrictMode, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import {
  ApiError,
  createSession,
  loadDocument,
  loadKnowledgeCapabilities,
  loadMessages,
  sendBasicMessage,
  sendKnowledgeMessage,
  type Citation,
  type ConversationMessage,
  type KnowledgeDocument,
  type RetrievalMode,
} from './api';
import { CitationList } from './components/CitationList';
import { WritingPanel } from './components/WritingPanel';
import './style.css';

type ChatMode = 'knowledge' | 'basic';
type RequestStatus = 'loading-history' | 'idle' | 'sending' | 'success' | 'failure' | 'no-results';

const SESSION_STORAGE_KEY = 'tomato-agent-session-id';

function statusLabel(status: RequestStatus): string {
  return {
    'loading-history': '正在恢复历史消息', idle: '就绪', sending: '正在请求',
    success: '回答已返回', failure: '请求失败', 'no-results': '材料不足',
  }[status];
}

function App() {
  const [sessionId, setSessionId] = useState('');
  const [input, setInput] = useState('');
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [mode, setMode] = useState<ChatMode>('knowledge');
  const [retrievalMode, setRetrievalMode] = useState<RetrievalMode>('keyword');
  const [availableRetrievalModes, setAvailableRetrievalModes] = useState<RetrievalMode[]>(['keyword']);
  const [capabilityNotice, setCapabilityNotice] = useState('');
  const [status, setStatus] = useState<RequestStatus>('idle');
  const [error, setError] = useState('');
  const [document, setDocument] = useState<KnowledgeDocument | null>(null);
  const [documentError, setDocumentError] = useState('');

  useEffect(() => {
    let cancelled = false;
    void loadKnowledgeCapabilities().then((capabilities) => {
      if (cancelled) return;
      setAvailableRetrievalModes(capabilities.retrieval_modes);
      setRetrievalMode((current) => capabilities.retrieval_modes.includes(current) ? current : 'keyword');
      setCapabilityNotice(capabilities.embedding_configured
        ? `已启用 Embedding：${capabilities.embedding_model ?? '未命名'}（${capabilities.embedding_dimensions ?? '?'} 维）`
        : '当前仅启用关键词检索，Embedding 服务尚未被后端识别。');
    }).catch((reason: unknown) => {
      if (!cancelled) {
        setAvailableRetrievalModes(['keyword']);
        setRetrievalMode('keyword');
        setCapabilityNotice(reason instanceof Error ? `无法读取检索能力：${reason.message}` : '无法读取检索能力，已退回关键词检索。');
      }
    });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    const storedSessionId = window.localStorage.getItem(SESSION_STORAGE_KEY);
    if (!storedSessionId) return;
    let cancelled = false;
    setSessionId(storedSessionId);
    setStatus('loading-history');
    void loadMessages(storedSessionId).then(({ messages: history }) => {
      if (!cancelled) { setMessages(history); setError(''); setStatus('idle'); }
    }).catch((reason: unknown) => {
      if (cancelled) return;
      if (reason instanceof ApiError && reason.status === 404) {
        window.localStorage.removeItem(SESSION_STORAGE_KEY);
        setSessionId(''); setMessages([]); setError('保存的会话已失效，请新建会话后继续。');
      } else setError(reason instanceof Error ? reason.message : '历史消息恢复失败。');
      setStatus('failure');
    });
    return () => { cancelled = true; };
  }, []);

  function resetSession() {
    window.localStorage.removeItem(SESSION_STORAGE_KEY);
    setSessionId(''); setMessages([]); setDocument(null); setDocumentError(''); setError(''); setStatus('idle');
  }

  async function ensureSession(): Promise<string> {
    if (sessionId) return sessionId;
    const newSessionId = await createSession();
    window.localStorage.setItem(SESSION_STORAGE_KEY, newSessionId);
    setSessionId(newSessionId);
    return newSessionId;
  }

  async function send() {
    const text = input.trim();
    if (!text || status === 'sending' || status === 'loading-history') return;
    setInput(''); setError(''); setMessages((current) => [...current, { role: 'user', content: text }]); setStatus('sending');
    try {
      const id = await ensureSession();
      if (mode === 'knowledge') {
        const result = await sendKnowledgeMessage(id, text, retrievalMode);
        setMessages((current) => [...current, { role: 'assistant', content: result.answer || (result.evidence_status === 'no_results' ? '未找到足够的相关材料。' : '无响应'), citations: result.citations, evidence_status: result.evidence_status, retrieval_mode: result.retrieval_mode }]);
        setStatus(result.evidence_status === 'no_results' ? 'no-results' : 'success');
      } else {
        const result = await sendBasicMessage(id, text);
        setMessages((current) => [...current, { role: 'assistant', content: result.answer ?? result.message ?? '无响应' }]);
        setStatus('success');
      }
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : '请求失败，请稍后重试。');
      setStatus('failure');
    }
  }

  async function openDocument(citation: Citation) {
    setDocument(null); setDocumentError('');
    try { setDocument(await loadDocument(citation.document_id)); }
    catch (reason: unknown) { setDocumentError(reason instanceof Error ? reason.message : '无法加载原文。'); }
  }

  return <main>
    <section className="card" aria-label="Tomato Agent 对话">
      <header><div><h1>Tomato Agent</h1><p>知识问答会展示可核验来源；基础聊天不将 Mock 搜索标记为来源。</p></div><span className={`status status-${status}`}>{statusLabel(status)}</span></header>
      <div className="controls">
        <label>对话模式<select value={mode} onChange={(event) => setMode(event.target.value as ChatMode)} disabled={status === 'sending'}><option value="knowledge">知识问答</option><option value="basic">基础聊天</option></select></label>
        {mode === 'knowledge' && <label>检索方式<select value={retrievalMode} onChange={(event) => setRetrievalMode(event.target.value as RetrievalMode)} disabled={status === 'sending'}>{availableRetrievalModes.map((item) => <option key={item} value={item}>{item === 'keyword' ? '关键词' : item === 'vector' ? '向量' : '混合'}</option>)}</select></label>}
        <button className="secondary-button" type="button" onClick={resetSession} disabled={status === 'sending'}>新建会话</button>
      </div>
      {mode === 'knowledge' && <small className="capability-note">{capabilityNotice}</small>}
      {error && <p className="notice error">{error}</p>}
      {status === 'no-results' && <p className="notice">本轮未检索到足够材料，回答不应视为有来源支撑。</p>}
      <div className="messages" aria-live="polite">
        {messages.length === 0 && <p className="empty-state">选择“知识问答”后提问，即可获得带来源的回答。</p>}
        {messages.map((message, index) => <article className={`message ${message.role}`} key={`${message.role}-${index}`}><b>{message.role === 'user' ? '你' : 'Agent'}</b><p>{message.content}</p>{message.role === 'assistant' && message.evidence_status === 'no_results' && <span className="evidence-warning">材料不足</span>}{message.role === 'assistant' && message.citations && <CitationList citations={message.citations} onOpenDocument={openDocument} />}</article>)}
      </div>
      <div className="composer"><input value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') void send(); }} placeholder={mode === 'knowledge' ? '向知识库提问…' : '输入基础聊天消息…'} disabled={status === 'sending' || status === 'loading-history'} /><button type="button" onClick={() => void send()} disabled={!input.trim() || status === 'sending' || status === 'loading-history'}>{status === 'sending' ? '发送中…' : '发送'}</button></div>
      {sessionId && <small>当前 Session：{sessionId}</small>}
    </section>
    <WritingPanel sessionId={sessionId} onRequireSession={ensureSession} />
    {(document || documentError) && <section className="document-panel" aria-label="已导入原文"><button className="close-button" type="button" onClick={() => { setDocument(null); setDocumentError(''); }}>关闭</button>{documentError ? <p className="notice error">{documentError}</p> : document && <><h2>{document.document.title}</h2><p className="document-meta">{document.document.source_path} · 版本 {document.document.document_version}</p>{document.chunks.map((chunk) => <article className="document-chunk" key={chunk.chunk_id}>{chunk.heading_path && <h3>{chunk.heading_path}</h3>}<span>第 {chunk.start_line}–{chunk.end_line} 行</span><p>{chunk.text}</p></article>)}</>}</section>}
  </main>;
}

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>);