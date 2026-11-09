#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod chat;

mod engine;

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
            chat::desktop_chat_older_history,
            chat::desktop_chat_latest_session,
            chat::desktop_chat_send,
            chat::desktop_chat_cancel,
            chat::desktop_chat_test_connection,
            chat::desktop_chat_cancel_connection_test,
            engine::desktop_agent_start,
            engine::desktop_agent_status,
            engine::desktop_agent_submit_task,
            engine::desktop_agent_session_tasks,
            engine::desktop_agent_task_status,
            engine::desktop_agent_task_action,
            engine::desktop_agent_task_events,
            engine::desktop_agent_workspaces,
            engine::desktop_agent_register_workspace,
            engine::desktop_agent_stop
        ])
        .build(tauri::generate_context!())
        .expect("failed to build desktop application");
    let runtime =
        chat::initialize(&app.handle()).expect("failed to initialize desktop chat storage");
    app.manage(runtime);
    app.manage(std::sync::Arc::new(
        engine::EngineManager::new().expect("failed to initialize agent engine manager"),
    ));
    let arguments = std::env::args().skip(1).collect::<Vec<_>>();
    if arguments
        .first()
        .is_some_and(|argument| argument == "--engine-probe-report")
    {
        let Some(output) = arguments.get(1) else {
            std::process::exit(2);
        };
        let Some(engine) = app.try_state::<std::sync::Arc<engine::EngineManager>>() else {
            std::process::exit(2);
        };
        let result = engine::write_probe_report(
            &app.handle(),
            engine.as_ref(),
            &std::path::PathBuf::from(output),
        );
        if let Err(error) = result {
            eprintln!("Engine probe failed: {error}");
            let _ = engine.stop();
            std::process::exit(1);
        }
        std::process::exit(0);
    }
    if arguments.first().is_some_and(|argument| argument == "--desktop-task-probe-report") {
        let Some(output) = arguments.get(1) else { std::process::exit(2); };
        let Some(engine) = app.try_state::<std::sync::Arc<engine::EngineManager>>() else {
            std::process::exit(2);
        };
        let Some(chat) = app.try_state::<chat::ChatRuntime>() else { std::process::exit(2); };
        let result = engine::write_task_probe_report(&app.handle(), engine.as_ref(), chat.inner(),
            &std::path::PathBuf::from(output));
        if let Err(error) = result {
            eprintln!("Desktop task probe failed: {error}");
            let _ = engine.stop();
            std::process::exit(1);
        }
        std::process::exit(0);
    }
    if arguments.first().is_some_and(|argument| argument == "--desktop-writing-probe-report") {
        let Some(output) = arguments.get(1) else { std::process::exit(2); };
        let Some(engine) = app.try_state::<std::sync::Arc<engine::EngineManager>>() else {
            std::process::exit(2);
        };
        let Some(chat) = app.try_state::<chat::ChatRuntime>() else { std::process::exit(2); };
        let result = engine::write_writing_probe_report(&app.handle(), engine.as_ref(), chat.inner(),
            &std::path::PathBuf::from(output));
        if let Err(error) = result {
            eprintln!("Desktop writing probe failed: {error}");
            let _ = engine.stop();
            std::process::exit(1);
        }
        std::process::exit(0);
    }
    if arguments.first().is_some_and(|argument| argument == "--desktop-cancel-probe-report") {
        let Some(output) = arguments.get(1) else { std::process::exit(2); };
        let Some(engine) = app.try_state::<std::sync::Arc<engine::EngineManager>>() else {
            std::process::exit(2);
        };
        let Some(chat) = app.try_state::<chat::ChatRuntime>() else { std::process::exit(2); };
        let result = engine::write_cancel_probe_report(&app.handle(), engine.as_ref(), chat.inner(),
            &std::path::PathBuf::from(output));
        if let Err(error) = result {
            eprintln!("Desktop cancel probe failed: {error}");
            let _ = engine.stop();
            std::process::exit(1);
        }
        std::process::exit(0);
    }
    if arguments.first().is_some_and(|argument| argument == "--desktop-project-isolation-probe-report") {
        let Some(output) = arguments.get(1) else { std::process::exit(2); };
        let Some(engine) = app.try_state::<std::sync::Arc<engine::EngineManager>>() else {
            std::process::exit(2);
        };
        let Some(chat) = app.try_state::<chat::ChatRuntime>() else { std::process::exit(2); };
        let result = engine::write_project_isolation_probe_report(&app.handle(), engine.as_ref(), chat.inner(),
            &std::path::PathBuf::from(output));
        if let Err(error) = result {
            eprintln!("Desktop project isolation probe failed: {error}");
            let _ = engine.stop();
            std::process::exit(1);
        }
        std::process::exit(0);
    }
    if arguments.first().is_some_and(|argument| argument == "--desktop-task-recovery-probe-report") {
        let Some(output) = arguments.get(1) else { std::process::exit(2); };
        let Some(engine) = app.try_state::<std::sync::Arc<engine::EngineManager>>() else {
            std::process::exit(2);
        };
        let Some(chat) = app.try_state::<chat::ChatRuntime>() else { std::process::exit(2); };
        let result = engine::write_task_recovery_probe_report(&app.handle(), engine.as_ref(), chat.inner(),
            &std::path::PathBuf::from(output));
        if let Err(error) = result {
            eprintln!("Desktop task recovery probe failed: {error}");
            let _ = engine.stop();
            std::process::exit(1);
        }
        std::process::exit(0);
    }
    app.run(|app, event| {
        if let tauri::RunEvent::Exit = event {
            if let Some(engine) = app.try_state::<std::sync::Arc<engine::EngineManager>>() {
                let _ = engine.stop();
            }
        }
    });
}
