import { useState } from 'react';

type NativeWindow = Window & {
  __TAURI__?: { core: { invoke<T>(command: string, args?: Record<string, unknown>): Promise<T> } };
};

export const isDesktop = () => Boolean((window as NativeWindow).__TAURI__);

export function DesktopProbe() {
  const [environments, setEnvironments] = useState<string[]>([]);
  const [distribution, setDistribution] = useState('');
  const [backendPath, setBackendPath] = useState('');
  const [result, setResult] = useState('');
  const [busy, setBusy] = useState(false);
  const invoke = <T,>(command: string, args?: Record<string, unknown>) =>
    (window as NativeWindow).__TAURI__!.core.invoke<T>(command, args);
  async function discover() {
    setBusy(true);
    try {
      const items = await invoke<string[]>('distributions');
      setEnvironments(items);
      setDistribution(items[0] ?? '');
      setResult(items.length ? '请选择环境并填写高级连接路径。' : '未发现已安装的 WSL 发行版。');
    } catch (error) { setResult(String(error)); }
    finally { setBusy(false); }
  }
  async function verify() {
    setBusy(true);
    setResult('正在验证 Windows 凭据与 WSL 连接…');
    try {
      const value = await invoke<unknown>('probe', { distribution, backendPath });
      setResult(JSON.stringify(value, null, 2));
    } catch (error) { setResult(String(error)); }
    finally { setBusy(false); }
  }
  return <main className="mx-auto max-w-3xl p-8 space-y-5">
    <h1 className="text-2xl font-bold">Windows 桌面技术验证</h1>
    <p>任务1验证入口：启动隔离的只读 WSL 探针，不调用模型。探针最多运行120秒，验证结果不代表完整助手服务已接通。</p>
    <button disabled={busy} onClick={discover}>发现 WSL 发行版</button>
    <label className="block">WSL 环境 <select value={distribution} onChange={e => setDistribution(e.target.value)}>
      {environments.map(item => <option key={item}>{item}</option>)}
    </select></label>
    <label className="block">高级连接设置：已有后端的 WSL 绝对路径
      <input className="block w-full border p-2" value={backendPath} onChange={e => setBackendPath(e.target.value)} placeholder="选择已有 backend 安装目录" />
    </label>
    <button disabled={busy || !distribution || !backendPath} onClick={verify}>验证认证、目录与原生凭据桥</button>
    <pre className="whitespace-pre-wrap break-all border p-4" role="status">{result}</pre>
  </main>;
}