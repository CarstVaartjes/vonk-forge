"""A stateless bearer fake for tests that exercise roles, not token storage.

Production has no self-contained token: a bearer is a revocable CLI-token
session row (``SessionBearerTokens``). Route tests still need viewer and
operator actors the single-administrator store cannot hold, so this fake
signs ``{sub, role, exp}`` with the test key and satisfies ``BearerTokens``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from vonk_control.auth import Actor, AuthError, CursorCodec
from vonk_control.models.fleet import LoginSession, SessionKind, User


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class TokenCodec:
    def __init__(self, signing_key: bytes) -> None:
        self._cursors = CursorCodec.from_signing_key(signing_key)
        self._key = signing_key

    def issue(self, actor: Actor, *, ttl_seconds: int, now: int) -> str:
        payload = json.dumps(
            {"sub": actor.subject, "role": actor.role, "exp": now + ttl_seconds},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        body = _encode(payload)
        signature = _encode(hmac.new(self._key, body.encode(), hashlib.sha256).digest())
        return f"{body}.{signature}"

    def verify(self, token: str, *, now: int) -> Actor:
        try:
            body, signature = token.split(".", 1)
            expected = hmac.new(self._key, body.encode(), hashlib.sha256).digest()
            if not hmac.compare_digest(_decode(signature), expected):
                raise AuthError("token signature is invalid")
            payload = json.loads(_decode(body))
            if payload["exp"] < now:
                raise AuthError("token is expired")
            return Actor(str(payload["sub"]), str(payload["role"]))
        except AuthError:
            raise
        except Exception as error:
            raise AuthError("token is malformed") from error

    def cursor_codec(self) -> CursorCodec:
        return self._cursors


def stored_cli_token(sessions: sessionmaker[Session]) -> str:
    """A live CLI-token row for the administrator: what production accepts."""
    token = secrets.token_urlsafe(32)
    with sessions.begin() as db:
        user = db.scalar(select(User).where(User.subject == "admin"))
        if user is None:
            user = User(subject="admin", role="administrator", disabled_at=None)
            db.add(user)
            db.flush()
        db.add(
            LoginSession(
                user_id=user.id,
                digest=hashlib.sha256(token.encode("ascii")).hexdigest(),
                expires_at=datetime.now(UTC) + timedelta(days=30),
                kind=SessionKind.CLI,
            )
        )
    return token
