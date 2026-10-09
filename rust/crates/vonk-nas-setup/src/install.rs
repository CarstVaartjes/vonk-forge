//! Install.

use super::*;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SetupMode {
    Install,
    Upgrade,
}

#[derive(Debug)]
pub struct SetupRequest {
    pub(super) output_root: PathBuf,
    pub(super) mode: SetupMode,
    pub(super) hermes_enabled: Option<bool>,
}

impl SetupRequest {
    pub fn install(output_root: impl AsRef<Path>) -> Self {
        Self {
            output_root: output_root.as_ref().to_path_buf(),
            mode: SetupMode::Install,
            hermes_enabled: None,
        }
    }

    pub fn upgrade(output_root: impl AsRef<Path>) -> Self {
        Self {
            output_root: output_root.as_ref().to_path_buf(),
            mode: SetupMode::Upgrade,
            hermes_enabled: None,
        }
    }

    pub fn with_hermes_enabled(mut self, enabled: bool) -> Self {
        self.hermes_enabled = Some(enabled);
        self
    }
}

#[derive(Debug, PartialEq, Eq)]
pub struct SetupOutcome {
    pub root: PathBuf,
    pub hermes_enabled: Option<bool>,
    /// `.env` keys an upgrade dropped because the payload no longer names them.
    pub dropped_environment: Vec<String>,
}

pub fn prepare<R: BufRead, W: Write, S: SecretInput<R, W>, G: SecretGenerator>(
    payload: &CanonicalTemplatePayload,
    request: SetupRequest,
    prompt: &mut PromptIo<R, W, S>,
    generator: &G,
) -> Result<SetupOutcome, SetupError> {
    let mut output = None;
    for attempt in 0..3 {
        match ensure_safe_output_root(&request.output_root) {
            Ok(root) => {
                output = Some(root);
                break;
            }
            Err(SetupError::Io(error))
                if error.kind() != io::ErrorKind::PermissionDenied && attempt < 2 =>
            {
                std::thread::sleep(std::time::Duration::from_millis(50 << attempt));
            }
            Err(error) => return Err(error),
        }
    }
    let output_root = output.ok_or_else(publication::unknown)?;
    let _publication_owner = publication::acquire_owner(&output_root)?;
    let bundle = output_root.join("vonk-forge");
    let result = match request.mode {
        SetupMode::Install => install(payload, &bundle, request.hermes_enabled, prompt, generator),
        SetupMode::Upgrade => upgrade(payload, &bundle, request.hermes_enabled, prompt, generator),
    };
    // A bare permission error names no file; point at the bundle at least.
    match result {
        Err(SetupError::Io(source)) if source.kind() == io::ErrorKind::PermissionDenied => {
            Err(SetupError::PermissionDenied {
                path: bundle,
                source,
            })
        }
        other => other,
    }
}

/// Attach the path to a permission failure so the operator sees what to fix.
pub(super) fn at_path(path: &Path, error: io::Error) -> SetupError {
    if error.kind() == io::ErrorKind::PermissionDenied {
        SetupError::PermissionDenied {
            path: path.to_path_buf(),
            source: error,
        }
    } else {
        SetupError::Io(error)
    }
}

pub(super) fn install<R: BufRead, W: Write, S: SecretInput<R, W>, G: SecretGenerator>(
    payload: &CanonicalTemplatePayload,
    bundle: &Path,
    requested_hermes_enabled: Option<bool>,
    prompt: &mut PromptIo<R, W, S>,
    generator: &G,
) -> Result<SetupOutcome, SetupError> {
    if fs::symlink_metadata(bundle).is_ok() {
        // Publication may have completed before its response was lost. The
        // normal recovery path observes retained credentials and never mints
        // replacement authority merely because this is a fresh request.
        return upgrade(payload, bundle, requested_hermes_enabled, prompt, generator);
    }

    let staging = create_staging_directory(bundle.parent().expect("bundle has output parent"))?;
    let result = (|| {
        let lab_mode = payload
            .install_modes
            .as_ref()
            .map(|modes| prompt.install_mode(modes))
            .transpose()?
            .unwrap_or(false);
        if !lab_mode {
            prompt.preflight(&payload.preflight)?;
        }
        let mut environment = payload
            .internal_values
            .iter()
            .map(|value| (value.env.clone(), value.value.clone()))
            .collect::<Vec<_>>();
        if let Some(modes) = &payload.install_modes {
            set_environment_value(
                &mut environment,
                "COMPOSE_PROFILES",
                if lab_mode {
                    String::new()
                } else {
                    modes.secure_remote_value.clone()
                },
            );
            if lab_mode {
                for value in &modes.lab_values {
                    set_environment_value(&mut environment, &value.env, value.value.clone());
                }
            }
        }
        collect_required_values(&payload.required_values, &mut environment, prompt)?;

        let mut secret_values = Vec::new();
        for secret in &payload.secrets {
            let value = if lab_mode && secret.secure_remote_only {
                String::new()
            } else if secret.optional {
                prompt.optional_secret(secret)?
            } else {
                prompt.secret(secret)?
            };
            secret_values.push((secret.file.clone(), value));
        }
        generate_missing_secrets(payload, None, &mut secret_values, generator)?;
        if let Some(request) = &payload.step_ca_controller {
            let files = retry_generated(|| generate_pki(request, &environment, generator))?;
            validate_pki_material(request, &environment, &files)?;
            secret_values.extend(files);
        }

        let hermes_enabled = if let Some(hermes) = &payload.hermes {
            let enabled = match requested_hermes_enabled {
                Some(enabled) => enabled,
                None if lab_mode => false,
                None => prompt.confirm(&hermes.prompt)?,
            };
            let profile_value = with_compose_profile(
                environment_value(&environment, &hermes.env).unwrap_or_default(),
                hermes,
                enabled,
            );
            set_environment_value(&mut environment, &hermes.env, profile_value);
            Some(enabled)
        } else {
            None
        };

        write_new_file(
            &staging.join("docker-compose.yaml"),
            payload.docker_compose_yaml.as_bytes(),
            0o644,
        )?;
        let environment = render_owned_environment(&environment)?;
        write_new_file(&staging.join(".env"), environment.as_bytes(), 0o600)?;
        let secret_directory = staging.join("secrets");
        create_secure_directory(&secret_directory)?;
        // The Controller writes the gateway client key here. Create it as the
        // invoking user so the bundle owner keeps ownership; the Controller
        // only adjusts the group and mode.
        create_secure_directory(&secret_directory.join(GATEWAY_SECRET_DIRECTORY))?;
        // Compose bind mounts the backup directories from the bundle. Create
        // them while the installer is still running as the invoking user so
        // Docker cannot materialize (or refuse) a missing host directory.
        for directory in BUNDLE_DIRECTORIES {
            create_secure_directory(&staging.join(directory))?;
        }
        for (name, value) in secret_values {
            let content = if value.is_empty() {
                Vec::new()
            } else {
                secret_file_content(value)
            };
            write_secret_file(&secret_directory, &name, &content)?;
        }
        apply_secret_group(payload, &secret_directory, prompt)?;
        publication::remember_verified(payload, &staging)?;
        sync_directory(&secret_directory)?;
        sync_directory(&staging)?;
        fs::rename(&staging, bundle)?;
        sync_directory(bundle.parent().expect("bundle has output parent"))?;
        Ok(SetupOutcome {
            root: bundle.to_path_buf(),
            hermes_enabled,
            dropped_environment: Vec::new(),
        })
    })();
    if result.is_err() {
        let _ = fs::remove_dir_all(&staging);
    }
    result
}

/// Host directories the Compose bundle bind mounts.
/// Host bind mount owned by the bundle user; the Controller writes into it.
pub(super) const GATEWAY_SECRET_DIRECTORY: &str = "gateway";
pub(super) const BUNDLE_DIRECTORIES: [&str; 2] = ["backups", "backups-offhost"];

/// Fill every required value that is not set yet. A value with a default
/// (fixed or derived from values already known) is taken without a prompt and
/// reported, so the operator can still change it in `.env` later.
pub(super) fn collect_required_values<R: BufRead, W: Write, S: SecretInput<R, W>>(
    required_values: &[RequiredValuePrompt],
    environment: &mut Vec<(String, String)>,
    prompt: &mut PromptIo<R, W, S>,
) -> Result<(), SetupError> {
    for required in required_values {
        if environment_value(environment, &required.env).is_some() {
            continue;
        }
        let value = match required_value_default(required, environment) {
            Some(default) => {
                prompt.note(&format!(
                    "{}: {default} (change it in .env)",
                    required.prompt
                ))?;
                default
            }
            None => prompt.required(required)?,
        };
        environment.push((required.env.clone(), value));
    }
    Ok(())
}

/// Generate every internal secret that does not exist yet. Nothing here is
/// asked: internal credentials, keys, and database URLs are always generated.
pub(super) fn generate_missing_secrets<G: SecretGenerator>(
    payload: &CanonicalTemplatePayload,
    existing_root: Option<&Path>,
    secret_values: &mut Vec<(String, String)>,
    generator: &G,
) -> Result<(), SetupError> {
    let exists = |file: &str| match existing_root {
        Some(root) => secret_file_exists(root, file),
        None => Ok(false),
    };
    let generated = &generated_secrets(payload);
    for request in &generated.random_text {
        if !exists(&request.file)? {
            let value = retry_generated(|| {
                generator
                    .generate(request.bytes as usize)
                    .map_err(SetupError::from)
            })?;
            let value = format!("{}{value}", request.prefix.as_deref().unwrap_or_default());
            secret_values.push((request.file.clone(), value));
        }
    }
    for request in &generated.ed25519_pkcs8_pem {
        if !exists(&request.file)? {
            secret_values.push((
                request.file.clone(),
                canonical_ed25519_pkcs8_pem(&retry_generated(generate_ed25519_key)?),
            ));
        }
    }
    for request in &generated.postgres_urls {
        if exists(&request.file)? {
            continue;
        }
        let password = match secret_value(secret_values, &request.password_file) {
            Ok(value) => value.to_owned(),
            Err(error) => match existing_root {
                Some(root) => read_existing_secret(root, &request.password_file)?,
                None => return Err(error),
            },
        };
        secret_values.push((
            request.file.clone(),
            render_postgres_url(request, &password)?,
        ));
    }
    Ok(())
}

pub(super) fn secret_value<'a>(
    values: &'a [(String, String)],
    file: &str,
) -> Result<&'a str, SetupError> {
    values
        .iter()
        .find_map(|(name, value)| (name.as_str() == file).then_some(value.as_str()))
        .ok_or_else(|| {
            SetupError::InvalidPayload(format!("generated secret dependency {file} is unavailable"))
        })
}

pub(super) fn render_postgres_url(
    request: &PostgresUrlRequest,
    password: &str,
) -> Result<String, SetupError> {
    validate_single_line_secret(password, &request.password_file)?;
    let mut url = url::Url::parse(&format!(
        "{}://{}:{}/",
        request.scheme, request.host, request.port
    ))
    .map_err(|_| {
        SetupError::InvalidPayload(format!(
            "PostgreSQL URL request for {} is invalid",
            request.file
        ))
    })?;
    url.set_username(&request.username).map_err(|_| {
        SetupError::InvalidPayload(format!(
            "PostgreSQL username for {} is invalid",
            request.file
        ))
    })?;
    url.set_password(Some(password)).map_err(|_| {
        SetupError::InvalidSecretMaterial(format!(
            "password for {} cannot be encoded",
            request.file
        ))
    })?;
    url.set_path(&request.database);
    Ok(url.into())
}

pub(super) fn secret_file_content(value: String) -> Vec<u8> {
    let mut content = value.trim_end_matches(['\r', '\n']).as_bytes().to_vec();
    content.push(b'\n');
    content
}

/// Generation has no published effects. Bound observation of a temporarily
/// unavailable entropy/crypto provider before ending this request unknown.
fn retry_generated<T>(
    mut generate: impl FnMut() -> Result<T, SetupError>,
) -> Result<T, SetupError> {
    for attempt in 0..3 {
        if let Ok(value) = generate() {
            return Ok(value);
        }
        if attempt < 2 {
            std::thread::sleep(std::time::Duration::from_millis(50 << attempt));
        }
    }
    Err(publication::unknown())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;

    #[test]
    fn unavailable_generation_reobserves_then_ends_without_poisoning_fresh_preparation() {
        let attempts = Cell::new(0);
        let value = retry_generated(|| {
            attempts.set(attempts.get() + 1);
            if attempts.get() == 1 {
                Err(publication::unknown())
            } else {
                Ok(17)
            }
        })
        .unwrap();
        assert_eq!(value, 17);
        assert_eq!(attempts.get(), 2);
        attempts.set(0);
        assert!(
            retry_generated::<u8>(|| {
                attempts.set(attempts.get() + 1);
                Err(publication::unknown())
            })
            .is_err()
        );
        assert_eq!(attempts.get(), 3);
        assert_eq!(retry_generated(|| Ok(23)).unwrap(), 23);
    }
}
