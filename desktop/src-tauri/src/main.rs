#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::{Deserialize, Serialize};
use std::io::{BufRead, BufReader, Write};
use std::process::{Command, Stdio};
use std::time::Duration;

mod lifecycle;
mod chat;

#[derive(Deserialize)]
struct Handshake {
    protocol: u32,
    port: u16,
    service_instance_id: String,
}

#[derive(Debug, Serialize)]
struct ProbeResult {
    health: serde_json::Value,
    identity: serde_json::Value,
    directory: serde_json::Value,
    credential_roundtrip: bool,
    missing_token_rejected: bool,
    wrong_token_rejected: bool,
    wrong_instance_rejected: bool,
}

fn valid_distribution(value: &str) -> bool {
    value.chars().next().is_some_and(|c| c.is_alphanumeric()) && value.len() <= 128
        && value.chars().all(|c| c.is_alphanumeric() || "-_.".contains(c))
}

fn validate_path(value: &str) -> Result<(), String> {
    if !value.starts_with('/') || value.len() > 4096
        || value.chars().any(|c| c.is_control())
        || value.split('/').any(|p| p == "..") {
        return Err("需要不含父级跳转的 WSL 绝对后端路径".into());
    }
    Ok(())
}

fn startup_args(distribution: &str, root: &str) -> Result<Vec<String>, String> {
    if !valid_distribution(distribution) {
        return Err("无效发行版名称".into());
    }
    validate_path(root)?;
    Ok(vec!["--distribution".into(), distribution.into(), "--cd".into(), root.into(),
        "--exec".into(), format!("{}/.venv/bin/python", root.trim_end_matches('/')),
        "-m".into(), "app.desktop.probe".into()])
}

fn validate_handshake(h: &Handshake) -> Result<(), String> {
    if h.protocol != 1 || h.port == 0 || h.service_instance_id.len() != 32
        || !h.service_instance_id.chars().all(|c| c.is_ascii_hexdigit()) {
        return Err("服务协议或身份无效".into());
    }
    Ok(())
}

fn decode_wsl(bytes: &[u8]) -> String {
    if bytes.contains(&0) {
        let words: Vec<u16> = bytes.chunks_exact(2)
            .map(|c| u16::from_le_bytes([c[0], c[1]])).collect();
        String::from_utf16_lossy(&words)
    } else { String::from_utf8_lossy(bytes).into_owned() }
}

#[tauri::command]
fn distributions() -> Result<Vec<String>, String> {
    let result = Command::new("wsl.exe").args(["--list", "--quiet"])
        .output().map_err(|_| "无法运行 wsl.exe".to_string())?;
    if !result.status.success() { return Err("WSL 发现失败".into()); }
    Ok(decode_wsl(&result.stdout).lines().map(|s| s.trim().trim_start_matches('\u{feff}'))
        .filter(|s| valid_distribution(s)).map(String::from).collect())
}

fn request(client: &reqwest::blocking::Client, port: u16, token: &str,
           instance: &str, route: &str) -> reqwest::blocking::RequestBuilder {
    client.get(format!("http://127.0.0.1:{port}/{route}"))
        .bearer_auth(token).header("X-Tomato-Instance", instance)
}

fn check_response(value: &serde_json::Value, instance: &str) -> Result<(), String> {
    if value["protocol"] != 1 || value["service_instance_id"] != instance {
        return Err("目标端口并非预期的服务实例".into());
    }
    Ok(())
}

fn run_probe(distribution: &str, backend_path: &str) -> Result<ProbeResult, String> {
    let args = startup_args(distribution, backend_path)?;
    if !distributions()?.iter().any(|s| s == distribution) {
        return Err("发行版不在已安装清单中".into());
    }
    let token = format!("{}{}", uuid::Uuid::new_v4().simple(), uuid::Uuid::new_v4().simple());
    let entry = keyring::Entry::new("dev.tomato.agent.probe", &uuid::Uuid::new_v4().to_string())
        .map_err(|_| "无法打开 Windows 凭据管理器".to_string())?;
    entry.set_password(&token).map_err(|_| "原生凭据保存失败".to_string())?;
    let outcome = (|| {
        let restored = entry.get_password().map_err(|_| "原生凭据读取失败".to_string())?;
        if restored != token { return Err("原生凭据往返不一致".into()); }
        let mut child = Command::new("wsl.exe").args(&args)
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null())
            .spawn().map_err(|_| "WSL 服务启动失败".to_string())?;
        child.stdin.take().ok_or("服务输入通道缺失")?.write_all(format!("{token}\n").as_bytes())
            .map_err(|_| "服务凭据传递失败".to_string())?;
        let stdout = child.stdout.take().ok_or("服务输出通道缺失")?;
        let (tx, rx) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            let mut line = String::new();
            let result = BufReader::new(stdout).read_line(&mut line).map(|_| line);
            let _ = tx.send(result);
        });
        let line = rx.recv_timeout(Duration::from_secs(20))
            .map_err(|_| "WSL 启动超时（探针最多运行120秒）".to_string())?
            .map_err(|_| "WSL 启动响应读取失败".to_string())?;
        let handshake: Handshake = serde_json::from_str(&line)
            .map_err(|_| "服务返回无效握手（未记录原始输出）".to_string())?;
        validate_handshake(&handshake)?;
        let client = reqwest::blocking::Client::builder().no_proxy()
            .timeout(Duration::from_secs(5)).build().map_err(|_| "HTTP 初始化失败".to_string())?;
        let mut values = Vec::new();
        for route in ["health", "identity", "directory"] {
            let response = request(&client, handshake.port, &token, &handshake.service_instance_id, route)
                .send().and_then(|r| r.error_for_status())
                .map_err(|_| "Windows 到 WSL 的认证连接失败".to_string())?;
            let value: serde_json::Value = response.json().map_err(|_| "服务响应无效".to_string())?;
            if route != "directory" { check_response(&value, &handshake.service_instance_id)?; }
            values.push(value);
        }
        let status = |token: &str, instance: &str| -> Result<u16, String> {
            request(&client, handshake.port, token, instance, "health").send()
                .map(|r| r.status().as_u16()).map_err(|_| "认证反例检查失败".to_string())
        };
        Ok(ProbeResult { health: values.remove(0), identity: values.remove(0), directory: values.remove(0),
            credential_roundtrip: true, missing_token_rejected: status("", &handshake.service_instance_id)? == 401,
            wrong_token_rejected: status("wrong", &handshake.service_instance_id)? == 401,
            wrong_instance_rejected: status(&token, "wrong")? == 409 })
    })();
    let cleanup = entry.delete_credential().map_err(|_| "临时凭据清理失败".to_string());
    cleanup?;
    outcome
}

#[tauri::command]
async fn probe(distribution: String, backend_path: String) -> Result<ProbeResult, String> {
    tauri::async_runtime::spawn_blocking(move || run_probe(&distribution, &backend_path))
        .await.map_err(|_| "原生验证线程失败".to_string())?
}

fn main() {
    use tauri::{Emitter, Manager};
    let args: Vec<String> = std::env::args().collect();
    if args.get(1).map(String::as_str) == Some("--lifecycle-report") {
        if args.len() != 6 { std::process::exit(2); }
        let result = lifecycle::acceptance_report(&args[2], &args[3], &args[4]);
        let ok = result.is_ok();
        let report = match result { Ok(value) => value, Err(error) => serde_json::json!({"passed":false,"error":error}) };
        if std::fs::write(&args[5], serde_json::to_vec_pretty(&report).unwrap()).is_err() { std::process::exit(3); }
        std::process::exit(if ok {0} else {1});
    }
    if args.get(1).map(String::as_str) == Some("--probe-report") {
        if args.len() != 5 { std::process::exit(2); }
        let result = run_probe(&args[2], &args[3]);
        let ok = result.as_ref().is_ok_and(|r| r.credential_roundtrip && r.missing_token_rejected
            && r.wrong_token_rejected && r.wrong_instance_rejected);
        let report = match result {
            Ok(value) => serde_json::json!({"passed":ok,"result":value}),
            Err(error) => serde_json::json!({"passed":false,"error":error}),
        };
        if std::fs::write(&args[4], serde_json::to_vec_pretty(&report).unwrap()).is_err() {
            std::process::exit(3);
        }
        std::process::exit(if ok {0} else {1});
    }
    let connection = lifecycle::SharedConnection::default();
    if args.get(1).map(String::as_str) == Some("--acceptance-data") {
        let path = args.get(2).expect("acceptance data path required");
        validate_path(path).expect("absolute WSL acceptance path required");
        connection.lock().unwrap().acceptance_root = Some(path.clone());
    }
    tauri::Builder::default()
        .manage(connection)
        .invoke_handler(tauri::generate_handler![chat::desktop_chat_status, distributions, probe,
            lifecycle::connect_service, lifecycle::connection_status, lifecycle::desktop_request,
            lifecycle::connection_facts, lifecycle::close_service, lifecycle::exit_desktop])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.emit("desktop-close-requested", ());
            }
        })
        .setup(|app| {
            let _ = app.state::<lifecycle::SharedConnection>();
            Ok(())
        })
        .run(tauri::generate_context!()).expect("desktop startup failed");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn paths_are_single_argv_values() {
        let args = startup_args("Ubuntu", "/home/用户/含 空格/backend").unwrap();
        assert_eq!(args[3], "/home/用户/含 空格/backend");
        assert_eq!(args[5], "/home/用户/含 空格/backend/.venv/bin/python");
        assert_eq!(args.len(), 8);
    }

    #[test]
    fn rejects_malicious_distribution_and_paths() {
        for name in ["--exec", "Ubuntu;whoami", "Ubuntu\n", "$(id)", ""] {
            assert!(startup_args(name, "/backend").is_err(), "{name}");
        }
        for path in ["relative", "/a/../b", "/a\nb"] {
            assert!(startup_args("Ubuntu", path).is_err());
        }
    }

    #[test]
    fn rejects_unknown_protocol_and_invalid_startup_identity() {
        for (protocol, port, id) in [(2, 8000, "a".repeat(32)), (1, 0, "a".repeat(32)), (1, 8000, "bad".into())] {
            assert!(validate_handshake(&Handshake { protocol, port, service_instance_id: id }).is_err());
        }
    }

    #[test]
    fn occupied_port_cannot_impersonate_instance() {
        assert!(check_response(&serde_json::json!({"protocol":1,"service_instance_id":"other"}), "expected").is_err());
    }

    #[test]
    fn occupied_loopback_port_rejects_another_service_identity() {
        use std::io::Read;
        use std::net::TcpListener;

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        assert!(TcpListener::bind(address).is_err());
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
            let mut buffer = [0; 4096];
            let _ = stream.read(&mut buffer).unwrap();
            let body = r#"{"protocol":1,"service_instance_id":"unrelated-service"}"#;
            write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}", body.len(), body).unwrap();
        });
        let client = reqwest::blocking::Client::builder().no_proxy()
            .timeout(Duration::from_secs(5)).build().unwrap();
        let response: serde_json::Value = request(&client, address.port(), "probe-token", "expected", "health")
            .send().unwrap().error_for_status().unwrap().json().unwrap();
        assert!(check_response(&response, "expected").is_err());
        server.join().unwrap();
    }

    #[test]
    fn credential_header_is_sensitive_and_absent_from_request_url() {
        let client = reqwest::blocking::Client::new();
        let built = request(&client, 12345, "private-test-token", "instance", "health")
            .build().unwrap();
        assert!(built.headers()[reqwest::header::AUTHORIZATION].is_sensitive());
        assert!(!built.url().as_str().contains("private-test-token"));
        assert!(!format!("{built:?}").contains("private-test-token"));
    }

    #[test]
    fn decodes_windows_wsl_output() {
        let bytes: Vec<u8> = "Ubuntu\r\n".encode_utf16().flat_map(u16::to_le_bytes).collect();
        assert_eq!(decode_wsl(&bytes), "Ubuntu\r\n");
    }

    #[test]
    fn startup_failure_is_reported_without_raw_output() {
        let result = run_probe("Tomato-Missing-Distribution-For-Test", "/does not exist/后端");
        assert!(result.is_err());
        assert!(!result.unwrap_err().contains("Bearer"));
    }

    #[test]
    fn argv_contains_no_credentials_or_shell() {
        let args = startup_args("Ubuntu", "/backend; literal $(text)").unwrap();
        assert!(!args.iter().any(|s| s == "sh" || s == "-c" || s.contains("Bearer")));
        assert_eq!(args[3], "/backend; literal $(text)");
    }
}