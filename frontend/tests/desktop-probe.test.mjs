import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { transformWithOxc } from 'vite';

let slots = [], index = 0;
globalThis.__desktopHooks = {
  useState(initial) {
    const slot = index++;
    if (!(slot in slots)) slots[slot] = initial;
    return [slots[slot], value => { slots[slot] = value; }];
  },
};
const path = '../src/components/DesktopProbe.tsx';
const source = readFileSync(new URL(path, import.meta.url), 'utf8')
  .replace("import { useState } from 'react';", 'const { useState } = globalThis.__desktopHooks;');
const { code } = await transformWithOxc(source, path, { jsx: { runtime: 'automatic' } });
const { DesktopProbe, isDesktop } = await import(`data:text/javascript;base64,${Buffer.from(
  code.replace('"react/jsx-runtime"', JSON.stringify(import.meta.resolve('react/jsx-runtime'))),
).toString('base64')}`);
function render() { index = 0; return DesktopProbe(); }
function nodes(tree, type) {
  if (!tree || typeof tree !== 'object') return [];
  return [...(tree.type === type ? [tree] : []),
    ...[tree.props?.children].flat(Infinity).flatMap(child => nodes(child, type))];
}

test('desktop bridge discovery and probe send only environment and path; failures stay visible', async () => {
  slots = [];
  const calls = [];
  globalThis.window = { __TAURI__: { core: { async invoke(command, args) {
    calls.push([command, args]);
    if (command === 'distributions') return ['Ubuntu'];
    throw new Error('Windows 到 WSL 的认证连接失败');
  } } } };
  assert.equal(isDesktop(), true);
  let tree = render();
  assert.equal(nodes(tree, 'button')[1].props.disabled, true);
  await nodes(tree, 'button')[0].props.onClick();
  tree = render();
  nodes(tree, 'input')[0].props.onChange({ target: { value: '/home/用户/含 空格/backend' } });
  tree = render();
  assert.equal(nodes(tree, 'button')[1].props.disabled, false);
  await nodes(tree, 'button')[1].props.onClick();
  tree = render();
  assert.deepEqual(calls[1], ['probe', { distribution: 'Ubuntu', backendPath: '/home/用户/含 空格/backend' }]);
  assert.match(nodes(tree, 'pre')[0].props.children, /认证连接失败/);
  assert.equal(nodes(tree, 'button')[1].props.disabled, false);
  globalThis.window = {};
  assert.equal(isDesktop(), false);
});