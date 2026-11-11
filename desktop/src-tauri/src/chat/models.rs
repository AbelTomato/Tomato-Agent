use super::*;

#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ChatConfigInput {
    pub base_url: String,
    pub model: String,
    pub api_key: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ChatConfig {
    pub base_url: String,
    pub model: String,
}

impl ChatConfig {
    pub(super) fn new(base_url: String, model: String) -> Result<Self, String> {
        let url = reqwest::Url::parse(&base_url).map_err(|_| "API 地址无效".to_string())?;
        if !matches!(url.scheme(), "http" | "https") || url.host_str().is_none() {
            return Err("API 地址必须使用 HTTP 或 HTTPS".into());
        }
        let model = model.trim();
        if model.is_empty() || model.len() > 256 || model.chars().any(char::is_control) {
            return Err("模型名称无效".into());
        }
        Ok(Self {
            base_url: base_url.trim_end_matches('/').to_string(),
            model: model.into(),
        })
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct ChatMessage {
    pub id: String,
    pub session_id: String,
    pub role: String,
    pub content: String,
    pub created_at: String,
}

#[derive(Clone, Debug, Serialize)]
pub struct ChatSession {
    pub id: String,
    pub title: String,
    pub created_at: String,
    pub project_id: Option<String>,
}

#[derive(Clone, Debug, Serialize)]
pub struct ChatProject {
    pub id: String,
    pub name: String,
    pub root_path: String,
    pub available: bool,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CreateProjectInput {
    pub root_path: String,
    pub name: Option<String>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SetSessionProjectInput {
    pub session_id: String,
    pub project_id: Option<String>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SendInput {
    pub request_id: String,
    pub session_id: String,
    pub content: String,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct MemoryReference {
    pub item_id: String,
    pub version: u32,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct MemoryChatContext {
    pub assistant_message_id: String,
    pub session_id: String,
    pub generation: u64,
    pub retrieval_mode: String,
    pub items: Vec<MemoryReference>,
}

#[derive(Clone, Debug)]
pub struct MemoryContextInput {
    pub prompt: String,
    pub generation: u64,
    pub retrieval_mode: String,
    pub items: Vec<MemoryReference>,
}

pub(super) fn utc_now() -> String {
    let elapsed = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    let secs = elapsed.as_secs() as i64;
    let days = secs.div_euclid(86_400);
    let day_secs = secs.rem_euclid(86_400);
    let (year, month, day) = civil_from_days(days);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}Z",
        day_secs / 3600,
        day_secs / 60 % 60,
        day_secs % 60
    )
}

fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    (
        y + i64::from(mp >= 10),
        mp + if mp < 10 { 3 } else { -9 },
        doy - (153 * mp + 2) / 5 + 1,
    )
}

#[derive(Serialize)]
pub struct HistoryPage {
    pub messages: Vec<ChatMessage>,
    pub has_more: bool,
}

#[derive(Clone, Debug, Serialize)]
pub struct MemorySourceDeletion {
    pub request_id: String,
    pub source_system: String,
    pub source_installation_id: String,
    pub session_id: String,
    pub message_id: String,
    pub source_revision: i64,
    pub status: String,
}
