use std::{net::IpAddr, time::Duration};

use chrono::{DateTime, FixedOffset, Utc};
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
    lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
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
        if Utc::now() >= lease_deadline.borrow().with_timezone(&Utc) {
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
    use super::{readiness_endpoint, wait_ready};
    use chrono::{Duration as ChronoDuration, FixedOffset, Utc};
    use std::{
        io::{Read, Write},
        net::TcpListener,
        thread,
        time::Duration,
    };

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

    #[tokio::test]
    async fn readiness_honors_a_controller_renewed_deadline() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = Vec::new();
            let mut buffer = [0_u8; 1024];
            while request.windows(4).all(|value| value != b"\r\n\r\n") {
                let size = stream.read(&mut buffer).unwrap();
                assert_ne!(size, 0);
                request.extend_from_slice(&buffer[..size]);
            }
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
                .unwrap();
        });
        let original_deadline = (Utc::now() + ChronoDuration::milliseconds(50))
            .with_timezone(&FixedOffset::east_opt(0).unwrap());
        let (deadline_sender, deadline_receiver) = tokio::sync::watch::channel(original_deadline);
        tokio::time::sleep(Duration::from_millis(60)).await;
        deadline_sender.send_replace(
            (Utc::now() + ChronoDuration::seconds(30))
                .with_timezone(&FixedOffset::east_opt(0).unwrap()),
        );

        wait_ready(
            "127.0.0.1".parse().unwrap(),
            port,
            "/health",
            deadline_receiver,
        )
        .await
        .unwrap();
        server.join().unwrap();
    }
}
