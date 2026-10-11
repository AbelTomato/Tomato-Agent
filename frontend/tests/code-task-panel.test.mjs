import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { transformWithOxc } from 'vite';

// 无 DOM 的组件 Hook 驱动器：执行真实组件的挂载、重渲染及卸载逻辑。
let harness;
const source = readFileSync(new URL('../src/components/CodeTaskPanel.tsx', import.meta.url), 'utf8')
  .replace("import { CodeTestResults } from './TaskCard';", 'const CodeTestResults = "test-results";')
  .replace(/import \{ useEffect, useRef, useState \} from 'react';/, 'const { useEffect, useRef, useState } = globalThis.__codePanelHooks;')
  .replace(/import \{.*?\} from 'lucide-react';/s, 'const Code2 = "icon", RefreshCw = "icon", Square = "icon", Play = "icon", FileText = "icon";')
  .replace(/import \{.*?\} from '..\/api';/s, 'const { createCodeTask, getCodeTask, listCodeTaskEvents, executeCodeTask, cancelCodeTask, recoverCodeTask, loadCodeTaskArtifact } = globalThis.__codePanelApi;');
globalThis.__codePanelHooks = {
  useState(initial) {
    const index = harness.index++;
    if (!(index in harness.slots)) harness.slots[index] = typeof initial === 'function' ? initial() : initial;
    return [harness.slots[index], (value) => {
      harness.slots[index] = typeof value === 'function' ? value(harness.slots[index]) : value;
      harness.dirty = true;
    }];
  },
  useRef(initial) {
    const index = harness.index++;
    return harness.slots[index] ??= { current: initial };
  },
  useEffect(effect, deps) {
    const index = harness.index++;
    const previous = harness.slots[index];
    if (!previous || deps.some((dep, i) => !Object.is(dep, previous.deps[i]))) {
      harness.effects.push(() => {
        previous?.cleanup?.();
        harness.slots[index] = { deps, cleanup: effect() };
      });
    }
  },
};
const calls = [];
let fail = false;
let eventCursor = 1;
let eventReader = null;
const task = (id) => ({ run_id: id, status: 'completed', workspace_id: `ws-${id}`, version: 2,
  event_cursor: eventCursor, snapshot_summary: { sequence: 3, checkpoint_revision: 3,
    phase: 'ready_finish', logical_index: 7, created_at: '2026-10-11T10:00:00Z' },
  artifacts: [{ artifact_id: `diff-${id}`, kind: 'diff', size_bytes: 4 }], test_results: [], changed_files: ['calculator.py'] });
globalThis.__codePanelApi = {
  async getCodeTask(id) { calls.push(['state', id]); if (fail || id === 'missing') throw new Error('Run 不存在'); return task(id); },
  async listCodeTaskEvents(id, after) { calls.push(['events', id, after]); return eventReader ? eventReader(id, after) : { events: [{ sequence: 1, event_type: id, payload: {} }] }; },
  async loadCodeTaskArtifact(id, artifactId) { calls.push(['artifact', id, artifactId]); return { artifact: { kind: 'diff' }, content: `content-${id}` }; },
  async createCodeTask() { return task('created'); },
};
const compiled = (await transformWithOxc(source, 'CodeTaskPanel.tsx', { jsx: { runtime: 'automatic' } })).code
  .replace('"react/jsx-runtime"', JSON.stringify(import.meta.resolve('react/jsx-runtime')));
const { CodeTaskPanel } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);

function mount() { harness = { slots: [], effects: [], index: 0, dirty: true, tree: null }; }
async function flush() {
  for (let i = 0; i < 20; i++) {
    if (harness.dirty) {
      harness.dirty = false; harness.index = 0;
      harness.tree = CodeTaskPanel();
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
  return [node, ...[node.props?.children].flat(Infinity).filter((child) => child != null).flatMap((child) => elements(child))];
}
function text(node) {
  if (node == null || typeof node === 'boolean') return '';
  if (typeof node !== 'object') return String(node);
  return [node.props?.children].flat(Infinity).map(text).join('');
}

test('刷新恢复状态和完整事件，读取产物，切换 ID 清空旧产物，错误可重试', async () => {
  const originalStorage = globalThis.localStorage;
  const values = new Map([['tomato-agent.code-task.run-id', 'saved'], ['chat', 'untouched']]);
  globalThis.localStorage = { getItem: (key) => values.get(key), setItem: (key, value) => values.set(key, value) };
  try {
    mount(); await flush();
    assert.match(text(harness.tree), /Run：saved/);
    assert.match(text(harness.tree), /执行阶段：准备完成 · 逻辑步骤：7/);
    assert.match(text(harness.tree), /检查点：#3 · 修订：3/);
    assert.match(text(harness.tree), /事件游标：1 · 已读取：1/);
    assert.ok(calls.some((call) => call.join() === 'events,saved,0'));
    const artifactButton = elements().find((node) => node.type === 'button' && text(node).includes('查看 diff'));
    artifactButton.props.onClick(); await flush();
    assert.match(text(harness.tree), /content-saved/);
    const idInput = elements().find((node) => node.type === 'input' && node.props.placeholder);
    idInput.props.onChange({ target: { value: ' next ' } }); await flush();
    elements().find((node) => node.type === 'form').props.onSubmit({ preventDefault() {} }); await flush();
    assert.match(text(harness.tree), /Run：next/);
    assert.doesNotMatch(text(harness.tree), /content-saved|#1 saved/);
    assert.ok(calls.some((call) => call.join() === 'events,next,0'));
    assert.equal(values.get('tomato-agent.code-task.run-id'), 'next');
    assert.equal(values.get('chat'), 'untouched');
    unmount(); mount(); await flush();
    assert.match(text(harness.tree), /Run：next/);
    unmount(); fail = true; mount(); await flush();
    assert.match(text(harness.tree), /Run 不存在/);
    fail = false;
    elements().find((node) => node.type === 'form').props.onSubmit({ preventDefault() {} }); await flush();
    assert.match(text(harness.tree), /Run：next/);
  } finally { unmount(); fail = false; globalThis.localStorage = originalStorage; }
});

test('存储不可用仍可创建并查看任务', async () => {
  const originalStorage = globalThis.localStorage;
  globalThis.localStorage = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } };
  try {
    mount(); await flush();
    elements().find((node) => node.type === 'textarea').props.onChange({ target: { value: 'fix' } }); await flush();
    elements().find((node) => node.type === 'button' && text(node) === '提交代码任务').props.onClick(); await flush();
    assert.match(text(harness.tree), /Run：created/);
    assert.ok(calls.some((call) => call.join() === 'events,created,0'));
  } finally { unmount(); globalThis.localStorage = originalStorage; }
});

test('已有任务轮询成功不会清除打开其他任务失败的操作错误', async () => {
  const originalStorage = globalThis.localStorage;
  globalThis.localStorage = { getItem: () => 'saved', setItem() {} };
  try {
    mount(); await flush();
    const idInput = elements().find((node) => node.type === 'input' && node.props.placeholder);
    idInput.props.onChange({ target: { value: 'missing' } }); await flush();
    elements().find((node) => node.type === 'form').props.onSubmit({ preventDefault() {} }); await flush();
    assert.match(text(harness.tree), /Run 不存在/);
    assert.match(text(harness.tree), /Run：saved/);
  } finally { unmount(); globalThis.localStorage = originalStorage; }
});

test('终态游标领先时继续增量读取，游标只推进到已收到的事件', async () => {
  const originalStorage = globalThis.localStorage;
  const originalTimeout = globalThis.setTimeout;
  const timers = [];
  const afterValues = [];
  globalThis.localStorage = { getItem: () => 'saved', setItem() {} };
  globalThis.setTimeout = (callback) => { timers.push(callback); return 0; };
  eventCursor = 2;
  eventReader = async (id, after) => {
    afterValues.push(after);
    return { events: [{ sequence: after + 1, event_type: id, payload: {} }] };
  };
  try {
    mount(); await flush();
    assert.deepEqual(afterValues, [0]);
    assert.match(text(harness.tree), /事件游标：2 · 已读取：1/);
    assert.equal(timers.length, 1);
    timers.shift()(); await flush();
    assert.deepEqual(afterValues, [0, 1]);
    assert.match(text(harness.tree), /事件游标：2 · 已读取：2/);
    assert.equal(timers.length, 0);
  } finally {
    unmount();
    globalThis.localStorage = originalStorage;
    globalThis.setTimeout = originalTimeout;
    eventCursor = 1;
    eventReader = null;
  }
});