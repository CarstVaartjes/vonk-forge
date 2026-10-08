//! Rootless, typed recipe image build execution.

use std::{
    fmt,
    fs::{self, File},
    io::{Read, Seek, SeekFrom, Write},
    os::unix::{
        ffi::OsStrExt,
        fs::{MetadataExt, PermissionsExt},
    },
    path::Path,
    time::{Duration, Instant},
};

use sha2::{Digest, Sha256};
use tempfile::{Builder, TempDir};
use thiserror::Error;
use uuid::Uuid;
use vonk_agent_protocol::generated::{FailureStage, RuntimePreflightFindingCode};
use vonk_agent_protocol::{
    RecipeBuildAdapter, RecipeBuildCleanupEvidence, RecipeBuildCleanupRequest, RecipeBuildEvidence,
    RecipeBuildRequest, canonical_json, generated::RecipeBuildEnvironmentArgumentValue, hex_sha256,
};

use crate::{
    base_images::{BaseImageError, BaseImageStore},
    build_source::{BuildSourceError, materialize_source_bundle},
    inventory::available_disk_bytes,
    process::{ProcessDiskReserve, ProcessError, ProcessRunner, Program},
    source_policy::{SourcePolicyReport, dockerfile_base_images, inspect_build_source},
};

const MAX_EGRESS_BINARY_BYTES: u64 = 16 * 1024 * 1024;
const MINIMUM_BUILD_DISK_RESERVE_BYTES: u64 = 4 * 1024 * 1024 * 1024;
const MAXIMUM_BUILD_DISK_RESERVE_BYTES: u64 = 64 * 1024 * 1024 * 1024;
const MAXIMUM_ADAPTER_CONTAINERFILE_BYTES: usize = 65_536;

/// The platform contract the adaptation stage installs.  These names are the
/// agent's policy boundary: the OCI admission policy at run time requires the
/// interface label, and the two adapter labels are how a prepared image proves
/// which reviewed adaptation produced it.
const RUNTIME_INTERFACE_LABEL: &str = "ai.vonkforge.runtime-interface";
const RUNTIME_INTERFACE_LABEL_VALUE: &str = "v1";
const RUNTIME_ADAPTER_LABEL: &str = "ai.vonkforge.runtime-adapter";
const RUNTIME_ADAPTER_DIGEST_LABEL: &str = "ai.vonkforge.runtime-adapter-sha256";
/// The adaptation stage names the recipe image through an argument so the
/// adapter bytes -- and therefore their digest -- do not change per build.
const ADAPTER_RECIPE_IMAGE_ARGUMENT: &str = "VONK_RECIPE_IMAGE";
/// Recipe images are always built for DGX Spark.
const BUILD_PLATFORM: &str = "linux/arm64";

mod budget;
mod builder;
mod cleanup;
mod diagnostics;
mod egress;
mod images;
mod import;

use budget::{ensure_build_disk_available, phase_time, remaining_build_time};

pub use builder::RecipeBuilder;
pub use cleanup::cleanup_build;
use diagnostics::sanitized_process_logs;
pub use diagnostics::{PodmanBuildDiagnostic, PodmanImportDiagnostic, RecipeBuildError};
#[cfg(test)]
use egress::network_boundary_error;
use egress::{
    BuildEgress, BuildEgressStart, BuildNetwork, build_network, podman_user_service_arguments,
};
use images::{
    IMAGE_INSPECT_FORMAT, inspect_adapted_image, inspect_base_image, inspect_recipe_image,
    podman_build_arguments, podman_storage_arguments, scalar, sha256_file,
    write_adapter_containerfile,
};
use import::{
    make_owned_directories_removable, podman_import_diagnostic, podman_import_process_error,
    recipe_base_image_error,
};

pub(crate) use builder::PodmanBuildStaging;
pub(crate) use images::podman_storage_arguments_with_cgroup_manager;
pub(crate) use import::podman_build_diagnostic;
