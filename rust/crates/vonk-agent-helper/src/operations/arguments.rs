//! Arguments.

use super::*;

#[cfg(test)]
mod process_command_runner_tests {
    #[cfg(target_os = "linux")]
    use std::path::Path;
    #[cfg(target_os = "linux")]
    use std::time::Duration;

    use super::ROOT_COMMAND_PATH;
    #[cfg(target_os = "linux")]
    use super::{CommandRunner, ProcessCommandRunner};

    #[test]
    fn privileged_command_path_includes_debian_administrative_binaries() {
        let entries = ROOT_COMMAND_PATH.split(':').collect::<Vec<_>>();
        assert!(entries.contains(&"/usr/sbin"));
        assert!(entries.contains(&"/sbin"));
        assert!(entries.iter().all(|entry| entry.starts_with('/')));
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn dpkg_diagnostics_use_service_output_without_bounded_capture() {
        let result = ProcessCommandRunner
            .run_with_timeout(
                Path::new("/usr/bin/dpkg"),
                &["--version".to_owned()],
                Duration::from_secs(5),
            )
            .expect("the Linux test host must provide dpkg");

        assert!(result.success);
        assert!(String::from_utf8_lossy(&result.stdout).contains("Debian"));
    }
}

pub(super) fn valid_artifact_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && !matches!(value, "." | "..")
        && value.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'.' | b'_' | b'-')
        })
}

pub(super) fn runtime_generation_fence_filename(
    installation_id: uuid::Uuid,
    runtime_id: uuid::Uuid,
) -> String {
    format!("{installation_id}-{runtime_id}.json")
}

pub(super) fn parse_numeric_user(value: &str) -> Result<(u32, Option<u32>), OperationError> {
    if !numeric_non_root_user(value) {
        return Err(OperationError::InvalidOperation);
    }
    let mut parts = value.split(':');
    let uid = parts
        .next()
        .and_then(|value| value.parse::<u32>().ok())
        .filter(|value| *value != 0)
        .ok_or(OperationError::InvalidOperation)?;
    let gid = parts.next().and_then(|value| value.parse::<u32>().ok());
    Ok((uid, gid))
}

/// The firewall's own refusal text, bounded, with the request it refused.
///
/// The firewall states which argument or rule failed on stderr. The request is
/// named here as well so the reason stays attributable if an older firewall
/// binary words its refusal without the offending value.
pub(super) fn firewall_rejection_reason(request: &str, output: &CommandOutput) -> String {
    const LIMIT: usize = 512;
    let text = String::from_utf8_lossy(&output.stderr);
    let text = text
        .trim()
        .chars()
        .map(|character| {
            if character.is_control() {
                ' '
            } else {
                character
            }
        })
        .take(LIMIT)
        .collect::<String>();
    let status = output.exit_code.map_or_else(
        || "no exit status".to_owned(),
        |code| format!("exit {code}"),
    );
    if text.is_empty() {
        format!("{request}: firewall refused without a reason ({status})")
    } else {
        format!("{request}: {text} ({status})")
    }
}

pub(super) fn parse_publication(value: &str) -> Option<(std::net::Ipv4Addr, u16, u16)> {
    let (address, ports) = if let Some(value) = value.strip_prefix('[') {
        let (address, ports) = value.split_once("]:")?;
        (address, ports)
    } else {
        let (address, ports) = value.split_once(':')?;
        (address, ports)
    };
    let address = address.parse::<std::net::Ipv4Addr>().ok()?;
    if address.is_unspecified()
        || address.is_loopback()
        || address.is_multicast()
        || address.is_link_local()
    {
        return None;
    }
    let (host, container) = ports.split_once(':')?;
    if container.contains(':') {
        return None;
    }
    let host = host
        .parse::<u16>()
        .ok()
        .filter(|port| (1024..=65535).contains(port))?;
    let container = container
        .parse::<u16>()
        .ok()
        .filter(|port| (1024..=65535).contains(port))?;
    Some((address, host, container))
}

/// The name is charset-restricted because it becomes an exec environment key.
/// Values are Controller-signed plan data, so their content is not restricted;
/// they are only bounded and kept NUL-free so the exec environment stays
/// well-formed.
pub(super) fn valid_environment(value: &str) -> bool {
    let Some((name, value)) = value.split_once('=') else {
        return false;
    };
    !name.is_empty()
        && name.len() <= 128
        && name.bytes().enumerate().all(|(index, byte)| {
            if index == 0 {
                byte.is_ascii_uppercase()
            } else {
                byte.is_ascii_alphanumeric() || byte == b'_'
            }
        })
        && value.len() <= MAX_ENVIRONMENT_VALUE_BYTES
        && !value.contains('\0')
}

pub(super) fn valid_local_image(value: &str) -> bool {
    let recipe_build = value
        .strip_prefix("localhost/vonk/recipe-build-")
        .and_then(|value| uuid::Uuid::parse_str(value).ok().map(|id| (value, id)))
        .is_some_and(|(value, id)| id.to_string() == value);
    let compiled_runtime = value
        .strip_prefix("localhost/vonk/compiled-runtime-")
        .is_some_and(|value| lower_hex(value, 64));
    recipe_build || compiled_runtime
}

pub(super) fn valid_local_image_reference(value: &str) -> bool {
    value
        .split_once('@')
        .is_some_and(|(image, digest)| valid_local_image(image) && valid_oci_digest(digest))
}

pub(super) fn valid_entrypoint(value: &str) -> bool {
    value.starts_with("/opt/vonk/bin/")
        && value.len() <= 256
        && !value.ends_with('/')
        && !value.contains("//")
        && !value.split('/').any(|part| part == "." || part == "..")
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'/' | b'_' | b'-' | b'.'))
}

pub(super) fn parse_local_image_reference(value: &str) -> Result<(String, String), OperationError> {
    let (image, digest) = value
        .split_once('@')
        .ok_or(OperationError::InvalidOperation)?;
    if !valid_local_image(image) || !valid_oci_digest(digest) {
        return Err(OperationError::InvalidOperation);
    }
    Ok((image.to_owned(), digest.to_owned()))
}

/// Only the agent's loopback forwarder may serve an image pull.
pub(super) fn valid_loopback_registry(value: &str) -> bool {
    value
        .strip_prefix("127.0.0.1:")
        .and_then(|port| {
            (!port.starts_with('0') && port.bytes().all(|byte| byte.is_ascii_digit()))
                .then(|| port.parse::<u16>().ok())
                .flatten()
        })
        .is_some_and(|port| port >= 1024)
}

pub(super) fn valid_oci_digest(value: &str) -> bool {
    value
        .strip_prefix("sha256:")
        .is_some_and(|value| lower_hex(value, 64))
}

pub(super) fn lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

pub(super) fn numeric_non_root_user(value: &str) -> bool {
    let mut parts = value.split(':');
    let valid = |part: &str| {
        !part.is_empty() && !part.starts_with('0') && part.bytes().all(|byte| byte.is_ascii_digit())
    };
    valid(parts.next().unwrap_or_default())
        && parts.next().is_none_or(valid)
        && parts.next().is_none()
}

#[cfg(test)]
mod tests;
