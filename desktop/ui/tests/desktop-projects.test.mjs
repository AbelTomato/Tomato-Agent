import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { transformWithOxc } from 'vite';
let slots = [], cursor = 0, effects = [], calls = [], handler, listeners;
globalThis.window = { setInterval: () => 1, clearInterval() {} };
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
  ["import { desktopClient } from '../desktop/client';", 'const desktopClient = { listen: globalThis.__chatListen, invoke: globalThis.__chatInvoke };'],
  ["import { ProjectNavigation } from './ProjectNavigation';", 'const ProjectNavigation = () => null;'],
]);
function nodes(tree, match = () => true) {
  if (!tree || typeof tree !== 'object') return [];
  return [...(match(tree) ? [tree] : []), ...[tree.props?.children].flat(Infinity).flatMap(child => nodes(child, match))];
}
const ofType = (tree, type) => nodes(tree, node => node.type === type);
const textOf = tree => typeof tree === 'string' || typeof tree === 'number' ? String(tree) : Array.isArray(tree) ? tree.map(textOf).join('') : tree?.props ? textOf(tree.props.children) : '';
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
  handler = (name, args) => name in overrides ? overrides[name](args) : ({ desktop_chat_status: { configured: false }, desktop_chat_sessions: [s1, s2], desktop_chat_projects: [p1], desktop_chat_latest_session: 's1', desktop_chat_history: [{ id: args?.sessionId, role: 'user', content: `history ${args?.sessionId}` }], desktop_agent_session_tasks: [] })[name];
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
test('memory usage and source render under the matching assistant reply after history reload', async () => {
  const reply = { id: 'assistant-1', session_id: 's1', role: 'assistant', content: '按中文偏好回答', created_at: '2026-11-10T00:00:00Z' };
  let tree = await setup({
    desktop_chat_history: () => [reply],
    desktop_chat_memory_contexts: () => ({ body: { contexts: [{ assistant_message_id: reply.id, generation: 4, retrieval_mode: 'keyword',
      items: [{ item_id: 'memory-1', version: 2, kind: 'preference', topic_key: 'response.language', content: '默认使用简体中文',
        sources: [{ session_id: 'source-session', message_id: 'source-message', source_revision: 1, excerpt: '记住我喜欢中文回答' }] }], unavailable_count: 1 }] } }),
  });
  tree = render();
  assert.match(textOf(tree), /本轮使用的长期记忆 · 1/);
  assert.match(textOf(tree), /默认使用简体中文/);
  assert.match(textOf(tree), /记住我喜欢中文回答/);
  assert.match(textOf(tree), /另有 1 条记忆已被忘记或来源已删除/);
  assert.deepEqual(calls.find(([name]) => name === 'desktop_chat_memory_contexts')[1], { sessionId: 's1' });
});
test('memory fallback notice remains visible when the source outbox reports pending afterward', async () => {
  let tree = await setup();
  listeners.desktop_memory_recall_status({ payload: { session_id: 's1', status: 'failed', error: 'memory engine unavailable' } });
  listeners.desktop_memory_ingest_status({ payload: { session_id: 's1', status: 'pending' } });
  tree = render();
  assert.match(textOf(tree), /本轮未使用记忆/);
  assert.doesNotMatch(textOf(tree), /本轮对话来源待同步/);
  const textarea = ofType(tree, 'textarea').find(node => node.props['aria-label'] === '消息');
  textarea.props.onChange({ target: { value: 'next turn' } }); tree = render();
  const composer = ofType(tree, 'form').find(node => node.props.className === 'desktop-chat-composer');
  await composer.props.onSubmit(event); tree = render();
  assert.doesNotMatch(textOf(tree), /本轮未使用记忆/);
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
  let tree = await setup({ desktop_chat_delete_session: () => { deleted = true; return { pending_memory_sync: 1 }; }, desktop_chat_sessions: () => deleted ? [s2] : [s1, s2] });
  nav(tree).onDeleteSession(s1); tree = render(); assert.match(textOf(dialog(tree)), /全部消息.*无法撤销/);
  await dialog(tree).props.onSubmit(event); tree = render();
  assert.deepEqual(calls.find(([name]) => name === 'desktop_chat_delete_session')[1], { sessionId: 's1' });
  assert.equal(nav(tree).sessionId, 's2'); assert.match(textOf(tree), /history s2/);
  assert.match(textOf(tree), /记忆来源删除将在引擎连接后同步/);
});

test('memory setting loads persisted state and applies changes through the desktop command', async () => {
  let tree = await setup({ desktop_memory_sync_status: () => ({ enabled: false, pending_deletions: false, pending_count: 0 }),
    desktop_memory_set_enabled: ({ enabled }) => ({ enabled }) });
  nav(tree).onSettings(); await flush(); tree = render();
  const checkbox = ofType(tree, 'input').find(node => node.props.type === 'checkbox');
  assert.ok(checkbox);
  assert.equal(checkbox.props.checked, false);
  checkbox.props.onChange({ target: { checked: true } }); await flush(); tree = render();
  assert.deepEqual(calls.find(([name]) => name === 'desktop_memory_set_enabled')[1], { enabled: true });
  assert.equal(ofType(tree, 'input').find(node => node.props.type === 'checkbox').props.checked, true);
  assert.match(textOf(tree), /新对话内容会提交给当前模型/);
});

test('memory manager loads items and sources, applies versioned correction, and reviews candidates', async () => {
  const item = { item_id: 'item-1', kind: 'preference', scope: 'personal', scope_id: 'windows-local', topic_key: 'response.language',
    status: 'active', confirmation: 'explicit', version: 2, content: '请用中文回答', updated_at: '2026-11-10T00:00:00Z' };
  const candidate = { candidate_id: 'candidate-1', kind: 'habit', topic_key: 'work.schedule', content: '通常在上午写代码', confidence: 0.8,
    reason: 'needs_review', created_at: '2026-11-10T00:00:00Z', sources: [] };
  let corrected = false, decided = false;
  let tree = await setup({
    desktop_memory_sync_status: () => ({ enabled: true, pending_deletions: false, pending_count: 0, pending_ingestions: 0, pending_mutations: 0 }),
    desktop_memory_items: () => ({ body: { items: [{ ...item, content: corrected ? '优先使用简体中文' : item.content }] } }),
    desktop_memory_candidates: () => ({ body: { candidates: decided ? [] : [candidate] } }),
    desktop_memory_status: () => ({ body: { generation: 8, ready: true, state: 'ready' } }),
    desktop_memory_item: () => ({ body: { ...item, content: corrected ? '优先使用简体中文' : item.content,
      sources: [{ source_system: 'tomato-desktop', session_id: 's1', message_id: 'm1', message_at: null, excerpt: '记住我喜欢中文回答', status: 'active' }] } }),
    desktop_memory_correct: ({ input }) => { corrected = true; assert.deepEqual(input, { itemId: 'item-1', expectedVersion: 2, content: '优先使用简体中文' }); },
    desktop_memory_candidate_decision: ({ input, action }) => { decided = true; assert.deepEqual(input, { candidateId: 'candidate-1' }); assert.equal(action, 'accept'); },
  });
  nav(tree).onSettings(); await flush(); tree = render();
  button(tree, '管理已保存的记忆').props.onClick(); await flush(); tree = render();
  assert.match(textOf(tree), /请用中文回答/);
  ofType(tree, 'button').find(node => textOf(node).startsWith('请用中文回答')).props.onClick(); await flush(); tree = render();
  assert.match(textOf(tree), /记住我喜欢中文回答/);
  const textarea = ofType(tree, 'textarea').find(node => node.props.maxLength === 4000);
  textarea.props.onChange({ target: { value: '优先使用简体中文' } }); tree = render();
  button(tree, '保存更正').props.onClick(); await flush(); tree = render();
  assert.equal(corrected, true); assert.match(textOf(tree), /记忆已更正/);
  ofType(tree, 'button').find(node => textOf(node).startsWith('待确认')).props.onClick(); tree = render();
  assert.match(textOf(tree), /通常在上午写代码/);
  button(tree, '接受').props.onClick(); await flush(); tree = render();
  assert.equal(decided, true); assert.match(textOf(tree), /已接受这条候选记忆/);
});

test('forget reports pending sync and clear requires a second confirmation with current generation', async () => {
  const item = { item_id: 'item-2', kind: 'background', scope: 'personal', scope_id: 'windows-local', topic_key: 'work.role',
    status: 'active', confirmation: 'explicit', version: 1, content: '我是软件工程师', updated_at: '2026-11-10T00:00:00Z' };
  let forgetResult, clearResult, clears = 0, forgotten = false;
  let tree = await setup({
    desktop_memory_sync_status: () => ({ enabled: true, pending_deletions: false, pending_count: 0, pending_ingestions: 0, pending_mutations: 0 }),
    desktop_memory_items: () => ({ body: { items: [item] } }), desktop_memory_candidates: () => ({ body: { candidates: [] } }),
    desktop_memory_status: () => ({ body: { generation: 21, ready: true, state: 'ready' } }),
    desktop_memory_item: () => { if (forgotten) throw new Error('memory_not_found'); return { body: { ...item, sources: [] } }; },
    desktop_memory_forget: ({ input, expectedVersion }) => { assert.deepEqual(input, { itemId: 'item-2' }); assert.equal(expectedVersion, 1); forgotten = true; return (forgetResult = { status: 'pending_sync' }); },
    desktop_memory_clear: ({ input }) => { assert.deepEqual(input, { expectedGeneration: 21 }); clears += 1; return (clearResult = { status: 'pending_sync' }); },
  });
  nav(tree).onSettings(); await flush(); tree = render(); button(tree, '管理已保存的记忆').props.onClick(); await flush(); tree = render();
  ofType(tree, 'button').find(node => textOf(node).startsWith('我是软件工程师')).props.onClick(); await flush(); tree = render();
  button(tree, '忘记这条记忆').props.onClick(); await flush(); tree = render();
  assert.equal(forgetResult.status, 'pending_sync'); assert.match(textOf(tree), /删除待引擎恢复后同步/); assert.doesNotMatch(textOf(tree), /memory_not_found/);
  ofType(tree, 'button').find(node => textOf(node).startsWith('已保存')).props.onClick(); tree = render();
  button(tree, '清空全部个人记忆').props.onClick(); tree = render(); assert.equal(clears, 0);
  button(tree, '再次点击确认清空全部个人记忆').props.onClick(); await flush(); tree = render();
  assert.equal(clearResult.status, 'pending_sync'); assert.equal(clears, 1); assert.match(textOf(tree), /清空待引擎恢复后同步/);
});

test('memory export invokes the scoped export command and downloads JSON explicitly', async () => {
  const previousCreateObjectURL = URL.createObjectURL;
  const previousRevokeObjectURL = URL.revokeObjectURL;
  const previousDocument = globalThis.document;
  const downloads = [];
  URL.createObjectURL = blob => { downloads.push({ blob }); return 'blob:memory-export'; };
  URL.revokeObjectURL = value => downloads.push({ revoked: value });
  globalThis.document = { createElement: tag => ({ tag, click() { downloads.push({ href: this.href, name: this.download }); } }) };
  try {
    let tree = await setup({
      desktop_memory_sync_status: () => ({ enabled: true, pending_deletions: false, pending_count: 0, pending_ingestions: 0, pending_mutations: 0 }),
      desktop_memory_items: () => ({ body: { items: [{ item_id: 'one', content: '中文偏好' }] } }),
      desktop_memory_candidates: () => ({ body: { candidates: [] } }),
      desktop_memory_status: () => ({ body: { generation: 1, ready: true, state: 'ready' } }),
      desktop_memory_export: () => ({ body: { scope: 'personal', scope_id: 'windows-local', items: [{ item_id: 'one', content: '中文偏好' }] } }),
    });
    nav(tree).onSettings(); await flush(); tree = render(); button(tree, '管理已保存的记忆').props.onClick(); await flush(); tree = render();
    button(tree, '导出个人记忆 JSON').props.onClick(); await flush(); tree = render();
    assert.ok(calls.some(([name]) => name === 'desktop_memory_export'));
    assert.equal(downloads[1].href, 'blob:memory-export');
    assert.match(downloads[1].name, /^tomato-agent-memory-\d{4}-\d{2}-\d{2}\.json$/);
    assert.match(textOf(tree), /已导出个人记忆 JSON/);
  } finally {
    URL.createObjectURL = previousCreateObjectURL;
    URL.revokeObjectURL = previousRevokeObjectURL;
    globalThis.document = previousDocument;
  }
});

test('late events from an earlier generation cannot alter the current conversation', async () => {
  let finish;
  let tree = await setup({ desktop_chat_status: () => ({ configured: true }), desktop_chat_send: () => new Promise(resolve => { finish = resolve; }) });
  ofType(tree, 'textarea')[0].props.onChange({ target: { value: 'question' } });
  tree = render();
  const composer = ofType(tree, 'form').find(node => node.props.className === 'desktop-chat-composer');
  const sending = composer.props.onSubmit(event); await flush();
  const requestId = calls.find(([name]) => name === 'desktop_chat_send')[1].input.requestId;
  listeners.desktop_chat_delta({ payload: { session_id: 's1', request_id: 'old-request', delta: 'WRONG' } });
  listeners.desktop_chat_completed({ payload: { session_id: 's1', request_id: 'old-request' } });
  tree = render(); assert.doesNotMatch(textOf(tree), /WRONG/); assert.ok(button(tree, '停止'));
  listeners.desktop_chat_delta({ payload: { session_id: 's1', request_id: requestId, delta: 'RIGHT' } });
  tree = render(); assert.match(textOf(tree), /RIGHT/);
  finish(); await sending;
});

test('assistant mode submits through the managed engine with the selected desktop session', async () => {
  let tree = await setup({ desktop_chat_status: () => ({ configured: true }), desktop_agent_submit_task: () => ({
    run_id: 'run-1', session_id: 'engine-session', kind: 'assistant', status: 'queued', version: 0,
    phase: 'ready_model', capabilities: ['cancel'], result: null, pending_action: null, error: null,
  }), desktop_agent_task_status: () => ({ run_id: 'run-1', session_id: 'engine-session', kind: 'assistant', status: 'queued', version: 0, phase: 'ready_model', capabilities: ['cancel'], result: null, pending_action: null, error: null }), desktop_agent_task_events: () => ({ body: { events: [] } }) });
  const modeButton = button(tree, '助手任务'); modeButton.props.onClick(); tree = render();
  ofType(tree, 'textarea').find(node => node.props['aria-label'] === '任务内容').props.onChange({ target: { value: '总结这个项目' } });
  tree = render();
  const composer = ofType(tree, 'form').find(node => node.props.className === 'desktop-chat-composer');
  await composer.props.onSubmit(event); tree = render();
  const call = calls.find(([name]) => name === 'desktop_agent_submit_task');
  assert.deepEqual(call[1].input, { sessionId: 's1', content: '总结这个项目', requestId: call[1].input.requestId, kind: 'assistant' });
  assert.match(call[1].input.requestId, /^[0-9a-f-]{36}$/);
  assert.match(textOf(tree), /助手任务.*排队中/);
  assert.equal(button(tree, '取消任务') !== undefined, true);
});

test('writing mode exposes versioned outline and draft save confirmations', async () => {
  const waiting = { run_id: 'write-run', session_id: 'engine-session', kind: 'writing', status: 'waiting', version: 2,
    phase: 'waiting', capabilities: ['confirm_outline', 'cancel'], result: { outline: { sections: ['A'] } }, error: null,
    pending_action: { action_id: 'outline-2', reason: 'confirm_outline', allowed_actions: ['confirm_outline', 'cancel'],
      display_data: { outline: { sections: ['A'] } } } };
  let tree = await setup({ desktop_chat_status: () => ({ configured: true }),
    desktop_agent_submit_task: input => ({ ...waiting, status: 'queued', submitted: input }),
    desktop_agent_task_action: input => ({ body: { ...waiting, status: 'queued', version: 3, action: input.input } }) });
  const writingMode = button(tree, '写作'); writingMode.props.onClick(); tree = render();
  ofType(tree, 'textarea').find(node => node.props['aria-label'] === '写作主题').props.onChange({ target: { value: '主题' } });
  tree = render();
  const composer = ofType(tree, 'form').find(node => node.props.className === 'desktop-chat-composer');
  await composer.props.onSubmit({ preventDefault() {} }); tree = render();
  assert.deepEqual(calls.find(([name]) => name === 'desktop_agent_submit_task')[1].input.kind, 'writing');
  tree = await setup({ desktop_agent_session_tasks: () => [waiting], desktop_agent_task_status: () => waiting,
    desktop_agent_task_events: () => ({ body: { events: [] } }), desktop_agent_task_action: input => ({ body: { ...waiting, status: 'queued', action: input.input } }) });
  await flush(); tree = render();
  await button(tree, '确认提纲并生成草稿').props.onClick(); await flush();
  const action = calls.find(([name]) => name === 'desktop_agent_task_action')[1].input;
  assert.equal(action.action, 'confirm_outline');
  assert.deepEqual(action.payload, { outline: { sections: ['A'] } });
});

test('assistant workspace binding lists only available roots and sends the selected ID', async () => {
  const waiting = { run_id: 'bind-run', session_id: 'engine-session', kind: 'assistant', status: 'waiting', version: 4,
    phase: 'waiting', capabilities: ['bind_workspace', 'reject_action'], result: null, error: null,
    pending_action: { action_id: 'bind-4', reason: 'workspace_required', allowed_actions: ['bind_workspace', 'reject_action'], display_data: {} } };
  let tree = await setup({ desktop_agent_session_tasks: () => [waiting], desktop_agent_task_status: () => waiting,
    desktop_agent_task_events: () => ({ body: { events: [] } }),
    desktop_agent_workspaces: () => [{ workspace_id: 'allowed-id', repo_path: 'C:/work/allowed', available: true },
      { workspace_id: 'missing-id', available: false }],
    desktop_agent_task_action: input => ({ body: { ...waiting, status: 'queued', action: input.input } }) });
  await flush(); await flush(); await flush(); await flush(); tree = render();
  const select = ofType(tree, 'select')[0];
  assert.ok(select);
  assert.deepEqual(ofType(tree, 'option').map(option => option.props.value).filter(Boolean), ['allowed-id']);
  select.props.onChange({ target: { value: 'allowed-id' } }); tree = render();
  const form = ofType(tree, 'form').find(node => node.props.onSubmit && textOf(node).includes('绑定并继续'));
  assert.ok(form);
  await form.props.onSubmit({ preventDefault() {} }); await flush();
  const action = calls.find(([name]) => name === 'desktop_agent_task_action')[1].input;
  assert.equal(action.action, 'bind_workspace');
  assert.deepEqual(action.payload, { workspace_id: 'allowed-id' });
});

test('assistant workspace request lets the user choose and explicitly register a directory', async () => {
  const waiting = { run_id: 'bind-run', session_id: 'engine-session', kind: 'assistant', status: 'waiting', version: 4,
    phase: 'waiting', capabilities: ['bind_workspace'], result: null, error: null,
    pending_action: { action_id: 'bind-4', reason: 'workspace_required', allowed_actions: ['bind_workspace'], display_data: {} } };
  let tree = await setup({ desktop_agent_session_tasks: () => [waiting], desktop_agent_task_status: () => waiting,
    desktop_agent_task_events: () => ({ body: { events: [] } }), desktop_agent_workspaces: () => [],
    desktop_chat_pick_project_directory: () => 'C:/user-selected/project',
    desktop_agent_register_workspace: () => ({ workspace_id: 'selected-id', repo_path: 'C:/user-selected/project', available: true }) });
  await flush(); await flush(); await flush(); tree = render();
  const select = ofType(tree, 'button').find(node => textOf(node) === '选择并授权新目录');
  assert.ok(select);
  await select.props.onClick(); await flush(); tree = render();
  assert.deepEqual(calls.filter(([name]) => name === 'desktop_agent_register_workspace')[0][1], { path: 'C:/user-selected/project' });
  assert.equal(calls.some(([name]) => name === 'desktop_chat_create_project'), false);
  const form = ofType(tree, 'form').find(node => node.props.onSubmit && textOf(node).includes('绑定并继续'));
  await form.props.onSubmit({ preventDefault() {} }); await flush();
  assert.deepEqual(calls.find(([name]) => name === 'desktop_agent_task_action')[1].input.payload, { workspace_id: 'selected-id' });
});

test('waiting assistant task displays clarification and submits the versioned answer action', async () => {
  const waiting = { run_id: '8e897cdc-0bf1-47cc-8b4a-6e85ce278232', session_id: 'engine-session', kind: 'assistant',
    status: 'waiting', version: 3, phase: 'waiting', capabilities: ['provide_input', 'cancel'], result: null, error: null,
    pending_action: { action_id: 'action-3', action_type: 'clarification', reason: '需要确认目标',
      allowed_actions: ['provide_input', 'cancel'], display_data: { choices: ['简要', '详细'] } } };
  let tree = await setup({ desktop_agent_session_tasks: () => [waiting], desktop_agent_task_status: () => waiting,
    desktop_agent_task_events: () => ({ body: { events: [] } }), desktop_agent_task_action: () => ({ body: { ...waiting, status: 'queued', version: 4 } }) });
  await flush(); await flush(); tree = render();
  assert.match(textOf(tree), /需要确认目标.*简要.*详细/);
  const answer = ofType(tree, 'textarea').at(-2);
  answer.props.onChange({ target: { value: '简要' } }); tree = render();
  const clarify = ofType(tree, 'form').find(node => node.props.onSubmit && node.props.className !== 'desktop-chat-composer');
  await clarify.props.onSubmit(event); await flush(); tree = render();
  const call = calls.find(([name]) => name === 'desktop_agent_task_action');
  assert.ok(call, JSON.stringify(calls));
  assert.equal(call[1].sessionId, 's1');
  assert.equal(call[1].runId, waiting.run_id);
  assert.deepEqual(call[1].input, { action_id: 'action-3', action: 'provide_input', expected_version: 3,
    client_request_id: call[1].input.client_request_id, payload: { message: '简要' } });
  assert.match(call[1].input.client_request_id, /^[0-9a-f-]{36}$/);
});
