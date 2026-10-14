// Invoked by the backend integration test with real HTTP responses on stdin.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { transformWithOxc } from 'vite';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

async function compile(path, replacements) {
  let source = readFileSync(new URL(path, import.meta.url), 'utf8');
  for (const [pattern, replacement] of replacements) source = source.replace(pattern, replacement);
  const { code } = await transformWithOxc(source, path, { jsx: { runtime: 'automatic' } });
  const resolved = code.replace('"react/jsx-runtime"', JSON.stringify(import.meta.resolve('react/jsx-runtime')));
  return import(`data:text/javascript;base64,${Buffer.from(resolved).toString('base64')}`);
}
const api = await compile('../src/api.ts', [
  ['import.meta.env.VITE_API_BASE_URL', 'undefined'],
]);
const { TaskCard } = await compile('../src/components/TaskCard.tsx', [
  [/from 'react'/g, `from ${JSON.stringify(import.meta.resolve('react'))}`],
  [/import \{[^;]*\} from '..\/api';/, 'const loadCodeTaskArtifact = () => { throw new Error("unexpected artifact request"); };'],
  [/import \{ CitationList \} from '.\/CitationList';/, 'const CitationList = () => null;'],
  [/import \{ AssistantResult \} from '.\/AssistantResult';/, 'const AssistantResult = () => null;'],
  [/import \{ PendingActionPanel \} from '.\/PendingActionPanel';/, 'const PendingActionPanel = () => null;'],
]);
const { PendingActionPanel } = await compile('../src/components/PendingActionPanel.tsx', [
  [/from 'react'/g, `from ${JSON.stringify(import.meta.resolve('react'))}`],
  [/import \{[^;]*\} from '..\/api';/, 'const __apiTypes = {};'],
]);
const originalFetch = globalThis.fetch;
try {
  const input = readFileSync(0, 'utf8').trim();
  for (const response of JSON.parse(input || '[]')) {
    globalThis.fetch = async (url) => {
      assert.equal(new URL(url).pathname, `/api/tasks/${response.run_id}/actions`);
      return new Response(JSON.stringify(response), { status: 200 });
    };
    const task = await api.actOnTask(response.run_id, {
      action_id: 'recovery', action: 'inspect_recovery', expected_version: response.version,
      client_request_id: 'render', payload: {},
    });
    assert.deepEqual(task.recovery, response.recovery);
    const html = renderToStaticMarkup(createElement(TaskCard, {
      task, busy: false, onAction: async () => {}, onOpenDocument() {},
    }));
    assert.ok(html.includes(`恢复分类原因：${response.recovery.reason}`));
    assert.ok(html.includes(`分类动作：${response.recovery.action}`));
    assert.ok(html.includes('允许的后续动作：'));
    if (task.status === 'waiting') assert.ok(html.includes('确认请求恢复'));
    if (task.recovery.action === 'manual_review') assert.ok(html.includes('副作用结果尚未确认'));
  }
  const panel = renderToStaticMarkup(createElement(PendingActionPanel, {
    task: { status: 'waiting', legacy: false, capabilities: ['resolve_side_effect'],
      pending_action: { allowed_actions: ['resolve_side_effect'], display_data: {} } },
    busy: false, workspaces: [], onAction: async () => {},
  }));
  const panelSource = readFileSync(new URL('../src/components/PendingActionPanel.tsx', import.meta.url), 'utf8');
  assert.ok(panel.includes('接受风险并继续'));
  assert.ok(panelSource.includes('旧 Worker 或 Docker 请求仍可能迟到执行'));
  assert.ok(panelSource.includes('role="alertdialog"'));
} finally {
  globalThis.fetch = originalFetch;
}