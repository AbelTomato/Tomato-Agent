import { useEffect, useRef, useState } from 'react';
import { Code2, RefreshCw, Square, Play, FileText } from 'lucide-react';
import {
  createCodeTask, getCodeTask, listCodeTaskEvents, executeCodeTask, cancelCodeTask,
  recoverCodeTask, loadCodeTaskArtifact,
  type CodeTask, type CodeTaskSubmission, type CodeTaskStatus, type CodeTaskEvent,
  type CodeTaskArtifact, type CodeTaskRecovery,
} from '../api';

const labels: Record<CodeTaskStatus, string> = {
  queued: '排队中', running: '执行中', waiting: '等待人工核对', completed: '已完成',
  failed: '失败', cancelled: '已取消', timed_out: '已超时',
};
const active = (status: CodeTaskStatus) => status === 'queued' || status === 'running';
const terminal = (status: CodeTaskStatus) => ['completed', 'failed', 'cancelled', 'timed_out'].includes(status);
const message = (reason: unknown) => reason instanceof Error ? reason.message : '代码任务请求失败。';

export function CodeTaskPanel() {
  const [input, setInput] = useState('');
  const [run, setRun] = useState<CodeTaskSubmission | CodeTask | null>(null);
  const [events, setEvents] = useState<CodeTaskEvent[]>([]);
  const [artifact, setArtifact] = useState<CodeTaskArtifact | null>(null);
  const [recovery, setRecovery] = useState<CodeTaskRecovery | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [updatedAt, setUpdatedAt] = useState('');
  const [refresh, setRefresh] = useState(0);
  const mounted = useRef(false);
  const generation = useRef(0);
  const actionController = useRef<AbortController | null>(null);
  const cursor = useRef(0);
  const runId = run?.run_id;
  const status = run?.status;
  const details = run && 'version' in run ? run : null;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      generation.current += 1;
      actionController.current?.abort();
    };
  }, []);

  useEffect(() => {
    if (!runId || busy) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;
    async function poll() {
      try {
        const [loaded, result] = await Promise.all([
          getCodeTask(runId!, controller.signal), listCodeTaskEvents(runId!, cursor.current, controller.signal),
        ]);
        if (controller.signal.aborted) return;
        setRun(loaded);
        setEvents((previous) => {
          const bySequence = new Map([...previous, ...result.events].map((event) => [event.sequence, event]));
          return [...bySequence.values()].sort((a, b) => a.sequence - b.sequence);
        });
        cursor.current = Math.max(cursor.current, ...result.events.map((event) => event.sequence));
        setUpdatedAt(new Date().toLocaleString());
        setError('');
        failures = 0;
        if (!terminal(loaded.status)) timer = setTimeout(() => void poll(), 1000);
      } catch (reason) {
        if (controller.signal.aborted) return;
        failures += 1;
        setError(`${message(reason)}${failures >= 3 ? ' 自动轮询已暂停，请手动刷新。' : ''}`);
        if (failures < 3) timer = setTimeout(() => void poll(), 1000);
      }
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [runId, status, refresh, busy]);

  async function act(operation: (signal: AbortSignal, current: () => boolean) => Promise<void>) {
    if (busy) return;
    actionController.current?.abort();
    const controller = new AbortController();
    actionController.current = controller;
    const token = ++generation.current;
    const current = () => mounted.current && !controller.signal.aborted && generation.current === token;
    setBusy(true);
    setError('');
    try { await operation(controller.signal, current); }
    catch (reason) { if (current()) setError(message(reason)); }
    finally { if (current()) { setBusy(false); setRefresh((value) => value + 1); } }
  }

  function start() {
    void act(async (signal, current) => {
      const created = await createCodeTask(input.trim(), signal);
      if (!current()) return;
      cursor.current = 0;
      setEvents([]); setArtifact(null); setRecovery(null); setConfirmed(false); setUpdatedAt('');
      setRun(created);
    });
  }

  function recover(action: 'inspect' | 'continue') {
    if (!details) return;
    void act(async (signal, current) => {
      const result = await recoverCodeTask(details.run_id, details.version, action, confirmed, signal);
      if (current()) { setRecovery(result); setConfirmed(false); }
    });
  }

  return <section className="code-task-panel" aria-label="受控代码任务">
    <div className="panel-heading">
      <div><h2><Code2 size={20} aria-hidden="true" /> 受控代码任务</h2><p>在固定 python_calculator fixture 中修复代码，查看隔离测试和 diff。</p></div>
      {run && <span className={`task-status task-status-${run.status}`} role="status">{labels[run.status]}</span>}
    </div>
    <label>任务描述<textarea rows={3} value={input} onChange={(event) => setInput(event.target.value)} placeholder="例如：修复 calculator 的加法错误并运行登记测试" disabled={busy || (run !== null && !terminal(run.status))} /></label>
    <button type="button" onClick={start} disabled={busy || !input.trim() || (run !== null && !terminal(run.status))}>{busy ? '请求中…' : '提交代码任务'}</button>
    {!run && <p className="code-empty">提交后自动观察后台状态。需要启用 Worker 并配置隔离执行环境。</p>}
    {error && <p className="notice error" role="alert">{error}</p>}
    {run && <>
      <p className="code-meta">Run：{run.run_id}<br />Workspace：{run.workspace_id}<br />版本：{details?.version ?? '待查询'} · 最近查询：{updatedAt || '尚未查询'}</p>
      {run.dispatch_status === 'worker_disabled' && <p className="notice">后台 Worker 未启用，任务不会自动执行。请在后端配置 CODE_TASK_WORKER_ENABLED=true 并重启服务。</p>}
      {details?.error && <pre className="notice error">{JSON.stringify(details.error, null, 2)}</pre>}
      <div className="code-actions">
        <button className="secondary-button" type="button" disabled={busy} onClick={() => setRefresh((value) => value + 1)}><RefreshCw size={16} />手动刷新</button>
        {active(run.status) && <button type="button" disabled={busy} onClick={() => void act(async (signal, current) => {
          const result = await executeCodeTask(run.run_id, signal);
          if (current()) setRun(result);
        })}><Play size={16} />查询调度</button>}
        {!terminal(run.status) && <button type="button" disabled={busy} onClick={() => void act(async (signal, current) => {
          const result = await cancelCodeTask(run.run_id, signal);
          if (current()) setRun((previous) => previous ? { ...previous, ...result } : previous);
        })}><Square size={16} />取消任务</button>}
      </div>
      {run.status === 'waiting' && <div className="code-recovery">
        <p className="notice">请人工核对文件写入和 Sandbox 副作用。恢复请求不会保证继续执行，未知事实仍可能保持等待。</p>
        <button className="secondary-button" type="button" disabled={busy || !details} onClick={() => recover('inspect')}>检查恢复原因</button>
        {recovery && <p>恢复原因：{recovery.recovery_reason ?? recovery.status} · 建议：{recovery.recovery_action ?? '保持后端判定'}</p>}
        <label className="code-confirm"><input type="checkbox" checked={confirmed} disabled={busy} onChange={(event) => setConfirmed(event.target.checked)} />我已核对副作用，确认按当前版本请求恢复</label>
        <button type="button" disabled={busy || !details || !confirmed} onClick={() => recover('continue')}>确认请求恢复</button>
      </div>}
      <div><h3>事件</h3>{events.length ? <ol className="code-events">{events.map((event) => <li key={event.sequence}><b>#{event.sequence} {event.event_type}</b><small>{event.created_at}</small><pre>{JSON.stringify(event.payload, null, 2)}</pre></li>)}</ol> : <p className="code-empty">暂无事件</p>}</div>
      <div><h3>测试结果</h3>{details?.test_results.length ? details.test_results.map((result, index) => <div key={index} className="code-test-result"><p>测试 {index + 1}：{result.passed ? '通过' : '未通过'} · 退出码 {result.exit_code ?? '未知'}</p><details><summary>结构化报告</summary><pre>{JSON.stringify(result, null, 2)}</pre></details></div>) : <p className="code-empty">尚无测试报告</p>}</div>
      <div><h3>Diff 与 Artifact</h3><p className="code-meta">修改文件：{details?.changed_files.join('、') || '暂无'}</p>
        <ul className="code-artifacts">{details?.artifacts.map((ref) => <li key={ref.artifact_id}><button className="secondary-button" type="button" disabled={busy} onClick={() => void act(async (signal, current) => {
          const loaded = await loadCodeTaskArtifact(run.run_id, ref.artifact_id, signal);
          if (current()) setArtifact(loaded);
        })}><FileText size={16} />{ref.kind === 'diff' ? '查看 diff' : ref.kind} · {ref.size_bytes} bytes</button><small>{ref.artifact_id}</small></li>)}</ul>
        {artifact && <article><h3>{artifact.artifact.kind}</h3><p className="code-meta">SHA256：{artifact.artifact.sha256}</p><pre className="code-artifact-content">{artifact.content.slice(0, 50000)}</pre>{artifact.content.length > 50000 && <p className="notice">页面只展示前 50000 字符。</p>}</article>}
      </div>
    </>}
  </section>;
}