//! Pki validation.

use super::*;

pub(super) fn validate_pki_material(
    request: &StepCaControllerRequest,
    environment: &[(String, String)],
    files: &[(String, String)],
) -> Result<(), SetupError> {
    validate_pki_material_at(
        request,
        environment,
        files,
        time::OffsetDateTime::now_utc(),
        false,
    )?;
    Ok(())
}

pub(super) struct ValidatedPkiMaterial {
    pub(super) controller_needs_renewal: bool,
}

pub(super) fn validate_upgrade_pki_material(
    request: &StepCaControllerRequest,
    environment: &[(String, String)],
    files: &[(String, String)],
) -> Result<ValidatedPkiMaterial, SetupError> {
    validate_pki_material_at(
        request,
        environment,
        files,
        time::OffsetDateTime::now_utc(),
        true,
    )
}

pub(super) fn validate_pki_material_at(
    request: &StepCaControllerRequest,
    environment: &[(String, String)],
    files: &[(String, String)],
    now: time::OffsetDateTime,
    allow_expired_controller: bool,
) -> Result<ValidatedPkiMaterial, SetupError> {
    let value = |name: &str| {
        files
            .iter()
            .find_map(|(path, value)| (path == name).then_some(value.as_str()))
            .ok_or_else(|| {
                SetupError::InvalidSecretMaterial(format!("PKI member {name} is missing"))
            })
    };
    let paths = &request.files;
    let root_pem = value(&paths.root_certificate)?;
    let intermediate_pem = value(&paths.intermediate_certificate)?;
    let server_pem = value(&paths.controller_server_certificate)?;
    let (root_trailing, root_block) = x509_parser::pem::parse_x509_pem(root_pem.as_bytes())
        .map_err(|_| invalid_pki("root certificate is not valid PEM"))?;
    let (intermediate_trailing, intermediate_block) =
        x509_parser::pem::parse_x509_pem(intermediate_pem.as_bytes())
            .map_err(|_| invalid_pki("intermediate certificate is not valid PEM"))?;
    let (server_chain, server_block) = x509_parser::pem::parse_x509_pem(server_pem.as_bytes())
        .map_err(|_| invalid_pki("controller certificate is not valid PEM"))?;
    let (server_trailing, bundled_intermediate_block) =
        x509_parser::pem::parse_x509_pem(server_chain)
            .map_err(|_| invalid_pki("controller certificate chain has no intermediate"))?;
    if !root_trailing.iter().all(u8::is_ascii_whitespace)
        || !intermediate_trailing.iter().all(u8::is_ascii_whitespace)
        || !server_trailing.iter().all(u8::is_ascii_whitespace)
        || bundled_intermediate_block.contents != intermediate_block.contents
    {
        return Err(invalid_pki("certificate chain is inconsistent"));
    }
    let (_, root) = x509_parser::parse_x509_certificate(&root_block.contents)
        .map_err(|_| invalid_pki("root certificate is not valid X.509"))?;
    let (_, intermediate) = x509_parser::parse_x509_certificate(&intermediate_block.contents)
        .map_err(|_| invalid_pki("intermediate certificate is not valid X.509"))?;
    let (_, server) = x509_parser::parse_x509_certificate(&server_block.contents)
        .map_err(|_| invalid_pki("controller certificate is not valid X.509"))?;
    root.verify_signature(None)
        .map_err(|_| invalid_pki("root certificate is not self-signed"))?;
    intermediate
        .verify_signature(Some(root.public_key()))
        .map_err(|_| invalid_pki("intermediate certificate is not signed by the root"))?;
    server
        .verify_signature(Some(intermediate.public_key()))
        .map_err(|_| invalid_pki("controller certificate is not signed by the intermediate"))?;

    // Strict RFC 5280 verification (OpenSSL's X509_V_FLAG_X509_STRICT, enabled
    // by default in Python 3.13+) rejects a non-self-signed certificate that
    // omits the Authority Key Identifier.  The control plane reaches step-ca
    // with only the root as its trust anchor, so an installed PKI group whose
    // intermediate predates the AKI cannot complete TLS to step-ca.  Refuse it
    // during an upgrade instead of silently installing a controller that fails
    // every enrollment.  The root is self-signed and may legitimately omit its
    // own AKI.
    let root_ski = certificate_subject_key_identifier(&root);
    let intermediate_aki = certificate_authority_key_identifier(&intermediate);
    if intermediate_aki.is_none() || intermediate_aki != root_ski {
        return Err(invalid_pki(
            "intermediate certificate must carry an Authority Key Identifier that matches the \
             root subject key identifier; regenerate the Step CA/controller PKI group",
        ));
    }
    if let Some(server_aki) = certificate_authority_key_identifier(&server)
        && Some(server_aki) != certificate_subject_key_identifier(&intermediate)
    {
        return Err(invalid_pki(
            "controller certificate Authority Key Identifier must match the intermediate \
             subject key identifier; regenerate the Step CA/controller PKI group",
        ));
    }

    let expected_hostnames = pki_hostnames(request, environment)?;
    let actual_hostnames = server
        .subject_alternative_name()
        .map_err(|_| invalid_pki("controller certificate has an invalid SAN extension"))?
        .ok_or_else(|| invalid_pki("controller certificate has no SAN extension"))?
        .value
        .general_names
        .iter()
        .filter_map(|name| match name {
            x509_parser::extensions::GeneralName::DNSName(value) => Some((*value).to_owned()),
            _ => None,
        })
        .collect::<Vec<_>>();
    let controller_hostnames_changed = actual_hostnames != expected_hostnames;
    if controller_hostnames_changed && !allow_expired_controller {
        return Err(invalid_pki(
            "controller certificate hostnames do not match the configured hostnames",
        ));
    }

    let server_key = KeyPair::from_pem(value(&paths.controller_server_private_key)?)
        .map_err(|_| invalid_pki("controller private key is not valid PKCS#8 PEM"))?;
    if !server_key.is_compatible(&PKCS_ED25519)
        || server_key.public_key_raw() != server.public_key().subject_public_key.data.as_ref()
    {
        return Err(invalid_pki(
            "controller private key does not match the controller certificate",
        ));
    }
    let password = value(&paths.password)?.trim_end_matches(['\r', '\n']);
    validate_single_line_secret(password, &paths.password)?;
    let intermediate_key = ed25519_dalek::SigningKey::from_pkcs8_encrypted_pem(
        value(&paths.intermediate_private_key)?,
        password.as_bytes(),
    )
    .map_err(|_| invalid_pki("intermediate private key cannot be decrypted"))?;
    if intermediate_key.verifying_key().as_bytes()
        != intermediate.public_key().subject_public_key.data.as_ref()
    {
        return Err(invalid_pki(
            "intermediate private key does not match the intermediate certificate",
        ));
    }

    let public: PublicJwk = serde_json::from_str(value(&paths.provisioner_public_jwk)?)
        .map_err(|_| invalid_pki("public provisioner JWK is invalid"))?;
    let private: PrivateJwk = serde_json::from_str(value(&paths.provisioner_private_jwk)?)
        .map_err(|_| invalid_pki("private provisioner JWK is invalid"))?;
    validate_es256_jwks(&public, &private)?;
    let config: StepCaConfigIdentity = serde_json::from_str(value(&paths.ca_config)?)
        .map_err(|_| invalid_pki("Step CA configuration is invalid JSON"))?;
    let provisioner = config
        .authority
        .as_ref()
        .and_then(|authority| authority.provisioners.first());
    if config.root.as_deref() != Some(STEP_CA_ROOT)
        || config.crt.as_deref() != Some(STEP_CA_CRT)
        || config.key.as_deref() != Some(STEP_CA_KEY)
        || provisioner.and_then(|item| item.name.as_deref())
            != Some(request.provisioner_name.as_str())
        || provisioner.and_then(|item| item.key.as_ref()) != Some(&public)
    {
        return Err(invalid_pki(
            "Step CA configuration does not describe the imported authority",
        ));
    }

    let now_asn1 = x509_parser::time::ASN1Time::new(now);
    if !root.validity().is_valid_at(now_asn1) || !intermediate.validity().is_valid_at(now_asn1) {
        return Err(invalid_pki("CA certificate is outside its validity period"));
    }
    if server.validity().not_before > now_asn1 {
        return Err(invalid_pki("controller certificate is not valid yet"));
    }
    let controller_expired = server.validity().not_after < now_asn1;
    if controller_expired && !allow_expired_controller {
        return Err(invalid_pki(
            "controller certificate is outside its validity period",
        ));
    }
    let renewal_deadline =
        now + time::Duration::days(CONTROLLER_CERTIFICATE_RENEWAL_THRESHOLD_DAYS);
    Ok(ValidatedPkiMaterial {
        controller_needs_renewal: controller_hostnames_changed
            || server.validity().not_after.timestamp() <= renewal_deadline.unix_timestamp(),
    })
}

pub(super) fn validate_es256_jwks(
    public: &PublicJwk,
    private: &PrivateJwk,
) -> Result<(), SetupError> {
    if public.alg != "ES256"
        || public.crv != "P-256"
        || public.kty != "EC"
        || public.key_use != "sig"
        || private.alg != public.alg
        || private.crv != public.crv
        || private.kid != public.kid
        || private.kty != public.kty
        || private.key_use != public.key_use
        || private.x != public.x
        || private.y != public.y
    {
        return Err(invalid_pki("provisioner JWK metadata is inconsistent"));
    }
    let canonical = format!(
        "{{\"crv\":\"P-256\",\"kty\":\"EC\",\"x\":\"{}\",\"y\":\"{}\"}}",
        public.x, public.y
    );
    let expected_kid = Base64UrlUnpadded::encode_string(&Sha256::digest(canonical.as_bytes()));
    if public.kid != expected_kid {
        return Err(invalid_pki(
            "provisioner JWK kid is not an RFC 7638 thumbprint",
        ));
    }
    let scalar = Base64UrlUnpadded::decode_vec(&private.d)
        .map_err(|_| invalid_pki("private provisioner JWK scalar is invalid"))?;
    let secret = p256::SecretKey::from_slice(&scalar)
        .map_err(|_| invalid_pki("private provisioner JWK scalar is invalid"))?;
    let point = secret.public_key().to_sec1_point(false);
    if Base64UrlUnpadded::encode_string(point.x().ok_or_else(|| invalid_pki("P-256 x is missing"))?)
        != public.x
        || Base64UrlUnpadded::encode_string(
            point.y().ok_or_else(|| invalid_pki("P-256 y is missing"))?,
        ) != public.y
    {
        return Err(invalid_pki(
            "private provisioner JWK does not match the public JWK",
        ));
    }
    Ok(())
}

/// Return the Subject Key Identifier bytes carried by one certificate.
pub(super) fn certificate_subject_key_identifier(
    certificate: &x509_parser::certificate::X509Certificate<'_>,
) -> Option<Vec<u8>> {
    certificate
        .extensions()
        .iter()
        .find_map(|extension| match extension.parsed_extension() {
            x509_parser::extensions::ParsedExtension::SubjectKeyIdentifier(value) => {
                Some(value.0.to_vec())
            }
            _ => None,
        })
}

/// Return the Authority Key Identifier bytes carried by one certificate.
pub(super) fn certificate_authority_key_identifier(
    certificate: &x509_parser::certificate::X509Certificate<'_>,
) -> Option<Vec<u8>> {
    certificate
        .extensions()
        .iter()
        .find_map(|extension| match extension.parsed_extension() {
            x509_parser::extensions::ParsedExtension::AuthorityKeyIdentifier(value) => {
                value.key_identifier.as_ref().map(|key| key.0.to_vec())
            }
            _ => None,
        })
}

pub(super) fn invalid_pki(message: &str) -> SetupError {
    SetupError::InvalidSecretMaterial(format!("Step CA/controller PKI {message}"))
}
