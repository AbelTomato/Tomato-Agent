use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use std::path::Path;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};
use tauri::Emitter;
use tauri::Manager;

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
             CREATE TABLE IF NOT EXISTS sessions (
                 id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL
             );
             CREATE TABLE IF NOT EXISTS messages (
                 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                 role TEXT NOT NULL CHECK(role IN ('user', 'assistant')), content TEXT NOT NULL,
                 created_at TEXT NOT NULL
             );",
        ).map_err(|_| "无法初始化聊天数据库".to_string())?;
        Ok(Self { connection: Mutex::new(connection) })
    }

    pub fn create_session(&self, title: &str) -> Result<String, String> {
        let id = uuid::Uuid::new_v4().to_string();
        self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?
            .execute("INSERT INTO sessions(id,title,created_at) VALUES(?1,?2,?3)",
                params![id, title, utc_now()]).map_err(|_| "无法创建对话".to_string())?;
        Ok(id)
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
        let connection = self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?;
        let mut statement = connection.prepare(
            "SELECT id,session_id,role,content,created_at FROM messages WHERE session_id=?1 ORDER BY rowid",
        ).map_err(|_| "无法读取消息".to_string())?;
        let rows = statement.query_map([session_id], |row| Ok(ChatMessage {
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
    fn complete(&self, prompt: &str, emit: &mut dyn FnMut(&str) -> Result<(), String>) -> Result<String, String>;
}

pub struct FakeProvider { chunks: Vec<String> }

impl FakeProvider {
    pub fn new(chunks: Vec<String>) -> Self { Self { chunks } }
}

impl ChatProvider for FakeProvider {
    fn complete(&self, _prompt: &str, emit: &mut dyn FnMut(&str) -> Result<(), String>) -> Result<String, String> {
        let mut answer = String::new();
        for chunk in &self.chunks { emit(chunk)?; answer.push_str(chunk); }
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
        credentials, provider: Arc::new(FakeProvider::new(vec!["Fake reply".into()])),
        cancellation: Arc::new(Mutex::new(None)) })
}

#[derive(Serialize)]
pub struct ChatStatus { pub configured: bool }

#[tauri::command]
pub fn desktop_chat_status(runtime: tauri::State<'_, ChatRuntime>) -> ChatStatus {
    ChatStatus { configured: runtime.config.lock().map(|v| v.is_some()).unwrap_or(false) }
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
pub fn desktop_chat_history(runtime: tauri::State<'_, ChatRuntime>, session_id: String) -> Result<Vec<ChatMessage>, String> {
    runtime.store.messages(&session_id)
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
    *runtime.cancellation.lock().map_err(|_| "聊天状态不可用".to_string())? = Some(cancel.clone());
    let provider = runtime.provider.clone();
    let prompt = format!("{}\n{}", config.model, content);
    drop(key);
    let (tx, rx) = std::sync::mpsc::channel::<String>();
    let task_cancel = cancel.clone();
    let worker = std::thread::spawn(move || {
        let mut emit = |delta: &str| { if task_cancel.load(Ordering::Relaxed) { return Err("cancelled".into()); } tx.send(delta.to_string()).map_err(|_| "接收方已关闭".to_string()) };
        provider.complete(&prompt, &mut emit)
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
    if cancel.load(Ordering::Relaxed) { let _ = app.emit("desktop_chat_cancelled", serde_json::json!({"session_id":session_id})); return Ok(()); }
    if let Err(error) = result {
        let _ = app.emit("desktop_chat_failed", serde_json::json!({"session_id":session_id,"error":error}));
        return Err(error);
    }
    runtime.store.append_message(&session_id, "assistant", &answer)?;
    let _ = app.emit("desktop_chat_completed", serde_json::json!({"session_id":session_id}));
    Ok(())
}

#[tauri::command]
pub fn desktop_chat_cancel(runtime: tauri::State<'_, ChatRuntime>) -> Result<(), String> {
    if let Some(cancel) = runtime.cancellation.lock().map_err(|_| "聊天状态不可用".to_string())?.take() { cancel.store(true, Ordering::Relaxed); }
    Ok(())
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
    fn fake_provider_emits_deltas_and_rejects_empty_success() {
        let provider = FakeProvider::new(vec!["Hello".into(), " world".into()]);
        let mut deltas = Vec::new();
        let answer = provider.complete("test", &mut |delta| { deltas.push(delta.to_string()); Ok(()) }).unwrap();
        assert_eq!(deltas, ["Hello", " world"]);
        assert_eq!(answer, "Hello world");
        assert!(FakeProvider::new(Vec::new()).complete("test", &mut |_| Ok(())).is_err());
    }

    #[test]
    fn fake_provider_stops_when_consumer_cancels_and_reports_provider_failure() {
        let mut count = 0;
        let result = FakeProvider::new(vec!["one".into(), "two".into()]).complete("test", &mut |_| {
            count += 1;
            Err("cancelled".into())
        });
        assert_eq!(result.unwrap_err(), "cancelled");
        assert_eq!(count, 1);
        assert!(FakeProvider::new(Vec::new()).complete("test", &mut |_| Ok(())).is_err());
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
