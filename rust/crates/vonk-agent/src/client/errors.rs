//! Typed Controller refusals and client retry decisions.

use super::*;
pub use vonk_agent_protocol::generated::Disposition;

#[derive(Debug, Error)]
pub enum ClientError {
    #[error("agent credential could not be read")]
    CredentialRead(std::io::Error),
    /// A local file or storage operation failed (a full disk, an I/O error).
    /// It is neither a credential nor a Controller refusal: a full disk is a
    /// resource prerequisite the work waits for.
    #[error("local storage operation failed: {0}")]
    Local(#[from] std::io::Error),
    #[error("agent TLS identity is invalid")]
    Identity,
    #[error("controller transport failed")]
    Transport(#[from] reqwest::Error),
    #[error("controller rejected: {0}")]
    Controller(Box<ControllerError>),
    /// No bytes moved for a whole inactivity window; never a total budget.
    #[error("controller transfer stalled: {0}")]
    Stalled(#[from] crate::stall_window::Stalled),
    #[error("request observation is temporarily unavailable")]
    Retryable,
    /// A transfer or local observation that did not settle, with its cause: the
    /// request is observed again (a wait), and the cause reaches the evidence.
    #[error("request observation did not settle: {0}")]
    Unsettled(&'static str),
    #[error("controller protocol response is invalid")]
    Protocol,
    // The Controller did not accept this result: the attempt is no longer
    // current, or its outcome was already consumed.  It is deliberately
    // distinct from an accepted result and from a transport failure, because
    // the agent must keep evidence the Controller never confirmed rather than
    // treat the refusal as an acknowledgement.
    #[error("controller did not accept the result for this attempt")]
    ResultSuperseded,
    // The Controller refused this exact result at its ingress validation
    // boundary (HTTP 422): a definite refusal of these bytes (`FAIL`).  It is
    // about this operation's evidence, not the agent's identity or transport,
    // so the caller records the refusal durably as evidence and keeps the rest
    // of the loop alive instead of exiting.
    #[error("controller refused the result as invalid")]
    ResultRejected(Box<ControllerError>),
    #[error("controller CA pin is invalid")]
    Pin,
}

#[derive(Debug)]
pub struct ControllerError {
    pub operation: String,
    pub endpoint: String,
    pub status: u16,
    pub code: String,
    pub request_id: Option<String>,
    pub decision: &'static str,
    pub retry_after_seconds: Option<u32>,
    /// Bounded, sanitized Controller validation context, when the endpoint
    /// published one, or a line saying a published body could not be read.
    /// Never raw request bytes and never the rejected values.
    pub summary: Option<String>,
}

impl fmt::Display for ControllerError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "{} {} HTTP {} [{}]",
            self.operation, self.code, self.status, self.decision
        )?;
        if let Some(request_id) = &self.request_id {
            write!(formatter, " request_id={request_id}")?;
        }
        if self.status == 413 {
            if vocabulary::is(&self.code, ControllerErrorCode::EdgeBodyLimit) {
                write!(formatter, " edge body limit")?;
            } else if vocabulary::is(&self.code, ControllerErrorCode::ControllerRequestTooLarge) {
                write!(formatter, " Controller limit")?;
            }
        }
        Ok(())
    }
}

impl std::error::Error for ControllerError {}

impl ControllerError {
    pub fn from_status(status: u16) -> Self {
        let status = StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_REQUEST);
        controller_error(status, "/", None, None)
    }

    /// Return the bounded context to persist for a refused submission.
    ///
    /// The Controller's own validation digest names the boundary and the
    /// failing field and rule, which is what an operator needs; the status,
    /// code and request id already have their own columns and log fields, so
    /// they are only the fallback when no digest was published.  Either way the
    /// result stays bounded and free of credentials and rejected values.
    pub fn rejection_context(&self) -> String {
        let context = self.summary.clone().unwrap_or_else(|| self.to_string());
        context.chars().take(MAX_REJECTION_CONTEXT_CHARS).collect()
    }

    /// A refused exact host operation is not refusal of the agent identity.
    pub(crate) fn host_runtime_grant_denied(&self) -> bool {
        self.status == 403 && self.endpoint == "/agent/host-runtime/grant"
    }

    /// The shared contract's disposition of this HTTP refusal (the one status
    /// table, `vonk_agent_protocol.disposition`). A refused exact host
    /// operation grant is that operation's wait, never a refusal of the agent.
    pub fn disposition(&self) -> Disposition {
        if self.host_runtime_grant_denied() {
            return Disposition::Wait;
        }
        http_status_disposition(self.status)
    }

    /// Whether the Controller refused the request's own bytes (malformed,
    /// oversized or invalid): the same request can never be accepted.  Other
    /// definite answers (a missing or no longer current resource) describe
    /// the Controller's view of an owner that may still be settling.
    pub fn request_invalid(&self) -> bool {
        self.disposition() == Disposition::Fail && matches!(self.status, 400 | 413 | 422)
    }

    /// Whether this refusal asks its owner to re-observe the same request.
    /// Renewal and diagnostic decisions consume this classification too.
    pub(crate) fn retryable(&self) -> bool {
        matches!(self.status, 429 | 500..=599) || self.host_runtime_grant_denied()
    }
}

impl ClientError {
    /// Retained job evidence can be delivered after an ingress limit is
    /// repaired. This never authorizes repeating the job's process.
    pub(crate) fn output_delivery_retryable(&self) -> bool {
        self.retryable() || self.status() == Some(413)
    }

    // Unknown/protocol/local identity errors never authorize HTTP replay.
    pub fn retryable(&self) -> bool {
        matches!(
            self,
            Self::Retryable | Self::Unsettled(_) | Self::Stalled(_)
        ) || matches!(self, Self::Transport(error) if transport_retryable(error))
            || matches!(self, Self::Controller(error) if error.retryable())
    }

    /// Refusals that stop agent effects until a fresh authority check:
    /// HTTP 401/403 (including node revocation),
    /// or the Controller CA pin does not match. Other nonretryable errors
    /// surface to their owner without replaying the request.
    pub fn fatal(&self) -> bool {
        matches!(self, Self::Pin)
            || (matches!(self.status(), Some(401 | 403))
                && !matches!(self, Self::Controller(error) if error.host_runtime_grant_denied()))
    }

    pub fn retry_after_seconds(&self) -> Option<u32> {
        match self {
            Self::Controller(error) => error.retry_after_seconds,
            _ => None,
        }
    }

    /// The request owner sleeps; the transport never repeats a request itself.
    pub fn retry_delay(&self, attempt: u32, minimum: Duration, cap: Duration) -> Duration {
        use ring::rand::{SecureRandom, SystemRandom};
        let mut bytes = [0_u8; 8];
        // Entropy failure uses the cap rather than an immediate retry storm.
        let entropy = if SystemRandom::new().fill(&mut bytes).is_ok() {
            u64::from_ne_bytes(bytes)
        } else {
            u64::MAX
        };
        self.retry_delay_with_entropy(attempt, minimum, cap, entropy)
    }

    pub(super) fn retry_delay_with_entropy(
        &self,
        attempt: u32,
        minimum: Duration,
        cap: Duration,
        entropy: u64,
    ) -> Duration {
        if let Some(seconds) = self.retry_after_seconds() {
            return Duration::from_secs(u64::from(seconds)).min(cap);
        }
        let ceiling = minimum.saturating_mul(1_u32 << attempt.min(31)).min(cap);
        ceiling.mul_f64(entropy as f64 / u64::MAX as f64)
    }

    pub fn status(&self) -> Option<u16> {
        match self {
            Self::Controller(error) | Self::ResultRejected(error) => Some(error.status),
            _ => None,
        }
    }

    pub fn code(&self) -> Option<&str> {
        match self {
            Self::Controller(error) | Self::ResultRejected(error) => Some(&error.code),
            _ => None,
        }
    }

    pub fn request_id(&self) -> Option<&str> {
        match self {
            Self::Controller(error) | Self::ResultRejected(error) => error.request_id.as_deref(),
            _ => None,
        }
    }

    /// The inactivity evidence of a transfer that stopped moving bytes.
    pub fn stalled(&self) -> Option<&crate::stall_window::Stalled> {
        match self {
            Self::Stalled(stalled) => Some(stalled),
            _ => None,
        }
    }

    /// Return the safe transport boundary when reqwest reliably identified it.
    /// A broad `connect` label is preferred to guessing DNS, TCP, or TLS.
    pub fn transport_kind(&self) -> Option<&'static str> {
        match self {
            Self::Stalled(_) => {
                Some(vonk_agent_protocol::generated::AgentTransportKind::Timeout.as_str())
            }
            Self::Transport(error) if error.is_timeout() => {
                Some(vonk_agent_protocol::generated::AgentTransportKind::Timeout.as_str())
            }
            Self::Transport(error) if error.is_connect() => {
                Some(vonk_agent_protocol::generated::AgentTransportKind::Connect.as_str())
            }
            Self::Transport(error) if error.is_body() => {
                Some(vonk_agent_protocol::generated::AgentTransportKind::Body.as_str())
            }
            Self::Transport(error) if error.is_decode() => {
                Some(vonk_agent_protocol::generated::AgentTransportKind::Protocol.as_str())
            }
            Self::Transport(_) => {
                Some(vonk_agent_protocol::generated::AgentTransportKind::Unknown.as_str())
            }
            _ => None,
        }
    }

    /// Return only the URL path captured by reqwest; queries and credentials
    /// are intentionally unavailable to callers of the diagnostic surface.
    pub fn endpoint(&self) -> Option<&str> {
        match self {
            Self::Controller(error) | Self::ResultRejected(error) => Some(&error.endpoint),
            _ => None,
        }
    }

    pub fn transport_endpoint(&self) -> Option<String> {
        match self {
            Self::Transport(error) => error.url().map(|url| url.path().to_owned()),
            _ => None,
        }
    }

    /// What this failure does to the work it interrupted: the one verdict,
    /// derived from the same facts as [`Self::decision`]. A wait never spends
    /// a retry and never ends work; a definite failure ends on its first
    /// occurrence with its evidence; only an unconfirmed effect is observed.
    pub fn disposition(&self) -> Disposition {
        match self {
            Self::Pin => Disposition::Security,
            // The rotation lane may be replacing the files: a local
            // observation, never the Controller's refusal of this agent.
            Self::CredentialRead(_) | Self::Identity => Disposition::Wait,
            Self::Controller(error) => error.disposition(),
            Self::ResultRejected(_) | Self::ResultSuperseded => Disposition::Fail,
            Self::Retryable | Self::Unsettled(_) | Self::Stalled(_) => Disposition::Wait,
            Self::Transport(error) if transport_retryable(error) => Disposition::Wait,
            Self::Transport(_) | Self::Protocol => Disposition::Observe,
            Self::Local(error) => local_disposition(error),
        }
    }

    /// Whether the Controller refused this agent's authority, or the exact host
    /// operation's grant: the security arm plus a denied grant.
    pub fn authority_refused(&self) -> bool {
        self.disposition() == Disposition::Security
            || matches!(self, Self::Controller(error) if error.host_runtime_grant_denied())
    }

    pub fn decision(&self) -> &'static str {
        if self.retryable() {
            vonk_agent_protocol::generated::AgentClientDecision::Retry.as_str()
        } else if matches!(self, Self::ResultRejected(_)) {
            vonk_agent_protocol::generated::AgentClientDecision::Record.as_str()
        } else if self.fatal() {
            vonk_agent_protocol::generated::AgentClientDecision::Exit.as_str()
        } else {
            vonk_agent_protocol::generated::AgentClientDecision::Defer.as_str()
        }
    }
}

/// The Controller route a diagnostic names, as its path segments joined by
/// `.` (`/agent/host-runtime/grant` is `agent.host-runtime.grant`); `None` for
/// the root.
///
/// The Controller's result ingress refuses path-like free text, and a refused
/// diagnostic costs its whole optional evidence group (diagnostic, stage and
/// helper code), so no rendered route may read as a filesystem path.
pub(crate) fn endpoint_name(path: &str) -> Option<String> {
    let name = path
        .split(['/', '\\'])
        .filter(|segment| !segment.is_empty())
        .collect::<Vec<_>>()
        .join(".");
    (!name.is_empty()).then_some(name)
}

/// The one HTTP status table, mirrored from the shared contract
/// (`HTTP_STATUS_DISPOSITIONS`); `tests/failure_disposition.rs` holds both to
/// the exported fixture.
pub fn http_status_disposition(status: u16) -> Disposition {
    match status {
        401 | 403 => Disposition::Security,
        429 => Disposition::Wait,
        // A conflict included: definite for this exact request, which its
        // owner re-plans as a fresh one.
        400..=499 => Disposition::Fail,
        500..=599 => Disposition::Wait,
        _ => Disposition::Observe,
    }
}

/// A full disk or quota is a resource the work waits for, and so is any local
/// condition that clears by itself (a busy or briefly unavailable device, a
/// file another step is replacing). Only a refusal the same input meets again
/// (permission, a read-only filesystem, an invalid path or bytes) is definite.
pub fn local_disposition(error: &std::io::Error) -> Disposition {
    use std::io::ErrorKind;
    match error.kind() {
        ErrorKind::PermissionDenied
        | ErrorKind::ReadOnlyFilesystem
        | ErrorKind::InvalidInput
        | ErrorKind::InvalidData
        | ErrorKind::InvalidFilename
        | ErrorKind::IsADirectory
        | ErrorKind::NotADirectory
        | ErrorKind::CrossesDevices
        | ErrorKind::Unsupported => Disposition::Fail,
        _ => Disposition::Wait,
    }
}

/// Whether a local failure is a full disk or quota: the resource prerequisite.
pub fn local_storage_full(error: &ClientError) -> bool {
    matches!(
        error,
        ClientError::Local(io)
            if matches!(io.kind(), std::io::ErrorKind::StorageFull | std::io::ErrorKind::QuotaExceeded)
    )
}

fn transport_retryable(error: &reqwest::Error) -> bool {
    use std::error::Error;
    if error.is_timeout() || error.is_connect() || error.is_body() || error.is_request() {
        return true;
    }
    // reqwest labels a truncated response body as Decode. Identify the actual
    // I/O cause rather than retrying arbitrary decoding/protocol errors.
    std::iter::successors(error.source(), |&cause| cause.source())
        .filter_map(|cause| cause.downcast_ref::<std::io::Error>())
        .any(|io| {
            matches!(
                io.kind(),
                std::io::ErrorKind::UnexpectedEof
                    | std::io::ErrorKind::ConnectionReset
                    | std::io::ErrorKind::ConnectionAborted
                    | std::io::ErrorKind::BrokenPipe
                    | std::io::ErrorKind::TimedOut
            )
        })
}
