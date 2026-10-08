//! Response for the client boundary.

use super::*;

pub fn parse_claim_response(status: u16, body: &[u8]) -> Result<Option<AgentClaim>, ClientError> {
    match status {
        204 if body.is_empty() => Ok(None),
        200 if body.len() <= MAX_CLAIM_BODY_BYTES => {
            let claim: AgentClaim = parse_strict(body).map_err(|_| ClientError::Protocol)?;
            claim.validate().map_err(|_| ClientError::Protocol)?;
            Ok(Some(claim))
        }
        status => {
            let status_code =
                StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
            if status_code.is_success() {
                Err(ClientError::Protocol)
            } else {
                classify_status(status_code).map(|_| None)
            }
        }
    }
}

pub(super) fn classify_status(status: StatusCode) -> Result<(), ClientError> {
    if status.is_success() {
        return Ok(());
    }
    Err(ClientError::Controller(Box::new(controller_error(
        status,
        "/",
        vonk_agent_protocol::generated::AgentDiagnosticOperation::ControllerRequest.as_str(),
        None,
        None,
    ))))
}

pub(super) fn classify_response(response: &reqwest::Response) -> Result<(), ClientError> {
    if response.status().is_success() {
        return Ok(());
    }
    Err(ClientError::Controller(Box::new(
        response_controller_error(response),
    )))
}

/// Build the bounded Controller error for one non-success response.
///
/// Only the URL path, the validated error token/code headers and the retry
/// hint are captured; bodies, queries and credentials stay unread.
pub(super) fn response_controller_error(response: &reqwest::Response) -> ControllerError {
    let endpoint = response.url().path();
    let operation = format!(
        "{} {endpoint}",
        vonk_agent_protocol::generated::AgentDiagnosticOperation::ControllerRequest.as_str()
    );
    let request_id = response
        .headers()
        .get("x-request-id")
        .and_then(|value| value.to_str().ok())
        .filter(|value| valid_error_token(value))
        .map(str::to_owned);
    let code = response
        .headers()
        .get("x-vonk-error-code")
        .and_then(|value| value.to_str().ok())
        .filter(|value| valid_error_code(value))
        .map(str::to_owned);
    let mut error = controller_error(response.status(), endpoint, &operation, request_id, code);
    error.retry_after_seconds = response
        .headers()
        .get(reqwest::header::RETRY_AFTER)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| {
            value.parse::<u32>().ok().or_else(|| {
                let deadline = chrono::DateTime::parse_from_rfc2822(value).ok()?;
                let delay =
                    (deadline.with_timezone(&chrono::Utc) - chrono::Utc::now()).num_seconds();
                u32::try_from(delay.max(0)).ok()
            })
        });
    error
}

/// Digest one Controller validation problem into a bounded refusal reason.
///
/// The published shape is `{"detail": str, "issues": [{"type", "loc", "msg"}]}`
/// with an optional `context`.  Only structural facts are kept: the detail
/// line, and each retained issue's location and error type.  Messages are
/// dropped because the location and type already name the field and the rule,
/// and dropping them keeps submitted content and validator input out of the
/// durable record.
///
/// A union payload fails every branch, so the Controller can report a hundred
/// issues for one bad field.  A constraint failure is therefore reported in
/// preference to the shape mismatches of branches that never applied, only a
/// bounded number of issues is kept, and the rest are counted.  Anything that
/// does not match the declared shape yields `None` rather than a guess.
pub(super) fn controller_rejection_digest(body: &[u8]) -> Option<String> {
    // A request-validation problem carries issues; any other bounded refusal
    // carries only its detail line.
    let (detail, issues) = match parse_strict::<RequestValidationProblem>(body) {
        Ok(problem) => (problem.detail, problem.issues),
        Err(_) => (
            parse_strict::<BoundedErrorResponse>(body).ok()?.detail,
            Vec::new(),
        ),
    };
    let detail = Some(detail)
        .filter(|detail| !detail.is_empty() && detail.len() <= MAX_REJECTION_CONTEXT_CHARS)
        .map(|detail| sanitize_text(&detail));
    let mut specific = Vec::new();
    let mut structural = Vec::new();
    let reported = issues.len();
    for issue in &issues {
        let location = issue
            .loc
            .iter()
            .map(render_rejection_location)
            .collect::<Vec<_>>()
            .join(".");
        if location.is_empty() {
            continue;
        }
        let kind = Some(issue.type_.as_str())
            .filter(|kind| valid_error_token(kind))
            .unwrap_or("invalid");
        let rendered = format!("{location} ({kind})");
        if STRUCTURAL_ERROR_TYPES.contains(&kind) {
            structural.push(rendered);
        } else {
            specific.push(rendered);
        }
    }
    // A union payload fails every branch at once, so a shape mismatch against
    // a branch that never applied would otherwise bury the single constraint
    // the producer actually broke.  Shape mismatches are reported only when
    // there is no constraint failure to report instead.
    let ranked = if specific.is_empty() {
        structural
    } else {
        specific
    };
    let kept = ranked.len().min(MAX_REJECTION_ISSUES);
    if kept == 0 && detail.is_none() {
        return None;
    }
    let mut digest = detail.unwrap_or_else(|| "request is invalid".to_owned());
    if kept > 0 {
        digest.push_str(": ");
        digest.push_str(&ranked[..kept].join("; "));
        if reported > kept {
            digest.push_str(&format!("; +{} more", reported - kept));
        }
    }
    Some(
        sanitize_text(&digest)
            .chars()
            .take(MAX_REJECTION_CONTEXT_CHARS)
            .collect(),
    )
}

/// Render one reported location segment as bounded, sanitized text.
pub(super) fn render_rejection_location(segment: &RequestValidationIssueLocItem) -> String {
    match segment {
        RequestValidationIssueLocItem::String(text) => sanitize_text(text)
            .chars()
            .take(MAX_REJECTION_LOCATION_CHARS)
            .collect(),
        RequestValidationIssueLocItem::VonkInteger(index) => {
            // A validated integer is path authority, not an opaque credential.
            // Brackets distinguish it from string members and retain the final
            // digest sanitizer without hiding an otherwise valid wide index.
            let bounded: String = index
                .to_string()
                .chars()
                .take(MAX_REJECTION_LOCATION_CHARS - 2)
                .collect();
            format!("[{bounded}]")
        }
    }
}

pub(super) fn controller_error(
    status: StatusCode,
    endpoint: &str,
    operation: &str,
    request_id: Option<String>,
    supplied_code: Option<String>,
) -> ControllerError {
    let status_code = status.as_u16();
    let code = supplied_code.unwrap_or_else(|| match status_code {
        401 => SecurityRefusalReason::ControllerAuthenticationRequired.to_string(),
        403 => SecurityRefusalReason::ControllerRequestRejected.to_string(),
        408 => ControllerErrorCode::ControllerTimeout.to_string(),
        429 => ControllerErrorCode::ControllerRateLimited.to_string(),
        500..=599 => ControllerErrorCode::ControllerUnavailable.to_string(),
        _ => format!("{}{status_code}", ControllerErrorCode::ControllerHttp),
    });
    let decision = match status_code {
        408 | 429 | 500..=599 => {
            vonk_agent_protocol::generated::AgentClientDecision::Retry.as_str()
        }
        401 | 403 => vonk_agent_protocol::generated::AgentClientDecision::Exit.as_str(),
        _ => vonk_agent_protocol::generated::AgentClientDecision::Defer.as_str(),
    };
    ControllerError {
        operation: operation.to_owned(),
        endpoint: endpoint.to_owned(),
        status: status_code,
        code,
        request_id,
        decision,
        retry_after_seconds: None,
        summary: None,
    }
}

pub(super) fn valid_error_token(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._:-".contains(&byte))
}

pub(super) fn valid_error_code(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value.bytes().enumerate().all(|(index, byte)| {
            byte.is_ascii_lowercase()
                || byte.is_ascii_digit()
                || (index > 0 && b"_.:-".contains(&byte))
        })
}

pub(super) fn is_rotation_conflict(body: &[u8]) -> bool {
    const STAGED: &str = "a different certificate rotation is already staged";
    if let Ok(refusal) = parse_strict::<BoundedErrorResponse>(body) {
        return refusal.detail == STAGED
            || refusal.context.is_some_and(|context| {
                vocabulary::is(
                    &context.code,
                    SecurityRefusalReason::AgentCertificateRotationConflict,
                )
            });
    }
    // An older Controller answered with a bare code and/or detail.
    parse_strict::<ControllerRefusalBody>(body).is_ok_and(|refusal| {
        refusal.detail.as_deref() == Some(STAGED)
            || refusal.code.as_deref().is_some_and(|code| {
                vocabulary::is(
                    code,
                    SecurityRefusalReason::AgentCertificateRotationConflict,
                ) || code == "agent_certificate_rotation_conflict"
            })
    })
}

pub(super) async fn bounded_body(response: reqwest::Response) -> Result<Vec<u8>, ClientError> {
    bounded_body_limit(response, MAX_BODY_BYTES).await
}

pub(super) async fn bounded_claim_body(
    response: reqwest::Response,
) -> Result<Vec<u8>, ClientError> {
    bounded_body_limit(response, MAX_CLAIM_BODY_BYTES).await
}

pub(super) async fn bounded_body_limit(
    mut response: reqwest::Response,
    maximum_bytes: usize,
) -> Result<Vec<u8>, ClientError> {
    if response
        .content_length()
        .is_some_and(|length| length > maximum_bytes as u64)
    {
        return Err(ClientError::Protocol);
    }
    let mut body = Vec::with_capacity(response.content_length().unwrap_or(0) as usize);
    let deadline = tokio::time::Instant::now() + CONTROLLER_REQUEST_TIMEOUT;
    while let Some(chunk) = tokio::time::timeout_at(deadline, response.chunk())
        .await
        .map_err(|_| ClientError::Retryable)??
    {
        if body.len().saturating_add(chunk.len()) > maximum_bytes {
            return Err(ClientError::Protocol);
        }
        body.extend_from_slice(&chunk);
    }
    Ok(body)
}

#[cfg(test)]
mod tests;
