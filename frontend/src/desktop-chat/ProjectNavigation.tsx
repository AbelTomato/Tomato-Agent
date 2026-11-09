import { ChevronDown, Folder, MessageSquare, MoreHorizontal, Plus, Settings } from 'lucide-react';

export type ChatSession = { id: string; title: string; created_at: string; project_id: string | null };
export type ChatProject = { id: string; name: string; root_path: string; available: boolean; created_at: string; updated_at: string };

type Props = {
  sessions: ChatSession[];
  projects: ChatProject[];
  sessionId: string | null;
  disabled: boolean;
  onSettings: () => void;
  onNewSession: (projectId?: string) => void;
  onNewProject: () => void;
  onSelectSession: (id: string) => void;
  onRenameSession: (session: ChatSession) => void;
  onDeleteSession: (session: ChatSession) => void;
  onMoveSession: (session: ChatSession, projectId: string) => void;
  onRenameProject: (project: ChatProject) => void;
  onDeleteProject: (project: ChatProject) => void;
};

export function ProjectNavigation(props: Props) {
  function sessionRow(session: ChatSession) {
    return <div className="desktop-chat-session-row" key={session.id}>
      <button type="button" className="desktop-chat-session" aria-current={session.id === props.sessionId ? 'page' : undefined}
        onClick={() => props.onSelectSession(session.id)} disabled={props.disabled} title={session.title}>
        <MessageSquare size={14} aria-hidden="true" /><span>{session.title}</span>
      </button>
      <details className="desktop-chat-menu">
        <summary aria-label={`对话操作 ${session.title}`}><MoreHorizontal size={16} aria-hidden="true" /></summary>
        <div className="desktop-chat-menu-panel" onClick={event => { if ((event.target as HTMLElement).closest('button')) event.currentTarget.closest('details')?.removeAttribute('open'); }} onChange={event => event.currentTarget.closest('details')?.removeAttribute('open')}>
          <label>所属项目<select aria-label={`归类 ${session.title}`} value={session.project_id ?? ''} disabled={props.disabled}
            onChange={event => props.onMoveSession(session, event.target.value)}>
            <option value="">独立会话</option>
            {props.projects.map(project => <option key={project.id} value={project.id}>{project.name}</option>)}
          </select></label>
          <button type="button" disabled={props.disabled} onClick={() => props.onRenameSession(session)}>重命名对话</button>
          <button type="button" className="danger-action" disabled={props.disabled} onClick={() => props.onDeleteSession(session)}>删除对话</button>
        </div>
      </details>
    </div>;
  }
  const independent = props.sessions.filter(session => !session.project_id || !props.projects.some(project => project.id === session.project_id));
  return <aside className="desktop-chat-sidebar" aria-label="项目与会话">
    <header><h1>Tomato Agent</h1><button type="button" className="desktop-chat-icon-button" onClick={props.onSettings} aria-label="模型设置"><Settings size={18} /></button></header>
    <button type="button" className="desktop-chat-new" disabled={props.disabled} onClick={() => props.onNewSession()}><Plus size={17} />新建对话</button>
    <nav aria-label="项目导航">
      <div className="desktop-chat-section-heading"><h2>项目</h2><button type="button" className="desktop-chat-icon-button" disabled={props.disabled} onClick={props.onNewProject} aria-label="新建项目"><Plus size={16} /></button></div>
      {props.projects.length === 0 && <p className="desktop-chat-project-empty">添加本地目录，整理相关对话</p>}
      {props.projects.map(project => <section className="desktop-chat-project" key={project.id} aria-label={`项目 ${project.name}`}>
        <div className="desktop-chat-project-heading">
          <Folder size={16} aria-hidden="true" /><strong title={project.root_path}>{project.name}</strong>
          <button type="button" className="desktop-chat-icon-button" disabled={props.disabled} onClick={() => props.onNewSession(project.id)} aria-label={`在 ${project.name} 新建对话`}><Plus size={15} /></button>
          <details className="desktop-chat-menu"><summary aria-label={`项目操作 ${project.name}`}><MoreHorizontal size={16} /></summary>
            <div className="desktop-chat-menu-panel" onClick={event => { if ((event.target as HTMLElement).closest('button')) event.currentTarget.closest('details')?.removeAttribute('open'); }} onChange={event => event.currentTarget.closest('details')?.removeAttribute('open')}><p className="desktop-chat-project-path">{project.root_path}</p>
              <button type="button" disabled={props.disabled} onClick={() => props.onRenameProject(project)}>重命名项目</button>
              <button type="button" className="danger-action" disabled={props.disabled} onClick={() => props.onDeleteProject(project)}>删除项目</button>
            </div>
          </details>
        </div>
        {!project.available && <p className="desktop-chat-directory-warning" role="status">目录不可用</p>}
        <details open className="desktop-chat-project-sessions"><summary><ChevronDown size={12} aria-hidden="true" />对话 ({props.sessions.filter(session => session.project_id === project.id).length})</summary>
          {props.sessions.filter(session => session.project_id === project.id).map(sessionRow)}
          {!props.sessions.some(session => session.project_id === project.id) && <p className="desktop-chat-project-empty">暂无会话</p>}
        </details>
      </section>)}
      <section className="desktop-chat-independent" aria-label="独立会话"><h2>独立会话</h2>
        {independent.map(sessionRow)}
        {independent.length === 0 && <p className="desktop-chat-project-empty">暂无独立会话</p>}
      </section>
    </nav>
  </aside>;
}
