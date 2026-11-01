import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const connection = readFileSync(new URL('../src/components/DesktopConnection.tsx', import.meta.url), 'utf8');
const api = readFileSync(new URL('../src/api.ts', import.meta.url), 'utf8');

test('发行版发现仅由设置刷新触发，保存偏好不连接服务', () => {
  assert.match(connection, /刷新 WSL 环境/);
  assert.match(connection, /已有后端的 WSL 绝对路径/);
  assert.match(connection, /新环境在下次工作区打开时生效/);
  assert.match(connection, /项目和会话不会迁移/);
  assert.match(connection, /save_environment_preferences/);
  assert.match(connection, /连接将在打开工作区时进行/);
  assert.match(connection, /无法连接执行环境；任务未提交/);
  assert.doesNotMatch(connection, /setInterval\(|发现 WSL 发行版.*useEffect/s);
});

test('业务 API 在桌面走固定原生桥，Web 模式保留 fetch 且不使用浏览器凭据存储', () => {
  assert.match(api, /native\.core\.invoke.*desktop_request/s);
  assert.match(api, /response = await fetch\(`\$\{API_BASE_URL\}\$\{path\}`, init\)/);
  assert.doesNotMatch(api, /localStorage.*(token|secret|api.?key)/i);
  assert.match(connection, /WSL 环境/);
  assert.match(connection, /关闭工作区/);
});