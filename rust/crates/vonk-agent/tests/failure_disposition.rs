//! The agent reads the one HTTP status table the Controller and the CLI read.

use serde_json::Value;
use vonk_agent::client::{Disposition, Refusal, http_status_disposition, local_disposition};

#[test]
fn every_status_has_the_contract_disposition() {
    // Exported from `vonk_agent_protocol.disposition.HTTP_STATUS_DISPOSITIONS`;
    // the Controller suite fails when the export is stale.
    let table: Value = serde_json::from_str(include_str!(
        "../../../../agent_protocol/tests/fixtures/http_status_dispositions.json"
    ))
    .unwrap();
    let rows = table["statuses"].as_object().unwrap();
    assert_eq!(rows.len(), 500);
    for (status, expected) in rows {
        let status: u16 = status.parse().unwrap();
        assert_eq!(
            http_status_disposition(status).as_str(),
            expected.as_str().unwrap(),
            "HTTP {status}"
        );
    }
}

#[test]
fn a_full_disk_is_a_wait_never_a_credential_failure() {
    let full = std::io::Error::from(std::io::ErrorKind::StorageFull);
    assert_eq!(local_disposition(&full), Disposition::Wait);
    let quota = std::io::Error::from(std::io::ErrorKind::QuotaExceeded);
    assert_eq!(local_disposition(&quota), Disposition::Wait);
    let denied = std::io::Error::from(std::io::ErrorKind::PermissionDenied);
    assert_eq!(local_disposition(&denied), Disposition::Fail);
}

#[test]
fn every_definite_refusal_has_one_settling_disposition() {
    for status in [400, 422] {
        assert_eq!(Refusal::of_status(status), Refusal::InvalidBytes);
    }
    assert_eq!(Refusal::of_status(413), Refusal::TooLarge);
    for status in [404, 409, 410] {
        assert_eq!(Refusal::of_status(status), Refusal::Stale, "HTTP {status}");
    }
    for status in [401, 403, 429, 500, 503] {
        assert_eq!(Refusal::of_status(status), Refusal::Open, "HTTP {status}");
    }
    for status in 400..=499 {
        let refusal = Refusal::of_status(status);
        assert_eq!(
            refusal.settles(),
            http_status_disposition(status) == Disposition::Fail,
            "HTTP {status}"
        );
    }
}
