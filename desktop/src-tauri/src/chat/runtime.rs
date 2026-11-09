use super::*;

#[derive(Clone)]
pub struct ChatRuntime {
    pub store: Arc<ChatStore>,
    pub config: Arc<Mutex<Option<ChatConfig>>>,
    pub config_path: PathBuf,
    pub credentials: Arc<dyn CredentialStore>,
    pub generation: Arc<Mutex<Option<Generation>>>,
    pub connection_test_cancellation: Arc<Mutex<Option<Arc<AtomicBool>>>>,
    pub provider: Arc<dyn ChatProvider>,
}

pub struct Generation {
    pub request_id: String,
    pub session_id: String,
    pub cancel: Arc<AtomicBool>,
}

impl ChatRuntime {
    pub fn cancel(&self, session: Option<&str>) -> Result<(), String> {
        if let Some(active) = self
            .generation
            .lock()
            .map_err(|_| "聊天状态不可用")?
            .as_ref()
        {
            if session.is_none_or(|id| id == active.session_id) {
                active.cancel.store(true, Ordering::Relaxed);
            }
        }
        Ok(())
    }

    pub fn send(
        &self,
        input: SendInput,
        emit: impl Fn(&str, serde_json::Value),
    ) -> Result<(), String> {
        let SendInput {
            session_id,
            content,
            request_id,
        } = input;
        if content.trim().is_empty()
            || content.len() > 64 * 1024
            || request_id.is_empty()
            || request_id.len() > 128
        {
            return Err("消息内容为空、过长或请求标识无效".into());
        }
        let config = self
            .config
            .lock()
            .map_err(|_| "聊天配置不可用")?
            .clone()
            .ok_or("请先配置模型")?;
        let key = self
            .credentials
            .get()?
            .filter(|v| !v.is_empty())
            .ok_or("请先配置 API Key")?;
        let cancel = Arc::new(AtomicBool::new(false));
        {
            let mut generation = self.generation.lock().map_err(|_| "聊天状态不可用")?;
            if generation.is_some() {
                return Err("上一条请求仍在结束，请稍后重试".into());
            }
            *generation = Some(Generation {
                request_id: request_id.clone(),
                session_id: session_id.clone(),
                cancel: cancel.clone(),
            });
        }
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            self.store.append_message(&session_id, "user", &content)?;
            let history = self.store.request_context(&session_id, 96 * 1024)?;
            let answer = self.provider.complete(&config, &key, &history, &cancel, &mut |delta| {
                if cancel.load(Ordering::Relaxed) { return Err("请求已取消".into()); }
                emit("desktop_chat_delta", serde_json::json!({"session_id":session_id,"request_id":request_id,"delta":delta}));
                Ok(())
            })?;
            if cancel.load(Ordering::Relaxed) { return Err("请求已取消".into()); }
            self.store.append_message(&session_id, "assistant", &answer)?;
            Ok(())
        })).unwrap_or_else(|_| Err("聊天 Provider 异常".into()));
        let cancelled = cancel.load(Ordering::Relaxed);
        // Clear before notifying so a completed/cancelled UI can safely submit a new generation.
        let mut generation = self.generation.lock().map_err(|_| "聊天状态不可用")?;
        if generation
            .as_ref()
            .is_some_and(|active| active.request_id == request_id)
        {
            *generation = None;
        }
        drop(generation);
        let event = if cancelled {
            "desktop_chat_cancelled"
        } else if result.is_ok() {
            "desktop_chat_completed"
        } else {
            "desktop_chat_failed"
        };
        emit(
            event,
            serde_json::json!({"session_id":session_id,"request_id":request_id,"error":result.as_ref().err()}),
        );
        if cancelled {
            Ok(())
        } else {
            result
        }
    }
}

pub fn initialize(app: &tauri::AppHandle) -> Result<ChatRuntime, String> {
    let root = app
        .path()
        .app_data_dir()
        .map_err(|_| "无法定位聊天数据目录".to_string())?
        .join("TomatoAgent")
        .join("desktop-chat");
    std::fs::create_dir_all(&root).map_err(|_| "无法创建聊天数据目录".to_string())?;
    let store = ChatStore::open(&root.join("chat.sqlite3"))?;
    let credentials: Arc<dyn CredentialStore> = Arc::new(NativeCredentialStore);
    let config_path = root.join("model.json");
    let config = if config_path.exists() {
        let bytes = std::fs::read(&config_path).map_err(|_| "无法读取模型设置".to_string())?;
        let saved: ChatConfig =
            serde_json::from_slice(&bytes).map_err(|_| "模型设置格式无效".to_string())?;
        Some(ChatConfig::new(saved.base_url, saved.model)?)
    } else {
        None
    };
    Ok(ChatRuntime {
        store: Arc::new(store),
        config: Arc::new(Mutex::new(config)),
        config_path,
        credentials,
        provider: Arc::new(OpenAiChatProvider),
        generation: Arc::new(Mutex::new(None)),
        connection_test_cancellation: Arc::new(Mutex::new(None)),
    })
}
