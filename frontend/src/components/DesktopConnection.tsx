import { useEffect, useState } from 'react';
import { ConversationShell } from './ConversationShell';

type Status = { state: 'disconnected' | 'connecting' | 'ready' | 'error'; summary: string; detail: string };
type NativeWindow = Window & { __TAURI__?: {
  core: { invoke<T>(command: string, args?: Record<string, unknown>): Promise<T> };
  event: { listen(name: string, callback: () => void): Promise<() => void> };
} };
const native = () => (window as NativeWindow).__TAURI__!;

export function DesktopConnection() {
  const [status, setStatus] = useState<Status>({ state: 'disconnected', summary: '尚未连接助手服务', detail: '' });
  const [environments, setEnvironments] = useState<string[]>([]);
  const [distribution, setDistribution] = useState('');
  const [backendPath, setBackendPath] = useState('');
  const [closing, setClosing] = useState(false);
  const [closeDetail, setCloseDetail] = useState('');
  const [epoch, setEpoch] = useState(0);
  const invoke = <T,>(command: string, args?: Record<string, unknown>) => native().core.invoke<T>(command, args);

  useEffect(() => {
    let mounted = true;
    const listener = native().event.listen('desktop-close-requested', () => {
      setClosing(true);
      setCloseDetail('正在核查后台任务…');
      void invoke<{ active_tasks: number }>('connection_facts').then(facts => {
        if (mounted) setCloseDetail(facts.active_tasks > 0
          ? `仍有 ${facts.active_tasks} 个任务运行或等待。退出会保留后台任务，重新打开后查询原任务。`
          : '退出客户端。仅空闲、由本应用启动且无其他连接的服务会受控关闭。');
      }).catch(() => { if (mounted) setCloseDetail('无法确认服务状态。请保留现场，核查连接后再退出。'); });
    });
    const timer = window.setInterval(() => {
      void invoke<Status>('connection_status').then(value => { if (mounted) setStatus(value); });
    }, 2000);
    return () => { mounted = false; window.clearInterval(timer); void listener.then(stop => stop()); };
  }, []);

  async function discover() {
    try {
      const values = await invoke<string[]>('distributions');
      setEnvironments(values);
      setDistribution(values[0] ?? '');
      if (!values.length) setStatus({ state: 'error', summary: '未发现 WSL 发行版', detail: '请先准备 WSL 和后端环境。' });
    } catch (error) { setStatus({ state: 'error', summary: 'WSL 发现失败', detail: String(error) }); }
  }

  async function connect() {
    setStatus({ state: 'connecting', summary: '正在连接助手服务…', detail: '' });
    try {
      setStatus(await invoke<Status>('connect_service', { distribution, backendPath }));
      // Remount loads existing sessions/tasks; it never submits a replacement Run.
      setEpoch(value => value + 1);
    } catch (error) { setStatus({ state: 'error', summary: '连接失败，请核查高级连接设置后手动重连', detail: String(error) }); }
  }

  return <>
    <header className="border-b p-3 space-y-2">
      <p role="status">{status.summary}</p>
      {status.detail && <details><summary>连接详情</summary><p>{status.detail}</p></details>}
      {status.state !== 'ready' && <>
        <p>选择已准备后端的 WSL 环境。切换环境将切换数据源，会话不会自动搬迁。</p>
        <button onClick={discover} disabled={status.state === 'connecting'}>发现 WSL 发行版</button>
        <label> WSL 环境 <select value={distribution} onChange={e => setDistribution(e.target.value)}>
          {environments.map(value => <option key={value}>{value}</option>)}
        </select></label>
        <label className="block">高级连接设置：已有后端的 WSL 绝对路径
          <input className="border p-2 w-full" value={backendPath} onChange={e => setBackendPath(e.target.value)} />
        </label>
        <button onClick={connect} disabled={!distribution || !backendPath || status.state === 'connecting'}>连接 / 重连助手服务</button>
      </>}
      {status.state === 'ready' && <button onClick={() => void invoke('close_service').then(() => setStatus({ state: 'disconnected', summary: '已断开连接，任务保留在原环境', detail: '' }))}>断开连接</button>}
    </header>
    {status.state === 'ready' && <ConversationShell key={epoch} />}
    {closing && <section role="dialog" aria-modal="true" aria-label="退出应用" className="fixed inset-0 bg-white p-10 z-50">
      <p>{closeDetail}</p>
      <button onClick={() => setClosing(false)}>返回应用</button>
      <button onClick={() => void invoke('exit_desktop').catch(error => setCloseDetail(String(error)))}>保留后台任务并退出</button>
    </section>}
  </>;
}