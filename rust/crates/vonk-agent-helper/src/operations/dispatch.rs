//! Dispatch.

use super::*;

impl<R: CommandRunner> OperationExecutor<R> {
    pub fn new(
        roots: ManagedRoots,
        release_public_key: &[u8],
        runner: R,
        required_owner_uid: Option<u32>,
    ) -> Result<Self, OperationError> {
        let release_public_key = release_public_key
            .try_into()
            .map_err(|_| OperationError::InvalidArtifact)?;
        if !roots.data.is_absolute() || !roots.incoming.starts_with(&roots.data) {
            return Err(OperationError::UnsafePath);
        }
        Ok(Self {
            roots,
            release_public_key,
            runner,
            required_owner_uid,
            package_owner_uid: required_owner_uid,
            runtime_request_owner_uid: required_owner_uid,
            package_install: Mutex::new(()),
            job_cancellation: JobCancellationFence::default(),
        })
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub fn with_package_owner(mut self, uid: u32) -> Self {
        self.package_owner_uid = Some(uid);
        self
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub fn with_runtime_request_owner(mut self, uid: u32) -> Self {
        self.runtime_request_owner_uid = Some(uid);
        self
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub fn execute(&self, operation: &HostOperation) -> Result<OperationOutcome, OperationError> {
        self.execute_for_node(operation, None)
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub fn execute_for_node(
        &self,
        operation: &HostOperation,
        observation_node_id: Option<&str>,
    ) -> Result<OperationOutcome, OperationError> {
        operation
            .validate()
            .map_err(|_| OperationError::InvalidOperation)?;
        self.require_directory(&self.roots.data)?;
        let (status, exit_code, evidence) = match operation {
            HostOperation::InstallVonkDebOperation(InstallVonkDebOperation {
                package_sha256,
                package_signature,
                rollback,
                ..
            }) => {
                self.install_package(
                    package_sha256,
                    package_signature,
                    rollback,
                    observation_node_id.ok_or(OperationError::InvalidOperation)?,
                )?;
                (HostHelperResponseStatus::PackageInstalled, None, None)
            }
            HostOperation::ConfirmPackageActivationOperation(
                ConfirmPackageActivationOperation {
                    package_sha256,
                    attempt_nonce,
                    ..
                },
            ) => {
                crate::package_rollback::Store::system()
                    .acknowledge(
                        observation_node_id.ok_or(OperationError::InvalidOperation)?,
                        package_sha256,
                        attempt_nonce,
                    )
                    .map_err(|_| OperationError::PackagePreflightFailed)?;
                (
                    HostHelperResponseStatus::PackageActivationConfirmed,
                    None,
                    None,
                )
            }
            HostOperation::ExecuteContainerRuntimeRequestOperation(
                ExecuteContainerRuntimeRequestOperation {
                    action,
                    fence,
                    request_sha256,
                    installation_id,
                    reconciliation_identity,
                    installation_intent_nonce,
                    installation_intent_ordinal,
                    start_plan_sha256,
                    stop_plan_sha256,
                    run_generation,
                    runtime_run_id,
                    runtime_target_id,
                    runtime_installation_id,
                    ..
                },
            ) => {
                let outcome = self.execute_runtime_request(
                    action,
                    RuntimeRequestGrantBinding {
                        fence,
                        installation_intent_nonce: installation_intent_nonce.as_deref(),
                        installation_intent_ordinal: *installation_intent_ordinal,
                        installation_id: installation_id.as_ref(),
                        reconciliation_identity: reconciliation_identity.as_ref(),
                        start_plan_sha256: start_plan_sha256.as_deref(),
                        stop_plan_sha256: stop_plan_sha256.as_deref(),
                        run_generation: *run_generation,
                        runtime_run_id: runtime_run_id.as_ref(),
                        runtime_target_id: runtime_target_id.as_ref(),
                        runtime_installation_id: runtime_installation_id.as_ref(),
                    },
                    request_sha256,
                );
                let (status, exit_code, evidence) = match outcome {
                    Ok(outcome) => (
                        HostHelperResponseStatus::ContainerRuntimeRequestExecuted,
                        outcome.exit_code,
                        outcome.evidence,
                    ),
                    Err(OperationError::StopUncertain) => (
                        HostHelperResponseStatus::ContainerRuntimeStopUncertain,
                        Some(124),
                        None,
                    ),
                    Err(error) => return Err(error),
                };
                (status, exit_code, evidence)
            }
        };
        Ok(OperationOutcome {
            schema_version: 1,
            status,
            exit_code: exit_code.map(i64::from),
            diagnostic: evidence.as_ref().map(|evidence| evidence.summary.clone()),
            process_logs: evidence
                .and_then(|evidence| evidence.logs)
                .map(|logs| *logs),
        })
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn require_directory(&self, path: &Path) -> Result<(), OperationError> {
        require_safe_directory(path, self.required_owner_uid)
    }
}
