use serde::Serialize;

#[derive(Serialize)]
pub struct ChatStatus {
    pub configured: bool,
}

#[tauri::command]
pub fn desktop_chat_status() -> ChatStatus {
    ChatStatus { configured: false }
}
