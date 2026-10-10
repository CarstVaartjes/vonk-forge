//! Typed Controller refusals and client retry decisions.

use super::*;

#[derive(Debug, Error)]
pub enum ClientError {
    #[error("agent credential could not be read")]
    CredentialRead(#[from] std::io::Error),
    #[error("agent TLS identity is invalid")]
    Identity,
    #[error("controller transport failed")]
    Transport(#[from] reqwest::Error),
    #[error("controller rejected: {0}")]
    Controller(Box<ControllerError>),
    #[error("request observation is temporarily unavailable")]
    Retryable,
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
    // boundary (HTTP 422).  Re-sending the same bytes cannot succeed, but the
    // refusal is about this operation's evidence, not about the agent's
    // identity or transport, so the caller records the bounded refusal durably
    // and keeps the rest of the loop alive instead of exiting.
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
    /// published one.  Never raw request bytes and never the rejected values.
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
        Ok(())
    }
}

impl std::error::Error for ControllerError {}

impl ControllerError {
    pub fn from_status(status: u16) -> Self {
        let status = StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_REQUEST);
        controller_error(
            status,
            "/",
            vonk_agent_protocol::generated::AgentDiagnosticOperation::ControllerRequest.as_str(),
            None,
            None,
        )
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

    /// Whether this refusal is the Controller asking for the same request again.
    ///
    /// One owner: the renewal loop classifies a failed heartbeat against this
    /// exact set rather than restating the statuses, so a change here cannot
    /// leave the two disagreeing.
    pub(crate) fn retryable(&self) -> bool {
        matches!(self.status, 429 | 500..=599)
    }
}

impl ClientError {
    // Unknown/protocol/local identity errors never authorize HTTP replay.
    pub fn retryable(&self) -> bool {
        matches!(self, Self::Retryable)
            || matches!(self, Self::Transport(error) if transport_retryable(error))
            || matches!(self, Self::Controller(error) if error.retryable())
    }

    /// The only errors that end the agent process: the Controller refused
    /// this agent's authority (HTTP 401/403, which includes node revocation),
    /// or the Controller CA pin does not match. Other nonretryable errors
    /// surface to their owner without replaying the request.
    pub fn fatal(&self) -> bool {
        matches!(self, Self::Pin) || matches!(self.status(), Some(401 | 403))
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

    /// Return the safe transport boundary when reqwest reliably identified it.
    /// A broad `connect` label is preferred to guessing DNS, TCP, or TLS.
    pub fn transport_kind(&self) -> Option<&'static str> {
        match self {
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
