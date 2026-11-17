use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{HashMap, HashSet};
use std::fs::{self, File, Metadata};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

pub const DESKTOP_BRIDGE_PROTOCOL_VERSION: u32 = 1;
pub const MAX_BRIDGE_MESSAGE_BYTES: usize = 1_048_576;
const MAX_CLOCK_SKEW_MS: u64 = 30_000;
const MAX_RECENT_NONCES: usize = 4096;
const MAX_GRANT_LIFETIME_MS: u64 = 4 * 60 * 60 * 1000;
const MAX_LIST_ENTRIES: usize = 200;
const MAX_LIST_DEPTH: usize = 4;
const MAX_SEARCH_RESULTS: usize = 100;
const MAX_SEARCH_BYTES: usize = 1_048_576;
const MAX_READ_BYTES: usize = 128 * 1024;
const MAX_PATH_BYTES: usize = 4096;
const MAX_APPROVAL_LIFETIME_MS: u64 = 2 * 60 * 1000;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct BridgeRequest {
    pub protocol_version: u32,
    pub instance_id: String,
    pub session_id: String,
    pub run_id: String,
    pub project_id: String,
    pub request_id: String,
    pub nonce: String,
    pub timestamp_ms: u64,
    pub authorization: String,
    pub operation: String,
    pub grant: String,
    pub payload: Value,
}

#[derive(Debug, Serialize)]
#[serde(deny_unknown_fields)]
pub struct BridgeResponse {
    pub protocol_version: u32,
    pub instance_id: String,
    pub request_id: String,
    pub status: String,
    pub result: Option<Value>,
    pub error_code: Option<String>,
}

pub struct BridgeServer {
    port: u16,
    instance_id: Arc<Mutex<String>>,
    stopped: Arc<AtomicBool>,
    broker: Arc<Mutex<WorkspaceBroker>>,
    worker: Option<JoinHandle<()>>,
}

impl BridgeServer {
    pub fn start(token: String, journal_dir: PathBuf) -> Result<Self, String> {
        fs::create_dir_all(&journal_dir).map_err(|_| "无法创建项目提交日志目录")?;
        let listener =
            TcpListener::bind(("127.0.0.1", 0)).map_err(|_| "无法建立桌面能力桥".to_string())?;
        listener
            .set_nonblocking(true)
            .map_err(|_| "无法配置桌面能力桥".to_string())?;
        let port = listener
            .local_addr()
            .map_err(|_| "无法读取桌面能力桥端口")?
            .port();
        let instance_id = Arc::new(Mutex::new(String::new()));
        let stopped = Arc::new(AtomicBool::new(false));
        let broker = Arc::new(Mutex::new(WorkspaceBroker {
            journal_dir: Some(journal_dir),
            ..WorkspaceBroker::default()
        }));
        let worker_instance = instance_id.clone();
        let worker_stopped = stopped.clone();
        let worker_broker = broker.clone();
        let worker = thread::spawn(move || {
            let mut seen_nonces = HashMap::new();
            while !worker_stopped.load(Ordering::Relaxed) {
                match listener.accept() {
                    Ok((mut stream, _)) => {
                        if stream.set_nonblocking(false).is_err() {
                            continue;
                        }
                        stream.set_read_timeout(Some(Duration::from_secs(2))).ok();
                        stream.set_write_timeout(Some(Duration::from_secs(2))).ok();
                        handle_connection(
                            &mut stream,
                            &token,
                            &worker_instance,
                            &worker_broker,
                            &mut seen_nonces,
                        );
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(20));
                    }
                    Err(_) => break,
                }
            }
        });
        Ok(Self {
            port,
            instance_id,
            stopped,
            broker,
            worker: Some(worker),
        })
    }

    pub fn port(&self) -> u16 {
        self.port
    }

    pub fn set_instance_id(&self, instance: String) -> Result<(), String> {
        if instance.is_empty() || instance.len() > 200 {
            return Err("助手引擎返回了无效身份".into());
        }
        *self
            .instance_id
            .lock()
            .map_err(|_| "桌面能力桥状态不可用")? = instance;
        Ok(())
    }

    /// Only the Rust host may call this after resolving `project_id` through ChatStore.
    /// The absolute path is never accepted from the RPC payload or WebView.
    pub fn authorize_run(
        &self,
        session_id: &str,
        run_id: &str,
        project_id: &str,
        registered_root: &Path,
    ) -> Result<String, String> {
        self.broker
            .lock()
            .map_err(|_| "桌面能力桥状态不可用")?
            .authorize_run(session_id, run_id, project_id, registered_root)
            .map_err(str::to_owned)
    }

    pub fn revoke_run(&self, session_id: &str, run_id: &str) {
        if let Ok(mut broker) = self.broker.lock() {
            broker.revoke_run(session_id, run_id);
        }
    }

    pub fn restore_run_grant(
        &self,
        grant: &str,
        session_id: &str,
        run_id: &str,
        project_id: &str,
        registered_root: &Path,
    ) -> Result<(), String> {
        self.broker
            .lock()
            .map_err(|_| "桌面能力桥状态不可用")?
            .restore_run_grant(grant, session_id, run_id, project_id, registered_root)
            .map_err(str::to_owned)
    }

    pub fn apply_approved_changeset(
        &self,
        session_id: &str,
        run_id: &str,
        project_id: &str,
        registered_root: &Path,
        changeset: &Value,
        digest: &str,
    ) -> Result<Value, String> {
        self.broker
            .lock()
            .map_err(|_| "桌面能力桥状态不可用")?
            .host_apply_changeset(
                session_id,
                run_id,
                project_id,
                registered_root,
                changeset,
                digest,
                now_ms(),
            )
            .map_err(str::to_owned)
    }

    pub fn inspect_changeset_commit(
        &self,
        project_id: &str,
        change_id: &str,
        digest: &str,
    ) -> Result<Value, String> {
        self.broker
            .lock()
            .map_err(|_| "桌面能力桥状态不可用")?
            .inspect_commit(project_id, change_id, digest)
            .map_err(str::to_owned)
    }
}

impl Drop for BridgeServer {
    fn drop(&mut self) {
        self.stopped.store(true, Ordering::Relaxed);
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }
}

fn handle_connection(
    stream: &mut TcpStream,
    token: &str,
    expected_instance: &Arc<Mutex<String>>,
    broker: &Arc<Mutex<WorkspaceBroker>>,
    seen_nonces: &mut HashMap<String, u64>,
) {
    let mut line = Vec::new();
    let read_result = match stream.try_clone() {
        Ok(reader) => BufReader::new(reader)
            .take((MAX_BRIDGE_MESSAGE_BYTES + 1) as u64)
            .read_until(b'\n', &mut line),
        Err(error) => Err(error),
    };
    let fallback_id = String::new();
    let (request_id, result, error_code) = match read_result {
        Ok(length) if length <= MAX_BRIDGE_MESSAGE_BYTES => {
            match serde_json::from_slice::<BridgeRequest>(&line) {
                Ok(request) => {
                    let request_id = request.request_id.clone();
                    let now_ms = SystemTime::now()
                        .duration_since(UNIX_EPOCH)
                        .map(|duration| duration.as_millis() as u64)
                        .unwrap_or(0);
                    seen_nonces
                        .retain(|_, timestamp| now_ms.abs_diff(*timestamp) <= MAX_CLOCK_SKEW_MS);
                    if seen_nonces.contains_key(&request.nonce) {
                        (request_id, None, Some("bridge_replay"))
                    } else if seen_nonces.len() >= MAX_RECENT_NONCES {
                        (request_id, None, Some("bridge_rate_limited"))
                    } else {
                        let instance = expected_instance
                            .lock()
                            .map(|value| value.clone())
                            .unwrap_or_default();
                        let nonce = request.nonce.clone();
                        match validate_request(
                            &request,
                            token,
                            &instance,
                            now_ms,
                            &mut HashSet::new(),
                        ) {
                            Ok(()) => {
                                seen_nonces.insert(nonce, now_ms);
                                match broker.lock() {
                                    Ok(mut broker) => match broker.execute(&request, now_ms) {
                                        Ok(result) => (request_id, Some(result), None),
                                        Err(code) => (request_id, None, Some(code)),
                                    },
                                    Err(_) => (request_id, None, Some("broker_unavailable")),
                                }
                            }
                            Err(code) => (request_id, None, Some(code)),
                        }
                    }
                }
                Err(_) => (fallback_id, None, Some("bridge_invalid_request")),
            }
        }
        Ok(_) => (fallback_id, None, Some("bridge_request_too_large")),
        Err(_) if line.len() >= MAX_BRIDGE_MESSAGE_BYTES => {
            (fallback_id, None, Some("bridge_request_too_large"))
        }
        Err(_) => (fallback_id, None, Some("bridge_invalid_request")),
    };
    let response = BridgeResponse {
        protocol_version: DESKTOP_BRIDGE_PROTOCOL_VERSION,
        instance_id: expected_instance
            .lock()
            .map(|value| value.clone())
            .unwrap_or_default(),
        request_id,
        status: if error_code.is_some() {
            "rejected"
        } else {
            "completed"
        }
        .into(),
        result,
        error_code: error_code.map(str::to_owned),
    };
    if let Ok(mut bytes) = serde_json::to_vec(&response) {
        bytes.push(b'\n');
        if bytes.len() <= MAX_BRIDGE_MESSAGE_BYTES {
            let _ = stream.write_all(&bytes);
        }
    }
}

pub fn validate_request(
    request: &BridgeRequest,
    expected_token: &str,
    expected_instance: &str,
    now_ms: u64,
    seen_nonces: &mut HashSet<String>,
) -> Result<(), &'static str> {
    if request.protocol_version != DESKTOP_BRIDGE_PROTOCOL_VERSION {
        return Err("bridge_protocol_mismatch");
    }
    if request.instance_id != expected_instance {
        return Err("bridge_instance_mismatch");
    }
    if !constant_time_eq(
        request.authorization.as_bytes(),
        format!("Bearer {expected_token}").as_bytes(),
    ) {
        return Err("bridge_unauthorized");
    }
    if request.request_id.is_empty()
        || request.request_id.len() > 200
        || request.session_id.is_empty()
        || request.session_id.len() > 200
        || request.run_id.is_empty()
        || request.run_id.len() > 200
        || request.project_id.is_empty()
        || request.project_id.len() > 200
        || request.nonce.is_empty()
        || request.nonce.len() > 200
        || request.grant.is_empty()
        || request.grant.len() > 512
    {
        return Err("bridge_invalid_request");
    }
    if now_ms.abs_diff(request.timestamp_ms) > MAX_CLOCK_SKEW_MS {
        return Err("bridge_request_expired");
    }
    if !matches!(
        request.operation.as_str(),
        "list_entries"
            | "search_text"
            | "read_file"
            | "apply_changeset"
            | "inspect_commit"
            | "revoke_run"
    ) {
        return Err("bridge_operation_rejected");
    }
    if !seen_nonces.insert(request.nonce.clone()) {
        return Err("bridge_replay");
    }
    Ok(())
}

#[derive(Clone)]
struct ActiveGrant {
    session_id: String,
    run_id: String,
    project_id: String,
    root: PathBuf,
    root_identity: RootIdentity,
    expires_at_ms: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ChangeSetRecord {
    change_id: String,
    run_id: String,
    session_id: String,
    project_id: String,
    authorization_version: String,
    changes: Vec<FileChangeRecord>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct FileChangeRecord {
    relative_path: String,
    operation: String,
    expected_old_sha256: Option<String>,
    base_content: Option<String>,
    content: String,
    summary: String,
}

#[derive(Clone)]
struct ApprovalTicket {
    session_id: String,
    run_id: String,
    project_id: String,
    grant: String,
    digest: String,
    expires_at_ms: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct CommitJournal {
    schema_version: u32,
    change_id: String,
    digest: String,
    status: String,
    files: Vec<JournalFile>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct JournalFile {
    relative_path: String,
    status: String,
    error: Option<String>,
}

impl CommitJournal {
    fn as_value(&self) -> Value {
        serde_json::to_value(self).unwrap_or_else(|_| serde_json::json!({"status":"unknown"}))
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct RootIdentity {
    canonical_path: PathBuf,
    file_id: (u64, u64),
}

#[derive(Default)]
pub struct WorkspaceBroker {
    grants: HashMap<String, ActiveGrant>,
    approvals: HashMap<String, ApprovalTicket>,
    journal_dir: Option<PathBuf>,
    #[cfg(test)]
    fail_before_write_for: Option<String>,
    #[cfg(test)]
    crash_after_write_for: Option<String>,
    #[cfg(test)]
    crash_process_after_write_for: Option<String>,
}

impl WorkspaceBroker {
    fn restore_run_grant(
        &mut self,
        grant: &str,
        session_id: &str,
        run_id: &str,
        project_id: &str,
        registered_root: &Path,
    ) -> Result<(), &'static str> {
        if grant.len() != 32
            || !grant
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
            || session_id.is_empty()
            || run_id.is_empty()
            || project_id.is_empty()
        {
            return Err("bridge_invalid_request");
        }
        let identity = root_identity(registered_root)?;
        let now = now_ms();
        self.grants.retain(|_, active| active.expires_at_ms > now);
        if let Some(existing) = self.grants.get(grant) {
            if existing.session_id == session_id
                && existing.run_id == run_id
                && existing.project_id == project_id
                && existing.root_identity == identity
            {
                return Ok(());
            }
            return Err("grant_identity_mismatch");
        }
        if self.grants.values().any(|active| active.run_id == run_id) {
            return Err("grant_identity_mismatch");
        }
        self.grants.insert(
            grant.to_owned(),
            ActiveGrant {
                session_id: session_id.to_owned(),
                run_id: run_id.to_owned(),
                project_id: project_id.to_owned(),
                root: identity.canonical_path.clone(),
                root_identity: identity,
                expires_at_ms: now.saturating_add(MAX_GRANT_LIFETIME_MS),
            },
        );
        Ok(())
    }

    fn inspect_commit(
        &self,
        project_id: &str,
        change_id: &str,
        digest: &str,
    ) -> Result<Value, &'static str> {
        if change_id.is_empty()
            || change_id.len() > 200
            || !change_id
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
        {
            return Err("changeset_invalid");
        }
        let journal_dir = self
            .journal_dir
            .as_ref()
            .ok_or("commit_journal_unavailable")?;
        let path = journal_dir.join(format!(
            "{}-{change_id}.json",
            sha256_bytes(project_id.as_bytes())
        ));
        recover_journal_backup(&path)?;
        if !path.exists() {
            return Ok(serde_json::json!({"schema_version":1,"change_id":change_id,
                "digest":digest,"status":"not_started","files":[]}));
        }
        let bytes = fs::read(path).map_err(|_| "commit_journal_unavailable")?;
        let journal: CommitJournal =
            serde_json::from_slice(&bytes).map_err(|_| "commit_journal_corrupt")?;
        validate_commit_journal(&journal)?;
        if journal.change_id != change_id || journal.digest != digest {
            return Err("commit_journal_conflict");
        }
        Ok(journal.as_value())
    }

    fn host_apply_changeset(
        &mut self,
        session_id: &str,
        run_id: &str,
        project_id: &str,
        registered_root: &Path,
        changeset: &Value,
        digest: &str,
        now: u64,
    ) -> Result<Value, &'static str> {
        let identity = root_identity(registered_root)?;
        let grant = self
            .grants
            .iter()
            .find(|(_, grant)| {
                grant.session_id == session_id
                    && grant.run_id == run_id
                    && grant.project_id == project_id
                    && grant.root_identity == identity
                    && grant.expires_at_ms > now
            })
            .map(|(token, _)| token.clone())
            .ok_or("grant_expired")?;
        let approval = self.issue_approval(&grant, changeset, digest, now)?;
        self.commit_approved(&approval, changeset, digest, now)
    }

    fn issue_approval(
        &mut self,
        grant_token: &str,
        changeset_value: &Value,
        digest: &str,
        now: u64,
    ) -> Result<String, &'static str> {
        let actual_digest = changeset_digest(changeset_value)?;
        if actual_digest != digest {
            return Err("changeset_digest_mismatch");
        }
        let changeset: ChangeSetRecord =
            serde_json::from_value(changeset_value.clone()).map_err(|_| "changeset_invalid")?;
        validate_changeset(&changeset, grant_token)?;
        let grant = self.grants.get(grant_token).ok_or("grant_expired")?;
        if grant.expires_at_ms <= now
            || grant.session_id != changeset.session_id
            || grant.run_id != changeset.run_id
            || grant.project_id != changeset.project_id
            || changeset.authorization_version != sha256_bytes(grant_token.as_bytes())
        {
            return Err("grant_identity_mismatch");
        }
        SecureRoot::open(&grant.root, &grant.root_identity)?;
        let ticket = uuid::Uuid::new_v4().simple().to_string();
        self.approvals.insert(
            ticket.clone(),
            ApprovalTicket {
                session_id: grant.session_id.clone(),
                run_id: grant.run_id.clone(),
                project_id: grant.project_id.clone(),
                grant: grant_token.to_owned(),
                digest: digest.to_owned(),
                expires_at_ms: now.saturating_add(MAX_APPROVAL_LIFETIME_MS),
            },
        );
        Ok(ticket)
    }

    fn commit_approved(
        &mut self,
        ticket_token: &str,
        changeset_value: &Value,
        digest: &str,
        now: u64,
    ) -> Result<Value, &'static str> {
        let ticket = self
            .approvals
            .remove(ticket_token)
            .ok_or("approval_ticket_invalid")?;
        if ticket.expires_at_ms <= now
            || ticket.digest != digest
            || changeset_digest(changeset_value)? != digest
        {
            return Err("approval_ticket_invalid");
        }
        let changeset: ChangeSetRecord =
            serde_json::from_value(changeset_value.clone()).map_err(|_| "changeset_invalid")?;
        if ticket.session_id != changeset.session_id
            || ticket.run_id != changeset.run_id
            || ticket.project_id != changeset.project_id
        {
            return Err("approval_ticket_invalid");
        }
        validate_changeset(&changeset, &ticket.grant)?;
        let grant = self
            .grants
            .get(&ticket.grant)
            .ok_or("grant_expired")?
            .clone();
        if grant.expires_at_ms <= now
            || grant.session_id != ticket.session_id
            || grant.run_id != ticket.run_id
            || grant.project_id != ticket.project_id
            || changeset.authorization_version != sha256_bytes(ticket.grant.as_bytes())
        {
            return Err("grant_identity_mismatch");
        }
        let secure = SecureRoot::open(&grant.root, &grant.root_identity)?;
        self.apply_changeset_files(&secure, &changeset, digest)
    }

    fn apply_changeset_files(
        &self,
        secure: &SecureRoot,
        changeset: &ChangeSetRecord,
        digest: &str,
    ) -> Result<Value, &'static str> {
        let journal_dir = self
            .journal_dir
            .as_ref()
            .ok_or("commit_journal_unavailable")?;
        fs::create_dir_all(journal_dir).map_err(|_| "commit_journal_directory_failed")?;
        if changeset.change_id.is_empty()
            || changeset.change_id.len() > 200
            || !changeset
                .change_id
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
        {
            return Err("changeset_invalid");
        }
        let journal_path = journal_dir.join(format!(
            "{}-{}.json",
            sha256_bytes(changeset.project_id.as_bytes()),
            changeset.change_id
        ));
        recover_journal_backup(&journal_path)?;
        let journal_exists = journal_path.exists();
        let mut journal = if journal_exists {
            let bytes = fs::read(&journal_path).map_err(|_| "commit_journal_unavailable")?;
            let existing: CommitJournal =
                serde_json::from_slice(&bytes).map_err(|_| "commit_journal_corrupt")?;
            validate_commit_journal(&existing)?;
            if existing.digest != digest || existing.change_id != changeset.change_id {
                return Err("commit_journal_conflict");
            }
            if existing.files.len() != changeset.changes.len()
                || existing
                    .files
                    .iter()
                    .zip(&changeset.changes)
                    .any(|(file, change)| {
                        file.relative_path != change.relative_path
                            || !matches!(
                                file.status.as_str(),
                                "pending" | "applied" | "failed" | "unknown"
                            )
                    })
            {
                return Err("commit_journal_corrupt");
            }
            existing
        } else {
            CommitJournal {
                schema_version: 1,
                change_id: changeset.change_id.clone(),
                digest: digest.to_owned(),
                status: "intent".into(),
                files: changeset
                    .changes
                    .iter()
                    .map(|change| JournalFile {
                        relative_path: change.relative_path.clone(),
                        status: "pending".into(),
                        error: None,
                    })
                    .collect(),
            }
        };
        if !journal_exists {
            persist_journal(&journal_path, &journal)?;
        }
        preflight_changeset(&secure, &changeset, Some(&journal))?;
        if journal.status == "completed" {
            return Ok(journal.as_value());
        }

        for (index, change) in changeset.changes.iter().enumerate() {
            if journal.files[index].status == "applied" {
                continue;
            }
            match file_sha256_at(secure, change) {
                Ok(Some(hash)) if hash == sha256_bytes(change.content.as_bytes()) => {
                    journal.files[index].status = "applied".into();
                    journal.files[index].error = None;
                    persist_journal(&journal_path, &journal)?;
                    continue;
                }
                Ok(None) if change.operation == "add" => {}
                Ok(Some(hash)) if Some(hash.as_str()) == change.expected_old_sha256.as_deref() => {}
                Ok(_) => {
                    journal.files[index].status = "unknown".into();
                    journal.files[index].error = Some("workspace_conflict".into());
                    journal.status = "needs_inspection".into();
                    persist_journal(&journal_path, &journal)?;
                    return Ok(journal.as_value());
                }
                Err(error) => {
                    journal.files[index].status = "unknown".into();
                    journal.files[index].error = Some(error.into());
                    journal.status = "needs_inspection".into();
                    persist_journal(&journal_path, &journal)?;
                    return Ok(journal.as_value());
                }
            }
            let expected = change.expected_old_sha256.as_deref();
            #[cfg(test)]
            if self.fail_before_write_for.as_deref() == Some(change.relative_path.as_str()) {
                journal.files[index].status = "failed".into();
                journal.files[index].error = Some("workspace_write_failed".into());
                journal.status = if journal.files.iter().any(|file| file.status == "applied") {
                    "partial"
                } else {
                    "failed"
                }
                .into();
                persist_journal(&journal_path, &journal)?;
                return Ok(journal.as_value());
            }
            match secure.atomic_write(
                Path::new(&change.relative_path),
                expected,
                change.content.as_bytes(),
                &changeset.change_id,
            ) {
                Ok(()) => {
                    #[cfg(feature = "desktop-acceptance-crash-hook")]
                    if std::env::var("TOMATO_AGENT_CRASH_AFTER_WRITE").as_deref()
                        == Ok(change.relative_path.as_str())
                    {
                        std::process::abort();
                    }
                    #[cfg(test)]
                    if self.crash_after_write_for.as_deref() == Some(change.relative_path.as_str())
                    {
                        return Err("simulated_process_crash");
                    }
                    #[cfg(test)]
                    if self.crash_process_after_write_for.as_deref()
                        == Some(change.relative_path.as_str())
                    {
                        std::process::abort();
                    }
                    journal.files[index].status = "applied".into();
                    journal.files[index].error = None;
                    journal.status = "applying".into();
                    persist_journal(&journal_path, &journal)?;
                }
                Err(error) => {
                    journal.files[index].status = "failed".into();
                    journal.files[index].error = Some(error.into());
                    journal.status = if journal.files.iter().any(|file| file.status == "applied") {
                        "partial"
                    } else {
                        "failed"
                    }
                    .into();
                    persist_journal(&journal_path, &journal)?;
                    return Ok(journal.as_value());
                }
            }
        }
        journal.status = "completed".into();
        persist_journal(&journal_path, &journal)?;
        Ok(journal.as_value())
    }

    fn authorize_run(
        &mut self,
        session_id: &str,
        run_id: &str,
        project_id: &str,
        registered_root: &Path,
    ) -> Result<String, &'static str> {
        if session_id.is_empty() || run_id.is_empty() || project_id.is_empty() {
            return Err("bridge_invalid_request");
        }
        let registered_metadata =
            fs::symlink_metadata(registered_root).map_err(|_| "project_unavailable")?;
        if registered_metadata.file_type().is_symlink() || !registered_metadata.is_dir() {
            return Err("project_unavailable");
        }
        let root = registered_root
            .canonicalize()
            .map_err(|_| "project_unavailable")?;
        let resolved_metadata = fs::symlink_metadata(&root).map_err(|_| "project_unavailable")?;
        if resolved_metadata.file_type().is_symlink()
            || root_identity(&root)?.canonical_path != root
        {
            return Err("project_identity_changed");
        }
        let identity = root_identity(&root)?;
        if root_identity(registered_root)? != identity {
            return Err("project_identity_changed");
        }
        let now = now_ms();
        self.grants.retain(|_, grant| grant.expires_at_ms > now);
        if let Some((token, existing)) =
            self.grants.iter().find(|(_, grant)| grant.run_id == run_id)
        {
            if existing.session_id == session_id
                && existing.project_id == project_id
                && existing.root_identity == identity
            {
                return Ok(token.clone());
            }
            return Err("grant_identity_mismatch");
        }
        let grant = uuid::Uuid::new_v4().simple().to_string();
        self.grants.insert(
            grant.clone(),
            ActiveGrant {
                session_id: session_id.into(),
                run_id: run_id.into(),
                project_id: project_id.into(),
                root,
                root_identity: identity,
                expires_at_ms: now_ms().saturating_add(MAX_GRANT_LIFETIME_MS),
            },
        );
        Ok(grant)
    }

    fn revoke_run(&mut self, session_id: &str, run_id: &str) {
        self.grants
            .retain(|_, grant| grant.session_id != session_id || grant.run_id != run_id);
    }

    fn execute(&mut self, request: &BridgeRequest, now: u64) -> Result<Value, &'static str> {
        self.grants.retain(|_, grant| grant.expires_at_ms > now);
        let grant = self.grants.get(&request.grant).ok_or("grant_expired")?;
        if grant.session_id != request.session_id
            || grant.run_id != request.run_id
            || grant.project_id != request.project_id
        {
            return Err("grant_identity_mismatch");
        }
        if request.operation == "revoke_run" {
            self.revoke_run(&request.session_id, &request.run_id);
            return Ok(serde_json::json!({"revoked": true}));
        }
        match request.operation.as_str() {
            "list_entries" => list_entries(&grant.root, &grant.root_identity, &request.payload),
            "search_text" => search_text(&grant.root, &grant.root_identity, &request.payload),
            "read_file" => read_file(&grant.root, &grant.root_identity, &request.payload),
            _ => Err("bridge_operation_rejected"),
        }
    }
}

fn changeset_digest(value: &Value) -> Result<String, &'static str> {
    let canonical = serde_json::to_vec(value).map_err(|_| "changeset_invalid")?;
    Ok(sha256_bytes(&canonical))
}

fn validate_commit_journal(journal: &CommitJournal) -> Result<(), &'static str> {
    if journal.schema_version != 1
        || journal.change_id.is_empty()
        || journal.digest.len() != 64
        || !journal
            .digest
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        || journal.files.is_empty()
    {
        return Err("commit_journal_corrupt");
    }
    let mut applied = false;
    let mut failed = false;
    let mut unknown = false;
    let mut pending = false;
    for file in &journal.files {
        if file.relative_path.is_empty() {
            return Err("commit_journal_corrupt");
        }
        match file.status.as_str() {
            "pending" if file.error.is_none() => pending = true,
            "applied" if file.error.is_none() => applied = true,
            "failed" if file.error.as_deref().is_some_and(|error| !error.is_empty()) => {
                failed = true
            }
            "unknown" if file.error.as_deref().is_some_and(|error| !error.is_empty()) => {
                unknown = true
            }
            _ => return Err("commit_journal_corrupt"),
        }
    }
    let consistent = match journal.status.as_str() {
        "intent" => pending && !applied && !failed && !unknown,
        "applying" => !failed && !unknown && (pending || applied),
        "partial" => applied && failed && !unknown,
        "failed" => failed && !applied && !unknown,
        "needs_inspection" => unknown,
        "completed" => applied && !pending && !failed && !unknown,
        _ => false,
    };
    if consistent {
        Ok(())
    } else {
        Err("commit_journal_corrupt")
    }
}

fn sha256_bytes(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    format!("{:x}", Sha256::digest(bytes))
}

fn validate_changeset(changeset: &ChangeSetRecord, grant: &str) -> Result<(), &'static str> {
    if changeset.change_id.is_empty()
        || changeset.run_id.is_empty()
        || changeset.session_id.is_empty()
        || changeset.project_id.is_empty()
        || changeset.authorization_version != sha256_bytes(grant.as_bytes())
        || changeset.changes.is_empty()
        || changeset.changes.len() > 100
    {
        return Err("changeset_invalid");
    }
    let mut paths = HashSet::new();
    let mut total = 0usize;
    for change in &changeset.changes {
        let path_payload = serde_json::json!({"relative_path": change.relative_path});
        let path = relative_path(&path_payload)?;
        if path.to_string_lossy() != change.relative_path
            || !paths.insert(change.relative_path.as_str())
        {
            return Err("changeset_invalid");
        }
        total = total
            .checked_add(change.content.len())
            .and_then(|size| size.checked_add(change.base_content.as_ref().map_or(0, String::len)))
            .ok_or("changeset_invalid")?;
        if change.content.as_bytes().contains(&0) || change.content.len() > 256 * 1024 {
            return Err("changeset_invalid");
        }
        match change.operation.as_str() {
            "add" if change.expected_old_sha256.is_none() && change.base_content.is_none() => {}
            "modify" => {
                let expected = change
                    .expected_old_sha256
                    .as_deref()
                    .ok_or("changeset_invalid")?;
                let base = change.base_content.as_ref().ok_or("changeset_invalid")?;
                if expected.len() != 64
                    || !expected
                        .bytes()
                        .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
                    || sha256_bytes(base.as_bytes()) != expected
                    || base.len() > 256 * 1024
                    || base.as_bytes().contains(&0)
                {
                    return Err("changeset_invalid");
                }
            }
            _ => return Err("changeset_invalid"),
        }
    }
    if total > 512 * 1024 {
        return Err("changeset_invalid");
    }
    Ok(())
}

fn preflight_changeset(
    secure: &SecureRoot,
    changeset: &ChangeSetRecord,
    journal: Option<&CommitJournal>,
) -> Result<(), &'static str> {
    for (index, change) in changeset.changes.iter().enumerate() {
        let journal_status = journal.map(|entry| entry.files[index].status.as_str());
        if journal_status == Some("unknown") {
            return Err("commit_needs_inspection");
        }
        let relative = Path::new(&change.relative_path);
        let parent = relative
            .parent()
            .filter(|path| !path.as_os_str().is_empty())
            .unwrap_or_else(|| Path::new(""));
        secure.open_relative(parent, true)?;
        match file_sha256_at(secure, change)? {
            Some(hash) if journal.is_some() && hash == sha256_bytes(change.content.as_bytes()) => {}
            Some(_) if journal_status == Some("applied") => return Err("commit_needs_inspection"),
            Some(_) if change.operation == "add" => return Err("workspace_conflict"),
            Some(hash) if Some(hash.as_str()) != change.expected_old_sha256.as_deref() => {
                return Err("workspace_conflict");
            }
            Some(_) => {}
            None if journal_status == Some("applied") => return Err("commit_needs_inspection"),
            None if change.operation == "modify" => return Err("workspace_conflict"),
            None => {}
        }
    }
    Ok(())
}

fn file_sha256_at(
    secure: &SecureRoot,
    change: &FileChangeRecord,
) -> Result<Option<String>, &'static str> {
    match secure.open_relative(Path::new(&change.relative_path), false) {
        Ok(file) => {
            let metadata = file.metadata().map_err(|_| "workspace_read_denied")?;
            if !metadata.is_file() || has_multiple_links(&metadata, &file) {
                return Err("workspace_file_rejected");
            }
            if metadata.len() > 256 * 1024 {
                return Err("workspace_file_too_large");
            }
            Ok(Some(sha256_file(file)?))
        }
        Err("workspace_path_unavailable") => Ok(None),
        Err(error) => Err(error),
    }
}

fn persist_journal(path: &Path, journal: &CommitJournal) -> Result<(), &'static str> {
    let bytes = serde_json::to_vec(journal).map_err(|_| "commit_journal_serialize_failed")?;
    let temporary = path.with_extension(format!("{}.tmp", uuid::Uuid::new_v4()));
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)
        .map_err(|_| "commit_journal_create_failed")?;
    if file
        .write_all(&bytes)
        .and_then(|_| file.sync_all())
        .is_err()
    {
        let _ = fs::remove_file(&temporary);
        return Err("commit_journal_write_failed");
    }
    drop(file);
    replace_journal_file(&temporary, path)?;
    if let Some(parent) = path.parent() {
        if let Ok(directory) = File::open(parent) {
            let _ = directory.sync_all();
        }
    }
    Ok(())
}

#[cfg(windows)]
fn journal_backup_path(path: &Path) -> PathBuf {
    path.with_extension("json.bak")
}

fn recover_journal_backup(_path: &Path) -> Result<(), &'static str> {
    #[cfg(windows)]
    {
        let backup = journal_backup_path(_path);
        if _path.exists() {
            let _ = fs::remove_file(backup);
        } else if backup.exists() {
            move_journal_file(&backup, _path).map_err(|_| "commit_journal_replace_failed")?;
        }
    }
    Ok(())
}

fn replace_journal_file(temporary: &Path, path: &Path) -> Result<(), &'static str> {
    #[cfg(windows)]
    {
        // MoveFileW works in EFS directories; the backup covers process exit between moves.
        recover_journal_backup(path)?;
        let backup = journal_backup_path(path);
        if path.exists() {
            if move_journal_file(path, &backup).is_err() {
                let _ = fs::remove_file(temporary);
                return Err("commit_journal_replace_failed");
            }
            if move_journal_file(temporary, path).is_err() {
                let _ = move_journal_file(&backup, path);
                let _ = fs::remove_file(temporary);
                return Err("commit_journal_replace_failed");
            }
            let _ = fs::remove_file(backup);
            return Ok(());
        }
    }

    move_journal_file(temporary, path).map_err(|_| {
        let _ = fs::remove_file(temporary);
        "commit_journal_replace_failed"
    })
}

fn move_journal_file(source: &Path, destination: &Path) -> std::io::Result<()> {
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::Storage::FileSystem::MoveFileW;

        let source = source
            .as_os_str()
            .encode_wide()
            .chain(Some(0))
            .collect::<Vec<_>>();
        let destination = destination
            .as_os_str()
            .encode_wide()
            .chain(Some(0))
            .collect::<Vec<_>>();
        if unsafe { MoveFileW(source.as_ptr(), destination.as_ptr()) } == 0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok(())
    }
    #[cfg(not(windows))]
    {
        fs::rename(source, destination)
    }
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

fn root_identity(root: &Path) -> Result<RootIdentity, &'static str> {
    let metadata = fs::symlink_metadata(root).map_err(|_| "project_unavailable")?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Err("project_identity_changed");
    }
    let canonical_path = root.canonicalize().map_err(|_| "project_unavailable")?;
    #[cfg(windows)]
    let identity = {
        let handle = open_windows_directory(root)?;
        let file_id = file_id_from_handle(&handle)?;
        let canonical_handle = open_windows_directory(&canonical_path)?;
        if file_id_from_handle(&canonical_handle)? != file_id {
            return Err("project_identity_changed");
        }
        file_id
    };
    #[cfg(not(windows))]
    let identity = file_id(&metadata);
    Ok(RootIdentity {
        canonical_path,
        file_id: identity,
    })
}

#[cfg(unix)]
fn file_id(metadata: &Metadata) -> (u64, u64) {
    use std::os::unix::fs::MetadataExt;
    (metadata.dev(), metadata.ino())
}

#[cfg(unix)]
fn has_multiple_links(metadata: &Metadata, _: &File) -> bool {
    use std::os::unix::fs::MetadataExt;
    metadata.nlink() > 1
}

#[cfg(windows)]
fn has_multiple_links(_: &Metadata, file: &File) -> bool {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::{
        GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION,
    };
    let mut info = BY_HANDLE_FILE_INFORMATION::default();
    unsafe {
        GetFileInformationByHandle(file.as_raw_handle() as _, &mut info) == 0
            || info.nNumberOfLinks > 1
    }
}

#[cfg(not(any(unix, windows)))]
fn has_multiple_links(_: &Metadata, _: &File) -> bool {
    true
}

#[cfg(windows)]
fn file_id_from_handle(file: &File) -> Result<(u64, u64), &'static str> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::{
        GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION,
    };
    let mut info = BY_HANDLE_FILE_INFORMATION::default();
    if unsafe { GetFileInformationByHandle(file.as_raw_handle() as _, &mut info) } == 0 {
        return Err("project_unavailable");
    }
    Ok((
        info.dwVolumeSerialNumber as u64,
        ((info.nFileIndexHigh as u64) << 32) | info.nFileIndexLow as u64,
    ))
}

#[cfg(windows)]
fn open_windows_directory(path: &Path) -> Result<File, &'static str> {
    use cap_primitives::fs::{FollowSymlinks, MetadataExt};
    use cap_std::fs::{Dir, OpenOptions};
    use std::path::Prefix;
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE_POINT,
    };

    let mut components = path.components();
    let Some(Component::Prefix(prefix)) = components.next() else {
        return Err("project_unavailable");
    };
    let drive = match prefix.kind() {
        Prefix::Disk(drive) | Prefix::VerbatimDisk(drive) => drive as char,
        _ => return Err("project_unavailable"),
    };
    if components.next() != Some(Component::RootDir) {
        return Err("project_unavailable");
    }
    let volume_root = PathBuf::from(format!("{drive}:\\"));
    let root = open_windows_directory_leaf(&volume_root)?;
    let mut current = Dir::from_std_file(root);
    let remaining = components.collect::<Vec<_>>();
    if remaining.is_empty() {
        return Ok(current.into_std_file());
    }
    for (index, component) in remaining.iter().enumerate() {
        let Component::Normal(name) = component else {
            return Err("project_unavailable");
        };
        let mut options = OpenOptions::new();
        options
            .read(true)
            ._cap_fs_ext_follow(FollowSymlinks::No)
            ._cap_fs_ext_maybe_dir(true);
        let file = current
            .open_with(name, &options)
            .map_err(|_| "project_unavailable")?;
        let metadata = file.metadata().map_err(|_| "project_unavailable")?;
        let attributes = metadata.file_attributes();
        if attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
            return Err("project_identity_changed");
        }
        if attributes & FILE_ATTRIBUTE_DIRECTORY == 0 {
            return Err("project_unavailable");
        }
        if index + 1 == remaining.len() {
            return Ok(file.into_std());
        }
        current = Dir::from_std_file(file.into_std());
    }
    Err("project_unavailable")
}

#[cfg(windows)]
fn open_windows_directory_leaf(path: &Path) -> Result<File, &'static str> {
    use std::os::windows::fs::OpenOptionsExt;
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_BACKUP_SEMANTICS,
        FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_READ, FILE_SHARE_WRITE,
    };
    let file = std::fs::OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE)
        .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)
        .open(path)
        .map_err(|_| "project_unavailable")?;
    let metadata = file.metadata().map_err(|_| "project_unavailable")?;
    use std::os::windows::fs::MetadataExt;
    let attributes = metadata.file_attributes();
    if attributes & FILE_ATTRIBUTE_DIRECTORY == 0 {
        return Err("project_unavailable");
    }
    if attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err("project_identity_changed");
    }
    Ok(file)
}

#[cfg(not(any(unix, windows)))]
fn file_id(metadata: &Metadata) -> (u64, u64) {
    (
        metadata.len(),
        metadata
            .modified()
            .ok()
            .and_then(|value| value.duration_since(UNIX_EPOCH).ok())
            .map(|value| value.as_nanos() as u64)
            .unwrap_or_default(),
    )
}

fn relative_path(payload: &Value) -> Result<PathBuf, &'static str> {
    let value = payload
        .get("relative_path")
        .and_then(Value::as_str)
        .ok_or("bridge_invalid_request")?;
    if value.len() > MAX_PATH_BYTES
        || value.contains('\\')
        || value.contains(':')
        || value.starts_with('/')
    {
        return Err("workspace_path_rejected");
    }
    let path = Path::new(value);
    let mut normalized = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::Normal(part) => {
                let name = part.to_str().ok_or("workspace_path_rejected")?;
                if is_sensitive_name(name) {
                    return Err("workspace_sensitive_path");
                }
                normalized.push(part);
            }
            _ => return Err("workspace_path_rejected"),
        }
    }
    Ok(normalized)
}

fn is_sensitive_name(name: &str) -> bool {
    let lower = name.to_ascii_lowercase();
    matches!(
        lower.as_str(),
        ".git" | ".svn" | ".hg" | ".ssh" | ".aws" | ".azure" | "secrets"
    ) || lower == ".env"
        || lower.starts_with(".env.")
}

fn is_change_temp_name(name: &std::ffi::OsStr, change_id: &str) -> bool {
    let Some(name) = name.to_str() else {
        return false;
    };
    let Some(identifier) = name
        .strip_prefix(&format!(".ta-changeset-{change_id}-"))
        .and_then(|value| value.strip_suffix(".tmp"))
    else {
        return false;
    };
    uuid::Uuid::parse_str(identifier).is_ok()
}

#[cfg(target_os = "linux")]
struct SecureRoot(File);

#[cfg(target_os = "linux")]
impl SecureRoot {
    fn open(path: &Path, expected: &RootIdentity) -> Result<Self, &'static str> {
        use std::os::fd::FromRawFd;
        use std::os::unix::ffi::OsStrExt;
        let path = std::ffi::CString::new(path.as_os_str().as_bytes())
            .map_err(|_| "project_unavailable")?;
        let fd = unsafe {
            libc::open(
                path.as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC | libc::O_NOFOLLOW,
            )
        };
        if fd < 0 {
            return Err("project_unavailable");
        }
        let file = unsafe { File::from_raw_fd(fd) };
        let metadata = file.metadata().map_err(|_| "project_unavailable")?;
        if !metadata.is_dir() || file_id(&metadata) != expected.file_id {
            return Err("project_identity_changed");
        }
        Ok(Self(file))
    }

    fn open_relative(&self, relative: &Path, directory: bool) -> Result<File, &'static str> {
        use std::os::fd::{AsRawFd, FromRawFd};
        use std::os::unix::ffi::OsStrExt;
        let mut current = self.0.try_clone().map_err(|_| "workspace_read_denied")?;
        let components = relative.components().collect::<Vec<_>>();
        if components.is_empty() {
            return Ok(current);
        }
        for (index, component) in components.iter().enumerate() {
            let Component::Normal(name) = component else {
                return Err("workspace_path_rejected");
            };
            let name =
                std::ffi::CString::new(name.as_bytes()).map_err(|_| "workspace_path_rejected")?;
            let is_directory = directory || index + 1 < components.len();
            let flags = libc::O_CLOEXEC
                | libc::O_NOFOLLOW
                | if is_directory {
                    libc::O_RDONLY | libc::O_DIRECTORY
                } else {
                    libc::O_RDONLY | libc::O_NONBLOCK
                };
            let fd = unsafe { libc::openat(current.as_raw_fd(), name.as_ptr(), flags) };
            if fd < 0 {
                let error = std::io::Error::last_os_error();
                return Err(
                    if matches!(error.raw_os_error(), Some(libc::ELOOP | libc::ENOTDIR)) {
                        "workspace_link_rejected"
                    } else {
                        "workspace_path_unavailable"
                    },
                );
            }
            current = unsafe { File::from_raw_fd(fd) };
        }
        Ok(current)
    }

    fn read_dir_at(&self, directory: &File) -> Result<Vec<std::ffi::OsString>, &'static str> {
        use std::os::fd::AsRawFd;
        let entries = fs::read_dir(format!("/proc/self/fd/{}", directory.as_raw_fd()))
            .map_err(|_| "workspace_read_denied")?;
        Ok(entries
            .filter_map(Result::ok)
            .map(|entry| entry.file_name())
            .collect())
    }

    fn atomic_write(
        &self,
        relative: &Path,
        expected_old_sha256: Option<&str>,
        content: &[u8],
        change_id: &str,
    ) -> Result<(), &'static str> {
        use std::os::fd::{AsRawFd, FromRawFd};
        use std::os::unix::ffi::OsStrExt;

        if content.len() > 256 * 1024 || change_id.is_empty() || change_id.len() > 200 {
            return Err("changeset_invalid");
        }
        let name = relative.file_name().ok_or("workspace_path_rejected")?;
        if relative
            .components()
            .any(|item| !matches!(item, Component::Normal(_)))
        {
            return Err("workspace_path_rejected");
        }
        let parent_path = relative
            .parent()
            .filter(|path| !path.as_os_str().is_empty())
            .unwrap_or_else(|| Path::new(""));
        let parent = self.open_relative(parent_path, true)?;
        cleanup_change_temps(&parent, change_id)?;
        let target_name =
            std::ffi::CString::new(name.as_bytes()).map_err(|_| "workspace_path_rejected")?;

        match expected_old_sha256 {
            Some(expected) => {
                let current = self.open_relative(relative, false)?;
                let metadata = current
                    .metadata()
                    .map_err(|_| "workspace_path_unavailable")?;
                if !metadata.is_file() || has_multiple_links(&metadata, &current) {
                    return Err("workspace_file_rejected");
                }
                if sha256_file(current)? != expected {
                    return Err("workspace_conflict");
                }
            }
            None => match self.open_relative(relative, false) {
                Ok(_) => return Err("workspace_conflict"),
                Err("workspace_path_unavailable") => {}
                Err(error) => return Err(error),
            },
        }

        let temporary_name = format!(".ta-changeset-{}-{}.tmp", change_id, uuid::Uuid::new_v4());
        let temporary_c = std::ffi::CString::new(temporary_name.as_bytes())
            .map_err(|_| "workspace_write_failed")?;
        let fd = unsafe {
            libc::openat(
                parent.as_raw_fd(),
                temporary_c.as_ptr(),
                libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                0o600,
            )
        };
        if fd < 0 {
            return Err("workspace_write_failed");
        }
        let mut temporary = unsafe { File::from_raw_fd(fd) };
        let write_result = temporary
            .write_all(content)
            .and_then(|_| temporary.sync_all());
        if write_result.is_err() {
            let _ = unsafe { libc::unlinkat(parent.as_raw_fd(), temporary_c.as_ptr(), 0) };
            return Err("workspace_write_failed");
        }
        drop(temporary);

        let rename_result = if expected_old_sha256.is_some() {
            unsafe {
                libc::renameat(
                    parent.as_raw_fd(),
                    temporary_c.as_ptr(),
                    parent.as_raw_fd(),
                    target_name.as_ptr(),
                )
            }
        } else {
            unsafe {
                libc::renameat2(
                    parent.as_raw_fd(),
                    temporary_c.as_ptr(),
                    parent.as_raw_fd(),
                    target_name.as_ptr(),
                    libc::RENAME_NOREPLACE,
                )
            }
        };
        if rename_result != 0 {
            let error = std::io::Error::last_os_error();
            let _ = unsafe { libc::unlinkat(parent.as_raw_fd(), temporary_c.as_ptr(), 0) };
            return Err(if error.kind() == std::io::ErrorKind::AlreadyExists {
                "workspace_conflict"
            } else {
                "workspace_write_failed"
            });
        }
        parent.sync_all().map_err(|_| "workspace_write_failed")
    }
}

#[cfg(target_os = "linux")]
fn sha256_file(mut file: File) -> Result<String, &'static str> {
    use sha2::{Digest, Sha256};
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 8192];
    loop {
        let count = file
            .read(&mut buffer)
            .map_err(|_| "workspace_read_denied")?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
    }
    Ok(format!("{:x}", digest.finalize()))
}

#[cfg(target_os = "linux")]
fn cleanup_change_temps(parent: &File, change_id: &str) -> Result<(), &'static str> {
    use std::os::fd::AsRawFd;
    use std::os::unix::ffi::OsStrExt;
    let entries = fs::read_dir(format!("/proc/self/fd/{}", parent.as_raw_fd()))
        .map_err(|_| "workspace_write_failed")?;
    for entry in entries {
        let name = entry.map_err(|_| "workspace_write_failed")?.file_name();
        if is_change_temp_name(&name, change_id) {
            let name =
                std::ffi::CString::new(name.as_bytes()).map_err(|_| "workspace_write_failed")?;
            if unsafe { libc::unlinkat(parent.as_raw_fd(), name.as_ptr(), 0) } != 0 {
                return Err("workspace_write_failed");
            }
        }
    }
    Ok(())
}

#[cfg(target_os = "windows")]
struct SecureRoot(cap_std::fs::Dir);

#[cfg(target_os = "windows")]
impl SecureRoot {
    fn open(path: &Path, expected: &RootIdentity) -> Result<Self, &'static str> {
        let file = open_windows_directory(path)?;
        if file_id_from_handle(&file)? != expected.file_id {
            return Err("project_identity_changed");
        }
        Ok(Self(cap_std::fs::Dir::from_std_file(file)))
    }

    fn open_relative(&self, relative: &Path, directory: bool) -> Result<File, &'static str> {
        use cap_primitives::fs::FollowSymlinks;
        use cap_std::fs::{Dir, MetadataExt, OpenOptions};
        use windows_sys::Win32::Storage::FileSystem::{
            FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE_POINT,
        };

        let components = relative
            .components()
            .filter(|component| !matches!(component, Component::CurDir))
            .collect::<Vec<_>>();
        let mut current = self.0.try_clone().map_err(|_| "workspace_read_denied")?;
        if components.is_empty() {
            return Ok(current.into_std_file());
        }
        for (index, component) in components.iter().enumerate() {
            let Component::Normal(name) = component else {
                return Err("workspace_path_rejected");
            };
            let is_directory = directory || index + 1 < components.len();
            let mut options = OpenOptions::new();
            options
                .read(true)
                ._cap_fs_ext_follow(FollowSymlinks::No)
                ._cap_fs_ext_maybe_dir(true);
            let file = current
                .open_with(name, &options)
                .map_err(|error| map_windows_open_error(&error))?;
            let metadata = file.metadata().map_err(|_| "workspace_read_denied")?;
            let attributes = metadata.file_attributes();
            if attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
                return Err("workspace_link_rejected");
            }
            if is_directory && attributes & FILE_ATTRIBUTE_DIRECTORY == 0 {
                return Err("workspace_path_unavailable");
            }
            if !is_directory && attributes & FILE_ATTRIBUTE_DIRECTORY != 0 {
                return Err("workspace_path_unavailable");
            }
            if index + 1 == components.len() {
                return Ok(file.into_std());
            }
            current = Dir::from_std_file(file.into_std());
        }
        Err("workspace_path_unavailable")
    }

    fn read_dir_at(&self, directory: &File) -> Result<Vec<std::ffi::OsString>, &'static str> {
        use std::os::windows::ffi::OsStringExt;
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::Foundation::{GetLastError, ERROR_NO_MORE_FILES};
        use windows_sys::Win32::Storage::FileSystem::{
            FileIdBothDirectoryInfo, GetFileInformationByHandleEx, FILE_ID_BOTH_DIR_INFO,
        };

        const BUFFER_SIZE: usize = 64 * 1024;
        let mut names = Vec::new();
        let mut buffer = vec![0_usize; BUFFER_SIZE / std::mem::size_of::<usize>()];
        loop {
            let success = unsafe {
                GetFileInformationByHandleEx(
                    directory.as_raw_handle() as _,
                    FileIdBothDirectoryInfo,
                    buffer.as_mut_ptr().cast(),
                    BUFFER_SIZE as u32,
                )
            };
            if success == 0 {
                let error = unsafe { GetLastError() };
                if error == ERROR_NO_MORE_FILES {
                    break;
                }
                return Err("workspace_read_denied");
            }
            let mut offset = 0usize;
            loop {
                let buffer_bytes = buffer.as_ptr().cast::<u8>();
                let name_offset = std::mem::offset_of!(FILE_ID_BOTH_DIR_INFO, FileName);
                if offset
                    .checked_add(std::mem::size_of::<FILE_ID_BOTH_DIR_INFO>())
                    .is_none_or(|end| end > BUFFER_SIZE)
                {
                    return Err("workspace_read_denied");
                }
                let entry = unsafe {
                    std::ptr::read_unaligned(
                        buffer_bytes.add(offset).cast::<FILE_ID_BOTH_DIR_INFO>(),
                    )
                };
                let name_bytes = entry.FileNameLength as usize;
                if name_bytes % std::mem::size_of::<u16>() != 0 {
                    return Err("workspace_read_denied");
                }
                let name_length = name_bytes / std::mem::size_of::<u16>();
                if offset
                    .checked_add(name_offset)
                    .and_then(|start| start.checked_add(name_bytes))
                    .is_none_or(|end| end > BUFFER_SIZE)
                {
                    return Err("workspace_read_denied");
                }
                if name_length > 0 {
                    let name = unsafe {
                        std::slice::from_raw_parts(
                            buffer_bytes.add(offset + name_offset).cast::<u16>(),
                            name_length,
                        )
                    };
                    let name = std::ffi::OsString::from_wide(name);
                    if name != "." && name != ".." {
                        names.push(name);
                        if names.len() >= MAX_LIST_ENTRIES {
                            return Ok(names);
                        }
                    }
                }
                if entry.NextEntryOffset == 0 {
                    break;
                }
                let next_offset = entry.NextEntryOffset as usize;
                if next_offset < name_offset + name_bytes
                    || next_offset % std::mem::align_of::<u64>() != 0
                {
                    return Err("workspace_read_denied");
                }
                offset = offset
                    .checked_add(next_offset)
                    .filter(|next| *next < BUFFER_SIZE)
                    .ok_or("workspace_read_denied")?;
            }
        }
        Ok(names)
    }

    fn atomic_write(
        &self,
        relative: &Path,
        expected_old_sha256: Option<&str>,
        content: &[u8],
        change_id: &str,
    ) -> Result<(), &'static str> {
        use cap_std::fs::{Dir, OpenOptions};

        if content.len() > 256 * 1024 || change_id.is_empty() || change_id.len() > 200 {
            return Err("changeset_invalid");
        }
        let name = relative.file_name().ok_or("workspace_path_rejected")?;
        if relative
            .components()
            .any(|item| !matches!(item, Component::Normal(_)))
        {
            return Err("workspace_path_rejected");
        }
        let parent_path = relative
            .parent()
            .filter(|path| !path.as_os_str().is_empty())
            .unwrap_or_else(|| Path::new(""));
        let parent_file = self.open_relative(parent_path, true)?;
        let parent = Dir::from_std_file(parent_file);
        cleanup_change_temps(&parent, change_id)?;

        match expected_old_sha256 {
            Some(expected) => {
                let current = self.open_relative(relative, false)?;
                let metadata = current
                    .metadata()
                    .map_err(|_| "workspace_path_unavailable")?;
                if !metadata.is_file() || has_multiple_links(&metadata, &current) {
                    return Err("workspace_file_rejected");
                }
                if sha256_file(current)? != expected {
                    return Err("workspace_conflict");
                }
            }
            None => match self.open_relative(relative, false) {
                Ok(_) => return Err("workspace_conflict"),
                Err("workspace_path_unavailable") => {}
                Err(error) => return Err(error),
            },
        }

        let temporary_name = format!(".ta-changeset-{}-{}.tmp", change_id, uuid::Uuid::new_v4());
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        let mut temporary = parent
            .open_with(&temporary_name, &options)
            .map_err(|_| "workspace_write_failed")?;
        if temporary
            .write_all(content)
            .and_then(|_| temporary.sync_all())
            .is_err()
        {
            drop(temporary);
            let _ = parent.remove_file(&temporary_name);
            return Err("workspace_write_failed");
        }
        drop(temporary);

        let result = if expected_old_sha256.is_some() {
            parent.rename(&temporary_name, &parent, name)
        } else {
            parent
                .hard_link(&temporary_name, &parent, name)
                .and_then(|_| parent.remove_file(&temporary_name))
        };
        if let Err(error) = result {
            let _ = parent.remove_file(&temporary_name);
            return Err(if error.kind() == std::io::ErrorKind::AlreadyExists {
                "workspace_conflict"
            } else {
                "workspace_write_failed"
            });
        }
        Ok(())
    }
}

#[cfg(target_os = "windows")]
fn sha256_file(mut file: File) -> Result<String, &'static str> {
    use sha2::{Digest, Sha256};
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 8192];
    loop {
        let count = file
            .read(&mut buffer)
            .map_err(|_| "workspace_read_denied")?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
    }
    Ok(format!("{:x}", digest.finalize()))
}

#[cfg(target_os = "windows")]
fn cleanup_change_temps(parent: &cap_std::fs::Dir, change_id: &str) -> Result<(), &'static str> {
    for entry in parent.read_dir(".").map_err(|_| "workspace_write_failed")? {
        let entry = entry.map_err(|_| "workspace_write_failed")?;
        let name = entry.file_name();
        if is_change_temp_name(&name, change_id) {
            parent
                .remove_file(&name)
                .map_err(|_| "workspace_write_failed")?;
        }
    }
    Ok(())
}

#[cfg(target_os = "windows")]
fn map_windows_open_error(error: &std::io::Error) -> &'static str {
    use windows_sys::Win32::Foundation::ERROR_STOPPED_ON_SYMLINK;
    if error.raw_os_error() == Some(ERROR_STOPPED_ON_SYMLINK as i32) {
        "workspace_link_rejected"
    } else if error.kind() == std::io::ErrorKind::PermissionDenied {
        "workspace_read_denied"
    } else {
        "workspace_path_unavailable"
    }
}

#[cfg(not(any(target_os = "linux", target_os = "windows")))]
struct SecureRoot;

#[cfg(not(any(target_os = "linux", target_os = "windows")))]
impl SecureRoot {
    fn open(_: &Path, _: &RootIdentity) -> Result<Self, &'static str> {
        Err("broker_platform_unsupported")
    }
    fn open_relative(&self, _: &Path, _: bool) -> Result<File, &'static str> {
        Err("broker_platform_unsupported")
    }
    fn read_dir_at(&self, _: &File) -> Result<Vec<std::ffi::OsString>, &'static str> {
        Err("broker_platform_unsupported")
    }
}

fn list_entries(
    root: &Path,
    identity: &RootIdentity,
    payload: &Value,
) -> Result<Value, &'static str> {
    let secure_root = SecureRoot::open(root, identity)?;
    let relative = relative_path(payload)?;
    let base = secure_root.open_relative(&relative, true)?;
    let requested_depth = payload
        .get("max_depth")
        .and_then(Value::as_u64)
        .unwrap_or(2) as usize;
    let mut entries = Vec::new();
    walk_entries(
        &secure_root,
        &base,
        &relative,
        0,
        requested_depth.min(MAX_LIST_DEPTH),
        &mut entries,
    )?;
    entries.sort_by(|a, b| a.as_str().cmp(&b.as_str()));
    entries.truncate(MAX_LIST_ENTRIES);
    Ok(serde_json::json!({"files": entries, "truncated": entries.len() == MAX_LIST_ENTRIES}))
}

fn walk_entries(
    secure_root: &SecureRoot,
    directory: &File,
    relative: &Path,
    depth: usize,
    max_depth: usize,
    entries: &mut Vec<Value>,
) -> Result<(), &'static str> {
    if entries.len() >= MAX_LIST_ENTRIES {
        return Ok(());
    }
    let mut children = secure_root.read_dir_at(directory)?;
    children.sort();
    for name in children {
        if entries.len() >= MAX_LIST_ENTRIES {
            break;
        }
        let Some(name_text) = name.to_str() else {
            continue;
        };
        if is_sensitive_name(name_text) {
            continue;
        }
        let child_relative = relative.join(&name);
        let child = match secure_root.open_relative(&child_relative, false) {
            Ok(file) => file,
            Err("workspace_link_rejected") => continue,
            Err(_) => match secure_root.open_relative(&child_relative, true) {
                Ok(file) => file,
                Err("workspace_link_rejected") => continue,
                Err(_) => continue,
            },
        };
        let metadata = child.metadata().map_err(|_| "workspace_read_denied")?;
        if metadata.is_file() && !has_multiple_links(&metadata, &child) {
            entries.push(Value::String(
                child_relative.to_string_lossy().replace('\\', "/"),
            ));
        }
        if metadata.is_dir() && depth < max_depth {
            walk_entries(
                secure_root,
                &child,
                &child_relative,
                depth + 1,
                max_depth,
                entries,
            )?;
        }
    }
    Ok(())
}

fn read_file(root: &Path, identity: &RootIdentity, payload: &Value) -> Result<Value, &'static str> {
    let secure_root = SecureRoot::open(root, identity)?;
    let relative = relative_path(payload)?;
    let file = secure_root.open_relative(&relative, false)?;
    let metadata = file.metadata().map_err(|_| "workspace_read_denied")?;
    if !metadata.is_file() {
        return Err("workspace_not_file");
    }
    if has_multiple_links(&metadata, &file) {
        return Err("workspace_hardlink_rejected");
    }
    if metadata.len() > MAX_READ_BYTES as u64 {
        return Err("workspace_file_too_large");
    }
    let bytes = read_limited(file, MAX_READ_BYTES)?;
    let content = String::from_utf8(bytes).map_err(|_| "workspace_not_text")?;
    if content.contains('\0') {
        return Err("workspace_not_text");
    }
    Ok(
        serde_json::json!({"path": relative.to_string_lossy().replace('\\', "/"), "content": content, "bytes": content.len()}),
    )
}

fn read_limited(file: File, limit: usize) -> Result<Vec<u8>, &'static str> {
    let mut bytes = Vec::with_capacity(limit.min(8192));
    file.take((limit + 1) as u64)
        .read_to_end(&mut bytes)
        .map_err(|_| "workspace_read_denied")?;
    if bytes.len() > limit {
        return Err("workspace_file_too_large");
    }
    Ok(bytes)
}

fn search_text(
    root: &Path,
    identity: &RootIdentity,
    payload: &Value,
) -> Result<Value, &'static str> {
    let secure_root = SecureRoot::open(root, identity)?;
    let base = relative_path(payload)?;
    let query = payload
        .get("query")
        .and_then(Value::as_str)
        .ok_or("bridge_invalid_request")?;
    if query.is_empty() || query.len() > 512 || query.chars().any(char::is_control) {
        return Err("bridge_invalid_request");
    }
    let limit = payload
        .get("max_results")
        .and_then(Value::as_u64)
        .unwrap_or(50)
        .min(MAX_SEARCH_RESULTS as u64) as usize;
    let mut files = Vec::new();
    collect_search_files(
        &secure_root,
        &secure_root.open_relative(&base, true)?,
        &base,
        0,
        &mut files,
    )?;
    let mut matches = Vec::new();
    let mut scanned = 0usize;
    for relative in files {
        if matches.len() >= limit || scanned >= MAX_SEARCH_BYTES {
            break;
        }
        let file = secure_root.open_relative(&relative, false)?;
        let metadata = file.metadata().map_err(|_| "workspace_read_denied")?;
        if !metadata.is_file() {
            continue;
        }
        let allowance = (MAX_SEARCH_BYTES - scanned).min(MAX_READ_BYTES);
        if metadata.len() > allowance as u64 {
            continue;
        }
        let bytes = read_limited(file, allowance)?;
        scanned += bytes.len();
        let Ok(content) = String::from_utf8(bytes) else {
            continue;
        };
        if content.contains('\0') {
            continue;
        }
        for (line_number, line) in content.lines().enumerate() {
            if line.contains(query) {
                matches.push(
                    serde_json::json!({"path": relative.to_string_lossy().replace('\\', "/"),
                    "line": line_number + 1, "text": line.chars().take(1000).collect::<String>()}),
                );
                if matches.len() >= limit {
                    break;
                }
            }
        }
    }
    Ok(
        serde_json::json!({"matches": matches, "truncated": matches.len() >= limit || scanned >= MAX_SEARCH_BYTES,
        "scanned_bytes": scanned}),
    )
}

fn collect_search_files(
    secure_root: &SecureRoot,
    directory: &File,
    relative: &Path,
    depth: usize,
    files: &mut Vec<PathBuf>,
) -> Result<(), &'static str> {
    if depth > MAX_LIST_DEPTH || files.len() >= MAX_LIST_ENTRIES {
        return Ok(());
    }
    let mut children = secure_root.read_dir_at(directory)?;
    children.sort();
    for name in children {
        if name.to_str().map_or(true, is_sensitive_name) {
            continue;
        }
        let child_relative = relative.join(&name);
        let child = match secure_root.open_relative(&child_relative, false) {
            Ok(file) => file,
            Err("workspace_link_rejected") => continue,
            Err(_) => match secure_root.open_relative(&child_relative, true) {
                Ok(file) => file,
                Err(_) => continue,
            },
        };
        let metadata = child.metadata().map_err(|_| "workspace_read_denied")?;
        if metadata.is_dir() {
            collect_search_files(secure_root, &child, &child_relative, depth + 1, files)?;
        } else if metadata.is_file() && !has_multiple_links(&metadata, &child) {
            files.push(child_relative);
        }
        if files.len() >= MAX_LIST_ENTRIES {
            break;
        }
    }
    Ok(())
}

fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    let mut difference = left.len() ^ right.len();
    for index in 0..left.len().max(right.len()) {
        difference |= usize::from(
            left.get(index).copied().unwrap_or(0) ^ right.get(index).copied().unwrap_or(0),
        );
    }
    difference == 0
}

#[cfg(test)]
mod tests {
    use super::{
        validate_request, BridgeRequest, BridgeServer, WorkspaceBroker,
        DESKTOP_BRIDGE_PROTOCOL_VERSION, MAX_BRIDGE_MESSAGE_BYTES,
    };
    use std::collections::HashSet;
    use std::io::{BufRead, BufReader, Write};
    use std::net::TcpStream;
    use std::time::{SystemTime, UNIX_EPOCH};

    static BRIDGE_LISTENER_TEST_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    #[test]
    fn workspace_broker_requires_exact_run_project_grant_and_reads_registered_root_only() {
        let temporary =
            std::env::temp_dir().join(format!("workspace-broker-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&temporary).unwrap();
        std::fs::write(temporary.join("README.md"), "project data").unwrap();
        let mut broker = WorkspaceBroker::default();
        let grant = broker
            .authorize_run("session-1", "run-1", "project-1", &temporary)
            .unwrap();
        let request = request();

        let now = super::now_ms();
        assert_eq!(broker.execute(&request, now).unwrap_err(), "grant_expired");
        let mut matching = request;
        matching.grant = grant.clone();
        let result = broker.execute(&matching, now).unwrap();
        assert_eq!(result["content"], "project data");

        let mut other_run = matching.clone();
        other_run.run_id = "run-2".into();
        assert_eq!(
            broker.execute(&other_run, now).unwrap_err(),
            "grant_identity_mismatch"
        );
        let mut revoke = matching.clone();
        revoke.operation = "revoke_run".into();
        revoke.payload = serde_json::json!({});
        assert_eq!(broker.execute(&revoke, now).unwrap()["revoked"], true);
        assert_eq!(broker.execute(&matching, now).unwrap_err(), "grant_expired");
        let _ = std::fs::remove_dir_all(temporary);
    }

    #[test]
    fn duplicate_approval_reuses_token_and_rejects_a_different_root_for_the_same_run() {
        let first_root =
            std::env::temp_dir().join(format!("workspace-grant-a-{}", uuid::Uuid::new_v4()));
        let second_root =
            std::env::temp_dir().join(format!("workspace-grant-b-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&first_root).unwrap();
        std::fs::create_dir_all(&second_root).unwrap();
        let mut broker = WorkspaceBroker::default();
        let first = broker
            .authorize_run("session-1", "run-1", "project-1", &first_root)
            .unwrap();
        let duplicate = broker
            .authorize_run("session-1", "run-1", "project-1", &first_root)
            .unwrap();
        assert_eq!(duplicate, first);
        assert_eq!(
            broker
                .authorize_run("session-1", "run-1", "project-1", &second_root)
                .unwrap_err(),
            "grant_identity_mismatch"
        );
        assert!(broker.grants.contains_key(&first));
        let _ = std::fs::remove_dir_all(first_root);
        let _ = std::fs::remove_dir_all(second_root);
    }

    #[test]
    fn workspace_broker_expires_grants_and_rejects_replaced_project_roots() {
        let temporary =
            std::env::temp_dir().join(format!("workspace-identity-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&temporary).unwrap();
        std::fs::write(temporary.join("file.txt"), "original").unwrap();
        let mut broker = WorkspaceBroker::default();
        let grant = broker
            .authorize_run("session-1", "run-1", "project-1", &temporary)
            .unwrap();
        let stored = broker.grants.get_mut(&grant).unwrap();
        stored.expires_at_ms = 0;
        let mut expired_request = request();
        expired_request.grant = grant.clone();
        assert_eq!(
            broker
                .execute(&expired_request, super::now_ms())
                .unwrap_err(),
            "grant_expired"
        );

        let renewed = broker
            .authorize_run("session-1", "run-1", "project-1", &temporary)
            .unwrap();
        let old_identity = broker.grants.get(&renewed).unwrap().root_identity.clone();
        std::fs::remove_dir_all(&temporary).unwrap();
        std::fs::create_dir_all(&temporary).unwrap();
        std::fs::write(temporary.join("file.txt"), "replacement").unwrap();
        let replacement = super::root_identity(&temporary).unwrap();
        if old_identity.file_id != replacement.file_id {
            let mut request = request();
            request.grant = renewed;
            assert_eq!(
                broker.execute(&request, super::now_ms()).unwrap_err(),
                "project_identity_changed"
            );
        }
        let _ = std::fs::remove_dir_all(temporary);
    }

    #[test]
    fn restored_run_grant_keeps_token_identity_and_revalidates_registered_root() {
        let root = std::env::temp_dir().join(format!("workspace-restore-{}", uuid::Uuid::new_v4()));
        let other_root = root.with_extension("other");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::create_dir_all(&other_root).unwrap();
        let mut original = WorkspaceBroker::default();
        let grant = original
            .authorize_run("session-1", "run-1", "project-1", &root)
            .unwrap();
        assert_eq!(grant.len(), 32);
        let mut restarted = WorkspaceBroker::default();
        restarted
            .restore_run_grant(&grant, "session-1", "run-1", "project-1", &root)
            .unwrap();
        restarted
            .restore_run_grant(&grant, "session-1", "run-1", "project-1", &root)
            .unwrap();
        assert_eq!(
            restarted.restore_run_grant(&grant, "session-1", "run-2", "project-1", &root),
            Err("grant_identity_mismatch")
        );
        assert_eq!(
            restarted.restore_run_grant(&grant, "session-1", "run-1", "project-1", &other_root),
            Err("grant_identity_mismatch")
        );
        assert_eq!(
            restarted.restore_run_grant("not-a-token", "session-1", "run-1", "project-1", &root),
            Err("bridge_invalid_request")
        );
        let _ = std::fs::remove_dir_all(root);
        let _ = std::fs::remove_dir_all(other_root);
    }

    #[cfg(unix)]
    #[test]
    fn workspace_broker_rejects_registered_root_replaced_by_symlink_before_approval() {
        let parent =
            std::env::temp_dir().join(format!("workspace-root-link-{}", uuid::Uuid::new_v4()));
        let registered = parent.join("project");
        let outside = parent.join("outside");
        std::fs::create_dir_all(&registered).unwrap();
        std::fs::create_dir_all(&outside).unwrap();
        let moved = parent.join("project-moved");
        std::fs::rename(&registered, &moved).unwrap();
        std::os::unix::fs::symlink(&outside, &registered).unwrap();
        let mut broker = WorkspaceBroker::default();
        assert_eq!(
            broker
                .authorize_run("session-1", "run-1", "project-1", &registered)
                .unwrap_err(),
            "project_unavailable"
        );
        assert!(broker.grants.is_empty());
        let _ = std::fs::remove_dir_all(parent);
    }

    #[test]
    fn workspace_broker_rejects_traversal_absolute_sensitive_and_symlink_paths() {
        let temporary =
            std::env::temp_dir().join(format!("workspace-path-{}", uuid::Uuid::new_v4()));
        let outside =
            std::env::temp_dir().join(format!("workspace-outside-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&temporary).unwrap();
        std::fs::create_dir_all(&outside).unwrap();
        std::fs::write(outside.join("secret.txt"), "secret").unwrap();
        std::fs::create_dir_all(temporary.join(".git")).unwrap();
        std::fs::write(temporary.join(".git/config"), "secret").unwrap();
        #[cfg(unix)]
        std::os::unix::fs::symlink(&outside, temporary.join("linked")).unwrap();
        #[cfg(any(unix, windows))]
        std::fs::hard_link(outside.join("secret.txt"), temporary.join("hardlinked.txt")).unwrap();
        let mut broker = WorkspaceBroker::default();
        let grant = broker
            .authorize_run("session-1", "run-1", "project-1", &temporary)
            .unwrap();
        for path in [
            "../secret.txt",
            "/etc/passwd",
            "C:\\Windows\\win.ini",
            ".git/config",
        ] {
            let mut request = request();
            request.grant = grant.clone();
            request.payload = serde_json::json!({"relative_path": path});
            assert!(
                broker.execute(&request, super::now_ms()).is_err(),
                "accepted {path}"
            );
        }
        #[cfg(unix)]
        {
            let mut link_request = request();
            link_request.grant = grant.clone();
            link_request.payload = serde_json::json!({"relative_path": "linked/secret.txt"});
            assert_eq!(
                broker.execute(&link_request, super::now_ms()).unwrap_err(),
                "workspace_link_rejected"
            );
            let mut hardlink = request();
            hardlink.grant = grant;
            hardlink.payload = serde_json::json!({"relative_path": "hardlinked.txt"});
            assert_eq!(
                broker.execute(&hardlink, super::now_ms()).unwrap_err(),
                "workspace_hardlink_rejected"
            );
        }
        #[cfg(windows)]
        {
            let mut hardlink = request();
            hardlink.grant = grant;
            hardlink.payload = serde_json::json!({"relative_path": "hardlinked.txt"});
            assert_eq!(
                broker.execute(&hardlink, super::now_ms()).unwrap_err(),
                "workspace_hardlink_rejected"
            );
        }
        let _ = std::fs::remove_dir_all(temporary);
        let _ = std::fs::remove_dir_all(outside);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn handle_relative_traversal_stays_on_original_directory_after_path_replacement() {
        let root =
            std::env::temp_dir().join(format!("workspace-race-root-{}", uuid::Uuid::new_v4()));
        let outside =
            std::env::temp_dir().join(format!("workspace-race-outside-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(root.join("sub")).unwrap();
        std::fs::create_dir_all(&outside).unwrap();
        std::fs::write(root.join("sub/original.txt"), "inside").unwrap();
        std::fs::write(outside.join("secret.txt"), "outside secret").unwrap();
        let identity = super::root_identity(&root).unwrap();
        let secure = super::SecureRoot::open(&root, &identity).unwrap();
        let pinned_subdirectory = secure.open_relative(super::Path::new("sub"), true).unwrap();

        std::fs::rename(root.join("sub"), root.join("sub-moved")).unwrap();
        std::os::unix::fs::symlink(&outside, root.join("sub")).unwrap();

        assert_eq!(
            secure
                .open_relative(super::Path::new("sub/secret.txt"), false)
                .unwrap_err(),
            "workspace_link_rejected"
        );
        let names = secure.read_dir_at(&pinned_subdirectory).unwrap();
        assert_eq!(names, vec![std::ffi::OsString::from("original.txt")]);
        let _ = std::fs::remove_dir_all(root);
        let _ = std::fs::remove_dir_all(outside);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn secure_root_atomic_write_checks_old_hash_and_never_follows_links() {
        use sha2::{Digest, Sha256};

        let root = std::env::temp_dir().join(format!("workspace-write-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(root.join("src")).unwrap();
        std::fs::write(root.join("src/main.py"), "old\n").unwrap();
        let identity = super::root_identity(&root).unwrap();
        let secure = super::SecureRoot::open(&root, &identity).unwrap();
        let old_hash = format!("{:x}", Sha256::digest(b"old\n"));

        secure
            .atomic_write(
                std::path::Path::new("src/main.py"),
                Some(&old_hash),
                b"new\n",
                "change-1",
            )
            .unwrap();
        assert_eq!(std::fs::read(root.join("src/main.py")).unwrap(), b"new\n");

        let stale = root.join(format!(
            "src/.ta-changeset-clean-{}.tmp",
            uuid::Uuid::new_v4()
        ));
        let unrelated = root.join(format!(
            "src/.ta-changeset-other-{}.tmp",
            uuid::Uuid::new_v4()
        ));
        std::fs::write(&stale, b"interrupted temp").unwrap();
        std::fs::write(&unrelated, b"unrelated temp").unwrap();
        let new_hash = format!("{:x}", Sha256::digest(b"new\n"));
        secure
            .atomic_write(
                std::path::Path::new("src/main.py"),
                Some(&new_hash),
                b"newer\n",
                "clean",
            )
            .unwrap();
        assert!(!stale.exists());
        assert!(unrelated.exists());

        assert_eq!(
            secure
                .atomic_write(
                    std::path::Path::new("src/main.py"),
                    Some(&old_hash),
                    b"overwrite\n",
                    "change-2"
                )
                .unwrap_err(),
            "workspace_conflict"
        );
        assert_eq!(std::fs::read(root.join("src/main.py")).unwrap(), b"newer\n");

        let outside = root.with_extension("outside");
        std::fs::create_dir_all(&outside).unwrap();
        std::fs::write(outside.join("target"), "outside").unwrap();
        std::os::unix::fs::symlink(outside.join("target"), root.join("src/linked")).unwrap();
        assert_eq!(
            secure
                .atomic_write(
                    std::path::Path::new("src/linked"),
                    None,
                    b"escape",
                    "change-3"
                )
                .unwrap_err(),
            "workspace_link_rejected"
        );
        assert_eq!(std::fs::read(outside.join("target")).unwrap(), b"outside");
        let _ = std::fs::remove_dir_all(root);
        let _ = std::fs::remove_dir_all(outside);
    }

    #[cfg(any(target_os = "linux", target_os = "windows"))]
    #[test]
    fn changeset_approval_binds_digest_is_single_use_and_preflights_all_files() {
        use sha2::{Digest, Sha256};

        let root = std::env::temp_dir().join(format!("workspace-commit-{}", uuid::Uuid::new_v4()));
        let journal = root.with_extension("journal");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("one.txt"), "one-old").unwrap();
        std::fs::write(root.join("two.txt"), "unexpected").unwrap();
        let mut broker = WorkspaceBroker::default();
        broker.journal_dir = Some(journal.clone());
        let grant = broker
            .authorize_run("session-1", "run-1", "project-1", &root)
            .unwrap();
        let mut changeset = serde_json::json!({
            "change_id":"change-1", "run_id":"run-1", "session_id":"session-1",
            "project_id":"project-1",
            "authorization_version":format!("{:x}", Sha256::digest(grant.as_bytes())),
            "changes":[
                {"relative_path":"one.txt", "operation":"modify",
                 "expected_old_sha256":format!("{:x}", Sha256::digest(b"one-old")),
                 "base_content":"one-old", "content":"one-new", "summary":""},
                {"relative_path":"two.txt", "operation":"modify",
                 "expected_old_sha256":format!("{:x}", Sha256::digest(b"two-old")),
                 "base_content":"two-old", "content":"two-new", "summary":""},
                {"relative_path":"new.txt", "operation":"add",
                 "expected_old_sha256":null, "base_content":null,
                 "content":"new-file", "summary":""}
            ]
        });
        let digest = super::changeset_digest(&changeset).unwrap();
        let now = super::now_ms();
        let expired = broker
            .issue_approval(&grant, &changeset, &digest, now)
            .unwrap();
        assert_eq!(
            broker
                .commit_approved(
                    &expired,
                    &changeset,
                    &digest,
                    now + super::MAX_APPROVAL_LIFETIME_MS + 1
                )
                .unwrap_err(),
            "approval_ticket_invalid"
        );
        assert_eq!(std::fs::read(root.join("one.txt")).unwrap(), b"one-old");
        assert!(!root.join("new.txt").exists());

        let tampered_ticket = broker
            .issue_approval(&grant, &changeset, &digest, now)
            .unwrap();
        let mut tampered = changeset.clone();
        tampered["changes"][0]["content"] = serde_json::Value::String("unreviewed".into());
        assert_eq!(
            broker
                .commit_approved(&tampered_ticket, &tampered, &digest, now)
                .unwrap_err(),
            "approval_ticket_invalid"
        );
        assert_eq!(std::fs::read(root.join("one.txt")).unwrap(), b"one-old");

        let approval = broker
            .issue_approval(&grant, &changeset, &digest, super::now_ms())
            .unwrap();
        assert_eq!(
            broker
                .commit_approved(&approval, &changeset, &digest, super::now_ms())
                .unwrap_err(),
            "workspace_conflict"
        );
        assert_eq!(std::fs::read(root.join("one.txt")).unwrap(), b"one-old");
        assert_eq!(std::fs::read(root.join("two.txt")).unwrap(), b"unexpected");
        assert!(!root.join("new.txt").exists());
        assert!(broker
            .commit_approved(&approval, &changeset, &digest, super::now_ms())
            .is_err());

        std::fs::write(root.join("two.txt"), "two-old").unwrap();
        changeset["change_id"] = serde_json::Value::String("change-2".into());
        let digest = super::changeset_digest(&changeset).unwrap();
        let result = broker
            .host_apply_changeset(
                "session-1",
                "run-1",
                "project-1",
                &root,
                &changeset,
                &digest,
                super::now_ms(),
            )
            .unwrap();
        assert_eq!(result["status"], "completed");
        assert_eq!(std::fs::read(root.join("one.txt")).unwrap(), b"one-new");
        assert_eq!(std::fs::read(root.join("two.txt")).unwrap(), b"two-new");
        assert_eq!(std::fs::read(root.join("new.txt")).unwrap(), b"new-file");
        let replay = broker
            .host_apply_changeset(
                "session-1",
                "run-1",
                "project-1",
                &root,
                &changeset,
                &digest,
                super::now_ms(),
            )
            .unwrap();
        assert_eq!(replay["status"], "completed");
        assert_eq!(std::fs::read(root.join("one.txt")).unwrap(), b"one-new");
        std::fs::write(root.join("one.txt"), "changed after commit").unwrap();
        let stale_ticket = broker
            .issue_approval(&grant, &changeset, &digest, super::now_ms())
            .unwrap();
        assert_eq!(
            broker
                .commit_approved(&stale_ticket, &changeset, &digest, super::now_ms())
                .unwrap_err(),
            "commit_needs_inspection"
        );
        assert!(journal
            .join(format!(
                "{}-change-2.json",
                super::sha256_bytes(b"project-1")
            ))
            .exists());
        let _ = std::fs::remove_dir_all(root);
        let _ = std::fs::remove_dir_all(journal);
    }

    #[test]
    fn commit_journal_rejects_invalid_schema_and_inconsistent_statuses() {
        let journal = super::CommitJournal {
            schema_version: 2,
            change_id: "change-1".into(),
            digest: "a".repeat(64),
            status: "completed".into(),
            files: vec![super::JournalFile {
                relative_path: "one.txt".into(),
                status: "pending".into(),
                error: None,
            }],
        };
        assert_eq!(
            super::validate_commit_journal(&journal),
            Err("commit_journal_corrupt")
        );

        let mut journal = journal;
        journal.schema_version = 1;
        assert_eq!(
            super::validate_commit_journal(&journal),
            Err("commit_journal_corrupt")
        );

        journal.status = "mystery".into();
        journal.files[0].status = "applied".into();
        assert_eq!(
            super::validate_commit_journal(&journal),
            Err("commit_journal_corrupt")
        );
    }

    #[cfg(any(target_os = "linux", target_os = "windows"))]
    #[test]
    fn partial_commit_retries_only_unapplied_files_and_completes_journal() {
        use sha2::{Digest, Sha256};
        use std::path::PathBuf;

        let root = std::env::var_os("TOMATO_AGENT_TEST_JOURNAL_ROOT")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                std::env::temp_dir().join(format!("workspace-partial-{}", uuid::Uuid::new_v4()))
            });
        let journal_dir = root.with_extension("journal");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("one.txt"), "one-old").unwrap();
        std::fs::write(root.join("two.txt"), "two-old").unwrap();

        let mut broker = WorkspaceBroker::default();
        broker.journal_dir = Some(journal_dir.clone());
        broker.fail_before_write_for = Some("two.txt".into());
        let grant = broker
            .authorize_run("session-1", "run-1", "project-1", &root)
            .unwrap();
        let retry_change_id = "01234567-1234-1234-1234-123456789012";
        let changeset = serde_json::json!({
            "change_id":retry_change_id, "run_id":"run-1", "session_id":"session-1",
            "project_id":"project-1",
            "authorization_version":format!("{:x}", Sha256::digest(grant.as_bytes())),
            "changes":[
                {"relative_path":"one.txt", "operation":"modify",
                 "expected_old_sha256":format!("{:x}", Sha256::digest(b"one-old")),
                 "base_content":"one-old", "content":"one-new", "summary":""},
                {"relative_path":"two.txt", "operation":"modify",
                 "expected_old_sha256":format!("{:x}", Sha256::digest(b"two-old")),
                 "base_content":"two-old", "content":"two-new", "summary":""}
            ]
        });
        let digest = super::changeset_digest(&changeset).unwrap();

        let approval = broker
            .issue_approval(&grant, &changeset, &digest, super::now_ms())
            .unwrap();
        let result = broker
            .commit_approved(&approval, &changeset, &digest, super::now_ms())
            .unwrap();
        assert_eq!(result["status"], "partial");
        assert_eq!(result["files"][0]["status"], "applied");
        assert_eq!(result["files"][1]["status"], "failed");
        assert_eq!(std::fs::read(root.join("one.txt")).unwrap(), b"one-new");
        assert_eq!(std::fs::read(root.join("two.txt")).unwrap(), b"two-old");

        broker.fail_before_write_for = None;
        let approval = broker
            .issue_approval(&grant, &changeset, &digest, super::now_ms())
            .unwrap();
        let result = broker
            .commit_approved(&approval, &changeset, &digest, super::now_ms())
            .unwrap();
        assert_eq!(result["status"], "completed");
        assert_eq!(result["files"][0]["status"], "applied");
        assert_eq!(result["files"][1]["status"], "applied");
        assert_eq!(std::fs::read(root.join("one.txt")).unwrap(), b"one-new");
        assert_eq!(std::fs::read(root.join("two.txt")).unwrap(), b"two-new");

        std::fs::write(root.join("only.txt"), "only-old").unwrap();
        let only = serde_json::json!({
            "change_id":"change-single-fail", "run_id":"run-1", "session_id":"session-1",
            "project_id":"project-1", "authorization_version":format!("{:x}", Sha256::digest(grant.as_bytes())),
            "changes":[{"relative_path":"only.txt", "operation":"modify",
                "expected_old_sha256":format!("{:x}", Sha256::digest(b"only-old")),
                "base_content":"only-old", "content":"only-new", "summary":""}]
        });
        let only_digest = super::changeset_digest(&only).unwrap();
        broker.fail_before_write_for = Some("only.txt".into());
        let only_approval = broker
            .issue_approval(&grant, &only, &only_digest, super::now_ms())
            .unwrap();
        let failed = broker
            .commit_approved(&only_approval, &only, &only_digest, super::now_ms())
            .unwrap();
        assert_eq!(failed["status"], "failed");
        assert_eq!(failed["files"][0]["status"], "failed");
        assert_eq!(std::fs::read(root.join("only.txt")).unwrap(), b"only-old");

        let journal_path = journal_dir.join(format!(
            "{}-{retry_change_id}.json",
            super::sha256_bytes(b"project-1")
        ));
        std::fs::write(&journal_path, b"not json").unwrap();
        assert_eq!(
            broker
                .inspect_commit("project-1", retry_change_id, &digest)
                .unwrap_err(),
            "commit_journal_corrupt"
        );

        let _ = std::fs::remove_dir_all(root);
        let _ = std::fs::remove_dir_all(journal_dir);
    }

    #[cfg(windows)]
    #[test]
    fn journal_recovers_backup_after_interrupted_windows_replace() {
        let root = std::env::temp_dir().join(format!("journal-backup-{}", uuid::Uuid::new_v4()));
        let journal = root.join("change.json");
        let backup = super::journal_backup_path(&journal);
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(&journal, b"last durable journal").unwrap();
        super::move_journal_file(&journal, &backup).unwrap();

        super::recover_journal_backup(&journal).unwrap();

        assert_eq!(std::fs::read(&journal).unwrap(), b"last durable journal");
        assert!(!backup.exists());
        let _ = std::fs::remove_dir_all(root);
    }

    #[cfg(any(target_os = "linux", target_os = "windows"))]
    #[test]
    fn journal_recovers_after_atomic_replace_before_file_status_persist() {
        use sha2::{Digest, Sha256};

        let root = std::env::temp_dir().join(format!("workspace-crash-{}", uuid::Uuid::new_v4()));
        let journal_dir = root.with_extension("journal");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("one.txt"), "one-old").unwrap();
        std::fs::write(root.join("two.txt"), "two-old").unwrap();
        let changeset_value = serde_json::json!({
            "change_id":"change-crash", "run_id":"run-crash", "session_id":"session-crash",
            "project_id":"project-crash", "authorization_version":"a".repeat(64),
            "changes":[
                {"relative_path":"one.txt", "operation":"modify",
                 "expected_old_sha256":format!("{:x}", Sha256::digest(b"one-old")),
                 "base_content":"one-old", "content":"one-new", "summary":""},
                {"relative_path":"two.txt", "operation":"modify",
                 "expected_old_sha256":format!("{:x}", Sha256::digest(b"two-old")),
                 "base_content":"two-old", "content":"two-new", "summary":""}
            ]
        });
        let changeset: super::ChangeSetRecord =
            serde_json::from_value(changeset_value.clone()).unwrap();
        let digest = super::changeset_digest(&changeset_value).unwrap();
        let identity = super::root_identity(&root).unwrap();
        let secure = super::SecureRoot::open(&root, &identity).unwrap();
        let mut interrupted = WorkspaceBroker::default();
        interrupted.journal_dir = Some(journal_dir.clone());
        interrupted.crash_after_write_for = Some("one.txt".into());

        assert_eq!(
            interrupted.apply_changeset_files(&secure, &changeset, &digest),
            Err("simulated_process_crash")
        );
        assert_eq!(std::fs::read(root.join("one.txt")).unwrap(), b"one-new");
        assert_eq!(std::fs::read(root.join("two.txt")).unwrap(), b"two-old");

        let restarted = WorkspaceBroker {
            journal_dir: Some(journal_dir.clone()),
            ..Default::default()
        };
        let recovered = restarted
            .apply_changeset_files(&secure, &changeset, &digest)
            .unwrap();
        assert_eq!(recovered["status"], "completed");
        assert_eq!(std::fs::read(root.join("one.txt")).unwrap(), b"one-new");
        assert_eq!(std::fs::read(root.join("two.txt")).unwrap(), b"two-new");

        let _ = std::fs::remove_dir_all(root);
        let _ = std::fs::remove_dir_all(journal_dir);
    }

    #[cfg(any(target_os = "linux", target_os = "windows"))]
    #[test]
    fn journal_recovers_after_worker_process_termination() {
        use sha2::{Digest, Sha256};
        use std::path::PathBuf;
        use std::process::Command;

        fn changeset_value() -> serde_json::Value {
            serde_json::json!({
                "change_id":"process-crash", "run_id":"run-process-crash",
                "session_id":"session-process-crash", "project_id":"project-process-crash",
                "authorization_version":"a".repeat(64),
                "changes":[
                    {"relative_path":"one.txt", "operation":"modify",
                     "expected_old_sha256":format!("{:x}", Sha256::digest(b"one-old")),
                     "base_content":"one-old", "content":"one-new", "summary":""},
                    {"relative_path":"two.txt", "operation":"modify",
                     "expected_old_sha256":format!("{:x}", Sha256::digest(b"two-old")),
                     "base_content":"two-old", "content":"two-new", "summary":""}
                ]
            })
        }

        const ROOT_ENV: &str = "TOMATO_TEST_CRASH_ROOT";
        const JOURNAL_ENV: &str = "TOMATO_TEST_CRASH_JOURNAL";
        if let Some(root) = std::env::var_os(ROOT_ENV) {
            let root = PathBuf::from(root);
            let journal_dir = PathBuf::from(std::env::var_os(JOURNAL_ENV).unwrap());
            let changeset_value = changeset_value();
            let changeset: super::ChangeSetRecord =
                serde_json::from_value(changeset_value.clone()).unwrap();
            let digest = super::changeset_digest(&changeset_value).unwrap();
            let secure =
                super::SecureRoot::open(&root, &super::root_identity(&root).unwrap()).unwrap();
            let broker = WorkspaceBroker {
                journal_dir: Some(journal_dir),
                crash_process_after_write_for: Some("one.txt".into()),
                ..Default::default()
            };
            let _ = broker.apply_changeset_files(&secure, &changeset, &digest);
            panic!("the worker should have terminated after the atomic replacement");
        }

        let root =
            std::env::temp_dir().join(format!("workspace-process-crash-{}", uuid::Uuid::new_v4()));
        let journal_dir = root.with_extension("journal");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("one.txt"), "one-old").unwrap();
        std::fs::write(root.join("two.txt"), "two-old").unwrap();
        let child = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "desktop_bridge::tests::journal_recovers_after_worker_process_termination",
            ])
            .env(ROOT_ENV, &root)
            .env(JOURNAL_ENV, &journal_dir)
            .output()
            .unwrap();
        assert!(
            !child.status.success(),
            "worker unexpectedly exited successfully: {}",
            String::from_utf8_lossy(&child.stdout)
        );
        assert_eq!(std::fs::read(root.join("one.txt")).unwrap(), b"one-new");
        assert_eq!(std::fs::read(root.join("two.txt")).unwrap(), b"two-old");

        let changeset_value = changeset_value();
        let changeset: super::ChangeSetRecord =
            serde_json::from_value(changeset_value.clone()).unwrap();
        let digest = super::changeset_digest(&changeset_value).unwrap();
        let secure = super::SecureRoot::open(&root, &super::root_identity(&root).unwrap()).unwrap();
        let restarted = WorkspaceBroker {
            journal_dir: Some(journal_dir.clone()),
            ..Default::default()
        };
        let recovered = restarted
            .apply_changeset_files(&secure, &changeset, &digest)
            .unwrap();
        assert_eq!(recovered["status"], "completed");
        assert_eq!(std::fs::read(root.join("one.txt")).unwrap(), b"one-new");
        assert_eq!(std::fs::read(root.join("two.txt")).unwrap(), b"two-new");

        let _ = std::fs::remove_dir_all(root);
        let _ = std::fs::remove_dir_all(journal_dir);
    }

    #[test]
    fn changeset_digest_matches_python_canonical_json_for_unicode_content() {
        let changeset = serde_json::json!({
            "change_id":"change-1", "run_id":"run-1", "session_id":"session-1",
            "project_id":"project-1", "authorization_version":"a".repeat(64),
            "changes":[{"relative_path":"src/app.py", "operation":"modify",
                "expected_old_sha256":"9160d4be34c8695bd172a76c7c7966587ea5a4d991ad22c87b2b91af54aa9ebb",
                "base_content":"before\n", "content":"after\n", "summary":"更新输出"}]
        });
        assert_eq!(
            super::changeset_digest(&changeset).unwrap(),
            "2a9808af3debb6132976d470f0955258c2810e4bf48fdb2200ee5dbb2a543cbc"
        );
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn windows_broker_enumerates_from_handles_and_rejects_directory_reparse_points() {
        use std::io::Read;
        use std::os::windows::fs::symlink_dir;
        use std::path::Path;
        use std::process::Command;

        fn create_directory_link(target: &Path, link: &Path) {
            if symlink_dir(target, link).is_ok() {
                return;
            }
            let status = Command::new("powershell.exe")
                .args([
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "New-Item -ItemType Junction -Path $env:TOMATO_TEST_LINK -Target $env:TOMATO_TEST_TARGET -ErrorAction Stop | Out-Null",
                ])
                .env("TOMATO_TEST_LINK", link)
                .env("TOMATO_TEST_TARGET", target)
                .status()
                .expect("powershell.exe must be available on Windows");
            assert!(
                status.success(),
                "could not create test junction at {link:?}"
            );
        }

        let parent = std::env::temp_dir().join(format!("workspace-win-{}", uuid::Uuid::new_v4()));
        let root = parent.join("project");
        let outside = parent.join("outside");
        std::fs::create_dir_all(root.join("src")).unwrap();
        std::fs::create_dir_all(&outside).unwrap();
        std::fs::write(root.join("src/main.py"), "inside").unwrap();
        std::fs::write(outside.join("secret.txt"), "outside").unwrap();

        let identity = super::root_identity(&root).unwrap();
        let secure = super::SecureRoot::open(&root, &identity).unwrap();
        let root_file = secure.open_relative(super::Path::new("."), true).unwrap();
        let mut names = secure.read_dir_at(&root_file).unwrap();
        names.sort();
        assert_eq!(names, vec![std::ffi::OsString::from("src")]);
        let mut file = secure
            .open_relative(super::Path::new("src/main.py"), false)
            .unwrap();
        let mut content = String::new();
        file.read_to_string(&mut content).unwrap();
        assert_eq!(content, "inside");

        let link = root.join("linked");
        create_directory_link(&outside, &link);
        assert_eq!(
            secure
                .open_relative(super::Path::new("linked/secret.txt"), false)
                .unwrap_err(),
            "workspace_link_rejected"
        );
        let parent_link = parent.with_extension("link");
        create_directory_link(&parent, &parent_link);
        assert!(matches!(
            super::open_windows_directory(&parent_link.join("project")),
            Err("project_identity_changed" | "project_unavailable")
        ));
        let _ = std::fs::remove_dir(&parent_link);

        let _ = std::fs::remove_dir_all(parent);
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn windows_broker_maps_acl_read_denial_to_a_stable_error() {
        use std::ffi::OsStr;
        use std::process::Command;

        let parent = std::env::temp_dir().join(format!("workspace-acl-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&parent).unwrap();
        let secret = parent.join("denied.txt");
        std::fs::write(&secret, "not readable").unwrap();
        let identity = super::root_identity(&parent).unwrap();
        let secure = super::SecureRoot::open(&parent, &identity).unwrap();

        let deny = Command::new("icacls.exe")
            .args([
                secret.as_os_str(),
                OsStr::new("/deny"),
                OsStr::new("*S-1-1-0:(R)"),
            ])
            .status()
            .expect("icacls.exe must be available on Windows");
        assert!(deny.success(), "could not apply the test ACL to {secret:?}");

        let result = secure.open_relative(super::Path::new("denied.txt"), false);
        let restore = Command::new("icacls.exe")
            .args([
                secret.as_os_str(),
                OsStr::new("/remove:d"),
                OsStr::new("*S-1-1-0"),
            ])
            .status()
            .expect("icacls.exe must be available on Windows");
        assert!(
            restore.success(),
            "could not restore the test ACL on {secret:?}"
        );
        assert_eq!(result.unwrap_err(), "workspace_read_denied");

        let _ = std::fs::remove_dir_all(parent);
    }

    #[test]
    fn workspace_broker_bounds_listing_search_and_file_reads() {
        let temporary =
            std::env::temp_dir().join(format!("workspace-limits-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(temporary.join("src")).unwrap();
        std::fs::write(temporary.join("src/main.py"), "needle = 1\n").unwrap();
        let mut broker = WorkspaceBroker::default();
        let grant = broker
            .authorize_run("session-1", "run-1", "project-1", &temporary)
            .unwrap();
        let mut list = request();
        list.grant = grant.clone();
        list.operation = "list_entries".into();
        list.payload = serde_json::json!({"relative_path":".","max_depth":99});
        let listed = broker.execute(&list, super::now_ms()).unwrap();
        assert!(listed["files"]
            .as_array()
            .unwrap()
            .iter()
            .any(|item| item == "src/main.py"));

        let mut search = request();
        search.grant = grant.clone();
        search.operation = "search_text".into();
        search.payload =
            serde_json::json!({"relative_path":".","query":"needle","max_results":9999});
        assert_eq!(
            broker.execute(&search, super::now_ms()).unwrap()["matches"]
                .as_array()
                .unwrap()
                .len(),
            1
        );

        let mut read = request();
        read.grant = grant;
        read.payload = serde_json::json!({"relative_path":"src/main.py","max_bytes":9999999});
        assert_eq!(
            broker.execute(&read, super::now_ms()).unwrap()["content"],
            "needle = 1\n"
        );
        let _ = std::fs::remove_dir_all(temporary);
    }

    fn request() -> BridgeRequest {
        BridgeRequest {
            protocol_version: 1,
            instance_id: "desktop-1".into(),
            session_id: "session-1".into(),
            run_id: "run-1".into(),
            project_id: "project-1".into(),
            request_id: "request-1".into(),
            nonce: "nonce-1".into(),
            timestamp_ms: 1_000,
            authorization: "Bearer secret-token".into(),
            operation: "read_file".into(),
            grant: "run-grant".into(),
            payload: serde_json::json!({"relative_path":"README.md"}),
        }
    }

    #[test]
    fn bridge_request_requires_matching_instance_and_authentication() {
        let mut seen = HashSet::new();
        assert!(
            validate_request(&request(), "secret-token", "desktop-1", 1_000, &mut seen).is_ok()
        );

        let mut wrong_instance = request();
        wrong_instance.instance_id = "other".into();
        assert_eq!(
            validate_request(
                &wrong_instance,
                "secret-token",
                "desktop-1",
                1_000,
                &mut HashSet::new()
            )
            .unwrap_err(),
            "bridge_instance_mismatch"
        );

        let mut wrong_token = request();
        wrong_token.authorization = "Bearer wrong".into();
        assert_eq!(
            validate_request(
                &wrong_token,
                "secret-token",
                "desktop-1",
                1_000,
                &mut HashSet::new()
            )
            .unwrap_err(),
            "bridge_unauthorized"
        );
    }

    #[test]
    fn bridge_request_rejects_stale_nonce_replay_and_unknown_operations() {
        let mut seen = HashSet::new();
        assert!(
            validate_request(&request(), "secret-token", "desktop-1", 1_000, &mut seen).is_ok()
        );
        assert_eq!(
            validate_request(&request(), "secret-token", "desktop-1", 1_000, &mut seen)
                .unwrap_err(),
            "bridge_replay"
        );

        let mut stale = request();
        stale.nonce = "nonce-2".into();
        stale.timestamp_ms = 0;
        assert_eq!(
            validate_request(
                &stale,
                "secret-token",
                "desktop-1",
                60_001,
                &mut HashSet::new()
            )
            .unwrap_err(),
            "bridge_request_expired"
        );

        let mut unknown = request();
        unknown.nonce = "nonce-3".into();
        unknown.operation = "run_command".into();
        assert_eq!(
            validate_request(
                &unknown,
                "secret-token",
                "desktop-1",
                1_000,
                &mut HashSet::new()
            )
            .unwrap_err(),
            "bridge_operation_rejected"
        );
    }

    #[test]
    fn bridge_request_requires_session_run_project_and_grant_identity() {
        let mut missing_run = request();
        missing_run.run_id.clear();
        assert_eq!(
            validate_request(
                &missing_run,
                "secret-token",
                "desktop-1",
                1_000,
                &mut HashSet::new()
            )
            .unwrap_err(),
            "bridge_invalid_request"
        );

        let mut missing_project = request();
        missing_project.project_id.clear();
        assert_eq!(
            validate_request(
                &missing_project,
                "secret-token",
                "desktop-1",
                1_000,
                &mut HashSet::new()
            )
            .unwrap_err(),
            "bridge_invalid_request"
        );
    }

    #[test]
    fn private_listener_authenticates_and_returns_closed_failure_until_broker_exists() {
        let _guard = BRIDGE_LISTENER_TEST_LOCK
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let journal = std::env::temp_dir().join(format!("bridge-journal-{}", uuid::Uuid::new_v4()));
        let server = BridgeServer::start("secret-token".into(), journal.clone()).unwrap();
        server.set_instance_id("desktop-1".into()).unwrap();
        let timestamp_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis() as u64;
        let request = serde_json::json!({
            "protocol_version": DESKTOP_BRIDGE_PROTOCOL_VERSION,
            "instance_id": "desktop-1",
            "session_id": "session-1",
            "run_id": "run-1",
            "project_id": "project-1",
            "request_id": "request-1",
            "nonce": "nonce-1",
            "timestamp_ms": timestamp_ms,
            "authorization": "Bearer secret-token",
            "operation": "read_file",
            "grant": "run-grant",
            "payload": {"relative_path": "README.md"}
        });
        let mut stream = TcpStream::connect(("127.0.0.1", server.port())).unwrap();
        writeln!(stream, "{request}").unwrap();
        let mut response = String::new();
        BufReader::new(stream).read_line(&mut response).unwrap();
        let response: serde_json::Value = serde_json::from_str(&response).unwrap();
        assert_eq!(response["request_id"], "request-1");
        assert_eq!(response["instance_id"], "desktop-1");
        assert_eq!(response["status"], "rejected");
        assert_eq!(response["error_code"], "grant_expired");
        let _ = std::fs::remove_dir_all(journal);
    }

    #[test]
    fn private_listener_rejects_oversized_messages() {
        let _guard = BRIDGE_LISTENER_TEST_LOCK
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let journal = std::env::temp_dir().join(format!("bridge-journal-{}", uuid::Uuid::new_v4()));
        let server = BridgeServer::start("secret-token".into(), journal.clone()).unwrap();
        server.set_instance_id("desktop-1".into()).unwrap();
        let mut stream = TcpStream::connect(("127.0.0.1", server.port())).unwrap();
        stream
            .write_all(&vec![b'x'; MAX_BRIDGE_MESSAGE_BYTES + 1])
            .unwrap();
        let mut response = String::new();
        BufReader::new(stream).read_line(&mut response).unwrap();
        let response: serde_json::Value = serde_json::from_str(&response).unwrap();
        assert_eq!(response["error_code"], "bridge_request_too_large");
        let _ = std::fs::remove_dir_all(journal);
    }
}
