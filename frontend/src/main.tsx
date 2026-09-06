import { StrictMode, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';

type Message = { role: 'user' | 'assistant'; content: string };
const API = 'http://127.0.0.1:8000';

function App() {
  const [session, setSession] = useState(''); const [input, setInput] = useState('');
  const [messages, setMessages] = useState<Message[]>([]); const [status, setStatus] = useState('未创建 Session');
  async function send() {
    if (!input.trim()) return; const text = input.trim(); setInput(''); setMessages((m) => [...m, { role: 'user', content: text }]); setStatus('处理中');
    let id = session;
    if (!id) { const response = await fetch(`${API}/api/sessions`, { method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}' }); id = (await response.json()).session_id; setSession(id); }
    const response = await fetch(`${API}/api/sessions/${id}/runs`, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ message: text }) });
    const result = await response.json(); setStatus(result.status); setMessages((m) => [...m, { role: 'assistant', content: result.message ?? result.answer ?? '无响应' }]);
  }
  return <main><section className="card"><header><h1>Tomato Agent</h1><span>{status}</span></header><div className="messages">{messages.map((m, i) => <div className={`message ${m.role}`} key={i}><b>{m.role === 'user' ? '你' : 'Agent'}</b><p>{m.content}</p></div>)}</div><div className="composer"><input value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && send()} placeholder="输入消息…"/><button onClick={send}>发送</button></div>{session && <small>Session: {session}</small>}</section></main>;
}
createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>);