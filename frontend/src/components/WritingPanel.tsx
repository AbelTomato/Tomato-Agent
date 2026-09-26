import { useEffect, useRef, useState } from 'react';
import {
  ApiError,
  confirmWritingOutline,
  createWritingTask,
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
  const [busy, setBusy] = useState(false);
  const researchRequests = useRef(new Set<string>());

  async function startResearch(currentTask: WritingTask): Promise<WritingTask> {
    const requestKey = `${currentTask.task_id}:${currentTask.version}`;
    if (researchRequests.current.has(requestKey)) return currentTask;
    researchRequests.current.add(requestKey);
    try {
      return await researchWritingTask(currentTask.task_id, currentTask.version);
    } finally {
      researchRequests.current.delete(requestKey);
    }
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
    void loadWritingTask(taskId).then(async (loaded) => {
      if (cancelled) return;
      if (loaded.session_id !== sessionId) {
        window.localStorage.removeItem(TASK_STORAGE_KEY);
        return;
      }
      setTask(loaded);
      setTopic(loaded.topic);
      setOutlineText(formatOutline(loaded.outline));
      if (loaded.status === 'researching') {
        const researched = await startResearch(loaded);
        if (!cancelled) {
          setTask(researched);
          setOutlineText(formatOutline(researched.outline));
        }
      }
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

  useEffect(() => {
    if (!task || !['researching', 'drafting'].includes(task.status)) return;
    const timer = window.setInterval(() => {
      void loadWritingTask(task.task_id).then((loaded) => {
        setTask(loaded);
        setOutlineText(formatOutline(loaded.outline));
      }).catch((reason: unknown) => {
        setError(reason instanceof Error ? reason.message : '无法刷新写作任务。');
      });
    }, 2000);
    return () => window.clearInterval(timer);
  }, [task]);

  async function startTask() {
    if (!topic.trim() || busy) return;
    setBusy(true);
    setError('');
    try {
      const id = await onRequireSession();
      const created = await createWritingTask(id, topic.trim());
      window.localStorage.setItem(TASK_STORAGE_KEY, created.task_id);
      const researched = await startResearch(created);
      setTask(researched);
      setOutlineText(formatOutline(researched.outline));
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : '无法创建写作任务。');
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
      const saved = await saveWritingTask(task.task_id, task.version, `frontend-save-${task.version}`);
      setTask(saved);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : '草稿保存失败。');
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
      const researched = await startResearch(reset);
      setTask(researched);
      setOutlineText(formatOutline(researched.outline));
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
          {task.status === 'awaiting_outline_confirmation' && <>
            <label>提纲 JSON<textarea value={outlineText} onChange={(event) => setOutlineText(event.target.value)} rows={9} disabled={busy} /></label>
            <button type="button" onClick={() => void confirmOutline()} disabled={busy}>{busy ? '确认中…' : '确认提纲并继续'}</button>
          </>}
          {task.status === 'awaiting_save_confirmation' && <>
            <label>草稿<textarea value={task.draft ?? ''} readOnly rows={14} /></label>
            <button type="button" onClick={() => void saveDraft()} disabled={busy}>{busy ? '保存中…' : '确认保存草稿'}</button>
          </>}
          {['researching', 'drafting'].includes(task.status) && <p className="notice">后端正在处理任务，页面会自动刷新状态。</p>}
          {task.status === 'saved' && <p className="notice success">草稿已保存：{task.saved_path}</p>}
          {task.status === 'failed' && <><p className="notice error">任务在“{task.failed_stage ?? '未知阶段'}”失败。</p><button type="button" onClick={() => void retry()} disabled={busy}>{busy ? '重试中…' : '重试任务'}</button></>}
          {error && <p className="notice error">{error}</p>}
          <button className="secondary-button" type="button" onClick={clearTask} disabled={busy}>结束当前写作任务</button>
        </div>
      )}
    </section>
  );
}