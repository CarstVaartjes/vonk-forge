#![cfg(test)]

use super::*;

pub(super) use crate::process::{ProcessError, ProcessOutput, ProcessRunner, Program};

pub(super) use serde_json::{Value, json};

pub(super) use sha2::Digest;

pub(super) use std::{
    fs,
    os::unix::fs::{PermissionsExt, symlink},
    path::{Path, PathBuf},
    time::Duration,
};

pub(super) use tempfile::tempdir;

pub(super) use uuid::Uuid;

pub(super) fn reconciliation_identity(
    installation_id: Uuid,
) -> vonk_agent_protocol::RecipeReconciliationIdentity {
    vonk_agent_protocol::RecipeReconciliationIdentity {
        installation_id,
        plan_digest: "c".repeat(64),
    }
}

pub(super) fn reconciliation_installation(
    data_root: &Path,
    installation_id: Uuid,
) -> (PathBuf, vonk_agent_protocol::RecipeReconciliationIdentity) {
    let installation = data_root
        .join("installations")
        .join(installation_id.to_string());
    fs::create_dir_all(&installation).unwrap();
    fs::set_permissions(&installation, fs::Permissions::from_mode(0o700)).unwrap();
    let spec = serde_json::to_vec(&json!({
        "identity": {"recipe_revision_sha256": "d".repeat(64)},
        "old invalid launch shape": [false, null, {"opaque": "preserved"}],
    }))
    .unwrap();
    fs::write(installation.join("spec.json"), &spec).unwrap();
    fs::set_permissions(
        installation.join("spec.json"),
        fs::Permissions::from_mode(0o600),
    )
    .unwrap();
    let recipe_digest = "d".repeat(64);
    fs::write(
        installation.join("recipe-content.sha256"),
        recipe_digest.as_bytes(),
    )
    .unwrap();
    fs::set_permissions(
        installation.join("recipe-content.sha256"),
        fs::Permissions::from_mode(0o600),
    )
    .unwrap();
    fs::write(installation.join("opaque-agent-file"), b"agent-owned").unwrap();
    (installation, reconciliation_identity(installation_id))
}

pub(super) struct NoProcess;

impl ProcessRunner for NoProcess {
    fn run(&self, _: Program, _: &[String], _: Duration) -> Result<ProcessOutput, ProcessError> {
        panic!("OCI verification tests must not launch a process");
    }
}

pub(super) struct Gb10MemoryRunner;

impl ProcessRunner for Gb10MemoryRunner {
    fn run(
        &self,
        program: Program,
        _: &[String],
        _: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        assert_eq!(program, Program::NvidiaSmi);
        Ok(ProcessOutput {
            success: true,
            stdout: b"NVIDIA GB10, [N/A], [N/A], 590.44\n".to_vec(),
            stderr: vec![],
        })
    }
}

pub(super) fn digest(value: &[u8]) -> String {
    hex::encode(sha2::Sha256::digest(value))
}

pub(super) fn compiled_plan() -> Value {
    let primary = digest(b"primary");
    let secondary = digest(b"secondary");
    json!({
        "identity": {
            "recipe_revision_sha256": "a".repeat(64),
            "model_artifact_set_sha256": "d".repeat(64)
        },
        "runtime": {
            "executable": "/opt/vonk/bin/vllm",
            "argv": ["serve", "/models"],
            "env": [],
            "placement": {
                "endpoint_address": null,
                "rank": 0,
                "role": "entrypoint",
                "world_size": 1,
                "local_address": null,
                "master_address": null,
                "master_port": null,
                "port": 8000,
                "reserved_memory_bytes": 4096,
                "memory_floor_bytes": 0
            }
        },
        "artifacts": [
            {
                "selection_id": "primary",
                "file_id": "config-primary",
                "path": "config.json",
                "sha256": primary,
                "size_bytes": 7,
                "roles": ["entrypoint"],
                "mount": {"target": "/models"},
                "model": {"publisher": "vonk-forge", "slug": "primary-model", "content_sha256": "e".repeat(64)}
            },
            {
                "selection_id": "secondary",
                "file_id": "config-secondary",
                "path": "config.json",
                "sha256": secondary,
                "size_bytes": 9,
                "roles": ["entrypoint"],
                "mount": {"target": "/models/secondary"},
                "model": {"publisher": "vonk-forge", "slug": "secondary-model", "content_sha256": "f".repeat(64)}
            }
        ],
        "runtime_image": {
            "image_digest": format!("sha256:{}", "1".repeat(64)),
            "local_image_config_id": format!("sha256:{}", "4".repeat(64)),
            "runtime_interface_label": "v1",
            "oci_layout_sha256": "2".repeat(64),
            "image_bytes": 4096,
            "build_id": "build-1"
        },
        "security": {
            "gpu": false,
            "network_mode": "none",
            "user": "10001:10001",
            "mounts": [
                {"source": "model", "target": "/models"},
                {"source": "outputs", "target": "/outputs"}
            ]
        },
        "topology": {"name": "solo", "node_count": 1},
        "lifecycle": {"stop_timeout_seconds": 30},
        "endpoint": {
            "port": 8000,
            "model_aliases": ["primary"], "health_path": "/v1/models"
        },
        "job": null
    })
}

pub(super) fn large_plan() -> crate::workloads::CompiledExecutionPlan {
    let mut value = compiled_plan();
    let artifacts = value["artifacts"].as_array_mut().unwrap();
    let template = artifacts[0].clone();
    for index in 2..751 {
        let mut artifact = template.clone();
        artifact["selection_id"] = json!(format!("model-{index:04}"));
        artifact["file_id"] = json!(format!("config-{index:04}"));
        artifact["model"]["slug"] = json!(format!("primary-model-{index:04}"));
        artifact["mount"]["target"] = json!(format!("/models/model-{index:04}"));
        artifacts.push(artifact);
    }
    serde_json::from_value(value).unwrap()
}

pub(super) fn persisted_installation(
    data: &Path,
) -> (String, PathBuf, crate::workloads::CompiledExecutionPlan) {
    let installation_id = "cb555393-764b-4eb6-8f15-b416d289428f".to_owned();
    let plan: crate::workloads::CompiledExecutionPlan =
        serde_json::from_value(compiled_plan()).unwrap();
    persisted_plan_installation(data, installation_id, plan)
}

pub(super) fn persisted_plan_installation(
    data: &Path,
    installation_id: String,
    plan: crate::workloads::CompiledExecutionPlan,
) -> (String, PathBuf, crate::workloads::CompiledExecutionPlan) {
    let installation = data.join("installations").join(&installation_id);
    for artifact in unique_plan_artifacts(&plan) {
        let path = installation
            .join("models")
            .join(&artifact.selection_id)
            .join(&artifact.path);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(
            &path,
            if artifact.size_bytes == 7 {
                b"primary".as_slice()
            } else {
                b"secondary".as_slice()
            },
        )
        .unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    }
    fs::write(
        installation.join("spec.json"),
        serde_json::to_vec(&plan).unwrap(),
    )
    .unwrap();
    write_installation_metadata(data, &installation, &plan).unwrap();
    (installation_id, installation, plan)
}

pub(super) fn runtime<'a>(data: &'a Path, runner: &'a NoProcess) -> OciRuntime<'a, NoProcess> {
    OciRuntime {
        runner,
        data_root: data,
    }
}

pub(super) fn authorize_installation(installation: &Path, recipe_digest: &str) {
    fs::write(installation.join("recipe-content.sha256"), recipe_digest).unwrap();
}

#[cfg(target_os = "linux")]
pub(super) fn apply_acl(path: &Path, entries: &[(u16, u16, u32)]) {
    let mut value = Vec::with_capacity(4 + entries.len() * 8);
    value.extend_from_slice(&0x0002_u32.to_le_bytes());
    for &(tag, permissions, identifier) in entries {
        value.extend_from_slice(&tag.to_le_bytes());
        value.extend_from_slice(&permissions.to_le_bytes());
        value.extend_from_slice(&identifier.to_le_bytes());
    }
    let file = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(path)
        .unwrap();
    rustix::fs::fsetxattr(
        &file,
        "system.posix_acl_access",
        &value,
        rustix::fs::XattrFlags::empty(),
    )
    .unwrap();
}

/// Put the plan's two model files in the shared store, as distribution does.
pub(super) fn stock_store(
    data: &Path,
    plan: &crate::workloads::CompiledExecutionPlan,
) -> Vec<PathBuf> {
    let root = data.join("distribution/models");
    fs::create_dir_all(&root).unwrap();
    [
        (b"primary".as_slice(), &plan.artifacts[0].sha256),
        (b"secondary".as_slice(), &plan.artifacts[1].sha256),
    ]
    .into_iter()
    .map(|(bytes, digest)| {
        let path = root.join(digest);
        fs::write(&path, bytes).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        path
    })
    .collect()
}

pub(super) const FIRST: &str = "cb555393-764b-4eb6-8f15-b416d289428f";

pub(super) const SECOND: &str = "cb555393-764b-4eb6-8f15-b416d2894290";

pub(super) fn linked_installation(
    data: &Path,
    plan: &crate::workloads::CompiledExecutionPlan,
    installation_id: &str,
) -> PathBuf {
    let installation = data.join("installations").join(installation_id);
    let mut reports = Vec::new();
    materialize_compiled_models_observed(
        data,
        plan,
        installation_id,
        &mut |done, of| reports.push((done, of)),
        &|| false,
    )
    .unwrap();
    // Linking moves no bytes, but the progress still ends complete.
    assert_eq!(reports.last().map(|(done, of)| done == of), Some(true));
    write_installation_metadata(data, &installation, plan).unwrap();
    fs::write(
        installation.join("spec.json"),
        serde_json::to_vec(plan).unwrap(),
    )
    .unwrap();
    installation
}
