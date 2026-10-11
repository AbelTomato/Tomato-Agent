import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { transformWithOxc } from 'vite';

const source = readFileSync(new URL('../src/api.ts', import.meta.url), 'utf8')
  .replace('import.meta.env.VITE_API_BASE_URL', 'undefined');
const { code } = await transformWithOxc(source, 'api.ts');
const api = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`);

test('统一协议编码 ID、分页、动作版本、原目录目标和 AbortSignal', async () => {
  const calls = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    calls.push({ path: new URL(url).pathname + new URL(url).search, init });
    return new Response('{}', { status: 202 });
  };
  try {
    const signal = new AbortController().signal;
    await api.listSessions(200, 4, signal);
    await api.listSessionTasks('s/a', 200, 2, signal);
    const body = { kind: 'code', message: 'fix', workspace_id: 'ws', client_request_id: 'req' };
    await api.submitTask('s/a', body, signal);
    await api.getTask('r/a', signal);
    await api.listTaskEvents('r/a', 7, signal);
    const action = { action_id: 'pending', action: 'continue_recovery', expected_version: 3,
      client_request_id: 'action', payload: { confirmed: true } };
    await api.actOnTask('r/a', action, signal);
    await api.listWorkspaces(signal);
    await api.registerWorkspace('/project', ['tests'], signal);
    await api.setSessionWorkspace('s/a', null, signal);
    assert.deepEqual(calls.map((call) => call.path), [
      '/api/sessions?limit=200&offset=4', '/api/sessions/s%2Fa/tasks?limit=200&offset=2',
      '/api/sessions/s%2Fa/tasks', '/api/tasks/r%2Fa', '/api/tasks/r%2Fa/events?after=7&limit=200',
      '/api/tasks/r%2Fa/actions', '/api/workspaces', '/api/workspaces', '/api/sessions/s%2Fa/workspace',
    ]);
    assert.deepEqual(JSON.parse(calls[2].init.body), body);
    assert.deepEqual(JSON.parse(calls[5].init.body), action);
    assert.deepEqual(JSON.parse(calls[7].init.body), { path: '/project', pytest_paths: ['tests'] });
    assert.deepEqual(JSON.parse(calls[8].init.body), { workspace_id: null });
    assert.ok(calls.every((call) => call.init.signal === signal));
  } finally { globalThis.fetch = original; }
});

test('统一 API 保留能力不足、冲突和不可用错误', async () => {
  const original = globalThis.fetch;
  try {
    for (const [status, code] of [[503, 'assistant_disabled'], [503, 'worker_disabled'], [409, 'stale_action'], [503, 'validation_backend_unavailable']]) {
      globalThis.fetch = async () => new Response(JSON.stringify({ detail: { code } }), { status });
      await assert.rejects(() => api.getTask('run'), (error) => error.status === status && error.code === code);
    }
  } finally { globalThis.fetch = original; }
});

test('助手空options、可空工作区和新公共动作按统一协议发送', async () => {
  const original = globalThis.fetch;
  const bodies = [];
  globalThis.fetch = async (_url, init) => { bodies.push(JSON.parse(init.body)); return new Response('{}', { status: 202 }); };
  try {
    await api.submitTask('s', { kind: 'assistant', message: '解释缓存', client_request_id: 'req', workspace_id: null, options: {} });
    for (const [action, payload] of [['provide_input', { message: '当前项目' }], ['bind_workspace', { workspace_id: 'ws' }], ['reject_action', {}], ['approve_action', {}]]) {
      await api.actOnTask('r', { action_id: 'a', expected_version: 4, client_request_id: action, action, payload });
    }
    assert.equal(bodies[0].workspace_id, null); assert.deepEqual(bodies[0].options, {});
    assert.deepEqual(bodies.slice(1).map((body) => [body.action, body.payload]), [
      ['provide_input', { message: '当前项目' }], ['bind_workspace', { workspace_id: 'ws' }], ['reject_action', {}], ['approve_action', {}],
    ]);
    assert.ok(bodies.slice(1).every((body) => body.expected_version === 4 && body.action_id === 'a'));
  } finally { globalThis.fetch = original; }
});