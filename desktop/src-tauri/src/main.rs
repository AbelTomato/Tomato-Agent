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
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![
            chat::desktop_chat_status,
            chat::desktop_chat_pick_project_directory,
            chat::desktop_chat_projects,
            chat::desktop_chat_create_project,
            chat::desktop_chat_rename_project,
            chat::desktop_chat_delete_project,
            chat::desktop_chat_set_session_project,
            chat::desktop_chat_configure,
            chat::desktop_chat_delete_credential,
            chat::desktop_chat_create_session,
            chat::desktop_chat_select_session,
            chat::desktop_chat_sessions,
            chat::desktop_chat_rename_session,
            chat::desktop_chat_delete_session,
            chat::desktop_chat_history,
            chat::desktop_chat_history_page,
            chat::desktop_chat_latest_session,
            chat::desktop_chat_send,
            chat::desktop_chat_cancel,
            chat::desktop_chat_test_connection,
            chat::desktop_chat_cancel_connection_test
        ])
        .build(tauri::generate_context!())
        .expect("failed to build chat application");
    let runtime = chat::initialize(&app.handle())
        .expect("failed to initialize desktop chat storage");
    app.manage(runtime);
    app.run(|_app, _event| {});
}
