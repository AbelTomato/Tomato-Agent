use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use std::path::Path;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};
use tauri::Emitter;
use tauri::Manager;
use tauri_plugin_dialog::DialogExt;

mod models;
use models::utc_now;
pub use models::*;
mod store;
#[cfg(test)]
use store::normalize_project_path;
pub use store::*;
mod provider;
use provider::wait_cancelled;
#[cfg(test)]
use provider::SseParser;
pub use provider::*;
mod credentials;
#[cfg(test)]
use credentials::TestCredentialStore;
pub use credentials::*;
mod runtime;
pub use runtime::*;
mod commands;
pub use commands::*;
#[cfg(test)]
mod tests;
