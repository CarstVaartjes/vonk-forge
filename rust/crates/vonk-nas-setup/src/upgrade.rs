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
    for attempt in 0..3 {
        match validate_existing_bundle(bundle, payload) {
            Ok(()) => break,
            Err(error @ SetupError::PermissionDenied { .. }) => return Err(error),
            Err(_) if attempt < 2 => {
                std::thread::sleep(std::time::Duration::from_millis(50 << attempt))
            }
            Err(_) => return Err(publication::unknown()),
        }
    }
    // Independent signed kit configuration converges even when credential
    // custody is unavailable. Never traverse a damaged Compose target.
    atomic_replace(
        &bundle.join("docker-compose.yaml"),
        payload.docker_compose_yaml.as_bytes(),
        0o644,
    )?;
    let mut last_error = publication::unknown();
    for attempt in 0..3 {
        match upgrade_credentials(payload, bundle, requested_hermes_enabled, prompt, generator) {
            Ok(outcome) => return Ok(outcome),
            Err(error @ (SetupError::PermissionDenied { .. } | SetupError::InputEnded)) => {
                return Err(error);
            }
            Err(error) => last_error = error,
        }
        if attempt < 2 {
            std::thread::sleep(std::time::Duration::from_millis(50 << attempt));
        }
    }
    Err(last_error)
}

fn upgrade_credentials<R: BufRead, W: Write, S: SecretInput<R, W>, G: SecretGenerator>(
    payload: &CanonicalTemplatePayload,
    bundle: &Path,
    requested_hermes_enabled: Option<bool>,
    prompt: &mut PromptIo<R, W, S>,
    generator: &G,
) -> Result<SetupOutcome, SetupError> {
    for directory in BUNDLE_DIRECTORIES {
        ensure_secure_directory(&bundle.join(directory))?;
    }
    let secret_root = bundle.join("secrets");
    require_secret_root(&secret_root)?;
    publication::replay(payload, bundle)?;
    let staging_owner = tempfile::Builder::new()
        .prefix(".vonk-forge.setup-")
        .tempdir_in(bundle)?;
    let staging = staging_owner.path();
    let candidate = staging.join("secrets");
    publication::copy_candidate(payload, bundle, &candidate)?;
    let gateway_directory = secret_root.join(GATEWAY_SECRET_DIRECTORY);
    if fs::symlink_metadata(&gateway_directory).is_err() {
        create_secure_directory(&gateway_directory)?;
    }
    let mut environment = parse_environment(&bundle.join(".env"))?;
    // Retained publication inputs repair missing/ambiguous generated config;
    // intact current operator values always take precedence.
    let retained_environment = parse_environment(&bundle.join(".vonk-verified-secrets/.env"))?;
    for (name, value) in &retained_environment {
        if environment_value(&environment, name).is_none() {
            environment.push((name.clone(), value.clone()));
        }
    }
    for required in &payload.required_values {
        if environment_value(&environment, &required.env)
            .is_some_and(|value| !valid_required_value(value, &required.validation))
        {
            environment.retain(|(name, _)| name != &required.env);
            if let Some(value) = environment_value(&retained_environment, &required.env)
                .filter(|value| valid_required_value(value, &required.validation))
            {
                environment.push((required.env.clone(), value.to_owned()));
            }
        }
    }
    for internal in &payload.internal_values {
        set_environment_value(&mut environment, &internal.env, internal.value.clone());
    }
    if payload.install_modes.is_some()
        && environment_value(&environment, "COMPOSE_PROFILES").is_none()
    {
        return Err(publication::unknown());
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
    for required in &payload.required_values {
        if environment_value(&environment, &required.env).is_none() {
            let value = match required_value_default(required, &environment) {
                Some(value) => value,
                None if publication::newly_requested_environment(bundle, &required.env) => prompt
                    .required(required)
                    .map_err(|_| publication::unknown())?,
                None => return Err(publication::unknown()),
            };
            environment.push((required.env.clone(), value));
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
    // Environment is independent generated configuration, never replacement
    // signing authority. Preserve its safe convergence if PKI is unavailable.
    atomic_replace(&bundle.join(".env"), environment_document.as_bytes(), 0o600)?;

    repair_stored_members(payload, bundle, &candidate)?;
    let mut new_secrets = Vec::new();
    for secret in &payload.secrets {
        if secret_file_exists(&candidate, &secret.file)? {
            continue;
        }
        let value = if secret.optional || (lab_mode && secret.secure_remote_only) {
            String::new()
        } else if publication::newly_requested(payload, bundle, &secret.file) {
            prompt.secret(secret).map_err(|_| publication::unknown())?
        } else {
            return Err(publication::unknown());
        };
        new_secrets.push((secret.file.clone(), value));
    }
    // An absent member of an existing authority is repaired from retained
    // verified bytes above. Never fabricate replacement database/signing keys.
    let generated = generated_secrets(payload);
    for path in generated
        .random_text
        .iter()
        .map(|value| value.file.as_str())
        .chain(
            generated
                .ed25519_pkcs8_pem
                .iter()
                .map(|value| value.file.as_str()),
        )
    {
        if !secret_file_exists(&candidate, path)?
            && !publication::newly_requested(payload, bundle, path)
        {
            return Err(publication::unknown());
        }
    }
    generate_missing_secrets(payload, Some(&candidate), &mut new_secrets, generator)?;
    // Database URLs are generated projections, not separate credentials.
    for request in &generated.postgres_urls {
        let password = install::secret_value(&new_secrets, &request.password_file)
            .map(str::to_owned)
            .or_else(|_| read_existing_secret(&candidate, &request.password_file))?;
        let url = install::render_postgres_url(request, &password)?;
        if let Some((_, value)) = new_secrets
            .iter_mut()
            .find(|(path, _)| path == &request.file)
        {
            *value = url;
        } else {
            new_secrets.push((request.file.clone(), url));
        }
    }
    for request in &generated_secrets(payload).ed25519_pkcs8_pem {
        if secret_file_exists(&candidate, &request.file)? {
            validate_ed25519_private_key(
                &read_existing_secret(&candidate, &request.file)?,
                &request.file,
            )?;
        }
    }

    let mut controller_leaf_replacement = None;
    if let Some(request) = &payload.step_ca_controller {
        let existing = step_ca_files(&request.files)
            .into_iter()
            .map(|file| secret_file_exists(&candidate, file))
            .collect::<Result<Vec<_>, _>>()?;
        match existing.into_iter().filter(|present| *present).count() {
            0 => return Err(publication::unknown()),
            count if count == step_ca_files(&request.files).len() => {
                let files = step_ca_files(&request.files)
                    .into_iter()
                    .map(|file| {
                        read_existing_secret(&candidate, file)
                            .map(|content| (file.to_owned(), content))
                    })
                    .collect::<Result<Vec<_>, _>>()?;
                let validated = validate_upgrade_pki_material(request, &environment, &files)?;
                if validated.controller_needs_renewal {
                    controller_leaf_replacement =
                        Some(renew_controller_leaf(request, &environment, &files)?);
                }
                new_secrets.extend(files);
            }
            _ => {
                return Err(publication::unknown());
            }
        }
    }

    for (name, value) in new_secrets {
        let content = if value.is_empty() {
            Vec::new()
        } else {
            secret_file_content(value)
        };
        filesystem::ensure_nested_parent(&candidate, Path::new(&name), &name)?;
        atomic_replace(&candidate.join(name), &content, 0o600)?;
    }
    if let Some(replacement) = controller_leaf_replacement {
        for (path, content) in [
            (replacement.certificate_path, replacement.certificate),
            (replacement.private_key_path, replacement.private_key),
        ] {
            atomic_replace(&candidate.join(path), &secret_file_content(content), 0o600)?;
        }
    }
    write_new_file(
        &staging.join(".env"),
        environment_document.as_bytes(),
        0o600,
    )?;
    publication::publish(payload, bundle, staging)?;
    apply_secret_group(payload, &secret_root, prompt)?;
    // Obsolete copies are never consumed. Cleanup is bounded and retried by
    // each subsequent upgrade; its outcome cannot undo successful publication.
    let mut cleanup_complete = false;
    for attempt in 0..3 {
        if remove_retired_runtime_configs(&secret_root).is_ok() {
            cleanup_complete = true;
            break;
        }
        if attempt < 2 {
            std::thread::sleep(std::time::Duration::from_millis(50));
        }
    }
    if !cleanup_complete {
        eprintln!(
            "{}",
            vonk_agent_protocol::generated::WaitReason::CleanupUnconfirmed
        );
    }
    Ok(SetupOutcome {
        root: bundle.to_path_buf(),
        hermes_enabled,
        dropped_environment: dropped,
    })
}

fn require_secret_root(root: &Path) -> Result<(), SetupError> {
    // A missing directory can be rehydrated, but a foreign object is retained.
    match fs::symlink_metadata(root) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => create_secure_directory(root),
        Ok(metadata) if metadata.is_dir() && !metadata.file_type().is_symlink() => Ok(()),
        Ok(_) => {
            let retired = create_staging_directory(root.parent().expect("secret root has parent"))?;
            fs::rename(root, retired.join("preserved"))?;
            create_secure_directory(root)
        }
        Err(error) => Err(error.into()),
    }
}

/// Stored malformed projections are repairable from the last complete group.
/// Well-formed cryptographic mismatches still reach signature/key validation.
fn repair_stored_members(
    payload: &CanonicalTemplatePayload,
    bundle: &Path,
    candidate: &Path,
) -> Result<(), SetupError> {
    let retained = bundle.join(".vonk-verified-secrets");
    let mut damaged = Vec::new();
    for request in &generated_secrets(payload).ed25519_pkcs8_pem {
        if publication::read_member(candidate, &request.file)
            .is_some_and(|value| validate_ed25519_private_key(&value, &request.file).is_err())
        {
            damaged.push(request.file.as_str());
        }
    }
    if let Some(request) = &payload.step_ca_controller {
        let paths = &request.files;
        for path in [
            &paths.root_certificate,
            &paths.intermediate_certificate,
            &paths.controller_server_certificate,
        ] {
            if publication::read_member(candidate, path).is_some_and(|value| {
                match x509_parser::pem::parse_x509_pem(value.as_bytes()) {
                    Ok((_, pem)) => x509_parser::parse_x509_certificate(&pem.contents).is_err(),
                    Err(_) => true,
                }
            }) {
                damaged.push(path.as_str());
            }
        }
        if publication::read_member(candidate, &paths.controller_server_private_key)
            .is_some_and(|value| KeyPair::from_pem(&value).is_err())
        {
            damaged.push(paths.controller_server_private_key.as_str());
        }
        for (path, invalid) in [(
            &paths.provisioner_public_jwk,
            publication::read_member(candidate, &paths.provisioner_public_jwk)
                .is_some_and(|value| serde_json::from_str::<PublicJwk>(&value).is_err()),
        )] {
            if invalid {
                damaged.push(path.as_str());
            }
        }
        for path in damaged.drain(..) {
            let value =
                publication::read_member(&retained, path).ok_or_else(publication::unknown)?;
            atomic_replace(&candidate.join(path), value.as_bytes(), 0o600)?;
        }
        let password = publication::read_member(candidate, &paths.password)
            .ok_or_else(publication::unknown)?;
        let encrypted = publication::read_member(candidate, &paths.intermediate_private_key)
            .ok_or_else(publication::unknown)?;
        if ed25519_dalek::SigningKey::from_pkcs8_encrypted_pem(
            &encrypted,
            password.trim_end_matches(['\r', '\n']).as_bytes(),
        )
        .is_err()
        {
            let password = publication::read_member(&retained, &paths.password)
                .ok_or_else(publication::unknown)?;
            let encrypted = publication::read_member(&retained, &paths.intermediate_private_key)
                .ok_or_else(publication::unknown)?;
            let key = ed25519_dalek::SigningKey::from_pkcs8_encrypted_pem(
                &encrypted,
                password.trim_end_matches(['\r', '\n']).as_bytes(),
            )
            .map_err(|_| publication::unknown())?;
            let pem = publication::read_member(candidate, &paths.intermediate_certificate)
                .ok_or_else(publication::unknown)?;
            let (_, block) = x509_parser::pem::parse_x509_pem(pem.as_bytes())
                .map_err(|_| publication::unknown())?;
            let (_, certificate) = x509_parser::parse_x509_certificate(&block.contents)
                .map_err(|_| publication::unknown())?;
            if key.verifying_key().as_bytes()
                != certificate.public_key().subject_public_key.data.as_ref()
            {
                return Err(publication::unknown());
            }
            // Content identity proves that this restores the same signer.
            atomic_replace(&candidate.join(&paths.password), password.as_bytes(), 0o600)?;
            atomic_replace(
                &candidate.join(&paths.intermediate_private_key),
                encrypted.as_bytes(),
                0o600,
            )?;
        }
    }
    for path in damaged {
        let value = publication::read_member(&retained, path).ok_or_else(publication::unknown)?;
        atomic_replace(&candidate.join(path), value.as_bytes(), 0o600)?;
    }
    Ok(())
}
