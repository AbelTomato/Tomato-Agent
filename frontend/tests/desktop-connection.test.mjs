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

test('explicit connect, failure without retry, reconnect and active-task close confirmation', async () => {
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
  const cleanup = effects[0]();
  await button(tree, '发现 WSL 发行版').props.onClick();
  tree = render();
  nodes(tree, 'input')[0].props.onChange({ target: { value: '/backend' } });
  tree = render();
  await button(tree, '连接 / 重连助手服务').props.onClick();
  tree = render();
  assert.match(nodes(tree, 'p').map(node => node.props.children).join(' '), /连接失败/);
  assert.equal(calls.filter(([name]) => name === 'connect_service').length, 1);
  fail = false;
  await button(tree, '连接 / 重连助手服务').props.onClick();
  tree = render();
  assert.ok(button(tree, '断开连接'));
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