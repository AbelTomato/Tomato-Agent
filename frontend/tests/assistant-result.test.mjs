import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { transformWithOxc } from 'vite';
import { renderToStaticMarkup } from 'react-dom/server';

let slots = [], index = 0;
globalThis.__assistantHooks = { useState(initial) {
  const key = index++;
  if (!(key in slots)) slots[key] = initial;
  return [slots[key], (value) => { slots[key] = value; }];
} };
async function compile(path, replacements = []) {
  let source = readFileSync(new URL(path, import.meta.url), 'utf8');
  for (const [pattern, replacement] of replacements) source = source.replace(pattern, replacement);
  const { code } = await transformWithOxc(source, path, { jsx: { runtime: 'automatic' } });
  return import(`data:text/javascript;base64,${Buffer.from(code.replace('"react/jsx-runtime"', JSON.stringify(import.meta.resolve('react/jsx-runtime')))).toString('base64')}`);
}
const apiSource = readFileSync(new URL('../src/api.ts', import.meta.url), 'utf8').replace('import.meta.env.VITE_API_BASE_URL', 'undefined');
const { code: apiCode } = await transformWithOxc(apiSource, 'api.ts');
globalThis.__assistantApi = await import(`data:text/javascript;base64,${Buffer.from(apiCode).toString('base64')}`);
const { CitationList } = await compile('../src/components/CitationList.tsx', [
  [/import \{.*?\} from '..\/api';/s, 'const { isSafeExternalUrl } = globalThis.__assistantApi;'],
]);
globalThis.__assistantCitations = CitationList;
const { AssistantResult } = await compile('../src/components/AssistantResult.tsx', [
  [/import \{ CitationList \} from '.\/CitationList';/, 'const CitationList = globalThis.__assistantCitations;'],
]);
const { PendingActionPanel } = await compile('../src/components/PendingActionPanel.tsx', [
  [/import \{ useState \} from 'react';/, 'const { useState } = globalThis.__assistantHooks;'],
]);
function elements(node) {
  if (!node || typeof node !== 'object') return [];
  if (Array.isArray(node)) return node.flatMap(elements);
  return [node, ...elements(node.props?.children)];
}
function text(node) {
  if (Array.isArray(node)) return node.map(text).join('');
  return node && typeof node === 'object' ? text(node.props?.children) : node == null ? '' : String(node);
}
function panel(props) { index = 0; return PendingActionPanel(props); }
const base = { run_id: 'r1', version: 3, kind: 'assistant', status: 'waiting', workspace_id: null, legacy: false,
  capabilities: ['bind_workspace', 'reject_action'], pending_action: { action_id: 'a1', action_type: 'workspace_binding',
    allowed_actions: ['bind_workspace', 'reject_action'], display_data: {} } };

test('助手组合结果展示引用、提纲、草稿、测试与产物，不把只读完成显示为测试通过', () => {
  const html = renderToStaticMarkup(AssistantResult({ result: { answer: '<script>unsafe</script>', validation_status: 'not_required',
    citations: [{ citation_id: 'c1', title: 'Cache guide', source_path: 'cache.md', start_line: 1, end_line: 4, text: '缓存原文' }],
    outline: { title: '提纲' }, draft: '草稿正文', test_results: [{ passed: false, exit_code: null }],
    artifacts: [{ artifact_id: 'art1', kind: 'diff', size_bytes: 20 }] }, onOpenDocument() {} }));
  assert.match(html, /Cache guide/); assert.match(html, /缓存原文/); assert.match(html, /查看提纲/);
  assert.match(html, /草稿正文/); assert.match(html, /未通过.*未知/); assert.match(html, /本轮 diff/);
  assert.match(html, /无需执行验证/); assert.doesNotMatch(html, /验证通过/); assert.doesNotMatch(html, /<script>/);
});

test('验证失败、不可用和缺少事实有独立展示', () => {
  for (const [status, expected] of [['failed', '验证失败'], ['unavailable', '验证不可用'], [undefined, '尚无验证事实']]) {
    const html = renderToStaticMarkup(AssistantResult({ result: { validation_status: status }, onOpenDocument() {} }));
    assert.ok(html.includes(expected)); assert.doesNotMatch(html, />验证状态：验证通过/);
  }
});

test('未知副作用核查提供独立分支，风险继续必须显式确认且不声称终止', () => {
  const calls = [];
  const task = { ...base, capabilities: ['resolve_side_effect'], pending_action: {
    ...base.pending_action, action_type: 'recovery', allowed_actions: ['resolve_side_effect'],
    target_capability: 'write_file', display_data: {},
  } };
  const html = renderToStaticMarkup(PendingActionPanel({ task, busy: false, workspaces: [],
    onAction: (...args) => calls.push(args) }));
  assert.match(html, /确认已执行/);
  assert.match(html, /确认未执行/);
  assert.match(html, /仍无法确认/);
  assert.match(html, /不代表旧操作已终止/);
  assert.match(html, /我接受旧操作仍可能产生影响/);
  assert.match(html, /确认已执行并继续/);
  assert.match(html, /disabled=""/);
});

test('工作区绑定和拒绝只发送服务端允许的版本动作，未选择和不可用项目禁止绑定', async () => {
  slots = [];
  const calls = [];
  const props = { task: base, busy: false, workspaces: [{ workspace_id: 'ws', available: true }, { workspace_id: 'bad', available: false }],
    onAction: async (...args) => calls.push(args) };
  let tree = panel(props);
  const find = (label) => elements(tree).find((node) => node.type === 'button' && text(node) === label);
  assert.equal(find('绑定并继续当前任务').props.disabled, true);
  assert.equal(elements(tree).find((node) => node.type === 'option' && node.props.value === 'bad').props.disabled, true);
  elements(tree).find((node) => node.type === 'select').props.onChange({ target: { value: 'bad' } }); tree = panel(props);
  assert.equal(find('绑定并继续当前任务').props.disabled, true);
  elements(tree).find((node) => node.type === 'select').props.onChange({ target: { value: 'ws' } }); tree = panel(props);
  await find('绑定并继续当前任务').props.onClick(); await find('拒绝此请求').props.onClick();
  assert.deepEqual(calls, [[base, 'bind_workspace', { workspace_id: 'ws' }], [base, 'reject_action', {}]]);
  tree = panel({ ...props, task: { ...base, workspace_id: 'ws' } });
  assert.equal(find('绑定并继续当前任务').props.disabled, true);
  tree = panel({ ...props, task: { ...base, capabilities: ['cancel'] } });
  assert.equal(find('绑定并继续当前任务'), undefined); assert.equal(find('拒绝此请求'), undefined);
});

test('澄清拒绝空白输入，提交严格message且终态或legacy无动作', async () => {
  slots = [];
  const calls = [];
  const task = { ...base, capabilities: ['provide_input'], pending_action: { ...base.pending_action, action_type: 'clarification',
    allowed_actions: ['provide_input'], display_data: { question: '需要哪个项目？' } } };
  const props = { task, busy: false, workspaces: [], onAction: async (...args) => calls.push(args) };
  let tree = panel(props);
  assert.match(text(tree), /需要哪个项目/);
  const button = () => elements(tree).find((node) => node.type === 'button');
  assert.equal(button().props.disabled, true);
  elements(tree).find((node) => node.type === 'textarea').props.onChange({ target: { value: '  当前缓存项目  ' } }); tree = panel(props);
  await button().props.onClick(); assert.deepEqual(calls, [[task, 'provide_input', { message: '当前缓存项目' }]]);
  assert.equal(panel({ ...props, task: { ...task, status: 'completed' } }), null);
  assert.equal(panel({ ...props, task: { ...task, legacy: true } }), null);
});