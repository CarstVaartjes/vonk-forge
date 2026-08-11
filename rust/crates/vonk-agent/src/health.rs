use std::{net::IpAddr, time::Duration};

use chrono::{DateTime, Utc};
use serde::Serialize;
use thiserror::Error;

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct HealthEvidence {
    pub recipe_revision_id: String,
    pub recipe_content_sha256: String,
    pub image_digest: String,
    pub artifact_set_digest: String,
    pub model_identity: String,
    pub rank: u32,
    pub world_size: u32,
    pub endpoint: String,
    pub memory_reservation_bytes: u64,
    pub ready: bool,
}

#[derive(Debug, Error)]
pub enum HealthError {
    #[error("workload readiness deadline elapsed")]
    Deadline,
    #[error("workload health path is invalid")]
    Path,
    #[error("workload readiness transport failed")]
    Transport(#[from] reqwest::Error),
}

pub async fn wait_ready(
    address: IpAddr,
    port: u16,
    path: &str,
    deadline: DateTime<Utc>,
) -> Result<(), HealthError> {
    if port < 1024
        || !path.starts_with('/')
        || path.contains("..")
        || path.contains(['?', '#', '\0'])
    {
        return Err(HealthError::Path);
    }
    let client = reqwest::Client::builder()
        .no_proxy()
        .connect_timeout(Duration::from_secs(2))
        .timeout(Duration::from_secs(3))
        .build()?;
    let endpoint = readiness_endpoint(address, port, path);
    loop {
        if Utc::now() >= deadline {
            return Err(HealthError::Deadline);
        }
        if let Ok(response) = client.get(&endpoint).send().await
            && response.status().is_success()
            && response
                .content_length()
                .is_none_or(|length| length <= 64 * 1024)
        {
            return Ok(());
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
}

pub(crate) fn readiness_endpoint(address: IpAddr, port: u16, path: &str) -> String {
    match address {
        IpAddr::V4(address) => format!("http://{address}:{port}{path}"),
        IpAddr::V6(address) => format!("http://[{address}]:{port}{path}"),
    }
}

#[cfg(test)]
mod tests {
    use super::readiness_endpoint;

    #[test]
    fn readiness_uses_the_exact_published_address() {
        assert_eq!(
            readiness_endpoint("192.168.1.211".parse().unwrap(), 8101, "/v1/models"),
            "http://192.168.1.211:8101/v1/models"
        );
        assert_eq!(
            readiness_endpoint("fd00::10".parse().unwrap(), 8101, "/health"),
            "http://[fd00::10]:8101/health"
        );
    }
}
