import { useState } from 'react';
import { type TaskAction, type UnifiedTask, type Workspace } from '../api';

export function PendingActionPanel({ task, busy, workspaces, onAction }: {
  task: UnifiedTask; busy: boolean; workspaces: Workspace[];
  onAction: (task: UnifiedTask, action: TaskAction, payload: Record<string, unknown>) => Promise<void>;
}) {
  const [input, setInput] = useState('');
  const [workspace, setWorkspace] = useState('');
  const [acceptRisk, setAcceptRisk] = useState(false);
  const pending = task.pending_action;
  if (!pending || task.status !== 'waiting' || task.legacy) return null;
  const allowed = (action: TaskAction) => task.capabilities.includes(action) && pending.allowed_actions.includes(action);
  const display = pending.display_data ?? {};
  return <section className="pending-action-panel" aria-label="助手待处理动作">
    <h3>{pending.action_type === 'workspace_binding' ? '选择本任务工作区' : '助手需要你的输入或确认'}</h3>
    {typeof display.question === 'string' && <p>{display.question}</p>}
    {typeof display.message === 'string' && <p>{display.message}</p>}
    {pending.target_capability && <p>请求能力：{pending.target_capability}</p>}
    {allowed('resolve_side_effect') && <fieldset disabled={busy}>
      <legend>核对未知操作</legend>
      <p>风险接受只允许后续任务继续，不代表旧操作已终止；迟到冲突仍会阻止继续。</p>
      <button type="button" disabled={acceptRisk} onClick={() => void onAction(task, 'resolve_side_effect', { resolution: 'executed' })}>确认已执行</button>
      <button type="button" disabled={acceptRisk} onClick={() => void onAction(task, 'resolve_side_effect', { resolution: 'not_executed' })}>确认未执行</button>
      <button type="button" disabled={acceptRisk} onClick={() => void onAction(task, 'resolve_side_effect', { resolution: 'unknown' })}>仍无法确认</button>
      <label><input type="checkbox" checked={acceptRisk} onChange={(event) => setAcceptRisk(event.target.checked)} />我接受旧操作仍可能产生影响，并允许后续任务继续</label>
      <button type="button" disabled={!acceptRisk} onClick={() => void onAction(task, 'resolve_side_effect', { resolution: 'executed', accept_risk: true })}>确认已执行并继续</button>
      <button type="button" disabled={!acceptRisk} onClick={() => void onAction(task, 'resolve_side_effect', { resolution: 'not_executed', accept_risk: true })}>确认未执行并继续</button>
    </fieldset>}
    {allowed('provide_input') && <>
      <label>补充说明<textarea value={input} disabled={busy} onChange={(event) => setInput(event.target.value)} rows={3} /></label>
      <button type="button" disabled={busy || !input.trim()} onClick={() => void onAction(task, 'provide_input', { message: input.trim() })}>提交补充说明</button>
    </>}
    {allowed('bind_workspace') && <>
      <p className="capability-note">仅可绑定已注册且获准的项目一次；服务端核对版本与可用性后继续当前 Run。</p>
      <label>任务工作区<select value={workspace} disabled={busy || !!task.workspace_id} onChange={(event) => setWorkspace(event.target.value)}>
        <option value="">请选择已注册工作区</option>
        {workspaces.map((item) => <option key={item.workspace_id} value={item.workspace_id} disabled={item.available === false}>{item.repo_path ?? item.workspace_id}{item.available === false ? '（不可用）' : ''}</option>)}
      </select></label>
      <button type="button" disabled={busy || !!task.workspace_id || !workspaces.some((item) => item.workspace_id === workspace && item.available !== false)} onClick={() => void onAction(task, 'bind_workspace', { workspace_id: workspace })}>绑定并继续当前任务</button>
    </>}
    {allowed('approve_action') && !display.outline && <button type="button" disabled={busy} onClick={() => void onAction(task, 'approve_action', {})}>确认此动作</button>}
    {allowed('approve_action') && display.outline && <p className="notice">提纲确认将在写作能力接入后提供。</p>}
    {allowed('reject_action') && <button type="button" className="secondary-button" disabled={busy} onClick={() => void onAction(task, 'reject_action', {})}>拒绝此请求</button>}
  </section>;
}