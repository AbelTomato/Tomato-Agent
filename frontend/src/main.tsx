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
    try {
      let id = session;
      if (!id) {
        const response = await fetch(`${API}/api/sessions`, { method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}' });
        if (!response.ok) throw new Error(`创建 Session 失败：HTTP ${response.status}`);
        const data = await response.json();
        id = data.session_id;
        if (!id) throw new Error('后端未返回 session_id');
        setSession(id);
      }
      const response = await fetch(`${API}/api/sessions/${id}/runs`, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ message: text }) });
      if (!response.ok) throw new Error(`发送消息失败：HTTP ${response.status}`);
      const result = await response.json();
      setStatus(result.status);
      setMessages((m) => [...m, { role: 'assistant', content: result.message ?? result.answer ?? result.error?.message ?? '无响应' }]);
    } catch (error) {
      const message = error instanceof Error ? error.message : '请求失败';
      setStatus('失败');
      setMessages((m) => [...m, { role: 'assistant', content: message }]);
    }
  }
  return <main><section className="card"><header><h1>Tomato Agent</h1><span>{status}</span></header><div className="messages">{messages.map((m, i) => <div className={`message ${m.role}`} key={i}><b>{m.role === 'user' ? '你' : 'Agent'}</b><p>{m.content}</p></div>)}</div><div className="composer"><input value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && send()} placeholder="输入消息…"/><button onClick={send}>发送</button></div>{session && <small>Session: {session}</small>}</section></main>;
}
createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>);