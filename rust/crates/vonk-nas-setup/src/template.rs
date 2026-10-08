//! Template.

use super::*;

/// The generated secrets a payload asks for; a payload that names none asks for none.
pub(super) fn generated_secrets(payload: &CanonicalTemplatePayload) -> &GeneratedSecrets {
    static NONE: GeneratedSecrets = GeneratedSecrets {
        random_text: Vec::new(),
        ed25519_pkcs8_pem: Vec::new(),
        postgres_urls: Vec::new(),
    };
    payload.generated_secrets.as_ref().unwrap_or(&NONE)
}

pub(super) fn step_ca_files(files: &StepCaControllerFiles) -> [&str; 9] {
    [
        &files.root_certificate,
        &files.intermediate_certificate,
        &files.intermediate_private_key,
        &files.controller_server_certificate,
        &files.controller_server_private_key,
        &files.provisioner_private_jwk,
        &files.provisioner_public_jwk,
        &files.ca_config,
        &files.password,
    ]
}

/// Parse and validate the canonical NAS template payload.
pub fn parse_template_payload(raw: &[u8]) -> Result<CanonicalTemplatePayload, SetupError> {
    let payload: CanonicalTemplatePayload = serde_json::from_slice(raw)
        .map_err(|error| SetupError::InvalidPayload(error.to_string()))?;
    payload.validate()?;
    Ok(payload)
}

pub(super) trait TemplateValidation {
    fn validate(&self) -> Result<(), SetupError>;
}

impl TemplateValidation for CanonicalTemplatePayload {
    fn validate(&self) -> Result<(), SetupError> {
        if self.schema_version != 2 {
            return Err(SetupError::InvalidPayload(
                "unsupported schema version".to_owned(),
            ));
        }
        if self.docker_compose_yaml.is_empty() || self.docker_compose_yaml.contains('\0') {
            return Err(SetupError::InvalidPayload(
                "docker compose payload is empty or malformed".to_owned(),
            ));
        }
        if self.preflight.len() > 32
            || self.preflight.iter().any(|item| {
                item.trim().is_empty() || item.len() > 512 || item.contains(['\0', '\r', '\n'])
            })
        {
            return Err(SetupError::InvalidPayload(
                "preflight checklist is malformed".to_owned(),
            ));
        }

        if let Some(group) = &self.group_readable_secrets {
            let mut seen = HashSet::new();
            if group.gid == 0 || group.files.is_empty() {
                return Err(SetupError::InvalidPayload(
                    "secret group needs a non-root gid and files".to_owned(),
                ));
            }
            for file in &group.files {
                validate_secret_name(file)?;
                if !seen.insert(file.as_str()) {
                    return Err(SetupError::InvalidPayload(format!(
                        "duplicate group-readable secret {file}"
                    )));
                }
            }
        }

        let mut environment = HashSet::new();
        let mut secrets = HashSet::new();
        for value in &self.internal_values {
            validate_env_name(&value.env)?;
            if value.value.contains('\0') {
                return Err(SetupError::InvalidPayload(format!(
                    "internal value for {} contains NUL",
                    value.env
                )));
            }
            if !environment.insert(value.env.as_str()) {
                return Err(SetupError::InvalidPayload(format!(
                    "duplicate environment key {}",
                    value.env
                )));
            }
        }
        validate_prompts(
            &self.required_values,
            &self.secrets,
            &mut environment,
            &mut secrets,
        )?;
        for name in &self.optional_values {
            validate_env_name(name)?;
        }
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
            if hermes.enabled_value == hermes.disabled_value
                || hermes.enabled_value.contains(['\0', '\r', '\n'])
                || hermes.disabled_value.contains(['\0', '\r', '\n'])
            {
                return Err(SetupError::InvalidPayload(
                    "Hermes enabled and disabled values must be distinct single-line values"
                        .to_owned(),
                ));
            }
        }
        if let Some(modes) = &self.install_modes {
            if modes.prompt.trim().is_empty()
                || modes.lab_value.trim().is_empty()
                || modes.secure_remote_value.trim().is_empty()
                || modes.lab_value == modes.secure_remote_value
                || ![modes.lab_value.as_str(), modes.secure_remote_value.as_str()]
                    .contains(&modes.default.as_str())
            {
                return Err(SetupError::InvalidPayload(
                    "install mode choices are invalid".to_owned(),
                ));
            }
            let mut lab_environment = HashSet::new();
            for value in &modes.lab_values {
                validate_env_name(&value.env)?;
                if !lab_environment.insert(&value.env) {
                    return Err(SetupError::InvalidPayload(format!(
                        "duplicate lab environment value {}",
                        value.env
                    )));
                }
            }
        }
        for request in &generated_secrets(self).random_text {
            validate_generated_file(&request.file, &mut secrets)?;
            if !(16..=128).contains(&request.bytes) {
                return Err(SetupError::InvalidPayload(format!(
                    "generation size for {} is outside 16..=128 bytes",
                    request.file
                )));
            }
            if request.prefix.as_deref().is_some_and(|prefix| {
                prefix.is_empty()
                    || prefix.len() > 32
                    || !prefix.chars().all(|character| {
                        character.is_ascii_alphanumeric() || "_.~-".contains(character)
                    })
            }) {
                return Err(SetupError::InvalidPayload(format!(
                    "generation prefix for {} is invalid",
                    request.file
                )));
            }
        }
        for request in &generated_secrets(self).ed25519_pkcs8_pem {
            validate_generated_file(&request.file, &mut secrets)?;
        }
        for request in &generated_secrets(self).postgres_urls {
            validate_generated_file(&request.file, &mut secrets)?;
            validate_secret_name(&request.password_file)?;
            if !secrets.contains(request.password_file.as_str()) {
                return Err(SetupError::InvalidPayload(format!(
                    "PostgreSQL URL {} references undeclared password file {}",
                    request.file, request.password_file
                )));
            }
            validate_postgres_url_request(request)?;
        }
        if let Some(request) = &self.step_ca_controller {
            if request.provisioner_name.trim().is_empty()
                || request.provisioner_name.contains(['\0', '\r', '\n'])
                || !(16..=128).contains(&request.password_bytes)
            {
                return Err(SetupError::InvalidPayload(
                    "Step CA/controller request is incomplete".to_owned(),
                ));
            }
            validate_env_name(&request.hostname_env)?;
            if !environment.contains(request.hostname_env.as_str()) {
                return Err(SetupError::InvalidPayload(format!(
                    "Step CA hostname environment key {} is undeclared",
                    request.hostname_env
                )));
            }
            for file in step_ca_files(&request.files) {
                validate_generated_file(file, &mut secrets)?;
            }
        }
        Ok(())
    }
}

pub(super) fn validate_generated_file<'a>(
    file: &'a str,
    secrets: &mut HashSet<&'a str>,
) -> Result<(), SetupError> {
    validate_secret_name(file)?;
    if !secrets.insert(file) {
        return Err(SetupError::InvalidPayload(format!(
            "duplicate secret file {file}"
        )));
    }
    Ok(())
}

pub(super) fn validate_postgres_url_request(
    request: &PostgresUrlRequest,
) -> Result<(), SetupError> {
    let valid_scheme = request
        .scheme
        .chars()
        .enumerate()
        .all(|(index, character)| {
            if index == 0 {
                character.is_ascii_lowercase()
            } else {
                character.is_ascii_lowercase()
                    || character.is_ascii_digit()
                    || matches!(character, '+' | '-' | '.')
            }
        });
    if !valid_scheme
        || request.username.is_empty()
        || request.host.is_empty()
        || request.port == 0
        || request.database.is_empty()
        || [&request.username, &request.host, &request.database]
            .iter()
            .any(|value| value.contains(['\0', '\r', '\n']))
    {
        return Err(SetupError::InvalidPayload(format!(
            "PostgreSQL URL request for {} is invalid",
            request.file
        )));
    }
    Ok(())
}

pub(super) fn validate_prompts<'a>(
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
        if value
            .default
            .as_deref()
            .is_some_and(|default| !valid_required_value(default, &value.validation))
        {
            return Err(SetupError::InvalidPayload(format!(
                "default for {} does not satisfy its validator",
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

pub(super) fn validate_env_name(name: &str) -> Result<(), SetupError> {
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

pub(super) fn valid_required_value(value: &str, validation: &RequiredValueValidation) -> bool {
    if value.contains(['\0', '\r', '\n']) {
        return false;
    }
    match validation {
        RequiredValueValidation::NonEmpty => !value.is_empty(),
        RequiredValueValidation::Ipv4 => value.parse::<Ipv4Addr>().is_ok(),
        RequiredValueValidation::CidrList => valid_cidr_list(value, false),
        RequiredValueValidation::OptionalCidrList => valid_cidr_list(value, true),
        RequiredValueValidation::Hostname => valid_hostname(value),
    }
}

pub(super) fn valid_cidr_list(value: &str, allow_empty: bool) -> bool {
    if value.is_empty() {
        return allow_empty;
    }
    value.split(',').all(|item| {
        let item = item.trim();
        let Some((address, prefix)) = item.split_once('/') else {
            return false;
        };
        if address.is_empty() || prefix.is_empty() || prefix.contains('/') {
            return false;
        }
        let Ok(address) = address.parse::<IpAddr>() else {
            return false;
        };
        let Ok(prefix) = prefix.parse::<u8>() else {
            return false;
        };
        prefix <= if address.is_ipv4() { 32 } else { 128 }
    })
}

pub(super) fn valid_hostname(value: &str) -> bool {
    if value.is_empty() || value.len() > 253 || value.starts_with('.') || value.ends_with('.') {
        return false;
    }
    value.split('.').all(|label| {
        !label.is_empty()
            && label.len() <= 63
            && !label.starts_with('-')
            && !label.ends_with('-')
            && label
                .chars()
                .all(|character| character.is_ascii_alphanumeric() || character == '-')
    })
}

pub(super) fn validate_secret_name(name: &str) -> Result<(), SetupError> {
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

pub(super) fn is_safe_secret_component(component: &str) -> bool {
    !component.is_empty()
        && component != "."
        && component != ".."
        && component.chars().all(|character| {
            character.is_ascii_lowercase()
                || character.is_ascii_digit()
                || matches!(character, '-' | '_' | '.')
        })
}
