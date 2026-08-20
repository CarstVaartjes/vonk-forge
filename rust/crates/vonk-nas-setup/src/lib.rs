#![forbid(unsafe_code)]

use std::collections::HashSet;
use std::fs::{self, File, OpenOptions};
use std::io::{self, BufRead, Write};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use ring::rand::{SecureRandom, SystemRandom};
use serde::Deserialize;
use thiserror::Error;

static STAGING_SEQUENCE: AtomicU64 = AtomicU64::new(0);

#[derive(Debug, Error)]
pub enum SetupError {
    #[error("template payload is invalid: {0}")]
    InvalidPayload(String),
    #[error("destination is unsafe: {0}")]
    UnsafeDestination(String),
    #[error("bundle already exists; use explicit upgrade mode")]
    AlreadyExists,
    #[error("bundle does not exist or is incomplete")]
    MissingBundle,
    #[error("input ended before setup was complete")]
    InputEnded,
    #[error("I/O error: {0}")]
    Io(#[from] io::Error),
    #[error(transparent)]
    SecretGeneration(#[from] SecretGenerationError),
}

#[derive(Debug, Error)]
#[error("operating-system random number generation failed")]
pub struct SecretGenerationError;

pub trait SecretGenerator {
    fn generate(&self, bytes: usize) -> Result<String, SecretGenerationError>;
}

#[derive(Debug, Default, Clone, Copy)]
pub struct OsSecretGenerator;

impl SecretGenerator for OsSecretGenerator {
    fn generate(&self, bytes: usize) -> Result<String, SecretGenerationError> {
        if bytes == 0 {
            return Err(SecretGenerationError);
        }
        let mut random = vec![0_u8; bytes];
        SystemRandom::new()
            .fill(&mut random)
            .map_err(|_| SecretGenerationError)?;
        Ok(hex::encode(random))
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CanonicalTemplatePayload {
    schema_version: u8,
    docker_compose_yaml: String,
    #[serde(default)]
    required_values: Vec<RequiredValuePrompt>,
    #[serde(default)]
    secrets: Vec<SecretPrompt>,
    hermes: Option<HermesPrompt>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RequiredValuePrompt {
    env: String,
    prompt: String,
    default: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct SecretPrompt {
    file: String,
    prompt: String,
    generate_bytes: Option<usize>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct HermesPrompt {
    env: String,
    prompt: String,
    #[serde(default)]
    required_values: Vec<RequiredValuePrompt>,
    #[serde(default)]
    secrets: Vec<SecretPrompt>,
}

impl CanonicalTemplatePayload {
    pub fn from_json(raw: &[u8]) -> Result<Self, SetupError> {
        let payload: Self = serde_json::from_slice(raw)
            .map_err(|error| SetupError::InvalidPayload(error.to_string()))?;
        payload.validate()?;
        Ok(payload)
    }

    fn validate(&self) -> Result<(), SetupError> {
        if self.schema_version != 1 {
            return Err(SetupError::InvalidPayload(
                "unsupported schema version".to_owned(),
            ));
        }
        if self.docker_compose_yaml.is_empty() || self.docker_compose_yaml.contains('\0') {
            return Err(SetupError::InvalidPayload(
                "docker compose payload is empty or malformed".to_owned(),
            ));
        }

        let mut environment = HashSet::new();
        let mut secrets = HashSet::new();
        validate_prompts(
            &self.required_values,
            &self.secrets,
            &mut environment,
            &mut secrets,
        )?;
        if let Some(hermes) = &self.hermes {
            validate_env_name(&hermes.env)?;
            if !environment.insert(hermes.env.as_str()) {
                return Err(SetupError::InvalidPayload(format!(
                    "duplicate environment key {}",
                    hermes.env
                )));
            }
            if hermes.prompt.trim().is_empty() {
                return Err(SetupError::InvalidPayload(
                    "Hermes prompt is empty".to_owned(),
                ));
            }
            validate_prompts(
                &hermes.required_values,
                &hermes.secrets,
                &mut environment,
                &mut secrets,
            )?;
        }
        Ok(())
    }
}

fn validate_prompts<'a>(
    values: &'a [RequiredValuePrompt],
    secret_prompts: &'a [SecretPrompt],
    environment: &mut HashSet<&'a str>,
    secrets: &mut HashSet<&'a str>,
) -> Result<(), SetupError> {
    for value in values {
        validate_env_name(&value.env)?;
        if value.prompt.trim().is_empty() {
            return Err(SetupError::InvalidPayload(format!(
                "prompt for {} is empty",
                value.env
            )));
        }
        if !environment.insert(&value.env) {
            return Err(SetupError::InvalidPayload(format!(
                "duplicate environment key {}",
                value.env
            )));
        }
    }
    for secret in secret_prompts {
        validate_secret_name(&secret.file)?;
        if secret.prompt.trim().is_empty() {
            return Err(SetupError::InvalidPayload(format!(
                "prompt for {} is empty",
                secret.file
            )));
        }
        if secret
            .generate_bytes
            .is_some_and(|bytes| !(16..=128).contains(&bytes))
        {
            return Err(SetupError::InvalidPayload(format!(
                "generation size for {} is outside 16..=128 bytes",
                secret.file
            )));
        }
        if !secrets.insert(&secret.file) {
            return Err(SetupError::InvalidPayload(format!(
                "duplicate secret file {}",
                secret.file
            )));
        }
        if secrets.iter().any(|existing| {
            *existing != secret.file
                && (existing
                    .strip_prefix(&secret.file)
                    .is_some_and(|suffix| suffix.starts_with('/'))
                    || secret
                        .file
                        .strip_prefix(*existing)
                        .is_some_and(|suffix| suffix.starts_with('/')))
        }) {
            return Err(SetupError::InvalidPayload(format!(
                "secret path {} conflicts with another secret",
                secret.file
            )));
        }
    }
    Ok(())
}

fn validate_env_name(name: &str) -> Result<(), SetupError> {
    let mut characters = name.chars();
    let valid = characters
        .next()
        .is_some_and(|character| character.is_ascii_uppercase() || character == '_')
        && characters.all(|character| {
            character.is_ascii_uppercase() || character.is_ascii_digit() || character == '_'
        });
    if !valid {
        return Err(SetupError::InvalidPayload(format!(
            "invalid environment key {name}"
        )));
    }
    Ok(())
}

fn validate_secret_name(name: &str) -> Result<(), SetupError> {
    let path = Path::new(name);
    let valid = !name.is_empty()
        && !path.is_absolute()
        && name.split('/').all(is_safe_secret_component)
        && path
            .components()
            .all(|component| matches!(component, Component::Normal(_)));
    if !valid {
        return Err(SetupError::InvalidPayload(format!(
            "invalid secret filename {name}"
        )));
    }
    Ok(())
}

fn is_safe_secret_component(component: &str) -> bool {
    !component.is_empty()
        && component != "."
        && component != ".."
        && component.chars().all(|character| {
            character.is_ascii_lowercase()
                || character.is_ascii_digit()
                || matches!(character, '-' | '_' | '.')
        })
}

pub struct PromptIo<R, W> {
    reader: R,
    writer: W,
}

impl<R, W> PromptIo<R, W> {
    pub fn new(reader: R, writer: W) -> Self {
        Self { reader, writer }
    }
}

impl<R: BufRead, W: Write> PromptIo<R, W> {
    fn line(&mut self, label: &str) -> Result<String, SetupError> {
        write!(self.writer, "{label}: ")?;
        self.writer.flush()?;
        let mut value = String::new();
        if self.reader.read_line(&mut value)? == 0 {
            return Err(SetupError::InputEnded);
        }
        Ok(value.trim_end_matches(['\r', '\n']).to_owned())
    }

    fn required(&mut self, prompt: &RequiredValuePrompt) -> Result<String, SetupError> {
        let label = match &prompt.default {
            Some(default) => format!("{} [{}]", prompt.prompt, default),
            None => prompt.prompt.clone(),
        };
        loop {
            let value = self.line(&label)?;
            if !value.is_empty() {
                return Ok(value);
            }
            if let Some(default) = &prompt.default {
                return Ok(default.clone());
            }
            writeln!(self.writer, "A value is required.")?;
        }
    }

    fn secret<G: SecretGenerator>(
        &mut self,
        prompt: &SecretPrompt,
        generator: &G,
    ) -> Result<String, SetupError> {
        let label = if prompt.generate_bytes.is_some() {
            format!("{} (leave blank to generate)", prompt.prompt)
        } else {
            prompt.prompt.clone()
        };
        loop {
            let value = self.line(&label)?;
            if !value.is_empty() {
                return Ok(value);
            }
            if let Some(bytes) = prompt.generate_bytes {
                return generator.generate(bytes).map_err(SetupError::from);
            }
            writeln!(self.writer, "A value is required.")?;
        }
    }

    fn confirm(&mut self, label: &str) -> Result<bool, SetupError> {
        loop {
            let value = self.line(&format!("{label} [y/N]"))?;
            match value.trim().to_ascii_lowercase().as_str() {
                "y" | "yes" => return Ok(true),
                "" | "n" | "no" => return Ok(false),
                _ => writeln!(self.writer, "Please answer yes or no.")?,
            }
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SetupMode {
    Install,
    Upgrade,
}

#[derive(Debug)]
pub struct SetupRequest {
    output_root: PathBuf,
    mode: SetupMode,
}

impl SetupRequest {
    pub fn install(output_root: impl AsRef<Path>) -> Self {
        Self {
            output_root: output_root.as_ref().to_path_buf(),
            mode: SetupMode::Install,
        }
    }

    pub fn upgrade(output_root: impl AsRef<Path>) -> Self {
        Self {
            output_root: output_root.as_ref().to_path_buf(),
            mode: SetupMode::Upgrade,
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub struct SetupOutcome {
    pub root: PathBuf,
    pub hermes_enabled: Option<bool>,
}

pub fn prepare<R: BufRead, W: Write, G: SecretGenerator>(
    payload: &CanonicalTemplatePayload,
    request: SetupRequest,
    prompt: &mut PromptIo<R, W>,
    generator: &G,
) -> Result<SetupOutcome, SetupError> {
    ensure_safe_output_root(&request.output_root)?;
    let bundle = request.output_root.join("vonk-forge");
    match request.mode {
        SetupMode::Install => install(payload, &bundle, prompt, generator),
        SetupMode::Upgrade => upgrade(payload, &bundle),
    }
}

fn install<R: BufRead, W: Write, G: SecretGenerator>(
    payload: &CanonicalTemplatePayload,
    bundle: &Path,
    prompt: &mut PromptIo<R, W>,
    generator: &G,
) -> Result<SetupOutcome, SetupError> {
    if fs::symlink_metadata(bundle).is_ok() {
        return Err(SetupError::AlreadyExists);
    }

    let staging = create_staging_directory(bundle.parent().expect("bundle has output parent"))?;
    let result = (|| {
        let mut environment = Vec::new();
        let mut secret_values = Vec::new();
        for value in &payload.required_values {
            environment.push((&value.env, prompt.required(value)?));
        }
        for secret in &payload.secrets {
            secret_values.push((&secret.file, prompt.secret(secret, generator)?));
        }

        let hermes_enabled = if let Some(hermes) = &payload.hermes {
            let enabled = prompt.confirm(&hermes.prompt)?;
            environment.push((&hermes.env, enabled.to_string()));
            if enabled {
                for value in &hermes.required_values {
                    environment.push((&value.env, prompt.required(value)?));
                }
                for secret in &hermes.secrets {
                    secret_values.push((&secret.file, prompt.secret(secret, generator)?));
                }
            }
            Some(enabled)
        } else {
            None
        };

        write_new_file(
            &staging.join("docker-compose.yaml"),
            payload.docker_compose_yaml.as_bytes(),
            0o644,
        )?;
        let environment = render_environment(&environment)?;
        write_new_file(&staging.join(".env"), environment.as_bytes(), 0o600)?;
        let secret_directory = staging.join("secrets");
        create_secure_directory(&secret_directory)?;
        for (name, value) in secret_values {
            let mut content = value.into_bytes();
            content.push(b'\n');
            write_secret_file(&secret_directory, name, &content)?;
        }
        sync_directory(&secret_directory)?;
        sync_directory(&staging)?;
        fs::rename(&staging, bundle)?;
        sync_directory(bundle.parent().expect("bundle has output parent"))?;
        Ok(SetupOutcome {
            root: bundle.to_path_buf(),
            hermes_enabled,
        })
    })();
    if result.is_err() {
        let _ = fs::remove_dir_all(&staging);
    }
    result
}

fn upgrade(payload: &CanonicalTemplatePayload, bundle: &Path) -> Result<SetupOutcome, SetupError> {
    validate_existing_bundle(bundle)?;
    atomic_replace(
        &bundle.join("docker-compose.yaml"),
        payload.docker_compose_yaml.as_bytes(),
        0o644,
    )?;
    Ok(SetupOutcome {
        root: bundle.to_path_buf(),
        hermes_enabled: None,
    })
}

fn render_environment(values: &[(&String, String)]) -> Result<String, SetupError> {
    let mut rendered = String::new();
    for (key, value) in values {
        if value.contains('\0') {
            return Err(SetupError::InvalidPayload(format!(
                "value for {key} contains NUL"
            )));
        }
        let encoded = if value
            .chars()
            .all(|character| character.is_ascii_alphanumeric() || "._-:/".contains(character))
        {
            value.clone()
        } else {
            serde_json::to_string(value)
                .map_err(|error| SetupError::InvalidPayload(error.to_string()))?
        };
        rendered.push_str(key);
        rendered.push('=');
        rendered.push_str(&encoded);
        rendered.push('\n');
    }
    Ok(rendered)
}

fn ensure_safe_output_root(output: &Path) -> Result<(), SetupError> {
    if output.as_os_str().is_empty() {
        return Err(SetupError::UnsafeDestination("path is empty".to_owned()));
    }
    reject_symlink_components(output)?;
    match fs::symlink_metadata(output) {
        Ok(metadata) if metadata.is_dir() && !metadata.file_type().is_symlink() => {}
        Ok(_) => {
            return Err(SetupError::UnsafeDestination(
                "output root is not a real directory".to_owned(),
            ));
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            fs::create_dir_all(output)?;
            set_directory_mode(output)?;
            reject_symlink_components(output)?;
        }
        Err(error) => return Err(error.into()),
    }
    Ok(())
}

fn reject_symlink_components(path: &Path) -> Result<(), SetupError> {
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()?.join(path)
    };
    let mut current = PathBuf::new();
    for component in absolute.components() {
        match component {
            Component::ParentDir => {
                return Err(SetupError::UnsafeDestination(
                    "parent traversal is not allowed".to_owned(),
                ));
            }
            Component::CurDir => continue,
            _ => current.push(component.as_os_str()),
        }
        match fs::symlink_metadata(&current) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                return Err(SetupError::UnsafeDestination(format!(
                    "{} is a symbolic link",
                    current.display()
                )));
            }
            Ok(_) => {}
            Err(error) if error.kind() == io::ErrorKind::NotFound => break,
            Err(error) => return Err(error.into()),
        }
    }
    Ok(())
}

fn validate_existing_bundle(bundle: &Path) -> Result<(), SetupError> {
    reject_symlink_components(bundle)?;
    require_real_directory(bundle)?;
    require_regular_file(&bundle.join("docker-compose.yaml"))?;
    require_regular_file(&bundle.join(".env"))?;
    let secrets = bundle.join("secrets");
    require_real_directory(&secrets)?;
    validate_secret_tree(&secrets)?;
    Ok(())
}

fn validate_secret_tree(directory: &Path) -> Result<(), SetupError> {
    for entry in fs::read_dir(directory)? {
        let entry = entry?;
        let path = entry.path();
        let name = entry
            .file_name()
            .into_string()
            .map_err(|_| SetupError::UnsafeDestination("non-UTF-8 secret path".to_owned()))?;
        if !is_safe_secret_component(&name) {
            return Err(SetupError::UnsafeDestination(format!(
                "{} has an unsafe path component",
                path.display()
            )));
        }
        let metadata = fs::symlink_metadata(&path)?;
        if metadata.file_type().is_symlink() {
            return Err(SetupError::UnsafeDestination(format!(
                "{} is a symbolic link",
                path.display()
            )));
        }
        if metadata.is_dir() {
            validate_secret_tree(&path)?;
        } else if !metadata.is_file() {
            return Err(SetupError::UnsafeDestination(format!(
                "{} is not a regular secret file",
                path.display()
            )));
        }
    }
    Ok(())
}

fn require_real_directory(path: &Path) -> Result<(), SetupError> {
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.is_dir() && !metadata.file_type().is_symlink() => Ok(()),
        _ => Err(SetupError::MissingBundle),
    }
}

fn require_regular_file(path: &Path) -> Result<(), SetupError> {
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.is_file() && !metadata.file_type().is_symlink() => Ok(()),
        _ => Err(SetupError::MissingBundle),
    }
}

fn create_staging_directory(parent: &Path) -> Result<PathBuf, SetupError> {
    for _ in 0..32 {
        let sequence = STAGING_SEQUENCE.fetch_add(1, Ordering::Relaxed);
        let candidate = parent.join(format!(
            ".vonk-forge.setup-{}-{sequence}",
            std::process::id()
        ));
        match fs::create_dir(&candidate) {
            Ok(()) => {
                set_directory_mode(&candidate)?;
                return Ok(candidate);
            }
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error.into()),
        }
    }
    Err(SetupError::UnsafeDestination(
        "could not reserve a staging directory".to_owned(),
    ))
}

fn create_secure_directory(path: &Path) -> Result<(), SetupError> {
    fs::create_dir(path)?;
    set_directory_mode(path)?;
    Ok(())
}

fn write_secret_file(root: &Path, relative: &str, content: &[u8]) -> Result<(), SetupError> {
    let path = Path::new(relative);
    let parent = path.parent().unwrap_or_else(|| Path::new(""));
    let mut current = root.to_path_buf();
    for component in parent.components() {
        let Component::Normal(component) = component else {
            return Err(SetupError::InvalidPayload(format!(
                "invalid secret path {relative}"
            )));
        };
        current.push(component);
        match fs::symlink_metadata(&current) {
            Ok(metadata) if metadata.is_dir() && !metadata.file_type().is_symlink() => {}
            Ok(_) => {
                return Err(SetupError::UnsafeDestination(format!(
                    "{} is not a real directory",
                    current.display()
                )));
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                create_secure_directory(&current)?;
            }
            Err(error) => return Err(error.into()),
        }
    }
    let target = root.join(path);
    write_new_file(&target, content, 0o600)?;
    sync_directory(target.parent().expect("secret has parent"))?;
    Ok(())
}

fn write_new_file(path: &Path, content: &[u8], mode: u32) -> Result<(), SetupError> {
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    set_open_mode(&mut options, mode);
    let mut file = options.open(path)?;
    file.write_all(content)?;
    file.sync_all()?;
    set_file_mode(path, mode)?;
    Ok(())
}

fn atomic_replace(path: &Path, content: &[u8], mode: u32) -> Result<(), SetupError> {
    require_regular_file(path)?;
    let parent = path.parent().expect("file has parent");
    let temporary = parent.join(format!(
        ".docker-compose.yaml.tmp-{}-{}",
        std::process::id(),
        STAGING_SEQUENCE.fetch_add(1, Ordering::Relaxed)
    ));
    write_new_file(&temporary, content, mode)?;
    match fs::rename(&temporary, path) {
        Ok(()) => {
            sync_directory(parent)?;
            Ok(())
        }
        Err(error) => {
            let _ = fs::remove_file(temporary);
            Err(error.into())
        }
    }
}

fn sync_directory(path: &Path) -> Result<(), SetupError> {
    File::open(path)?.sync_all()?;
    Ok(())
}

#[cfg(unix)]
fn set_open_mode(options: &mut OpenOptions, mode: u32) {
    use std::os::unix::fs::OpenOptionsExt;
    options.mode(mode);
}

#[cfg(not(unix))]
fn set_open_mode(_options: &mut OpenOptions, _mode: u32) {}

#[cfg(unix)]
fn set_directory_mode(path: &Path) -> Result<(), SetupError> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    Ok(())
}

#[cfg(not(unix))]
fn set_directory_mode(_path: &Path) -> Result<(), SetupError> {
    Ok(())
}

#[cfg(unix)]
fn set_file_mode(path: &Path, mode: u32) -> Result<(), SetupError> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(mode))?;
    Ok(())
}

#[cfg(not(unix))]
fn set_file_mode(_path: &Path, _mode: u32) -> Result<(), SetupError> {
    Ok(())
}
