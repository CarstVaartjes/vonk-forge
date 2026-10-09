//! Errors.

use super::*;

#[derive(Debug, Error)]
pub enum SetupError {
    #[error("template payload is invalid: {0}")]
    InvalidPayload(String),
    #[error("destination is unsafe: {0}")]
    UnsafeDestination(String),
    #[error("bundle does not exist or is incomplete")]
    MissingBundle,
    #[error("input ended before setup was complete")]
    InputEnded,
    #[error("I/O error: {0}")]
    Io(#[from] io::Error),
    #[error(
        "permission denied for {}: {source}; existing bundle files belong to root, so {}",
        path.display(),
        root_rerun_hint(std::env::var("VONK_INSTALLER_URL").ok().as_deref())
    )]
    PermissionDenied { path: PathBuf, source: io::Error },
    #[error(transparent)]
    SecretGeneration(#[from] SecretGenerationError),
    #[error("generated secret material is invalid: {0}")]
    InvalidSecretMaterial(String),
}

/// Name the exact rerun command. `sudo` belongs on the `sh` side of the pipe;
/// `sudo curl ... | sh` still runs the installer unprivileged. The channel
/// endpoint exports the URL it was fetched from as `VONK_INSTALLER_URL`.
pub fn root_rerun_hint(installer_url: Option<&str>) -> String {
    match installer_url.filter(|url| {
        url.starts_with("https://")
            && url
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || b"-._~:/".contains(&byte))
    }) {
        Some(url) => format!("rerun it as: curl -fsSL {url} | sudo sh"),
        None => "rerun the same installer command with sudo on the sh side: curl -fsSL <installer URL> | sudo sh".to_owned(),
    }
}

#[cfg(test)]
mod tests {

    #[test]
    fn root_rerun_hint_names_the_exact_command() {
        use crate::root_rerun_hint;
        assert_eq!(
            root_rerun_hint(Some("https://install.vonkforge.ai/dev/nas")),
            "rerun it as: curl -fsSL https://install.vonkforge.ai/dev/nas | sudo sh"
        );
        assert!(root_rerun_hint(Some("https://x/nas; rm -rf /")).contains("<installer URL>"));
        assert!(root_rerun_hint(Some("http://x/nas")).contains("| sudo sh"));
        assert!(root_rerun_hint(None).contains("| sudo sh"));
    }
}
