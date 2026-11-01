#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

#[cfg(not(feature = "desktop-chat"))]
#[path = "legacy.rs"]
mod legacy;

#[cfg(feature = "desktop-chat")]
mod chat;

#[cfg(not(feature = "desktop-chat"))]
fn main() {
    legacy::main();
}

#[cfg(feature = "desktop-chat")]
fn main() {
    use tauri::Manager;

    let app = tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            chat::desktop_chat_status,
            chat::desktop_chat_configure,
            chat::desktop_chat_delete_credential,
            chat::desktop_chat_create_session,
            chat::desktop_chat_history,
            chat::desktop_chat_latest_session,
            chat::desktop_chat_send,
            chat::desktop_chat_cancel
        ])
        .build(tauri::generate_context!())
        .expect("failed to build chat application");
    let runtime = chat::initialize(&app.handle())
        .expect("failed to initialize desktop chat storage");
    app.manage(runtime);
    app.run(|_app, _event| {});
}