"""Dependency-free development token authority shared by local tooling."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json


def issue_development_admin_token(
    *,
    signing_key: bytes,
    ttl_seconds: int,
    now: int,
) -> str:
    if len(signing_key) < 32:
        raise ValueError("signing key must contain at least 32 bytes")
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
        hmac.new(signing_key, body.encode(), hashlib.sha256).digest()
    ).rstrip(b"=")
    return f"{body}.{signature.decode()}"


__all__ = ["issue_development_admin_token"]
