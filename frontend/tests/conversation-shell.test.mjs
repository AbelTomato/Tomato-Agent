import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { transformWithOxc } from 'vite';

let harness;
const hooks = {
  useState(initial) {
    const index = harness.index++;
    if (!(index in harness.slots)) harness.slots[index] = typeof initial === 'function' ? initial() : initial;
    return [harness.slots[index], (value) => {
      harness.slots[index] = typeof value === 'function' ? value(harness.slots[index]) : value;
      harness.dirty = true;
    }];
  },
  useRef(initial) { const index = harness.index++; return harness.slots[index] ??= { current: initial }; },
  useEffect(effect, deps) {
    const index = harness.index++;
    const previous = harness.slots[index];
    if (!previous || deps.some((dep, i) => !Object.is(dep, previous.deps[i]))) {
      harness.effects.push(() => { previous?.cleanup?.(); harness.slots[index] = { deps, cleanup: effect() }; });
    }
  },
};
globalThis.__conversationHooks = hooks;
const calls = [];
let settlementReader = async () => ({ settlements: [] });
let cardSettlementReader = async () => ({ settlements: [] });
let cardRiskAcceptor = async () => ({ settlements: [] });
let historyReader;
let eventReader;
let submitReader;
let actionReader;
let taskReader;
const session = (id) => ({ id, metadata: {}, default_workspace_id: 'ws' });
const task = (id, status = 'completed', kind = 'chat') => ({ run_id: id, session_id: 's1', kind, status,
  phase: 'finished', version: 3, capabilities: status === 'running' ? ['cancel'] : [],
  pending_action: null, result: { answer: `answer-${id}` }, messages: [], legacy: false });
globalThis.__conversationApi = {
  async listSessionQueue() { return { paused: false, requests: [] }; },
  async enqueueTask(id, body) { calls.push(['enqueue', id, body]); return { request_id: 'queued' }; },
  async steerTask(id, body) { calls.push(['steer', id, body]); return { request_id: 'steer', status: 'requested' }; },
  async controlSessionQueue() { return { paused: false, requests: [] }; },
  async listSessions() { return { sessions: [session('s1'), session('s2')] }; },
  async listWorkspaces() { return { workspaces: [{ workspace_id: 'ws', repo_path: '/project', available: true }] }; },
  async listSessionTasks(id, limit, offset, signal) { calls.push(['history', id, offset, signal]); return historyReader(id, offset); },
  async getTask(id, signal) { calls.push(['task', id, signal]); return taskReader(id); },
  async listTaskEvents(id, after, signal) { calls.push(['events', id, after, signal]); return eventReader(id, after); },
  async loadKnowledgeCapabilities() { return { retrieval_modes: ['keyword'], embedding_configured: false }; },
  async createSession() { return 's2'; },
  async submitTask(id, body) { calls.push(['submit', id, body]); return submitReader(id, body); },
  async actOnTask(id, body) { calls.push(['action', id, body]); return actionReader(id, body); },
  async setSessionWorkspace(id, workspace_id) { calls.push(['bind', id, workspace_id]); return { ...session(id), default_workspace_id: workspace_id }; },
  async registerWorkspace(path, targets) { calls.push(['register', path, targets]); return { workspace_id: 'registered', repo_path: path }; },
  async loadDocument(id) { return { document: { title: id }, chunks: [] }; },
};
async function compile(path, replacements) {
  let source = readFileSync(new URL(path, import.meta.url), 'utf8');
  for (const [pattern, replacement] of replacements) source = source.replace(pattern, replacement);
  const { code } = await transformWithOxc(source, path, { jsx: { runtime: 'automatic' } });
  return import(`data:text/javascript;base64,${Buffer.from(code.replace('"react/jsx-runtime"', JSON.stringify(import.meta.resolve('react/jsx-runtime')))).toString('base64')}`);
}
const { ConversationShell } = await compile('../src/components/ConversationShell.tsx', [
  [/import \{ useEffect, useRef, useState \} from 'react';/, 'const { useEffect, useRef, useState } = globalThis.__conversationHooks;'],
  [/import \{.*?\} from '..\/api';/s, 'const { createSession, listSessions, listSessionTasks, submitTask, getTask, listTaskEvents, actOnTask, listWorkspaces, registerWorkspace, setSessionWorkspace, loadKnowledgeCapabilities, loadDocument, listSessionQueue, enqueueTask, controlSessionQueue, steerTask } = globalThis.__conversationApi;'],
  [/import \{ TaskCard, taskLabels \} from '.\/TaskCard';/, 'const TaskCard = "task-card", taskLabels = { assistant: "助手", chat: "普通聊天", knowledge: "知识问答", writing: "技术写作", code: "代码任务" };'],
  [/import \{ WorkspacePicker \} from '.\/WorkspacePicker';/, 'const WorkspacePicker = "workspace-picker";'],
]);
globalThis.__cardApi = {
  async loadCodeTaskArtifact() { return { content: 'diff-content' }; },
  async listEffectSettlements(...args) { return cardSettlementReader(...args); },
  async acceptEffectConflictRisk(...args) { return cardRiskAcceptor(...args); },
};
const { TaskCard, CodeTestResults } = await compile('../src/components/TaskCard.tsx', [
  [/import \{ useEffect, useRef, useState \} from 'react';/, 'const { useEffect, useRef, useState } = globalThis.__conversationHooks;'],
  [/import \{.*?\} from '..\/api';/s, 'const { loadCodeTaskArtifact, listEffectSettlements, acceptEffectConflictRisk } = globalThis.__cardApi;'],
  [/import \{ CitationList \} from '.\/CitationList';/, 'const CitationList = "citations";'],
  [/import \{ AssistantResult \} from '.\/AssistantResult';/, 'const AssistantResult = "assistant-result";'],
  [/import \{ PendingActionPanel \} from '.\/PendingActionPanel';/, 'const PendingActionPanel = "pending-action-panel";'],
]);
const { WorkspacePicker } = await compile('../src/components/WorkspacePicker.tsx', [
  [/import \{ useState \} from 'react';/, 'const { useState } = globalThis.__conversationHooks;'],
]);

let render;
function mount(component = () => ConversationShell()) { render = component; harness = { slots: [], effects: [], index: 0, dirty: true, tree: null }; }
async function flush() {
  for (let i = 0; i < 50; i++) {
    if (harness.dirty) {
      harness.dirty = false; harness.index = 0; harness.tree = render();
      harness.effects.splice(0).forEach((effect) => effect());
    }
    await new Promise((resolve) => setImmediate(resolve));
    if (!harness.dirty) return;
  }
  assert.fail('组件未稳定');
}
function unmount() { harness.slots.forEach((slot) => slot?.cleanup?.()); }
function elements(node = harness.tree) {
  if (!node || typeof node !== 'object') return [];
  if (Array.isArray(node)) return node.flatMap((child) => elements(child ?? null));
  return [node, ...elements(node.props?.children ?? null)];
}
function text(node = harness.tree) {
  if (Array.isArray(node)) return node.map((child) => text(child ?? null)).join('');
  if (node && typeof node === 'object') return text(node.props?.children ?? null);
  return node == null ? '' : String(node);
}
function button(label) { const found = elements().find((node) => node.type === 'button' && text(node) === label); assert.ok(found, label); return found; }
function cards() { return elements().filter((node) => node.type === 'task-card'); }
async function scenario(run) {
  const originalStorage = globalThis.localStorage;
  const originalTimeout = globalThis.setTimeout;
  const originalClear = globalThis.clearTimeout;
  const values = new Map([['tomato-agent-session-id', 's1']]);
  const timers = new Map(); let timerId = 0;
  globalThis.localStorage = { getItem: (key) => values.get(key), setItem: (key, value) => values.set(key, value) };
  globalThis.setTimeout = (callback) => { timers.set(++timerId, callback); return timerId; };
  globalThis.clearTimeout = (id) => timers.delete(id);
  calls.length = 0;
  historyReader = async (id) => ({ tasks: [task(id)] });
  eventReader = async (id, after) => ({ events: after ? [] : [{ sequence: 1, event_type: id, payload: {} }], next_cursor: 1 });
  submitReader = async () => task('created', 'queued');
  actionReader = async () => task('cancelled', 'cancelled');
  taskReader = async (id) => task(id, 'running');
  try { await run({ values, timers }); }
  finally { unmount(); globalThis.localStorage = originalStorage; globalThis.setTimeout = originalTimeout; globalThis.clearTimeout = originalClear; }
}

test('刷新恢复服务端事实、切模式保留历史、终态停止轮询、存储只写 ID', async () => scenario(async ({ values, timers }) => {
  mount(); await flush();
  assert.equal(cards()[0].props.task.result.answer, 'answer-s1');
  assert.equal(timers.size, 0);
  button('s1').props.onClick(); await flush();
  assert.doesNotMatch(text(), /恢复中/);
  elements().find((node) => node.type === 'select').props.onChange({ target: { value: 'writing' } }); await flush();
  assert.equal(cards().length, 1);
  unmount(); mount(); await flush();
  assert.equal(cards()[0].props.task.run_id, 's1');
  button('s2').props.onClick(); await flush();
  assert.equal(cards()[0].props.task.run_id, 's2');
  assert.deepEqual([...values.keys()], ['tomato-agent-session-id']);
  assert.equal(values.get('tomato-agent-session-id'), 's2');
}));

test('默认助手提交可选工作区且不带检索配置，禁用时保留输入并提示配置', async () => scenario(async () => {
  historyReader = async () => ({ tasks: [] });
  submitReader = async () => { const error = new Error('assistant_disabled'); error.code = 'assistant_disabled'; throw error; };
  mount(); await flush();
  assert.equal(elements().find((node) => node.type === 'select').props.value, 'assistant');
  elements().find((node) => node.type === 'textarea').props.onChange({ target: { value: '解释缓存' } }); await flush();
  elements().find((node) => node.type === 'form').props.onSubmit({ preventDefault() {} }); await flush();
  const body = calls.find((call) => call[0] === 'submit')[2];
  assert.equal(body.kind, 'assistant'); assert.equal(body.workspace_id, 'ws'); assert.deepEqual(body.options, {});
  assert.match(text(), /助手.*未启用/);
  assert.equal(elements().find((node) => node.type === 'textarea').props.value, '解释缓存');
  elements().find((node) => node.type === 'workspace-picker').props.onSelect(''); await flush();
  elements().find((node) => node.type === 'form').props.onSubmit({ preventDefault() {} }); await flush();
  assert.equal(calls.filter((call) => call[0] === 'submit').at(-1)[2].workspace_id, undefined);
}));

test('助手绑定是版本动作，过期动作刷新事实并保留错误，网络重试复用动作ID', async () => scenario(async () => {
  let currentTask = { ...task('assistant', 'waiting', 'assistant'), capabilities: ['bind_workspace', 'reject_action'],
    pending_action: { action_id: 'bind:3', action_type: 'workspace_binding', allowed_actions: ['bind_workspace', 'reject_action'] } };
  historyReader = async () => ({ tasks: [currentTask] }); taskReader = async () => currentTask;
  let errorCode = 'network';
  actionReader = async () => { const error = new Error(errorCode); error.code = errorCode; if (errorCode === 'stale_action') currentTask = { ...currentTask, version: 4 }; throw error; };
  mount(); await flush();
  const original = cards()[0].props.task;
  await cards()[0].props.onAction(original, 'bind_workspace', { workspace_id: 'ws' }); await flush();
  await cards()[0].props.onAction(original, 'bind_workspace', { workspace_id: 'ws' }); await flush();
  const attempts = calls.filter((call) => call[0] === 'action');
  assert.equal(attempts[0][2].client_request_id, attempts[1][2].client_request_id);
  assert.deepEqual(attempts[0][2].payload, { workspace_id: 'ws' }); assert.equal(attempts[0][2].expected_version, 3);
  errorCode = 'stale_action';
  await cards()[0].props.onAction(original, 'reject_action', {}); await flush();
  assert.equal(cards()[0].props.task.version, 4); assert.match(text(), /stale_action/);
  assert.equal(calls.some((call) => call[0] === 'bind'), false);
}));

test('切换清理计时器与请求，旧会话迟到历史不能覆盖新会话', async () => scenario(async ({ timers }) => {
  let release;
  historyReader = (id) => id === 's1' ? new Promise((resolve) => { release = resolve; }) : Promise.resolve({ tasks: [task('new')] });
  mount(); await flush();
  const oldSignal = calls.find((call) => call[0] === 'history')[3];
  button('s2').props.onClick(); await flush();
  assert.equal(oldSignal.aborted, true);
  release({ tasks: [task('old')] }); await flush();
  assert.deepEqual(cards().map((card) => card.props.task.run_id), ['new']);
  assert.equal(timers.size, 0);
}));

test('活动任务可编辑并排队、增量游标排空、失败动作不被成功轮询清除', async () => scenario(async ({ timers }) => {
  let state = 'running';
  historyReader = async () => ({ tasks: [task('active', state)] });
  actionReader = async () => { throw new Error('version_conflict'); };
  mount(); await flush();
  const input = elements().find((node) => node.type === 'textarea');
  assert.equal(input.props.disabled, false);
  input.props.onChange({ target: { value: 'next task' } }); await flush();
  elements().find((node) => node.type === 'form').props.onSubmit({ preventDefault() {} }); await flush();
  assert.equal(calls.find((call) => call[0] === 'enqueue')[2].message, 'next task');
  assert.equal(timers.size, 1);
  await cards()[0].props.onAction(cards()[0].props.task, 'cancel', {}); await flush();
  assert.match(text(), /version_conflict/);
  state = 'completed';
  const [id, tick] = [...timers][0]; timers.delete(id); tick(); await flush();
  assert.match(text(), /version_conflict/);
  assert.equal(timers.size, 0);
  assert.ok(calls.some((call) => call[0] === 'events' && call[2] === 1));
}));

test('分页读取所有历史及终态事件，终态仍排空满页事件', async () => scenario(async ({ timers }) => {
  historyReader = async (id, offset) => ({ tasks: offset === 0 ? Array.from({ length: 200 }, (_, i) => task(`r${i}`)) : [task('last', 'completed', 'writing')] });
  eventReader = async (id, after) => ({ events: id === 'r0' && after === 0 ? Array.from({ length: 200 }, (_, i) => ({ sequence: i + 1, event_type: 'event', payload: {} })) : [], next_cursor: id === 'r0' ? 200 : after });
  mount(); await flush();
  assert.equal(cards().length, 201);
  assert.ok(calls.some((call) => call[0] === 'history' && call[2] === 200));
  assert.equal(timers.size, 1);
  const [id, tick] = [...timers][0]; timers.delete(id); tick(); await flush();
  assert.ok(calls.some((call) => call[0] === 'events' && call[1] === 'r0' && call[2] === 200));
  assert.equal(timers.size, 0);
}));

test('提交网络失败重试复用请求 ID，代码显式绑定，只有知识模式带检索 options', async () => scenario(async () => {
  historyReader = async () => ({ tasks: [] });
  let fail = true;
  submitReader = async () => { if (fail) throw new Error('network'); return task('created', 'queued'); };
  mount(); await flush();
  elements().find((node) => node.type === 'select').props.onChange({ target: { value: 'code' } }); await flush();
  elements().find((node) => node.type === 'textarea').props.onChange({ target: { value: 'fix' } }); await flush();
  elements().find((node) => node.type === 'form').props.onSubmit({ preventDefault() {} }); await flush();
  assert.match(text(), /network/);
  fail = false;
  elements().find((node) => node.type === 'form').props.onSubmit({ preventDefault() {} }); await flush();
  const submissions = calls.filter((call) => call[0] === 'submit');
  assert.equal(submissions.length, 2);
  assert.equal(submissions[0][2].client_request_id, submissions[1][2].client_request_id);
  assert.equal(submissions[0][2].workspace_id, 'ws');
  assert.equal(submissions[0][2].options, undefined);
}));

test('写作可编辑提纲并显式保存，恢复默认未确认，版本动作由服务端能力限定', async () => scenario(async () => {
  const actions = [];
  let currentTask = { ...task('writing', 'waiting', 'writing'), result: { outline: { title: 'old' }, draft: 'draft', citations: [] },
    capabilities: ['confirm_outline', 'save_draft'], pending_action: { action_id: 'outline:1', reason: 'confirm_outline' } };
  mount(() => TaskCard({ task: currentTask, busy: false, onAction: async (...args) => actions.push(args), onOpenDocument() {} })); await flush();
  elements().find((node) => node.type === 'textarea').props.onChange({ target: { value: '{"title":"edited"}' } }); await flush();
  button('确认提纲并生成草稿').props.onClick(); await flush();
  assert.deepEqual(actions[0][2], { outline: { title: 'edited' } });
  button('确认保存草稿').props.onClick(); await flush();
  assert.equal(actions[1][1], 'save_draft');
  currentTask = { ...task('code', 'waiting', 'code'), result: {}, capabilities: ['inspect_recovery', 'continue_recovery'],
    pending_action: { action_id: 'recovery:1', reason: 'recovery_required' } };
  harness.dirty = true; await flush();
  assert.equal(button('确认请求恢复').props.disabled, true);
  assert.match(text(), /当前恢复事实/);
  elements().find((node) => node.type === 'input').props.onChange({ target: { checked: true } }); await flush();
  button('确认请求恢复').props.onClick(); await flush();
  assert.deepEqual(actions[2][2], { confirmed: true });
  currentTask = { ...currentTask, pending_action: { action_id: 'recovery:2', reason: 'recovery_required' } };
  harness.dirty = true; await flush();
  assert.equal(button('确认请求恢复').props.disabled, true);
}));

test('材料不足、来源、legacy、无动作与未知测试事实明确展示', async () => scenario(async () => {
  const currentTask = { ...task('knowledge', 'waiting', 'knowledge'), legacy: true,
    result: { answer: 'insufficient', evidence_status: 'no_results', citations: [] }, capabilities: ['cancel'] };
  mount(() => TaskCard({ task: currentTask, busy: false, onAction: async () => {}, onOpenDocument() {} })); await flush();
  assert.match(text(), /材料不足|无来源/); assert.match(text(), /legacy/);
  assert.equal(elements().some((node) => node.type === 'button' && text(node) === '取消任务'), false);
}));

test('冲突处置可接受 admission 风险、保留隔离事实并展示迟到冲突重新阻断', async () => scenario(async () => {
  let settlements = [{ invocation_id: 'inv-1', resolution: 'executed', quarantined: true,
    termination_evidence: null, conflict: true, risk_accepted: false, admission_blocked: true }];
  let releaseAcceptance;
  cardSettlementReader = async () => ({ settlements });
  cardRiskAcceptor = async () => new Promise((resolve) => {
    releaseAcceptance = () => {
      settlements = [{ ...settlements[0], risk_accepted: true, admission_blocked: false }];
      resolve({ settlements });
    };
  });
  const currentTask = { ...task('assistant-conflict', 'waiting', 'assistant') };
  mount(() => TaskCard({ task: currentTask, busy: false, onAction: async () => {}, onOpenDocument() {} }));
  await flush();
  assert.match(text(), /检测到迟到结果与核查报告冲突/);
  assert.match(text(), /不会证明操作已终止，也不会解除隔离/);
  assert.equal(button('我接受冲突风险并允许后续任务继续').props.disabled, false);

  button('我接受冲突风险并允许后续任务继续').props.onClick();
  await flush();
  assert.equal(button('正在记录…').props.disabled, true);
  releaseAcceptance();
  await flush();
  assert.match(text(), /已接受 admission 风险；冲突和隔离状态仍保留/);
  assert.doesNotMatch(text(), /正在记录…/);

  settlements = [{ ...settlements[0], admission_blocked: true }];
  currentTask.version += 1;
  harness.dirty = true;
  await flush();
  assert.equal(button('我接受冲突风险并允许后续任务继续').props.disabled, false);
  unmount();
}));

test('冲突风险接受失败时显示服务端错误并允许重试', async () => scenario(async () => {
  cardSettlementReader = async () => ({ settlements: [{ invocation_id: 'inv-2',
    resolution: 'not_executed', quarantined: true, termination_evidence: null, conflict: true,
    risk_accepted: false, admission_blocked: true }] });
  cardRiskAcceptor = async () => { throw new Error('action_request_conflict'); };
  mount(() => TaskCard({ task: { ...task('assistant-conflict', 'waiting', 'assistant') }, busy: false,
    onAction: async () => {}, onOpenDocument() {} }));
  await flush();
  button('我接受冲突风险并允许后续任务继续').props.onClick();
  await flush();
  assert.match(text(), /action_request_conflict/);
  assert.equal(button('我接受冲突风险并允许后续任务继续').props.disabled, false);
}));

test('原目录注册保留目标，选择 unavailable 禁用，不自动执行命令', async () => scenario(async () => {
  const registrations = [];
  mount(() => WorkspacePicker({ workspaces: [{ workspace_id: 'bad', available: false }], value: '', disabled: false,
    onSelect() {}, onRegister: async (...args) => registrations.push(args) })); await flush();
  const inputs = elements().filter((node) => node.type === 'input');
  inputs[0].props.onChange({ target: { value: ' /project ' } });
  inputs[1].props.onChange({ target: { value: 'tests, test_unit.py' } }); await flush();
  assert.equal(elements().find((node) => node.type === 'option' && node.props.value === 'bad').props.disabled, true);
  button('注册并绑定目录').props.onClick(); await flush();
  assert.deepEqual(registrations, [['/project', ['tests', 'test_unit.py']]]);
}));

test('无测试报告与未知退出码不显示通过', async () => scenario(async () => {
  mount(() => CodeTestResults({ results: [] })); await flush();
  assert.match(text(), /测试未运行/); assert.doesNotMatch(text(), /通过/);
  unmount();
  mount(() => CodeTestResults({ results: [{ exit_code: null }] })); await flush();
  assert.match(text(), /未通过.*退出码 未知/);
}));

test('统一动作携带 pending ID 与 Run 版本，成功后刷新取消终态', async () => scenario(async () => {
  let currentTask = { ...task('active', 'waiting', 'writing'), capabilities: ['save_draft'],
    pending_action: { action_id: 'save:7', reason: 'save_draft' } };
  historyReader = async () => ({ tasks: [currentTask] });
  taskReader = async () => currentTask;
  actionReader = async () => { currentTask = task('active', 'completed', 'writing'); return currentTask; };
  mount(); await flush();
  await cards()[0].props.onAction(cards()[0].props.task, 'save_draft', {}); await flush();
  const body = calls.find((call) => call[0] === 'action')[2];
  assert.equal(body.action_id, 'save:7'); assert.equal(body.expected_version, 3);
  assert.equal(body.action, 'save_draft'); assert.deepEqual(body.payload, {});
  assert.equal(cards()[0].props.task.status, 'completed');
}));

test('调整当前任务使用独立入口并展示接收状态，不消费等待动作', async () => scenario(async () => {
  const currentTask = { ...task('assistant-run', 'waiting', 'assistant'),
    pending_action: { action_id: 'input:1', reason: 'clarification' }, capabilities: ['provide_input'] };
  historyReader = async () => ({ tasks: [currentTask] });
  taskReader = async () => currentTask;
  mount(); await flush();
  elements().find((node) => node.type === 'textarea').props.onChange({ target: { value: 'change direction' } });
  await flush();
  button('调整当前任务').props.onClick(); await flush();
  const sent = calls.find((call) => call[0] === 'steer');
  assert.equal(sent[1], 'assistant-run'); assert.equal(sent[2].message, 'change direction');
  assert.equal(calls.some((call) => call[0] === 'action'), false);
  assert.match(text(), /调整请求已接收/);
  assert.equal(cards()[0].props.task.pending_action.action_id, 'input:1');
}));

test('卸载取消正在读取的任务与事件，不发布迟到结果', async () => scenario(async () => {
  let release;
  eventReader = () => new Promise((resolve) => { release = resolve; });
  mount(); await flush();
  const signal = calls.find((call) => call[0] === 'events')[3];
  unmount(); harness.dirty = false;
  assert.equal(signal.aborted, true);
  release({ events: [], next_cursor: 0 });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(harness.dirty, false);
}));

test('恢复观察在动作后刷新和同版本轮询中保留，版本变化时清除', async () => scenario(async ({ timers }) => {
  let currentTask = { ...task('recover', 'waiting', 'code'), capabilities: ['inspect_recovery', 'continue_recovery'],
    pending_action: { action_id: 'recover:3', reason: 'recovery_required' } };
  historyReader = async () => ({ tasks: [currentTask] });
  taskReader = async () => currentTask;
  const recovery = { version: 3, reason: 'unknown_side_effect', action: 'manual_review',
    allowed_actions: ['inspect_recovery', 'continue_recovery'] };
  actionReader = async () => ({ ...currentTask, recovery });
  mount(); await flush();
  await cards()[0].props.onAction(cards()[0].props.task, 'inspect_recovery', {}); await flush();
  assert.deepEqual(cards()[0].props.task.recovery, recovery);
  const tick = [...timers.values()].at(-1); timers.clear(); tick(); await flush();
  assert.deepEqual(cards()[0].props.task.recovery, recovery);
  currentTask = { ...currentTask, version: 4 };
  const next = [...timers.values()].at(-1); timers.clear(); next(); await flush();
  assert.equal(cards()[0].props.task.recovery, undefined);
}));