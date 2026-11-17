import { createServer } from 'node:http';
import { execFileSync, spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const debugPort = Number(process.env.TOMATO_AGENT_UI_PROBE_PORT ?? 9333);
const decisions = [];
let providerRequests = 0;
let providerConnections = 0;
let provider;
let target;
let changeset;
let project;
let sessionId;
let runId;
let aclDeniedFile;
let conflictUiError;
let aclUiError;
let appProcessRestarted = false;
let nextCommandId = 1;
let lastWebSocketEvent = 'connected';
let stage = 'startup';
let report = { passed: false };

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function waitFor(check, description, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const value = await check();
    if (value) return value;
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  throw new Error(`Timed out waiting for ${description}`);
}

async function pageTarget() {
  const response = await fetch(`http://127.0.0.1:${debugPort}/json/list`);
  assert(response.ok, `WebView2 debug endpoint returned ${response.status}`);
  const targets = await response.json();
  return targets.find(item => item.type === 'page' && item.title === 'Tomato Agent');
}

async function evaluate(expression) {
  target ??= await waitFor(pageTarget, 'the Tomato Agent WebView');
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true });
    socket.addEventListener('error', () => reject(new Error('Could not connect to the WebView debugger')), { once: true });
  });
  const id = nextCommandId++;
  const result = new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`WebView command timed out (${lastWebSocketEvent})`)), 10000);
    socket.addEventListener('message', event => {
      let message;
      try { message = JSON.parse(String(event.data)); } catch {
        lastWebSocketEvent = `unparseable message: ${Object.prototype.toString.call(event.data)}`;
        return;
      }
      lastWebSocketEvent = `received ${message.method ?? 'response'}${message.id === undefined ? '' : ` id=${message.id}`}`;
      if (message.id !== id) return;
      clearTimeout(timer);
      if (message.error) reject(new Error(message.error.message));
      else if (message.result?.exceptionDetails) {
        reject(new Error(message.result.exceptionDetails.exception?.description
          ?? message.result.exceptionDetails.text));
      } else resolve(message.result?.result?.value);
    });
    socket.addEventListener('close', event => {
      lastWebSocketEvent = `closed code=${event.code} reason=${event.reason}`;
    });
    socket.addEventListener('error', () => { lastWebSocketEvent = 'websocket error'; });
  });
  socket.send(JSON.stringify({
    id,
    method: 'Runtime.evaluate',
    params: { expression, awaitPromise: true, returnByValue: true, userGesture: true },
  }));
  try {
    return await result;
  } finally {
    socket.close();
  }
}

async function invoke(command, args = {}) {
  const serializedCommand = JSON.stringify(command);
  const serializedArgs = JSON.stringify(args);
  const result = await evaluate(`(async()=>{
    try {
      return JSON.stringify({ ok: true, value: await window.__TAURI__.core.invoke(${serializedCommand},${serializedArgs}) });
    } catch (error) {
      return JSON.stringify({ ok: false, error: String(error), stack: error?.stack ?? null });
    }
  })()`);
  const decoded = result === undefined ? undefined : JSON.parse(result);
  if (!decoded?.ok) {
    const detail = decoded?.error ?? 'Tauri command returned no result';
    throw new Error(`${command} failed: ${detail}${decoded?.stack ? `\n${decoded.stack}` : ''}`);
  }
  return decoded.value;
}

async function uiText() {
  return evaluate('document.body.innerText');
}

async function clickButton(label) {
  return evaluate(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()===${JSON.stringify(label)}&&!x.disabled);if(!b)return false;b.click();return true})()`);
}

async function reloadPage() {
  const previousTimeOrigin = await evaluate('performance.timeOrigin');
  await evaluate('setTimeout(()=>location.reload(),100);true');
  target = undefined;
  await waitFor(async () => {
    try {
      return await evaluate('document.readyState === "complete" && performance.timeOrigin') !== previousTimeOrigin;
    } catch {
      return false;
    }
  }, 'a newly loaded desktop page');
}

async function startProvider(projectRoot) {
  await queueChangeset(projectRoot, 'after\n', '修改提案已获批准并落盘');

  provider = createServer(async (request, response) => {
    providerConnections += 1;
    let requestBody = '';
    for await (const chunk of request) requestBody += chunk;
    let parsed;
    try { parsed = JSON.parse(requestBody); } catch {
      response.writeHead(400).end();
      return;
    }
    if (!Array.isArray(parsed.messages) || providerRequests >= decisions.length) {
      response.writeHead(400).end();
      return;
    }
    const decision = decisions[providerRequests++];
    const content = JSON.stringify({ protocol_version: 1, decision });
    const body = JSON.stringify({ choices: [{ message: { content } }] });
    response.writeHead(200, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(body) });
    response.end(body);
  });
  await new Promise((resolve, reject) => {
    provider.once('error', reject);
    provider.listen(0, '127.0.0.1', resolve);
  });
  return provider.address().port;
}

async function queueChangeset(projectRoot, content, answer) {
  const file = join(projectRoot, 'README.md');
  const baseContent = await readFile(file, 'utf8');
  const expectedOldSha256 = createHash('sha256').update(baseContent).digest('hex');
  changeset = {
    relative_path: 'README.md', operation: 'modify', expected_old_sha256: expectedOldSha256,
    base_content: baseContent, content: 'after\n', summary: 'Update project notes',
  };
  decisions.push(
    { type: 'call_tool', payload: { name: 'read_file', contract_version: '1', arguments: JSON.stringify({ path: 'README.md' }) } },
    { type: 'call_tool', payload: { name: 'read_file', contract_version: '1', arguments: JSON.stringify({ path: 'README.md' }) } },
    { type: 'call_tool', payload: { name: 'propose_changeset', contract_version: '1', arguments: JSON.stringify({ changes: [changeset] }) } },
    { type: 'finish', payload: { result: { answer, citations: [], artifacts: [], test_results: [], validation_status: 'not_required' } } },
  );
}

async function proposeFromComposer(projectRoot, prompt, content) {
  await queueChangeset(projectRoot, content, '提案已处理');
  await waitFor(async () => submitFromComposer(prompt), 'the desktop composer to accept a follow-up request');
  await waitForButton('允许本次读取');
  assert(await clickButton('允许本次读取'), 'The follow-up project-read button was not clickable');
  await waitForButton('批准并写入项目');
}

function setWriteDenied(file) {
  aclDeniedFile = file;
  execFileSync('attrib.exe', ['+R', file], { windowsHide: true, stdio: 'pipe' });
}

function restoreWriteAccess() {
  if (!aclDeniedFile) return;
  execFileSync('attrib.exe', ['-R', aclDeniedFile], { windowsHide: true, stdio: 'pipe' });
  aclDeniedFile = undefined;
}

function processExists(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    if (error?.code === 'ESRCH') return false;
    throw error;
  }
}

function restartProbeDesktop() {
  const executable = process.env.TOMATO_AGENT_UI_PROBE_APP;
  assert(executable, 'The isolated desktop executable path was not supplied');
  const environment = { ...process.env, WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS: `--remote-debugging-port=${debugPort}` };
  delete environment.TOMATO_AGENT_CRASH_AFTER_WRITE;
  const child = spawn(executable, [], { env: environment, stdio: 'ignore', windowsHide: false });
  assert(child.pid, 'Could not restart the isolated desktop application');
  child.unref();
  return child.pid;
}

async function waitForButton(label) {
  return waitFor(async () => {
    return evaluate(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()===${JSON.stringify(label)});return Boolean(b&&!b.disabled)})()`);
  }, `the enabled '${label}' action`);
}

async function submitFromComposer(text) {
  return evaluate(`(()=>{
    const area=document.querySelector('textarea[aria-label="消息"]');
    const form=document.querySelector('form.desktop-chat-composer');
    if(!area||area.disabled||!form)return false;
    const setter=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;
    setter.call(area,${JSON.stringify(text)});
    area.dispatchEvent(new Event('input',{bubbles:true}));
    area.dispatchEvent(new Event('change',{bubbles:true}));
    const button=form.querySelector('button[type="submit"]');
    if(!button||button.disabled)return false;
    button.click();
    return true;
  })()`);
}

async function main() {
  const projectRoot = await mkdtemp(join(tmpdir(), 'tomato-ui-ipc-project-'));
  const output = process.env.TOMATO_AGENT_UI_PROBE_REPORT ?? join(tmpdir(), 'tomato-agent-ui-ipc-probe.json');
  try {
    await writeFile(join(projectRoot, 'README.md'), 'before\n', 'utf8');
    const port = await startProvider(projectRoot);
    await waitFor(async () => Boolean(await pageTarget()), 'the WebView2 target');

    stage = 'configure-local-provider';
    await invoke('desktop_chat_configure', { input: {
      baseUrl: `http://127.0.0.1:${port}/v1`, model: 'desktop-ui-ipc-probe',
      apiKey: process.env.TOMATO_AGENT_UI_PROBE_API_KEY ?? null,
    } });
    stage = 'verify-engine-start-is-on-demand';
    const idleEngine = await invoke('desktop_agent_status');
    assert(idleEngine === null, `The engine started before a Runtime operation: ${JSON.stringify(idleEngine)}`);
    stage = 'reload-after-configuration';
    await reloadPage();

    stage = 'migrate-history-through-tauri-ipc';
    const historySource = await waitFor(async () => {
      const source = await invoke('desktop_chat_history_source');
      return source === 'python' ? source : false;
    }, 'the UI to migrate and activate Python history');
    assert(historySource === 'python', 'Python history did not become active');
    stage = 'verify-engine-process-reuse';
    const firstEngine = await invoke('desktop_agent_start');
    const secondEngine = await invoke('desktop_agent_start');
    assert(firstEngine.service_instance_id === secondEngine.service_instance_id,
      'Repeated Runtime startup did not reuse the engine process');

    stage = 'create-project-and-session-through-tauri-ipc';
    project = await invoke('desktop_chat_create_project', { input: { rootPath: projectRoot, name: 'IPC 验收项目' } });
    sessionId = await invoke('desktop_runtime_create_session', { projectId: project.id });
    await invoke('desktop_runtime_set_session_project', { sessionId, projectId: project.id });
    await reloadPage();
    stage = 'wait-for-project-session-in-ui';
    await waitFor(async () => {
      try {
        const text = await uiText();
        return text.includes('IPC 验收项目') && text.includes('发送消息开始聊天');
      } catch { return false; }
    }, 'the project session in the desktop UI');
    stage = 'select-project-session-in-ui';
    await waitForButton('新对话');
    const projectSessionButtons = await evaluate(`JSON.stringify([...document.querySelectorAll('button')].filter(x=>x.textContent.trim()==='新对话').map(x=>({disabled:x.disabled,current:x.getAttribute('aria-current')})))`);
    assert(await clickButton('新对话'), `The project session was not clickable: ${projectSessionButtons}`);
    await waitFor(async () => evaluate('Boolean(document.querySelector(\'textarea[aria-label="消息"]\') && !document.querySelector(\'textarea[aria-label="消息"]\').disabled)'), 'the selected session composer');

    stage = 'submit-from-composer';
    assert(await submitFromComposer('请阅读并修改 README'), 'The desktop composer did not submit');
    stage = 'authorize-project-read-in-ui';
    await waitForButton('允许本次读取');
    const projectReadAuthorized = await clickButton('允许本次读取');
    assert(projectReadAuthorized, 'The project-read approval button was not clickable');
    stage = 'review-changeset-in-ui';
    await waitForButton('批准并写入项目');
    const pendingApproval = await uiText();
    assert(pendingApproval.includes('逐行差异'), 'The ChangeSet diff was not rendered');
    const proposedDigest = pendingApproval.match(/\b[a-f0-9]{64}\b/)?.[0];
    assert(proposedDigest, 'The pending ChangeSet digest was not rendered');
    stage = 'restore-pending-approval-after-page-reload';
    await reloadPage();
    await waitForButton('批准并写入项目');
    const restoredApproval = await uiText();
    assert(restoredApproval.includes(proposedDigest), 'The pending ChangeSet was not restored after reload');
    assert(restoredApproval.includes('逐行差异'), 'The restored ChangeSet diff was not rendered');
    stage = 'approve-changeset-in-ui';
    assert(await clickButton('批准并写入项目'), 'The ChangeSet approval button was not clickable');

    if (process.env.TOMATO_AGENT_UI_PROBE_RESTART_APP === '1') {
      stage = 'verify-application-process-interruption';
      const interruptedPid = Number(process.env.TOMATO_AGENT_UI_PROBE_APP_PID);
      assert(Number.isInteger(interruptedPid) && interruptedPid > 0, 'The isolated desktop process ID was not supplied');
      await waitFor(async () => !processExists(interruptedPid), 'the desktop process to stop at the journal interruption point');
      const interruptedContents = await readFile(join(projectRoot, 'README.md'), 'utf8');
      assert(interruptedContents === 'after\n', 'The forced process interruption did not occur after the atomic file replacement');
      const restartedPid = restartProbeDesktop();
      target = undefined;
      await waitFor(async () => {
        try { return Boolean(await pageTarget()); } catch { return false; }
      }, 'the restarted desktop WebView', 60000);
      await waitForButton('批准并写入项目');
      const recoveredApproval = await uiText();
      assert(recoveredApproval.includes(proposedDigest), 'The pending ChangeSet did not recover after application restart');
      assert(recoveredApproval.includes('逐行差异'), 'The recovered ChangeSet diff was not rendered');
      assert(await clickButton('批准并写入项目'), 'The recovered ChangeSet could not be approved after restart');
      appProcessRestarted = true;
    }

    stage = 'verify-runtime-completion';
    const committed = await waitFor(async () => {
      const contents = await readFile(join(projectRoot, 'README.md'), 'utf8');
      return contents === 'after\n' ? contents : false;
    }, 'the approved project file write');
    const task = await waitFor(async () => {
      const tasks = await invoke('desktop_agent_session_tasks', { sessionId });
      return tasks.find(item => item.status === 'completed');
    }, 'the Runtime task to finish after the approved write');
    runId = task.run_id;
    const eventsResponse = await invoke('desktop_agent_task_events', { sessionId, runId, after: 0 });
    const eventTypes = (eventsResponse.body?.events ?? []).map(item => item.event_type);
    assert(eventTypes.includes('changeset.proposed'), 'The Runtime did not persist changeset.proposed');
    assert(eventTypes.includes('assistant.action'), 'The Runtime did not persist assistant.action');
    await waitFor(async () => (await uiText()).includes('修改提案已获批准并落盘'),
      'the final approved-write answer in the desktop UI');
    assert(providerRequests === 4, `Expected four Provider calls, received ${providerRequests}`);

    stage = 'verify-ui-external-conflict-protection';
    await proposeFromComposer(projectRoot, '再次修改 README', 'conflict-generated\n');
    await writeFile(join(projectRoot, 'README.md'), 'external-conflict\n', 'utf8');
    assert(await clickButton('批准并写入项目'), 'The conflicting ChangeSet approval button was not clickable');
    conflictUiError = await waitFor(async () => {
      const error = await evaluate('document.querySelector(".desktop-chat-error[role=alert]")?.textContent ?? ""');
      return error.includes('workspace_conflict') ? error : false;
    }, 'the visible external-conflict approval error');
    const conflictContents = await readFile(join(projectRoot, 'README.md'), 'utf8');
    assert(conflictContents === 'external-conflict\n', 'External file changes were overwritten');
    assert(await clickButton('拒绝修改'), 'Could not close the conflicted pending proposal');
    await waitFor(async () => (await uiText()).includes('提案已处理'), 'the conflict Run to finish');

    stage = 'verify-ui-write-permission-error';
    await proposeFromComposer(projectRoot, '检查无写权限时的审批反馈', 'permission-generated\n');
    setWriteDenied(join(projectRoot, 'README.md'));
    assert(await clickButton('批准并写入项目'), 'The permission-denied ChangeSet approval button was not clickable');
    aclUiError = await waitFor(async () => {
      const error = await evaluate('document.querySelector(".desktop-chat-error[role=alert]")?.textContent ?? ""');
      return error.includes('workspace_write_failed') || error.includes('修改未能完整应用') ? error : false;
    }, 'the visible project write-permission error');
    restoreWriteAccess();
    const permissionContents = await readFile(join(projectRoot, 'README.md'), 'utf8');
    assert(permissionContents === 'external-conflict\n', 'A write-permission failure changed the project file');
    assert(await clickButton('查看提交状态'), 'Could not inspect the failed commit journal');
    const permissionInspection = await waitFor(async () => {
      const text = await uiText();
      return text.includes('提交状态：未写入') && text.includes('workspace_write_failed') ? text : false;
    }, 'the visible failed commit status');
    assert(permissionInspection.includes('当前没有文件写入成功'), 'The failed write status did not explain that no files were written');
    assert(await clickButton('停止剩余修改'), 'Could not stop the permission-denied pending proposal');
    await waitFor(async () => (await uiText()).includes('提案已处理'), 'the permission-denied Run to finish');
    assert(providerRequests >= 8 && providerRequests <= decisions.length,
      `Unexpected Provider call count after fault checks: ${providerRequests}`);

    report = {
      passed: true,
      history_source: historySource,
      desktop_session_id: sessionId,
      project_id: project.id,
      run_id: runId,
      status: task.status,
      authorized: projectReadAuthorized,
      engine_started_on_demand: idleEngine === null,
      engine_reused: firstEngine.service_instance_id === secondEngine.service_instance_id,
      pending_approval_restored_after_reload: restoredApproval.includes(proposedDigest),
      committed_file: 'README.md',
      committed_content: committed,
      application_process_restarted: appProcessRestarted,
      journal_recovery_after_app_interruption: appProcessRestarted,
      external_conflict_preserved: conflictContents === 'external-conflict\n',
      external_conflict_ui_error: conflictUiError,
      write_permission_denied: permissionContents === 'external-conflict\n',
      write_permission_ui_error: aclUiError,
      failed_commit_status_visible: permissionInspection.includes('workspace_write_failed'),
      provider_requests: providerRequests,
      event_types: eventTypes,
      webview_ipc: true,
      provider_scope: '127.0.0.1',
    };
  } catch (error) {
    report = { ...report, error: error instanceof Error ? error.message : String(error), stage, provider_requests: providerRequests, provider_connections: providerConnections };
    try { report.ui_text = (await uiText()).slice(0, 2000); } catch { report.ui_text = null; }
    if (sessionId) {
      try {
        const tasks = await invoke('desktop_agent_session_tasks', { sessionId });
        const latestTask = tasks[0];
        report.runtime_task = latestTask ? {
          run_id: latestTask.run_id,
          status: latestTask.status,
          phase: latestTask.phase,
          error: latestTask.error,
        } : null;
        if (latestTask?.run_id) {
          const events = await invoke('desktop_agent_task_events', { sessionId, runId: latestTask.run_id, after: 0 });
          report.runtime_events = (events.body?.events ?? []).map(item => ({
            event_type: item.event_type,
            summary: item.summary,
            payload: item.payload,
          }));
        }
      } catch (diagnosticError) {
        report.runtime_diagnostic_error = diagnosticError instanceof Error ? diagnosticError.message : String(diagnosticError);
      }
    }
  } finally {
    try { restoreWriteAccess(); } catch (error) {
      report.cleanup_error = error instanceof Error ? error.message : String(error);
      report.passed = false;
    }
    try {
      if (sessionId) await invoke('desktop_runtime_delete_session', { sessionId });
      if (project) await invoke('desktop_chat_delete_project', { projectId: project.id });
      await invoke('desktop_agent_stop').catch(() => undefined);
    } catch (error) {
      report.cleanup_error = error instanceof Error ? error.message : String(error);
      report.passed = false;
    }
    if (provider) await new Promise(resolve => provider.close(resolve));
    await rm(projectRoot, { recursive: true, force: true });
    await writeFile(output, JSON.stringify(report, null, 2), 'utf8');
  }
  if (!report.passed) throw new Error(`Desktop WebView IPC probe failed; report: ${output}`);
  console.log(JSON.stringify(report));
}

await main();
