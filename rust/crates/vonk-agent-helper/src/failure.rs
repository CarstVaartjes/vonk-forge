//! Security authority is distinct from unavailable host observations.
use vonk_agent_protocol::generated::{
    HelperErrorCode, HttpFailureResponse, HttpRefusal, HttpRefusalFamily, HttpRefusalReason,
    HttpTransient, HttpTransientFamily, TransientReason,
};

pub fn outcome(code: HelperErrorCode) -> HttpFailureResponse {
    let refusal = match code {
        HelperErrorCode::GrantInvalid => Some(HttpRefusalReason::InvalidSignature),
        HelperErrorCode::GrantNodeMismatch | HelperErrorCode::GrantUnauthorized => {
            Some(HttpRefusalReason::AuthorityDenied)
        }
        HelperErrorCode::PeerIdentityInvalid => Some(HttpRefusalReason::UnknownIdentity),
        _ => None,
    };
    HttpFailureResponse {
        failure: match refusal {
            Some(reason) => HttpRefusal {
                family: HttpRefusalFamily::Refusal,
                reason,
            }
            .into(),
            None => HttpTransient {
                family: HttpTransientFamily::Transient,
                reason: if code == HelperErrorCode::ConcurrencyLimit {
                    TransientReason::AdmissionBusy
                } else {
                    TransientReason::LocalStateUnavailable
                },
                retry_after: 2,
                resolution_window: 300,
                suggested_window: None,
            }
            .into(),
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use vonk_agent_protocol::generated::HttpFailureResponseFailure;

    #[test]
    fn damaged_bookkeeping_is_retryable_and_only_authority_edges_refuse() {
        for code in [
            HelperErrorCode::RequestLedgerFailed,
            HelperErrorCode::OperationIo,
            HelperErrorCode::ConcurrencyLimit,
        ] {
            let HttpFailureResponseFailure::Transient(answer) = outcome(code).failure else {
                panic!("local state cannot refuse authority");
            };
            assert!(answer.retry_after > 0);
            assert!(answer.resolution_window >= answer.retry_after);
        }
        for code in [
            HelperErrorCode::GrantInvalid,
            HelperErrorCode::GrantNodeMismatch,
            HelperErrorCode::GrantUnauthorized,
            HelperErrorCode::PeerIdentityInvalid,
        ] {
            assert!(matches!(
                outcome(code).failure,
                HttpFailureResponseFailure::Refusal(_)
            ));
        }
    }
}
