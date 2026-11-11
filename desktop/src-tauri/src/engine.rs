use reqwest::blocking::Client;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;
use tauri::Manager;

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AgentTaskInput {
    pub session_id: String,
    pub request_id: String,
    pub content: String,
    #[serde(default = "default_task_kind")]
    pub kind: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct MemoryItemIdInput { pub item_id: String }

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct MemoryCorrectInput { pub item_id: String, pub expected_version: u32, pub content: String }

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct MemoryCandidateIdInput { pub candidate_id: String }

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct MemoryGenerationInput { pub expected_generation: u64 }

fn default_task_kind() -> String { "assistant".into() }

fn local_draft_directory(local_app_data: &Path, identifier: &str) -> PathBuf {
    local_app_data
        .join("TomatoAgent")
        .join("drafts")
        .join(identifier)
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct EngineIdentity {
    pub protocol: u32,
    pub port: u16,
    pub service_instance_id: String,
    #[serde(default)]
    pub owned: bool,
}

#[cfg(test)]
mod tests {
    use super::{local_draft_directory, AgentTaskInput, EngineIdentity, EngineManager};
    use std::path::{Path, PathBuf};

    #[test]
    fn draft_directory_is_host_local_and_scoped_to_application_identifier() {
        assert_eq!(
            local_draft_directory(Path::new("local-data"), "dev.tomato.agent"),
            PathBuf::from("local-data")
                .join("TomatoAgent")
                .join("drafts")
                .join("dev.tomato.agent")
        );
    }

    #[test]
    fn desktop_task_kind_defaults_to_assistant_and_accepts_writing() {
        let input: AgentTaskInput = serde_json::from_value(serde_json::json!({
            "sessionId": "session", "requestId": "request", "content": "topic"
        })).unwrap();
        assert_eq!(input.kind, "assistant");
        let input: AgentTaskInput = serde_json::from_value(serde_json::json!({
            "sessionId": "session", "requestId": "request", "content": "topic", "kind": "writing"
        })).unwrap();
        assert_eq!(input.kind, "writing");
    }

    #[test]
    fn python_service_identity_defaults_to_unowned_until_process_is_tracked() {
        let identity: EngineIdentity = serde_json::from_value(serde_json::json!({
            "protocol": 1,
            "port": 43210,
            "service_instance_id": "instance"
        }))
        .unwrap();
        assert!(!identity.owned);
    }

    #[test]
    fn pending_source_deletions_gate_memory_recall_before_engine_access() {
        let path = std::env::temp_dir().join(format!("memory-gate-{}.db", uuid::Uuid::new_v4()));
        let store = crate::chat::ChatStore::open(&path).unwrap();
        let session = store.create_session("Memory gate").unwrap();
        store.append_message(&session, "user", "private source").unwrap();
        store.set_memory_enabled(true).unwrap();
        store.delete_session(&session).unwrap();
        let engine = EngineManager::new().unwrap();
        let result = engine.memory_request(&store, "POST", "/api/memory/recall", None);
        assert_eq!(result.unwrap_err(), "记忆撤销尚未同步，暂时停用记忆读取");
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn pending_item_forget_is_persistent_and_gates_memory_reads_until_acknowledged() {
        let path = std::env::temp_dir().join(format!("memory-forget-gate-{}.db", uuid::Uuid::new_v4()));
        let item_id = uuid::Uuid::new_v4().to_string();
        {
            let store = crate::chat::ChatStore::open(&path).unwrap();
            store.set_memory_enabled(true).unwrap();
            let request_id = store.enqueue_memory_item_forget(&item_id, 3).unwrap();
            assert_eq!(store.enqueue_memory_item_forget(&item_id, 3).unwrap(), request_id);
            assert_eq!(store.pending_memory_item_forgets().unwrap(), vec![(item_id.clone(), request_id, 3)]);
            assert_eq!(store.ensure_memory_reads_allowed().unwrap_err(), "记忆撤销尚未同步，暂时停用记忆读取");
        }
        let store = crate::chat::ChatStore::open(&path).unwrap();
        let pending = store.pending_memory_item_forgets().unwrap();
        assert_eq!(pending.len(), 1);
        store.acknowledge_memory_item_forget(&pending[0].1).unwrap();
        assert!(store.ensure_memory_reads_allowed().is_ok());
        assert_eq!(store.pending_memory_mutation_count().unwrap(), 0);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn pending_clear_is_persistent_and_blocks_reads_until_acknowledged() {
        let path = std::env::temp_dir().join(format!("memory-clear-gate-{}.db", uuid::Uuid::new_v4()));
        let store = crate::chat::ChatStore::open(&path).unwrap();
        store.set_memory_enabled(true).unwrap();
        let request_id = store.enqueue_memory_clear("windows-local", 12).unwrap();
        assert_eq!(store.enqueue_memory_clear("windows-local", 12).unwrap(), request_id);
        assert_eq!(store.pending_memory_clear().unwrap(), Some(("windows-local".into(), request_id.clone(), 12)));
        assert!(store.ensure_memory_reads_allowed().is_err());
        store.acknowledge_memory_clear(&request_id).unwrap();
        assert!(store.ensure_memory_reads_allowed().is_ok());
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn pending_memory_mutations_block_source_ingestion_until_acknowledged() {
        let path = std::env::temp_dir().join(format!("memory-mutation-ingestion-gate-{}.db", uuid::Uuid::new_v4()));
        let store = crate::chat::ChatStore::open(&path).unwrap();
        store.set_memory_enabled(true).unwrap();
        assert!(super::memory_ingestion_allowed(&store).unwrap());
        store.enqueue_memory_clear("windows-local", 2).unwrap();
        assert!(!super::memory_ingestion_allowed(&store).unwrap());
        let (_, request_id, _) = store.pending_memory_clear().unwrap().unwrap();
        store.acknowledge_memory_clear(&request_id).unwrap();
        assert!(super::memory_ingestion_allowed(&store).unwrap());
        let _ = std::fs::remove_file(path);
    }
}

struct Process {
    child: Child,
    identity: EngineIdentity,
    token: String,
    owner: String,
    client: String,
    memory_enabled: bool,
}

pub struct EngineManager {
    process: Mutex<Option<Process>>,
    client: Client,
}

pub fn write_probe_report(
    app: &tauri::AppHandle,
    engine: &EngineManager,
    output: &Path,
) -> Result<(), String> {
    if !output.is_absolute() {
        return Err("验收报告路径必须是绝对路径".into());
    }
    let config = super::chat::ChatConfig {
        base_url: "https://engine-probe.invalid/v1".into(),
        model: "engine-probe".into(),
    };
    let result = (|| {
        let identity = engine.start(app, &config, "engine-probe-key")?;
        let response = engine.request("GET", "/api/desktop/connection", None)?;
        let data_dir = app
            .path()
            .app_data_dir()
            .map_err(|_| "无法定位应用数据目录")?
            .join("TomatoAgent")
            .join("engine");
        Ok::<_, String>((identity, response, data_dir))
    })();
    let (identity, response, data_dir) = match result {
        Ok(value) => value,
        Err(error) => {
            let stopped = engine.stop().is_ok() && engine.identity()?.is_none();
            let report = serde_json::json!({
                "passed": false,
                "error": error,
                "provider_requests": 0,
                "engine_stopped": stopped
            });
            if let Ok(bytes) = serde_json::to_vec_pretty(&report) {
                let _ = std::fs::write(output, bytes);
            }
            return Err(error);
        }
    };
    let stopped = engine.stop().is_ok() && engine.identity()?.is_none();
    let report = serde_json::json!({
        "passed": response["body"]["protocol"] == identity.protocol
            && response["body"]["service_instance_id"] == identity.service_instance_id
            && stopped,
        "protocol": identity.protocol,
        "service_instance_id": identity.service_instance_id,
        "response_status": response["status"],
        "data_dir": data_dir,
        "provider_requests": 0,
        "engine_stopped": stopped
    });
    let bytes = serde_json::to_vec_pretty(&report).map_err(|_| "无法编码验收报告")?;
    std::fs::write(output, bytes).map_err(|_| "无法写入验收报告")?;
    if report["passed"] != true {
        return Err("助手引擎验收失败".into());
    }
    Ok(())
}

pub fn write_task_probe_report(
    app: &tauri::AppHandle,
    engine: &EngineManager,
    chat: &super::chat::ChatRuntime,
    output: &Path,
) -> Result<(), String> {
    if !output.is_absolute() {
        return Err("验收报告路径必须是绝对路径".into());
    }
    let listener = TcpListener::bind("127.0.0.1:0").map_err(|_| "无法启动本地 Provider 探针")?;
    listener.set_nonblocking(true).map_err(|_| "无法配置本地 Provider 探针")?;
    let provider_address = listener.local_addr().map_err(|_| "无法读取本地 Provider 地址")?;
    let provider = std::thread::spawn(move || -> Result<u32, String> {
        let responses = [
            serde_json::json!({"decision":{"protocol_version":1,"type":"ask_user","payload":{
                "question":"请确认验收目标","choices":[],"allow_free_text":true
            }}}).to_string(),
            serde_json::json!({"decision":{"protocol_version":1,"type":"finish","payload":{"result":{
                "answer":"桌面任务闭环通过","citations":[],"artifacts":[],"test_results":[],
                "validation_status":"not_required"
            }}}}).to_string(),
        ];
        for content in responses {
            let deadline = std::time::Instant::now() + Duration::from_secs(35);
            let (mut stream, _) = loop {
                match listener.accept() {
                    Ok(value) => break value,
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock
                        && std::time::Instant::now() < deadline => std::thread::sleep(Duration::from_millis(25)),
                    Err(_) => return Err("本地 Provider 未收到请求".into()),
                }
            };
            stream.set_read_timeout(Some(Duration::from_secs(20))).ok();
            let mut request = Vec::new();
            let mut chunk = [0_u8; 4096];
            let header_end;
            loop {
                let count = stream.read(&mut chunk).map_err(|_| "无法读取本地 Provider 请求")?;
                if count == 0 { return Err("本地 Provider 请求提前结束".into()); }
                request.extend_from_slice(&chunk[..count]);
                if let Some(index) = request.windows(4).position(|item| item == b"\r\n\r\n") {
                    header_end = index + 4;
                    break;
                }
            }
            let headers = String::from_utf8_lossy(&request[..header_end]).to_ascii_lowercase();
            let content_length = headers.lines().find_map(|line| line.strip_prefix("content-length:"))
                .and_then(|value| value.trim().parse::<usize>().ok()).unwrap_or(0);
            while request.len() < header_end + content_length {
                let count = stream.read(&mut chunk).map_err(|_| "无法读取本地 Provider 请求")?;
                if count == 0 { return Err("本地 Provider 请求体不完整".into()); }
                request.extend_from_slice(&chunk[..count]);
            }
            let body: Value = serde_json::from_slice(&request[header_end..header_end + content_length])
                .map_err(|_| "本地 Provider 请求格式无效")?;
            if body["messages"].as_array().is_none() {
                return Err("桌面助手 Provider 契约不匹配".into());
            }
            let response = serde_json::json!({"choices":[{"message":{"content":content}}]}).to_string();
            write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                response.len(), response).map_err(|_| "无法写入本地 Provider 响应")?;
            stream.flush().map_err(|_| "无法发送本地 Provider 响应")?;
        }
        Ok(2)
    });

    let result = (|| -> Result<Value, String> {
        let config = super::chat::ChatConfig {
            base_url: format!("http://{provider_address}/v1"),
            model: "desktop-task-probe".into(),
        };
        engine.start(app, &config, "desktop-task-probe-key")?;
        let session_id = chat.store.create_session("桌面任务验收")?;
        let engine_session = ensure_engine_session(engine, &chat.store, &session_id)?;
        let request_id = uuid::Uuid::new_v4().to_string();
        chat.store.append_message_with_id(&format!("agent-user-{request_id}"), &session_id,
            "user", "请运行本地桌面闭环验收")?;
        let submitted = engine.request("POST", &format!("/api/sessions/{engine_session}/tasks"), Some(
            serde_json::json!({"kind":"assistant","message":"请运行本地桌面闭环验收",
                "client_request_id":request_id,"context_messages":[]})))?;
        let task = submitted.get("body").cloned().ok_or("任务提交没有返回状态")?;
        let run_id = task.get("run_id").and_then(Value::as_str).ok_or("任务标识缺失")?.to_string();
        let deadline = std::time::Instant::now() + Duration::from_secs(30);
        let mut waiting = None;
        while std::time::Instant::now() < deadline {
            let task = task_for_session(engine, &engine_session, &run_id)?;
            if task["status"] == "waiting" { waiting = Some(task); break; }
            if task["status"] == "failed" { return Err("助手任务在澄清前失败".into()); }
            std::thread::sleep(Duration::from_millis(100));
        }
        let waiting = waiting.ok_or("助手任务未进入澄清状态")?;
        let pending = waiting.get("pending_action").cloned().ok_or("澄清动作缺失")?;
        let action = engine.request("POST", &format!("/api/tasks/{run_id}/actions"), Some(
            serde_json::json!({"action_id":pending["action_id"],"action":"provide_input",
                "expected_version":waiting["version"],"client_request_id":uuid::Uuid::new_v4().to_string(),
                "payload":{"message":"按普通聊天会话继续"}})))?;
        if action.pointer("/body/status").and_then(Value::as_str) != Some("queued") {
            return Err("澄清回答没有恢复任务".into());
        }
        let deadline = std::time::Instant::now() + Duration::from_secs(30);
        let completed = loop {
            let task = task_for_session(engine, &engine_session, &run_id)?;
            if task["status"] == "completed" { break task; }
            if matches!(task["status"].as_str(), Some("failed" | "cancelled" | "timed_out")) {
                return Err("助手任务未能完成".into());
            }
            if std::time::Instant::now() >= deadline { return Err("助手任务完成超时".into()); }
            std::thread::sleep(Duration::from_millis(100));
        };
        let answer = completed.pointer("/result/answer").and_then(Value::as_str)
            .ok_or("助手任务结果缺少回答")?;
        chat.store.append_message_with_id(&format!("agent-assistant-{run_id}"), &session_id,
            "assistant", answer)?;
        chat.store.append_message_with_id(&format!("agent-assistant-{run_id}"), &session_id,
            "assistant", answer)?;
        let events = engine.request("GET", &format!("/api/tasks/{run_id}/events?after=0&limit=30"), None)?;
        let history = chat.store.messages(&session_id)?;
        let events_count = events.pointer("/body/events").and_then(Value::as_array).map_or(0, Vec::len);
        let passed = completed["session_id"] == engine_session
            && answer == "桌面任务闭环通过"
            && history.iter().any(|message| message.role == "user" && message.content == "请运行本地桌面闭环验收")
            && history.iter().any(|message| message.role == "assistant" && message.content == answer)
            && events_count >= 2;
        Ok(serde_json::json!({"passed":passed,"desktop_session_id":session_id,
            "engine_session_id":engine_session,"run_id":run_id,"waiting_action":"provide_input",
            "status":completed["status"],"events":events_count,"history_messages":history.len(),
            "answer":answer,"provider_requests":2,"result_message_id":format!("agent-assistant-{run_id}")}))
    })();
    let provider_result = provider.join().map_err(|_| "本地 Provider 探针异常退出".to_string())
        .and_then(|result| result);
    let provider_requests = provider_result.as_ref().copied().unwrap_or(0);
    let stopped = engine.stop().is_ok() && engine.identity()?.is_none();
    let report = match result {
        Ok(mut value) => {
            value["engine_stopped"] = Value::Bool(stopped);
            value["provider_requests"] = Value::from(provider_requests);
            value["passed"] = Value::Bool(value["passed"] == true && stopped
                && provider_requests == 2 && provider_result.is_ok());
            value
        }
        Err(error) => serde_json::json!({"passed":false,"error":error,
            "engine_stopped":stopped,"provider_requests":provider_requests,
            "provider_error":provider_result.err()}),
    };
    let bytes = serde_json::to_vec_pretty(&report).map_err(|_| "无法编码桌面任务验收报告")?;
    std::fs::write(output, bytes).map_err(|_| "无法写入桌面任务验收报告")?;
    if report["passed"] != true { return Err("桌面任务验收失败".into()); }
    Ok(())
}

pub fn write_writing_probe_report(
    app: &tauri::AppHandle,
    engine: &EngineManager,
    chat: &super::chat::ChatRuntime,
    output: &Path,
) -> Result<(), String> {
    if !output.is_absolute() {
        return Err("验收报告路径必须是绝对路径".into());
    }
    if !app.config().identifier.to_ascii_lowercase().contains("probe") {
        return Err("写作验收只允许使用隔离验收应用标识".into());
    }
    let listener = TcpListener::bind("127.0.0.1:0").map_err(|_| "无法启动本地 Provider 探针")?;
    listener.set_nonblocking(true).map_err(|_| "无法配置本地 Provider 探针")?;
    let provider_address = listener.local_addr().map_err(|_| "无法读取本地 Provider 地址")?;
    let provider = std::thread::spawn(move || -> Result<u32, String> {
        for _ in 0..2 {
            let deadline = std::time::Instant::now() + Duration::from_secs(45);
            let (mut stream, _) = loop {
                match listener.accept() {
                    Ok(value) => break value,
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock
                        && std::time::Instant::now() < deadline => std::thread::sleep(Duration::from_millis(25)),
                    Err(_) => return Err("写作任务未调用本地 Provider".into()),
                }
            };
            stream.set_read_timeout(Some(Duration::from_secs(20))).ok();
            let mut request = Vec::new();
            let mut chunk = [0_u8; 4096];
            let header_end;
            loop {
                let count = stream.read(&mut chunk).map_err(|_| "无法读取写作 Provider 请求")?;
                if count == 0 { return Err("写作 Provider 请求提前结束".into()); }
                request.extend_from_slice(&chunk[..count]);
                if let Some(index) = request.windows(4).position(|item| item == b"\r\n\r\n") {
                    header_end = index + 4;
                    break;
                }
            }
            let headers = String::from_utf8_lossy(&request[..header_end]).to_ascii_lowercase();
            let content_length = headers.lines().find_map(|line| line.strip_prefix("content-length:"))
                .and_then(|value| value.trim().parse::<usize>().ok()).unwrap_or(0);
            while request.len() < header_end + content_length {
                let count = stream.read(&mut chunk).map_err(|_| "无法读取写作 Provider 请求体")?;
                if count == 0 { return Err("写作 Provider 请求体不完整".into()); }
                request.extend_from_slice(&chunk[..count]);
            }
            let body: Value = serde_json::from_slice(&request[header_end..header_end + content_length])
                .map_err(|_| "写作 Provider 请求格式无效")?;
            let messages = body["messages"].as_array().ok_or("写作 Provider 缺少 messages")?;
            let user_content = messages.iter().find(|item| item["role"] == "user")
                .and_then(|item| item["content"].as_str()).ok_or("写作 Provider 缺少用户内容")?;
            let payload: Value = serde_json::from_str(user_content)
                .map_err(|_| "写作 Provider 用户内容不是 JSON")?;
            let citation = payload["evidence"].as_array().and_then(|items| items.first())
                .and_then(|item| item["citation_id"].as_str()).ok_or("写作任务没有检索到引用证据")?;
            let system = messages.iter().find(|item| item["role"] == "system")
                .and_then(|item| item["content"].as_str()).unwrap_or("");
            let content = if system.contains("generate a technical writing outline") {
                serde_json::json!({"title":"Rust 桌面生命周期验收","sections":[{
                    "title":"引擎生命周期","points":["引擎由桌面进程按需启动并在退出时关闭"],
                    "citation_ids":[citation]}],"gaps":[]}).to_string()
            } else if system.contains("write a technical draft") {
                serde_json::json!({"title":"Rust 桌面生命周期验收","sections":[{
                    "title":"引擎生命周期","content":"桌面进程按需启动引擎，并在退出时关闭引擎。",
                    "citation_ids":[citation]}]}).to_string()
            } else {
                return Err("写作 Provider 收到未知的生成阶段".into());
            };
            let response = serde_json::json!({"choices":[{"message":{"content":content}}]}).to_string();
            write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                response.len(), response).map_err(|_| "无法写入写作 Provider 响应")?;
            stream.flush().map_err(|_| "无法发送写作 Provider 响应")?;
        }
        Ok(2)
    });

    let result = (|| -> Result<Value, String> {
        let config = super::chat::ChatConfig {
            base_url: format!("http://{provider_address}/v1"),
            model: "desktop-writing-probe".into(),
        };
        engine.start(app, &config, "desktop-writing-probe-key")?;
        let data_dir = app.path().app_data_dir().map_err(|_| "无法定位应用数据目录")?
            .join("TomatoAgent").join("engine");
        let database = data_dir.join("agent.db");
        let connection = rusqlite::Connection::open(&database).map_err(|_| "无法打开隔离验收知识库")?;
        connection.execute("INSERT OR REPLACE INTO documents(document_id,source_path,source_url,title,content_hash,index_status,index_error) VALUES (?1,?2,NULL,?3,?4,'ready',NULL)",
            rusqlite::params!["desktop-writing-probe-doc", "acceptance/desktop-lifecycle.md", "Desktop assistant engine lifecycle", "desktop-writing-probe-v1"])
            .map_err(|_| "无法写入隔离验收文档")?;
        connection.execute("INSERT OR REPLACE INTO chunks(chunk_id,document_id,document_version,heading_path,start_line,end_line,text,token_count) VALUES (?1,?2,?3,?4,?5,?6,?7,?8)",
            rusqlite::params!["desktop-writing-probe-chunk", "desktop-writing-probe-doc", "desktop-writing-probe-v1",
                "Desktop assistant engine lifecycle", 1, 2,
                "Write about the local assistant engine lifecycle in the Rust desktop application. The desktop application starts the local assistant engine on demand and stops it when the application exits.", 40])
            .map_err(|_| "无法写入隔离验收证据")?;
        drop(connection);

        let session_id = chat.store.create_session("桌面写作验收")?;
        let engine_session = ensure_engine_session(engine, &chat.store, &session_id)?;
        let request_id = uuid::Uuid::new_v4().to_string();
        let topic = "Write about the local assistant engine lifecycle in the Rust desktop application";
        chat.store.append_message_with_id(&format!("agent-user-{request_id}"), &session_id, "user", topic)?;
        let submitted = engine.request("POST", &format!("/api/sessions/{engine_session}/tasks"), Some(
            serde_json::json!({"kind":"writing","message":topic,"client_request_id":request_id})))?;
        let run_id = submitted.pointer("/body/run_id").and_then(Value::as_str)
            .ok_or("写作任务没有返回 Run 标识")?.to_string();
        let mut run = wait_for_task(engine, &engine_session, &run_id, "waiting", Duration::from_secs(60))?;
        if run.pointer("/pending_action/reason").and_then(Value::as_str) != Some("confirm_outline") {
            return Err("写作任务未等待大纲确认".into());
        }
        let outline = run.pointer("/result/outline").cloned().ok_or("写作结果缺少大纲")?;
        let action_id = run.pointer("/pending_action/action_id").and_then(Value::as_str)
            .ok_or("大纲确认动作缺失")?;
        engine.request("POST", &format!("/api/tasks/{run_id}/actions"), Some(serde_json::json!({
            "action_id":action_id,"action":"confirm_outline","expected_version":run["version"],
            "client_request_id":uuid::Uuid::new_v4().to_string(),"payload":{"outline":outline}
        })))?;
        run = wait_for_task(engine, &engine_session, &run_id, "waiting", Duration::from_secs(60))?;
        if run.pointer("/pending_action/reason").and_then(Value::as_str) != Some("save_draft") {
            return Err("写作任务未等待草稿保存确认".into());
        }
        let draft = run.pointer("/result/draft").and_then(Value::as_str).unwrap_or("");
        if !draft.contains("桌面进程按需启动引擎") || !draft.contains("desktop-writing-probe-chunk") {
            return Err("写作草稿缺少生成内容或引用".into());
        }
        let action_id = run.pointer("/pending_action/action_id").and_then(Value::as_str)
            .ok_or("草稿保存动作缺失")?;
        engine.request("POST", &format!("/api/tasks/{run_id}/actions"), Some(serde_json::json!({
            "action_id":action_id,"action":"save_draft","expected_version":run["version"],
            "client_request_id":uuid::Uuid::new_v4().to_string(),"payload":{}
        })))?;
        let completed = wait_for_task(engine, &engine_session, &run_id, "completed", Duration::from_secs(60))?;
        let saved_path = completed.pointer("/result/saved_path").and_then(Value::as_str)
            .ok_or("已完成写作任务没有保存路径")?;
        let saved_content = std::fs::read_to_string(saved_path).map_err(|_| "验收草稿文件未落盘")?;
        chat.store.append_message_with_id(&format!("agent-assistant-{run_id}"), &session_id,
            "assistant", &saved_content)?;
        let history = chat.store.messages(&session_id)?;
        let stopped = engine.stop().is_ok() && engine.identity()?.is_none();
        let result_messages = history.iter().filter(|message| message.id == format!("agent-assistant-{run_id}")).count();
        Ok(serde_json::json!({"passed":saved_content == draft && result_messages == 1 && stopped,
            "desktop_session_id":session_id,"engine_session_id":engine_session,"run_id":run_id,
            "status":completed["status"],"citations":["desktop-writing-probe-chunk"],
            "saved_path":saved_path,"saved_bytes":saved_content.len(),"result_message_count":result_messages,
            "history_messages":history.len()}))
    })();
    let provider_result = provider.join().map_err(|_| "本地写作 Provider 探针异常退出".to_string())
        .and_then(|result| result);
    let provider_requests = provider_result.as_ref().copied().unwrap_or(0);
    let stopped = engine.stop().is_ok() && engine.identity()?.is_none();
    let mut report = match result {
        Ok(value) => value,
        Err(error) => serde_json::json!({"passed":false,"error":error}),
    };
    report["engine_stopped"] = Value::Bool(stopped);
    report["provider_requests"] = Value::from(provider_requests);
    report["passed"] = Value::Bool(report["passed"] == true && stopped
        && provider_requests == 2 && provider_result.is_ok());
    std::fs::write(output, serde_json::to_vec_pretty(&report)
        .map_err(|_| "无法编码桌面写作验收报告")?)
        .map_err(|_| "无法写入桌面写作验收报告")?;
    if report["passed"] != true { return Err("桌面写作验收失败".into()); }
    Ok(())
}

fn wait_for_task(
    engine: &EngineManager,
    engine_session: &str,
    run_id: &str,
    expected: &str,
    timeout: Duration,
) -> Result<Value, String> {
    let deadline = std::time::Instant::now() + timeout;
    loop {
        let task = task_for_session(engine, engine_session, run_id)?;
        if task["status"] == expected { return Ok(task); }
        if matches!(task["status"].as_str(), Some("failed" | "cancelled" | "timed_out")) {
            return Err(format!("写作 Run 进入终态 {}：{}", task["status"], task["error"]));
        }
        if std::time::Instant::now() >= deadline {
            let events = engine.request("GET", &format!("/api/tasks/{run_id}/events?after=0&limit=100"), None)
                .ok().and_then(|value| value.pointer("/body/events").cloned()).unwrap_or(Value::Null);
            return Err(format!("Run 等待超时，期望={expected}，当前={}，事件={}",
                serde_json::to_string(&task).unwrap_or_default(),
                serde_json::to_string(&events).unwrap_or_default()));
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

pub fn write_project_isolation_probe_report(
    app: &tauri::AppHandle,
    engine: &EngineManager,
    chat: &super::chat::ChatRuntime,
    output: &Path,
) -> Result<(), String> {
    if !output.is_absolute() {
        return Err("验收报告路径必须是绝对路径".into());
    }
    if !app.config().identifier.to_ascii_lowercase().contains("probe") {
        return Err("目录隔离验收只允许使用隔离验收应用标识".into());
    }
    let result = (|| -> Result<Value, String> {
        let config = super::chat::ChatConfig {
            base_url: "https://desktop-isolation-probe.invalid/v1".into(),
            model: "desktop-isolation-probe".into(),
        };
        engine.start(app, &config, "desktop-isolation-probe-key")?;
        let app_data = app.path().app_data_dir().map_err(|_| "无法定位应用数据目录")?
            .join("TomatoAgent");
        let selected_path = app_data.join("desktop-project-probe");
        std::fs::create_dir_all(&selected_path).map_err(|_| "无法创建隔离项目目录")?;
        let selected_path_text = selected_path.to_string_lossy().into_owned();
        let project = chat.store.create_project(&selected_path_text, Some("隔离项目验收"))?;
        let before = engine.request("GET", "/api/workspaces", None)?;
        let before_items = before.pointer("/body/workspaces").and_then(Value::as_array)
            .ok_or("助手引擎未返回工作区列表")?;
        let project_was_not_authorized = !before_items.iter().any(|item|
            item.pointer("/repo_path").and_then(Value::as_str) == Some(selected_path_text.as_str()));
        let registered = engine.request("POST", "/api/desktop/workspaces", Some(
            serde_json::json!({"path":selected_path_text})))?;
        let workspace = registered.get("body").cloned().ok_or("助手引擎未返回显式目录登记")?;
        let after = engine.request("GET", "/api/workspaces", None)?;
        let after_items = after.pointer("/body/workspaces").and_then(Value::as_array)
            .ok_or("助手引擎未返回登记后工作区列表")?;
        let registered_workspace = after_items.iter().find(|item|
            item.get("workspace_id") == workspace.get("workspace_id"));
        let registered_workspace_visible = registered_workspace.is_some_and(|item|
            item.get("available").and_then(Value::as_bool) == Some(true));
        let registered_workspace_path_matches = registered_workspace
            .and_then(|item| item.pointer("/repo_path").and_then(Value::as_str))
            .is_some_and(|path| {
                let registered_path = workspace.pointer("/repo_path").and_then(Value::as_str);
                let Some(registered_path) = registered_path else { return false; };
                #[cfg(windows)]
                { path.eq_ignore_ascii_case(registered_path) }
                #[cfg(not(windows))]
                { path == registered_path }
            });
        let stopped = engine.stop().is_ok() && engine.identity()?.is_none();
        Ok(serde_json::json!({"passed":project_was_not_authorized && registered_workspace_visible
            && registered_workspace_path_matches && stopped,
            "desktop_project_id":project.id,"project_path":selected_path_text,
            "project_was_not_authorized":project_was_not_authorized,
            "registered_workspace_id":workspace["workspace_id"],
            "registered_workspace_visible":registered_workspace_visible,
            "registered_workspace_path_matches":registered_workspace_path_matches,
            "engine_stopped":stopped}))
    })();
    let mut report = match result {
        Ok(value) => value,
        Err(error) => serde_json::json!({"passed":false,"error":error}),
    };
    let stopped = engine.stop().is_ok() && engine.identity()?.is_none();
    report["engine_stopped"] = Value::Bool(stopped);
    report["passed"] = Value::Bool(report["passed"] == true && stopped);
    std::fs::write(output, serde_json::to_vec_pretty(&report)
        .map_err(|_| "无法编码目录隔离验收报告")?)
        .map_err(|_| "无法写入目录隔离验收报告")?;
    if report["passed"] != true { return Err("桌面目录隔离验收失败".into()); }
    Ok(())
}

pub fn write_cancel_probe_report(
    app: &tauri::AppHandle,
    engine: &EngineManager,
    chat: &super::chat::ChatRuntime,
    output: &Path,
) -> Result<(), String> {
    if !output.is_absolute() {
        return Err("验收报告路径必须是绝对路径".into());
    }
    if !app.config().identifier.to_ascii_lowercase().contains("probe") {
        return Err("取消验收只允许使用隔离验收应用标识".into());
    }
    let listener = TcpListener::bind("127.0.0.1:0").map_err(|_| "无法启动本地 Provider 探针")?;
    listener.set_nonblocking(true).map_err(|_| "无法配置本地 Provider 探针")?;
    let provider_address = listener.local_addr().map_err(|_| "无法读取本地 Provider 地址")?;
    let (received_tx, received_rx) = std::sync::mpsc::channel();
    let (release_tx, release_rx) = std::sync::mpsc::channel();
    let provider = std::thread::spawn(move || -> Result<(), String> {
        let deadline = std::time::Instant::now() + Duration::from_secs(45);
        let (mut stream, _) = loop {
            match listener.accept() {
                Ok(value) => break value,
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock
                    && std::time::Instant::now() < deadline => std::thread::sleep(Duration::from_millis(25)),
                Err(_) => return Err("取消任务未调用本地 Provider".into()),
            }
        };
        stream.set_read_timeout(Some(Duration::from_secs(20))).ok();
        let mut request = Vec::new();
        let mut chunk = [0_u8; 4096];
        let header_end;
        loop {
            let count = stream.read(&mut chunk).map_err(|_| "无法读取取消 Provider 请求")?;
            if count == 0 { return Err("取消 Provider 请求提前结束".into()); }
            request.extend_from_slice(&chunk[..count]);
            if let Some(index) = request.windows(4).position(|item| item == b"\r\n\r\n") {
                header_end = index + 4;
                break;
            }
        }
        let headers = String::from_utf8_lossy(&request[..header_end]).to_ascii_lowercase();
        let content_length = headers.lines().find_map(|line| line.strip_prefix("content-length:"))
            .and_then(|value| value.trim().parse::<usize>().ok()).unwrap_or(0);
        while request.len() < header_end + content_length {
            let count = stream.read(&mut chunk).map_err(|_| "无法读取取消 Provider 请求体")?;
            if count == 0 { return Err("取消 Provider 请求体不完整".into()); }
            request.extend_from_slice(&chunk[..count]);
        }
        let body: Value = serde_json::from_slice(&request[header_end..header_end + content_length])
            .map_err(|_| "取消 Provider 请求格式无效")?;
        if body["messages"].as_array().is_none() {
            return Err("取消 Provider 契约不匹配".into());
        }
        received_tx.send(()).map_err(|_| "取消探针已退出")?;
        release_rx.recv_timeout(Duration::from_secs(30)).map_err(|_| "取消探针未释放 Provider")?;
        let content = serde_json::json!({"decision":{"protocol_version":1,"type":"finish",
            "payload":{"result":{"answer":"不应写回的取消结果","citations":[],"artifacts":[],
                "test_results":[],"validation_status":"not_required"}}}}).to_string();
        let response = serde_json::json!({"choices":[{"message":{"content":content}}]}).to_string();
        write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            response.len(), response).map_err(|_| "无法写入取消 Provider 响应")?;
        stream.flush().map_err(|_| "无法发送取消 Provider 响应")?;
        Ok(())
    });

    let result = (|| -> Result<Value, String> {
        let config = super::chat::ChatConfig {
            base_url: format!("http://{provider_address}/v1"),
            model: "desktop-cancel-probe".into(),
        };
        engine.start(app, &config, "desktop-cancel-probe-key")?;
        let session_id = chat.store.create_session("桌面取消验收")?;
        let engine_session = ensure_engine_session(engine, &chat.store, &session_id)?;
        let request_id = uuid::Uuid::new_v4().to_string();
        let message = "启动一项可取消的桌面助手任务";
        chat.store.append_message_with_id(&format!("agent-user-{request_id}"), &session_id,
            "user", message)?;
        let submitted = engine.request("POST", &format!("/api/sessions/{engine_session}/tasks"), Some(
            serde_json::json!({"kind":"assistant","message":message,"client_request_id":request_id,
                "context_messages":[]})))?;
        let run_id = submitted.pointer("/body/run_id").and_then(Value::as_str)
            .ok_or("取消任务没有返回 Run 标识")?.to_string();
        received_rx.recv_timeout(Duration::from_secs(45)).map_err(|_| "任务未进入 Provider 等待阶段")?;
        let running = task_for_session(engine, &engine_session, &run_id)?;
        if running["status"] != "running" {
            return Err(format!("取消前 Run 状态不是 running：{}", running["status"]));
        }
        let cancelled = engine.request("POST", &format!("/api/tasks/{run_id}/actions"), Some(
            serde_json::json!({"action_id":"cancel","action":"cancel","expected_version":running["version"],
                "client_request_id":uuid::Uuid::new_v4().to_string(),"payload":{}})))?;
        release_tx.send(()).map_err(|_| "无法释放本地 Provider")?;
        let final_run = wait_for_task(engine, &engine_session, &run_id, "cancelled", Duration::from_secs(20))?;
        let history = chat.store.messages(&session_id)?;
        let result_count = history.iter().filter(|item| item.id == format!("agent-assistant-{run_id}")).count();
        Ok(serde_json::json!({"passed":cancelled.pointer("/body/status").and_then(Value::as_str) == Some("cancelled")
            && final_run["status"] == "cancelled" && result_count == 0,
            "desktop_session_id":session_id,"engine_session_id":engine_session,"run_id":run_id,
            "status":final_run["status"],"provider_released_after_cancel":true,
            "result_message_count":result_count,"history_messages":history.len()}))
    })();
    let provider_result = provider.join().map_err(|_| "本地取消 Provider 探针异常退出".to_string())
        .and_then(|result| result);
    let stopped = engine.stop().is_ok() && engine.identity()?.is_none();
    let mut report = match result {
        Ok(value) => value,
        Err(error) => serde_json::json!({"passed":false,"error":error}),
    };
    report["engine_stopped"] = Value::Bool(stopped);
    report["provider_completed"] = Value::Bool(provider_result.is_ok());
    report["passed"] = Value::Bool(report["passed"] == true && stopped && provider_result.is_ok());
    std::fs::write(output, serde_json::to_vec_pretty(&report)
        .map_err(|_| "无法编码取消验收报告")?)
        .map_err(|_| "无法写入取消验收报告")?;
    if report["passed"] != true { return Err("桌面取消验收失败".into()); }
    Ok(())
}

pub fn write_task_recovery_probe_report(
    app: &tauri::AppHandle,
    engine: &EngineManager,
    chat: &super::chat::ChatRuntime,
    output: &Path,
) -> Result<(), String> {
    if !output.is_absolute() {
        return Err("验收报告路径必须是绝对路径".into());
    }
    let original: Value = serde_json::from_slice(
        &std::fs::read(output).map_err(|_| "缺少首次桌面任务探针报告")?,
    ).map_err(|_| "首次桌面任务探针报告无效")?;
    let result = (|| -> Result<Value, String> {
        if original["passed"] != true {
            return Err("首次桌面任务探针未通过".into());
        }
        let session_id = original["desktop_session_id"].as_str().ok_or("桌面会话标识缺失")?;
        let engine_session_id = original["engine_session_id"].as_str().ok_or("引擎会话标识缺失")?;
        let run_id = original["run_id"].as_str().ok_or("任务标识缺失")?;
        let answer = original["answer"].as_str().ok_or("任务结果缺失")?;
        let config = super::chat::ChatConfig {
            base_url: "https://desktop-recovery-probe.invalid/v1".into(),
            model: "desktop-recovery-probe".into(),
        };
        engine.start(app, &config, "desktop-recovery-probe-key")?;
        let mapped_session = chat.store.engine_session(session_id)?
            .ok_or_else(|| "桌面会话映射未能跨进程恢复".to_string())?;
        let task = task_for_session(engine, &mapped_session, run_id)?;
        if mapped_session != engine_session_id || task["status"] != "completed"
            || task.pointer("/result/answer").and_then(Value::as_str) != Some(answer) {
            return Err("引擎重启后任务状态或会话映射不一致".into());
        }
        chat.store.append_message_with_id(&format!("agent-assistant-{run_id}"), session_id,
            "assistant", answer)?;
        let history = chat.store.messages(session_id)?;
        let result_messages = history.iter().filter(|message|
            message.id == format!("agent-assistant-{run_id}")).count();
        let events = engine.request("GET", &format!("/api/tasks/{run_id}/events?after=0&limit=30"), None)?;
        let events_count = events.pointer("/body/events").and_then(Value::as_array).map_or(0, Vec::len);
        Ok(serde_json::json!({
            "passed": result_messages == 1 && events_count >= 2,
            "desktop_session_id": session_id,
            "engine_session_id": mapped_session,
            "run_id": run_id,
            "status": task["status"],
            "history_messages": history.len(),
            "result_message_count": result_messages,
            "events": events_count,
        }))
    })();
    let stopped = engine.stop().is_ok() && engine.identity()?.is_none();
    let mut report = match result {
        Ok(value) => value,
        Err(error) => serde_json::json!({"passed":false,"error":error}),
    };
    report["engine_stopped"] = Value::Bool(stopped);
    report["passed"] = Value::Bool(report["passed"] == true && stopped);
    std::fs::write(output, serde_json::to_vec_pretty(&report)
        .map_err(|_| "无法编码桌面恢复验收报告")?)
        .map_err(|_| "无法写入桌面恢复验收报告")?;
    if report["passed"] != true { return Err("桌面恢复验收失败".into()); }
    Ok(())
}

impl EngineManager {
    pub fn new() -> Result<Self, String> {
        let client = Client::builder()
            .no_proxy()
            .timeout(Duration::from_secs(30))
            .build()
            .map_err(|_| "无法初始化助手引擎连接".to_string())?;
        Ok(Self {
            process: Mutex::new(None),
            client,
        })
    }

    fn executable(app: &tauri::AppHandle) -> Result<(PathBuf, bool), String> {
        if let Some(path) = std::env::var_os("TOMATO_AGENT_ENGINE") {
            let path = PathBuf::from(path);
            if path.is_file() {
                return Ok((path, true));
            }
            return Err("指定的助手引擎不存在".into());
        }
        if let Ok(path) = app.path().resolve(
            "engine/tomato-agent-engine.exe",
            tauri::path::BaseDirectory::Resource,
        ) {
            if path.is_file() {
                return Ok((path, true));
            }
        }
        #[cfg(windows)]
        {
            Ok((PathBuf::from("python.exe"), false))
        }
        #[cfg(not(windows))]
        {
            Ok((PathBuf::from("python3"), false))
        }
    }

    pub fn start(
        &self,
        app: &tauri::AppHandle,
        config: &super::chat::ChatConfig,
        key: &str,
    ) -> Result<EngineIdentity, String> {
        let memory_enabled = self.process.lock().map_err(|_| "助手引擎状态不可用")?
            .as_ref().map(|process| process.memory_enabled).unwrap_or(false);
        self.start_with_memory(app, config, key, memory_enabled)
    }

    pub fn start_with_memory(
        &self,
        app: &tauri::AppHandle,
        config: &super::chat::ChatConfig,
        key: &str,
        memory_enabled: bool,
    ) -> Result<EngineIdentity, String> {
        let mut state = self.process.lock().map_err(|_| "助手引擎状态不可用")?;
        if let Some(process) = state.as_mut() {
            if process
                .child
                .try_wait()
                .map_err(|_| "无法读取助手引擎状态")?
                .is_none()
            {
                if process.memory_enabled != memory_enabled {
                    return Err("助手引擎记忆设置需要重启引擎".into());
                }
                return Ok(process.identity.clone());
            }
            *state = None;
        }
        let (executable, packaged) = Self::executable(app)?;
        let app_data = app
            .path()
            .app_data_dir()
            .map_err(|_| "无法定位应用数据目录")?
            .join("TomatoAgent")
            .join("engine");
        std::fs::create_dir_all(&app_data).map_err(|_| "无法创建助手数据目录")?;
        let local_app_data = std::env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .filter(|path| path.is_absolute())
            .ok_or("无法定位 Windows 本地应用数据目录")?;
        let draft_directory = local_draft_directory(&local_app_data, &app.config().identifier);
        let resource = app
            .path()
            .resource_dir()
            .map_err(|_| "无法定位应用资源目录")?;
        let token = uuid::Uuid::new_v4().simple().to_string().repeat(2);
        let owner = uuid::Uuid::new_v4().simple().to_string();
        let client_id = uuid::Uuid::new_v4().to_string();
        let mut command = Command::new(executable);
        if !packaged {
            let backend = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../backend");
            command
                .args(["-m", "app.desktop.bootstrap", "--serve"])
                .current_dir(backend.clone())
                .env("PYTHONPATH", backend);
        } else {
            command.arg("--serve");
        }
        let mut child = command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|_| "无法启动助手引擎；请检查安装资源")?;
        let input = serde_json::json!({
            "token": token, "owner": owner, "environment_id": "windows-local",
            "data_dir": app_data, "draft_dir": draft_directory, "resource_dir": resource,
            "llm_api_key": key, "llm_base_url": config.base_url, "llm_model": config.model,
            "memory_enabled": memory_enabled,
            "memory_auto_mode": if memory_enabled { "automatic" } else { "off" }
        });
        let write_result = child
            .stdin
            .take()
            .ok_or("助手引擎启动管道不可用")?
            .write_all(format!("{}\n", input).as_bytes());
        if write_result.is_err() {
            let _ = child.kill();
            let _ = child.wait();
            return Err("无法向助手引擎发送启动配置".into());
        }
        let stdout = child.stdout.take().ok_or("助手引擎启动响应不可用")?;
        let (sender, receiver) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            let mut line = String::new();
            let result = BufReader::new(stdout).read_line(&mut line).map(|_| line);
            let _ = sender.send(result);
        });
        let line = match receiver.recv_timeout(Duration::from_secs(25)) {
            Ok(Ok(line)) => line,
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return Err("助手引擎启动超时".into());
            }
        };
        let mut identity: EngineIdentity = match serde_json::from_str::<EngineIdentity>(&line) {
            Ok(value) if value.protocol == 1 && value.port != 0 => value,
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return Err("助手引擎返回了无效身份".into());
            }
        };
        identity.owned = true;
        let endpoint = format!("http://127.0.0.1:{}/api/desktop/connection", identity.port);
        let response = self
            .client
            .get(endpoint)
            .bearer_auth(&token)
            .header("X-Tomato-Instance", &identity.service_instance_id)
            .send()
            .and_then(|response| response.error_for_status())
            .and_then(|response| response.json::<Value>());
        match response {
            Ok(value)
                if value["protocol"] == 1
                    && value["service_instance_id"] == identity.service_instance_id => {}
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return Err("助手引擎身份校验失败".into());
            }
        }
        let attach = self.request_for(
            identity.port,
            &token,
            &identity.service_instance_id,
            &client_id,
            None,
            "POST",
            "/api/desktop/attach",
            None,
        );
        if attach.is_err() {
            let _ = child.kill();
            let _ = child.wait();
            return Err("无法连接助手引擎".into());
        }
        *state = Some(Process {
            child,
            identity: identity.clone(),
            token,
            owner,
            client: client_id,
            memory_enabled,
        });
        Ok(identity)
    }

    fn request_for(
        &self,
        port: u16,
        token: &str,
        instance: &str,
        client: &str,
        owner: Option<&str>,
        method: &str,
        path: &str,
        body: Option<Value>,
    ) -> Result<Value, String> {
        if !path.starts_with("/api/") || path.contains("..") || path.contains('#') {
            return Err("助手请求路径无效".into());
        }
        let method =
            reqwest::Method::from_bytes(method.as_bytes()).map_err(|_| "助手请求方法无效")?;
        let mut request = self
            .client
            .request(method, format!("http://127.0.0.1:{port}{path}"))
            .bearer_auth(token)
            .header("X-Tomato-Instance", instance)
            .header("X-Tomato-Client", client);
        if path == "/api/memory/recall" || path.starts_with("/api/memory/recall?") {
            request = request.timeout(Duration::from_millis(1500));
        }
        if let Some(owner) = owner {
            request = request.header("X-Tomato-Owner", owner);
        }
        if let Some(body) = body {
            request = request.json(&body);
        }
        let response = request.send().map_err(|_| "助手引擎请求失败".to_string())?;
        let status = response.status();
        let payload = response.json::<Value>().unwrap_or(Value::Null);
        if !status.is_success() {
            return Err(payload
                .pointer("/detail/code")
                .and_then(Value::as_str)
                .unwrap_or("助手引擎请求失败")
                .to_string());
        }
        Ok(serde_json::json!({"status":status.as_u16(),"body":payload}))
    }

    pub fn request(&self, method: &str, path: &str, body: Option<Value>) -> Result<Value, String> {
        let state = self.process.lock().map_err(|_| "助手引擎状态不可用")?;
        let process = state.as_ref().ok_or("助手引擎尚未启动")?;
        self.request_for(
            process.identity.port,
            &process.token,
            &process.identity.service_instance_id,
            &process.client,
            None,
            method,
            path,
            body,
        )
    }

    pub fn memory_request(
        &self,
        store: &super::chat::ChatStore,
        method: &str,
        path: &str,
        body: Option<Value>,
    ) -> Result<Value, String> {
        let is_memory_read = path == "/api/memory/recall" || path.starts_with("/api/memory/recall?")
            || path == "/api/layered-memory/context"
            || (method.eq_ignore_ascii_case("GET")
                && (path == "/api/layered-memory/profile" || path == "/api/layered-memory/episodes"))
            || (method.eq_ignore_ascii_case("GET") && path.starts_with("/api/memory/items/"));
        if is_memory_read {
            store.ensure_memory_reads_allowed()?;
        }
        self.request(method, path, body)
    }

    pub fn identity(&self) -> Result<Option<EngineIdentity>, String> {
        Ok(self
            .process
            .lock()
            .map_err(|_| "助手引擎状态不可用")?
            .as_ref()
            .map(|process| process.identity.clone()))
    }

    pub fn stop(&self) -> Result<(), String> {
        let mut state = self.process.lock().map_err(|_| "助手引擎状态不可用")?;
        let Some(mut process) = state.take() else {
            return Ok(());
        };
        let close = self.request_for(
            process.identity.port,
            &process.token,
            &process.identity.service_instance_id,
            &process.client,
            Some(&process.owner),
            "POST",
            "/api/desktop/close",
            None,
        );
        let shutdown = close
            .ok()
            .and_then(|response| response.pointer("/body/shutdown").and_then(Value::as_bool))
            .unwrap_or(false);
        if shutdown {
            let deadline = std::time::Instant::now() + Duration::from_secs(5);
            loop {
                if process.child.try_wait().ok().flatten().is_some() {
                    return Ok(());
                }
                if std::time::Instant::now() >= deadline {
                    let _ = process.child.kill();
                    break;
                }
                std::thread::sleep(Duration::from_millis(100));
            }
        }
        Ok(())
    }
}

pub fn flush_memory_source_deletions(
    engine: &EngineManager,
    store: &super::chat::ChatStore,
) -> usize {
    let Ok(pending) = store.pending_memory_source_deletions(100) else {
        return usize::MAX;
    };
    for deletion in pending {
        let response = engine.request(
            "POST",
            &format!("/api/memory/sources/delete?request_id={}", deletion.request_id),
            Some(serde_json::json!({
                "source_system": deletion.source_system,
                "source_installation_id": deletion.source_installation_id,
                "session_id": deletion.session_id,
                "message_id": deletion.message_id,
                "source_revision": deletion.source_revision
            })),
        );
        if response.is_err() || store.acknowledge_memory_source_deletion(&deletion.request_id).is_err() {
            break;
        }
    }
    store
        .pending_memory_source_deletions(500)
        .map(|items| items.len())
        .unwrap_or(usize::MAX)
}

pub fn flush_memory_source_ingestions(
    engine: &EngineManager,
    store: &super::chat::ChatStore,
) -> usize {
    let Ok(pending) = store.pending_memory_source_ingestions(100) else {
        return usize::MAX;
    };
    for (request_id, payload) in pending {
        let response = engine.request(
            "POST",
            &format!("/api/memory/sources/ingest?request_id={request_id}"),
            Some(payload),
        );
        if response.is_err() || store.acknowledge_memory_source_ingestion(&request_id).is_err() {
            break;
        }
    }
    store.pending_memory_source_ingestions(500)
        .map(|items| items.len())
        .unwrap_or(usize::MAX)
}

pub fn flush_memory_mutations(engine: &EngineManager, store: &super::chat::ChatStore) -> usize {
    let mut pending = 0usize;
    if let Ok(items) = store.pending_memory_item_forgets() {
        for (item_id, request_id, expected_version) in items {
            let Ok(item_id) = uuid::Uuid::parse_str(&item_id) else { pending += 1; continue; };
            let response = engine.memory_request(store, "POST",
                &format!("/api/memory/items/{item_id}/forget?request_id={request_id}"),
                Some(serde_json::json!({"expected_version": expected_version})));
            if response.is_ok() { let _ = store.acknowledge_memory_item_forget(&request_id); }
            else { pending += 1; }
        }
    }
    if let Ok(Some((scope_id, request_id, expected_generation))) = store.pending_memory_clear() {
        let response = engine.memory_request(store, "POST",
            &format!("/api/memory/clear?request_id={request_id}"),
            Some(serde_json::json!({"scope":"personal","scope_id":scope_id,
                "expected_generation":expected_generation})));
        if response.is_ok() { let _ = store.acknowledge_memory_clear(&request_id); }
        else { pending += 1; }
    }
    pending
}

fn memory_ingestion_allowed(store: &super::chat::ChatStore) -> Result<bool, String> {
    Ok(store.memory_enabled()? && store.pending_memory_mutation_count()? == 0)
}

fn checked_memory_id(value: &str) -> Result<String, String> {
    uuid::Uuid::parse_str(value).map(|id| id.to_string()).map_err(|_| "记忆标识无效".into())
}

fn ensure_memory_engine(app: &tauri::AppHandle, engine: &EngineManager,
                        chat: &super::chat::ChatRuntime) -> Result<(), String> {
    start_for_chat(app, engine, chat)
}

#[tauri::command]
pub async fn desktop_memory_items(app: tauri::AppHandle,
    engine: tauri::State<'_, std::sync::Arc<EngineManager>>,
    chat: tauri::State<'_, super::chat::ChatRuntime>, offset: Option<u32>) -> Result<Value, String> {
    let engine = engine.inner().clone(); let chat = chat.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        ensure_memory_engine(&app, &engine, &chat)?;
        let offset = offset.unwrap_or(0).min(100_000);
        engine.memory_request(&chat.store, "GET",
            &format!("/api/memory/items?scope=personal&scope_id=windows-local&limit=100&offset={offset}"), None)
    }).await.map_err(|_| "读取记忆列表失败".to_string())?
}

#[tauri::command]
pub async fn desktop_memory_status(app: tauri::AppHandle,
    engine: tauri::State<'_, std::sync::Arc<EngineManager>>,
    chat: tauri::State<'_, super::chat::ChatRuntime>) -> Result<Value, String> {
    let engine = engine.inner().clone(); let chat = chat.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        ensure_memory_engine(&app, &engine, &chat)?;
        engine.memory_request(&chat.store, "GET", "/api/memory/status", None)
    }).await.map_err(|_| "读取记忆状态失败".to_string())?
}

#[tauri::command]
pub async fn desktop_memory_export(app: tauri::AppHandle,
    engine: tauri::State<'_, std::sync::Arc<EngineManager>>,
    chat: tauri::State<'_, super::chat::ChatRuntime>) -> Result<Value, String> {
    let engine = engine.inner().clone(); let chat = chat.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        ensure_memory_engine(&app, &engine, &chat)?;
        engine.memory_request(&chat.store, "GET", "/api/memory/export", None)
    }).await.map_err(|_| "导出记忆失败".to_string())?
}

#[tauri::command]
pub async fn desktop_chat_memory_contexts(app: tauri::AppHandle,
    engine: tauri::State<'_, std::sync::Arc<EngineManager>>,
    chat: tauri::State<'_, super::chat::ChatRuntime>, session_id: String) -> Result<Value, String> {
    let engine = engine.inner().clone(); let chat = chat.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let contexts = chat.store.memory_chat_contexts(&session_id)?;
        if contexts.is_empty() { return Ok(serde_json::json!({"contexts":[]})); }
        ensure_memory_engine(&app, &engine, &chat)?;
        let mut rendered = Vec::with_capacity(contexts.len());
        for context in contexts {
            let mut items = Vec::with_capacity(context.items.len());
            let mut unavailable_count = 0usize;
            for reference in &context.items {
                let detail = engine.memory_request(&chat.store, "GET",
                    &format!("/api/memory/items/{}/versions/{}", reference.item_id, reference.version), None);
                match detail {
                    Ok(value) => {
                        if let Some(item) = value.get("body") { items.push(item.clone()); }
                        else { unavailable_count += 1; }
                    }
                    Err(error) if error == "memory_version_not_found" => unavailable_count += 1,
                    Err(error) => return Err(error),
                }
            }
            rendered.push(serde_json::json!({
                "assistant_message_id":context.assistant_message_id,
                "generation":context.generation,
                "retrieval_mode":context.retrieval_mode,
                "items":items,
                "unavailable_count":unavailable_count,
            }));
        }
        Ok(serde_json::json!({"contexts":rendered}))
    }).await.map_err(|_| "读取本轮记忆来源失败".to_string())?
}

#[tauri::command]
pub async fn desktop_memory_candidates(app: tauri::AppHandle,
    engine: tauri::State<'_, std::sync::Arc<EngineManager>>,
    chat: tauri::State<'_, super::chat::ChatRuntime>) -> Result<Value, String> {
    let engine = engine.inner().clone(); let chat = chat.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        ensure_memory_engine(&app, &engine, &chat)?;
        engine.memory_request(&chat.store, "GET", "/api/memory/candidates", None)
    }).await.map_err(|_| "读取记忆候选失败".to_string())?
}

#[tauri::command]
pub async fn desktop_memory_item(app: tauri::AppHandle,
    engine: tauri::State<'_, std::sync::Arc<EngineManager>>,
    chat: tauri::State<'_, super::chat::ChatRuntime>, input: MemoryItemIdInput) -> Result<Value, String> {
    let item_id = checked_memory_id(&input.item_id)?;
    let engine = engine.inner().clone(); let chat = chat.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        ensure_memory_engine(&app, &engine, &chat)?;
        engine.memory_request(&chat.store, "GET", &format!("/api/memory/items/{item_id}"), None)
    }).await.map_err(|_| "读取记忆详情失败".to_string())?
}

#[tauri::command]
pub async fn desktop_memory_correct(app: tauri::AppHandle,
    engine: tauri::State<'_, std::sync::Arc<EngineManager>>,
    chat: tauri::State<'_, super::chat::ChatRuntime>, input: MemoryCorrectInput) -> Result<Value, String> {
    if input.content.trim().is_empty() || input.content.chars().count() > 4000 || input.expected_version == 0 {
        return Err("记忆内容或版本无效".into());
    }
    let item_id = checked_memory_id(&input.item_id)?;
    let engine = engine.inner().clone(); let chat = chat.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        ensure_memory_engine(&app, &engine, &chat)?;
        engine.memory_request(&chat.store, "PATCH",
            &format!("/api/memory/items/{item_id}?request_id={}", uuid::Uuid::new_v4()),
            Some(serde_json::json!({"content":input.content.trim(),"expected_version":input.expected_version,
                "reason":"user_correction"})))
    }).await.map_err(|_| "纠正记忆失败".to_string())?
}

#[tauri::command]
pub async fn desktop_memory_forget(app: tauri::AppHandle,
    engine: tauri::State<'_, std::sync::Arc<EngineManager>>,
    chat: tauri::State<'_, super::chat::ChatRuntime>, input: MemoryItemIdInput,
    expected_version: u32) -> Result<Value, String> {
    let item_id = checked_memory_id(&input.item_id)?;
    let chat = chat.inner().clone();
    {
        let generation = chat.generation.lock().map_err(|_| "聊天状态不可用")?;
        if generation.is_some() {
            return Err("请等待当前回复完成后再忘记记忆".into());
        }
        chat.store.enqueue_memory_item_forget(&item_id, i64::from(expected_version))?;
    }
    let engine = engine.inner().clone();
    let result = tauri::async_runtime::spawn_blocking(move || {
        if ensure_memory_engine(&app, &engine, &chat).is_err() {
            return Ok::<_, String>(serde_json::json!({"item_id":item_id,"status":"pending_sync"}));
        }
        let pending = flush_memory_mutations(&engine, &chat.store);
        Ok::<_, String>(serde_json::json!({"item_id":item_id,
            "status":if pending == 0 {"forgotten"} else {"pending_sync"}}))
    }).await.map_err(|_| "忘记记忆失败".to_string())??;
    Ok(result)
}

#[tauri::command]
pub async fn desktop_memory_clear(app: tauri::AppHandle,
    engine: tauri::State<'_, std::sync::Arc<EngineManager>>,
    chat: tauri::State<'_, super::chat::ChatRuntime>, input: MemoryGenerationInput) -> Result<Value, String> {
    let engine = engine.inner().clone(); let chat = chat.inner().clone();
    let _request_id = {
        let generation = chat.generation.lock().map_err(|_| "聊天状态不可用")?;
        if generation.is_some() {
            return Err("请等待当前回复完成后再清空记忆".into());
        }
        chat.store.enqueue_memory_clear("windows-local", input.expected_generation as i64)?
    };
    tauri::async_runtime::spawn_blocking(move || {
        if ensure_memory_engine(&app, &engine, &chat).is_err() {
            return Ok::<_, String>(serde_json::json!({"status":"pending_sync"}));
        }
        let pending = flush_memory_mutations(&engine, &chat.store);
        Ok(serde_json::json!({"status":if pending == 0 {"cleared"} else {"pending_sync"}}))
    }).await.map_err(|_| "清空记忆失败".to_string())?
}

#[tauri::command]
pub async fn desktop_memory_candidate_decision(app: tauri::AppHandle,
    engine: tauri::State<'_, std::sync::Arc<EngineManager>>,
    chat: tauri::State<'_, super::chat::ChatRuntime>, input: MemoryCandidateIdInput,
    action: String) -> Result<Value, String> {
    if !matches!(action.as_str(), "accept" | "reject") { return Err("候选操作无效".into()); }
    let candidate_id = checked_memory_id(&input.candidate_id)?;
    let engine = engine.inner().clone(); let chat = chat.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        ensure_memory_engine(&app, &engine, &chat)?;
        engine.memory_request(&chat.store, "POST",
            &format!("/api/memory/candidates/{candidate_id}/decision"),
            Some(serde_json::json!({"action":action})))
    }).await.map_err(|_| "处理记忆候选失败".to_string())?
}

#[tauri::command]
pub fn desktop_memory_sync_status(
    chat: tauri::State<'_, super::chat::ChatRuntime>,
) -> Result<serde_json::Value, String> {
    Ok(serde_json::json!({
        "enabled": chat.store.memory_enabled()?,
        "pending_deletions": chat.store.has_pending_memory_source_deletions()?,
        "pending_count": chat.store.pending_memory_source_deletions(500)?.len(),
        "pending_ingestions": chat.store.pending_memory_source_ingestions(500)?.len(),
        "pending_mutations": chat.store.pending_memory_mutation_count()?
    }))
}

#[tauri::command]
pub fn desktop_memory_set_enabled(
    engine: tauri::State<'_, std::sync::Arc<EngineManager>>,
    chat: tauri::State<'_, super::chat::ChatRuntime>,
    enabled: bool,
) -> Result<serde_json::Value, String> {
    if chat.generation.lock().map_err(|_| "聊天状态不可用")?.is_some() {
        return Err("请等待当前回复完成后再修改记忆设置".into());
    }
    let current = chat.store.memory_enabled()?;
    if current != enabled {
        if engine.identity()?.is_some() {
            let status = engine.request("GET", "/api/desktop/connection", None)?;
            let active_tasks = status.pointer("/body/active_tasks").and_then(Value::as_u64).unwrap_or(0);
            if active_tasks > 0 {
                return Err("助手任务运行期间不能切换记忆设置".into());
            }
        }
        engine.stop()?;
        chat.store.set_memory_enabled(enabled)?;
    }
    Ok(serde_json::json!({"enabled": enabled}))
}

pub(crate) fn start_for_chat(
    app: &tauri::AppHandle,
    engine: &EngineManager,
    chat: &super::chat::ChatRuntime,
) -> Result<(), String> {
    let config = chat.config.lock().map_err(|_| "聊天配置不可用")?.clone()
        .ok_or("请先配置模型")?;
    let key = chat.credentials.get()?.filter(|value| !value.is_empty())
        .ok_or("请先配置 API Key")?;
    engine.start_with_memory(app, &config, &key, chat.store.memory_enabled()?)?;
    let _ = flush_memory_source_deletions(engine, &chat.store);
    let _ = flush_memory_mutations(engine, &chat.store);
    if memory_ingestion_allowed(&chat.store)? {
        let _ = flush_memory_source_ingestions(engine, &chat.store);
    }
    Ok(())
}

fn ensure_engine_session(
    engine: &EngineManager,
    store: &super::chat::ChatStore,
    desktop_session_id: &str,
) -> Result<String, String> {
    if let Some(session_id) = store.engine_session(desktop_session_id)? {
        return Ok(session_id);
    }
    let session_id = (|| {
        let listing = engine.request("GET", "/api/sessions?limit=200&offset=0", None)?;
        if let Some(existing) = listing.pointer("/body/sessions").and_then(Value::as_array)
            .and_then(|items| items.iter().find(|item|
                item.pointer("/metadata/desktop_session_id").and_then(Value::as_str)
                    == Some(desktop_session_id)))
            .and_then(|item| item.get("id")).and_then(Value::as_str) {
            return Ok::<String, String>(existing.to_string());
        }
        let response = engine.request("POST", "/api/sessions", Some(serde_json::json!({
            "metadata": {"title": "桌面助手会话", "desktop_session_id": desktop_session_id}
        })))?;
        response.pointer("/body/session_id").and_then(Value::as_str)
            .map(str::to_owned).ok_or_else(|| "助手引擎未返回会话标识".into())
    })()?;
    store.bind_engine_session(desktop_session_id, &session_id)?;
    Ok(session_id)
}

impl Drop for EngineManager {
    fn drop(&mut self) {
        let _ = self.stop();
    }
}

#[tauri::command]
pub async fn desktop_agent_start(
    app: tauri::AppHandle,
    engine: tauri::State<'_, std::sync::Arc<EngineManager>>,
    chat: tauri::State<'_, super::chat::ChatRuntime>,
) -> Result<EngineIdentity, String> {
    let engine = engine.inner().clone();
    let chat = chat.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let config = chat
            .config
            .lock()
            .map_err(|_| "聊天配置不可用")?
            .clone()
            .ok_or("请先配置模型")?;
        let key = chat
            .credentials
            .get()?
            .filter(|value| !value.is_empty())
            .ok_or("请先配置 API Key")?;
        let identity = engine.start_with_memory(&app, &config, &key, chat.store.memory_enabled()?)?;
        let _ = flush_memory_source_deletions(&engine, &chat.store);
        if chat.store.memory_enabled()? {
            let _ = flush_memory_source_ingestions(&engine, &chat.store);
        }
        Ok(identity)
    })
    .await
    .map_err(|_| "助手引擎启动异常".to_string())?
}

#[tauri::command]
pub fn desktop_agent_status(
    engine: tauri::State<'_, std::sync::Arc<EngineManager>>,
) -> Result<Option<EngineIdentity>, String> {
    engine.identity()
}

#[tauri::command]
pub async fn desktop_agent_submit_task(
    app: tauri::AppHandle,
    engine: tauri::State<'_, std::sync::Arc<EngineManager>>,
    chat: tauri::State<'_, super::chat::ChatRuntime>,
    input: AgentTaskInput,
) -> Result<Value, String> {
    if input.content.trim().is_empty() || input.content.len() > 16_384
        || input.request_id.trim().is_empty() || input.request_id.len() > 200 {
        return Err("任务内容为空、过长或请求标识无效".into());
    }
    let engine = engine.inner().clone();
    let chat = chat.inner().clone();
    tauri::async_runtime::spawn_blocking(move || -> Result<Value, String> {
        start_for_chat(&app, &engine, &chat)?;
        let engine_session = ensure_engine_session(&engine, &chat.store, &input.session_id)?;
        let history = chat.store.request_context(&input.session_id, 60 * 1024)?;
        let context_messages = history.into_iter().rev().take(80).collect::<Vec<_>>().into_iter().rev().map(|item| serde_json::json!({
            "role": item.role, "content": item.content
        })).collect::<Vec<_>>();
        if !matches!(input.kind.as_str(), "assistant" | "writing") {
            return Err("桌面任务类型不受支持".into());
        }
        chat.store.append_message_with_id(
            &format!("agent-user-{}", input.request_id),
            &input.session_id, "user", &input.content,
        )?;
        let mut request = serde_json::json!({
            "kind": input.kind, "message": input.content,
            "client_request_id": input.request_id
        });
        if request["kind"] == "assistant" {
            request["context_messages"] = Value::Array(context_messages);
        }
        let response = engine.request("POST", &format!("/api/sessions/{engine_session}/tasks"),
            Some(request))?;
        let body = response.get("body").cloned().ok_or_else(|| "助手引擎未返回任务".to_string())?;
        if body["session_id"].as_str() != Some(engine_session.as_str()) {
            return Err("助手任务会话不匹配".into());
        }
        Ok(body)
    }).await.map_err(|_| "助手任务提交异常".to_string())?
}

#[tauri::command]
pub async fn desktop_agent_session_tasks(
    app: tauri::AppHandle,
    engine: tauri::State<'_, std::sync::Arc<EngineManager>>,
    chat: tauri::State<'_, super::chat::ChatRuntime>,
    session_id: String,
) -> Result<Vec<Value>, String> {
    let engine = engine.inner().clone();
    let chat = chat.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let Some(engine_session) = chat.store.engine_session(&session_id)? else {
            return Ok(Vec::new());
        };
        start_for_chat(&app, &engine, &chat)?;
        let response = engine.request("GET", &format!("/api/sessions/{engine_session}/tasks?limit=50"), None)?;
        response.pointer("/body/tasks").and_then(Value::as_array).cloned()
            .ok_or("助手引擎未返回任务历史".into())
    }).await.map_err(|_| "助手任务恢复异常".to_string())?
}

#[tauri::command]
pub async fn desktop_agent_workspaces(
    app: tauri::AppHandle,
    engine: tauri::State<'_, std::sync::Arc<EngineManager>>,
    chat: tauri::State<'_, super::chat::ChatRuntime>,
) -> Result<Vec<Value>, String> {
    let engine = engine.inner().clone();
    let chat = chat.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        start_for_chat(&app, &engine, &chat)?;
        let response = engine.request("GET", "/api/workspaces", None)?;
        response.pointer("/body/workspaces").and_then(Value::as_array).cloned()
            .ok_or("助手引擎未返回可用工作区".into())
    }).await.map_err(|_| "助手工作区查询异常".to_string())?
}

#[tauri::command]
pub async fn desktop_agent_register_workspace(
    app: tauri::AppHandle,
    engine: tauri::State<'_, std::sync::Arc<EngineManager>>,
    chat: tauri::State<'_, super::chat::ChatRuntime>,
    path: String,
) -> Result<Value, String> {
    if path.trim().is_empty() || path.len() > 32_768 {
        return Err("所选项目目录无效".into());
    }
    let engine = engine.inner().clone();
    let chat = chat.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        start_for_chat(&app, &engine, &chat)?;
        let response = engine.request("POST", "/api/desktop/workspaces", Some(
            serde_json::json!({"path":path})))?;
        response.get("body").cloned().ok_or_else(|| "助手引擎未返回目录授权".into())
    }).await.map_err(|_| "助手目录授权异常".to_string())?
}

#[tauri::command]
pub async fn desktop_agent_task_status(
    app: tauri::AppHandle,
    engine: tauri::State<'_, std::sync::Arc<EngineManager>>,
    chat: tauri::State<'_, super::chat::ChatRuntime>,
    session_id: String,
    run_id: String,
) -> Result<Value, String> {
    let engine = engine.inner().clone();
    let chat = chat.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        start_for_chat(&app, &engine, &chat)?;
        let engine_session = chat.store.engine_session(&session_id)?
            .ok_or_else(|| "助手会话映射不存在".to_string())?;
        let body = task_for_session(&engine, &engine_session, &run_id)?;
        if body["status"] == "completed" {
            let answer = body.pointer("/result/answer").and_then(Value::as_str).unwrap_or("");
            if !answer.is_empty() {
                chat.store.append_message_with_id(
                    &format!("agent-assistant-{run_id}"), &session_id, "assistant", answer,
                )?;
            }
        }
        Ok(body)
    }).await.map_err(|_| "助手任务状态查询异常".to_string())?
}

#[tauri::command]
pub async fn desktop_agent_task_action(
    app: tauri::AppHandle,
    engine: tauri::State<'_, std::sync::Arc<EngineManager>>,
    chat: tauri::State<'_, super::chat::ChatRuntime>,
    session_id: String,
    run_id: String,
    input: Value,
) -> Result<Value, String> {
    let run_id = uuid::Uuid::parse_str(&run_id).map_err(|_| "助手任务标识无效")?;
    let engine = engine.inner().clone();
    let chat = chat.inner().clone();
    tauri::async_runtime::spawn_blocking(move || -> Result<Value, String> {
        start_for_chat(&app, &engine, &chat)?;
        let engine_session = chat.store.engine_session(&session_id)?
            .ok_or_else(|| "助手会话映射不存在".to_string())?;
        task_for_session(&engine, &engine_session, &run_id.to_string())?;
        engine.request("POST", &format!("/api/tasks/{run_id}/actions"), Some(input))
    }).await.map_err(|_| "助手任务动作异常".to_string())?
}

#[tauri::command]
pub async fn desktop_agent_task_events(
    engine: tauri::State<'_, std::sync::Arc<EngineManager>>,
    chat: tauri::State<'_, super::chat::ChatRuntime>,
    session_id: String,
    run_id: String,
    after: u64,
) -> Result<Value, String> {
    let run_id = uuid::Uuid::parse_str(&run_id).map_err(|_| "助手任务标识无效")?;
    let engine = engine.inner().clone();
    let chat = chat.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let engine_session = chat.store.engine_session(&session_id)?
            .ok_or_else(|| "助手会话映射不存在".to_string())?;
        task_for_session(&engine, &engine_session, &run_id.to_string())?;
        engine.request("GET", &format!("/api/tasks/{run_id}/events?after={after}&limit=30"), None)
    }).await.map_err(|_| "助手任务事件查询异常".to_string())?
}

fn task_for_session(engine: &EngineManager, engine_session: &str, run_id: &str) -> Result<Value, String> {
    let response = engine.request("GET", &format!("/api/tasks/{run_id}"), None)?;
    let body = response.get("body").cloned().ok_or_else(|| "助手引擎未返回任务状态".to_string())?;
    if body["session_id"].as_str() != Some(engine_session) {
        return Err("助手任务不属于当前会话".into());
    }
    Ok(body)
}

#[tauri::command]
pub async fn desktop_agent_stop(
    engine: tauri::State<'_, std::sync::Arc<EngineManager>>,
) -> Result<(), String> {
    engine.stop()
}
