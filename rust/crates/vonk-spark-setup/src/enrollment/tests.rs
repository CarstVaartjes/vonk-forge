#![cfg(test)]
use super::*;

fn enrollment_bootstrap_document() -> serde_json::Value {
    serde_json::json!({
        "controller_endpoint": "https://controller.example.test/",
        "enrollment_endpoint": "https://enroll.example.test/",
        "ca_fingerprint": "a".repeat(64),
        "ca_pem": "-----BEGIN CERTIFICATE-----\nY2E=\n-----END CERTIFICATE-----\n",
        "controller_address": null,
        "service_hostnames": [],
        "host_helper_authority_public_key": "b".repeat(64),
    })
}

#[test]
fn enrollment_bootstrap_uses_the_exact_generated_wire_shape() {
    let document = enrollment_bootstrap_document();
    assert!(parse_enrollment_bootstrap(&serde_json::to_vec(&document).unwrap()).is_ok());

    let mut missing_nullable = document.clone();
    missing_nullable
        .as_object_mut()
        .unwrap()
        .remove("controller_address");
    assert!(parse_enrollment_bootstrap(&serde_json::to_vec(&missing_nullable).unwrap()).is_err());

    let mut wrong_type = document.clone();
    wrong_type["service_hostnames"] = serde_json::json!("controller.example.test");
    assert!(parse_enrollment_bootstrap(&serde_json::to_vec(&wrong_type).unwrap()).is_err());

    let mut unknown = document;
    unknown["legacy_controller"] = serde_json::json!("controller.example.test");
    assert!(parse_enrollment_bootstrap(&serde_json::to_vec(&unknown).unwrap()).is_err());
}
