"""Durable, opaque browser authentication without HTTP concerns."""

from __future__ import annotations

import re
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new as hmac_new
from threading import Lock
from typing import Literal

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from .auth import Actor
from .models import LoginSession, User
from .passwords import verify_password


_ADMIN_SUBJECT = "admin"
_ADMIN_ROLE = "administrator"
_SESSION_LIFETIME = timedelta(hours=12)
_OPAQUE_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_EXPIRED_CLEANUP_LIMIT = 100
_LOGIN_WINDOW_SECONDS = 300.0
_MAX_SUBJECT_FAILURES = 5
_MAX_GLOBAL_FAILURES = 20
_MAX_TRACKED_SUBJECTS = 1_024


class BrowserAuthenticationError(ValueError):
    """A generic browser-authentication failure safe for an HTTP boundary."""

    def __init__(self) -> None:
        super().__init__("authentication failed")


class BrowserAuthenticationThrottledError(BrowserAuthenticationError):
    """A generic temporary failure used when login work must be throttled."""

    def __init__(self) -> None:
        ValueError.__init__(self, "authentication temporarily unavailable")


@dataclass(frozen=True)
class BrowserIdentity:
    actor: Actor
    expires_at: datetime
    session_id: str


@dataclass(frozen=True)
class IssuedBrowserSession:
    identity: BrowserIdentity
    token: str = field(repr=False)
    csrf: str = field(repr=False)


@dataclass(frozen=True)
class BootstrapResult:
    status: Literal["created", "unchanged", "rotated"]


class LoginRateLimiter:
    """A bounded, process-local failure limiter with opaque subject identifiers."""

    def __init__(
        self,
        *,
        token_signing_key: bytes,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(token_signing_key, bytes) or not token_signing_key:
            raise ValueError("token signing key is invalid")
        self._token_signing_key = token_signing_key
        self._clock = clock
        self._subjects: dict[str, deque[float]] = {}
        self._global: deque[float] = deque()
        self._lock = Lock()

    def admit(self, subject: str) -> bool:
        identifier = self._identifier(subject)
        now = self._clock()
        with self._lock:
            self._evict(now)
            failures = self._subjects.get(identifier)
            if len(self._global) >= _MAX_GLOBAL_FAILURES:
                return False
            if failures is not None and len(failures) >= _MAX_SUBJECT_FAILURES:
                return False
            return failures is not None or len(self._subjects) < _MAX_TRACKED_SUBJECTS

    def record_failure(self, subject: str) -> bool:
        identifier = self._identifier(subject)
        now = self._clock()
        with self._lock:
            self._evict(now)
            if len(self._global) >= _MAX_GLOBAL_FAILURES:
                return False
            failures = self._subjects.get(identifier)
            if failures is None:
                if len(self._subjects) >= _MAX_TRACKED_SUBJECTS:
                    return False
                failures = deque()
                self._subjects[identifier] = failures
            if len(failures) >= _MAX_SUBJECT_FAILURES:
                return False
            failures.append(now)
            self._global.append(now)
            return True

    def record_success(self, subject: str) -> None:
        identifier = self._identifier(subject)
        now = self._clock()
        with self._lock:
            self._evict(now)
            self._subjects.pop(identifier, None)

    def _identifier(self, subject: str) -> str:
        if not isinstance(subject, str):
            return ""
        return hmac_new(
            self._token_signing_key, subject.encode("utf-8"), sha256
        ).hexdigest()

    def _evict(self, now: float) -> None:
        cutoff = now - _LOGIN_WINDOW_SECONDS
        while self._global and self._global[0] <= cutoff:
            self._global.popleft()
        expired = [
            identifier
            for identifier, failures in self._subjects.items()
            if _discard_expired(failures, cutoff)
        ]
        for identifier in expired:
            del self._subjects[identifier]


class BrowserAuthService:
    """Own browser-session lifecycle operations backed by SQLAlchemy rows."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        token_signing_key: bytes,
        clock: Callable[[], datetime],
        token_source: Callable[[], str] | None = None,
        rate_limiter: LoginRateLimiter | None = None,
    ) -> None:
        if not isinstance(token_signing_key, bytes) or not token_signing_key:
            raise ValueError("token signing key is invalid")
        self._sessions = sessions
        self._token_signing_key = token_signing_key
        self._clock = clock
        self._token_source = token_source or _new_token
        self._rate_limiter = rate_limiter or LoginRateLimiter(
            token_signing_key=token_signing_key
        )

    def login(self, subject: str, password: str) -> IssuedBrowserSession:
        if not self._rate_limiter.admit(subject):
            raise BrowserAuthenticationThrottledError()
        now = self._now()
        issued: IssuedBrowserSession | None = None
        authenticated = False
        with self._sessions.begin() as db:
            self._cleanup_expired(db, now)
            user = db.scalar(select(User).where(User.subject == subject))
            authenticated = not (
                subject != _ADMIN_SUBJECT
                or user is None
                or user.role != _ADMIN_ROLE
                or user.disabled_at is not None
                or user.password_verifier is None
                or not verify_password(user.password_verifier, password).valid
            )
            if authenticated:
                token = self._opaque_token()
                csrf = self._opaque_token()
                expires_at = now + _SESSION_LIFETIME
                row = LoginSession(
                    user_id=user.id,
                    digest=_digest(token),
                    expires_at=expires_at,
                )
                db.add(row)
                db.flush()
                issued = IssuedBrowserSession(
                    identity=BrowserIdentity(
                        actor=Actor(user.subject, user.role),
                        expires_at=expires_at,
                        session_id=row.id,
                    ),
                    token=token,
                    csrf=csrf,
                )
        if not authenticated:
            if not self._rate_limiter.record_failure(subject):
                raise BrowserAuthenticationThrottledError()
            raise BrowserAuthenticationError()
        self._rate_limiter.record_success(subject)
        if issued is None:
            raise RuntimeError("authenticated login did not issue a session")
        return issued

    def resolve(self, token: str) -> BrowserIdentity:
        now = self._now()
        digest = self._submitted_digest(token)
        with self._sessions.begin() as db:
            row, user = self._active_session(db, digest, now)
            return BrowserIdentity(
                actor=Actor(user.subject, user.role),
                expires_at=_database_utc(row.expires_at),
                session_id=row.id,
            )

    def logout(self, token: str) -> None:
        now = self._now()
        digest = self._submitted_digest(token)
        with self._sessions.begin() as db:
            row, _ = self._active_session(db, digest, now)
            row.revoked_at = now

    def bootstrap_admin(self, verifier: str) -> BootstrapResult:
        self._verifier(verifier)
        with self._sessions.begin() as db:
            users = db.scalars(
                select(User).where(
                    or_(User.subject == _ADMIN_SUBJECT, User.role == _ADMIN_ROLE)
                )
            ).all()
            if not users:
                db.add(
                    User(
                        subject=_ADMIN_SUBJECT,
                        role=_ADMIN_ROLE,
                        disabled_at=None,
                        password_verifier=verifier,
                    )
                )
                return BootstrapResult("created")
            if len(users) != 1:
                raise BrowserAuthenticationError()
            user = users[0]
            if user.subject != _ADMIN_SUBJECT or user.role != _ADMIN_ROLE:
                raise BrowserAuthenticationError()
            if user.password_verifier != verifier:
                raise BrowserAuthenticationError()
            return BootstrapResult("unchanged")

    def rotate_admin(self, verifier: str) -> BootstrapResult:
        self._verifier(verifier)
        now = self._now()
        with self._sessions.begin() as db:
            user = db.scalar(
                select(User)
                .where(User.subject == _ADMIN_SUBJECT)
                .with_for_update()
            )
            if user is None or user.role != _ADMIN_ROLE:
                raise BrowserAuthenticationError()
            if user.password_verifier == verifier:
                return BootstrapResult("unchanged")
            user.password_verifier = verifier
            db.execute(
                update(LoginSession)
                .where(
                    LoginSession.user_id == user.id,
                    LoginSession.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            return BootstrapResult("rotated")

    def _active_session(
        self, db: Session, digest: str, now: datetime
    ) -> tuple[LoginSession, User]:
        result = db.execute(
            select(LoginSession, User)
            .join(User, LoginSession.user_id == User.id)
            .where(LoginSession.digest == digest)
        ).one_or_none()
        if result is None:
            raise BrowserAuthenticationError()
        row, user = result._tuple()
        if (
            row.revoked_at is not None
            or _database_utc(row.expires_at) <= now
            or user.disabled_at is not None
        ):
            raise BrowserAuthenticationError()
        return row, user

    def _cleanup_expired(self, db: Session, now: datetime) -> None:
        expired_ids = db.scalars(
            select(LoginSession.id)
            .where(LoginSession.expires_at < now)
            .order_by(LoginSession.expires_at)
            .limit(_EXPIRED_CLEANUP_LIMIT)
        ).all()
        if expired_ids:
            db.execute(delete(LoginSession).where(LoginSession.id.in_(expired_ids)))

    def _opaque_token(self) -> str:
        token = self._token_source()
        if not isinstance(token, str) or _OPAQUE_TOKEN.fullmatch(token) is None:
            raise RuntimeError("token source returned an invalid token")
        return token

    def _submitted_digest(self, token: str) -> str:
        if not isinstance(token, str) or _OPAQUE_TOKEN.fullmatch(token) is None:
            raise BrowserAuthenticationError()
        return _digest(token)

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("browser authentication clock must return an aware datetime")
        return now.astimezone(UTC)

    @staticmethod
    def _verifier(verifier: str) -> None:
        if not isinstance(verifier, str) or not 1 <= len(verifier) <= 255:
            raise BrowserAuthenticationError()


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _digest(token: str) -> str:
    return sha256(token.encode("ascii")).hexdigest()


def _database_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _discard_expired(failures: deque[float], cutoff: float) -> bool:
    while failures and failures[0] <= cutoff:
        failures.popleft()
    return not failures
