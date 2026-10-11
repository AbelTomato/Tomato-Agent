import { useEffect, useRef, useState } from 'react';
import { loadCodeTaskArtifact, type Citation, type CodeTask, type TaskAction, type UnifiedTask, type Workspace } from '../api';
import { CitationList } from './CitationList';
import { AssistantResult } from './AssistantResult';
import { PendingActionPanel } from './PendingActionPanel';

export const taskLabels = { assistant: '助手', chat: '普通聊天', knowledge: '知识问答', writing: '技术写作', code: '代码任务' };
export const statusLabels: Record<string, string> = {
  queued: '排队中', running: '执行中', waiting: '等待确认', completed: '已完成',
  failed: '失败', cancelled: '已取消', timed_out: '已超时',
};

// 展示组件不拥有 Session/Run，也不调用领域执行入口。
export function CodeTestResults({ results }: { results: CodeTask['test_results'] }) {
  return <div><h3>测试结果</h3>{results.length ? results.map((result, index) => <div key={index} className="code-test-result">
    <p>测试 {index + 1}：{result.passed === true ? '通过' : '未通过'} · 退出码 {result.exit_code ?? '未知'}</p>
    <details><summary>结构化报告</summary><pre>{JSON.stringify(result, null, 2)}</pre></details>
  </div>) : <p className="code-empty">测试未运行 / 尚无测试报告</p>}</div>;
}

export function WritingDraft({ draft }: { draft: string | null }) {
  return <details><summary>查看草稿</summary><pre>{draft ?? '尚无草稿'}</pre></details>;
}

export function TaskCard({ task, busy, onAction, onOpenDocument, workspaces = [] }: {
  task: UnifiedTask; busy: boolean; workspaces?: Workspace[];
  onAction: (task: UnifiedTask, action: TaskAction, payload: Record<string, unknown>) => Promise<void>;
  onOpenDocument: (citation: Citation) => void;
}) {
  const result = task.result ?? task.pending_action?.display_data ?? {};
  const [outline, setOutline] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState('');
  const [artifact, setArtifact] = useState('');
  const [loading, setLoading] = useState(false);
  const request = useRef<AbortController | null>(null);
  useEffect(() => {
    setOutline(JSON.stringify(result.outline ?? {}, null, 2));
    setConfirmed(false); setError('');
  }, [task.run_id, task.pending_action?.action_id]);
  useEffect(() => {
    setArtifact(''); setLoading(false);
    return () => request.current?.abort();
  }, [task.run_id]);
  const allowed = (action: TaskAction) => !task.legacy && task.capabilities.includes(action);
  const recovery = task.recovery?.version === task.version ? task.recovery : undefined;
  async function confirmOutline() {
    try {
      const parsed: unknown = JSON.parse(outline);
      if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) throw new Error('提纲必须为 JSON 对象。');
      setError(''); await onAction(task, 'confirm_outline', { outline: parsed });
    } catch (reason) { setError(reason instanceof Error ? reason.message : '提纲无效。'); }
  }
  async function openArtifact(id: string) {
    request.current?.abort();
    const controller = new AbortController(); request.current = controller;
    setLoading(true); setError('');
    try {
      const loaded = await loadCodeTaskArtifact(task.run_id, id, controller.signal);
      if (!controller.signal.aborted) setArtifact(loaded.content);
    } catch (reason) {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : '无法读取产物。');
    } finally { if (!controller.signal.aborted) setLoading(false); }
  }
  return <article className="task-card" aria-label={`${taskLabels[task.kind]}任务`}>
    <div className="panel-heading"><h2>{taskLabels[task.kind]}</h2><span className={`task-status task-status-${task.status}`}>{statusLabels[task.status] ?? task.status}</span></div>
    <small>Run：{task.run_id} · 阶段：{task.phase ?? '无'} · 版本：{task.version}</small>
    {task.legacy && <p className="notice">历史记录（legacy），仅供查看，无统一动作能力。</p>}
    {task.messages?.filter((message) => message.role === 'user').map((message, index) => <p className="task-prompt" key={index}>你：{message.content}</p>)}
    {(task.kind === 'chat' || task.kind === 'knowledge') && <>
      <details open><summary>回答</summary><p className="task-answer">{result.answer ?? result.content ?? (task.status === 'completed' ? '无回答正文' : '等待回答')}</p></details>
      {['no_results', 'insufficient'].includes(result.evidence_status ?? '') && <p className="notice">材料不足（{result.evidence_status}），本轮回答无充分来源支撑。</p>}
      {task.kind === 'knowledge' && !result.citations?.length && <p className="evidence-warning">无来源（source）</p>}
    </>}
    {task.kind === 'assistant' && <AssistantResult result={result} onOpenDocument={onOpenDocument} />}
    {task.kind === 'assistant' && <PendingActionPanel key={`${task.run_id}:${task.version}:${task.pending_action?.action_id}`} task={task} busy={busy} workspaces={workspaces} onAction={onAction} />}
    {task.kind !== 'assistant' && result.citations && <CitationList citations={result.citations} onOpenDocument={onOpenDocument} />}
    {task.kind === 'writing' && <>
      <p>主题：{result.topic ?? '研究中'}</p>
      {allowed('confirm_outline') ? <label>提纲 JSON<textarea rows={9} value={outline} disabled={busy} onChange={(event) => setOutline(event.target.value)} /></label> :
        result.outline && <details><summary>查看提纲</summary><pre>{JSON.stringify(result.outline, null, 2)}</pre></details>}
      {allowed('confirm_outline') && <button disabled={busy} type="button" onClick={() => void confirmOutline()}>确认提纲并生成草稿</button>}
      {result.draft != null && <WritingDraft draft={result.draft} />}
      {allowed('save_draft') && <button type="button" disabled={busy} onClick={() => void onAction(task, 'save_draft', {})}>确认保存草稿</button>}
      {result.saved_path && <p className="notice success">草稿已保存：{result.saved_path}</p>}
    </>}
    {task.kind === 'code' && <>
      <p className="code-meta">本轮工作区：{task.workspace_id} · 修改文件：{result.changed_files?.join('、') || '暂无'}</p>
      <CodeTestResults results={result.test_results ?? []} />
      <details><summary>Diff 与 Artifact</summary><ul className="code-artifacts">{result.artifacts?.map((ref) => <li key={ref.artifact_id}>
        <button type="button" className="secondary-button" disabled={loading} onClick={() => void openArtifact(ref.artifact_id)}>{ref.kind === 'diff' ? '查看本轮 diff' : ref.kind} · {ref.size_bytes} bytes</button>
      </li>)}</ul>{artifact && <pre>{artifact}</pre>}</details>
    </>}
    {task.pending_action && <p className="notice">等待原因：{task.pending_action.reason}</p>}
    {recovery && <section aria-label="恢复检查结果">
      <p className="notice">恢复分类原因：{recovery.reason} · 分类动作：{recovery.action}</p>
      <p>允许的后续动作：{recovery.allowed_actions.map((action) => ({
        cancel: '取消任务', inspect_recovery: '检查恢复原因', continue_recovery: '确认请求恢复',
      })[action]).join('、') || '无'}</p>
      {recovery.action === 'manual_review' && <p className="notice">副作用结果尚未确认，需要人工核对；请求恢复仍可能保持等待。</p>}
      <details><summary>本次恢复观察</summary><pre>{JSON.stringify(recovery, null, 2)}</pre></details>
    </section>}
    {allowed('inspect_recovery') && <>
      <p className="notice">核对文件写入、测试与保存事实。检查结果不代表副作用已确认；未知结果保持等待。</p>
      <details><summary>当前恢复事实</summary><pre>{JSON.stringify({ error: task.error, pending_action: task.pending_action }, null, 2)}</pre></details>
      <button type="button" disabled={busy} onClick={() => void onAction(task, 'inspect_recovery', {})}>检查恢复原因</button>
    </>}
    {allowed('continue_recovery') && <>
      <label className="code-confirm"><input type="checkbox" checked={confirmed} disabled={busy} onChange={(event) => setConfirmed(event.target.checked)} />我已核对副作用，确认按当前版本请求恢复</label>
      <button type="button" disabled={busy || !confirmed} onClick={() => void onAction(task, 'continue_recovery', { confirmed: true })}>确认请求恢复</button>
    </>}
    {allowed('cancel') && <button className="secondary-button" type="button" disabled={busy} onClick={() => void onAction(task, 'cancel', {})}>取消任务</button>}
    {task.status === 'waiting' && !task.capabilities.length && <p className="notice">当前记录无可用动作能力。</p>}
    {task.error && <pre className="notice error">{JSON.stringify(task.error, null, 2)}</pre>}
    {error && <p className="notice error">{error}</p>}
  </article>;
}