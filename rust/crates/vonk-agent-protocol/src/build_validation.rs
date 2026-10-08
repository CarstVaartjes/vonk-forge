//! Build validation.

use super::*;

pub(super) fn valid_build_options(value: &RecipeBuildOptions) -> bool {
    value.additional_contexts.len() <= 16
        && value
            .additional_contexts
            .iter()
            .enumerate()
            .all(|(index, item)| {
                valid_name(&item.name)
                    && valid_bundle_path(&item.path)
                    && !value.additional_contexts[..index]
                        .iter()
                        .any(|prior| prior.name == item.name)
            })
        && [&value.annotations, &value.labels, &value.layer_labels]
            .into_iter()
            .all(|entries| {
                entries.len() <= 64
                    && entries.iter().enumerate().all(|(index, item)| {
                        valid_build_metadata_name(&item.name)
                            && item.value.len() <= 1024
                            && !item.value.contains('\0')
                            && !entries[..index].iter().any(|prior| prior.name == item.name)
                    })
            })
        && value.environment.len() <= 64
        && value.environment.iter().enumerate().all(|(index, item)| {
            valid_build_environment_name(&item.name)
                && valid_scalar(&item.value)
                && !value.environment[..index]
                    .iter()
                    .any(|prior| prior.name == item.name)
        })
        && matches!(value.format.as_str(), "oci" | "docker")
        && value
            .ignorefile
            .as_ref()
            .is_none_or(|path| valid_bundle_path(path))
        && (1..=32).contains(&value.jobs)
        && matches!(value.layer_compression.as_str(), "disabled" | "gzip")
        && value.os_features.len() <= 32
        && value
            .os_features
            .iter()
            .enumerate()
            .all(|(index, feature)| {
                !feature.is_empty()
                    && feature.len() <= 64
                    && feature
                        .bytes()
                        .all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(&byte))
                    && !value.os_features[..index].contains(feature)
            })
        && value.os_version.as_ref().is_none_or(|version| {
            !version.is_empty()
                && version.len() <= 64
                && version
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || b"._+-".contains(&byte))
        })
        && (65_536..=64 * 1024_u64.pow(3)).contains(&value.shm_bytes)
        && matches!(value.squash.as_str(), "none" | "new" | "all")
        && value
            .timestamp
            .is_none_or(|timestamp| timestamp <= 4_102_444_800)
        && value.unset_environment.len() <= 64
        && value
            .unset_environment
            .iter()
            .enumerate()
            .all(|(index, name)| {
                valid_build_environment_name(name)
                    && !value.unset_environment[..index].contains(name)
            })
        && value.unset_labels.len() <= 64
        && value.unset_labels.iter().enumerate().all(|(index, name)| {
            valid_build_metadata_name(name) && !value.unset_labels[..index].contains(name)
        })
}

pub(super) fn valid_public_host(value: &str) -> bool {
    let lowered = value.to_ascii_lowercase();
    let reserved = matches!(
        lowered.as_str(),
        "localhost"
            | "localhost.localdomain"
            | "metadata"
            | "metadata.google.internal"
            | "instance-data.ec2.internal"
    ) || lowered.ends_with(".localhost")
        || lowered.ends_with(".localdomain")
        || lowered.ends_with(".internal");
    let numeric = value
        .bytes()
        .all(|byte| byte.is_ascii_digit() || byte == b'.');
    let numeric_public = if numeric {
        value.parse::<std::net::Ipv4Addr>().is_ok_and(public_ipv4)
    } else {
        true
    };
    !value.is_empty()
        && value.len() <= 253
        && !value.starts_with('.')
        && !value.ends_with('.')
        && !reserved
        && numeric_public
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'-'))
}

pub(super) fn public_ipv4(address: std::net::Ipv4Addr) -> bool {
    let octets = address.octets();
    let [first, second, ..] = octets;
    first != 0
        && first != 10
        && first != 127
        && !(first == 100 && (64..=127).contains(&second))
        && !(first == 169 && second == 254)
        && !(first == 172 && (16..=31).contains(&second))
        && !(first == 192 && second == 168)
        && !(first == 198 && (18..=19).contains(&second))
        && first < 224
}

pub(super) fn link_local(value: std::net::IpAddr) -> bool {
    match value {
        std::net::IpAddr::V4(address) => address.is_link_local() || address.is_broadcast(),
        std::net::IpAddr::V6(address) => address.is_unicast_link_local(),
    }
}

pub(super) fn valid_fabric_address(value: std::net::IpAddr) -> bool {
    !value.is_loopback() && !value.is_unspecified() && !value.is_multicast() && !link_local(value)
}
