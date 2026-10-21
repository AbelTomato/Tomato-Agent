import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const entry = new URL('../src/main.tsx', import.meta.url);
const chatApp = new URL('../src/desktop-chat/DesktopChatApp.tsx', import.meta.url);

test('desktop chat development route selects an isolated empty chat shell', async () => {
  const source = await readFile(entry, 'utf8');
  assert.match(source, /desktop-chat/);
  assert.match(source, /DesktopChatApp/);
  assert.match(source, /URLSearchParams\(window\.location\.search\)\.get\(['"]desktop-chat['"]\)/);

  const component = await readFile(chatApp, 'utf8');
  assert.match(component, /模型尚未配置/);
  assert.doesNotMatch(component, /invoke\(|fetch\(|DesktopConnection|ConversationShell|Project|Workspace/i);
});

test('native chat command namespace is independent and has no startup side effects', async () => {
  const native = await readFile(new URL('../../desktop/src-tauri/src/chat.rs', import.meta.url), 'utf8');
  assert.match(native, /desktop_chat_status/);
  assert.doesNotMatch(native, /lifecycle|wsl|worker|agent\.db|knowledge\.db/i);
});