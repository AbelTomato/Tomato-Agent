use chrono::{DateTime, Days, NaiveDate, Utc};
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashSet};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use uuid::Uuid;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeTraceSpan {
    pub schema_version: String,
    pub event_id: Uuid,
    pub runtime_instance_id: Uuid,
    pub sequence: u64,
    pub trace_id: Uuid,
    pub span_id: Uuid,
    pub parent_span_id: Option<Uuid>,
    pub run_id: Uuid,
    pub operation: String,
    pub started_at: String,
    pub duration_ms: f64,
    pub status: String,
    pub error_code: Option<String>,
    pub error_category: Option<String>,
    pub attributes: BTreeMap<String, Value>,
}

impl RuntimeTraceSpan {
    fn validate(&self, runtime_instance_id: Uuid) -> Result<(), String> {
        if self.schema_version != "runtime-trace/v1"
            || self.runtime_instance_id != runtime_instance_id
            || self.sequence == 0
            || !self.duration_ms.is_finite()
            || self.duration_ms < 0.0
        {
            return Err("invalid_trace_span".into());
        }
        if !matches!(
            self.operation.as_str(),
            "run.execute"
                | "model.complete"
                | "tool.execute"
                | "memory.recall"
                | "knowledge.search"
                | "validation.run"
        ) {
            return Err("invalid_trace_operation".into());
        }
        let parsed = DateTime::parse_from_rfc3339(&self.started_at)
            .map_err(|_| "invalid_trace_timestamp")?;
        if parsed.offset().local_minus_utc() != 0 {
            return Err("invalid_trace_timestamp".into());
        }
        let has_error = self.error_code.is_some() || self.error_category.is_some();
        if (self.status == "ok" && has_error)
            || (self.status != "ok"
                && !matches!(self.status.as_str(), "error" | "cancelled" | "timeout"))
            || (self.status != "ok" && (self.error_code.is_none() || self.error_category.is_none()))
        {
            return Err("invalid_trace_status".into());
        }
        if let Some(code) = &self.error_code {
            if code.is_empty()
                || code.len() > 64
                || !code
                    .bytes()
                    .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_')
            {
                return Err("invalid_trace_error_code".into());
            }
        }
        if self.attributes.len() > 16 {
            return Err("invalid_trace_attributes".into());
        }
        for (key, value) in &self.attributes {
            if !matches!(
                key.as_str(),
                "provider_name"
                    | "model_name"
                    | "attempt"
                    | "input_tokens"
                    | "output_tokens"
                    | "total_tokens"
                    | "usage_known"
                    | "tool_name"
                    | "result_count"
                    | "result_chars"
                    | "retry_count"
                    | "timeout_ms"
            ) {
                return Err("invalid_trace_attributes".into());
            }
            match value {
                Value::Bool(_) if key == "usage_known" => {}
                Value::Number(number)
                    if matches!(
                        key.as_str(),
                        "attempt"
                            | "input_tokens"
                            | "output_tokens"
                            | "total_tokens"
                            | "result_count"
                            | "result_chars"
                            | "retry_count"
                            | "timeout_ms"
                    ) && (number.as_i64().is_some_and(|n| n >= 0)
                        || number.as_u64().is_some()) => {}
                Value::Number(number)
                    if matches!(
                        key.as_str(),
                        "attempt"
                            | "input_tokens"
                            | "output_tokens"
                            | "total_tokens"
                            | "result_count"
                            | "result_chars"
                            | "retry_count"
                            | "timeout_ms"
                    ) && number.as_f64().is_some_and(|n| n.is_finite() && n >= 0.0) => {}
                Value::String(text)
                    if matches!(key.as_str(), "provider_name" | "model_name" | "tool_name")
                        && !text.trim().is_empty()
                        && text.len() <= 128 =>
                {
                    if matches!(key.as_str(), "provider_name" | "model_name" | "tool_name")
                        && !text
                            .bytes()
                            .all(|byte| byte.is_ascii_alphanumeric() || b"._:/-".contains(&byte))
                    {
                        return Err("invalid_trace_identifier".into());
                    }
                }
                _ => return Err("invalid_trace_attributes".into()),
            }
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct TracePage {
    pub schema_version: String,
    pub runtime_instance_id: Uuid,
    pub items: Vec<RuntimeTraceSpan>,
    pub next_cursor: u64,
    pub dropped_total: u64,
    pub oldest_sequence: Option<u64>,
    pub gap_count: u64,
}

pub fn trace_api_cursor(
    instance: Option<Uuid>,
    cursor: u64,
    response: &Value,
) -> Result<u64, String> {
    let page: TracePage = serde_json::from_value(response.clone())
        .map_err(|_| "Trace 引擎返回了无效分页".to_string())?;
    if instance.is_some_and(|current| current != page.runtime_instance_id) {
        return Ok(0);
    }
    page.validate(cursor)?;
    Ok(page.next_cursor)
}

impl TracePage {
    fn validate(&self, after: u64) -> Result<(), String> {
        if self.schema_version != "runtime-trace/v1" || self.next_cursor < after {
            return Err("invalid_trace_page".into());
        }
        let expected_gap = self
            .oldest_sequence
            .map(|oldest| oldest.saturating_sub(after.saturating_add(1)))
            .unwrap_or(0);
        if self.gap_count != expected_gap {
            return Err("invalid_trace_gap".into());
        }
        let mut previous = after;
        for span in &self.items {
            span.validate(self.runtime_instance_id)?;
            if span.sequence <= previous {
                return Err("unordered_trace_page".into());
            }
            previous = span.sequence;
        }
        if (self.items.is_empty() && self.next_cursor != after)
            || self
                .items
                .last()
                .is_some_and(|span| span.sequence != self.next_cursor)
        {
            return Err("invalid_trace_cursor".into());
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ConsumeReport {
    pub cursor: u64,
    pub persisted_count: usize,
    pub duplicate_count: usize,
}

pub struct TraceStore {
    root: PathBuf,
    db: Connection,
    event_ids: HashSet<Uuid>,
}

impl TraceStore {
    pub fn open(root: &Path) -> Result<Self, String> {
        fs::create_dir_all(root).map_err(|_| "无法创建 Trace 目录".to_string())?;
        set_private_directory(root)?;
        let db_path = root.join("trace-state.sqlite");
        let db = Connection::open(&db_path).map_err(|_| "无法打开 Trace 游标状态".to_string())?;
        db.execute_batch(
            "PRAGMA journal_mode=WAL;
             CREATE TABLE IF NOT EXISTS trace_cursors (
                 runtime_instance_id TEXT PRIMARY KEY,
                 sequence INTEGER NOT NULL,
                 dropped_total INTEGER NOT NULL
             );
             CREATE TABLE IF NOT EXISTS trace_metadata (
                 key TEXT PRIMARY KEY,
                 value TEXT NOT NULL
             );
             CREATE TABLE IF NOT EXISTS trace_consumer_errors (
                 error_code TEXT PRIMARY KEY,
                 occurrence_count INTEGER NOT NULL
             );",
        )
        .map_err(|_| "无法初始化 Trace 游标状态".to_string())?;
        set_private_file(&db_path)?;
        let mut store = Self {
            root: root.to_path_buf(),
            db,
            event_ids: HashSet::new(),
        };
        let cutoff = (Utc::now().date_naive() - Days::new(29))
            .format("%Y-%m-%d")
            .to_string();
        store.prune_before(&cutoff)?;
        store.rebuild_event_ids()?;
        Ok(store)
    }

    pub fn cursor(&self, runtime_instance_id: Uuid) -> Result<u64, String> {
        self.db
            .query_row(
                "SELECT sequence FROM trace_cursors WHERE runtime_instance_id=?1",
                [runtime_instance_id.to_string()],
                |row| row.get::<_, i64>(0),
            )
            .optional()
            .map_err(|_| "无法读取 Trace 游标".to_string())?
            .map(|value| u64::try_from(value).map_err(|_| "Trace 游标无效".to_string()))
            .unwrap_or(Ok(0))
    }

    pub fn active_runtime_instance(&self) -> Result<Option<Uuid>, String> {
        let value = self
            .db
            .query_row(
                "SELECT value FROM trace_metadata WHERE key='active_runtime_instance_id'",
                [],
                |row| row.get::<_, String>(0),
            )
            .optional()
            .map_err(|_| "无法读取 Trace 引擎实例".to_string())?;
        value
            .map(|value| Uuid::parse_str(&value).map_err(|_| "Trace 引擎实例无效".into()))
            .transpose()
    }

    pub fn record_consumer_error(&mut self, error_code: &str) -> Result<(), String> {
        if !matches!(
            error_code,
            "trace_poll_failed" | "trace_store_failed" | "trace_response_invalid"
        ) {
            return Err("invalid_trace_consumer_error".into());
        }
        self.db
            .execute(
                "INSERT INTO trace_consumer_errors(error_code,occurrence_count) VALUES(?1,1)
             ON CONFLICT(error_code) DO UPDATE SET occurrence_count=occurrence_count+1",
                [error_code],
            )
            .map(|_| ())
            .map_err(|_| "无法更新 Trace 消费错误统计".into())
    }

    #[cfg(test)]
    fn consumer_error_count(&self, error_code: &str) -> Result<u64, String> {
        self.db
            .query_row(
                "SELECT occurrence_count FROM trace_consumer_errors WHERE error_code=?1",
                [error_code],
                |row| row.get::<_, i64>(0),
            )
            .optional()
            .map_err(|_| "无法读取 Trace 消费错误统计".to_string())?
            .map(|count| u64::try_from(count).map_err(|_| "Trace 错误统计无效".into()))
            .unwrap_or(Ok(0))
    }

    pub fn consume_page(
        &mut self,
        page: TracePage,
        requested_after: u64,
    ) -> Result<ConsumeReport, String> {
        let active = self.active_runtime_instance()?;
        if active == Some(page.runtime_instance_id) {
            if requested_after != self.cursor(page.runtime_instance_id)? {
                return Err("trace_cursor_mismatch".into());
            }
        } else if requested_after != 0 {
            return Err("trace_instance_changed".into());
        }
        page.validate(requested_after)?;

        let previous_dropped = if active == Some(page.runtime_instance_id) {
            self.db
                .query_row(
                    "SELECT dropped_total FROM trace_cursors WHERE runtime_instance_id=?1",
                    [page.runtime_instance_id.to_string()],
                    |row| row.get::<_, i64>(0),
                )
                .optional()
                .map_err(|_| "无法读取 Trace 丢弃统计".to_string())?
                .unwrap_or(0)
        } else {
            0
        };
        let previous_dropped =
            u64::try_from(previous_dropped).map_err(|_| "Trace 丢弃统计无效".to_string())?;
        if page.dropped_total < previous_dropped {
            return Err("trace_drop_counter_reversed".into());
        }

        let mut records = BTreeMap::<String, Vec<(Uuid, Vec<u8>)>>::new();
        let mut loss_id = None;
        if page.gap_count > 0 || page.dropped_total > previous_dropped {
            let mut digest = Sha256::new();
            digest.update(page.runtime_instance_id.as_bytes());
            digest.update(requested_after.to_be_bytes());
            digest.update(page.next_cursor.to_be_bytes());
            digest.update(page.dropped_total.to_be_bytes());
            digest.update(page.oldest_sequence.unwrap_or_default().to_be_bytes());
            let digest = digest.finalize();
            let id = Uuid::from_slice(&digest[..16]).map_err(|_| "Trace 丢弃记录 ID 无效")?;
            let record = json!({
                "record_type": "trace_loss",
                "schema_version": "runtime-trace/v1",
                "loss_id": id,
                "runtime_instance_id": page.runtime_instance_id,
                "after_sequence": requested_after,
                "oldest_sequence": page.oldest_sequence,
                "gap_count": page.gap_count,
                "dropped_delta": page.dropped_total - previous_dropped,
                "dropped_total": page.dropped_total,
                "observed_at": Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true),
            });
            records
                .entry(Utc::now().format("%Y-%m-%d").to_string())
                .or_default()
                .push((
                    id,
                    serde_json::to_vec(&record).map_err(|_| "无法编码 Trace 丢弃记录")?,
                ));
            loss_id = Some(id);
        }

        let mut duplicate_count = 0;
        let mut pending_ids = HashSet::new();
        for span in &page.items {
            let serialized = serde_json::to_value(span).map_err(|_| "无法编码 Trace Span")?;
            let mut record = serialized
                .as_object()
                .cloned()
                .ok_or("Trace Span 格式无效")?;
            record.insert("record_type".into(), Value::String("span".into()));
            if self.event_ids.contains(&span.event_id) || !pending_ids.insert(span.event_id) {
                duplicate_count += 1;
                continue;
            }
            let day = DateTime::parse_from_rfc3339(&span.started_at)
                .map_err(|_| "Trace 时间戳无效")?
                .date_naive()
                .format("%Y-%m-%d")
                .to_string();
            records.entry(day).or_default().push((
                span.event_id,
                serde_json::to_vec(&record).map_err(|_| "无法编码 Trace Span")?,
            ));
        }

        let mut persisted_count = 0;
        for (day, records) in records {
            let new_records = records
                .into_iter()
                .filter(|(id, _)| !self.event_ids.contains(id))
                .collect::<Vec<_>>();
            if new_records.is_empty() {
                continue;
            }
            self.append_records(&day, &new_records)?;
            for (id, _) in &new_records {
                self.event_ids.insert(*id);
                if Some(*id) != loss_id {
                    persisted_count += 1;
                }
            }
        }

        let transaction = self
            .db
            .unchecked_transaction()
            .map_err(|_| "无法更新 Trace 游标".to_string())?;
        transaction.execute(
            "INSERT INTO trace_cursors(runtime_instance_id,sequence,dropped_total) VALUES(?1,?2,?3)
             ON CONFLICT(runtime_instance_id) DO UPDATE SET sequence=excluded.sequence,dropped_total=excluded.dropped_total",
            params![page.runtime_instance_id.to_string(), to_sql_u64(page.next_cursor)?, to_sql_u64(page.dropped_total)?],
        ).map_err(|_| "无法更新 Trace 游标".to_string())?;
        transaction
            .execute(
                "INSERT INTO trace_metadata(key,value) VALUES('active_runtime_instance_id',?1)
             ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [page.runtime_instance_id.to_string()],
            )
            .map_err(|_| "无法更新 Trace 引擎实例".to_string())?;
        transaction
            .commit()
            .map_err(|_| "无法提交 Trace 游标".to_string())?;

        Ok(ConsumeReport {
            cursor: page.next_cursor,
            persisted_count,
            duplicate_count,
        })
    }

    pub fn prune_before(&mut self, cutoff: &str) -> Result<(), String> {
        let cutoff = NaiveDate::parse_from_str(cutoff, "%Y-%m-%d")
            .map_err(|_| "Trace 保留日期无效".to_string())?;
        let entries = fs::read_dir(&self.root).map_err(|_| "无法读取 Trace 目录".to_string())?;
        for entry in entries {
            let entry = entry.map_err(|_| "无法读取 Trace 文件项".to_string())?;
            if !entry
                .file_type()
                .map_err(|_| "无法读取 Trace 文件属性")?
                .is_file()
            {
                continue;
            }
            let Some(name) = entry.file_name().to_str().map(str::to_owned) else {
                continue;
            };
            let Some(date) = name.strip_suffix(".jsonl") else {
                continue;
            };
            let Ok(date) = NaiveDate::parse_from_str(date, "%Y-%m-%d") else {
                continue;
            };
            if date < cutoff {
                fs::remove_file(entry.path()).map_err(|_| "无法清理过期 Trace 文件".to_string())?;
            }
        }
        self.rebuild_event_ids()
    }

    fn append_records(&self, day: &str, records: &[(Uuid, Vec<u8>)]) -> Result<(), String> {
        let path = self.root.join(format!("{day}.jsonl"));
        if let Ok(metadata) = fs::symlink_metadata(&path) {
            if !metadata.file_type().is_file() {
                return Err("Trace 日志目标不是普通文件".into());
            }
        }
        let mut options = OpenOptions::new();
        options.create(true).append(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options
            .open(&path)
            .map_err(|_| "无法打开 Trace 日志块".to_string())?;
        set_private_file(&path)?;
        let original_length = file.metadata().map_err(|_| "无法读取 Trace 日志块")?.len();
        let write_result = (|| {
            for (_, record) in records {
                file.write_all(record)
                    .map_err(|_| "无法写入 Trace 日志块")?;
                file.write_all(b"\n").map_err(|_| "无法写入 Trace 日志块")?;
            }
            file.sync_data().map_err(|_| "无法同步 Trace 日志块")
        })();
        if let Err(error) = write_result {
            file.set_len(original_length)
                .map_err(|_| "无法回滚 Trace 日志块")?;
            let _ = file.sync_data();
            return Err(error.into());
        }
        Ok(())
    }

    fn rebuild_event_ids(&mut self) -> Result<(), String> {
        self.event_ids.clear();
        let entries = fs::read_dir(&self.root).map_err(|_| "无法读取 Trace 目录".to_string())?;
        for entry in entries {
            let entry = entry.map_err(|_| "无法读取 Trace 文件项".to_string())?;
            if !entry
                .file_type()
                .map_err(|_| "无法读取 Trace 文件属性")?
                .is_file()
            {
                continue;
            }
            let path = entry.path();
            if !is_trace_log_path(&path) {
                continue;
            }
            let bytes = fs::read(&path).map_err(|_| "无法读取 Trace 日志块".to_string())?;
            let valid_length = bytes
                .iter()
                .rposition(|byte| *byte == b'\n')
                .map(|index| index + 1)
                .unwrap_or(0);
            if valid_length != bytes.len() {
                let file = OpenOptions::new()
                    .write(true)
                    .open(&path)
                    .map_err(|_| "无法修复 Trace 日志尾部".to_string())?;
                file.set_len(valid_length as u64)
                    .map_err(|_| "无法修复 Trace 日志尾部".to_string())?;
                file.sync_data()
                    .map_err(|_| "无法同步 Trace 日志修复".to_string())?;
            }
            for line in bytes[..valid_length]
                .split(|byte| *byte == b'\n')
                .filter(|line| !line.is_empty())
            {
                let value: Value =
                    serde_json::from_slice(line).map_err(|_| "Trace 日志块格式无效".to_string())?;
                let id = if value["record_type"] == "span" {
                    value.get("event_id")
                } else if value["record_type"] == "trace_loss" {
                    value.get("loss_id")
                } else {
                    return Err("Trace 日志记录类型无效".into());
                }
                .and_then(Value::as_str)
                .ok_or("Trace 日志记录 ID 缺失")?;
                self.event_ids
                    .insert(Uuid::parse_str(id).map_err(|_| "Trace 日志记录 ID 无效")?);
            }
        }
        Ok(())
    }
}

fn to_sql_u64(value: u64) -> Result<i64, String> {
    i64::try_from(value).map_err(|_| "Trace 游标超出范围".into())
}

fn is_trace_log_path(path: &Path) -> bool {
    path.extension()
        .is_some_and(|extension| extension == "jsonl")
        && path
            .file_stem()
            .and_then(|name| name.to_str())
            .is_some_and(|name| NaiveDate::parse_from_str(name, "%Y-%m-%d").is_ok())
}

fn set_private_directory(path: &Path) -> Result<(), String> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))
            .map_err(|_| "无法限制 Trace 目录权限".to_string())?;
    }
    Ok(())
}

fn set_private_file(path: &Path) -> Result<(), String> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o600))
            .map_err(|_| "无法限制 Trace 文件权限".to_string())?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{ConsumeReport, RuntimeTraceSpan, TracePage, TraceStore};
    use rusqlite::params;
    use serde_json::Value;
    use std::fs;
    use std::path::{Path, PathBuf};
    use uuid::Uuid;

    fn temporary_directory(name: &str) -> PathBuf {
        let path = std::env::temp_dir().join(format!("{name}-{}", Uuid::new_v4()));
        fs::create_dir_all(&path).unwrap();
        path
    }

    fn span(instance: Uuid, sequence: u64) -> RuntimeTraceSpan {
        RuntimeTraceSpan {
            schema_version: "runtime-trace/v1".into(),
            event_id: Uuid::new_v4(),
            runtime_instance_id: instance,
            sequence,
            trace_id: Uuid::new_v4(),
            span_id: Uuid::new_v4(),
            parent_span_id: None,
            run_id: Uuid::new_v4(),
            operation: "run.execute".into(),
            started_at: "2026-11-20T00:00:00Z".into(),
            duration_ms: 1.25,
            status: "ok".into(),
            error_code: None,
            error_category: None,
            attributes: Default::default(),
        }
    }

    fn page(instance: Uuid, after: u64, sequences: &[u64]) -> TracePage {
        let items = sequences
            .iter()
            .map(|sequence| span(instance, *sequence))
            .collect::<Vec<_>>();
        TracePage {
            schema_version: "runtime-trace/v1".into(),
            runtime_instance_id: instance,
            items,
            next_cursor: sequences.last().copied().unwrap_or(after),
            dropped_total: 0,
            oldest_sequence: Some(1),
            gap_count: 0,
        }
    }

    fn jsonl_files(root: &Path) -> Vec<PathBuf> {
        fs::read_dir(root)
            .unwrap()
            .filter_map(Result::ok)
            .map(|entry| entry.path())
            .filter(|path| {
                path.extension()
                    .is_some_and(|extension| extension == "jsonl")
            })
            .collect()
    }

    fn jsonl_records(root: &Path) -> Vec<Value> {
        jsonl_files(root)
            .into_iter()
            .flat_map(|path| {
                fs::read_to_string(path)
                    .unwrap()
                    .lines()
                    .map(|line| serde_json::from_str(line).unwrap())
                    .collect::<Vec<Value>>()
            })
            .collect()
    }

    #[test]
    fn trace_store_replays_after_restart_without_duplicate_jsonl_records() {
        let root = temporary_directory("trace-store-replay");
        let instance = Uuid::new_v4();
        let first = page(instance, 0, &[1, 2]);
        let ids = first
            .items
            .iter()
            .map(|item| item.event_id)
            .collect::<Vec<_>>();

        {
            let mut store = TraceStore::open(&root).unwrap();
            let report = store.consume_page(first.clone(), 0).unwrap();
            assert_eq!(report.cursor, 2);
            assert_eq!(report.persisted_count, 2);
            store
                .db
                .execute(
                    "UPDATE trace_cursors SET sequence=0 WHERE runtime_instance_id=?1",
                    params![instance.to_string()],
                )
                .unwrap();
        }

        let mut store = TraceStore::open(&root).unwrap();
        assert!(ids
            .iter()
            .all(|event_id| store.event_ids.contains(event_id)));
        let report = store.consume_page(first, 0).unwrap();
        assert_eq!(
            report,
            ConsumeReport {
                cursor: 2,
                persisted_count: 0,
                duplicate_count: 2
            }
        );
        assert_eq!(
            jsonl_records(&root)
                .iter()
                .filter(|item| item["record_type"] == "span")
                .count(),
            2
        );

        drop(store);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn trace_store_persists_loss_marker_when_ring_buffer_has_a_gap() {
        let root = temporary_directory("trace-store-gap");
        let instance = Uuid::new_v4();
        let mut input = page(instance, 0, &[4, 5]);
        input.dropped_total = 3;
        input.oldest_sequence = Some(4);
        input.gap_count = 3;

        let mut store = TraceStore::open(&root).unwrap();
        let report = store.consume_page(input.clone(), 0).unwrap();
        assert_eq!(report.cursor, 5);
        let records = jsonl_records(&root);
        let loss = records
            .iter()
            .find(|item| item["record_type"] == "trace_loss")
            .unwrap();
        assert_eq!(loss["gap_count"], 3);
        assert_eq!(loss["dropped_total"], 3);
        assert_eq!(
            records
                .iter()
                .filter(|item| item["record_type"] == "span")
                .count(),
            2
        );

        store
            .db
            .execute(
                "UPDATE trace_cursors SET sequence=0 WHERE runtime_instance_id=?1",
                params![instance.to_string()],
            )
            .unwrap();
        let replay = store.consume_page(input, 0).unwrap();
        assert_eq!(replay.persisted_count, 0);
        assert_eq!(
            jsonl_records(&root)
                .iter()
                .filter(|item| item["record_type"] == "trace_loss")
                .count(),
            1
        );

        drop(store);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn trace_store_does_not_advance_cursor_when_log_append_fails() {
        let root = temporary_directory("trace-store-write-fail");
        let instance = Uuid::new_v4();
        let mut store = TraceStore::open(&root).unwrap();
        let log_path = root.join("2026-11-20.jsonl");
        fs::create_dir(&log_path).unwrap();

        assert!(store.consume_page(page(instance, 0, &[1]), 0).is_err());
        assert_eq!(store.cursor(instance).unwrap(), 0);

        drop(store);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn trace_store_counts_consumer_failures_without_persisting_error_text() {
        let root = temporary_directory("trace-store-errors");
        let mut store = TraceStore::open(&root).unwrap();
        store.record_consumer_error("trace_poll_failed").unwrap();
        store.record_consumer_error("trace_poll_failed").unwrap();
        assert_eq!(store.consumer_error_count("trace_poll_failed").unwrap(), 2);
        assert!(store.record_consumer_error("provider body secret").is_err());
        drop(store);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn trace_store_prunes_only_old_date_blocks_and_keeps_other_files() {
        let root = temporary_directory("trace-store-retention");
        let mut store = TraceStore::open(&root).unwrap();
        let unmanaged_trace_record = format!(
            "{{\"record_type\":\"span\",\"event_id\":\"{}\"}}\n",
            Uuid::new_v4()
        );
        fs::write(root.join("2026-10-20.jsonl"), &unmanaged_trace_record).unwrap();
        fs::write(root.join("2026-10-21.jsonl"), &unmanaged_trace_record).unwrap();
        fs::write(root.join("notes.jsonl"), "unmanaged\n").unwrap();
        fs::create_dir(root.join("2026-10-01.jsonl")).unwrap();

        store.prune_before("2026-10-21").unwrap();

        assert!(!root.join("2026-10-20.jsonl").exists());
        assert!(root.join("2026-10-21.jsonl").exists());
        assert!(root.join("notes.jsonl").exists());
        assert!(root.join("2026-10-01.jsonl").is_dir());

        drop(store);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn trace_api_cursor_restarts_at_zero_for_a_new_engine_instance() {
        let first = Uuid::new_v4();
        let next = Uuid::new_v4();
        let response = serde_json::to_value(page(next, 0, &[1])).unwrap();

        assert_eq!(
            super::trace_api_cursor(Some(first), 18, &response).unwrap(),
            0
        );
        assert_eq!(
            super::trace_api_cursor(Some(next), 0, &response).unwrap(),
            1
        );
        assert!(super::trace_api_cursor(Some(next), 5, &response).is_err());
    }
}
