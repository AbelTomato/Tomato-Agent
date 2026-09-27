import { useEffect, useState } from 'react';
import {
  ApiError,
  confirmWritingOutline,
  createWritingTask,
  generateWritingDraft,
  loadDraftAttempt,
  loadWritingTask,
  researchWritingTask,
  retryWritingTask,
  saveWritingTask,
  type WritingStatus,
  type WritingTask,
} from '../api';

type WritingPanelProps = {
  sessionId: string;
  onRequireSession: () => Promise<string>;
};

const TASK_STORAGE_KEY = 'tomato-agent-writing-task-id';
const SAVE_KEY_PREFIX = 'tomato-agent-writing-save-key:';

const statusLabels: Record<WritingStatus, string> = {
  researching: '正在研究',
  awaiting_outline_confirmation: '等待确认提纲',
  drafting: '正在生成草稿',
  awaiting_save_confirmation: '等待保存确认',
  saved: '已保存',
  failed: '处理失败',
};

function formatOutline(outline: Record<string, unknown>): string {
  return JSON.stringify(outline, null, 2);
}

export function WritingPanel({ sessionId, onRequireSession }: WritingPanelProps) {
  const [topic, setTopic] = useState('');
  const [task, setTask] = useState<WritingTask | null>(null);
  const [outlineText, setOutlineText] = useState('');
  const [error, setError] = useState('');
  const [attemptError, setAttemptError] = useState('');
  const [busy, setBusy] = useState(false);

  function saveKey(taskId: string): string {
    const storageKey = `${SAVE_KEY_PREFIX}${taskId}`;
    const existing = window.localStorage.getItem(storageKey);
    if (existing) return existing;
    const created = `frontend-save-${taskId}`;
    window.localStorage.setItem(storageKey, created);
    return created;
  }

  async function refreshTask(taskId: string): Promise<void> {
    const loaded = await loadWritingTask(taskId);
    setTask(loaded);
    setTopic(loaded.topic);
    setOutlineText(formatOutline(loaded.outline));
    const attempt = await loadDraftAttempt(taskId);
    setAttemptError(attempt?.error_code ?? '');
  }

  useEffect(() => {
    if (!sessionId) {
      setTask(null);
      setTopic('');
      setOutlineText('');
      return;
    }
    const taskId = window.localStorage.getItem(TASK_STORAGE_KEY);
    if (!taskId) return;
    let cancelled = false;
    void loadWritingTask(taskId).then((loaded) => {
      if (cancelled) return;
      if (loaded.session_id !== sessionId) {
        window.localStorage.removeItem(TASK_STORAGE_KEY);
        return;
      }
      void refreshTask(taskId).catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : '无法恢复写作任务。');
      });
    }).catch((reason: unknown) => {
      if (cancelled) return;
      if (reason instanceof ApiError && reason.status === 404) {
        window.localStorage.removeItem(TASK_STORAGE_KEY);
        return;
      }
      setError(reason instanceof Error ? reason.message : '无法恢复写作任务。');
    });
    return () => { cancelled = true; };
  }, [sessionId]);

  async function startTask() {
    if (!topic.trim() || busy) return;
    setBusy(true);
    setError('');
    try {
      const id = await onRequireSession();
      const created = await createWritingTask(id, topic.trim());
      window.localStorage.setItem(TASK_STORAGE_KEY, created.task_id);
      setTask(created);
      setOutlineText(formatOutline(created.outline));
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : '无法创建写作任务。');
    } finally {
      setBusy(false);
    }
  }

  async function startResearch() {
    if (!task || busy) return;
    setBusy(true);
    setError('');
    try {
      const researched = await researchWritingTask(task.task_id, task.version);
      setTask(researched);
      setOutlineText(formatOutline(researched.outline));
    } catch (reason: unknown) {
      setError(reason instanceof ApiError && reason.code ? `研究失败：${reason.code}` : reason instanceof Error ? reason.message : '研究失败。');
    } finally {
      setBusy(false);
    }
  }

  async function confirmOutline() {
    if (!task || busy) return;
    let outline: Record<string, unknown>;
    try {
      const parsed: unknown = JSON.parse(outlineText);
      if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) throw new Error('提纲必须是 JSON 对象。');
      outline = parsed as Record<string, unknown>;
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : '提纲格式无效。');
      return;
    }
    setBusy(true);
    setError('');
    try {
      setTask(await confirmWritingOutline(task.task_id, task.version, outline));
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : '提纲确认失败。');
    } finally {
      setBusy(false);
    }
  }

  async function saveDraft() {
    if (!task || busy) return;
    setBusy(true);
    setError('');
    try {
      const saved = await saveWritingTask(task.task_id, task.version, saveKey(task.task_id));
      setTask(saved);
      setAttemptError('');
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : '草稿保存失败。');
    } finally {
      setBusy(false);
    }
  }

  async function generateDraft() {
    if (!task || busy) return;
    setBusy(true);
    setError('');
    try {
      const drafted = await generateWritingDraft(task.task_id, task.version);
      setTask(drafted);
      setAttemptError('');
    } catch (reason: unknown) {
      setError(reason instanceof ApiError && reason.code ? `草稿生成失败：${reason.code}` : reason instanceof Error ? reason.message : '草稿生成失败。');
      try {
        const attempt = await loadDraftAttempt(task.task_id);
        if (attempt?.error_code) setError(`草稿生成失败：${attempt.error_code}`);
      } catch {
        // 保留原始错误，attempt 查询仅用于补充失败原因。
      }
      const refreshed = await loadWritingTask(task.task_id).catch(() => null);
      if (refreshed) setTask(refreshed);
    } finally {
      setBusy(false);
    }
  }

  async function retrySave() {
    if (!task || busy) return;
    setBusy(true);
    setError('');
    try {
      const saved = await saveWritingTask(task.task_id, task.version, saveKey(task.task_id));
      setTask(saved);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : '保存重试失败。');
    } finally {
      setBusy(false);
    }
  }

  async function retry() {
    if (!task || busy) return;
    setBusy(true);
    setError('');
    try {
      const reset = await retryWritingTask(task.task_id, task.version);
      setTask(reset);
      setOutlineText(formatOutline(reset.outline));
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : '重试失败。');
    } finally {
      setBusy(false);
    }
  }

  function clearTask() {
    window.localStorage.removeItem(TASK_STORAGE_KEY);
    setTask(null);
    setTopic('');
    setOutlineText('');
    setError('');
    setAttemptError('');
  }

  return (
    <section className="writing-panel" aria-label="技术写作">
      <div className="panel-heading">
        <div><h2>技术写作</h2><p>从研究结果进入提纲确认，再保存已确认的草稿。</p></div>
        {task && <span className={`task-status task-status-${task.status}`}>{statusLabels[task.status]}</span>}
      </div>
      {!task ? (
        <div className="writing-start">
          <label>写作主题<input value={topic} onChange={(event) => setTopic(event.target.value)} placeholder="例如：Redis 持久化机制" disabled={busy} /></label>
          <button type="button" onClick={() => void startTask()} disabled={!topic.trim() || busy}>{busy ? '创建中…' : '开始写作'}</button>
        </div>
      ) : (
        <div className="writing-task">
          <p className="task-topic">主题：{task.topic} · 版本 {task.version}</p>
          {task.status === 'researching' && <>
            <p className="notice">任务已创建，研究只会在你明确点击后开始。</p>
            <button type="button" onClick={() => void startResearch()} disabled={busy}>{busy ? '研究中…' : '开始研究'}</button>
          </>}
          {task.status === 'awaiting_outline_confirmation' && <>
            <p className="notice">研究完成，请检查证据和提纲后再继续。</p>
            <p className="evidence-summary">证据状态：{task.citations.length > 0 ? `已有 ${task.citations.length} 条引用` : '材料不足，无法生成有依据的草稿'}</p>
            {task.citations.length > 0 && <ol className="writing-citations">{task.citations.map((citation) => <li key={citation.citation_id}>{citation.title}：{citation.heading_path || '未标注章节'}（第 {citation.start_line}–{citation.end_line} 行）</li>)}</ol>}
            <label>提纲 JSON<textarea value={outlineText} onChange={(event) => setOutlineText(event.target.value)} rows={9} disabled={busy} /></label>
            <button type="button" onClick={() => void confirmOutline()} disabled={busy}>{busy ? '确认中…' : '确认提纲并生成草稿'}</button>
          </>}
          {task.status === 'drafting' && <>
            <p className="notice">提纲已确认。点击按钮后才会调用草稿生成。</p>
            <button type="button" onClick={() => void generateDraft()} disabled={busy}>{busy ? '生成中…' : '生成草稿'}</button>
          </>}
          {task.status === 'awaiting_save_confirmation' && <>
            <label>草稿<textarea value={task.draft ?? ''} readOnly rows={14} /></label>
            <p className="evidence-summary">引用归属：{task.citations.length} 条服务端证据</p>
            <button type="button" onClick={() => void saveDraft()} disabled={busy}>{busy ? '保存中…' : '确认保存草稿'}</button>
          </>}
          {task.status === 'saved' && <p className="notice success">草稿已保存：{task.saved_path}</p>}
          {task.status === 'failed' && <>
            <p className="notice error">任务在“{task.failed_stage ?? '未知阶段'}”失败{attemptError ? `：${attemptError}` : '。'}</p>
            {task.failed_stage === 'saving' ? (
              <button type="button" onClick={() => void retrySave()} disabled={busy}>{busy ? '保存重试中…' : '重试保存'}</button>
            ) : (
              <button type="button" onClick={() => void retry()} disabled={busy}>{busy ? '重试中…' : '重试生成'}</button>
            )}
          </>}
          {error && <p className="notice error">{error}</p>}
          <button className="secondary-button" type="button" onClick={clearTask} disabled={busy}>结束当前写作任务</button>
        </div>
      )}
    </section>
  );
}