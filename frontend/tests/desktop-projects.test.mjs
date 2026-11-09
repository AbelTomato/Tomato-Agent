import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const component = new URL('../src/desktop-chat/DesktopChatApp.tsx', import.meta.url);
const native = new URL('../../desktop/src-tauri/src/chat.rs', import.meta.url);
const entry = new URL('../../desktop/src-tauri/src/main.rs', import.meta.url);

test('project navigation groups sessions and keeps unassigned sessions visible', async () => {
  const source = await readFile(component, 'utf8');
  assert.match(source, /sessions\.filter\(session => session\.project_id === project\.id\)/);
  assert.match(source, /sessions\.filter\(session => !session\.project_id\)/);
  assert.match(source, /desktop_chat_set_session_project/);
  assert.match(source, /desktop_chat_delete_project/);
});

test('native project commands use the chat database and native directory picker', async () => {
  const source = await readFile(native, 'utf8');
  const main = await readFile(entry, 'utf8');
  assert.match(source, /CREATE TABLE IF NOT EXISTS projects/);
  assert.match(source, /project_id TEXT REFERENCES projects/);
  assert.match(source, /desktop_chat_pick_project_directory/);
  assert.match(source, /desktop_chat_set_session_project/);
  assert.match(main, /tauri_plugin_dialog::init/);
  assert.doesNotMatch(source, /lifecycle|wsl\.exe|agent\.db|knowledge\.db/i);
});
