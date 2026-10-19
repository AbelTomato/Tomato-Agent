import type { Project, Session } from '../api';

type Props = {
  projects: Project[];
  sessions: Session[];
  sessionId: string;
  environment: string;
  busy: boolean;
  onNewSession: () => void;
  onOpenProject: () => void;
  onSelectSession: (id: string) => void;
  onRenameSession: (session: Session) => void;
  onShowUnavailable: (project: Project) => void;
};

export function ProjectSidebar(props: Props) {
  const { projects, sessions, sessionId, environment, busy, onNewSession, onOpenProject, onSelectSession, onRenameSession, onShowUnavailable } = props;
  const projectGroups = projects.map((project) => ({
    project,
    sessions: sessions.filter((session) => session.project_id === project.project_id),
  }));
  const ungrouped = sessions.filter((session) => !session.project_id || !projects.some((project) => project.project_id === session.project_id));
  const sessionButton = (session: Session) => <div className="session-nav-item" key={session.id}>
    <button type="button" className="session-nav-select" aria-current={session.id === sessionId ? 'true' : undefined} onClick={() => onSelectSession(session.id)}>{String(session.title ?? session.metadata.title ?? '未命名对话')}</button>
    <button type="button" className="session-nav-rename" aria-label={`重命名 ${String(session.title ?? '对话')}`} title="重命名对话" disabled={busy} onClick={() => onRenameSession(session)}>编辑</button>
  </div>;

  return <aside className="session-sidebar project-sidebar"><h1>Tomato Agent</h1>
    <p className="environment-label">WSL · {environment || '当前连接环境'}</p>
    <div className="sidebar-actions"><button type="button" disabled={busy} onClick={onOpenProject}>打开文件夹</button><button type="button" disabled={busy} onClick={onNewSession}>新建对话</button></div>
    <nav aria-label="项目与会话" className="project-session-nav">
      {projectGroups.map(({ project, sessions: items }) => <section className="project-nav-group" key={project.project_id}>
        <button type="button" className="project-nav-heading" disabled={busy} title={project.root_path} onClick={() => { if (!project.available) onShowUnavailable(project); }} aria-label={`${project.display_name}${project.available ? '' : '，不可用'}`}>
          <span>{project.display_name}</span><small>{environment || 'WSL'}</small>
        </button>
        {!project.available && <p className="project-unavailable">项目路径不可用，请检查 WSL 环境。</p>}
        {items.map(sessionButton)}
      </section>)}
      {ungrouped.length > 0 && <section className="project-nav-group"><h2>对话</h2>{ungrouped.map(sessionButton)}</section>}
      {projects.length === 0 && sessions.length === 0 && <p className="empty-state">打开文件夹开始项目，或新建独立对话。</p>}
    </nav>
  </aside>;
}