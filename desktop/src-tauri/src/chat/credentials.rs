#[cfg(test)]
use super::*;

pub trait CredentialStore: Send + Sync {
    fn get(&self) -> Result<Option<String>, String>;
    fn set(&self, value: &str) -> Result<(), String>;
    fn delete(&self) -> Result<(), String>;
}

pub struct NativeCredentialStore;

impl CredentialStore for NativeCredentialStore {
    fn get(&self) -> Result<Option<String>, String> {
        let entry = keyring::Entry::new("dev.tomato.agent.chat", "provider-api-key")
            .map_err(|_| "系统凭据库不可用".to_string())?;
        match entry.get_password() {
            Ok(value) => Ok(Some(value)),
            Err(keyring::Error::NoEntry) => Ok(None),
            Err(_) => Err("系统凭据库不可用".into()),
        }
    }
    fn set(&self, value: &str) -> Result<(), String> {
        let entry = keyring::Entry::new("dev.tomato.agent.chat", "provider-api-key")
            .map_err(|_| "系统凭据库不可用".to_string())?;
        entry
            .set_password(value)
            .map_err(|_| "无法保存系统凭据".to_string())
    }
    fn delete(&self) -> Result<(), String> {
        let entry = keyring::Entry::new("dev.tomato.agent.chat", "provider-api-key")
            .map_err(|_| "系统凭据库不可用".to_string())?;
        match entry.delete_credential() {
            Ok(_) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(_) => Err("无法删除系统凭据".into()),
        }
    }
}

#[cfg(test)]
pub(super) struct TestCredentialStore(pub(super) Mutex<Option<String>>);

#[cfg(test)]
impl CredentialStore for TestCredentialStore {
    fn get(&self) -> Result<Option<String>, String> {
        Ok(self.0.lock().unwrap().clone())
    }
    fn set(&self, value: &str) -> Result<(), String> {
        *self.0.lock().unwrap() = Some(value.into());
        Ok(())
    }
    fn delete(&self) -> Result<(), String> {
        *self.0.lock().unwrap() = None;
        Ok(())
    }
}
