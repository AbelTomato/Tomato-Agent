import { useEffect, useState } from 'react';
import { browseProjectDirectories, listProjectRoots, type ProjectDirectory } from '../api';

type Props = {
  onOpen: (path: string) => Promise<void>;
  onCreate: (parent: string, name: string) => Promise<void>;
  onClose: () => void;
};

export function ProjectDialog({ onOpen, onCreate, onClose }: Props) {
  const [path, setPath] = useState('');
  const [directories, setDirectories] = useState<ProjectDirectory[]>([]);
  const [name, setName] = useState('');
  const [mode, setMode] = useState<'open' | 'create'>('open');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function browse(next: string) {
    setBusy(true);
    setError('');
    try {
      const result = await browseProjectDirectories(next);
      setPath(result.path);
      setDirectories(result.directories);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '目录读取失败');
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    let mounted = true;
    void listProjectRoots().then((result) => {
      if (!mounted) return;
      setDirectories(result.roots);
    }).catch((reason) => {
      if (mounted) setError(reason instanceof Error ? reason.message : '无法读取已批准的 WSL 根目录');
    });
    return () => { mounted = false; };
  }, []);

  return <section role="dialog" aria-modal="true" aria-label="项目目录" className="project-dialog">
    <header><h2>{mode === 'open' ? '打开文件夹' : '新建项目'}</h2><button type="button" aria-label="关闭" onClick={onClose}>关闭</button></header>
    <p className="project-path" title={path}>{path || '已批准的 WSL 根目录'}</p>
    <div className="project-dialog-actions">
      {path && <button type="button" disabled={busy || !path || path === '/'} onClick={() => void browse(path.replace(/\/$/, '').replace(/\/[^/]+$/, '') || '/')}>上级目录</button>}
      <button type="button" aria-pressed={mode === 'open'} onClick={() => setMode('open')}>打开文件夹</button>
      <button type="button" aria-pressed={mode === 'create'} onClick={() => setMode('create')}>新建项目</button>
    </div>
    {error && <p role="alert" className="notice error">{error}</p>}
    <nav aria-label="WSL 目录" className="project-directory-list">
      {directories.map((directory) => <button type="button" key={directory.path} disabled={busy} onClick={() => void browse(directory.path)}>{directory.name}<span aria-hidden="true">›</span></button>)}
      {!busy && path && directories.length === 0 && <p>此目录没有可浏览的子目录。</p>}
    </nav>
    {mode === 'open' ? <button type="button" className="primary-button" disabled={busy || !path} onClick={async () => { setBusy(true); try { await onOpen(path); onClose(); } catch (reason) { setError(reason instanceof Error ? reason.message : '项目打开失败'); } finally { setBusy(false); } }}>打开此文件夹</button> : <form onSubmit={async (event) => { event.preventDefault(); setBusy(true); try { await onCreate(path, name); onClose(); } catch (reason) { setError(reason instanceof Error ? reason.message : '项目创建失败'); } finally { setBusy(false); } }}>
      <label>项目名称<input value={name} onChange={(event) => setName(event.target.value)} required maxLength={255} /></label>
      <button type="submit" className="primary-button" disabled={busy || !path || !name.trim()}>创建项目</button>
    </form>}
  </section>;
}