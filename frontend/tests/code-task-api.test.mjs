import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { transformWithOxc } from 'vite';

const source = readFileSync(new URL('../src/api.ts', import.meta.url), 'utf8')
  .replace('import.meta.env.VITE_API_BASE_URL', 'undefined');
const compiled = (await transformWithOxc(source, 'api.ts')).code;
const api = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);

test('代码任务协议包含全部端点、编码标识、游标和恢复确认', async () => {
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return new Response(JSON.stringify({ status: 'queued' }), { status: 202 });
  };
  try {
    const controller = new AbortController();
    await api.createCodeTask('fix calculator', controller.signal);
    await api.getCodeTask('run/a', controller.signal);
    await api.listCodeTaskEvents('run/a', 7, controller.signal);
    await api.executeCodeTask('run/a', controller.signal);
    await api.cancelCodeTask('run/a', controller.signal);
    await api.recoverCodeTask('run/a', 3, 'inspect', false, controller.signal);
    await api.recoverCodeTask('run/a', 3, 'continue', true, controller.signal);
    await api.loadCodeTaskArtifact('run/a', 'artifact/b', controller.signal);
    assert.deepEqual(calls.map(({ url }) => new URL(url).pathname + new URL(url).search), [
      '/api/code-tasks', '/api/code-tasks/run%2Fa', '/api/code-tasks/run%2Fa/events?after=7',
      '/api/code-tasks/run%2Fa/execute', '/api/code-tasks/run%2Fa/cancel',
      '/api/code-tasks/run%2Fa/recover', '/api/code-tasks/run%2Fa/recover',
      '/api/code-tasks/run%2Fa/artifacts/artifact%2Fb',
    ]);
    assert.deepEqual(JSON.parse(calls[0].init.body), { task: 'fix calculator' });
    assert.deepEqual(JSON.parse(calls[6].init.body), { expected_version: 3, action: 'continue', confirm: true });
    assert.ok(calls.every(({ init }) => init.signal === controller.signal));
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('恢复冲突保留 ApiError 状态与后端错误码', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: { code: 'version_conflict' } }), { status: 409 });
  try {
    await assert.rejects(() => api.recoverCodeTask('run', 1, 'continue', true),
      (error) => error instanceof api.ApiError && error.status === 409 && error.code === 'version_conflict');
  } finally {
    globalThis.fetch = originalFetch;
  }
});