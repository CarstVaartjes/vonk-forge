//! Canonical.

use super::*;

pub fn parse_strict<T: DeserializeOwned>(input: &[u8]) -> Result<T, ProtocolError> {
    Ok(serde_json::from_slice(input)?)
}

pub fn canonical_json<T: Serialize>(value: &T) -> Result<Vec<u8>, ProtocolError> {
    Ok(passthrough::WireDocument::of(value)?.canonical_bytes()?)
}

pub fn hex_sha256(value: &[u8]) -> String {
    Sha256::digest(value)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

pub(super) fn valid_node_id(value: &str) -> bool {
    value.len() == 36
        && value.starts_with("spk_")
        && value[4..]
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

pub(super) fn valid_reconciliation_identity(value: &RecipeReconciliationIdentity) -> bool {
    !value.installation_id.is_nil() && lower_hex(&value.plan_digest, 64)
}

/// An empty success: an object without any non-null value.
pub(super) fn empty_result(result: &generated::AgentResultResult) -> bool {
    passthrough::WireDocument::of(result).is_ok_and(|document| document.is_object_of_nulls())
}

pub(super) fn lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

pub(super) fn valid_oci_digest(value: &str) -> bool {
    value
        .strip_prefix("sha256:")
        .is_some_and(|digest| lower_hex(digest, 64))
}

pub(super) fn valid_pinned_image(reference: &str, manifest_digest: &str) -> bool {
    let Some((name, digest)) = reference.rsplit_once('@') else {
        return false;
    };
    !name.is_empty()
        && name.len() <= 512
        && name
            .as_bytes()
            .first()
            .is_some_and(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit())
        && name.bytes().all(|byte| {
            byte.is_ascii_lowercase()
                || byte.is_ascii_digit()
                || matches!(byte, b'.' | b'_' | b':' | b'/' | b'-')
        })
        && digest == manifest_digest
        && valid_oci_digest(digest)
}

pub(super) fn valid_role(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value.bytes().enumerate().all(|(index, byte)| {
            if index == 0 {
                byte.is_ascii_lowercase()
            } else {
                byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'_' | b'-')
            }
        })
}

pub(super) fn valid_scalar(value: &generated::RecipeBuildEnvironmentArgumentValue) -> bool {
    match value {
        generated::RecipeBuildEnvironmentArgumentValue::Boolean(_)
        | generated::RecipeBuildEnvironmentArgumentValue::VonkInteger(_) => true,
        generated::RecipeBuildEnvironmentArgumentValue::String(value) => {
            value.len() <= 1024 && !value.contains('\0')
        }
    }
}

pub(super) fn valid_name(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value.bytes().enumerate().all(|(index, byte)| {
            if index == 0 {
                byte.is_ascii_lowercase()
            } else {
                byte.is_ascii_lowercase()
                    || byte.is_ascii_digit()
                    || matches!(byte, b'.' | b'_' | b'-')
            }
        })
}

pub(super) fn valid_bundle_path(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 512
        && !value.starts_with('/')
        && !value.contains('\\')
        && !value.contains('\0')
        && value
            .split('/')
            .all(|part| !part.is_empty() && !matches!(part, "." | ".."))
}

pub(super) fn valid_build_metadata_name(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value.bytes().enumerate().all(|(index, byte)| {
            (index > 0 || byte.is_ascii_alphanumeric())
                && (byte.is_ascii_alphanumeric() || b"._/-".contains(&byte))
        })
}

pub(super) fn valid_build_environment_name(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value.bytes().enumerate().all(|(index, byte)| {
            (index > 0 || byte.is_ascii_uppercase() || byte == b'_')
                && (byte.is_ascii_uppercase() || byte.is_ascii_digit() || byte == b'_')
        })
}

/// Validate an outgoing generated document before producing its canonical bytes.
/// This also covers direct Rust construction, which does not invoke Deserialize.
pub fn canonical_generated_json<T: Serialize + DeserializeOwned>(
    document: &T,
) -> Result<Vec<u8>, ProtocolError> {
    canonical_json(&revalidate(document)?)
}
