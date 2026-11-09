import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { transformWithOxc } from 'vite';
let slots = [], cursor = 0, effects = [], calls = [], handler, listeners;
globalThis.__chatHooks = {
  useState(value) { const i = cursor++; if (!(i in slots)) slots[i] = value; return [slots[i], value => { slots[i] = typeof value === 'function' ? value(slots[i]) : value; }]; },
  useRef(value) { const i = cursor++; return slots[i] ??= { current: value }; },
  useEffect(effect, deps) { const i = cursor++; if (!slots[i] || deps.some((value, j) => value !== slots[i][j])) { slots[i] = deps; effects.push(effect); } },
};
globalThis.__chatInvoke = (name, args) => { calls.push([name, args]); return Promise.resolve().then(() => handler(name, args)); };
globalThis.__chatListen = async (name, callback) => { listeners[name] = callback; return () => {}; };
async function load(file, replacements) {
  let source = await readFile(new URL(file, import.meta.url), 'utf8');
  for (const [from, to] of replacements) source = source.replace(from, to);
  const { code } = await transformWithOxc(source, file, { jsx: { runtime: 'automatic' } });
  return import(`data:text/javascript;base64,${Buffer.from(code.replaceAll('"react/jsx-runtime"', JSON.stringify(import.meta.resolve('react/jsx-runtime')))).toString('base64')}`);
}
const { ProjectNavigation } = await load('../src/desktop-chat/ProjectNavigation.tsx', [
  [/import \{[^}]+\} from 'lucide-react';/, 'const ChevronDown = () => null, Folder = () => null, MessageSquare = () => null, MoreHorizontal = () => null, Plus = () => null, Settings = () => null;'],
]);
const { DesktopChatApp } = await load('../src/desktop-chat/DesktopChatApp.tsx', [
  ["import { useEffect, useRef, useState } from 'react';", 'const { useEffect, useRef, useState } = globalThis.__chatHooks;'],
  ["import { listen } from '@tauri-apps/api/event';", 'const listen = globalThis.__chatListen;'],
  ["import { invoke } from '@tauri-apps/api/core';", 'const invoke = globalThis.__chatInvoke;'],
  ["import { ProjectNavigation } from './ProjectNavigation';", 'const ProjectNavigation = () => null;'],
]);
function nodes(tree, match = () => true) {
  if (!tree || typeof tree !== 'object') return [];
  return [...(match(tree) ? [tree] : []), ...[tree.props?.children].flat(Infinity).flatMap(child => nodes(child, match))];
}
const ofType = (tree, type) => nodes(tree, node => node.type === type);
const textOf = tree => typeof tree === 'string' ? tree : Array.isArray(tree) ? tree.map(textOf).join('') : tree?.props ? textOf(tree.props.children) : '';
const button = (tree, label) => ofType(tree, 'button').find(node => textOf(node) === label || node.props['aria-label'] === label);
const flush = () => new Promise(resolve => setImmediate(resolve));
function render() { cursor = 0; effects = []; const tree = DesktopChatApp(); effects.forEach(effect => effect()); return tree; }
const nav = tree => nodes(tree, node => node.type?.name === 'ProjectNavigation')[0].props;
const dialog = tree => ofType(tree, 'form').find(node => node.props.role === 'dialog');
const event = { preventDefault() {} };
const p1 = { id: 'p1', name: '项目', root_path: 'C:\\项目 with spaces', available: false };
const s1 = { id: 's1', title: '项目对话', project_id: 'p1' };
const s2 = { id: 's2', title: '独立对话', project_id: null };
async function setup(overrides = {}) {
  slots = []; listeners = {}; calls = [];
  handler = (name, args) => name in overrides ? overrides[name](args) : ({ desktop_chat_status: { configured: false }, desktop_chat_sessions: [s1, s2], desktop_chat_projects: [p1], desktop_chat_latest_session: 's1', desktop_chat_history: [{ id: args?.sessionId, role: 'user', content: `history ${args?.sessionId}` }] })[name];
  render(); await flush(); return render();
}
test('navigation groups chats, keeps orphans visible, and forwards create/select/move/delete actions', () => {
  const actions = [];
  const tree = ProjectNavigation({ projects: [p1], sessions: [s1, s2, { id: 'orphan', title: '遗留对话', project_id: 'missing' }], sessionId: 's1', disabled: false,
    onNewSession: id => actions.push(['new', id]), onSelectSession: id => actions.push(['select', id]), onMoveSession: (session, id) => actions.push(['move', session.id, id]), onDeleteProject: project => actions.push(['delete', project.id]),
  });
  const sections = ofType(tree, 'section');
  assert.match(textOf(sections[0]), /项目对话/); assert.doesNotMatch(textOf(sections[0]), /独立对话/);
  assert.match(textOf(sections[1]), /独立对话.*遗留对话/); assert.match(textOf(tree), /目录不可用/);
  assert.equal(button(tree, '项目对话').props['aria-current'], 'page');
  button(tree, '在 项目 新建对话').props.onClick(); button(tree, '独立对话').props.onClick();
  ofType(tree, 'select')[0].props.onChange({ target: { value: '' } }); button(tree, '删除项目').props.onClick();
  assert.deepEqual(actions, [['new', 'p1'], ['select', 's2'], ['move', 's1', ''], ['delete', 'p1']]);
  const empty = ProjectNavigation({ projects: [], sessions: [], disabled: true });
  assert.match(textOf(empty), /添加本地目录.*暂无独立会话/); assert.equal(button(empty, '新建对话').props.disabled, true);
});
test('cancelled directory picker reports cancellation without creating records', async () => {
  let tree = await setup({ desktop_chat_pick_project_directory: () => null });
  nav(tree).onNewProject(); await flush(); tree = render();
  assert.equal(dialog(tree), undefined); assert.match(textOf(tree), /已取消目录选择/);
  assert.equal(calls.some(([name]) => name === 'desktop_chat_create_project'), false);
});
test('picker opens editable dialog; duplicate failure retains input and can be cancelled', async () => {
  let tree = await setup({ desktop_chat_pick_project_directory: () => 'C:\\项目 with spaces', desktop_chat_create_project: () => { throw new Error('该目录已经绑定项目'); } });
  nav(tree).onNewProject(); await flush(); tree = render();
  assert.equal(ofType(dialog(tree), 'input')[0].props.value, '项目 with spaces');
  await dialog(tree).props.onSubmit(event); tree = render(); assert.match(textOf(dialog(tree)), /该目录已经绑定项目/);
  assert.deepEqual(calls.find(([name]) => name === 'desktop_chat_create_project')[1], { input: { rootPath: 'C:\\项目 with spaces', name: '项目 with spaces' } });
  button(dialog(tree), '取消').props.onClick(); assert.equal(dialog(render()), undefined);
});
test('project deletion requires confirmation and preserves the selected chat after refresh', async () => {
  let deleted = false;
  let tree = await setup({ desktop_chat_delete_project: () => { deleted = true; }, desktop_chat_projects: () => deleted ? [] : [p1], desktop_chat_sessions: () => deleted ? [{ ...s1, project_id: null }, s2] : [s1, s2] });
  nav(tree).onDeleteProject(p1); tree = render(); assert.match(textOf(dialog(tree)), /会话会回到独立会话列表.*磁盘目录将保留/);
  button(dialog(tree), '取消').props.onClick(); assert.equal(calls.some(([name]) => name === 'desktop_chat_delete_project'), false);
  nav(render()).onDeleteProject(p1); await dialog(render()).props.onSubmit(event); tree = render();
  assert.equal(dialog(tree), undefined); assert.equal(nav(tree).sessionId, 's1'); assert.match(textOf(tree), /history s1/); assert.equal(nav(tree).sessions[0].project_id, null);
});
test('moving in and out passes nullable ownership without loading another history', async () => {
  let tree = await setup(); nav(tree).onMoveSession(s2, 'p1'); await flush(); tree = render(); nav(tree).onMoveSession(s2, ''); await flush();
  assert.deepEqual(calls.filter(([name]) => name === 'desktop_chat_set_session_project').map(([, args]) => args), [{ input: { sessionId: 's2', projectId: 'p1' } }, { input: { sessionId: 's2', projectId: null } }]);
  assert.equal(calls.filter(([name]) => name === 'desktop_chat_history').length, 1);
});
test('late history cannot overwrite a newly selected chat', async () => {
  let resolveOld;
  let tree = await setup({ desktop_chat_latest_session: () => null, desktop_chat_history: ({ sessionId }) => sessionId === 's1' ? new Promise(resolve => { resolveOld = resolve; }) : [{ id: 'current', role: 'user', content: 'current s2' }] });
  nav(tree).onSelectSession('s1'); await flush(); nav(render()).onSelectSession('s2'); await flush();
  resolveOld([{ id: 'late', role: 'user', content: 'stale s1' }]); await flush(); tree = render();
  assert.match(textOf(tree), /current s2/); assert.doesNotMatch(textOf(tree), /stale s1/); assert.equal(nav(tree).sessionId, 's2');
  assert.equal(calls.filter(([name]) => name === 'desktop_chat_select_session').at(-1)[1].sessionId, 's2');
});
test('new project chat is atomic and repeated clicks are ignored while pending', async () => {
  let finish;
  let tree = await setup({ desktop_chat_create_session: () => new Promise(resolve => { finish = resolve; }) });
  nav(tree).onNewSession('p1'); nav(tree).onNewSession('p1'); await flush(); assert.equal(nav(render()).disabled, true);
  assert.equal(calls.filter(([name]) => name === 'desktop_chat_create_session').length, 1); assert.deepEqual(calls.find(([name]) => name === 'desktop_chat_create_session')[1], { projectId: 'p1' });
  finish('new-id'); await flush(); tree = render(); assert.equal(nav(tree).sessionId, 'new-id'); assert.equal(nav(tree).disabled, false);
});
test('operation failure is visible and re-enables navigation', async () => {
  let tree = await setup({ desktop_chat_set_session_project: () => { throw new Error('项目不存在'); } });
  nav(tree).onMoveSession(s2, 'p1'); await flush(); tree = render(); assert.match(textOf(tree), /项目不存在/); assert.equal(nav(tree).disabled, false);
});
test('renaming a project submits edited name and restores navigation', async () => {
  let tree = await setup(); nav(tree).onRenameProject(p1); tree = render();
  ofType(dialog(tree), 'input')[0].props.onChange({ target: { value: '新的项目名' } });
  await dialog(render()).props.onSubmit(event); tree = render();
  assert.deepEqual(calls.find(([name]) => name === 'desktop_chat_rename_project')[1], { projectId: 'p1', name: '新的项目名' });
  assert.equal(dialog(tree), undefined); assert.equal(nav(tree).disabled, false);
});
test('deleting current chat confirms message deletion and switches to remaining history', async () => {
  let deleted = false;
  let tree = await setup({ desktop_chat_delete_session: () => { deleted = true; }, desktop_chat_sessions: () => deleted ? [s2] : [s1, s2] });
  nav(tree).onDeleteSession(s1); tree = render(); assert.match(textOf(dialog(tree)), /全部消息.*无法撤销/);
  await dialog(tree).props.onSubmit(event); tree = render();
  assert.deepEqual(calls.find(([name]) => name === 'desktop_chat_delete_session')[1], { sessionId: 's1' });
  assert.equal(nav(tree).sessionId, 's2'); assert.match(textOf(tree), /history s2/);
});
