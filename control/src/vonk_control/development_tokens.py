"""Dependency-free development token authority shared by local tooling."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

DEVELOPMENT_TOKEN_SIGNING_KEY = b"development-only-signing-key-32b"


def issue_development_admin_token(*, ttl_seconds: int, now: int) -> str:
    if ttl_seconds <= 0:
        raise ValueError("token lifetime must be positive")
    payload = json.dumps(
        {
            "sub": "development-operator",
            "role": "administrator",
            "exp": now + ttl_seconds,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    body = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    signature = base64.urlsafe_b64encode(
        hmac.new(DEVELOPMENT_TOKEN_SIGNING_KEY, body.encode(), hashlib.sha256).digest()
    ).rstrip(b"=")
    return f"{body}.{signature.decode()}"


__all__ = ["DEVELOPMENT_TOKEN_SIGNING_KEY", "issue_development_admin_token"]
