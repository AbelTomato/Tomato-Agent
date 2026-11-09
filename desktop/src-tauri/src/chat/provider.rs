use super::*;

#[derive(Default)]
pub(super) struct SseParser {
    buffer: Vec<u8>,
    done: bool,
}

impl SseParser {
    pub(super) fn push(&mut self, bytes: &[u8]) -> Result<Vec<String>, String> {
        self.buffer.extend_from_slice(bytes);
        let mut deltas = Vec::new();
        while let Some((end, separator_len)) = frame_boundary(&self.buffer) {
            let frame = self.buffer.drain(..end).collect::<Vec<_>>();
            self.buffer.drain(..separator_len);
            let frame =
                String::from_utf8(frame).map_err(|_| "Provider 返回无效 UTF-8".to_string())?;
            let data = frame
                .lines()
                .filter_map(|line| line.strip_prefix("data:").map(str::trim_start))
                .collect::<Vec<_>>()
                .join("\n");
            if data.is_empty() {
                continue;
            }
            if data == "[DONE]" {
                self.done = true;
                continue;
            }
            let value: serde_json::Value =
                serde_json::from_str(&data).map_err(|_| "Provider 返回无效流数据".to_string())?;
            if let Some(content) = value
                .pointer("/choices/0/delta/content")
                .and_then(serde_json::Value::as_str)
            {
                if !content.is_empty() {
                    deltas.push(content.to_string());
                }
            }
            if let Some(error) = value
                .pointer("/error/message")
                .and_then(serde_json::Value::as_str)
            {
                let _ = error;
                return Err("Provider 请求失败".into());
            }
        }
        Ok(deltas)
    }

    pub(super) fn finish(&mut self) -> Result<(), String> {
        if !self.done {
            return Err("Provider 流意外结束".into());
        }
        if !self.buffer.iter().all(u8::is_ascii_whitespace) {
            return Err("Provider 流意外结束".into());
        }
        Ok(())
    }
}

fn frame_boundary(buffer: &[u8]) -> Option<(usize, usize)> {
    let lf = buffer
        .windows(2)
        .position(|w| w == b"\n\n")
        .map(|at| (at, 2));
    let crlf = buffer
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .map(|at| (at, 4));
    match (lf, crlf) {
        (Some(a), Some(b)) => Some(if a.0 <= b.0 { a } else { b }),
        (Some(value), None) | (None, Some(value)) => Some(value),
        (None, None) => None,
    }
}

pub trait ChatProvider: Send + Sync {
    fn complete(
        &self,
        config: &ChatConfig,
        api_key: &str,
        messages: &[ChatMessage],
        cancel: &AtomicBool,
        emit: &mut dyn FnMut(&str) -> Result<(), String>,
    ) -> Result<String, String>;
}

#[cfg(test)]
pub struct FakeProvider {
    chunks: Vec<String>,
}

#[cfg(test)]
impl FakeProvider {
    pub fn new(chunks: Vec<String>) -> Self {
        Self { chunks }
    }
}

#[cfg(test)]
impl ChatProvider for FakeProvider {
    fn complete(
        &self,
        _config: &ChatConfig,
        _api_key: &str,
        _messages: &[ChatMessage],
        cancel: &AtomicBool,
        emit: &mut dyn FnMut(&str) -> Result<(), String>,
    ) -> Result<String, String> {
        let mut answer = String::new();
        for chunk in &self.chunks {
            if cancel.load(Ordering::Relaxed) {
                return Err("请求已取消".into());
            }
            emit(chunk)?;
            answer.push_str(chunk);
        }
        if answer.trim().is_empty() {
            return Err("模型返回空回复".into());
        }
        Ok(answer)
    }
}

pub struct OpenAiChatProvider;

impl ChatProvider for OpenAiChatProvider {
    fn complete(
        &self,
        config: &ChatConfig,
        api_key: &str,
        messages: &[ChatMessage],
        cancel: &AtomicBool,
        emit: &mut dyn FnMut(&str) -> Result<(), String>,
    ) -> Result<String, String> {
        let endpoint = format!("{}/chat/completions", config.base_url.trim_end_matches('/'));
        let body = serde_json::json!({
            "model": config.model,
            "stream": true,
            "messages": messages.iter().map(|m| serde_json::json!({"role":m.role,"content":m.content})).collect::<Vec<_>>()
        });
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|_| "无法初始化 Provider 请求".to_string())?;
        let mut response = runtime.block_on(async {
            let client = reqwest::Client::builder()
                .redirect(reqwest::redirect::Policy::none())
                .timeout(std::time::Duration::from_secs(120))
                .build()
                .map_err(|_| "无法初始化 Provider 请求".to_string())?;
            tokio::select! {
                _ = wait_cancelled(cancel) => Err("请求已取消".to_string()),
                result = client.post(endpoint).bearer_auth(api_key).json(&body).send() =>
                    result.map_err(|_| "Provider 网络请求失败或超时".to_string())
            }
        })?;
        if !response.status().is_success() {
            return Err(format!(
                "Provider 请求失败（HTTP {}）",
                response.status().as_u16()
            ));
        }
        let mut parser = SseParser::default();
        let mut answer = String::new();
        loop {
            if cancel.load(Ordering::Relaxed) {
                return Err("请求已取消".into());
            }
            let chunk = runtime.block_on(async {
                tokio::select! {
                    _ = wait_cancelled(cancel) => Err("请求已取消".to_string()),
                    result = response.chunk() => result.map_err(|_| "Provider 流读取失败".to_string())
                }
            })?;
            let Some(chunk) = chunk else {
                break;
            };
            for delta in parser.push(&chunk)? {
                emit(&delta)?;
                answer.push_str(&delta);
            }
        }
        parser.finish()?;
        if answer.trim().is_empty() {
            return Err("模型返回空回复".into());
        }
        Ok(answer)
    }
}

pub(super) async fn wait_cancelled(cancel: &AtomicBool) {
    while !cancel.load(Ordering::Relaxed) {
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
    }
}
