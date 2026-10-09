from typing import Literal

HttpRefusalReason = Literal['authentication_required', 'authority_denied', 'expired_credential', 'invalid_digest', 'invalid_signature', 'revoked_identity', 'tampered_token', 'unknown_identity']

HTTP_REFUSAL_REASON_VALUES: set[HttpRefusalReason] = { 'authentication_required', 'authority_denied', 'expired_credential', 'invalid_digest', 'invalid_signature', 'revoked_identity', 'tampered_token', 'unknown_identity',  }

def check_http_refusal_reason(value: str) -> HttpRefusalReason:
    if value in HTTP_REFUSAL_REASON_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {HTTP_REFUSAL_REASON_VALUES!r}")
