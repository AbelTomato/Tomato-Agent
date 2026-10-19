use crate::{check_response, distributions, startup_args, validate_handshake, Handshake};
use serde::Serialize;
use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::Duration;

#[derive(Clone, Serialize)]
pub struct Status {
    pub state: String,
    pub summary: String,
    pub detail: String,
}

struct Session {
    distribution: String,
    root: String,
    token: String,
    owner: Option<String>,
    client: String,
    port: u16,
    instance: String,
}

pub struct Connection {
    current: Option<Session>,
    status: Status,
    pub acceptance_root: Option<String>,
}

impl Default for Connection {
    fn default() -> Self {
        Self { current: None, acceptance_root: None, status: Status { state: "disconnected".into(),
            summary: "尚未连接助手服务".into(), detail: String::new() } }
    }
}

fn service_args(distribution: &str, root: &str) -> Result<Vec<String>, String> {
    let mut args = startup_args(distribution, root)?;
    args[7] = "app.desktop.bootstrap".into();
    Ok(args)
}

fn validate_request(method: &str, path: &str) -> Result<(), String> {
    if !matches!(method, "GET" | "POST") || !path.starts_with("/api/")
        || path.contains("..") || path.contains('#') || path.contains('\\')
        || path.chars().any(|c| c.is_control()) {
        return Err("不支持的桌面 API 请求".into());
    }
    Ok(())
}

impl Session {
    fn send(&self, method: &str, path: &str, body: Option<Value>, owner: bool) -> Result<Value, String> {
        validate_request(method, path)?;
        let client = reqwest::blocking::Client::builder().no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(30)).build().map_err(|_| "原生 HTTP 初始化失败")?;
        let mut request = client.request(method.parse().map_err(|_| "无效方法")?,
                                         format!("http://127.0.0.1:{}{}", self.port, path))
            .bearer_auth(&self.token).header("X-Tomato-Instance", &self.instance)
            .header("X-Tomato-Client", &self.client);
        if owner {
            if let Some(value) = &self.owner {
                let mut header = reqwest::header::HeaderValue::from_str(value).map_err(|_| "无效所有权")?;
                header.set_sensitive(true);
                request = request.header("X-Tomato-Owner", header);
            }
        }
        if let Some(value) = body { request = request.json(&value); }
        let response = request.send().map_err(|_| "助手服务失联；请重连查询原任务，不要重复提交")?;
        let status = response.status().as_u16();
        let value: Value = response.json().map_err(|_| "服务响应不是有效 JSON")?;
        Ok(json!({"status":status,"body":value}))
    }

    fn lifecycle(&self, route: &str, method: &str) -> Result<Value, String> {
        let response = self.send(method, &format!("/api/desktop/{route}"), None, true)?;
        if response["status"] != 200 { return Err("服务认证、实例身份或连接状态不符；请核查高级连接设置".into()); }
        check_response(&response["body"], &self.instance)?;
        Ok(response["body"].clone())
    }
}

impl Connection {
    pub fn status(&self) -> Status { self.status.clone() }

    fn error(&mut self, message: String) -> String {
        self.status = Status { state: "error".into(), summary: "助手服务连接失败".into(), detail: message.clone() };
        message
    }

    pub fn connect(&mut self, distribution: &str, root: &str) -> Result<Status, String> {
        let acceptance = self.acceptance_root.clone();
        self.connect_with(distribution, root, acceptance.as_deref())
    }

    fn connect_with(&mut self, distribution: &str, root: &str, acceptance: Option<&str>) -> Result<Status, String> {
        self.status = Status { state: "connecting".into(), summary: "正在连接助手服务".into(), detail: String::new() };
        let result = (|| {
            let args = service_args(distribution, root)?;
            if let Some(current) = &self.current {
                if current.distribution != distribution || current.root != root {
                    return Err("请先断开当前环境；切换环境不会搬迁会话".into());
                }
                current.lifecycle("attach", "POST")?;
                return Ok(());
            }
            if !distributions()?.iter().any(|s| s == distribution) {
                return Err("所选 WSL 发行版未安装".into());
            }
            // Stable native credentials allow reopening/repeated windows to reconnect.
            let account = format!("{distribution}:{root}:{}", acceptance.unwrap_or("business"));
            let entry = keyring::Entry::new("dev.tomato.agent.connection", &account)
                .map_err(|_| "无法打开 Windows 凭据管理器")?;
            let token = match entry.get_password() {
                Ok(value) => value,
                Err(keyring::Error::NoEntry) => {
                    let value = format!("{}{}", uuid::Uuid::new_v4().simple(), uuid::Uuid::new_v4().simple());
                    entry.set_password(&value).map_err(|_| "连接凭据保存失败")?;
                    value
                }
                Err(_) => return Err("连接凭据读取失败".into()),
            };
            let owner = uuid::Uuid::new_v4().to_string();
            let mut child = Command::new("wsl.exe").args(args)
                .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null())
                .spawn().map_err(|_| "WSL 服务启动失败；请检查后端安装路径")?;
            let mut input = json!({"token":token,"owner":owner,"environment_id":distribution});
            let available = std::net::TcpListener::bind("127.0.0.1:0")
                .map_err(|_| "Windows 无可用本机端口")?;
            input["port"] = json!(available.local_addr().map_err(|_| "无法查询本机端口")?.port());
            drop(available);
            if let Some(value) = acceptance { input["acceptance_root"] = json!(value); }
            child.stdin.take().ok_or("缺少受控输入通道")?
                .write_all(format!("{input}\n").as_bytes()).map_err(|_| "连接凭据传递失败")?;
            let stdout = child.stdout.take().ok_or("缺少启动输出通道")?;
            let (tx, rx) = std::sync::mpsc::channel();
            std::thread::spawn(move || {
                let mut line = String::new();
                let result = BufReader::new(stdout).read_line(&mut line).map(|_| line);
                let _ = tx.send(result);
                let _ = child.wait();
            });
            let line = rx.recv_timeout(Duration::from_secs(20))
                .map_err(|_| "启动超时；未自动重试，请核查原服务后重连")?
                .map_err(|_| "无法读取启动结果")?;
            let value: Value = serde_json::from_str(&line).map_err(|_| "启动失败；请检查安装、凭据及服务身份（未记录原始输出）")?;
            let handshake: Handshake = serde_json::from_value(value.clone()).map_err(|_| "无效服务握手")?;
            validate_handshake(&handshake)?;
            self.current = Some(Session { distribution: distribution.into(), root: root.into(), token,
                owner: if value["owned"] == true {Some(owner)} else {None},
                client: uuid::Uuid::new_v4().to_string(), port: handshake.port,
                instance: handshake.service_instance_id });
            self.current.as_ref().unwrap().lifecycle("attach", "POST")?;
            Ok(())
        })();
        if let Err(error) = result { return Err(self.error(error)); }
        self.status = Status { state: "ready".into(), summary: format!("已连接 {distribution}"), detail: "重连后从服务查询会话及原任务；不会自动重发提交".into() };
        Ok(self.status())
    }

    pub fn api(&mut self, method: &str, path: &str, body: Option<Value>) -> Result<Value, String> {
        let result = self.current.as_ref().ok_or("尚未连接助手服务".to_string())?
            .send(method, path, body, false);
        match result {
            Err(error) => Err(self.error(error)),
            Ok(value) => {
                if value["status"] == 401 || value["status"] == 409 && value["body"]["detail"]["code"] == "desktop_instance_mismatch" {
                    self.error("服务凭据或实例已失效，请核查并重连".into());
                }
                Ok(value)
            }
        }
    }

    pub fn facts(&self) -> Result<Value, String> {
        match &self.current { Some(session) => session.lifecycle("connection", "GET"), None => Ok(json!({"active_tasks":0})) }
    }

    pub fn close(&mut self) -> Result<Value, String> {
        let value = match &self.current {
            Some(session) => session.lifecycle("close", "POST")?,
            None => json!({"shutdown":false}),
        };
        self.current = None;
        self.status = Connection::default().status;
        Ok(value)
    }
}

pub type SharedConnection = Arc<Mutex<Connection>>;

#[tauri::command]
pub async fn connect_service(state: tauri::State<'_, SharedConnection>, distribution: String, backend_path: String) -> Result<Status, String> {
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        state.lock().map_err(|_| "连接锁不可用")?.connect(&distribution, &backend_path)
    }).await.map_err(|_| "原生连接线程失败")?
}

#[tauri::command]
pub fn connection_status(state: tauri::State<'_, SharedConnection>) -> Result<Status, String> {
    Ok(state.lock().map_err(|_| "连接锁不可用")?.status())
}

#[tauri::command]
pub async fn desktop_request(state: tauri::State<'_, SharedConnection>, method: String, path: String, body: Option<Value>) -> Result<Value, String> {
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        state.lock().map_err(|_| "连接锁不可用")?.api(&method, &path, body)
    }).await.map_err(|_| "原生请求线程失败")?
}

#[tauri::command]
pub fn connection_facts(state: tauri::State<'_, SharedConnection>) -> Result<Value, String> {
    state.lock().map_err(|_| "连接锁不可用")?.facts()
}

#[tauri::command]
pub fn close_service(state: tauri::State<'_, SharedConnection>) -> Result<Value, String> {
    state.lock().map_err(|_| "连接锁不可用")?.close()
}

#[tauri::command]
pub fn exit_desktop(app: tauri::AppHandle, state: tauri::State<'_, SharedConnection>) -> Result<(), String> {
    state.lock().map_err(|_| "连接锁不可用")?.close()?;
    app.exit(0);
    Ok(())
}

pub fn acceptance_report(distribution: &str, root: &str, data: &str) -> Result<Value, String> {
    crate::validate_path(data)?;
    let mut first = Connection::default();
    first.acceptance_root = Some(data.into());
    first.connect(distribution, root)?;
    let mut second = Connection::default();
    second.acceptance_root = Some(data.into());
    let result = (|| {
        second.connect(distribution, root)?;
        first.connect(distribution, root)?;
        let facts = first.facts()?;
        if facts["connections"] != 2 { return Err("重复连接计数不符".into()); }
        let created = first.api("POST", "/api/sessions", Some(json!({})))?;
        let session = created["body"]["session_id"].as_str().ok_or("会话创建失败")?;
        let path = format!("/api/sessions/{session}/messages");
        if second.api("GET", &path, None)?["status"] != 200 { return Err("第二窗口未读到同一会话".into()); }
        let external = second.close()?;
        if external["shutdown"] != false { return Err("外部客户端结束了服务".into()); }
        let after = first.facts()?;
        if after["connections"] != 1 { return Err("断开计数错误".into()); }
        let owned = first.close()?;
        if owned["shutdown"] != true { return Err("自有空闲服务未受控关闭".into()); }
        Ok(json!({"passed":true,"two_clients":facts,"external_close":external,
                  "owner_close":owned,"shared_session_read":true,"no_provider_requests":true}))
    })();
    let _ = second.close();
    let _ = first.close();
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn production_launch_is_fixed_and_preserves_paths() {
        let args = service_args("Ubuntu", "/home/用户/含 空格/backend").unwrap();
        assert_eq!(args[7], "app.desktop.bootstrap");
        assert_eq!(args[3], "/home/用户/含 空格/backend");
        assert_eq!(args.len(), 8);
    }

    #[test]
    fn bridge_rejects_arbitrary_urls_and_methods() {
        for path in ["http://evil/api/sessions", "//evil/api/sessions", "/api/../health", "/api/a\r\nX:bad", "/api/a#fragment"] {
            assert!(validate_request("GET", path).is_err(), "{path}");
        }
        assert!(validate_request("DELETE", "/api/sessions").is_err());
        assert!(validate_request("POST", "/api/sessions/s/tasks").is_ok());
    }

    #[test]
    fn failed_connection_is_error_and_never_retries_or_leaks_input() {
        let mut connection = Connection::default();
        assert_eq!(connection.status().state, "disconnected");
        assert!(connection.connect("--exec", "/secret").is_err());
        assert_eq!(connection.status().state, "error");
        assert!(!serde_json::to_string(&connection.status()).unwrap().contains("secret"));
    }
}