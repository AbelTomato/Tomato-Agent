import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { transformWithOxc } from 'vite';

const source = readFileSync(new URL('../src/components/ProjectSidebar.tsx', import.meta.url), 'utf8')
  .replace("import type { Project, Session } from '../api';", '');
const { code } = await transformWithOxc(source, 'ProjectSidebar.tsx', { jsx: { runtime: 'automatic' } });
const { ProjectSidebar } = await import(`data:text/javascript;base64,${Buffer.from(code.replace('"react/jsx-runtime"', JSON.stringify(import.meta.resolve('react/jsx-runtime')))).toString('base64')}`);

test('侧栏按项目组织会话，显示发行版、标题、独立对话及不可用项目状态', () => {
  let unavailable;
  const tree = ProjectSidebar({
    projects: [
      { project_id: 'p1', workspace_id: 'w1', display_name: '博客', root_path: '/home/blog', available: true },
      { project_id: 'p2', workspace_id: 'w2', display_name: '失效项目', root_path: '/missing', available: false },
    ],
    sessions: [
      { id: 's1', title: '文章草稿', metadata: {}, project_id: 'p1' },
      { id: 's2', title: '独立对话', metadata: {}, project_id: null },
    ],
    sessionId: 's1', environment: 'Ubuntu', busy: false,
    onNewSession() {}, onOpenProject() {}, onSelectSession() {}, onRenameSession() {},
    onShowUnavailable(project) { unavailable = project.project_id; },
  });
  const nodes = [];
  const visit = (node) => { if (Array.isArray(node)) return node.forEach(visit); if (node && typeof node === 'object') { nodes.push(node); visit(node.props?.children); } };
  visit(tree);
  assert.ok(nodes.some((node) => node.type === 'small' && node.props.children === 'Ubuntu'));
  assert.ok(nodes.some((node) => node.type === 'button' && node.props['aria-current'] === 'true' && node.props.children === '文章草稿'));
  assert.ok(nodes.some((node) => node.type === 'button' && node.props.children === '独立对话'));
  nodes.find((node) => node.type === 'button' && node.props['aria-label']?.startsWith('失效项目')).props.onClick();
  assert.equal(unavailable, 'p2');
});