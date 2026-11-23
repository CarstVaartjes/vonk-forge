#![cfg(test)]
use super::{ClientError, RotationError};

#[test]
fn expired_renewal_refused_for_revocation_never_retries_or_activates() {
    let error = RotationError::Client(ClientError::Controller(Box::new(
        crate::client::ControllerError::from_status(403),
    )));
    assert!(error.fatal());
    assert!(!error.retryable());
    assert_eq!(error.decision(), "exit");
}

#[test]
fn rotation_preserves_controller_status_code_and_decision() {
    let error = RotationError::Client(ClientError::Controller(Box::new(
        crate::client::ControllerError::from_status(403),
    )));
    assert_eq!(error.code(), "controller.request_rejected");
    assert_eq!(error.decision(), "exit");
    assert!(!error.retryable());
    assert!(error.fatal());
    assert!(error.to_string().contains("HTTP 403"));
}
