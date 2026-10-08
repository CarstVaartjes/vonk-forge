//! Upgrade.

use super::*;

pub(super) struct ControllerLeafReplacement {
    pub(super) certificate_path: String,
    pub(super) certificate: String,
    pub(super) private_key_path: String,
    pub(super) private_key: String,
}

pub(super) fn renew_controller_leaf(
    request: &StepCaControllerRequest,
    environment: &[(String, String)],
    files: &[(String, String)],
) -> Result<ControllerLeafReplacement, SetupError> {
    let value = |name: &str| {
        files
            .iter()
            .find_map(|(path, value)| (path == name).then_some(value.as_str()))
            .ok_or_else(|| {
                SetupError::InvalidSecretMaterial(format!("PKI member {name} is missing"))
            })
    };
    let paths = &request.files;
    let password = value(&paths.password)?.trim_end_matches(['\r', '\n']);
    let intermediate_signing_key = ed25519_dalek::SigningKey::from_pkcs8_encrypted_pem(
        value(&paths.intermediate_private_key)?,
        password.as_bytes(),
    )
    .map_err(|_| invalid_pki("intermediate private key cannot be decrypted"))?;
    let intermediate_der = intermediate_signing_key
        .to_pkcs8_der()
        .map_err(|_| invalid_pki("intermediate private key cannot be represented"))?;
    let intermediate_key = KeyPair::try_from(intermediate_der.as_bytes())
        .map_err(|_| invalid_pki("intermediate private key cannot be represented"))?;
    let intermediate_pem = value(&paths.intermediate_certificate)?;
    let issuer = Issuer::from_ca_cert_pem(intermediate_pem, intermediate_key)
        .map_err(|_| invalid_pki("intermediate certificate cannot issue a controller leaf"))?;
    let hostnames = pki_hostnames(request, environment)?;
    let controller_key = KeyPair::generate_for(&PKCS_ED25519)
        .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?;
    let controller_params =
        controller_certificate_params(&hostnames, time::OffsetDateTime::now_utc())?;
    let controller_certificate = controller_params
        .signed_by(&controller_key, &issuer)
        .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?;
    let replacement = ControllerLeafReplacement {
        certificate_path: paths.controller_server_certificate.clone(),
        certificate: format!("{}{}", controller_certificate.pem(), intermediate_pem),
        private_key_path: paths.controller_server_private_key.clone(),
        private_key: controller_key.serialize_pem(),
    };

    let mut candidate = files.to_vec();
    for (path, content) in [
        (&replacement.certificate_path, &replacement.certificate),
        (&replacement.private_key_path, &replacement.private_key),
    ] {
        let (_, existing) = candidate
            .iter_mut()
            .find(|(candidate_path, _)| candidate_path == path)
            .ok_or_else(|| {
                SetupError::InvalidSecretMaterial(format!("PKI member {path} is missing"))
            })?;
        *existing = content.clone();
    }
    validate_pki_material(request, environment, &candidate)?;
    Ok(replacement)
}

pub(super) fn upgrade<R: BufRead, W: Write, S: SecretInput<R, W>, G: SecretGenerator>(
    payload: &CanonicalTemplatePayload,
    bundle: &Path,
    requested_hermes_enabled: Option<bool>,
    prompt: &mut PromptIo<R, W, S>,
    generator: &G,
) -> Result<SetupOutcome, SetupError> {
    validate_existing_bundle(bundle)?;
    for directory in BUNDLE_DIRECTORIES {
        ensure_secure_directory(&bundle.join(directory))?;
    }
    let secret_root = bundle.join("secrets");
    let gateway_directory = secret_root.join(GATEWAY_SECRET_DIRECTORY);
    if fs::symlink_metadata(&gateway_directory).is_err() {
        // Keep the mode of an existing directory: the Controller manages it.
        create_secure_directory(&gateway_directory)?;
    }
    let mut environment = parse_environment(&bundle.join(".env"))?;
    for internal in &payload.internal_values {
        set_environment_value(&mut environment, &internal.env, internal.value.clone());
    }
    let lab_mode = payload.install_modes.as_ref().is_some_and(|modes| {
        !environment_value(&environment, "COMPOSE_PROFILES")
            .is_some_and(|profiles| compose_profile_enabled(profiles, &modes.secure_remote_value))
    });
    if let Some(modes) = payload.install_modes.as_ref().filter(|_| lab_mode) {
        for value in &modes.lab_values {
            if environment_value(&environment, &value.env).is_none() {
                environment.push((value.env.clone(), value.value.clone()));
            }
        }
    }
    collect_required_values(&payload.required_values, &mut environment, prompt)?;

    let mut new_secrets = Vec::new();
    for secret in &payload.secrets {
        if secret_file_exists(&secret_root, &secret.file)? {
            continue;
        }
        let value = if secret.optional || (lab_mode && secret.secure_remote_only) {
            String::new()
        } else {
            prompt.secret(secret)?
        };
        new_secrets.push((secret.file.clone(), value));
    }
    generate_missing_secrets(payload, Some(&secret_root), &mut new_secrets, generator)?;
    for request in &generated_secrets(payload).ed25519_pkcs8_pem {
        if secret_file_exists(&secret_root, &request.file)? {
            validate_ed25519_private_key(
                &read_existing_secret(&secret_root, &request.file)?,
                &request.file,
            )?;
        }
    }

    let mut controller_leaf_replacement = None;
    if let Some(request) = &payload.step_ca_controller {
        let existing = step_ca_files(&request.files)
            .into_iter()
            .map(|file| secret_file_exists(&secret_root, file))
            .collect::<Result<Vec<_>, _>>()?;
        match existing.into_iter().filter(|present| *present).count() {
            0 => new_secrets.extend(generate_pki(request, &environment, generator)?),
            count if count == step_ca_files(&request.files).len() => {
                let files = step_ca_files(&request.files)
                    .into_iter()
                    .map(|file| {
                        read_existing_secret(&secret_root, file)
                            .map(|content| (file.to_owned(), content))
                    })
                    .collect::<Result<Vec<_>, _>>()?;
                let validated = validate_upgrade_pki_material(request, &environment, &files)?;
                if validated.controller_needs_renewal {
                    controller_leaf_replacement =
                        Some(renew_controller_leaf(request, &environment, &files)?);
                }
            }
            _ => {
                return Err(SetupError::InvalidSecretMaterial(
                    "partial Step CA/controller PKI group cannot be upgraded".to_owned(),
                ));
            }
        }
    }

    let hermes_enabled = if let Some(hermes) = &payload.hermes {
        // The Hermes switch shares its variable with other Compose profiles
        // (for example the secure-remote install mode), so it is one member
        // of a comma-separated list rather than the whole value.
        let current = environment_value(&environment, &hermes.env)
            .is_some_and(|value| compose_profile_enabled(value, &hermes.enabled_value));
        let enabled = requested_hermes_enabled.unwrap_or(current);
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

    let dropped = retain_known_environment(payload, &mut environment);
    let environment_document = render_owned_environment(&environment)?;
    for (name, value) in new_secrets {
        let content = if value.is_empty() {
            Vec::new()
        } else {
            secret_file_content(value)
        };
        write_secret_file(&secret_root, &name, &content)?;
    }
    apply_secret_group(payload, &secret_root, prompt)?;
    if let Some(replacement) = controller_leaf_replacement {
        atomic_replace_controller_leaf(&secret_root, replacement)?;
    }
    atomic_replace(&bundle.join(".env"), environment_document.as_bytes(), 0o600)?;
    atomic_replace(
        &bundle.join("docker-compose.yaml"),
        payload.docker_compose_yaml.as_bytes(),
        0o644,
    )?;
    remove_retired_runtime_configs(&secret_root)?;
    Ok(SetupOutcome {
        root: bundle.to_path_buf(),
        hermes_enabled,
        dropped_environment: dropped,
    })
}
