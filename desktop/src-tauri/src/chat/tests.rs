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
    assert_eq!(
        store.latest_session().unwrap().as_deref(),
        Some(session_id.as_str())
    );
    let messages = store.messages(&session_id).unwrap();
    assert_eq!(messages.len(), 1);
    assert_eq!(messages[0].content, "hello");
    assert!(messages[0].created_at.ends_with('Z'));
    let _ = std::fs::remove_file(path);
}

#[test]
fn engine_session_mapping_and_task_messages_survive_reopening_idempotently() {
    let path = std::env::temp_dir().join(format!("desktop-engine-map-{}.db", uuid::Uuid::new_v4()));
    let desktop_session;
    {
        let store = ChatStore::open(&path).unwrap();
        desktop_session = store.create_session("Task session").unwrap();
        store.bind_engine_session(&desktop_session, "engine-session").unwrap();
        store.append_message_with_id("task-run-user", &desktop_session, "user", "run this").unwrap();
        store.append_message_with_id("task-run-user", &desktop_session, "user", "duplicate").unwrap();
    }
    let store = ChatStore::open(&path).unwrap();
    assert_eq!(store.engine_session(&desktop_session).unwrap().as_deref(), Some("engine-session"));
    let messages = store.messages(&desktop_session).unwrap();
    assert_eq!(messages.len(), 1);
    assert_eq!(messages[0].content, "run this");
    let _ = std::fs::remove_file(path);
}

#[test]
fn projects_group_sessions_without_deleting_history_or_directories() {
    let root = std::env::temp_dir().join(format!("desktop-chat-project-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&root).unwrap();
    let db = root.join("chat.sqlite3");
    let store = ChatStore::open(&db).unwrap();
    let session = store.create_session("Conversation").unwrap();
    let project = store
        .create_project(root.to_str().unwrap(), Some("Workspace"))
        .unwrap();
    assert!(store
        .create_project(root.to_str().unwrap(), Some("Duplicate"))
        .is_err());
    assert!(store
        .create_project("relative/path", Some("Invalid"))
        .is_err());
    assert!(store
        .set_session_project(&session, Some("missing-project"))
        .is_err());
    store.append_message(&session, "user", "Keep me").unwrap();
    assert_eq!(
        store.projects().unwrap()[0].root_path,
        normalize_project_path(root.to_str().unwrap()).unwrap()
    );
    store
        .set_session_project(&session, Some(&project.id))
        .unwrap();
    assert_eq!(
        store.sessions().unwrap()[0].project_id.as_deref(),
        Some(project.id.as_str())
    );
    store.rename_project(&project.id, "Renamed").unwrap();
    store.delete_project(&project.id).unwrap();
    assert!(store.sessions().unwrap()[0].project_id.is_none());
    assert_eq!(store.messages(&session).unwrap().len(), 1);
    assert!(root.is_dir());
    let _ = std::fs::remove_dir_all(root);
}

#[test]
fn legacy_sessions_are_migrated_without_project_assignment() {
    let path =
        std::env::temp_dir().join(format!("desktop-chat-legacy-{}.db", uuid::Uuid::new_v4()));
    {
        let connection = Connection::open(&path).unwrap();
        connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL)", []).unwrap();
        connection
            .execute(
                "INSERT INTO sessions VALUES ('legacy', 'Legacy', '2026-01-01T00:00:00Z')",
                [],
            )
            .unwrap();
        connection.execute_batch("CREATE TABLE messages (id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL);
                INSERT INTO messages VALUES ('old-message','legacy','user','old history','2026-01-01T00:00:00Z');").unwrap();
    }
    for _ in 0..2 {
        let store = ChatStore::open(&path).unwrap();
        let session = store.sessions().unwrap();
        assert_eq!(session.len(), 1);
        assert!(session[0].project_id.is_none());
        assert!(store.projects().unwrap().is_empty());
        assert_eq!(store.messages("legacy").unwrap()[0].content, "old history");
    }
    let _ = std::fs::remove_file(path);
}

#[test]
fn projects_selection_and_history_survive_repeated_reopen_and_directory_removal() {
    let root = std::env::temp_dir().join(format!("desktop-chat-reopen-{}", uuid::Uuid::new_v4()));
    let directory = root.join("项目 with spaces");
    std::fs::create_dir_all(&directory).unwrap();
    let db = root.join("chat.sqlite3");
    let (project_id, selected_id, other_id);
    {
        let store = ChatStore::open(&db).unwrap();
        let project = store
            .create_project(directory.to_str().unwrap(), None)
            .unwrap();
        project_id = project.id;
        assert_eq!(project.name, "项目 with spaces");
        selected_id = store
            .create_session_in_project("Selected", Some(&project_id))
            .unwrap();
        store
            .append_message(&selected_id, "user", "persistent message")
            .unwrap();
        other_id = store.create_session("Independent").unwrap();
        store.select_session(&selected_id).unwrap();
        assert!(store.select_session("missing").is_err());
        assert!(store
            .create_session_in_project("Invalid", Some("missing"))
            .is_err());
        assert_eq!(store.sessions().unwrap().len(), 2);
    }
    for _ in 0..2 {
        let store = ChatStore::open(&db).unwrap();
        assert_eq!(store.latest_session().unwrap(), Some(selected_id.clone()));
        assert_eq!(
            store.messages(&selected_id).unwrap()[0].content,
            "persistent message"
        );
        assert_eq!(
            store
                .sessions()
                .unwrap()
                .iter()
                .find(|s| s.id == selected_id)
                .unwrap()
                .project_id
                .as_deref(),
            Some(project_id.as_str())
        );
        assert!(store.projects().unwrap()[0].available);
    }
    std::fs::remove_dir(&directory).unwrap();
    {
        let store = ChatStore::open(&db).unwrap();
        assert!(!store.projects().unwrap()[0].available);
        store.set_session_project(&selected_id, None).unwrap();
        assert!(store
            .sessions()
            .unwrap()
            .iter()
            .find(|s| s.id == selected_id)
            .unwrap()
            .project_id
            .is_none());
        store
            .set_session_project(&selected_id, Some(&project_id))
            .unwrap();
        store.delete_project(&project_id).unwrap();
        assert_eq!(store.messages(&selected_id).unwrap().len(), 1);
        store.delete_session(&selected_id).unwrap();
        assert_eq!(store.latest_session().unwrap(), Some(other_id));
    }
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn project_paths_reject_files_missing_relative_and_parent_paths() {
    let root = std::env::temp_dir().join(format!("desktop-chat-path-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&root).unwrap();
    let file = root.join("file.txt");
    std::fs::write(&file, "keep").unwrap();
    for value in [
        "",
        "relative/path",
        "bad\npath",
        file.to_str().unwrap(),
        root.join("missing").to_str().unwrap(),
        root.join("..").to_str().unwrap(),
    ] {
        assert!(
            normalize_project_path(value).is_err(),
            "accepted invalid path: {value}"
        );
    }
    #[cfg(windows)]
    {
        assert!(normalize_project_path(r"\\localhost\C$\Windows").is_err());
        let db = root.join("chat.sqlite3");
        let store = ChatStore::open(&db).unwrap();
        store.create_project(root.to_str().unwrap(), None).unwrap();
        assert!(store
            .create_project(&root.to_str().unwrap().to_uppercase(), None)
            .is_err());
        drop(store);
    }
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn sessions_can_be_listed_renamed_and_deleted_with_cascading_history() {
    let path = std::env::temp_dir().join(format!("desktop-chat-crud-{}.db", uuid::Uuid::new_v4()));
    let store = ChatStore::open(&path).unwrap();
    let first = store.create_session("First").unwrap();
    let second = store.create_session("Second").unwrap();
    store
        .append_message(&first, "user", "kept private")
        .unwrap();
    assert_eq!(store.sessions().unwrap().len(), 2);
    store.rename_session(&first, "Renamed").unwrap();
    assert_eq!(
        store
            .sessions()
            .unwrap()
            .iter()
            .find(|s| s.id == first)
            .unwrap()
            .title,
        "Renamed"
    );
    store.delete_session(&first).unwrap();
    assert!(store.messages(&first).unwrap().is_empty());
    assert_eq!(
        store
            .sessions()
            .unwrap()
            .iter()
            .map(|s| s.id.as_str())
            .collect::<Vec<_>>(),
        [second.as_str()]
    );
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
    for index in 0..4 {
        store
            .append_message(&id, "user", &index.to_string())
            .unwrap();
    }
    let page = store.messages_page(&id, 1, 2).unwrap();
    assert_eq!(
        page.iter()
            .map(|message| message.content.as_str())
            .collect::<Vec<_>>(),
        ["1", "2"]
    );
    assert_eq!(store.messages_page(&id, 0, 900).unwrap().len(), 4);
    let _ = std::fs::remove_file(path);
}

#[test]
fn sse_parser_preserves_utf8_and_delta_boundaries_and_rejects_incomplete_stream() {
    let input = b"data: {\"choices\":[{\"delta\":{\"content\":\"\xe4\xbd\xa0\"}}]}\r\n\r\ndata: {\"choices\":[{\"delta\":{\"content\":\"hello\"}}]}\n\ndata: [DONE]\n\n";
    let mut parser = SseParser::default();
    let mut result = Vec::new();
    for part in input.chunks(7) {
        result.extend(parser.push(part).unwrap());
    }
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
        assert!(request
            .to_ascii_lowercase()
            .contains("authorization: bearer test-key"));
        assert!(request.contains("current session"));
        assert!(!request.contains("other session"));
        let body =
            "data: {\"choices\":[{\"delta\":{\"content\":\"Hello\"}}]}\r\n\r\ndata: [DONE]\r\n\r\n";
        write!(socket, "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}", body.len(), body).unwrap();
    });
    let config = ChatConfig::new(format!("http://{address}/v1"), "test-model".into()).unwrap();
    let history = vec![ChatMessage {
        id: "1".into(),
        session_id: "s1".into(),
        role: "user".into(),
        content: "current session".into(),
        created_at: utc_now(),
    }];
    let mut deltas = Vec::new();
    let answer = OpenAiChatProvider
        .complete(
            &config,
            "test-key",
            &history,
            &AtomicBool::new(false),
            &mut |delta| {
                deltas.push(delta.to_string());
                Ok(())
            },
        )
        .unwrap();
    server.join().unwrap();
    assert_eq!(answer, "Hello");
    assert_eq!(deltas, ["Hello"]);
}

#[test]
fn fake_provider_emits_deltas_and_rejects_empty_success() {
    let provider = FakeProvider::new(vec!["Hello".into(), " world".into()]);
    let mut deltas = Vec::new();
    let answer = provider
        .complete(
            &ChatConfig::new("https://example.test/v1".into(), "m".into()).unwrap(),
            "key",
            &[],
            &AtomicBool::new(false),
            &mut |delta| {
                deltas.push(delta.to_string());
                Ok(())
            },
        )
        .unwrap();
    assert_eq!(deltas, ["Hello", " world"]);
    assert_eq!(answer, "Hello world");
    assert!(FakeProvider::new(Vec::new())
        .complete(
            &ChatConfig::new("https://example.test/v1".into(), "m".into()).unwrap(),
            "key",
            &[],
            &AtomicBool::new(false),
            &mut |_| Ok(())
        )
        .is_err());
}

#[test]
fn fake_provider_stops_when_consumer_cancels_and_reports_provider_failure() {
    let mut count = 0;
    let result = FakeProvider::new(vec!["one".into(), "two".into()]).complete(
        &ChatConfig::new("https://example.test/v1".into(), "m".into()).unwrap(),
        "key",
        &[],
        &AtomicBool::new(false),
        &mut |_| {
            count += 1;
            Err("cancelled".into())
        },
    );
    assert_eq!(result.unwrap_err(), "cancelled");
    assert_eq!(count, 1);
    assert!(FakeProvider::new(Vec::new())
        .complete(
            &ChatConfig::new("https://example.test/v1".into(), "m".into()).unwrap(),
            "key",
            &[],
            &AtomicBool::new(false),
            &mut |_| Ok(())
        )
        .is_err());
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

#[test]
fn latest_display_pages_and_model_input_keep_question_after_five_hundred_messages() {
    let store = ChatStore::open(Path::new(":memory:")).unwrap();
    let id = store.create_session("Long conversation").unwrap();
    for n in 0..510 {
        store
            .append_message(
                &id,
                if n % 2 == 0 { "user" } else { "assistant" },
                &format!("turn {n}"),
            )
            .unwrap();
    }
    store
        .append_message(&id, "user", "LATEST QUESTION")
        .unwrap();
    let latest = store.messages(&id).unwrap();
    assert_eq!(latest.len(), 100);
    assert_eq!(latest.last().unwrap().content, "LATEST QUESTION");
    let older = store.history_before(&id, Some(&latest[0].id), 100).unwrap();
    assert!(older.has_more);
    assert!(!older
        .messages
        .iter()
        .any(|a| latest.iter().any(|b| a.id == b.id)));
    let context = store.request_context(&id, 600).unwrap();
    assert_eq!(context.last().unwrap().content, "LATEST QUESTION");
    assert_eq!(context.first().unwrap().role, "user");
    assert!(context.iter().map(|m| m.content.len() + 64).sum::<usize>() <= 600);
    assert!(store.request_context(&id, 1).is_err());
    let other = store.create_session("Other").unwrap();
    assert!(store
        .history_before(&other, Some(&latest[0].id), 100)
        .unwrap()
        .messages
        .is_empty());
}

fn test_runtime(provider: Arc<dyn ChatProvider>) -> ChatRuntime {
    ChatRuntime {
        store: Arc::new(ChatStore::open(Path::new(":memory:")).unwrap()),
        config: Arc::new(Mutex::new(Some(
            ChatConfig::new("https://example.test/v1".into(), "test".into()).unwrap(),
        ))),
        config_path: PathBuf::new(),
        credentials: Arc::new(TestCredentialStore(Mutex::new(Some("test-key".into())))),
        generation: Arc::new(Mutex::new(None)),
        connection_test_cancellation: Arc::new(Mutex::new(None)),
        provider,
    }
}

#[test]
fn generation_events_are_correlated_and_cancelled_partial_output_is_not_committed() {
    let runtime = test_runtime(Arc::new(FakeProvider::new(vec![
        "partial".into(),
        "late".into(),
    ])));
    let id = runtime.store.create_session("Cancel").unwrap();
    let events = Mutex::new(Vec::new());
    runtime
        .send(
            SendInput {
                session_id: id.clone(),
                content: "hello".into(),
                request_id: "generation-1".into(),
            },
            |name, payload| {
                assert_eq!(payload["request_id"], "generation-1");
                assert_eq!(payload["session_id"], id);
                events.lock().unwrap().push(name.to_string());
                if name == "desktop_chat_delta" {
                    runtime.cancel(None).unwrap();
                }
            },
        )
        .unwrap();
    assert_eq!(
        *events.lock().unwrap(),
        ["desktop_chat_delta", "desktop_chat_cancelled"]
    );
    assert_eq!(runtime.store.messages(&id).unwrap().len(), 1);
    assert!(runtime.generation.lock().unwrap().is_none());
    runtime
        .send(
            SendInput {
                session_id: id.clone(),
                content: "again".into(),
                request_id: "generation-2".into(),
            },
            |_, _| {},
        )
        .unwrap();
    assert_eq!(
        runtime.store.messages(&id).unwrap().last().unwrap().content,
        "partiallate"
    );
}

#[test]
fn provider_failure_records_no_success_and_releases_generation() {
    let runtime = test_runtime(Arc::new(FakeProvider::new(vec![])));
    let id = runtime.store.create_session("Failure").unwrap();
    let events = Mutex::new(Vec::new());
    assert!(runtime
        .send(
            SendInput {
                session_id: id.clone(),
                content: "hello".into(),
                request_id: "failed".into()
            },
            |name, _| {
                events.lock().unwrap().push(name.to_string());
            }
        )
        .is_err());
    assert_eq!(*events.lock().unwrap(), ["desktop_chat_failed"]);
    assert_eq!(runtime.store.messages(&id).unwrap().len(), 1);
    assert!(runtime.generation.lock().unwrap().is_none());
}
