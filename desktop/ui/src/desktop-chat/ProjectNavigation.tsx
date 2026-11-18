import { ChevronDown, Folder, MessageSquare, MoreHorizontal, Plus, Settings } from 'lucide-react';
import { useState, useRef, forwardRef, useImperativeHandle } from 'react';
import { SearchBar, SearchBarHandle } from '../components/SearchBar';

export type ChatSession = { id: string; title: string; created_at: string; project_id: string | null; title_version?: number };
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

export interface ProjectNavigationHandle {
  focusSearch: () => void;
}

export const ProjectNavigation = forwardRef<ProjectNavigationHandle, Props>((props, ref) => {
  const [searchQuery, setSearchQuery] = useState('');
  const searchBarRef = useRef<SearchBarHandle>(null);

  // 暴露给父组件的方法
  useImperativeHandle(ref, () => ({
    focusSearch: () => searchBarRef.current?.focus(),
  }));

  // 搜索过滤逻辑
  const filteredSessions = searchQuery.trim()
    ? props.sessions.filter(session =>
        session.title.toLowerCase().includes(searchQuery.toLowerCase())
      )
    : props.sessions;

  const handleSearch = (query: string) => {
    setSearchQuery(query);
  };

  // 高亮匹配文本
  const highlightText = (text: string) => {
    if (!searchQuery.trim()) return text;
    const regex = new RegExp(`(${searchQuery.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'gi');
    const parts = text.split(regex);
    return parts.map((part, i) =>
      regex.test(part) ? <mark key={i} className="search-highlight">{part}</mark> : part
    );
  };

  function sessionRow(session: ChatSession) {
    return <div className="desktop-chat-session-row" key={session.id}>
      <button type="button" className="desktop-chat-session hover-transition" aria-current={session.id === props.sessionId ? 'page' : undefined}
        onClick={() => props.onSelectSession(session.id)} disabled={props.disabled} title={session.title}>
        <MessageSquare size={14} aria-hidden="true" /><span>{highlightText(session.title)}</span>
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

  const independent = filteredSessions.filter(session => !session.project_id || !props.projects.some(project => project.id === session.project_id));
  const hasResults = filteredSessions.length > 0;
  const isSearching = searchQuery.trim().length > 0;

  return <aside className="desktop-chat-sidebar" aria-label="项目与会话">
    <header><h1>Tomato Agent</h1><button type="button" className="desktop-chat-icon-button hover-transition" onClick={props.onSettings} aria-label="模型设置"><Settings size={18} /></button></header>
    <SearchBar 
      ref={searchBarRef}
      onSearch={handleSearch}
      placeholder="搜索会话..."
      className="px-4 pb-3"
    />
    <button type="button" className="desktop-chat-new hover-transition" disabled={props.disabled} onClick={() => props.onNewSession()}><Plus size={17} />新建对话</button>
    {isSearching && !hasResults && (
      <div className="px-4 py-6 text-center text-gray-500 dark:text-gray-400">
        <p>未找到匹配的会话</p>
        <p className="text-sm mt-1">试试其他关键词</p>
      </div>
    )}
    {isSearching && hasResults && (
      <div className="px-4 py-2 text-sm text-gray-600 dark:text-gray-400">
        找到 {filteredSessions.length} 个会话
      </div>
    )}
    <nav aria-label="项目导航">
      {!isSearching && <div className="desktop-chat-section-heading"><h2>项目</h2><button type="button" className="desktop-chat-icon-button hover-transition" disabled={props.disabled} onClick={props.onNewProject} aria-label="新建项目"><Plus size={16} /></button></div>}
      {!isSearching && props.projects.length === 0 && <p className="desktop-chat-project-empty">添加本地目录，整理相关对话</p>}
      {!isSearching && props.projects.map(project => {
        const projectSessions = filteredSessions.filter(session => session.project_id === project.id);
        return <section className="desktop-chat-project" key={project.id} aria-label={`项目 ${project.name}`}>
          <div className="desktop-chat-project-heading">
            <Folder size={16} aria-hidden="true" /><strong title={project.root_path}>{project.name}</strong>
            <button type="button" className="desktop-chat-icon-button hover-transition" disabled={props.disabled} onClick={() => props.onNewSession(project.id)} aria-label={`在 ${project.name} 新建对话`}><Plus size={15} /></button>
            <details className="desktop-chat-menu"><summary aria-label={`项目操作 ${project.name}`}><MoreHorizontal size={16} /></summary>
              <div className="desktop-chat-menu-panel" onClick={event => { if ((event.target as HTMLElement).closest('button')) event.currentTarget.closest('details')?.removeAttribute('open'); }} onChange={event => event.currentTarget.closest('details')?.removeAttribute('open')}><p className="desktop-chat-project-path">{project.root_path}</p>
                <button type="button" disabled={props.disabled} onClick={() => props.onRenameProject(project)}>重命名项目</button>
                <button type="button" className="danger-action" disabled={props.disabled} onClick={() => props.onDeleteProject(project)}>删除项目</button>
              </div>
            </details>
          </div>
          {!project.available && <p className="desktop-chat-directory-warning" role="status">目录不可用</p>}
          <details open className="desktop-chat-project-sessions"><summary><ChevronDown size={12} aria-hidden="true" />对话 ({props.sessions.filter(session => session.project_id === project.id).length})</summary>
            {projectSessions.map(sessionRow)}
            {projectSessions.length === 0 && <p className="desktop-chat-project-empty">暂无会话</p>}
          </details>
        </section>;
      })}
      {isSearching && hasResults && (
        <section className="desktop-chat-search-results" aria-label="搜索结果">
          <h2 className="px-4 py-2 font-semibold text-sm text-gray-700 dark:text-gray-300">搜索结果</h2>
          {filteredSessions.map(sessionRow)}
        </section>
      )}
      {!isSearching && <section className="desktop-chat-independent" aria-label="独立会话"><h2>独立会话</h2>
        {independent.map(sessionRow)}
        {independent.length === 0 && <p className="desktop-chat-project-empty">暂无独立会话</p>}
      </section>}
    </nav>
  </aside>;
});

ProjectNavigation.displayName = 'ProjectNavigation';
