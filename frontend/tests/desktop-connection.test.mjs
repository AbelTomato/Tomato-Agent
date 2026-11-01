import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { transformWithOxc } from 'vite';

let slots = [], index = 0, effects = [];
globalThis.__connectionHooks = {
  useState(initial) {
    const slot = index++;
    if (!(slot in slots)) slots[slot] = initial;
    return [slots[slot], value => { slots[slot] = typeof value === 'function' ? value(slots[slot]) : value; }];
  },
  useEffect(effect) { effects.push(effect); },
};
const source = readFileSync(new URL('../src/components/DesktopConnection.tsx', import.meta.url), 'utf8')
  .replace("import { useEffect, useState } from 'react';", 'const { useEffect, useState } = globalThis.__connectionHooks;')
  .replace("import { ConversationShell } from './ConversationShell';", "const ConversationShell = () => null;");
const { code } = await transformWithOxc(source, 'DesktopConnection.tsx', { jsx: { runtime: 'automatic' } });
const { DesktopConnection } = await import(`data:text/javascript;base64,${Buffer.from(
  code.replace('"react/jsx-runtime"', JSON.stringify(import.meta.resolve('react/jsx-runtime'))),
).toString('base64')}`);
function render() { index = 0; effects = []; return DesktopConnection(); }
function nodes(tree, type) {
  if (!tree || typeof tree !== 'object') return [];
  return [...(tree.type === type ? [tree] : []),
    ...[tree.props?.children].flat(Infinity).flatMap(child => nodes(child, type))];
}
const button = (tree, label) => nodes(tree, 'button').find(node => node.props.children === label);

test('desktop launch is idle; preferences save without connecting and workspace entry connects on demand', async () => {
  slots = [];
  const calls = [];
  globalThis.window = {
    setInterval: () => 1, clearInterval() {},
    __TAURI__: {
      event: { async listen() { return () => {}; } },
      core: { async invoke(command, args) {
        calls.push([command, args]);
        if (command === 'environment_preferences') return { distribution: 'Ubuntu', backend_path: '/backend' };
        if (command === 'pick_windows_directory') return null;
        if (command === 'bridge_windows_directory') return '/mnt/c/Users/Test/Project';
        if (command === 'save_environment_preferences') return { distribution: args.distribution, backend_path: args.backendPath };
        if (command === 'distributions') return ['Ubuntu'];
        if (command === 'connect_service') return { state: 'ready', summary: '已连接 Ubuntu', detail: '' };
        if (command === 'desktop_request') return { status: 200, body: [] };
      } },
    },
  };
  let tree = render();
  effects.forEach(effect => effect());
  await Promise.resolve(); await Promise.resolve();
  tree = render();
  assert.ok(button(tree, '打开工作区'));
  assert.equal(calls.some(([name]) => ['distributions', 'connect_service', 'desktop_request'].includes(name)), false);
  button(tree, '设置').props.onClick();
  tree = render();
  assert.ok(button(tree, '刷新 WSL 环境'));
  assert.equal(calls.some(([name]) => name === 'distributions'), false);
  await button(tree, '刷新 WSL 环境').props.onClick();
  tree = render();
  assert.equal(calls.filter(([name]) => name === 'connect_service').length, 0);
  await button(tree, '选择并检查 Windows 文件夹').props.onClick();
  assert.equal(calls.some(([name]) => name === 'pick_windows_directory'), true);
  assert.equal(calls.some(([name]) => name === 'bridge_windows_directory'), false);
  tree = render();
  await button(tree, '保存环境设置').props.onClick();
  assert.equal(calls.filter(([name]) => name === 'save_environment_preferences').length, 1);
  assert.equal(calls.filter(([name]) => name === 'connect_service').length, 0);
  tree = render();
  button(tree, '打开工作区').props.onClick();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.filter(([name]) => name === 'connect_service').length, 1);
  assert.equal(calls.filter(([name]) => name === 'desktop_request').length, 0);
  tree = render();
  const walk = value => Array.isArray(value) ? value.flatMap(walk) : value?.type ? [value, ...walk(value.props?.children)] : [];
  assert.equal(walk(tree).some(node => node.type.name === 'ConversationShell'), true);
  assert.equal(button(tree, '关闭工作区') !== undefined, true);
  delete globalThis.window;
});

test('explicit connect failure does not retry and active-task close confirmation remains', async () => {
  slots = [];
  const calls = [];
  let closeRequested;
  let fail = true;
  globalThis.window = {
    setInterval: () => 1, clearInterval() {},
    __TAURI__: {
      event: { async listen(_name, callback) { closeRequested = callback; return () => {}; } },
      core: { async invoke(command, args) {
        calls.push([command, args]);
        if (command === 'environment_preferences') return { distribution: 'Ubuntu', backend_path: '/backend' };
        if (command === 'distributions') return ['Ubuntu'];
        if (command === 'connect_service') {
          if (fail) throw new Error('启动超时');
          return { state: 'ready', summary: '已连接 Ubuntu', detail: '' };
        }
        if (command === 'connection_facts') return { active_tasks: 2 };
      } },
    },
  };
  let tree = render();
  effects[0]();
  await Promise.resolve(); await Promise.resolve();
  tree = render();
  const cleanup = effects[0]();
  await button(tree, '打开工作区').props.onClick();
  tree = render();
  assert.match(nodes(tree, 'summary').map(node => node.props.children).join(' '), /无法连接执行环境/);
  assert.equal(calls.filter(([name]) => name === 'connect_service').length, 1);
  fail = false;
  await button(tree, '重试连接').props.onClick();
  tree = render();
  assert.ok(button(tree, '关闭工作区'));
  closeRequested();
  await Promise.resolve(); await Promise.resolve();
  tree = render();
  assert.match(nodes(tree, 'section')[0].props.children[0].props.children, /2.*退出会保留后台任务/);
  button(tree, '返回应用').props.onClick();
  assert.equal(nodes(render(), 'section').length, 0);
  closeRequested();
  await Promise.resolve();
  await button(render(), '保留后台任务并退出').props.onClick();
  assert.equal(calls.at(-1)[0], 'exit_desktop');
  assert.equal(calls.some(([name]) => /submit|cancel/.test(name)), false);
  cleanup();
  delete globalThis.window;
});