import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const connection = readFileSync(new URL('../src/components/DesktopConnection.tsx', import.meta.url), 'utf8');
const api = readFileSync(new URL('../src/api.ts', import.meta.url), 'utf8');

test('桌面未连接时给出发行版发现、已有后端路径和明确错误详情', () => {
  assert.match(connection, /未发现 WSL 发行版/);
  assert.match(connection, /WSL 发现失败/);
  assert.match(connection, /高级连接设置：已有后端的 WSL 绝对路径/);
  assert.match(connection, /连接详情/);
  assert.match(connection, /切换环境将切换数据源/);
});

test('业务 API 在桌面走固定原生桥，Web 模式保留 fetch 且不使用浏览器凭据存储', () => {
  assert.match(api, /native\.core\.invoke.*desktop_request/s);
  assert.match(api, /response = await fetch\(`\$\{API_BASE_URL\}\$\{path\}`, init\)/);
  assert.doesNotMatch(api, /localStorage.*(token|secret|api.?key)/i);
  assert.match(connection, /WSL 环境/);
  assert.match(connection, /断开连接/);
});