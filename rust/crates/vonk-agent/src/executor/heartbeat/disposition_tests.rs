use super::*;

fn refused(status: u16) -> ClientError {
    ClientError::Controller(Box::new(ControllerError::from_status(status)))
}

#[test]
fn a_definite_renewal_refusal_ends_the_fence_and_a_wait_is_retried() {
    // Wrong implementation: a renewal whose own bytes were refused
    // (400/413/422) was retried silently until the order stalled.
    for status in [400, 413, 422] {
        assert!(
            matches!(
                classify_heartbeat_failure(&refused(status)),
                HeartbeatFailure::Refused
            ),
            "HTTP {status}"
        );
    }
    for status in [401, 403] {
        assert!(matches!(
            classify_heartbeat_failure(&refused(status)),
            HeartbeatFailure::Terminal
        ));
    }
    for error in [
        refused(429),
        refused(503),
        // The Controller settles a lapsed fence before it accepts it again.
        refused(404),
        refused(409),
        refused(410),
        ClientError::Retryable,
        ClientError::Identity,
        ClientError::Local(std::io::Error::from(std::io::ErrorKind::StorageFull)),
    ] {
        assert!(matches!(
            classify_heartbeat_failure(&error),
            HeartbeatFailure::Retryable
        ));
    }
    assert!(matches!(
        classify_heartbeat_failure(&ClientError::Pin),
        HeartbeatFailure::Terminal
    ));
}
