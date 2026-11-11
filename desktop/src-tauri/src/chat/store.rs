use super::*;

pub struct ChatStore {
    connection: Mutex<Connection>,
}

impl ChatStore {
    pub fn open(path: &Path) -> Result<Self, String> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|_| "无法创建聊天数据目录".to_string())?;
        }
        let connection = Connection::open(path).map_err(|_| "无法打开聊天数据库".to_string())?;
        connection.execute_batch(
            "PRAGMA journal_mode=WAL;
             PRAGMA foreign_keys=ON;
             BEGIN IMMEDIATE;
             CREATE TABLE IF NOT EXISTS chat_preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL);
             CREATE TABLE IF NOT EXISTS projects (
                 id TEXT PRIMARY KEY, name TEXT NOT NULL, root_path TEXT NOT NULL UNIQUE,
                 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
             );
             CREATE TABLE IF NOT EXISTS sessions (
                 id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL,
                 project_id TEXT REFERENCES projects(id) ON DELETE SET NULL
             );
             CREATE TABLE IF NOT EXISTS messages (
                 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                 role TEXT NOT NULL CHECK(role IN ('user', 'assistant')), content TEXT NOT NULL,
                 created_at TEXT NOT NULL
             );
             CREATE TABLE IF NOT EXISTS memory_chat_contexts (
                 assistant_message_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
                 session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                 generation INTEGER NOT NULL,
                 retrieval_mode TEXT NOT NULL,
                 created_at TEXT NOT NULL
             );
             CREATE TABLE IF NOT EXISTS memory_chat_context_items (
                 assistant_message_id TEXT NOT NULL REFERENCES memory_chat_contexts(assistant_message_id) ON DELETE CASCADE,
                 item_id TEXT NOT NULL,
                 version INTEGER NOT NULL,
                 PRIMARY KEY(assistant_message_id,item_id,version)
             );
             CREATE TABLE IF NOT EXISTS engine_sessions (
                 desktop_session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
                 engine_session_id TEXT NOT NULL UNIQUE
             );
             CREATE TABLE IF NOT EXISTS memory_source_deletions (
                 request_id TEXT PRIMARY KEY,
                 source_system TEXT NOT NULL,
                 source_installation_id TEXT NOT NULL,
                 session_id TEXT NOT NULL,
                 message_id TEXT NOT NULL,
                 source_revision INTEGER NOT NULL,
                 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','synced')),
                 created_at TEXT NOT NULL,
                 UNIQUE(source_system, source_installation_id, session_id, message_id, source_revision)
             );
             CREATE TABLE IF NOT EXISTS memory_source_ingestions (
                 request_id TEXT PRIMARY KEY,
                 source_system TEXT NOT NULL,
                 source_installation_id TEXT NOT NULL,
                 session_id TEXT NOT NULL,
                 message_id TEXT NOT NULL,
                 source_revision INTEGER NOT NULL,
                 payload_json TEXT NOT NULL,
                 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','synced','cancelled')),
                 created_at TEXT NOT NULL,
                 UNIQUE(source_system, source_installation_id, session_id, message_id, source_revision)
             );
             CREATE TABLE IF NOT EXISTS memory_item_tombstones (
                 item_id TEXT PRIMARY KEY,
                 request_id TEXT NOT NULL UNIQUE,
                 expected_version INTEGER NOT NULL,
                 created_at TEXT NOT NULL
             );
             CREATE TABLE IF NOT EXISTS memory_clear_tombstones (
                 scope_id TEXT PRIMARY KEY,
                 request_id TEXT NOT NULL UNIQUE,
                 expected_generation INTEGER NOT NULL,
                 created_at TEXT NOT NULL
             );",
        ).map_err(|_| "无法初始化聊天数据库".to_string())?;
        let columns = connection
            .prepare("PRAGMA table_info(sessions)")
            .map_err(|_| "无法读取聊天数据库结构".to_string())?
            .query_map([], |row| row.get::<_, String>(1))
            .map_err(|_| "无法读取聊天数据库结构".to_string())?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|_| "无法读取聊天数据库结构".to_string())?;
        if !columns.iter().any(|column| column == "project_id") {
            connection.execute("ALTER TABLE sessions ADD COLUMN project_id TEXT REFERENCES projects(id) ON DELETE SET NULL", [])
                .map_err(|_| "无法升级聊天数据库".to_string())?;
        }
        connection.execute(
            "INSERT OR IGNORE INTO chat_preferences(key,value) VALUES('memory_installation_id',?1)",
            [uuid::Uuid::new_v4().to_string()],
        ).map_err(|_| "无法初始化记忆来源身份".to_string())?;
        connection.execute(
            "INSERT OR IGNORE INTO chat_preferences(key,value) VALUES('memory_enabled','0')",
            [],
        ).map_err(|_| "无法初始化记忆设置".to_string())?;
        connection
            .execute_batch("COMMIT;")
            .map_err(|_| "无法完成聊天数据库升级".to_string())?;
        Ok(Self {
            connection: Mutex::new(connection),
        })
    }

    pub fn create_session(&self, title: &str) -> Result<String, String> {
        self.create_session_in_project(title, None)
    }

    pub fn create_session_in_project(
        &self,
        title: &str,
        project_id: Option<&str>,
    ) -> Result<String, String> {
        let title = normalize_title(title)?;
        let id = uuid::Uuid::new_v4().to_string();
        let mut connection = self
            .connection
            .lock()
            .map_err(|_| "聊天数据库不可用".to_string())?;
        let transaction = connection
            .transaction()
            .map_err(|_| "无法创建对话".to_string())?;
        if let Some(project_id) = project_id {
            if !transaction
                .query_row(
                    "SELECT EXISTS(SELECT 1 FROM projects WHERE id=?1)",
                    [project_id],
                    |row| row.get::<_, bool>(0),
                )
                .map_err(|_| "无法读取项目".to_string())?
            {
                return Err("项目不存在".into());
            }
        }
        transaction
            .execute(
                "INSERT INTO sessions(id,title,created_at,project_id) VALUES(?1,?2,?3,?4)",
                params![id, title, utc_now(), project_id],
            )
            .map_err(|_| "无法创建对话".to_string())?;
        transaction
            .execute(
                "INSERT OR REPLACE INTO chat_preferences(key,value) VALUES('current_session',?1)",
                [&id],
            )
            .map_err(|_| "无法保存当前对话".to_string())?;
        transaction
            .commit()
            .map_err(|_| "无法创建对话".to_string())?;
        Ok(id)
    }

    pub fn select_session(&self, session_id: &str) -> Result<(), String> {
        let connection = self
            .connection
            .lock()
            .map_err(|_| "聊天数据库不可用".to_string())?;
        let changed = connection.execute("INSERT OR REPLACE INTO chat_preferences(key,value) SELECT 'current_session',id FROM sessions WHERE id=?1", [session_id])
            .map_err(|_| "无法保存当前对话".to_string())?;
        if changed == 0 {
            return Err("对话不存在".into());
        }
        Ok(())
    }

    pub fn sessions(&self) -> Result<Vec<ChatSession>, String> {
        let connection = self
            .connection
            .lock()
            .map_err(|_| "聊天数据库不可用".to_string())?;
        let mut statement = connection.prepare("SELECT id,title,created_at,project_id FROM sessions ORDER BY created_at DESC,id DESC")
            .map_err(|_| "无法读取对话列表".to_string())?;
        let rows = statement
            .query_map([], |row| {
                Ok(ChatSession {
                    id: row.get(0)?,
                    title: row.get(1)?,
                    created_at: row.get(2)?,
                    project_id: row.get(3)?,
                })
            })
            .map_err(|_| "无法读取对话列表".to_string())?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|_| "无法读取对话列表".to_string())
    }

    pub fn projects(&self) -> Result<Vec<ChatProject>, String> {
        let connection = self
            .connection
            .lock()
            .map_err(|_| "聊天数据库不可用".to_string())?;
        let mut statement = connection.prepare(
            "SELECT id,name,root_path,created_at,updated_at FROM projects ORDER BY name COLLATE NOCASE,id",
        ).map_err(|_| "无法读取项目列表".to_string())?;
        let rows = statement
            .query_map([], |row| {
                let root_path: String = row.get(2)?;
                Ok(ChatProject {
                    id: row.get(0)?,
                    name: row.get(1)?,
                    available: Path::new(&root_path).is_dir(),
                    root_path,
                    created_at: row.get(3)?,
                    updated_at: row.get(4)?,
                })
            })
            .map_err(|_| "无法读取项目列表".to_string())?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|_| "无法读取项目列表".to_string())
    }

    pub fn create_project(
        &self,
        root_path: &str,
        name: Option<&str>,
    ) -> Result<ChatProject, String> {
        let root_path = normalize_project_path(root_path)?;
        if !Path::new(&root_path).is_dir() {
            return Err("项目目录不可用".into());
        }
        let name = normalize_project_name(
            name.filter(|value| !value.trim().is_empty())
                .unwrap_or_else(|| {
                    Path::new(&root_path)
                        .file_name()
                        .and_then(|value| value.to_str())
                        .unwrap_or("新项目")
                }),
        )?;
        let id = uuid::Uuid::new_v4().to_string();
        let timestamp = utc_now();
        self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?
            .execute("INSERT INTO projects(id,name,root_path,created_at,updated_at) VALUES(?1,?2,?3,?4,?5)",
                params![id, name, root_path, timestamp, timestamp])
            .map_err(|error| -> String {
                if error.to_string().contains("UNIQUE") { "该目录已经绑定项目".into() }
                else { "无法创建项目".into() }
            })?;
        Ok(ChatProject {
            id,
            name,
            root_path,
            available: true,
            created_at: timestamp.clone(),
            updated_at: timestamp,
        })
    }

    pub fn rename_project(&self, project_id: &str, name: &str) -> Result<(), String> {
        let name = normalize_project_name(name)?;
        let changed = self
            .connection
            .lock()
            .map_err(|_| "聊天数据库不可用".to_string())?
            .execute(
                "UPDATE projects SET name=?1,updated_at=?2 WHERE id=?3",
                params![name, utc_now(), project_id],
            )
            .map_err(|_| "无法重命名项目".to_string())?;
        if changed == 0 {
            return Err("项目不存在".into());
        }
        Ok(())
    }

    pub fn delete_project(&self, project_id: &str) -> Result<(), String> {
        let changed = self
            .connection
            .lock()
            .map_err(|_| "聊天数据库不可用".to_string())?
            .execute("DELETE FROM projects WHERE id=?1", [project_id])
            .map_err(|_| "无法删除项目".to_string())?;
        if changed == 0 {
            return Err("项目不存在".into());
        }
        Ok(())
    }

    pub fn set_session_project(
        &self,
        session_id: &str,
        project_id: Option<&str>,
    ) -> Result<(), String> {
        let connection = self
            .connection
            .lock()
            .map_err(|_| "聊天数据库不可用".to_string())?;
        if let Some(project_id) = project_id {
            let exists: Option<String> = connection
                .query_row("SELECT id FROM projects WHERE id=?1", [project_id], |row| {
                    row.get(0)
                })
                .optional()
                .map_err(|_| "无法读取项目".to_string())?;
            if exists.is_none() {
                return Err("项目不存在".into());
            }
        }
        let changed = connection
            .execute(
                "UPDATE sessions SET project_id=?1 WHERE id=?2",
                params![project_id, session_id],
            )
            .map_err(|_| "无法更新对话项目".to_string())?;
        if changed == 0 {
            return Err("对话不存在".into());
        }
        Ok(())
    }

    pub fn rename_session(&self, session_id: &str, title: &str) -> Result<(), String> {
        let title = normalize_title(title)?;
        let changed = self
            .connection
            .lock()
            .map_err(|_| "聊天数据库不可用".to_string())?
            .execute(
                "UPDATE sessions SET title=?1 WHERE id=?2",
                params![title, session_id],
            )
            .map_err(|_| "无法重命名对话".to_string())?;
        if changed == 0 {
            return Err("对话不存在".into());
        }
        Ok(())
    }

    pub fn delete_session(&self, session_id: &str) -> Result<(), String> {
        let installation_id = self.memory_installation_id()?;
        self.delete_session_with_memory_sources(session_id, "tomato-desktop", &installation_id)
    }

    pub fn memory_enabled(&self) -> Result<bool, String> {
        self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?
            .query_row("SELECT value FROM chat_preferences WHERE key='memory_enabled'", [], |row| row.get::<_, String>(0))
            .map(|value| value == "1")
            .map_err(|_| "无法读取记忆设置".to_string())
    }

    pub fn set_memory_enabled(&self, enabled: bool) -> Result<(), String> {
        let mut connection = self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?;
        let transaction = connection.transaction().map_err(|_| "无法保存记忆设置".to_string())?;
        transaction.execute("UPDATE chat_preferences SET value=?1 WHERE key='memory_enabled'",
            [if enabled { "1" } else { "0" }])
            .map_err(|_| "无法保存记忆设置".to_string())?;
        if !enabled {
            transaction.execute(
                "UPDATE memory_source_ingestions SET status='cancelled',payload_json='{}' WHERE status='pending'",
                [],
            ).map_err(|_| "无法清除待处理记忆来源".to_string())?;
        }
        transaction.commit().map_err(|_| "无法保存记忆设置".to_string())?;
        Ok(())
    }

    pub fn memory_installation_id(&self) -> Result<String, String> {
        self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?
            .query_row("SELECT value FROM chat_preferences WHERE key='memory_installation_id'", [], |row| row.get(0))
            .map_err(|_| "无法读取记忆来源身份".to_string())
    }

    pub fn delete_session_with_memory_sources(
        &self,
        session_id: &str,
        source_system: &str,
        installation_id: &str,
    ) -> Result<(), String> {
        let mut connection = self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?;
        let transaction = connection.transaction().map_err(|_| "无法删除对话".to_string())?;
        let messages = {
            let mut statement = transaction
                .prepare("SELECT id FROM messages WHERE session_id=?1")
                .map_err(|_| "无法读取待删除来源".to_string())?;
            let rows = statement.query_map([session_id], |row| row.get::<_, String>(0))
                .map_err(|_| "无法读取待删除来源".to_string())?;
            rows.collect::<Result<Vec<_>, _>>().map_err(|_| "无法读取待删除来源".to_string())?
        };
        for message_id in &messages {
            transaction.execute(
                "UPDATE memory_source_ingestions SET status='cancelled',payload_json='{}' WHERE source_system=?1 AND source_installation_id=?2 AND session_id=?3 AND message_id=?4 AND status='pending'",
                params![source_system, installation_id, session_id, message_id],
            ).map_err(|_| "无法清除待同步记忆来源".to_string())?;
        }
        let changed = transaction.execute("DELETE FROM sessions WHERE id=?1", [session_id])
            .map_err(|_| "无法删除对话".to_string())?;
        if changed == 0 {
            return Err("对话不存在".into());
        }
        for message_id in messages {
            let memory_enabled: bool = transaction.query_row(
                "SELECT value='1' FROM chat_preferences WHERE key='memory_enabled'",
                [], |row| row.get(0),
            ).map_err(|_| "无法读取记忆设置".to_string())?;
            let was_ingested: bool = transaction.query_row(
                "SELECT EXISTS(SELECT 1 FROM memory_source_ingestions WHERE source_system=?1 AND source_installation_id=?2 AND session_id=?3 AND message_id=?4 AND status='synced')",
                params![source_system, installation_id, session_id, message_id], |row| row.get(0),
            ).map_err(|_| "无法读取记忆来源状态".to_string())?;
            if !memory_enabled && !was_ingested {
                continue;
            }
            let request_id = uuid::Uuid::new_v4().to_string();
            transaction.execute(
                "INSERT OR IGNORE INTO memory_source_deletions(request_id,source_system,source_installation_id,session_id,message_id,source_revision,status,created_at) VALUES(?1,?2,?3,?4,?5,1,'pending',?6)",
                params![request_id, source_system, installation_id, session_id, message_id, utc_now()],
            ).map_err(|_| "无法保存记忆来源删除记录".to_string())?;
        }
        transaction.commit().map_err(|_| "无法完成对话删除".to_string())?;
        Ok(())
    }

    pub fn pending_memory_source_deletions(&self, limit: u32) -> Result<Vec<MemorySourceDeletion>, String> {
        let connection = self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?;
        let mut statement = connection.prepare(
            "SELECT request_id,source_system,source_installation_id,session_id,message_id,source_revision,status FROM memory_source_deletions WHERE status='pending' ORDER BY created_at,request_id LIMIT ?1",
        ).map_err(|_| "无法读取待同步记忆来源".to_string())?;
        let rows = statement.query_map([limit.clamp(1, 500)], |row| Ok(MemorySourceDeletion {
            request_id: row.get(0)?, source_system: row.get(1)?, source_installation_id: row.get(2)?,
            session_id: row.get(3)?, message_id: row.get(4)?, source_revision: row.get(5)?, status: row.get(6)?,
        })).map_err(|_| "无法读取待同步记忆来源".to_string())?;
        rows.collect::<Result<Vec<_>, _>>().map_err(|_| "无法读取待同步记忆来源".to_string())
    }

    pub fn acknowledge_memory_source_deletion(&self, request_id: &str) -> Result<(), String> {
        let changed = self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?
            .execute("UPDATE memory_source_deletions SET status='synced' WHERE request_id=?1 AND status='pending'", [request_id])
            .map_err(|_| "无法确认记忆来源删除".to_string())?;
        if changed == 0 { return Err("待同步记忆来源不存在".into()); }
        Ok(())
    }

    pub fn enqueue_memory_source_ingestion(&self, payload: &serde_json::Value) -> Result<String, String> {
        let source_system = payload["source_system"].as_str().ok_or("记忆来源系统无效")?;
        let installation_id = payload["source_installation_id"].as_str().ok_or("记忆安装身份无效")?;
        let session_id = payload["session_id"].as_str().ok_or("记忆会话标识无效")?;
        let message = payload["messages"].as_array().and_then(|items| items.first())
            .ok_or("记忆来源消息缺失")?;
        if message["role"] != "user" || message["content"].as_str().is_none_or(|text| text.chars().count() > 6000) {
            return Err("记忆来源消息超出自动提取范围".into());
        }
        let message_id = message["message_id"].as_str().ok_or("记忆来源消息标识无效")?;
        let revision = message["revision"].as_i64().unwrap_or(1);
        let request_id = uuid::Uuid::new_v4().to_string();
        let serialized = serde_json::to_string(payload).map_err(|_| "无法编码记忆来源")?;
        let connection = self.connection.lock().map_err(|_| "聊天数据库不可用")?;
        connection.execute(
            "INSERT OR IGNORE INTO memory_source_ingestions(request_id,source_system,source_installation_id,session_id,message_id,source_revision,payload_json,status,created_at) VALUES(?1,?2,?3,?4,?5,?6,?7,'pending',?8)",
            params![request_id, source_system, installation_id, session_id, message_id, revision, serialized, utc_now()],
        ).map_err(|_| "无法登记记忆来源待处理任务")?;
        connection.query_row(
            "SELECT request_id FROM memory_source_ingestions WHERE source_system=?1 AND source_installation_id=?2 AND session_id=?3 AND message_id=?4 AND source_revision=?5",
            params![source_system, installation_id, session_id, message_id, revision], |row| row.get(0),
        ).map_err(|_| "无法读取记忆来源待处理任务".into())
    }

    pub fn pending_memory_source_ingestions(&self, limit: u32) -> Result<Vec<(String, serde_json::Value)>, String> {
        let connection = self.connection.lock().map_err(|_| "聊天数据库不可用")?;
        let mut statement = connection.prepare(
            "SELECT request_id,payload_json FROM memory_source_ingestions WHERE status='pending' ORDER BY created_at,request_id LIMIT ?1",
        ).map_err(|_| "无法读取记忆来源待处理任务")?;
        let rows = statement.query_map([limit.clamp(1, 500)], |row| {
            let request_id: String = row.get(0)?;
            let payload: String = row.get(1)?;
            Ok((request_id, payload))
        }).map_err(|_| "无法读取记忆来源待处理任务")?;
        rows.map(|row| {
            let (request_id, payload) = row.map_err(|_| "无法读取记忆来源待处理任务")?;
            let payload = serde_json::from_str(&payload).map_err(|_| "记忆来源待处理数据损坏")?;
            Ok((request_id, payload))
        }).collect()
    }

    pub fn acknowledge_memory_source_ingestion(&self, request_id: &str) -> Result<(), String> {
        self.connection.lock().map_err(|_| "聊天数据库不可用")?
            .execute("UPDATE memory_source_ingestions SET status='synced' WHERE request_id=?1", [request_id])
            .map_err(|_| "无法确认记忆来源同步")?;
        Ok(())
    }

    pub fn has_pending_memory_source_deletions(&self) -> Result<bool, String> {
        self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?
            .query_row("SELECT EXISTS(SELECT 1 FROM memory_source_deletions WHERE status='pending')", [], |row| row.get(0))
            .map_err(|_| "无法检查记忆来源同步状态".to_string())
    }

    pub fn ensure_memory_reads_allowed(&self) -> Result<(), String> {
        let connection = self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?;
        let pending: bool = connection.query_row(
            concat!("SELECT EXISTS(SELECT 1 FROM memory_source_deletions WHERE status='pending') OR ",
                "EXISTS(SELECT 1 FROM memory_item_tombstones) OR EXISTS(SELECT 1 FROM memory_clear_tombstones)"),
            [], |row| row.get(0),
        ).map_err(|_| "无法检查记忆撤销状态".to_string())?;
        if pending {
            return Err("记忆撤销尚未同步，暂时停用记忆读取".into());
        }
        Ok(())
    }

    pub fn enqueue_memory_item_forget(&self, item_id: &str, expected_version: i64) -> Result<String, String> {
        if item_id.is_empty() || expected_version < 1 { return Err("记忆版本无效".into()); }
        let request_id = uuid::Uuid::new_v4().to_string();
        self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?
            .execute(concat!("INSERT INTO memory_item_tombstones(item_id,request_id,expected_version,created_at) VALUES(?1,?2,?3,?4) ",
                "ON CONFLICT(item_id) DO UPDATE SET request_id=excluded.request_id,expected_version=excluded.expected_version,created_at=excluded.created_at ",
                "WHERE memory_item_tombstones.expected_version<>excluded.expected_version"),
                rusqlite::params![item_id, request_id, expected_version, utc_now()])
            .map_err(|_| "无法保存待同步的记忆撤销".to_string())?;
        self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?
            .execute(concat!("DELETE FROM memory_chat_contexts WHERE assistant_message_id IN (",
                "SELECT assistant_message_id FROM memory_chat_context_items WHERE item_id=?1)"), [item_id])
            .map_err(|_| "无法清理已撤销记忆的使用记录".to_string())?;
        self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?
            .query_row("SELECT request_id FROM memory_item_tombstones WHERE item_id=?1", [item_id], |row| row.get(0))
            .map_err(|_| "无法读取待同步的记忆撤销".to_string())
    }

    pub fn pending_memory_item_forgets(&self) -> Result<Vec<(String, String, i64)>, String> {
        let connection = self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?;
        let mut statement = connection.prepare("SELECT item_id,request_id,expected_version FROM memory_item_tombstones ORDER BY created_at")
            .map_err(|_| "无法读取待同步的记忆撤销".to_string())?;
        let rows = statement.query_map([], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))
            .map_err(|_| "无法读取待同步的记忆撤销".to_string())?
            .collect::<Result<Vec<_>, _>>().map_err(|_| "无法读取待同步的记忆撤销".to_string());
        rows
    }

    pub fn acknowledge_memory_item_forget(&self, request_id: &str) -> Result<(), String> {
        self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?
            .execute("DELETE FROM memory_item_tombstones WHERE request_id=?1", [request_id])
            .map(|_| ()).map_err(|_| "无法确认记忆撤销同步".to_string())
    }

    pub fn enqueue_memory_clear(&self, scope_id: &str, expected_generation: i64) -> Result<String, String> {
        if scope_id.is_empty() || expected_generation < 0 { return Err("记忆空间版本无效".into()); }
        let request_id = uuid::Uuid::new_v4().to_string();
        self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?
            .execute(concat!("INSERT INTO memory_clear_tombstones(scope_id,request_id,expected_generation,created_at) VALUES(?1,?2,?3,?4) ",
                "ON CONFLICT(scope_id) DO UPDATE SET request_id=excluded.request_id,expected_generation=excluded.expected_generation,created_at=excluded.created_at ",
                "WHERE memory_clear_tombstones.expected_generation<>excluded.expected_generation"),
                rusqlite::params![scope_id, request_id, expected_generation, utc_now()])
            .map_err(|_| "无法保存待同步的记忆清空".to_string())?;
        self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?
            .execute("DELETE FROM memory_chat_contexts", [])
            .map_err(|_| "无法清理已清空记忆的使用记录".to_string())?;
        self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?
            .query_row("SELECT request_id FROM memory_clear_tombstones WHERE scope_id=?1", [scope_id], |row| row.get(0))
            .map_err(|_| "无法读取待同步的记忆清空".to_string())
    }

    pub fn pending_memory_clear(&self) -> Result<Option<(String, String, i64)>, String> {
        self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?
            .query_row("SELECT scope_id,request_id,expected_generation FROM memory_clear_tombstones LIMIT 1", [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))
            .optional().map_err(|_| "无法读取待同步的记忆清空".to_string())
    }

    pub fn acknowledge_memory_clear(&self, request_id: &str) -> Result<(), String> {
        self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?
            .execute("DELETE FROM memory_clear_tombstones WHERE request_id=?1", [request_id])
            .map(|_| ()).map_err(|_| "无法确认记忆清空同步".to_string())
    }

    pub fn pending_memory_mutation_count(&self) -> Result<usize, String> {
        self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?
            .query_row("SELECT (SELECT count(*) FROM memory_item_tombstones)+(SELECT count(*) FROM memory_clear_tombstones)", [],
                |row| row.get::<_, i64>(0))
            .map(|count| count.max(0) as usize).map_err(|_| "无法读取待同步的记忆变更".to_string())
    }

    pub fn record_memory_chat_context(&self, context: &MemoryChatContext) -> Result<(), String> {
        if context.assistant_message_id.is_empty() || context.session_id.is_empty()
            || context.generation > i64::MAX as u64 || context.items.len() > 16
            || !matches!(context.retrieval_mode.as_str(), "keyword" | "hybrid") {
            return Err("记忆使用引用无效".into());
        }
        for item in &context.items {
            uuid::Uuid::parse_str(&item.item_id).map_err(|_| "记忆使用引用无效")?;
            if item.version == 0 { return Err("记忆使用引用无效".into()); }
        }
        let mut connection = self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?;
        let transaction = connection.transaction().map_err(|_| "无法记录记忆使用引用".to_string())?;
        transaction.execute(
            "INSERT OR REPLACE INTO memory_chat_contexts(assistant_message_id,session_id,generation,retrieval_mode,created_at) VALUES(?1,?2,?3,?4,?5)",
            rusqlite::params![context.assistant_message_id, context.session_id, context.generation as i64, context.retrieval_mode, utc_now()],
        ).map_err(|_| "无法记录记忆使用引用".to_string())?;
        for item in &context.items {
            transaction.execute(
                "INSERT OR IGNORE INTO memory_chat_context_items(assistant_message_id,item_id,version) VALUES(?1,?2,?3)",
                rusqlite::params![context.assistant_message_id, item.item_id, item.version],
            ).map_err(|_| "无法记录记忆使用引用".to_string())?;
        }
        transaction.commit().map_err(|_| "无法记录记忆使用引用".to_string())
    }

    pub fn memory_chat_contexts(&self, session_id: &str) -> Result<Vec<MemoryChatContext>, String> {
        let connection = self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?;
        let mut statement = connection.prepare(
            "SELECT assistant_message_id,session_id,generation,retrieval_mode FROM memory_chat_contexts WHERE session_id=?1 ORDER BY created_at"
        ).map_err(|_| "无法读取记忆使用记录".to_string())?;
        let rows = statement.query_map([session_id], |row| Ok((
            row.get::<_, String>(0)?, row.get::<_, String>(1)?, row.get::<_, i64>(2)?, row.get::<_, String>(3)?,
        ))).map_err(|_| "无法读取记忆使用记录".to_string())?
            .collect::<Result<Vec<_>, _>>().map_err(|_| "无法读取记忆使用记录".to_string())?;
        let mut result = Vec::with_capacity(rows.len());
        for (assistant_message_id, session_id, generation, retrieval_mode) in rows {
            let mut items_statement = connection.prepare(
                "SELECT item_id,version FROM memory_chat_context_items WHERE assistant_message_id=?1 ORDER BY item_id,version"
            ).map_err(|_| "无法读取记忆使用记录".to_string())?;
            let items = items_statement.query_map([&assistant_message_id], |row| Ok(MemoryReference {
                item_id: row.get(0)?, version: row.get(1)?,
            })).map_err(|_| "无法读取记忆使用记录".to_string())?
                .collect::<Result<Vec<_>, _>>().map_err(|_| "无法读取记忆使用记录".to_string())?;
            result.push(MemoryChatContext { assistant_message_id, session_id,
                generation: generation.max(0) as u64, retrieval_mode, items });
        }
        Ok(result)
    }

    pub fn append_message(
        &self,
        session_id: &str,
        role: &str,
        content: &str,
    ) -> Result<ChatMessage, String> {
        if !matches!(role, "user" | "assistant") {
            return Err("消息角色无效".into());
        }
        let message = ChatMessage {
            id: uuid::Uuid::new_v4().to_string(),
            session_id: session_id.into(),
            role: role.into(),
            content: content.into(),
            created_at: utc_now(),
        };
        self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?
            .execute("INSERT INTO messages(id,session_id,role,content,created_at) VALUES(?1,?2,?3,?4,?5)",
                params![message.id, message.session_id, message.role, message.content, message.created_at])
            .map_err(|_| "无法保存消息".to_string())?;
        Ok(message)
    }

    pub fn append_message_with_id(
        &self,
        id: &str,
        session_id: &str,
        role: &str,
        content: &str,
    ) -> Result<(), String> {
        if !matches!(role, "user" | "assistant") {
            return Err("消息角色无效".into());
        }
        self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?
            .execute("INSERT OR IGNORE INTO messages(id,session_id,role,content,created_at) VALUES(?1,?2,?3,?4,?5)",
                params![id, session_id, role, content, utc_now()])
            .map_err(|_| "无法保存消息".to_string())?;
        Ok(())
    }

    pub fn engine_session(&self, desktop_session_id: &str) -> Result<Option<String>, String> {
        self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?
            .query_row("SELECT engine_session_id FROM engine_sessions WHERE desktop_session_id=?1",
                [desktop_session_id], |row| row.get(0))
            .optional().map_err(|_| "无法读取助手会话映射".to_string())
    }

    pub fn bind_engine_session(&self, desktop_session_id: &str, engine_session_id: &str) -> Result<(), String> {
        let connection = self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?;
        connection.execute("INSERT OR IGNORE INTO engine_sessions(desktop_session_id,engine_session_id) VALUES(?1,?2)",
            params![desktop_session_id, engine_session_id])
            .map_err(|_| "无法保存助手会话映射".to_string())?;
        let stored: String = connection.query_row("SELECT engine_session_id FROM engine_sessions WHERE desktop_session_id=?1",
            [desktop_session_id], |row| row.get(0)).map_err(|_| "无法读取助手会话映射".to_string())?;
        if stored != engine_session_id {
            return Err("助手会话映射冲突".into());
        }
        Ok(())
    }

    pub fn messages(&self, session_id: &str) -> Result<Vec<ChatMessage>, String> {
        Ok(self.history_before(session_id, None, 100)?.messages)
    }

    pub fn history_before(
        &self,
        session_id: &str,
        before: Option<&str>,
        limit: usize,
    ) -> Result<HistoryPage, String> {
        let connection = self.connection.lock().map_err(|_| "聊天数据库不可用")?;
        let mut statement = connection.prepare(
            "SELECT id,session_id,role,content,created_at FROM messages WHERE session_id=?1 AND (?2 IS NULL OR rowid < (SELECT rowid FROM messages WHERE id=?2 AND session_id=?1)) ORDER BY rowid DESC LIMIT ?3"
        ).map_err(|_| "无法读取消息")?;
        let mut messages = statement
            .query_map(
                params![session_id, before, limit.clamp(1, 500) as i64 + 1],
                |row| {
                    Ok(ChatMessage {
                        id: row.get(0)?,
                        session_id: row.get(1)?,
                        role: row.get(2)?,
                        content: row.get(3)?,
                        created_at: row.get(4)?,
                    })
                },
            )
            .map_err(|_| "无法读取消息")?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|_| "无法读取消息")?;
        let has_more = messages.len() > limit.clamp(1, 500);
        messages.truncate(limit.clamp(1, 500));
        messages.reverse();
        Ok(HistoryPage { messages, has_more })
    }

    /// Select the current question and recent complete turns within a byte budget.
    /// The display page never determines model input. Bytes are a conservative size limit, not a token count.
    pub fn request_context(
        &self,
        session_id: &str,
        max_bytes: usize,
    ) -> Result<Vec<ChatMessage>, String> {
        let connection = self.connection.lock().map_err(|_| "聊天数据库不可用")?;
        let mut statement = connection.prepare(
            "SELECT id,session_id,role,content,created_at FROM messages WHERE session_id=?1 ORDER BY rowid DESC"
        ).map_err(|_| "无法读取上下文")?;
        let mut rows = statement
            .query([session_id])
            .map_err(|_| "无法读取上下文")?;
        let mut messages = Vec::new();
        let mut used = 0;
        while let Some(row) = rows.next().map_err(|_| "无法读取上下文")? {
            let message = ChatMessage {
                id: row.get(0).map_err(|_| "无法读取上下文")?,
                session_id: row.get(1).map_err(|_| "无法读取上下文")?,
                role: row.get(2).map_err(|_| "无法读取上下文")?,
                content: row.get(3).map_err(|_| "无法读取上下文")?,
                created_at: row.get(4).map_err(|_| "无法读取上下文")?,
            };
            let size = message.content.len() + 64;
            if used + size > max_bytes {
                if messages.is_empty() {
                    return Err("当前消息超过上下文上限，请缩短内容".into());
                }
                break;
            }
            used += size;
            messages.push(message);
        }
        messages.reverse();
        while messages.first().is_some_and(|m| m.role == "assistant") {
            messages.remove(0);
        }
        Ok(messages)
    }

    pub fn messages_page(
        &self,
        session_id: &str,
        offset: u32,
        limit: u32,
    ) -> Result<Vec<ChatMessage>, String> {
        let connection = self
            .connection
            .lock()
            .map_err(|_| "聊天数据库不可用".to_string())?;
        let mut statement = connection.prepare(
            "SELECT id,session_id,role,content,created_at FROM messages WHERE session_id=?1 ORDER BY rowid LIMIT ?2 OFFSET ?3",
        ).map_err(|_| "无法读取消息".to_string())?;
        let rows = statement
            .query_map(params![session_id, limit.clamp(1, 500), offset], |row| {
                Ok(ChatMessage {
                    id: row.get(0)?,
                    session_id: row.get(1)?,
                    role: row.get(2)?,
                    content: row.get(3)?,
                    created_at: row.get(4)?,
                })
            })
            .map_err(|_| "无法读取消息".to_string())?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|_| "无法读取消息".to_string())
    }

    pub fn latest_session(&self) -> Result<Option<String>, String> {
        self.connection.lock().map_err(|_| "聊天数据库不可用".to_string())?
            .query_row("SELECT id FROM sessions ORDER BY (id=(SELECT value FROM chat_preferences WHERE key='current_session')) IS TRUE DESC,created_at DESC,id DESC LIMIT 1", [], |row| row.get(0))
            .optional().map_err(|_| "无法恢复最近对话".to_string())
    }
}

fn normalize_title(title: &str) -> Result<String, String> {
    let title = title.trim();
    if title.is_empty() || title.chars().any(char::is_control) || title.chars().count() > 120 {
        return Err("对话标题无效".into());
    }
    Ok(title.to_string())
}

fn normalize_project_name(name: &str) -> Result<String, String> {
    let name = name.trim();
    if name.is_empty() || name.chars().any(char::is_control) || name.chars().count() > 120 {
        return Err("项目名称无效".into());
    }
    Ok(name.to_string())
}

pub(super) fn normalize_project_path(value: &str) -> Result<String, String> {
    if value.is_empty()
        || value.len() > 4096
        || value.chars().any(char::is_control)
        || value.split(['/', '\\']).any(|part| part == "..")
    {
        return Err("项目目录路径无效".into());
    }
    let path = Path::new(value);
    if !path.is_absolute() {
        return Err("项目目录必须是绝对路径".into());
    }
    #[cfg(windows)]
    {
        let local = value.strip_prefix(r"\\?\").unwrap_or(value);
        let bytes = local.as_bytes();
        if bytes.len() < 3
            || !bytes[0].is_ascii_alphabetic()
            || bytes[1] != b':'
            || !matches!(bytes[2], b'\\' | b'/')
        {
            return Err("请选择本地盘符目录；不支持网络目录".into());
        }
    }
    let canonical = std::fs::canonicalize(path).map_err(|_| "项目目录不可用".to_string())?;
    if !canonical.is_dir() {
        return Err("项目路径必须是目录".into());
    }
    let canonical = canonical.to_str().ok_or("项目目录路径无法编码")?;
    Ok(canonical
        .strip_prefix(r"\\?\")
        .unwrap_or(canonical)
        .to_string())
}
