//! Budget.

use super::*;

pub(super) fn build_disk_envelope(request: &RecipeBuildRequest) -> Result<u64, RecipeBuildError> {
    let retained_inputs_and_output = request
        .base_image_storage_bytes
        .checked_add(request.source_bundle_bytes.into())
        .and_then(|bytes| bytes.checked_add(request.limits.output_bytes))
        .ok_or(RecipeBuildError::Evidence)?;
    Ok(request
        .limits
        .temporary_bytes
        .max(retained_inputs_and_output))
}

pub(super) fn required_build_disk(
    request: &RecipeBuildRequest,
    minimum_free_disk_bytes: u64,
) -> Result<u64, RecipeBuildError> {
    build_disk_envelope(request)?
        .checked_add(minimum_free_disk_bytes)
        .ok_or(RecipeBuildError::Evidence)
}

pub(super) fn ensure_build_disk_available(
    data_root: &Path,
    request: &RecipeBuildRequest,
) -> Result<u64, RecipeBuildError> {
    let minimum_free_disk_bytes = build_disk_reserve(data_root)?;
    let available = available_disk_bytes(data_root).map_err(|_| {
        RecipeBuildError::Process(ProcessError::Io(std::io::Error::other(
            "filesystem capacity is unavailable",
        )))
    })?;
    if available < required_build_disk(request, minimum_free_disk_bytes)? {
        return Err(ProcessError::StorageLimit.into());
    }
    Ok(minimum_free_disk_bytes)
}

pub(super) fn build_disk_reserve(data_root: &Path) -> Result<u64, RecipeBuildError> {
    let filesystem = rustix::fs::statvfs(data_root).map_err(|_| {
        RecipeBuildError::Process(ProcessError::Io(std::io::Error::other(
            "filesystem capacity is unavailable",
        )))
    })?;
    let total = filesystem
        .f_blocks
        .checked_mul(filesystem.f_frsize)
        .ok_or(RecipeBuildError::Evidence)?;
    // Two percent reaches the 64 GiB cap on the Sparks while scaling down for
    // disposable development filesystems and future smaller builders.
    Ok((total / 50)
        .clamp(
            MINIMUM_BUILD_DISK_RESERVE_BYTES,
            MAXIMUM_BUILD_DISK_RESERVE_BYTES,
        )
        .min(total / 4))
}

pub(super) fn remaining_build_time(deadline: Instant) -> Result<Duration, RecipeBuildError> {
    deadline
        .checked_duration_since(Instant::now())
        .filter(|remaining| !remaining.is_zero())
        .ok_or_else(|| ProcessError::Timeout.into())
}

pub(super) fn phase_time(
    deadline: Instant,
    maximum: Duration,
) -> Result<Duration, RecipeBuildError> {
    Ok(remaining_build_time(deadline)?.min(maximum))
}
