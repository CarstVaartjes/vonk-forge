//! Operation diagnostics.

use super::*;
use vonk_agent_protocol::generated::HelperOperationCode as Code;

impl OperationError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::InvalidOperation => Code::HelperOperationInvalid.as_str(),
            Self::UnsafePath => Code::HelperUnsafePath.as_str(),
            Self::InvalidArtifact => Code::HelperArtifactInvalid.as_str(),
            Self::PackagePreparationUnavailable => {
                Code::HelperPackagePreparationUnavailable.as_str()
            }
            Self::InstallationIntentObservationRequired { .. } => {
                Code::HelperInstallationIntentObservationRequired.as_str()
            }
            Self::PackageMetadataInvalid => Code::HelperPackageMetadataInvalid.as_str(),
            Self::PackagePreflightFailed => Code::HelperPackagePreflightFailed.as_str(),
            Self::PackageInstallFailed { .. } => Code::HelperPackageInstallFailed.as_str(),
            Self::CommandFailed | Self::RuntimeJobWaitFailed { .. } => {
                Code::HelperCommandFailed.as_str()
            }
            Self::RuntimeInvocationLimitExceeded {
                string_limit: false,
                ..
            } => Code::HelperRuntimeInvocationLimitExceeded.as_str(),
            Self::RuntimeInvocationLimitExceeded {
                string_limit: true, ..
            } => Code::HelperRuntimeInvocationStringLimitExceeded.as_str(),
            Self::RuntimeInvocationLimitsUnavailable => {
                Code::HelperRuntimeInvocationLimitsUnavailable.as_str()
            }
            Self::RuntimeImageLoadFailed => Code::HelperRuntimeImageLoadFailed.as_str(),
            Self::RuntimeImageInspectFailed => Code::HelperRuntimeImageInspectFailed.as_str(),
            Self::RuntimeImageIdentityInvalid => Code::HelperRuntimeImageIdentityInvalid.as_str(),
            Self::RuntimeImageReceiptFailed => Code::HelperRuntimeImageReceiptFailed.as_str(),
            Self::RuntimeProcessExited { .. } => Code::HelperRuntimeProcessExited.as_str(),
            Self::RuntimeRunMissing => Code::HelperRuntimeRunMissing.as_str(),
            Self::RuntimeFabricUnavailable => Code::HelperRuntimeFabricUnavailable.as_str(),
            Self::StopUncertain => Code::HelperStopUncertain.as_str(),
            Self::InstallationReconciliationBusy => {
                Code::HelperInstallationReconciliationBusy.as_str()
            }
            Self::InstallationReconciliationStorageUnavailable => {
                Code::HelperInstallationReconciliationStorageUnavailable.as_str()
            }
            Self::Io(_) => Code::HelperIoFailed.as_str(),
        }
    }

    pub fn safe_detail(&self) -> &'static str {
        match self {
            Self::InvalidOperation => "managed operation is invalid",
            Self::UnsafePath => "managed path is unsafe",
            Self::InvalidArtifact => "artifact verification failed",
            Self::PackagePreparationUnavailable => "package preparation observation is unavailable",
            Self::InstallationIntentObservationRequired { .. } => {
                "current installation intent observation is required"
            }
            Self::PackageMetadataInvalid => "package metadata verification failed",
            Self::PackagePreflightFailed => "package activation prerequisites failed",
            Self::PackageInstallFailed { .. } => "package installation failed",
            Self::CommandFailed => "compiled command failed",
            Self::RuntimeJobWaitFailed { .. } => "runtime job wait failed",
            Self::RuntimeInvocationLimitExceeded {
                string_limit: false,
                ..
            } => "projected runtime invocation exceeds the host argument limit",
            Self::RuntimeInvocationLimitExceeded {
                string_limit: true, ..
            } => "projected runtime argument exceeds the host per-string limit",
            Self::RuntimeInvocationLimitsUnavailable => {
                "host runtime argument limits could not be determined"
            }
            Self::RuntimeImageLoadFailed => "runtime image load failed",
            Self::RuntimeImageInspectFailed => "runtime image inspection failed",
            Self::RuntimeImageIdentityInvalid => "runtime image identity is invalid",
            Self::RuntimeImageReceiptFailed => "runtime image receipt could not be written",
            Self::RuntimeProcessExited { .. } => "runtime process exited",
            Self::RuntimeRunMissing => "exact runtime container is absent",
            Self::RuntimeFabricUnavailable => "native fabric is unavailable or ambiguous",
            Self::StopUncertain => "one-shot runtime could not be stopped safely",
            Self::InstallationReconciliationBusy => "installation runtime reconciliation is busy",
            Self::InstallationReconciliationStorageUnavailable => {
                "installation runtime storage is temporarily unavailable"
            }
            Self::Io(_) => "host mutation I/O failed",
        }
    }
}
