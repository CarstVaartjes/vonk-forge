use super::responses::helper_finding_code;
use super::*;

#[derive(Debug, Error)]
pub enum HostRuntimeError {
    #[error("host runtime request storage is invalid")]
    Io(#[from] std::io::Error),
    #[error("host runtime authority is unavailable")]
    Controller(#[from] ClientError),
    #[error("host runtime helper protocol contract is invalid")]
    HelperProtocol(HelperProtocolCause),
    /// A request contract was refused for exceeding a measured bound. The limit
    /// and the observed value travel with the error so the failure evidence can
    /// name them without carrying engine-owned content.
    #[error("host runtime helper protocol bound was exceeded")]
    HelperProtocolBound {
        cause: HelperProtocolCause,
        limit: Option<u64>,
        observed: u64,
    },
    /// The helper answered, but did not confirm whether the workload started or
    /// stopped. That is an ambiguous effect that needs reconciliation, not a
    /// malformed reply, so it keeps a state of its own.
    #[error("host runtime could not confirm the workload outcome")]
    StopUncertain,
    #[error("host runtime helper rejected request: {code}")]
    HelperRejected {
        failure: Option<Box<vonk_agent_protocol::generated::HttpFailureResponse>>,
        code: HelperErrorCode,
        diagnostic: Option<String>,
        /// The rejected container's own retained output, per stream, when the
        /// helper could read it. Absence is reported, never read as empty.
        process_logs: Option<Box<FailureProcessLogs>>,
    },
}

/// The distinct contracts this agent verifies while exchanging one message with
/// the privileged helper.
///
/// Every one of these contracts was previously collapsed into a single
/// `helper_protocol_invalid` label that could not say which was violated -- the
/// live symptom of a blocked privileged start, with no helper involvement and
/// nothing to act on. That label now survives only as the fallback for a cause
/// or helper code that is not on the stable allowlist.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HelperProtocolCause {
    /// The canonical request body or the signed grant could not be encoded.
    RequestEncoding,
    /// The blocking helper-call worker failed to join.
    HelperCallJoin,
    /// The length-prefixed helper message was empty, oversized or undecodable.
    MessageFraming,
    /// The reply did not bind to the request this agent sent.
    ResponseUnbound,
    /// A helper rejection did not match the stable rejection contract.
    RejectionMalformed,
    /// An executed helper outcome did not match the executed-outcome contract.
    OutcomeMalformed,
    /// The agent-built runtime request or inspection binding failed canonical
    /// validation before any helper was called.
    RequestDocument,
    /// The agent-built request carries arguments for the wrong action.
    RequestArgumentsPresence,
    /// The typed lifecycle plan is missing, duplicated, or bound to another
    /// runtime generation.
    RequestPlanBinding,
    /// The agent-built request carries an installation identity for the wrong
    /// action.
    RequestInstallationIdentity,
    /// The agent-built request carries a canonical document larger than the
    /// bounded helper exchange reads.
    RequestBytes,
    /// A typed lifecycle plan exceeds its declared canonical byte ceiling.
    RequestPlanBytes,
    /// An agent-built request argument carries a NUL byte an exec argv cannot
    /// frame.
    RequestArgumentNulByte,
    /// The owner-only signed request file could not be established.
    RequestStorage,
    /// The host clock is before the Unix epoch.
    SystemClock,
    /// An inspection reply did not say whether the process is running.
    InspectionOutcome,
}

impl HelperProtocolCause {
    /// The stable contract code of this cause.
    pub fn code(self) -> HelperErrorCode {
        match self {
            Self::RequestEncoding => HelperErrorCode::RequestEncodingInvalid,
            Self::HelperCallJoin => HelperErrorCode::CallJoinFailed,
            Self::MessageFraming => HelperErrorCode::MessageFramingInvalid,
            Self::ResponseUnbound => HelperErrorCode::ResponseUnbound,
            Self::RejectionMalformed => HelperErrorCode::RejectionMalformed,
            Self::OutcomeMalformed => HelperErrorCode::OutcomeMalformed,
            Self::RequestDocument => HelperErrorCode::RequestDocumentInvalid,
            Self::RequestArgumentsPresence => HelperErrorCode::RequestArgumentsPresenceInvalid,
            Self::RequestPlanBinding => HelperErrorCode::RequestPlanBindingInvalid,
            Self::RequestInstallationIdentity => {
                HelperErrorCode::RequestInstallationIdentityInvalid
            }
            Self::RequestBytes => HelperErrorCode::RequestBytesInvalid,
            Self::RequestPlanBytes => HelperErrorCode::RequestPlanBytesInvalid,
            Self::RequestArgumentNulByte => HelperErrorCode::RequestArgumentNulByte,
            Self::RequestStorage => HelperErrorCode::RequestStorageInvalid,
            Self::SystemClock => HelperErrorCode::SystemClockInvalid,
            Self::InspectionOutcome => HelperErrorCode::InspectionOutcomeInvalid,
        }
    }

    /// The code this cause carries in failure evidence, in the `runtime_helper_`
    /// namespace.
    pub fn runtime_helper_code(self) -> HelperErrorCode {
        match self {
            Self::RequestEncoding => HelperErrorCode::RuntimeHelperRequestEncodingInvalid,
            Self::HelperCallJoin => HelperErrorCode::RuntimeHelperCallJoinFailed,
            Self::MessageFraming => HelperErrorCode::RuntimeHelperMessageFramingInvalid,
            Self::ResponseUnbound => HelperErrorCode::RuntimeHelperResponseUnbound,
            Self::RejectionMalformed => HelperErrorCode::RuntimeHelperRejectionMalformed,
            Self::OutcomeMalformed => HelperErrorCode::RuntimeHelperOutcomeMalformed,
            Self::RequestDocument => HelperErrorCode::RuntimeHelperRequestDocumentInvalid,
            Self::RequestArgumentsPresence => {
                HelperErrorCode::RuntimeHelperRequestArgumentsPresenceInvalid
            }
            Self::RequestPlanBinding => HelperErrorCode::RuntimeHelperRequestPlanBindingInvalid,
            Self::RequestInstallationIdentity => {
                HelperErrorCode::RuntimeHelperRequestInstallationIdentityInvalid
            }
            Self::RequestBytes => HelperErrorCode::RuntimeHelperRequestBytesInvalid,
            Self::RequestPlanBytes => HelperErrorCode::RuntimeHelperRequestPlanBytesInvalid,
            Self::RequestArgumentNulByte => HelperErrorCode::RuntimeHelperRequestArgumentNulByte,
            Self::RequestStorage => HelperErrorCode::RuntimeHelperRequestStorageInvalid,
            Self::SystemClock => HelperErrorCode::RuntimeHelperSystemClockInvalid,
            Self::InspectionOutcome => HelperErrorCode::RuntimeHelperInspectionOutcomeInvalid,
        }
    }

    /// Map one canonical request rule to the cause this agent reports. Every
    /// envelope rule keeps its own code so a refused Start names the rule and,
    /// for a measured bound, the limit and the observed value rather than one
    /// opaque label.
    pub fn from_request_rule(rule: HostRuntimeRequestRule) -> Self {
        match rule {
            HostRuntimeRequestRule::ArgumentsPresence => Self::RequestArgumentsPresence,
            HostRuntimeRequestRule::PlanBinding => Self::RequestPlanBinding,
            HostRuntimeRequestRule::InstallationIdentity => Self::RequestInstallationIdentity,
            HostRuntimeRequestRule::RequestBytes { .. } => Self::RequestBytes,
            HostRuntimeRequestRule::PlanBytes { .. } => Self::RequestPlanBytes,
            HostRuntimeRequestRule::ArgumentNulByte { .. } => Self::RequestArgumentNulByte,
            HostRuntimeRequestRule::Encoding => Self::RequestDocument,
        }
    }
}

impl HostRuntimeError {
    pub fn security_edge(&self) -> bool {
        match self {
            Self::Controller(error) => error.refused(),
            Self::HelperRejected {
                failure: Some(answer),
                ..
            } => matches!(
                answer.failure,
                vonk_agent_protocol::generated::HttpFailureResponseFailure::Refusal(_)
            ),
            _ => false,
        }
    }

    pub fn retry_after_seconds(&self) -> Option<u32> {
        match self {
            Self::Controller(error) => error.retry_after_seconds(),
            Self::HelperRejected {
                failure: Some(answer),
                ..
            } => match &answer.failure {
                vonk_agent_protocol::generated::HttpFailureResponseFailure::Transient(value) => {
                    Some(value.retry_after)
                }
                _ => None,
            },
            _ => None,
        }
    }

    /// The closed finding code this failure reports in a runtime preflight
    /// result and, spelled without the finding prefix, in admission evidence.
    pub fn finding_code(&self) -> RuntimePreflightFindingCode {
        match self {
            Self::Io(_) => RuntimePreflightFindingCode::PreflightFindingHelperIoFailed,
            Self::Controller(ClientError::Protocol) => {
                RuntimePreflightFindingCode::PreflightFindingHelperGrantUnavailable
            }
            Self::Controller(error) if error.refused() => {
                RuntimePreflightFindingCode::PreflightFindingHelperGrantUnauthorized
            }
            Self::Controller(_) => {
                RuntimePreflightFindingCode::PreflightFindingHelperGrantUnavailable
            }
            Self::HelperProtocol(cause) | Self::HelperProtocolBound { cause, .. } => {
                helper_finding_code(cause.code())
            }
            // An ambiguous stop is named for what it is, so it can never be
            // read as a malformed helper reply.
            Self::StopUncertain => RuntimePreflightFindingCode::PreflightFindingHelperStopUncertain,
            Self::HelperRejected { code, .. } => helper_finding_code(*code),
        }
    }

    /// Bounded, non-sensitive evidence for the admission receipt: the finding
    /// code's word without the finding prefix.
    pub fn preflight_code(&self) -> String {
        finding_word(self.finding_code())
    }

    /// Build the refusal for one canonical request rule, carrying the measured
    /// bound when the rule has one.
    pub(super) fn request_refusal(rule: HostRuntimeRequestRule) -> Self {
        let cause = HelperProtocolCause::from_request_rule(rule);
        match rule.bound() {
            Some((limit, observed)) => Self::HelperProtocolBound {
                cause,
                limit,
                observed,
            },
            None => Self::HelperProtocol(cause),
        }
    }

    /// The measured limit and observed value behind a refusal, when the rule
    /// measured one. Only these bounded integers ever cross; the offending
    /// argument itself never does.
    pub fn refusal_bound(&self) -> Option<(Option<u64>, u64)> {
        match self {
            Self::HelperProtocolBound {
                limit, observed, ..
            } => Some((*limit, *observed)),
            _ => None,
        }
    }

    pub fn diagnostic(&self) -> Option<&str> {
        match self {
            Self::HelperRejected { diagnostic, .. } => diagnostic.as_deref(),
            _ => None,
        }
    }

    /// The rejected container's own retained output, when the helper read it.
    pub fn process_logs(&self) -> Option<&FailureProcessLogs> {
        match self {
            Self::HelperRejected { process_logs, .. } => process_logs.as_deref(),
            _ => None,
        }
    }
}
