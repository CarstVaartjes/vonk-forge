//! Images.

use super::*;

pub(super) fn podman_build_arguments(
    storage: &Path,
    runroot: &Path,
    network: Option<(&str, &str)>,
) -> Vec<String> {
    let mut arguments = podman_storage_arguments_with_cgroup_manager(storage, runroot, "cgroupfs");
    if network.is_some() {
        // Buildah's default rootless isolation cannot attach named networks.
        // Enter Podman's existing unprivileged user/network namespace first,
        // then let OCI create the build's separate internal-only namespace.
        // Both Podman processes share this operation's storage and cgroup.
        arguments.extend([
            "unshare".to_owned(),
            "--rootless-netns".to_owned(),
            "/usr/bin/podman".to_owned(),
        ]);
        arguments.extend(podman_storage_arguments_with_cgroup_manager(
            storage, runroot, "cgroupfs",
        ));
    }
    arguments.extend([
        "--runtime=/usr/bin/crun".to_owned(),
        "build".to_owned(),
        "--isolation=oci".to_owned(),
        format!("--network={}", network.map_or("none", |(name, _)| name)),
    ]);
    if let Some((_, proxy)) = network {
        for name in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"] {
            arguments.extend(["--build-arg".to_owned(), format!("{name}={proxy}")]);
        }
        for name in ["NO_PROXY", "no_proxy"] {
            arguments.extend(["--build-arg".to_owned(), format!("{name}=")]);
        }
    }
    arguments
}

pub(super) fn podman_storage_arguments(storage: &Path, runroot: &Path) -> Vec<String> {
    podman_storage_arguments_with_cgroup_manager(storage, runroot, "systemd")
}

pub(crate) fn podman_storage_arguments_with_cgroup_manager(
    storage: &Path,
    runroot: &Path,
    cgroup_manager: &str,
) -> Vec<String> {
    vec![
        format!("--cgroup-manager={cgroup_manager}"),
        "--root".to_owned(),
        storage.display().to_string(),
        "--runroot".to_owned(),
        runroot.display().to_string(),
        "--storage-opt".to_owned(),
        "overlay.ignore_chown_errors=true".to_owned(),
        "--storage-opt".to_owned(),
        "overlay.mount_program=/usr/bin/fuse-overlayfs".to_owned(),
        "--storage-opt".to_owned(),
        "overlay.force_mask=shared".to_owned(),
    ]
}

pub(super) fn scalar(
    value: &RecipeBuildEnvironmentArgumentValue,
) -> Result<String, RecipeBuildError> {
    match value {
        RecipeBuildEnvironmentArgumentValue::Boolean(value) => Ok(value.to_string()),
        RecipeBuildEnvironmentArgumentValue::VonkInteger(value) => Ok(value.to_string()),
        RecipeBuildEnvironmentArgumentValue::String(value) if !value.contains('\0') => {
            Ok(value.clone())
        }
        RecipeBuildEnvironmentArgumentValue::String(_) => Err(RecipeBuildError::Evidence),
    }
}

/// One inspect template carries the platform fields the agent must verify.
/// A recipe image legitimately lacks the adapter labels; the recipe-stage
/// check ignores them and the adapted-stage check requires them.
pub(super) const IMAGE_INSPECT_FORMAT: &str = "{{.Os}}\t{{.Architecture}}\t\
{{index .Config.Labels \"ai.vonkforge.runtime-interface\"}}\t\
{{index .Config.Labels \"ai.vonkforge.runtime-adapter\"}}\t\
{{index .Config.Labels \"ai.vonkforge.runtime-adapter-sha256\"}}\t{{.Config.User}}";

/// Write the reviewed adaptation stage after verifying it against its plan.
///
/// The Controller derives ``adapter_sha256`` from the canonical definition and
/// this re-derives it from the received bytes, so a plan cannot install a
/// different adaptation than the one the prepared-image identity recorded.
pub(super) fn write_adapter_containerfile(
    path: &Path,
    adapter: &RecipeBuildAdapter,
) -> Result<(), RecipeBuildError> {
    let definition = &adapter.definition;
    let canonical = canonical_json(definition).map_err(|_| RecipeBuildError::AdapterInvalid)?;
    if hex_sha256(&canonical) != adapter.adapter_sha256 {
        return Err(RecipeBuildError::AdapterInvalid);
    }
    let mut file = File::create(path)?;
    file.write_all(definition.containerfile.as_bytes())?;
    file.sync_all()?;
    Ok(())
}

pub(super) fn inspect_recipe_image(payload: &[u8]) -> Result<(), RecipeBuildError> {
    let fields = inspect_fields(payload)?;
    if fields.len() != 6 || fields[0] != "linux" || fields[1] != "arm64" {
        return Err(RecipeBuildError::ImageInspect);
    }
    Ok(())
}

pub(super) fn inspect_adapted_image(
    payload: &[u8],
    adapter: &RecipeBuildAdapter,
) -> Result<(), RecipeBuildError> {
    let fields = inspect_fields(payload)?;
    if fields.len() != 6
        || fields[0] != "linux"
        || fields[1] != "arm64"
        || fields[2] != RUNTIME_INTERFACE_LABEL_VALUE
        || fields[3] != adapter.definition.adapter_id
        || fields[4] != adapter.adapter_sha256
        || fields[5] != adapter.definition.image_user
        || !non_root_user(fields[5])
    {
        return Err(RecipeBuildError::AdapterInspect);
    }
    Ok(())
}

pub(super) fn inspect_fields(payload: &[u8]) -> Result<Vec<&str>, RecipeBuildError> {
    let text = std::str::from_utf8(payload).map_err(|_| RecipeBuildError::Evidence)?;
    Ok(text.trim().split('\t').collect())
}

pub(super) fn inspect_base_image(
    output: &crate::process::ProcessOutput,
    manifest_digest: &str,
) -> Result<(), RecipeBuildError> {
    let text = std::str::from_utf8(&output.stdout).map_err(|_| RecipeBuildError::Evidence)?;
    let fields = text.trim().split('\t').collect::<Vec<_>>();
    if !output.success || fields != [manifest_digest, "linux", "arm64"] {
        return Err(RecipeBuildError::Evidence);
    }
    Ok(())
}

pub(super) fn non_root_user(value: &str) -> bool {
    let mut parts = value.split(':');
    let valid = |part: &str| {
        !part.is_empty() && !part.starts_with('0') && part.bytes().all(|byte| byte.is_ascii_digit())
    };
    valid(parts.next().unwrap_or_default())
        && parts.next().is_none_or(valid)
        && parts.next().is_none()
}

pub(super) fn sha256_file_cancellable(
    path: &Path,
    deadline: Instant,
    cancelled: &dyn Fn() -> bool,
) -> Result<String, std::io::Error> {
    use std::io::Read;
    let mut file = fs::File::open(path)?;
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 1024 * 1024];
    loop {
        if cancelled() || Instant::now() >= deadline {
            return Err(std::io::ErrorKind::TimedOut.into());
        }
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        digest.update(&buffer[..read]);
    }
    Ok(hex::encode(digest.finalize()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use vonk_agent_protocol::RecipeBuildAdapterDefinition;

    const ADAPTER_STAGE: &str = "ARG VONK_RECIPE_IMAGE\nFROM ${VONK_RECIPE_IMAGE}\nUSER 0\n\
    RUN set -eu \\\n && install --directory --owner=10001 --group=10001 /outputs \\\n \
    && test -x /opt/vonk/bin/vllm\nUSER 10001:10001\nWORKDIR /tmp\n";

    fn adapter(containerfile: &str) -> RecipeBuildAdapter {
        let definition = RecipeBuildAdapterDefinition {
            adapter_id: "vonk.runtime-contract.vllm.v1".to_owned(),
            containerfile: containerfile.to_owned(),
            engine: "vllm".to_owned(),
            image_user: "10001:10001".to_owned(),
        };
        let digest = hex_sha256(&canonical_json(&definition).expect("canonical adapter"));
        RecipeBuildAdapter {
            adapter_sha256: digest,
            definition,
        }
    }

    fn inspect_payload(adapter: &RecipeBuildAdapter, user: &str, interface: &str) -> Vec<u8> {
        format!(
            "linux\tarm64\t{interface}\t{}\t{}\t{user}",
            adapter.definition.adapter_id, adapter.adapter_sha256
        )
        .into_bytes()
    }

    #[test]
    fn adapter_stage_is_written_only_for_its_own_digest() {
        let directory = tempfile::tempdir().expect("temporary adapter context");
        let path = directory.path().join("Containerfile");
        let accepted = adapter(ADAPTER_STAGE);
        super::write_adapter_containerfile(&path, &accepted).expect("matching digest");
        assert_eq!(
            std::fs::read_to_string(&path).expect("written adapter"),
            ADAPTER_STAGE
        );
        // The wrong implementation trusts the declared fields without
        // re-deriving the canonical digest, so it installs a tampered stage.
        let tampered = RecipeBuildAdapter {
            adapter_sha256: "0".repeat(64),
            definition: accepted.definition.clone(),
        };
        assert!(super::write_adapter_containerfile(&path, &tampered).is_err());
        assert_eq!(std::fs::read_to_string(&path).unwrap(), ADAPTER_STAGE);
        super::write_adapter_containerfile(&path, &accepted).unwrap();
    }

    #[test]
    fn digest_bound_adapter_is_consumed_without_a_second_syntax_policy() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("Containerfile");
        for source in [
            "FROM scratch\n",
            "FROM ${VONK_RECIPE_IMAGE}\nADD https://example.invalid/x /x\n",
        ] {
            super::write_adapter_containerfile(&path, &adapter(source)).unwrap();
            assert_eq!(std::fs::read_to_string(&path).unwrap(), source);
        }
    }

    #[test]
    fn adapted_image_evidence_binds_interface_adapter_and_user() {
        let request = adapter(ADAPTER_STAGE);
        super::inspect_adapted_image(&inspect_payload(&request, "10001:10001", "v1"), &request)
            .expect("matching adapted evidence");
        // The wrong implementation checks only that some label and some user
        // exist, so a different adapter or user would be accepted.
        for payload in [
            inspect_payload(&request, "0:0", "v1"),
            inspect_payload(&request, "10001:10001", "v2"),
            b"linux\tarm64\tv1\tvonk.runtime-contract.vllm.v1\t".to_vec(),
        ] {
            assert!(super::inspect_adapted_image(&payload, &request).is_err());
            super::inspect_adapted_image(&inspect_payload(&request, "10001:10001", "v1"), &request)
                .unwrap();
        }
    }
}
