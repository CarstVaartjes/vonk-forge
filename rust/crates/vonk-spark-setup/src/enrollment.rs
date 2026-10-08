//! Enrollment.

use super::*;

pub(super) fn required_origin(prompt: &mut dyn Prompt, label: &str) -> Result<Url, SetupError> {
    let value = prompt.value(label).map_err(|_| SetupError::Prompt)?;
    let url = Url::parse(&value).map_err(|_| SetupError::UnsafeInput("endpoint URL"))?;
    if valid_origin(&url) {
        Ok(url)
    } else {
        Err(SetupError::UnsafeInput("endpoint URL"))
    }
}

/// Parse the current Controller bootstrap document; omitted fields are invalid.
pub fn parse_enrollment_bootstrap(bytes: &[u8]) -> Result<EnrollmentBootstrapResponse, SetupError> {
    if bytes.is_empty() || bytes.len() > MAX_BOOTSTRAP_BYTES {
        return Err(SetupError::EnrollmentBootstrap);
    }
    serde_json::from_slice(bytes).map_err(|_| SetupError::EnrollmentBootstrap)
}

/// Refuse a re-enrollment whose stored controller CA no longer matches the CA
/// the controller advertises.
///
/// A rotated controller CA otherwise reaches the enroll POST as an opaque
/// rustls `InvalidCertificate(BadSignature)`, because the old and new roots can
/// share a common name, and the operator has to hand-refresh the pinned CA on
/// every Spark.  The insecure bootstrap read supplies only the advertised
/// fingerprint: nothing else from it is trusted and the discovered CA is never
/// adopted here.
pub(super) fn verify_reenroll_controller_ca(
    config: &WrittenConfig,
    controller_address: Option<Ipv4Addr>,
    runner: &mut dyn CommandRunner,
) -> Result<(), SetupError> {
    let mut bootstrap_url = config.enrollment_url.clone();
    bootstrap_url.set_path("/agent/bootstrap");
    let output = run_checked(
        runner,
        bootstrap_curl(&bootstrap_url, controller_address, None),
    )?
    .stdout;
    let bootstrap = parse_enrollment_bootstrap(&output)?;
    if bootstrap.ca_fingerprint != config.ca_sha256 {
        return Err(SetupError::ControllerCaChanged {
            stored: config.ca_sha256.clone(),
            advertised: bootstrap.ca_fingerprint,
        });
    }
    Ok(())
}

pub(super) fn discover_enrollment(
    expected_enrollment: &Url,
    expected_ca_sha256: &str,
    expected_controller_address: Option<Ipv4Addr>,
    runner: &mut dyn CommandRunner,
) -> Result<EnrollmentDiscovery, SetupError> {
    let mut bootstrap_url = expected_enrollment.clone();
    bootstrap_url.set_path("/agent/bootstrap");
    let output = run_checked(
        runner,
        bootstrap_curl(&bootstrap_url, expected_controller_address, None),
    )?
    .stdout;
    let bootstrap = parse_enrollment_bootstrap(&output)?;
    let discovered = validate_enrollment_bootstrap(
        bootstrap,
        expected_enrollment,
        expected_ca_sha256,
        expected_controller_address,
    )?;
    let directory = secure_tempdir("vonk-enrollment-ca.")?;
    let ca_path = directory.path().join("controller-ca.pem");
    fs::write(&ca_path, &discovered.ca_pem).map_err(SetupError::PrivilegedWrite)?;
    let authenticated = run_checked(
        runner,
        bootstrap_curl(
            &bootstrap_url,
            discovered.host_mapping.as_ref().map(|value| {
                value
                    .address
                    .parse::<Ipv4Addr>()
                    .expect("validated controller address")
            }),
            Some(&ca_path),
        ),
    )?
    .stdout;
    let authenticated = parse_enrollment_bootstrap(&authenticated)?;
    let verified = validate_enrollment_bootstrap(
        authenticated,
        expected_enrollment,
        expected_ca_sha256,
        discovered
            .host_mapping
            .as_ref()
            .map(|value| value.address.parse().expect("validated controller address")),
    )?;
    if verified != discovered {
        return Err(SetupError::EnrollmentBootstrap);
    }
    Ok(verified)
}

pub(super) fn bootstrap_curl(
    bootstrap_url: &Url,
    controller_address: Option<Ipv4Addr>,
    ca_path: Option<&Path>,
) -> Command {
    let mut arguments = vec![
        "--fail".to_owned(),
        "--silent".to_owned(),
        "--show-error".to_owned(),
        "--proto".to_owned(),
        "=https".to_owned(),
        "--tlsv1.2".to_owned(),
    ];
    if let Some(address) = controller_address {
        let hostname = bootstrap_url.host_str().expect("validated HTTPS origin");
        let port = bootstrap_url.port_or_known_default().unwrap_or(443);
        arguments.extend([
            "--resolve".to_owned(),
            format!("{hostname}:{port}:{address}"),
        ]);
    }
    if let Some(path) = ca_path {
        arguments.extend(["--cacert".to_owned(), path.display().to_string()]);
    } else {
        arguments.push("--insecure".to_owned());
    }
    arguments.extend([
        "--max-filesize".to_owned(),
        MAX_BOOTSTRAP_BYTES.to_string(),
        bootstrap_url.as_str().to_owned(),
    ]);
    Command::new("/usr/bin/curl", arguments)
}

pub(super) fn validate_enrollment_bootstrap(
    bootstrap: EnrollmentBootstrapResponse,
    expected_enrollment: &Url,
    expected_ca_sha256: &str,
    expected_controller_address: Option<Ipv4Addr>,
) -> Result<EnrollmentDiscovery, SetupError> {
    let enrollment =
        Url::parse(&bootstrap.enrollment_endpoint).map_err(|_| SetupError::EnrollmentBootstrap)?;
    let controller =
        Url::parse(&bootstrap.controller_endpoint).map_err(|_| SetupError::EnrollmentBootstrap)?;
    if !valid_origin(&enrollment)
        || !valid_origin(&controller)
        || &enrollment != expected_enrollment
        || bootstrap.ca_fingerprint != expected_ca_sha256
        || bootstrap.ca_pem.len() > MAX_CA_BYTES
    {
        return Err(SetupError::EnrollmentBootstrap);
    }
    let ca = bootstrap.ca_pem.into_bytes();
    verify_ca(&ca, expected_ca_sha256).map_err(|_| SetupError::EnrollmentBootstrap)?;
    if bootstrap.host_helper_authority_public_key.len() != 64
        || !bootstrap
            .host_helper_authority_public_key
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        return Err(SetupError::EnrollmentBootstrap);
    }
    let helper_authority = hex::decode(bootstrap.host_helper_authority_public_key)
        .map_err(|_| SetupError::EnrollmentBootstrap)?;
    if !valid_helper_authority(&helper_authority) {
        return Err(SetupError::EnrollmentBootstrap);
    }
    let mapping = match bootstrap.controller_address {
        Some(address) => {
            let address = address
                .parse::<Ipv4Addr>()
                .map_err(|_| SetupError::EnrollmentBootstrap)?;
            if expected_controller_address.is_some_and(|expected| expected != address) {
                return Err(SetupError::EnrollmentBootstrap);
            }
            let mapping = HostMapping {
                address: address.to_string(),
                hostnames: bootstrap.service_hostnames,
            };
            if !valid_host_mapping(Some(&mapping))
                || !mapping
                    .hostnames
                    .iter()
                    .any(|value| Some(value.as_str()) == enrollment.host_str())
                || !mapping
                    .hostnames
                    .iter()
                    .any(|value| Some(value.as_str()) == controller.host_str())
            {
                return Err(SetupError::EnrollmentBootstrap);
            }
            Some(mapping)
        }
        None if bootstrap.service_hostnames.is_empty() && expected_controller_address.is_none() => {
            None
        }
        None => return Err(SetupError::EnrollmentBootstrap),
    };
    Ok(EnrollmentDiscovery {
        controller_url: controller,
        ca_pem: ca,
        host_mapping: mapping,
        helper_authority,
    })
}

pub(super) fn required_sha256(prompt: &mut dyn Prompt, label: &str) -> Result<String, SetupError> {
    let value = prompt.value(label).map_err(|_| SetupError::Prompt)?;
    if valid_sha256(&value) {
        Ok(value)
    } else {
        Err(SetupError::UnsafeInput("SHA-256"))
    }
}

pub(super) fn valid_origin(url: &Url) -> bool {
    url.scheme() == "https"
        && url.host_str().is_some()
        && url.username().is_empty()
        && url.password().is_none()
        && url.query().is_none()
        && url.fragment().is_none()
        && url.path() == "/"
}

pub(super) fn valid_host_mapping(mapping: Option<&HostMapping>) -> bool {
    let Some(mapping) = mapping else {
        return true;
    };
    mapping.address.parse::<Ipv4Addr>().is_ok()
        && !mapping.hostnames.is_empty()
        && mapping.hostnames.len() <= 16
        && mapping
            .hostnames
            .iter()
            .enumerate()
            .all(|(index, hostname)| {
                valid_hostname(hostname) && !mapping.hostnames[..index].contains(hostname)
            })
}

pub(super) fn valid_hostname(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 253
        && value.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-' || byte == b'.'
        })
        && value.split('.').all(|label| {
            !label.is_empty()
                && label.len() <= 63
                && label
                    .as_bytes()
                    .first()
                    .is_some_and(u8::is_ascii_alphanumeric)
                && label
                    .as_bytes()
                    .last()
                    .is_some_and(u8::is_ascii_alphanumeric)
        })
        && value.parse::<Ipv4Addr>().is_err()
}

pub(super) fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

pub(super) fn valid_package_version(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'+' | b'~' | b':' | b'-')
        })
}

pub(super) fn valid_token(value: &str) -> bool {
    value.len() == 43
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
}

pub(super) fn verify_ca(ca: &[u8], expected: &str) -> Result<(), SetupError> {
    if ca.len() > MAX_CA_BYTES {
        return Err(SetupError::ControllerCa);
    }
    let mut reader = BufReader::new(ca);
    let certificate = rustls_pemfile::certs(&mut reader)
        .next()
        .ok_or(SetupError::ControllerCa)
        .and_then(|value| value.map_err(|_| SetupError::ControllerCa))?;
    if rustls_pemfile::certs(&mut reader).next().is_some()
        || hex::encode(Sha256::digest(certificate.as_ref())) != expected
    {
        return Err(SetupError::ControllerCa);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn enrollment_bootstrap_document() -> serde_json::Value {
        serde_json::json!({
            "controller_endpoint": "https://controller.example.test/",
            "enrollment_endpoint": "https://enroll.example.test/",
            "ca_fingerprint": "a".repeat(64),
            "ca_pem": "-----BEGIN CERTIFICATE-----\nY2E=\n-----END CERTIFICATE-----\n",
            "controller_address": null,
            "service_hostnames": [],
            "host_helper_authority_public_key": "b".repeat(64),
        })
    }

    #[test]
    fn enrollment_bootstrap_uses_the_exact_generated_wire_shape() {
        let document = enrollment_bootstrap_document();
        assert!(parse_enrollment_bootstrap(&serde_json::to_vec(&document).unwrap()).is_ok());

        let mut missing_nullable = document.clone();
        missing_nullable
            .as_object_mut()
            .unwrap()
            .remove("controller_address");
        assert!(
            parse_enrollment_bootstrap(&serde_json::to_vec(&missing_nullable).unwrap()).is_err()
        );

        let mut wrong_type = document.clone();
        wrong_type["service_hostnames"] = serde_json::json!("controller.example.test");
        assert!(parse_enrollment_bootstrap(&serde_json::to_vec(&wrong_type).unwrap()).is_err());

        let mut unknown = document;
        unknown["legacy_controller"] = serde_json::json!("controller.example.test");
        assert!(parse_enrollment_bootstrap(&serde_json::to_vec(&unknown).unwrap()).is_err());
    }
}
