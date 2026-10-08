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

#[derive(Deserialize, Serialize)]
pub(super) struct PrivateJwk {
    pub(super) alg: String,
    pub(super) crv: String,
    pub(super) d: String,
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
    let plaintext = signing_key
        .to_pkcs8_der()
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
    let encrypted_intermediate = encrypted_document
        .to_pem("ENCRYPTED PRIVATE KEY", LineEnding::LF)
        .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?
        .to_string();
    let (public_jwk, private_jwk) = generate_es256_jwks()?;
    let ca_config = render_ca_config(request, &public_jwk)?;
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
            files.provisioner_private_jwk.clone(),
            serde_json::to_string(&private_jwk)
                .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?,
        ),
        (
            files.provisioner_public_jwk.clone(),
            serde_json::to_string(&public_jwk)
                .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?,
        ),
        (files.ca_config.clone(), ca_config),
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
    // omits the Authority Key Identifier.  The controller reaches step-ca over
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

pub(super) fn generate_es256_jwks() -> Result<(PublicJwk, PrivateJwk), SetupError> {
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
    let private = PrivateJwk {
        alg: "ES256".to_owned(),
        crv: "P-256".to_owned(),
        d: Base64UrlUnpadded::encode_string(&secret.to_bytes()),
        kid,
        kty: "EC".to_owned(),
        key_use: "sig".to_owned(),
        x,
        y,
    };
    Ok((public, private))
}

/// The Step CA configuration this setup writes. Field order is the file's
/// key order, so an existing install re-renders byte-identically.
#[derive(Serialize)]
pub(super) struct StepCaConfig<'a> {
    pub(super) root: &'static str,
    pub(super) crt: &'static str,
    pub(super) key: &'static str,
    pub(super) address: &'static str,
    #[serde(rename = "insecureAddress")]
    pub(super) insecure_address: &'static str,
    #[serde(rename = "dnsNames")]
    pub(super) dns_names: [&'static str; 1],
    pub(super) logger: StepCaLogger,
    pub(super) db: StepCaDatabase,
    pub(super) crl: StepCaRevocationList,
    pub(super) authority: StepCaAuthority<'a>,
}

#[derive(Serialize)]
pub(super) struct StepCaLogger {
    pub(super) format: &'static str,
}

#[derive(Serialize)]
pub(super) struct StepCaDatabase {
    #[serde(rename = "type")]
    pub(super) kind: &'static str,
    #[serde(rename = "dataSource")]
    pub(super) data_source: &'static str,
}

#[derive(Serialize)]
pub(super) struct StepCaRevocationList {
    pub(super) enabled: bool,
    #[serde(rename = "generateOnRevoke")]
    pub(super) generate_on_revoke: bool,
    #[serde(rename = "cacheDuration")]
    pub(super) cache_duration: &'static str,
    #[serde(rename = "renewPeriod")]
    pub(super) renew_period: &'static str,
}

#[derive(Serialize)]
pub(super) struct StepCaAuthority<'a> {
    pub(super) provisioners: [StepCaProvisioner<'a>; 1],
}

#[derive(Serialize)]
pub(super) struct StepCaProvisioner<'a> {
    #[serde(rename = "type")]
    pub(super) kind: &'static str,
    pub(super) name: &'a str,
    pub(super) key: &'a PublicJwk,
    pub(super) claims: StepCaClaims,
    pub(super) options: StepCaOptions,
}

#[derive(Serialize)]
pub(super) struct StepCaClaims {
    #[serde(rename = "minTLSCertDuration")]
    pub(super) min_tls_cert_duration: &'static str,
    #[serde(rename = "maxTLSCertDuration")]
    pub(super) max_tls_cert_duration: &'static str,
    #[serde(rename = "defaultTLSCertDuration")]
    pub(super) default_tls_cert_duration: &'static str,
    #[serde(rename = "disableRenewal")]
    pub(super) disable_renewal: bool,
    #[serde(rename = "disableSmallstepExtensions")]
    pub(super) disable_smallstep_extensions: bool,
}

#[derive(Serialize)]
pub(super) struct StepCaOptions {
    pub(super) x509: StepCaX509Options,
}

#[derive(Serialize)]
pub(super) struct StepCaX509Options {
    pub(super) template: &'static str,
}

/// The parts of an existing Step CA configuration that must still describe the
/// imported authority. Anything else in the file is Step CA's own business.
#[derive(Deserialize)]
pub(super) struct StepCaConfigIdentity {
    pub(super) root: Option<String>,
    pub(super) crt: Option<String>,
    pub(super) key: Option<String>,
    pub(super) authority: Option<StepCaAuthorityIdentity>,
}

#[derive(Deserialize)]
pub(super) struct StepCaAuthorityIdentity {
    #[serde(default)]
    pub(super) provisioners: Vec<StepCaProvisionerIdentity>,
}

#[derive(Deserialize)]
pub(super) struct StepCaProvisionerIdentity {
    pub(super) name: Option<String>,
    pub(super) key: Option<PublicJwk>,
}

pub(super) const STEP_CA_ROOT: &str = "/run/vonk-normalized-secrets/step-ca/root-certificate";
pub(super) const STEP_CA_CRT: &str =
    "/run/vonk-normalized-secrets/step-ca/intermediate-certificate";
pub(super) const STEP_CA_KEY: &str = "/run/vonk-normalized-secrets/step-ca/intermediate-key";

pub(super) fn render_ca_config(
    request: &StepCaControllerRequest,
    public_jwk: &PublicJwk,
) -> Result<String, SetupError> {
    let document = StepCaConfig {
        root: STEP_CA_ROOT,
        crt: STEP_CA_CRT,
        key: STEP_CA_KEY,
        address: ":9000",
        insecure_address: "",
        dns_names: ["step-ca"],
        logger: StepCaLogger { format: "json" },
        db: StepCaDatabase {
            kind: "badgerv2",
            data_source: "/home/step/db",
        },
        crl: StepCaRevocationList {
            enabled: true,
            generate_on_revoke: true,
            cache_duration: "1h",
            renew_period: "30m",
        },
        authority: StepCaAuthority {
            provisioners: [StepCaProvisioner {
                kind: "JWK",
                name: &request.provisioner_name,
                key: public_jwk,
                claims: StepCaClaims {
                    min_tls_cert_duration: "720h",
                    max_tls_cert_duration: "720h",
                    default_tls_cert_duration: "720h",
                    disable_renewal: true,
                    disable_smallstep_extensions: true,
                },
                options: StepCaOptions {
                    x509: StepCaX509Options {
                        template: "{\"subject\":{\"commonName\":{{ toJson .Subject.CommonName }}},\"sans\":{{ toJson .SANs }},\"keyUsage\":[\"digitalSignature\"],\"extKeyUsage\":[\"clientAuth\"]}",
                    },
                },
            }],
        },
    };
    serde_json::to_string_pretty(&document)
        .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))
}
