import { useEffect, useState } from 'react';
import { ConversationShell } from './ConversationShell';

type Status = { state: 'disconnected' | 'connecting' | 'ready' | 'error'; summary: string; detail: string };
type Preferences = { distribution: string; backend_path: string };
type NativeWindow = Window & { __TAURI__?: {
  core: { invoke<T>(command: string, args?: Record<string, unknown>): Promise<T> };
  event: { listen(name: string, callback: () => void): Promise<() => void> };
} };
const native = () => (window as NativeWindow).__TAURI__!;
const emptyPreferences: Preferences = { distribution: '', backend_path: '' };

export function DesktopConnection() {
  const [status, setStatus] = useState<Status>({ state: 'disconnected', summary: '工作区尚未打开', detail: '' });
  const [preferences, setPreferences] = useState<Preferences>(emptyPreferences);
  const [draftPreferences, setDraftPreferences] = useState<Preferences>(emptyPreferences);
  const [environments, setEnvironments] = useState<string[]>([]);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsError, setSettingsError] = useState('');
  const [settingsNotice, setSettingsNotice] = useState('');
  const [connecting, setConnecting] = useState(false);
  const [connected, setConnected] = useState(false);
  const [activeEnvironment, setActiveEnvironment] = useState('');
  const [closing, setClosing] = useState(false);
  const [closeDetail, setCloseDetail] = useState('');
  const [epoch, setEpoch] = useState(0);
  const [workspaceRequested, setWorkspaceRequested] = useState(false);
  const invoke = <T,>(command: string, args?: Record<string, unknown>) => native().core.invoke<T>(command, args);

  useEffect(() => {
    let mounted = true;
    void invoke<Preferences>('environment_preferences').then(value => {
      if (mounted) { setPreferences(value); setDraftPreferences(value); }
    }).catch(error => {
      if (mounted) setSettingsError(`无法读取执行环境设置：${String(error)}`);
    });
    const listener = native().event.listen('desktop-close-requested', () => {
      setClosing(true);
      setCloseDetail('正在核查后台任务…');
      void invoke<{ active_tasks: number }>('connection_facts').then(facts => {
        if (mounted) setCloseDetail(facts.active_tasks > 0
          ? `仍有 ${facts.active_tasks} 个任务运行或等待。退出会保留后台任务，重新打开后查询原任务。`
          : '退出客户端。仅空闲、由本应用启动且无其他连接的服务会受控关闭。');
      }).catch(() => { if (mounted) setCloseDetail('当前没有工作区连接。退出应用不会启动或关闭 WSL 服务。'); });
    });
    return () => { mounted = false; void listener.then(stop => stop()); };
  }, []);

  async function discover() {
    setSettingsError(''); setSettingsNotice('');
    try {
      const values = await invoke<string[]>('distributions');
      setEnvironments(values);
      setSettingsNotice(values.length ? `发现 ${values.length} 个 WSL 环境。` : '未发现 WSL 发行版。');
    } catch (error) { setSettingsError(`WSL 发现失败：${String(error)}`); }
  }

  async function savePreferences() {
    setSettingsError(''); setSettingsNotice('');
    try {
      const saved = await invoke<Preferences>('save_environment_preferences', { distribution: draftPreferences.distribution, backendPath: draftPreferences.backend_path });
      setPreferences(saved);
      setDraftPreferences(saved);
      setSettingsNotice('执行环境设置已保存；连接将在打开工作区时进行。');
    } catch (error) { setSettingsError(`保存失败：${String(error)}`); }
  }

  async function selectWindowsDirectory() {
    setSettingsError(''); setSettingsNotice('');
    try {
      const selected = await invoke<string | null>('pick_windows_directory');
      if (!selected) {
        setSettingsNotice('已取消目录选择。');
        return;
      }
      if (!draftPreferences.distribution) {
        setSettingsError('请先选择 WSL 发行版，再桥接 Windows 目录。');
        return;
      }
      const mapped = await invoke<string>('bridge_windows_directory', {
        distribution: draftPreferences.distribution,
        windowsPath: selected,
      });
      setSettingsNotice(`目录可由 WSL 访问：${mapped}。选择目录不会自动授权文件访问。`);
    } catch (error) { setSettingsError(`Windows 目录桥接失败：${String(error)}`); }
  }

  async function openWorkspace() {
    if (!preferences.distribution || !preferences.backend_path) {
      setSettingsOpen(true);
      setStatus({ state: 'disconnected', summary: '请先在设置中选择并保存执行环境', detail: '' });
      return;
    }
    if (connecting) return;
    if (connected && activeEnvironment !== preferences.distribution) {
      setStatus({ state: 'error', summary: '环境偏好已更改', detail: `当前工作区仍连接 ${activeEnvironment}。请先关闭工作区，再打开新环境。` });
      return;
    }
    setConnecting(true);
    setStatus({ state: 'connecting', summary: '正在连接已保存的 WSL 执行环境…', detail: '' });
    try {
      setStatus(await invoke<Status>('connect_service', { distribution: preferences.distribution, backendPath: preferences.backend_path }));
      setConnected(true);
      setActiveEnvironment(preferences.distribution);
      setEpoch(value => value + 1);
    } catch (error) {
      setConnected(false);
      setStatus({ state: 'error', summary: '无法连接执行环境；任务未提交', detail: String(error) });
    } finally { setConnecting(false); }
  }

  async function disconnect() {
    try {
      await invoke('close_service');
      setConnected(false);
      setStatus({ state: 'disconnected', summary: '工作区已关闭；后台任务保留在原环境', detail: '' });
    } catch (error) { setStatus({ state: 'error', summary: '断开连接失败', detail: String(error) }); }
  }

  function enterWorkspace() {
    setWorkspaceRequested(true);
    void openWorkspace();
  }

  return <>
    <header className="desktop-header">
      <strong>Tomato Agent</strong>
      <div className="desktop-header-actions">
        <span role="status">{connected ? `当前工作区 · ${activeEnvironment}` : status.summary}</span>
      <button type="button" onClick={() => { setSettingsError(''); setSettingsNotice(''); setDraftPreferences(preferences); setSettingsOpen(true); }}>设置</button>
        {connected
          ? <button type="button" onClick={() => void disconnect()}>关闭工作区</button>
          : <button type="button" disabled={connecting} onClick={enterWorkspace}>{connecting ? '正在连接…' : '打开工作区'}</button>}
      </div>
    </header>
    {!connected && <main className="desktop-home">
      <h1>Tomato Agent</h1>
      <p>{preferences.distribution ? `执行环境：${preferences.distribution}` : '尚未配置执行环境'}</p>
      {status.state === 'error' && <details open><summary>{status.summary}</summary><p role="alert">{status.detail}</p><button type="button" onClick={() => void openWorkspace()}>重试连接</button></details>}
      {!preferences.distribution && <p>在设置中选择 WSL 执行环境后即可打开工作区。</p>}
      <button type="button" className="primary-button" disabled={connecting} onClick={enterWorkspace}>{connecting ? '正在连接…' : '打开工作区'}</button>
    </main>}
    {connected && <ConversationShell key={epoch} environment={activeEnvironment} />}
    {workspaceRequested && status.state === 'error' && !settingsOpen && <p role="status">工作区未打开。可重试连接或在设置中修改执行环境。</p>}
    {settingsOpen && <section role="dialog" aria-modal="true" aria-label="执行环境设置" className="desktop-settings">
      <header><h2>执行环境</h2><button type="button" onClick={() => setSettingsOpen(false)}>关闭</button></header>
      <p>设置只保存偏好，不会连接或启动 WSL 服务。新环境在下次工作区打开时生效；现有工作区仍属于 {activeEnvironment || '当前连接环境'}，项目和会话不会迁移。</p>
      <button type="button" onClick={() => void discover()}>刷新 WSL 环境</button>
      <button type="button" onClick={() => void selectWindowsDirectory()}>选择并检查 Windows 文件夹</button>
      <label>WSL 发行版<select value={draftPreferences.distribution} onChange={event => setDraftPreferences(value => ({ ...value, distribution: event.target.value }))}>
        <option value="">选择发行版</option>
        {[...new Set([...environments, ...(draftPreferences.distribution ? [draftPreferences.distribution] : [])])].map(value => <option key={value} value={value}>{value}</option>)}
      </select></label>
      <label>已有后端的 WSL 绝对路径<input value={draftPreferences.backend_path} onChange={event => setDraftPreferences(value => ({ ...value, backend_path: event.target.value }))} placeholder="/home/user/Tomato-Agent/backend" /></label>
      {settingsError && <p role="alert">{settingsError}</p>}
      {settingsNotice && <p role="status">{settingsNotice}</p>}
      <footer><button type="button" onClick={() => void savePreferences()}>保存环境设置</button><button type="button" onClick={() => { setDraftPreferences(preferences); setSettingsOpen(false); }}>取消</button></footer>
    </section>}
    {closing && <section role="dialog" aria-modal="true" aria-label="退出应用" className="fixed inset-0 bg-white p-10 z-50">
      <p>{closeDetail}</p>
      <button type="button" onClick={() => setClosing(false)}>返回应用</button>
      <button type="button" onClick={() => void invoke('exit_desktop').catch(error => setCloseDetail(String(error)))}>保留后台任务并退出</button>
    </section>}
  </>;
}