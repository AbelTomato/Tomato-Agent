use super::*;

#[derive(Serialize)]
pub struct ChatStatus {
    pub configured: bool,
}

#[tauri::command]
pub fn desktop_chat_status(runtime: tauri::State<'_, ChatRuntime>) -> ChatStatus {
    ChatStatus {
        configured: runtime.config.lock().map(|v| v.is_some()).unwrap_or(false),
    }
}

#[tauri::command]
pub async fn desktop_chat_pick_project_directory(
    app: tauri::AppHandle,
) -> Result<Option<String>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let selected = app
            .dialog()
            .file()
            .blocking_pick_folder()
            .map(|path| {
                path.into_path()
                    .map_err(|_| "所选目录不是本地文件系统路径".to_string())
            })
            .transpose()?;
        selected
            .map(|path| {
                path.into_os_string()
                    .into_string()
                    .map_err(|_| "项目目录路径无法编码".to_string())
            })
            .transpose()
    })
    .await
    .map_err(|_| "目录选择器执行失败".to_string())?
}

#[tauri::command]
pub fn desktop_chat_projects(
    runtime: tauri::State<'_, ChatRuntime>,
) -> Result<Vec<ChatProject>, String> {
    runtime.store.projects()
}

#[tauri::command]
pub fn desktop_chat_create_project(
    runtime: tauri::State<'_, ChatRuntime>,
    input: CreateProjectInput,
) -> Result<ChatProject, String> {
    runtime
        .store
        .create_project(&input.root_path, input.name.as_deref())
}

#[tauri::command]
pub fn desktop_chat_rename_project(
    runtime: tauri::State<'_, ChatRuntime>,
    project_id: String,
    name: String,
) -> Result<(), String> {
    runtime.store.rename_project(&project_id, &name)
}

#[tauri::command]
pub fn desktop_chat_delete_project(
    runtime: tauri::State<'_, ChatRuntime>,
    project_id: String,
) -> Result<(), String> {
    runtime.store.delete_project(&project_id)
}

#[tauri::command]
pub fn desktop_chat_set_session_project(
    runtime: tauri::State<'_, ChatRuntime>,
    input: SetSessionProjectInput,
) -> Result<(), String> {
    runtime
        .store
        .set_session_project(&input.session_id, input.project_id.as_deref())
}

#[tauri::command]
pub fn desktop_chat_configure(
    runtime: tauri::State<'_, ChatRuntime>,
    input: ChatConfigInput,
) -> Result<ChatConfig, String> {
    let config = ChatConfig::new(input.base_url, input.model)?;
    if let Some(key) = input.api_key.filter(|v| !v.is_empty()) {
        runtime.credentials.set(&key)?;
    }
    std::fs::write(
        &runtime.config_path,
        serde_json::to_vec(&config).map_err(|_| "无法编码模型设置".to_string())?,
    )
    .map_err(|_| "无法保存模型设置".to_string())?;
    *runtime
        .config
        .lock()
        .map_err(|_| "聊天配置不可用".to_string())? = Some(config.clone());
    Ok(config)
}

#[tauri::command]
pub fn desktop_chat_delete_credential(
    runtime: tauri::State<'_, ChatRuntime>,
) -> Result<(), String> {
    runtime.credentials.delete()?;
    let _ = std::fs::remove_file(&runtime.config_path);
    *runtime
        .config
        .lock()
        .map_err(|_| "聊天配置不可用".to_string())? = None;
    Ok(())
}

#[tauri::command]
pub fn desktop_chat_create_session(
    runtime: tauri::State<'_, ChatRuntime>,
    project_id: Option<String>,
) -> Result<String, String> {
    match project_id {
        Some(id) => runtime.store.create_session_in_project("新对话", Some(&id)),
        None => runtime.store.create_session("新对话"),
    }
}

#[tauri::command]
pub fn desktop_chat_select_session(
    runtime: tauri::State<'_, ChatRuntime>,
    session_id: String,
) -> Result<(), String> {
    runtime.store.select_session(&session_id)
}

#[tauri::command]
pub fn desktop_chat_sessions(
    runtime: tauri::State<'_, ChatRuntime>,
) -> Result<Vec<ChatSession>, String> {
    runtime.store.sessions()
}

#[tauri::command]
pub fn desktop_chat_rename_session(
    runtime: tauri::State<'_, ChatRuntime>,
    session_id: String,
    title: String,
) -> Result<(), String> {
    runtime.store.rename_session(&session_id, &title)
}

#[tauri::command]
pub fn desktop_chat_delete_session(
    runtime: tauri::State<'_, ChatRuntime>,
    session_id: String,
) -> Result<(), String> {
    runtime.cancel(Some(&session_id))?;
    runtime.store.delete_session(&session_id)
}

#[tauri::command]
pub fn desktop_chat_history(
    runtime: tauri::State<'_, ChatRuntime>,
    session_id: String,
) -> Result<Vec<ChatMessage>, String> {
    runtime.store.messages(&session_id)
}

#[tauri::command]
pub fn desktop_chat_history_page(
    runtime: tauri::State<'_, ChatRuntime>,
    session_id: String,
    offset: u32,
    limit: u32,
) -> Result<Vec<ChatMessage>, String> {
    runtime.store.messages_page(&session_id, offset, limit)
}

#[tauri::command]
pub fn desktop_chat_latest_session(
    runtime: tauri::State<'_, ChatRuntime>,
) -> Result<Option<String>, String> {
    runtime.store.latest_session()
}

#[tauri::command]
pub async fn desktop_chat_send(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, ChatRuntime>,
    input: SendInput,
) -> Result<(), String> {
    let runtime = runtime.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        runtime.send(input, |name, payload| {
            let _ = app.emit(name, payload);
        })
    })
    .await
    .map_err(|_| "聊天运行异常".to_string())?
}

#[tauri::command]
pub async fn desktop_chat_test_connection(
    runtime: tauri::State<'_, ChatRuntime>,
    input: ChatConfigInput,
) -> Result<(), String> {
    let config = ChatConfig::new(input.base_url, input.model)?;
    let key = input
        .api_key
        .filter(|value| !value.is_empty())
        .or(runtime.credentials.get()?)
        .filter(|value| !value.is_empty())
        .ok_or("请先配置 API Key")?;
    let endpoint = format!("{}/chat/completions", config.base_url.trim_end_matches('/'));
    let client = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(std::time::Duration::from_secs(120))
        .build()
        .map_err(|_| "无法初始化 Provider 请求".to_string())?;
    let cancel = Arc::new(AtomicBool::new(false));
    if let Some(previous) = runtime
        .connection_test_cancellation
        .lock()
        .map_err(|_| "聊天状态不可用".to_string())?
        .replace(cancel.clone())
    {
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
    if !result.status().is_success() {
        return Err(format!(
            "Provider 连接失败（HTTP {}）",
            result.status().as_u16()
        ));
    }
    Ok(())
}

#[tauri::command]
pub fn desktop_chat_cancel(runtime: tauri::State<'_, ChatRuntime>) -> Result<(), String> {
    runtime.cancel(None)
}

#[tauri::command]
pub fn desktop_chat_cancel_connection_test(
    runtime: tauri::State<'_, ChatRuntime>,
) -> Result<(), String> {
    if let Some(cancel) = runtime
        .connection_test_cancellation
        .lock()
        .map_err(|_| "聊天状态不可用".to_string())?
        .take()
    {
        cancel.store(true, Ordering::Relaxed);
    }
    Ok(())
}

fn clear_connection_test_cancellation(runtime: &ChatRuntime, request: &Arc<AtomicBool>) {
    if let Ok(mut active) = runtime.connection_test_cancellation.lock() {
        if active
            .as_ref()
            .is_some_and(|current| Arc::ptr_eq(current, request))
        {
            *active = None;
        }
    }
}

#[tauri::command]
pub fn desktop_chat_older_history(
    runtime: tauri::State<'_, ChatRuntime>,
    session_id: String,
    before: String,
) -> Result<HistoryPage, String> {
    runtime
        .store
        .history_before(&session_id, Some(&before), 100)
}
