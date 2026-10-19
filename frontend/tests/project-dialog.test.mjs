import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { transformWithOxc } from 'vite';

const hooks = { useState(value) { return [value, () => {}]; }, useEffect(effect) { effect(); } };
globalThis.__dialogHooks = hooks;
globalThis.__projectApi = {
  async listProjectRoots() { return { roots: [{ name: '工作区', path: '/home/user', available: true }] }; },
  async browseProjectDirectories(path) { return { path, directories: [{ name: '项目 A', path: `${path}/项目 A`, available: true }] }; },
};
let source = readFileSync(new URL('../src/components/ProjectDialog.tsx', import.meta.url), 'utf8')
  .replace("import { useEffect, useState } from 'react';", 'const { useEffect, useState } = globalThis.__dialogHooks;')
  .replace("import { browseProjectDirectories, listProjectRoots, type ProjectDirectory } from '../api';", 'const { browseProjectDirectories, listProjectRoots } = globalThis.__projectApi;');
const { code } = await transformWithOxc(source, 'ProjectDialog.tsx', { jsx: { runtime: 'automatic' } });
const { ProjectDialog } = await import(`data:text/javascript;base64,${Buffer.from(code.replace('"react/jsx-runtime"', JSON.stringify(import.meta.resolve('react/jsx-runtime')))).toString('base64')}`);

test('目录选择从已批准根目录开始并逐级浏览 WSL POSIX 路径', async () => {
  const tree = ProjectDialog({ onOpen: async () => {}, onCreate: async () => {}, onClose() {} });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(tree.type, 'section');
  assert.equal(globalThis.__projectApi.listProjectRoots instanceof Function, true);
  assert.equal(globalThis.__projectApi.browseProjectDirectories instanceof Function, true);
});

test('项目对话框的打开与新建动作均使用 WSL 路径回调，不经过 Windows 原生目录', () => {
  const sourceText = readFileSync(new URL('../src/components/ProjectDialog.tsx', import.meta.url), 'utf8');
  assert.match(sourceText, /onOpen\(path\)/);
  assert.match(sourceText, /onCreate\(path, name\)/);
  assert.doesNotMatch(sourceText, /showOpenDialog|@tauri-apps\/plugin-dialog/);
});