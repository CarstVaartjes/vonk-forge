//! Typed OCI failures with bounded, safe diagnostic context.

use super::*;

#[derive(Debug, Error)]
pub enum OciError {
    #[error("OCI subprocess failed")]
    Process(#[from] ProcessError),
    #[error("workload policy rejected the request")]
    Workload(#[from] WorkloadError),
    #[error("container runtime rejected the request")]
    Runtime,
    #[error("container image digest did not match")]
    ImageDigest,
    #[error("managed artifact content is corrupt")]
    Artifact,
    #[error("managed workload storage failed")]
    Io(#[from] std::io::Error),
    #[error("managed workload metadata is invalid")]
    Json(#[from] serde_json::Error),
    #[error("local disk or memory capacity changed after admission")]
    Capacity,
    #[error("installation reconciliation is waiting for its current local owner")]
    ReconciliationBusy,
    #[error("install {stage} failed: {source}")]
    Install {
        stage: FailureStage,
        #[source]
        source: Box<OciError>,
    },
    #[error("start {stage} failed: {source}")]
    Start {
        stage: FailureStage,
        #[source]
        source: Box<OciError>,
    },
}

impl OciError {
    pub fn safe_start_context(&self) -> (FailureStage, &'static str) {
        let (stage, source) = match self {
            Self::Start { stage, source } => (*stage, source.as_ref()),
            error => (FailureStage::Unknown, error),
        };
        let category = match source {
            Self::Io(error) if error.kind() == std::io::ErrorKind::PermissionDenied => {
                vonk_agent_protocol::generated::OciFailureCategory::StoragePermissionDenied.as_str()
            }
            Self::Io(error) if error.kind() == std::io::ErrorKind::NotFound => {
                vonk_agent_protocol::generated::OciFailureCategory::StorageNotFound.as_str()
            }
            error => error.safe_category(),
        };
        (stage, category)
    }

    pub fn safe_install_context(&self) -> (FailureStage, &'static str) {
        match self {
            Self::Install { stage, source } => (*stage, source.safe_category()),
            error => (FailureStage::Unknown, error.safe_category()),
        }
    }

    pub(crate) fn safe_category(&self) -> &'static str {
        match self {
            Self::Process(_) => {
                vonk_agent_protocol::generated::OciFailureCategory::Process.as_str()
            }
            Self::Workload(_) => {
                vonk_agent_protocol::generated::OciFailureCategory::Workload.as_str()
            }
            Self::Runtime => vonk_agent_protocol::generated::OciFailureCategory::Runtime.as_str(),
            Self::ImageDigest => {
                vonk_agent_protocol::generated::OciFailureCategory::ImageDigest.as_str()
            }
            Self::Artifact => vonk_agent_protocol::generated::OciFailureCategory::Artifact.as_str(),
            Self::Io(_) => vonk_agent_protocol::generated::OciFailureCategory::Storage.as_str(),
            Self::Json(_) => vonk_agent_protocol::generated::OciFailureCategory::Metadata.as_str(),
            Self::Capacity => vonk_agent_protocol::generated::OciFailureCategory::Capacity.as_str(),
            Self::ReconciliationBusy => {
                vonk_agent_protocol::generated::OciFailureCategory::ReconciliationBusy.as_str()
            }
            Self::Install { source, .. } | Self::Start { source, .. } => source.safe_category(),
        }
    }
}
