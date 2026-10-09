//! Transfer for the client boundary.

use super::*;

impl AgentHttpClient {
    pub async fn download_artifact(
        &self,
        sha256: &str,
        expected_bytes: u64,
        destination: &Path,
    ) -> Result<(), ClientError> {
        self.download_content_addressed(
            &format!("/agent/artifacts/{sha256}"),
            None,
            sha256,
            expected_bytes,
            destination,
        )
        .await
    }

    pub(super) async fn download_content_addressed(
        &self,
        endpoint: &str,
        plan_digest: Option<&str>,
        sha256: &str,
        expected_bytes: u64,
        destination: &Path,
    ) -> Result<(), ClientError> {
        self.download_content_addressed_with_progress(
            endpoint,
            plan_digest,
            sha256,
            expected_bytes,
            destination,
            |_| {},
        )
        .await
    }

    pub(super) async fn download_content_addressed_with_progress<F>(
        &self,
        endpoint: &str,
        plan_digest: Option<&str>,
        sha256: &str,
        expected_bytes: u64,
        destination: &Path,
        mut progress: F,
    ) -> Result<(), ClientError>
    where
        F: FnMut(u64),
    {
        self.download_trusted_object_with_progress(
            endpoint,
            plan_digest,
            sha256,
            expected_bytes,
            ObjectPlacement {
                destination,
                managed_root: destination.parent().ok_or(ClientError::Protocol)?,
                governor: &StreamGovernor::default(),
            },
            |bytes, _| progress(bytes),
        )
        .await
    }
}
