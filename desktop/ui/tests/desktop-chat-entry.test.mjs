import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const entry = new URL('../src/main.tsx', import.meta.url);
const chatApp = new URL('../src/desktop-chat/DesktopChatApp.tsx', import.meta.url);

test('the application has one native desktop UI entry', async () => {
  const source = await readFile(entry, 'utf8');
  assert.match(source, /DesktopChatApp/);
  assert.doesNotMatch(source, /ConversationShell|DesktopConnection|legacy-web|searchParams/);

  const component = await readFile(chatApp, 'utf8');
  assert.match(component, /模型设置/);
  assert.match(component, /desktop_chat_send/);
  assert.match(component, /desktop_chat_history/);
  assert.match(component, /desktop_runtime_history/);
  assert.doesNotMatch(component, /desktop-chat-mode|setMode\(|useState<'chat'/);
  assert.match(component, /desktop_agent_submit_task/);
  assert.match(component, /desktop_chat_cancel/);
  assert.doesNotMatch(component, /DesktopConnection|ConversationShell/i);
  assert.match(component, /desktop_agent_workspaces/);
  assert.match(component, /bind_workspace/);
  assert.match(component, /authorize_project_read/);
  assert.match(component, /列出、搜索和读取/);
  assert.match(component, /任务结束后失效/);
  assert.match(component, /待审批文件修改/);
  assert.match(component, /change\.base_content/);
  assert.match(component, /拒绝修改/);
  assert.match(component, /批准并写入项目/);
  assert.match(component, /desktop_agent_inspect_changeset/);
  assert.match(component, /停止剩余修改/);
  assert.match(component, /完整内容均参与审批摘要/);
  assert.match(component, /逐行差异/);
  assert.match(component, /部分文件已经写入/);
  assert.match(component, /检查并继续剩余修改/);
  assert.match(component, /commitInspection\?\.status === 'needs_inspection'/);
});

test('native chat command namespace is independent and has no startup side effects', async () => {
  const native = (await Promise.all(['commands', 'store', 'runtime'].map(name => readFile(new URL(`../../src-tauri/src/chat/${name}.rs`, import.meta.url), 'utf8')))).join('\n');
  assert.match(native, /desktop_chat_status/);
  assert.match(native, /desktop_chat_send/);
  assert.match(native, /CREATE TABLE IF NOT EXISTS sessions/);
  assert.match(native, /NativeCredentialStore/);
  assert.doesNotMatch(native, /mod lifecycle|wsl::|agent\\.db|knowledge\\.db/i);
});

test('the default Tauri build bundles the desktop UI and managed engine', async () => {
  const native = await readFile(new URL('../../src-tauri/src/main.rs', import.meta.url), 'utf8');
  const appConfig = await readFile(new URL('../../src-tauri/tauri.conf.json', import.meta.url), 'utf8');
  const packageJson = await readFile(new URL('../../package.json', import.meta.url), 'utf8');
  const windowsBuild = await readFile(new URL('../../scripts/windows.ps1', import.meta.url), 'utf8');
  const engineBuild = await readFile(new URL('../../scripts/build-engine.ps1', import.meta.url), 'utf8');
  assert.match(native, /mod chat/);
  assert.match(native, /desktop_chat_latest_session/);
  assert.doesNotMatch(native, /legacy\.rs|feature\s*=\s*"desktop-chat"/);
  const tauriConfig = JSON.parse(appConfig);
  assert.equal(tauriConfig.build.beforeDevCommand, 'pnpm --dir ui dev -- --host 127.0.0.1');
  assert.equal(tauriConfig.build.beforeBuildCommand, 'pnpm --dir ui build');
  assert.match(appConfig, /tomato-agent-engine\.exe/);
  assert.match(packageJson, /"dev": "powershell .*windows\.ps1 -Action dev/);
  assert.match(packageJson, /"build": "powershell .*windows\.ps1 -Action build/);
  assert.match(packageJson, /"test:native": "powershell .*windows\.ps1 -Action test/);
  assert.match(windowsBuild, /D:\\Software\\TomatoAgent/);
  assert.match(windowsBuild, /Join-Path \$softwareRoot "windows-target"/);
  assert.match(windowsBuild, /Join-Path \$softwareRoot "installers"/);
  assert.match(windowsBuild, /AcceptanceCrashHook/);
  assert.match(windowsBuild, /desktop-acceptance-crash-hook/);
  assert.match(engineBuild, /D:\\Software\\TomatoAgent\\engine-build/);
  const cargo = await readFile(new URL('../../src-tauri/Cargo.toml', import.meta.url), 'utf8');
  assert.doesNotMatch(cargo, /desktop-chat/);
});

test('desktop chat exposes session management and explicit real-provider connection test', async () => {
  const component = await readFile(chatApp, 'utf8');
  assert.match(component, /desktop_chat_sessions/);
  assert.match(component, /desktop_chat_rename_session/);
  assert.match(component, /desktop_chat_delete_session/);
  assert.match(component, /确认删除/);
  assert.doesNotMatch(component, /window\.(prompt|confirm)/);
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
  assert.doesNotMatch(component, /desktop_chat_send[^\n]*rootPath|rootPath[^\n]*desktop_chat_send|read_file|wsl/i);
  assert.doesNotMatch(component, /project\.root_path[^\n]*workspace|workspace[^\n]*project\.root_path/i);
});
