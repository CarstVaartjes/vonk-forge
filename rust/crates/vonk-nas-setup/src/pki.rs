//! Pki.

use super::*;

#[derive(Clone, Deserialize, PartialEq, Serialize)]
pub(super) struct PublicJwk {
    pub(super) alg: String,
    pub(super) crv: String,
    pub(super) kid: String,
    pub(super) kty: String,
    #[serde(rename = "use")]
    pub(super) key_use: String,
    pub(super) x: String,
    pub(super) y: String,
}

pub(super) fn generate_pki<G: SecretGenerator>(
    request: &StepCaControllerRequest,
    environment: &[(String, String)],
    generator: &G,
) -> Result<Vec<(String, String)>, SetupError> {
    let hostnames = pki_hostnames(request, environment)?;
    let password = generator.generate(request.password_bytes as usize)?;
    validate_single_line_secret(&password, &request.files.password)?;

    let root_key = KeyPair::generate_for(&PKCS_ED25519)
        .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?;
    let root_params = ca_certificate_params("Vonk Forge Root CA", 1, 3650)?;
    let root = CertifiedIssuer::self_signed(root_params, root_key)
        .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?;

    let intermediate_key = KeyPair::generate_for(&PKCS_ED25519)
        .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?;
    let intermediate_der = intermediate_key.serialize_der();
    let intermediate_params = ca_certificate_params("Vonk Forge Agent Intermediate CA", 0, 1825)?;
    let intermediate = CertifiedIssuer::signed_by(intermediate_params, intermediate_key, &root)
        .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?;

    let controller_key = KeyPair::generate_for(&PKCS_ED25519)
        .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?;
    let controller_params =
        controller_certificate_params(&hostnames, time::OffsetDateTime::now_utc())?;
    let controller_certificate = controller_params
        .signed_by(&controller_key, &intermediate)
        .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?;

    let signing_key = ed25519_dalek::SigningKey::from_pkcs8_der(&intermediate_der)
        .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?;
    let encrypted_intermediate = encrypt_intermediate_key(&signing_key, &password)?;
    let public_jwk = generate_es256_jwk()?;
    let root_pem = root.pem();
    let intermediate_pem = intermediate.pem();
    let controller_chain = format!("{}{}", controller_certificate.pem(), intermediate_pem);
    let files = &request.files;

    Ok(vec![
        (files.root_certificate.clone(), root_pem.clone()),
        (files.intermediate_certificate.clone(), intermediate_pem),
        (
            files.intermediate_private_key.clone(),
            encrypted_intermediate,
        ),
        (
            files.controller_server_certificate.clone(),
            controller_chain,
        ),
        (
            files.controller_server_private_key.clone(),
            controller_key.serialize_pem(),
        ),
        (
            files.provisioner_public_jwk.clone(),
            serde_json::to_string(&public_jwk)
                .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?,
        ),
        (files.password.clone(), password),
    ])
}

pub(super) fn controller_certificate_params(
    hostnames: &[String],
    now: time::OffsetDateTime,
) -> Result<CertificateParams, SetupError> {
    let mut params = CertificateParams::new(hostnames.to_vec()).map_err(|error| {
        SetupError::InvalidPayload(format!("invalid controller hostname: {error}"))
    })?;
    params.not_before = now - time::Duration::hours(1);
    params.not_after = now + time::Duration::days(CONTROLLER_CERTIFICATE_VALIDITY_DAYS);
    params
        .distinguished_name
        .push(DnType::CommonName, hostnames[0].clone());
    params.key_usages.push(KeyUsagePurpose::DigitalSignature);
    params
        .extended_key_usages
        .push(ExtendedKeyUsagePurpose::ServerAuth);
    params.use_authority_key_identifier_extension = true;
    Ok(params)
}

pub(super) fn ca_certificate_params(
    common_name: &str,
    path_length: u8,
    validity_days: i64,
) -> Result<CertificateParams, SetupError> {
    let mut params = CertificateParams::new(Vec::<String>::new())
        .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?;
    let now = time::OffsetDateTime::now_utc();
    params.not_before = now - time::Duration::hours(1);
    params.not_after = now + time::Duration::days(validity_days);
    params
        .distinguished_name
        .push(DnType::CommonName, common_name);
    params.is_ca = IsCa::Ca(BasicConstraints::Constrained(path_length));
    params.key_usages = vec![
        KeyUsagePurpose::DigitalSignature,
        KeyUsagePurpose::KeyCertSign,
        KeyUsagePurpose::CrlSign,
    ];
    // Strict RFC 5280 verification (OpenSSL's X509_V_FLAG_X509_STRICT, enabled
    // by default in Python 3.13+) rejects any non-self-signed certificate that
    // omits the Authority Key Identifier.  The agent reaches the Controller over
    // TLS with that verifier and only the root as its trust anchor, so a
    // generated intermediate without the extension makes every enrollment
    // fail closed.  The controller leaf already sets this flag.
    params.use_authority_key_identifier_extension = true;
    Ok(params)
}

/// The controller certificate names: the control hostname and the fixed
/// enrollment, agent, and registry names derived from it.
pub(super) const DERIVED_HOSTNAME_PREFIXES: [&str; 3] = ["enroll", "agents", "registry"];

pub(super) fn pki_hostnames(
    request: &StepCaControllerRequest,
    environment: &[(String, String)],
) -> Result<Vec<String>, SetupError> {
    let control = environment_value(environment, &request.hostname_env)
        .filter(|value| valid_hostname(value))
        .ok_or_else(|| {
            SetupError::InvalidPayload(format!(
                "Step CA hostname value for {} is invalid",
                request.hostname_env
            ))
        })?
        .to_ascii_lowercase();
    let mut hostnames = vec![control.clone()];
    hostnames.extend(
        DERIVED_HOSTNAME_PREFIXES
            .iter()
            .map(|prefix| format!("{prefix}.{control}")),
    );
    Ok(hostnames)
}

pub(super) fn generate_es256_jwk() -> Result<PublicJwk, SetupError> {
    // Rejection sampling: a uniformly random 32-byte string is a valid P-256
    // scalar except with negligible probability.
    let secret = loop {
        if let Ok(secret) = p256::SecretKey::from_slice(&random_bytes::<32>()?) {
            break secret;
        }
    };
    let point = secret.public_key().to_sec1_point(false);
    let x = Base64UrlUnpadded::encode_string(
        point
            .x()
            .ok_or_else(|| SetupError::InvalidSecretMaterial("P-256 x is missing".to_owned()))?,
    );
    let y = Base64UrlUnpadded::encode_string(
        point
            .y()
            .ok_or_else(|| SetupError::InvalidSecretMaterial("P-256 y is missing".to_owned()))?,
    );
    let canonical = format!("{{\"crv\":\"P-256\",\"kty\":\"EC\",\"x\":\"{x}\",\"y\":\"{y}\"}}");
    let kid = Base64UrlUnpadded::encode_string(&Sha256::digest(canonical.as_bytes()));
    let public = PublicJwk {
        alg: "ES256".to_owned(),
        crv: "P-256".to_owned(),
        kid: kid.clone(),
        kty: "EC".to_owned(),
        key_use: "sig".to_owned(),
        x: x.clone(),
        y: y.clone(),
    };
    Ok(public)
}

pub(super) fn encrypt_intermediate_key(
    signing_key: &ed25519_dalek::SigningKey,
    password: &str,
) -> Result<String, SetupError> {
    // Encrypt the same version-0 encoding used by the Controller's other
    // signing keys; its cryptography loader rejects the optional public key.
    let canonical = canonical_ed25519_pkcs8_pem(signing_key);
    let (_, plaintext) = pkcs8::SecretDocument::from_pem(&canonical)
        .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?;
    let private_key_info = pkcs8::PrivateKeyInfoRef::try_from(plaintext.as_bytes())
        .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?;
    let salt: [u8; 16] = random_bytes()?;
    let encryption = pkcs8::pkcs5::pbes2::Parameters::generate_pbkdf2_sha256_aes256cbc(
        600_000,
        &salt,
        random_bytes()?,
    )
    .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?;
    let encrypted_document = private_key_info
        .encrypt_with_params(encryption, password.as_bytes())
        .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?;
    encrypted_document
        .to_pem("ENCRYPTED PRIVATE KEY", LineEnding::LF)
        .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))
        .map(|pem| pem.to_string())
}

/// Normalize using the same library decoder and encoder as fresh installation.
/// The caller validates the complete PKI group before publishing any replacement.
pub(super) fn normalize_intermediate_key(
    request: &StepCaControllerRequest,
    files: &mut [(String, String)],
) -> Result<(), SetupError> {
    let paths = &request.files;
    let password = files
        .iter()
        .find(|(path, _)| path == &paths.password)
        .ok_or_else(|| invalid_pki("intermediate password is missing"))?
        .1
        .trim_end_matches(['\r', '\n'])
        .to_owned();
    let (_, pem) = files
        .iter_mut()
        .find(|(path, _)| path == &paths.intermediate_private_key)
        .ok_or_else(|| invalid_pki("intermediate private key is missing"))?;
    let (_, document) = pkcs8::SecretDocument::from_pem(pem)
        .map_err(|_| invalid_pki("intermediate private key cannot be decrypted"))?;
    let encrypted = pkcs8::EncryptedPrivateKeyInfoRef::try_from(document.as_bytes())
        .map_err(|_| invalid_pki("intermediate private key cannot be decrypted"))?;
    let plaintext = encrypted
        .decrypt(password.as_bytes())
        .map_err(|_| invalid_pki("intermediate private key cannot be decrypted"))?;
    let key = ed25519_dalek::SigningKey::from_pkcs8_der(plaintext.as_bytes())
        .map_err(|_| invalid_pki("intermediate private key is not Ed25519"))?;
    let canonical = canonical_ed25519_pkcs8_pem(&key);
    let (_, canonical_document) = pkcs8::SecretDocument::from_pem(&canonical)
        .map_err(|_| invalid_pki("intermediate private key cannot be represented"))?;
    if plaintext.as_bytes() != canonical_document.as_bytes() {
        *pem = encrypt_intermediate_key(&key, &password)?;
    }
    Ok(())
}
