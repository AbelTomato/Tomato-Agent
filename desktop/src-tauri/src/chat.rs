use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use std::path::Path;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};
use tauri::Emitter;
use tauri::Manager;
use tauri_plugin_dialog::DialogExt;

#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ChatConfigInput {
    pub base_url: String,
    pub model: String,
    pub api_key: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ChatConfig {
    pub base_url: String,
    pub model: String,
}

impl ChatConfig {
    fn new(base_url: String, model: String) -> Result<Self, String> {
        let url = reqwest::Url::parse(&base_url).map_err(|_| "API 地址无效".to_string())?;
        if !matches!(url.scheme(), "http" | "https") || url.host_str().is_none() {
            return Err("API 地址必须使用 HTTP 或 HTTPS".into());
        }
        let model = model.trim();
        if model.is_empty() || model.len() > 256 || model.chars().any(char::is_control) {
            return Err("模型名称无效".into());
        }
        Ok(Self { base_url: base_url.trim_end_matches('/').to_string(), model: model.into() })
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct ChatMessage {
    pub id: String,
    pub session_id: String,
    pub role: String,
    pub content: String,
    pub created_at: String,
}

#[derive(Clone, Debug, Serialize)]
pub struct ChatSession {
    pub id: String,
    pub title: String,
    pub created_at: String,
    pub project_id: Option<String>,
}

#[derive(Clone, Debug, Serialize)]
pub struct ChatProject {
    pub id: String,
    pub name: String,
    pub root_path: String,
    pub available: bool,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CreateProjectInput {
    pub root_path: String,
    pub name: Option<String>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SetSessionProjectInput {
    pub session_id: String,
    pub project_id: Option<String>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SendInput {
    pub session_id: String,
    pub content: String,
}

pub struct ChatStore {
    connection: Mutex<Connection>,
}

impl ChatStore {
    pub fn open(path: &Path) -> Result<Self, String> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|_| "无法创建聊天数据目录".to_string())?;
        }
        let connection = Connection::open(path).map_err(|_| "无法打开聊天数据库".to_string())?;
        connection.execute_batch(
            "PRAGMA journal_mode=WAL;
             PRAGMA foreign_keys=ON;
             CREATE TABLE IF NOT EXISTS projects (
                 id TEXT PRIMARY KEY, name TEXT NOT NULL, root_path TEXT NOT NULL UNIQUE,
                 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
             );
             CREATE TABLE IF NOT EXISTS sessions (
                 id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL,
                 project_id TEXT REFERENCES projects(id) ON DELETE SET NULL
             );
             CREATE TABLE IF NOT EXISTS messages (
                 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                 role TEXT NOT NULL CHECK(role IN ('user', 'assistant')), content TEXT NOT NULL,
                 created_at TEXT NOT NULL
             );",
        ).map_err(|_| "无法初始化聊天数据库".to_string())?;
        let columns = connection.prepare("PRAGMA table_info(sessions)")
            .map_err(|_| "无法读取聊天数据库结构".to_string())?
            .query_map([], |row| row.get::<_, String>(1))
            .map_err(|_| "无法读取聊天数据库结构".to_string())?
            .collect::<Result<Vec<_>, _>>().map_err(|_| "无法读取聊天数据库结构".to_string())?;
        if !columns.iter().any(|column| column == "project_id") {
            connection.execute("ALTER TABLE sessions ADD COLUMN project_id TEXT REFERENCES projects(id) ON DELETE SET NULL", [])
                .map_err(|_| "无法升级聊天数据库".to_string())?;
        }
        Ok(Self { connection: Mutex::new(connection) })
    }

    pub fn create_session(&self, title: &str) -> Result<String, String> {
        let title = normalize_title(title)?;
        let id = uuid::Uuid::new_v4().to_string();
        self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?
            .execute("INSERT INTO sessions(id,title,created_at) VALUES(?1,?2,?3)",
                params![id, title, utc_now()]).map_err(|_| "无法创建对话".to_string())?;
        Ok(id)
    }

    pub fn sessions(&self) -> Result<Vec<ChatSession>, String> {
        let connection = self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?;
        let mut statement = connection.prepare("SELECT id,title,created_at,project_id FROM sessions ORDER BY created_at DESC,id DESC")
            .map_err(|_| "无法读取对话列表".to_string())?;
        let rows = statement.query_map([], |row| Ok(ChatSession { id: row.get(0)?, title: row.get(1)?, created_at: row.get(2)?, project_id: row.get(3)? }))
            .map_err(|_| "无法读取对话列表".to_string())?;
        rows.collect::<Result<Vec<_>, _>>().map_err(|_| "无法读取对话列表".to_string())
    }

    pub fn projects(&self) -> Result<Vec<ChatProject>, String> {
        let connection = self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?;
        let mut statement = connection.prepare(
            "SELECT id,name,root_path,created_at,updated_at FROM projects ORDER BY name COLLATE NOCASE,id",
        ).map_err(|_| "无法读取项目列表".to_string())?;
        let rows = statement.query_map([], |row| {
            let root_path: String = row.get(2)?;
            Ok(ChatProject { id: row.get(0)?, name: row.get(1)?, available: Path::new(&root_path).is_dir(),
                root_path, created_at: row.get(3)?, updated_at: row.get(4)? })
        }).map_err(|_| "无法读取项目列表".to_string())?;
        rows.collect::<Result<Vec<_>, _>>().map_err(|_| "无法读取项目列表".to_string())
    }

    pub fn create_project(&self, root_path: &str, name: Option<&str>) -> Result<ChatProject, String> {
        let root_path = normalize_project_path(root_path)?;
        if !Path::new(&root_path).is_dir() { return Err("项目目录不可用".into()); }
        let name = normalize_project_name(name.filter(|value| !value.trim().is_empty()).unwrap_or_else(|| {
            Path::new(&root_path).file_name().and_then(|value| value.to_str()).unwrap_or("新项目")
        }))?;
        let id = uuid::Uuid::new_v4().to_string();
        let timestamp = utc_now();
        self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?
            .execute("INSERT INTO projects(id,name,root_path,created_at,updated_at) VALUES(?1,?2,?3,?4,?5)",
                params![id, name, root_path, timestamp, timestamp])
            .map_err(|error| -> String {
                if error.to_string().contains("UNIQUE") { "该目录已经绑定项目".into() }
                else { "无法创建项目".into() }
            })?;
        Ok(ChatProject { id, name, root_path, available: true, created_at: timestamp.clone(), updated_at: timestamp })
    }

    pub fn rename_project(&self, project_id: &str, name: &str) -> Result<(), String> {
        let name = normalize_project_name(name)?;
        let changed = self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?
            .execute("UPDATE projects SET name=?1,updated_at=?2 WHERE id=?3", params![name, utc_now(), project_id])
            .map_err(|_| "无法重命名项目".to_string())?;
        if changed == 0 { return Err("项目不存在".into()); }
        Ok(())
    }

    pub fn delete_project(&self, project_id: &str) -> Result<(), String> {
        let changed = self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?
            .execute("DELETE FROM projects WHERE id=?1", [project_id])
            .map_err(|_| "无法删除项目".to_string())?;
        if changed == 0 { return Err("项目不存在".into()); }
        Ok(())
    }

    pub fn set_session_project(&self, session_id: &str, project_id: Option<&str>) -> Result<(), String> {
        let connection = self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?;
        if let Some(project_id) = project_id {
            let exists: Option<String> = connection.query_row("SELECT id FROM projects WHERE id=?1", [project_id], |row| row.get(0))
                .optional().map_err(|_| "无法读取项目".to_string())?;
            if exists.is_none() { return Err("项目不存在".into()); }
        }
        let changed = connection.execute("UPDATE sessions SET project_id=?1 WHERE id=?2", params![project_id, session_id])
            .map_err(|_| "无法更新对话项目".to_string())?;
        if changed == 0 { return Err("对话不存在".into()); }
        Ok(())
    }

    pub fn rename_session(&self, session_id: &str, title: &str) -> Result<(), String> {
        let title = normalize_title(title)?;
        let changed = self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?
            .execute("UPDATE sessions SET title=?1 WHERE id=?2", params![title, session_id])
            .map_err(|_| "无法重命名对话".to_string())?;
        if changed == 0 { return Err("对话不存在".into()); }
        Ok(())
    }

    pub fn delete_session(&self, session_id: &str) -> Result<(), String> {
        let changed = self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?
            .execute("DELETE FROM sessions WHERE id=?1", [session_id])
            .map_err(|_| "无法删除对话".to_string())?;
        if changed == 0 { return Err("对话不存在".into()); }
        Ok(())
    }

    pub fn append_message(&self, session_id: &str, role: &str, content: &str) -> Result<ChatMessage, String> {
        if !matches!(role, "user" | "assistant") { return Err("消息角色无效".into()); }
        let message = ChatMessage { id: uuid::Uuid::new_v4().to_string(), session_id: session_id.into(),
            role: role.into(), content: content.into(), created_at: utc_now() };
        self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?
            .execute("INSERT INTO messages(id,session_id,role,content,created_at) VALUES(?1,?2,?3,?4,?5)",
                params![message.id, message.session_id, message.role, message.content, message.created_at])
            .map_err(|_| "无法保存消息".to_string())?;
        Ok(message)
    }

    pub fn messages(&self, session_id: &str) -> Result<Vec<ChatMessage>, String> {
        self.messages_page(session_id, 0, 500)
    }

    pub fn messages_page(&self, session_id: &str, offset: u32, limit: u32) -> Result<Vec<ChatMessage>, String> {
        let connection = self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?;
        let mut statement = connection.prepare(
            "SELECT id,session_id,role,content,created_at FROM messages WHERE session_id=?1 ORDER BY rowid LIMIT ?2 OFFSET ?3",
        ).map_err(|_| "无法读取消息".to_string())?;
        let rows = statement.query_map(params![session_id, limit.clamp(1, 500), offset], |row| Ok(ChatMessage {
            id: row.get(0)?, session_id: row.get(1)?, role: row.get(2)?,
            content: row.get(3)?, created_at: row.get(4)?,
        })).map_err(|_| "无法读取消息".to_string())?;
        rows.collect::<Result<Vec<_>, _>>().map_err(|_| "无法读取消息".to_string())
    }

    pub fn latest_session(&self) -> Result<Option<String>, String> {
        self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?
            .query_row("SELECT id FROM sessions ORDER BY created_at DESC,id DESC LIMIT 1", [], |row| row.get(0))
            .optional().map_err(|_| "无法恢复最近对话".to_string())
    }
}

fn normalize_title(title: &str) -> Result<String, String> {
    let title = title.trim();
    if title.is_empty() || title.chars().any(char::is_control) || title.chars().count() > 120 {
        return Err("对话标题无效".into());
    }
    Ok(title.to_string())
}

fn normalize_project_name(name: &str) -> Result<String, String> {
    let name = name.trim();
    if name.is_empty() || name.chars().any(char::is_control) || name.chars().count() > 120 {
        return Err("项目名称无效".into());
    }
    Ok(name.to_string())
}

fn normalize_project_path(value: &str) -> Result<String, String> {
    let value = value.trim();
    if value.is_empty() || value.len() > 4096 || value.chars().any(char::is_control)
        || value.split(['/', '\\']).any(|part| part == "..") {
        return Err("项目目录路径无效".into());
    }
    let path = Path::new(value);
    if !path.is_absolute() {
        return Err("项目目录必须是绝对路径".into());
    }
    std::fs::canonicalize(path).map(|path| path.to_string_lossy().into_owned())
        .map_err(|_| "项目目录不可用".into())
}

#[derive(Default)]
struct SseParser { buffer: Vec<u8>, done: bool }

impl SseParser {
    fn push(&mut self, bytes: &[u8]) -> Result<Vec<String>, String> {
        self.buffer.extend_from_slice(bytes);
        let mut deltas = Vec::new();
        while let Some((end, separator_len)) = frame_boundary(&self.buffer) {
            let frame = self.buffer.drain(..end).collect::<Vec<_>>();
            self.buffer.drain(..separator_len);
            let frame = String::from_utf8(frame).map_err(|_| "Provider 返回无效 UTF-8".to_string())?;
            let data = frame.lines().filter_map(|line| line.strip_prefix("data:").map(str::trim_start)).collect::<Vec<_>>().join("\n");
            if data.is_empty() { continue; }
            if data == "[DONE]" { self.done = true; continue; }
            let value: serde_json::Value = serde_json::from_str(&data).map_err(|_| "Provider 返回无效流数据".to_string())?;
            if let Some(content) = value.pointer("/choices/0/delta/content").and_then(serde_json::Value::as_str) {
                if !content.is_empty() { deltas.push(content.to_string()); }
            }
            if let Some(error) = value.pointer("/error/message").and_then(serde_json::Value::as_str) {
                let _ = error;
                return Err("Provider 请求失败".into());
            }
        }
        Ok(deltas)
    }

    fn finish(&mut self) -> Result<(), String> {
        if !self.done { return Err("Provider 流意外结束".into()); }
        if !self.buffer.iter().all(u8::is_ascii_whitespace) { return Err("Provider 流意外结束".into()); }
        Ok(())
    }
}

fn frame_boundary(buffer: &[u8]) -> Option<(usize, usize)> {
    let lf = buffer.windows(2).position(|w| w == b"\n\n").map(|at| (at, 2));
    let crlf = buffer.windows(4).position(|w| w == b"\r\n\r\n").map(|at| (at, 4));
    match (lf, crlf) {
        (Some(a), Some(b)) => Some(if a.0 <= b.0 { a } else { b }),
        (Some(value), None) | (None, Some(value)) => Some(value),
        (None, None) => None,
    }
}

#[cfg(test)]
struct TestCredentialStore(Mutex<Option<String>>);

#[cfg(test)]
impl CredentialStore for TestCredentialStore {
    fn get(&self) -> Result<Option<String>, String> { Ok(self.0.lock().unwrap().clone()) }
    fn set(&self, value: &str) -> Result<(), String> { *self.0.lock().unwrap() = Some(value.into()); Ok(()) }
    fn delete(&self) -> Result<(), String> { *self.0.lock().unwrap() = None; Ok(()) }
}

fn utc_now() -> String {
    let elapsed = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default();
    let secs = elapsed.as_secs() as i64;
    let days = secs.div_euclid(86_400);
    let day_secs = secs.rem_euclid(86_400);
    let (year, month, day) = civil_from_days(days);
    format!("{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}Z", day_secs / 3600, day_secs / 60 % 60, day_secs % 60)
}

fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    (y + i64::from(mp >= 10), mp + if mp < 10 { 3 } else { -9 }, doy - (153 * mp + 2) / 5 + 1)
}

pub trait ChatProvider: Send + Sync {
    fn complete(&self, config: &ChatConfig, api_key: &str, messages: &[ChatMessage], cancel: &AtomicBool, emit: &mut dyn FnMut(&str) -> Result<(), String>) -> Result<String, String>;
}

#[cfg(test)]
pub struct FakeProvider { chunks: Vec<String> }

#[cfg(test)]
impl FakeProvider {
    pub fn new(chunks: Vec<String>) -> Self { Self { chunks } }
}

#[cfg(test)]
impl ChatProvider for FakeProvider {
    fn complete(&self, _config: &ChatConfig, _api_key: &str, _messages: &[ChatMessage], cancel: &AtomicBool, emit: &mut dyn FnMut(&str) -> Result<(), String>) -> Result<String, String> {
        let mut answer = String::new();
        for chunk in &self.chunks { if cancel.load(Ordering::Relaxed) { return Err("请求已取消".into()); } emit(chunk)?; answer.push_str(chunk); }
        if answer.trim().is_empty() { return Err("模型返回空回复".into()); }
        Ok(answer)
    }
}

pub struct OpenAiChatProvider;

impl ChatProvider for OpenAiChatProvider {
    fn complete(&self, config: &ChatConfig, api_key: &str, messages: &[ChatMessage], cancel: &AtomicBool, emit: &mut dyn FnMut(&str) -> Result<(), String>) -> Result<String, String> {
        let endpoint = format!("{}/chat/completions", config.base_url.trim_end_matches('/'));
        let body = serde_json::json!({
            "model": config.model,
            "stream": true,
            "messages": messages.iter().map(|m| serde_json::json!({"role":m.role,"content":m.content})).collect::<Vec<_>>()
        });
        let runtime = tokio::runtime::Builder::new_current_thread().enable_all().build()
            .map_err(|_| "无法初始化 Provider 请求".to_string())?;
        let mut response = runtime.block_on(async {
            let client = reqwest::Client::builder().redirect(reqwest::redirect::Policy::none())
                .timeout(std::time::Duration::from_secs(120)).build()
                .map_err(|_| "无法初始化 Provider 请求".to_string())?;
            tokio::select! {
                _ = wait_cancelled(cancel) => Err("请求已取消".to_string()),
                result = client.post(endpoint).bearer_auth(api_key).json(&body).send() =>
                    result.map_err(|_| "Provider 网络请求失败或超时".to_string())
            }
        })?;
        if !response.status().is_success() { return Err(format!("Provider 请求失败（HTTP {}）", response.status().as_u16())); }
        let mut parser = SseParser::default();
        let mut answer = String::new();
        loop {
            if cancel.load(Ordering::Relaxed) { return Err("请求已取消".into()); }
            let chunk = runtime.block_on(async {
                tokio::select! {
                    _ = wait_cancelled(cancel) => Err("请求已取消".to_string()),
                    result = response.chunk() => result.map_err(|_| "Provider 流读取失败".to_string())
                }
            })?;
            let Some(chunk) = chunk else { break; };
            for delta in parser.push(&chunk)? {
                emit(&delta)?;
                answer.push_str(&delta);
            }
        }
        parser.finish()?;
        if answer.trim().is_empty() { return Err("模型返回空回复".into()); }
        Ok(answer)
    }
}

pub trait CredentialStore: Send + Sync {
    fn get(&self) -> Result<Option<String>, String>;
    fn set(&self, value: &str) -> Result<(), String>;
    fn delete(&self) -> Result<(), String>;
}

pub struct NativeCredentialStore;

impl CredentialStore for NativeCredentialStore {
    fn get(&self) -> Result<Option<String>, String> {
        let entry = keyring::Entry::new("dev.tomato.agent.chat", "provider-api-key").map_err(|_| "系统凭据库不可用".to_string())?;
        match entry.get_password() { Ok(value) => Ok(Some(value)), Err(keyring::Error::NoEntry) => Ok(None), Err(_) => Err("系统凭据库不可用".into()) }
    }
    fn set(&self, value: &str) -> Result<(), String> {
        let entry = keyring::Entry::new("dev.tomato.agent.chat", "provider-api-key").map_err(|_| "系统凭据库不可用".to_string())?;
        entry.set_password(value).map_err(|_| "无法保存系统凭据".to_string())
    }
    fn delete(&self) -> Result<(), String> {
        let entry = keyring::Entry::new("dev.tomato.agent.chat", "provider-api-key").map_err(|_| "系统凭据库不可用".to_string())?;
        match entry.delete_credential() { Ok(_) | Err(keyring::Error::NoEntry) => Ok(()), Err(_) => Err("无法删除系统凭据".into()) }
    }
}

#[derive(Clone)]
pub struct ChatRuntime {
    pub store: Arc<ChatStore>,
    pub config: Arc<Mutex<Option<ChatConfig>>>,
    pub config_path: PathBuf,
    pub credentials: Arc<dyn CredentialStore>,
    pub cancellation: Arc<Mutex<Option<Arc<AtomicBool>>>>,
    pub active_chat_session: Arc<Mutex<Option<String>>>,
    pub connection_test_cancellation: Arc<Mutex<Option<Arc<AtomicBool>>>>,
    pub provider: Arc<dyn ChatProvider>,
}

pub fn initialize(app: &tauri::AppHandle) -> Result<ChatRuntime, String> {
    let root = app.path().app_data_dir().map_err(|_| "无法定位聊天数据目录".to_string())?
        .join("TomatoAgent").join("desktop-chat");
    std::fs::create_dir_all(&root).map_err(|_| "无法创建聊天数据目录".to_string())?;
    let store = ChatStore::open(&root.join("chat.sqlite3"))?;
    let credentials: Arc<dyn CredentialStore> = Arc::new(NativeCredentialStore);
    let config_path = root.join("model.json");
    let config = if config_path.exists() {
        let bytes = std::fs::read(&config_path).map_err(|_| "无法读取模型设置".to_string())?;
        let saved: ChatConfig = serde_json::from_slice(&bytes).map_err(|_| "模型设置格式无效".to_string())?;
        Some(ChatConfig::new(saved.base_url, saved.model)?)
    } else { None };
    Ok(ChatRuntime { store: Arc::new(store), config: Arc::new(Mutex::new(config)), config_path,
        credentials, provider: Arc::new(OpenAiChatProvider),
        cancellation: Arc::new(Mutex::new(None)), active_chat_session: Arc::new(Mutex::new(None)),
        connection_test_cancellation: Arc::new(Mutex::new(None)) })
}

#[derive(Serialize)]
pub struct ChatStatus { pub configured: bool }

#[tauri::command]
pub fn desktop_chat_status(runtime: tauri::State<'_, ChatRuntime>) -> ChatStatus {
    ChatStatus { configured: runtime.config.lock().map(|v| v.is_some()).unwrap_or(false) }
}

#[tauri::command]
pub async fn desktop_chat_pick_project_directory(app: tauri::AppHandle) -> Result<Option<String>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        app.dialog().file().blocking_pick_folder()
            .map(|path| path.into_path().map_err(|_| "所选目录不是本地文件系统路径".to_string()))
            .transpose()
            .map(|path| path.map(|value| value.to_string_lossy().into_owned()))
    }).await.map_err(|_| "目录选择器执行失败".to_string())?
}

#[tauri::command]
pub fn desktop_chat_projects(runtime: tauri::State<'_, ChatRuntime>) -> Result<Vec<ChatProject>, String> {
    runtime.store.projects()
}

#[tauri::command]
pub fn desktop_chat_create_project(runtime: tauri::State<'_, ChatRuntime>, input: CreateProjectInput) -> Result<ChatProject, String> {
    runtime.store.create_project(&input.root_path, input.name.as_deref())
}

#[tauri::command]
pub fn desktop_chat_rename_project(runtime: tauri::State<'_, ChatRuntime>, project_id: String, name: String) -> Result<(), String> {
    runtime.store.rename_project(&project_id, &name)
}

#[tauri::command]
pub fn desktop_chat_delete_project(runtime: tauri::State<'_, ChatRuntime>, project_id: String) -> Result<(), String> {
    runtime.store.delete_project(&project_id)
}

#[tauri::command]
pub fn desktop_chat_set_session_project(runtime: tauri::State<'_, ChatRuntime>, input: SetSessionProjectInput) -> Result<(), String> {
    runtime.store.set_session_project(&input.session_id, input.project_id.as_deref())
}

#[tauri::command]
pub fn desktop_chat_configure(runtime: tauri::State<'_, ChatRuntime>, input: ChatConfigInput) -> Result<ChatConfig, String> {
    let config = ChatConfig::new(input.base_url, input.model)?;
    if let Some(key) = input.api_key.filter(|v| !v.is_empty()) { runtime.credentials.set(&key)?; }
    std::fs::write(&runtime.config_path, serde_json::to_vec(&config).map_err(|_| "无法编码模型设置".to_string())?)
        .map_err(|_| "无法保存模型设置".to_string())?;
    *runtime.config.lock().map_err(|_| "聊天配置不可用".to_string())? = Some(config.clone());
    Ok(config)
}

#[tauri::command]
pub fn desktop_chat_delete_credential(runtime: tauri::State<'_, ChatRuntime>) -> Result<(), String> {
    runtime.credentials.delete()?;
    let _ = std::fs::remove_file(&runtime.config_path);
    *runtime.config.lock().map_err(|_| "聊天配置不可用".to_string())? = None;
    Ok(())
}

#[tauri::command]
pub fn desktop_chat_create_session(runtime: tauri::State<'_, ChatRuntime>) -> Result<String, String> {
    runtime.store.create_session("新对话")
}

#[tauri::command]
pub fn desktop_chat_sessions(runtime: tauri::State<'_, ChatRuntime>) -> Result<Vec<ChatSession>, String> {
    runtime.store.sessions()
}

#[tauri::command]
pub fn desktop_chat_rename_session(runtime: tauri::State<'_, ChatRuntime>, session_id: String, title: String) -> Result<(), String> {
    runtime.store.rename_session(&session_id, &title)
}

#[tauri::command]
pub fn desktop_chat_delete_session(runtime: tauri::State<'_, ChatRuntime>, session_id: String) -> Result<(), String> {
    if runtime.active_chat_session.lock().map_err(|_| "聊天状态不可用".to_string())?.as_deref() == Some(session_id.as_str()) {
        if let Some(cancel) = runtime.cancellation.lock().map_err(|_| "聊天状态不可用".to_string())?.take() {
            cancel.store(true, Ordering::Relaxed);
        }
    }
    runtime.store.delete_session(&session_id)
}

#[tauri::command]
pub fn desktop_chat_history(runtime: tauri::State<'_, ChatRuntime>, session_id: String) -> Result<Vec<ChatMessage>, String> {
    runtime.store.messages(&session_id)
}

#[tauri::command]
pub fn desktop_chat_history_page(runtime: tauri::State<'_, ChatRuntime>, session_id: String, offset: u32, limit: u32) -> Result<Vec<ChatMessage>, String> {
    runtime.store.messages_page(&session_id, offset, limit)
}

#[tauri::command]
pub fn desktop_chat_latest_session(runtime: tauri::State<'_, ChatRuntime>) -> Result<Option<String>, String> {
    runtime.store.latest_session()
}

#[tauri::command]
pub async fn desktop_chat_send(app: tauri::AppHandle, runtime: tauri::State<'_, ChatRuntime>, input: SendInput) -> Result<(), String> {
    let SendInput { session_id, content } = input;
    if content.trim().is_empty() || content.len() > 64 * 1024 { return Err("消息内容为空或过长".into()); }
    let config = runtime.config.lock().map_err(|_| "聊天配置不可用".to_string())?.clone().ok_or("请先配置模型")?;
    let key = runtime.credentials.get()?.filter(|v| !v.is_empty()).ok_or("请先配置 API Key")?;
    runtime.store.append_message(&session_id, "user", &content)?;
    let cancel = Arc::new(AtomicBool::new(false));
    if let Some(previous) = runtime.cancellation.lock().map_err(|_| "聊天状态不可用".to_string())?.replace(cancel.clone()) {
        previous.store(true, Ordering::Relaxed);
    }
    *runtime.active_chat_session.lock().map_err(|_| "聊天状态不可用".to_string())? = Some(session_id.clone());
    let provider = runtime.provider.clone();
    let history = runtime.store.messages(&session_id)?;
    let (tx, rx) = std::sync::mpsc::channel::<String>();
    let task_cancel = cancel.clone();
    let worker = std::thread::spawn(move || {
        let mut emit = |delta: &str| { if task_cancel.load(Ordering::Relaxed) { return Err("cancelled".into()); } tx.send(delta.to_string()).map_err(|_| "接收方已关闭".to_string()) };
        provider.complete(&config, &key, &history, &task_cancel, &mut emit)
    });
    let mut answer = String::new();
    loop {
        if cancel.load(Ordering::Relaxed) { break; }
        match rx.recv_timeout(std::time::Duration::from_millis(40)) {
            Ok(delta) => {
                answer.push_str(&delta);
                let _ = app.emit("desktop_chat_delta", serde_json::json!({"session_id":session_id,"delta":delta}));
            }
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => continue,
            Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => break,
        }
    }
    let result = worker.join().map_err(|_| "聊天 Provider 异常".to_string())?;
    if cancel.load(Ordering::Relaxed) {
        clear_cancellation(&runtime, &cancel);
        let _ = app.emit("desktop_chat_cancelled", serde_json::json!({"session_id":session_id}));
        return Ok(());
    }
    if let Err(error) = result {
        clear_cancellation(&runtime, &cancel);
        let _ = app.emit("desktop_chat_failed", serde_json::json!({"session_id":session_id,"error":error}));
        return Err(error);
    }
    runtime.store.append_message(&session_id, "assistant", &answer)?;
    let _ = app.emit("desktop_chat_completed", serde_json::json!({"session_id":session_id}));
    clear_cancellation(&runtime, &cancel);
    Ok(())
}

#[tauri::command]
pub async fn desktop_chat_test_connection(runtime: tauri::State<'_, ChatRuntime>, input: ChatConfigInput) -> Result<(), String> {
    let config = ChatConfig::new(input.base_url, input.model)?;
    let key = input.api_key.filter(|value| !value.is_empty())
        .or(runtime.credentials.get()?).filter(|value| !value.is_empty()).ok_or("请先配置 API Key")?;
    let endpoint = format!("{}/chat/completions", config.base_url.trim_end_matches('/'));
    let client = reqwest::Client::builder().redirect(reqwest::redirect::Policy::none())
        .timeout(std::time::Duration::from_secs(120)).build()
        .map_err(|_| "无法初始化 Provider 请求".to_string())?;
    let cancel = Arc::new(AtomicBool::new(false));
    if let Some(previous) = runtime.connection_test_cancellation.lock().map_err(|_| "聊天状态不可用".to_string())?.replace(cancel.clone()) {
        previous.store(true, Ordering::Relaxed);
    }
    let result = tokio::select! {
        _ = wait_cancelled(&cancel) => Err("连接测试已取消".to_string()),
        response = client.post(endpoint).bearer_auth(&key)
            .json(&serde_json::json!({"model":config.model,"stream":false,"messages":[{"role":"user","content":"Reply with OK."}]})).send() =>
                response.map_err(|_| "Provider 连接失败或超时".to_string())
    };
    clear_connection_test_cancellation(&runtime, &cancel);
    let result = result?;
        if !result.status().is_success() { return Err(format!("Provider 连接失败（HTTP {}）", result.status().as_u16())); }
    Ok(())
}

#[tauri::command]
pub fn desktop_chat_cancel(runtime: tauri::State<'_, ChatRuntime>) -> Result<(), String> {
    if let Some(cancel) = runtime.cancellation.lock().map_err(|_| "聊天状态不可用".to_string())?.take() { cancel.store(true, Ordering::Relaxed); }
    Ok(())
}

#[tauri::command]
pub fn desktop_chat_cancel_connection_test(runtime: tauri::State<'_, ChatRuntime>) -> Result<(), String> {
    if let Some(cancel) = runtime.connection_test_cancellation.lock().map_err(|_| "聊天状态不可用".to_string())?.take() {
        cancel.store(true, Ordering::Relaxed);
    }
    Ok(())
}

fn clear_connection_test_cancellation(runtime: &ChatRuntime, request: &Arc<AtomicBool>) {
    if let Ok(mut active) = runtime.connection_test_cancellation.lock() {
        if active.as_ref().is_some_and(|current| Arc::ptr_eq(current, request)) { *active = None; }
    }
}

async fn wait_cancelled(cancel: &AtomicBool) {
    while !cancel.load(Ordering::Relaxed) { tokio::time::sleep(std::time::Duration::from_millis(20)).await; }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn chat_config_requires_http_endpoint_and_model_without_exposing_credentials() {
        assert!(ChatConfig::new("https://api.example.test/v1".into(), "model-x".into()).is_ok());
        assert!(ChatConfig::new("file:///tmp/provider".into(), "model-x".into()).is_err());
        assert!(ChatConfig::new("https://api.example.test/v1".into(), " ".into()).is_err());
        let config = ChatConfig::new("https://api.example.test/v1".into(), "model-x".into()).unwrap();
        assert!(!serde_json::to_string(&config).unwrap().contains("api_key"));
    }

    #[test]
    fn sqlite_history_survives_reopening_and_uses_utc_timestamps() {
        let path = std::env::temp_dir().join(format!("desktop-chat-{}.db", uuid::Uuid::new_v4()));
        let session_id;
        {
            let store = ChatStore::open(&path).unwrap();
            session_id = store.create_session("Test").unwrap();
            store.append_message(&session_id, "user", "hello").unwrap();
        }
        let store = ChatStore::open(&path).unwrap();
        assert_eq!(store.latest_session().unwrap().as_deref(), Some(session_id.as_str()));
        let messages = store.messages(&session_id).unwrap();
        assert_eq!(messages.len(), 1);
        assert_eq!(messages[0].content, "hello");
        assert!(messages[0].created_at.ends_with('Z'));
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn projects_group_sessions_without_deleting_history_or_directories() {
        let root = std::env::temp_dir().join(format!("desktop-chat-project-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&root).unwrap();
        let db = root.join("chat.sqlite3");
        let store = ChatStore::open(&db).unwrap();
        let session = store.create_session("Conversation").unwrap();
        let project = store.create_project(root.to_str().unwrap(), Some("Workspace")).unwrap();
        assert!(store.create_project(root.to_str().unwrap(), Some("Duplicate")).is_err());
        assert!(store.create_project("relative/path", Some("Invalid")).is_err());
        assert!(store.set_session_project(&session, Some("missing-project")).is_err());
        store.append_message(&session, "user", "Keep me").unwrap();
        assert_eq!(store.projects().unwrap()[0].root_path, std::fs::canonicalize(&root).unwrap().to_string_lossy());
        store.set_session_project(&session, Some(&project.id)).unwrap();
        assert_eq!(store.sessions().unwrap()[0].project_id.as_deref(), Some(project.id.as_str()));
        store.rename_project(&project.id, "Renamed").unwrap();
        store.delete_project(&project.id).unwrap();
        assert!(store.sessions().unwrap()[0].project_id.is_none());
        assert_eq!(store.messages(&session).unwrap().len(), 1);
        assert!(root.is_dir());
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn legacy_sessions_are_migrated_without_project_assignment() {
        let path = std::env::temp_dir().join(format!("desktop-chat-legacy-{}.db", uuid::Uuid::new_v4()));
        {
            let connection = Connection::open(&path).unwrap();
            connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL)", []).unwrap();
            connection.execute("INSERT INTO sessions VALUES ('legacy', 'Legacy', '2026-01-01T00:00:00Z')", []).unwrap();
        }
        let store = ChatStore::open(&path).unwrap();
        let session = store.sessions().unwrap();
        assert_eq!(session.len(), 1);
        assert!(session[0].project_id.is_none());
        assert!(store.projects().unwrap().is_empty());
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn sessions_can_be_listed_renamed_and_deleted_with_cascading_history() {
        let path = std::env::temp_dir().join(format!("desktop-chat-crud-{}.db", uuid::Uuid::new_v4()));
        let store = ChatStore::open(&path).unwrap();
        let first = store.create_session("First").unwrap();
        let second = store.create_session("Second").unwrap();
        store.append_message(&first, "user", "kept private").unwrap();
        assert_eq!(store.sessions().unwrap().len(), 2);
        store.rename_session(&first, "Renamed").unwrap();
        assert_eq!(store.sessions().unwrap().iter().find(|s| s.id == first).unwrap().title, "Renamed");
        store.delete_session(&first).unwrap();
        assert!(store.messages(&first).unwrap().is_empty());
        assert_eq!(store.sessions().unwrap().iter().map(|s| s.id.as_str()).collect::<Vec<_>>(), [second.as_str()]);
        assert!(store.rename_session(&first, "missing").is_err());
        assert!(store.create_session("bad\ntitle").is_err());
        assert!(store.create_session(&"x".repeat(121)).is_err());
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn message_pages_are_ordered_and_bounded() {
        let path = std::env::temp_dir().join(format!("desktop-chat-pages-{}.db", uuid::Uuid::new_v4()));
        let store = ChatStore::open(&path).unwrap();
        let id = store.create_session("Pages").unwrap();
        for index in 0..4 { store.append_message(&id, "user", &index.to_string()).unwrap(); }
        let page = store.messages_page(&id, 1, 2).unwrap();
        assert_eq!(page.iter().map(|message| message.content.as_str()).collect::<Vec<_>>(), ["1", "2"]);
        assert_eq!(store.messages_page(&id, 0, 900).unwrap().len(), 4);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn sse_parser_preserves_utf8_and_delta_boundaries_and_rejects_incomplete_stream() {
        let input = b"data: {\"choices\":[{\"delta\":{\"content\":\"\xe4\xbd\xa0\"}}]}\r\n\r\ndata: {\"choices\":[{\"delta\":{\"content\":\"hello\"}}]}\n\ndata: [DONE]\n\n";
        let mut parser = SseParser::default();
        let mut result = Vec::new();
        for part in input.chunks(7) { result.extend(parser.push(part).unwrap()); }
        assert_eq!(result, ["你", "hello"]);
        assert!(parser.finish().is_ok());
        let mut broken = SseParser::default();
        assert!(broken.push(b"data: {bad}\n\n").is_err());
    }

    #[test]
    fn openai_provider_sends_only_given_session_history_and_streams_response() {
        use std::io::{Read, Write};
        use std::net::TcpListener;
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            let mut request = vec![0; 8192];
            let count = socket.read(&mut request).unwrap();
            let request = String::from_utf8_lossy(&request[..count]);
            assert!(request.to_ascii_lowercase().contains("authorization: bearer test-key"));
            assert!(request.contains("current session"));
            assert!(!request.contains("other session"));
            let body = "data: {\"choices\":[{\"delta\":{\"content\":\"Hello\"}}]}\r\n\r\ndata: [DONE]\r\n\r\n";
            write!(socket, "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}", body.len(), body).unwrap();
        });
        let config = ChatConfig::new(format!("http://{address}/v1"), "test-model".into()).unwrap();
        let history = vec![ChatMessage { id: "1".into(), session_id: "s1".into(), role: "user".into(), content: "current session".into(), created_at: utc_now() }];
        let mut deltas = Vec::new();
        let answer = OpenAiChatProvider.complete(&config, "test-key", &history, &AtomicBool::new(false), &mut |delta| { deltas.push(delta.to_string()); Ok(()) }).unwrap();
        server.join().unwrap();
        assert_eq!(answer, "Hello");
        assert_eq!(deltas, ["Hello"]);
    }

    #[test]
    fn fake_provider_emits_deltas_and_rejects_empty_success() {
        let provider = FakeProvider::new(vec!["Hello".into(), " world".into()]);
        let mut deltas = Vec::new();
        let answer = provider.complete(&ChatConfig::new("https://example.test/v1".into(), "m".into()).unwrap(), "key", &[], &AtomicBool::new(false), &mut |delta| { deltas.push(delta.to_string()); Ok(()) }).unwrap();
        assert_eq!(deltas, ["Hello", " world"]);
        assert_eq!(answer, "Hello world");
        assert!(FakeProvider::new(Vec::new()).complete(&ChatConfig::new("https://example.test/v1".into(), "m".into()).unwrap(), "key", &[], &AtomicBool::new(false), &mut |_| Ok(())).is_err());
    }

    #[test]
    fn fake_provider_stops_when_consumer_cancels_and_reports_provider_failure() {
        let mut count = 0;
        let result = FakeProvider::new(vec!["one".into(), "two".into()]).complete(&ChatConfig::new("https://example.test/v1".into(), "m".into()).unwrap(), "key", &[], &AtomicBool::new(false), &mut |_| {
            count += 1;
            Err("cancelled".into())
        });
        assert_eq!(result.unwrap_err(), "cancelled");
        assert_eq!(count, 1);
        assert!(FakeProvider::new(Vec::new()).complete(&ChatConfig::new("https://example.test/v1".into(), "m".into()).unwrap(), "key", &[], &AtomicBool::new(false), &mut |_| Ok(())).is_err());
    }

    #[test]
    fn credential_adapter_supports_missing_replace_and_delete_without_persistence_in_config() {
        let store = TestCredentialStore(Mutex::new(None));
        assert_eq!(store.get().unwrap(), None);
        store.set("secret-one").unwrap();
        assert_eq!(store.get().unwrap().as_deref(), Some("secret-one"));
        store.set("secret-two").unwrap();
        assert_eq!(store.get().unwrap().as_deref(), Some("secret-two"));
        let config = ChatConfig::new("https://api.example.test/v1".into(), "model-x".into()).unwrap();
        let encoded = serde_json::to_string(&config).unwrap();
        assert!(!encoded.contains("secret-two"));
        store.delete().unwrap();
        assert_eq!(store.get().unwrap(), None);
    }
}

fn clear_cancellation(runtime: &ChatRuntime, request: &Arc<AtomicBool>) {
    if let Ok(mut active) = runtime.cancellation.lock() {
        if active.as_ref().is_some_and(|current| Arc::ptr_eq(current, request)) {
            *active = None;
            if let Ok(mut session) = runtime.active_chat_session.lock() { *session = None; }
        }
    }
}
