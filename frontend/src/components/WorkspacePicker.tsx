import { useState } from 'react';
import { type Workspace } from '../api';

export function WorkspacePicker({ workspaces, value, disabled, onSelect, onRegister }: {
  workspaces: Workspace[]; value: string; disabled: boolean;
  onSelect: (id: string) => void; onRegister: (path: string, targets: string[]) => Promise<void>;
}) {
  const [path, setPath] = useState('');
  const [targets, setTargets] = useState('tests');
  return <details className="workspace-picker"><summary>原目录工作区</summary>
    <label>选择项目<select value={value} disabled={disabled} onChange={(event) => onSelect(event.target.value)}>
      <option value="">未绑定工作区</option>
      {workspaces.map((item) => <option key={item.workspace_id} value={item.workspace_id} disabled={item.available === false}>
        {item.repo_path ?? item.workspace_id}{item.available === false ? '（不可用）' : ''}
      </option>)}
    </select></label>
    <label>后端可访问的绝对目录<input value={path} disabled={disabled} onChange={(event) => setPath(event.target.value)} placeholder="/home/user/python-project" /></label>
    <label>pytest 目标（逗号分隔）<input value={targets} disabled={disabled} onChange={(event) => setTargets(event.target.value)} /></label>
    <p className="notice">代码任务直接修改该目录。验证限定 Python/pytest、固定 Docker 镜像和只读挂载；取消不会撤销修改。</p>
    <button type="button" disabled={disabled || !path.trim() || !targets.trim()} onClick={() => void onRegister(path.trim(), targets.split(',').map((item) => item.trim()).filter(Boolean))}>注册并绑定目录</button>
  </details>;
}