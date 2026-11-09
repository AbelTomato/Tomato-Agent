import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const entry = new URL('../src/main.tsx', import.meta.url);
const chatApp = new URL('../src/desktop-chat/DesktopChatApp.tsx', import.meta.url);

test('desktop chat development route selects an isolated empty chat shell', async () => {
  const source = await readFile(entry, 'utf8');
  assert.match(source, /desktop-chat/);
  assert.match(source, /DesktopChatApp/);
  assert.match(source, /import\.meta\.env\.MODE\s*===\s*['\"]desktop-chat['\"]/);
  assert.match(source, /URLSearchParams\(window\.location\.search\)\.get\(['"]desktop-chat['"]\)/);

  const component = await readFile(chatApp, 'utf8');
  assert.match(component, /模型设置/);
  assert.match(component, /desktop_chat_send/);
  assert.match(component, /desktop_chat_history/);
  assert.match(component, /desktop_chat_cancel/);
  assert.doesNotMatch(component, /DesktopConnection|ConversationShell|Workspace/i);
});

test('native chat command namespace is independent and has no startup side effects', async () => {
  const native = await readFile(new URL('../../desktop/src-tauri/src/chat.rs', import.meta.url), 'utf8');
  assert.match(native, /desktop_chat_status/);
  assert.match(native, /desktop_chat_send/);
  assert.match(native, /CREATE TABLE IF NOT EXISTS sessions/);
  assert.match(native, /NativeCredentialStore/);
  assert.doesNotMatch(native, /mod lifecycle|wsl::|agent\\.db|knowledge\\.db/i);
});

test('chat-only Tauri build selects isolated commands and keeps legacy config untouched', async () => {
  const native = await readFile(new URL('../../desktop/src-tauri/src/main.rs', import.meta.url), 'utf8');
  const chatConfig = await readFile(new URL('../../desktop/src-tauri/tauri.chat.conf.json', import.meta.url), 'utf8');
  const packageJson = await readFile(new URL('../../desktop/package.json', import.meta.url), 'utf8');
  const frontendPackageJson = await readFile(new URL('../package.json', import.meta.url), 'utf8');
  assert.match(native, /cfg\(feature = "desktop-chat"\)/);
  assert.match(native, /mod chat/);
  const chatEntry = native.split('#[cfg(feature = "desktop-chat")]').at(-1);
  assert.doesNotMatch(chatEntry, /lifecycle|legacy/);
  assert.match(native, /desktop_chat_latest_session/);
  assert.match(chatConfig, /desktop-chat=1/);
  assert.match(chatConfig, /frontend build:chat/);
  assert.match(frontendPackageJson, /build:chat.*vite build --mode desktop-chat/);
  assert.match(packageJson, /tauri:chat/);
  const legacyConfig = await readFile(new URL('../../desktop/src-tauri/tauri.conf.json', import.meta.url), 'utf8');
  assert.doesNotMatch(legacyConfig, /desktop-chat/);
});

test('desktop chat exposes session management and explicit real-provider connection test', async () => {
  const component = await readFile(chatApp, 'utf8');
  assert.match(component, /desktop_chat_sessions/);
  assert.match(component, /desktop_chat_rename_session/);
  assert.match(component, /desktop_chat_delete_session/);
  assert.match(component, /window\.confirm/);
  assert.match(component, /desktop_chat_test_connection/);
  assert.doesNotMatch(component, /Fake Provider/);
});

test('desktop chat exposes project directory grouping without file-access coupling', async () => {
  const component = await readFile(chatApp, 'utf8');
  assert.match(component, /desktop_chat_pick_project_directory/);
  assert.match(component, /desktop_chat_create_project/);
  assert.match(component, /desktop_chat_set_session_project/);
  assert.match(component, /desktop_chat_delete_project/);
  assert.match(component, /独立会话/);
  assert.doesNotMatch(component, /desktop_chat_send.*rootPath|workspace|read_file|wsl/i);
});
