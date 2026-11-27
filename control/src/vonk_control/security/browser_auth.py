"""Durable, opaque browser sessions and CLI tokens without HTTP concerns.

Both are rows of the ``sessions`` table that store only a token digest, so
either can be revoked one by one or all together.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256

from sqlalchemy import delete, or_, select, text, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from vonk_control.auth import ADMIN_ROLE, Actor, AuthError, CursorCodec
from vonk_control.db.connections import require_durable_commit
from vonk_control.models.fleet import LoginSession, SessionKind, User
from vonk_control.security.passwords import hash_password, verify_password

_LOGGER = logging.getLogger(__name__)
_ADMIN_SUBJECT = "admin"
_ADMIN_ROLE = ADMIN_ROLE
_SESSION_LIFETIME = timedelta(hours=12)
CLI_TOKEN_LIFETIME = timedelta(days=30)
_OPAQUE_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_EXPIRED_CLEANUP_LIMIT = 100
_CLI_TOKEN_LIST_LIMIT = 100
# The staged administrator password the bootstrap adopts. Only the bootstrap
# enforces it: login verification accepts whatever verifier is stored.
MIN_ADMIN_PASSWORD_CHARACTERS = 12
# A failed login answers only after this delay. It slows a sequential guesser
# and is awaited outside the verification lock, so it never queues an honest
# login behind it. There is no lockout: no failure count can refuse the
# correct password.
FAILED_LOGIN_DELAY_SECONDS = 1.0
# A login waits at most this long for its turn on the verification lock. A
# waiter past it gets a temporary answer and its retry takes a fresh turn, so
# a flood bounds every wait instead of queueing it without end.
LOGIN_LOCK_WAIT_SECONDS = 10.0


class BrowserAuthenticationError(ValueError):
    """A generic browser-authentication failure safe for an HTTP boundary."""

    def __init__(self) -> None:
        super().__init__("authentication failed")


class BrowserLoginBusyError(Exception):
    """The verification lock stayed taken past the wait deadline; retry heals."""


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
class IssuedCliToken:
    session_id: str
    expires_at: datetime
    token: str = field(repr=False)


@dataclass(frozen=True)
class CliTokenRecord:
    session_id: str
    expires_at: datetime
    revoked: bool
    #: This row authenticated the listing request.
    presented: bool


class BootstrapOutcome(StrEnum):
    """What one administrator bootstrap did with the staged password."""

    CREATED = "created"
    UNCHANGED = "unchanged"
    # The staged file differed from the stored verifier: it was adopted and
    # every session and CLI token of the administrator was revoked.
    REWRITTEN = "rewritten"
    # The staged password or the stored authority is unusable: nothing was
    # written, the stored verifier (if any) still governs login.
    REFUSED = "refused"


class BrowserAuthService:
    """Own browser-session lifecycle operations backed by SQLAlchemy rows."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], object],
        token_source: Callable[[], object] | None = None,
        password_check: Callable[[str, str], bool] | None = None,
        failure_delay: Callable[[], Awaitable[None]] | None = None,
        lock_wait_seconds: float = LOGIN_LOCK_WAIT_SECONDS,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._token_source = token_source or _new_token
        self._password_check = password_check or _password_matches
        self._failure_delay = failure_delay or _failed_login_delay
        # One Argon2 verification (64 MiB) at a time per process. Waiting
        # logins wait here, on the event loop: they hold no worker thread and
        # no database connection.
        self._verification = asyncio.Lock()
        self._lock_wait_seconds = lock_wait_seconds

    async def admit(self, subject: str, password: str) -> IssuedBrowserSession:
        """The HTTP login: serialized verification, then a delay on failure only.

        No failure count ever refuses a login, so the correct password succeeds
        during a flood once its turn on the lock comes. A turn that does not
        come within the wait deadline raises BrowserLoginBusyError, which the
        boundary answers as a temporary 503; the retry waits again.
        """
        try:
            async with asyncio.timeout(self._lock_wait_seconds):
                await self._verification.acquire()
        except TimeoutError:
            raise BrowserLoginBusyError from None
        try:
            return await asyncio.to_thread(self.login, subject, password)
        except BrowserAuthenticationError:
            pass
        finally:
            self._verification.release()
        await self._failure_delay()
        raise BrowserAuthenticationError

    def login(self, subject: str, password: str) -> IssuedBrowserSession:
        """Verify outside any transaction, then issue the session under the row lock."""
        now = self._now()
        with self._sessions() as db:
            verifier = _eligible_verifier(
                db.scalar(select(User).where(User.subject == subject)), subject
            )
        if verifier is None or not self._password_check(verifier, password):
            raise BrowserAuthenticationError
        with self._sessions.begin() as db:
            self._cleanup_expired(db, now)
            user = db.scalar(
                select(User).where(User.subject == subject).with_for_update()
            )
            # The verifier just checked must still be the stored one: a reset
            # that committed meanwhile wins, and its revocation stands.
            if user is None or _eligible_verifier(user, subject) != verifier:
                raise BrowserAuthenticationError
            token = self._opaque_token()
            csrf = self._opaque_token()
            expires_at = now + _SESSION_LIFETIME
            row = LoginSession(
                user_id=user.id,
                digest=_digest(token),
                expires_at=expires_at,
                kind=SessionKind.BROWSER,
            )
            db.add(row)
            db.flush()
            return IssuedBrowserSession(
                identity=BrowserIdentity(
                    actor=Actor(user.subject, user.role),
                    expires_at=expires_at,
                    session_id=row.id,
                ),
                token=token,
                csrf=csrf,
            )

    def resolve(self, token: str) -> BrowserIdentity:
        """Resolve a browser cookie; a CLI token is never a cookie."""
        return self._resolve(token, SessionKind.BROWSER)

    def resolve_bearer(self, token: str) -> BrowserIdentity:
        """Resolve a CLI bearer token; a browser session is never a bearer."""
        return self._resolve(token, SessionKind.CLI)

    def _resolve(self, token: str, kind: SessionKind) -> BrowserIdentity:
        now = self._now()
        digest = self._submitted_digest(token)
        with self._sessions.begin() as db:
            row, user = self._active_session(
                db, LoginSession.digest == digest, now, kind
            )
            return BrowserIdentity(
                actor=Actor(user.subject, user.role),
                expires_at=_database_utc(row.expires_at),
                session_id=row.id,
            )

    def logout(self, token: str) -> None:
        now = self._now()
        digest = self._submitted_digest(token)
        with self._sessions.begin() as db:
            row, _ = self._active_session(
                db, LoginSession.digest == digest, now, SessionKind.BROWSER
            )
            revoke_sessions(db, now, LoginSession.id == row.id)

    def issue_cli_token(self, identity: BrowserIdentity) -> IssuedCliToken:
        """Store a new 30-day CLI token for the browser session's administrator."""
        now = self._now()
        token = self._opaque_token()
        expires_at = now + CLI_TOKEN_LIFETIME
        with self._sessions.begin() as db:
            self._cleanup_expired(db, now)
            # The minting browser session must still be live at this instant.
            session, _ = self._active_session(
                db, LoginSession.id == identity.session_id, now, SessionKind.BROWSER
            )
            row = LoginSession(
                user_id=session.user_id,
                digest=_digest(token),
                expires_at=expires_at,
                kind=SessionKind.CLI,
            )
            db.add(row)
            db.flush()
            require_durable_commit(db)
            return IssuedCliToken(row.id, expires_at, token)

    def cli_tokens(self, presented_token: str | None) -> list[CliTokenRecord]:
        """The newest unexpired CLI tokens of the administrator, at most 100.

        ``presented_token`` is the bearer of the listing request: its row is
        marked by digest, without resolving the token a second time.
        """
        now = self._now()
        presented = (
            _digest(presented_token)
            if presented_token is not None
            and _OPAQUE_TOKEN.fullmatch(presented_token) is not None
            else None
        )
        with self._sessions.begin() as db:
            rows = db.scalars(
                select(LoginSession)
                .join(User, LoginSession.user_id == User.id)
                .where(
                    User.subject == _ADMIN_SUBJECT,
                    LoginSession.kind == SessionKind.CLI,
                    LoginSession.expires_at > now,
                )
                .order_by(LoginSession.expires_at.desc(), LoginSession.id)
                .limit(_CLI_TOKEN_LIST_LIMIT)
            ).all()
            return [
                CliTokenRecord(
                    row.id,
                    _database_utc(row.expires_at),
                    row.revoked_at is not None,
                    row.digest == presented,
                )
                for row in rows
            ]

    def revoke_cli_token(self, session_id: str) -> bool:
        """Revoke one CLI token by id; False when no such token exists."""
        now = self._now()
        with self._sessions.begin() as db:
            row = db.scalar(
                select(LoginSession).where(
                    LoginSession.id == session_id,
                    LoginSession.kind == SessionKind.CLI,
                )
            )
            if row is None:
                return False
            revoke_sessions(db, now, LoginSession.id == row.id)
            return True

    def revoke_all(self) -> None:
        """Revoke every browser session and CLI token of the administrator."""
        now = self._now()
        with self._sessions.begin() as db:
            revoke_sessions(db, now)

    def _active_session(
        self,
        db: Session,
        selected: ColumnElement[bool],
        now: datetime,
        kind: SessionKind,
    ) -> tuple[LoginSession, User]:
        result = db.execute(
            select(LoginSession, User)
            .join(User, LoginSession.user_id == User.id)
            .where(selected)
        ).one_or_none()
        if result is None:
            raise BrowserAuthenticationError
        row, user = result
        if (
            row.kind != kind
            or row.revoked_at is not None
            or _database_utc(row.expires_at) <= now
            or user.disabled_at is not None
            or user.subject != _ADMIN_SUBJECT
            or user.role != _ADMIN_ROLE
        ):
            raise BrowserAuthenticationError
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

    def _submitted_digest(self, token: object) -> str:
        if not isinstance(token, str) or _OPAQUE_TOKEN.fullmatch(token) is None:
            raise BrowserAuthenticationError
        return _digest(token)

    def _now(self) -> datetime:
        now = self._clock()
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            raise ValueError(
                "browser authentication clock must return an aware datetime"
            )
        return now.astimezone(UTC)


def revoke_sessions(db: Session, now: datetime, *selected: ColumnElement[bool]) -> None:
    """The one session revocation: stamp revoked_at on the selected live rows.

    No condition selects every browser session and CLI token. Rows are never
    deleted here, so a revocation stays visible until the row expires.
    """
    db.execute(
        update(LoginSession)
        .where(LoginSession.revoked_at.is_(None), *selected)
        .values(revoked_at=now)
    )
    require_durable_commit(db)


class SessionBearerTokens:
    """The production bearer authority: a bearer is a live CLI-token row."""

    def __init__(self, browser_auth: BrowserAuthService, signing_key: bytes) -> None:
        self._browser_auth = browser_auth
        self._cursors = CursorCodec.from_signing_key(signing_key)

    def verify(self, token: str, *, now: int) -> Actor:
        # The row carries its own expiry, judged by the service clock; the
        # request clock is the stateless contract's and is not consulted.
        del now
        try:
            return self._browser_auth.resolve_bearer(token).actor
        except BrowserAuthenticationError:
            raise AuthError("token is not a live CLI token") from None

    def cursor_codec(self) -> CursorCodec:
        return self._cursors


def _eligible_verifier(user: User | None, subject: str) -> str | None:
    if (
        subject != _ADMIN_SUBJECT
        or user is None
        or user.role != _ADMIN_ROLE
        or user.disabled_at is not None
    ):
        return None
    return user.password_verifier


def _password_matches(verifier: str, password: str) -> bool:
    return verify_password(verifier, password).valid


async def _failed_login_delay() -> None:
    await asyncio.sleep(FAILED_LOGIN_DELAY_SECONDS)


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _digest(token: str) -> str:
    return sha256(token.encode("ascii")).hexdigest()


def _database_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def bootstrap_administrator(
    sessions: sessionmaker[Session], password: str
) -> BootstrapOutcome:
    """Make the staged password the administrator's, never ending startup.

    The staged file is the authority and the reset path: a different password
    replaces the stored verifier and revokes every session and CLI token. An
    unusable password or administrator authority writes nothing and is logged;
    the API keeps serving and login keeps answering from the stored verifier.
    """
    if len(password) < MIN_ADMIN_PASSWORD_CHARACTERS:
        _LOGGER.error(
            "staged administrator password is shorter than %d characters; "
            "the stored password is kept",
            MIN_ADMIN_PASSWORD_CHARACTERS,
        )
        return BootstrapOutcome.REFUSED
    with sessions.begin() as db:
        # The one fence on user authority: it serializes administrator writers,
        # including absent rows. Request paths read users without any lock.
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE"))
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
                    password_verifier=hash_password(password),
                )
            )
            return BootstrapOutcome.CREATED
        user = users[0]
        if (
            len(users) != 1
            or user.subject != _ADMIN_SUBJECT
            or user.role != _ADMIN_ROLE
        ):
            # Conflicting administrator rows: never pick one to rewrite. Login
            # refuses anything but the sole admin row, so this fails closed.
            _LOGGER.error(
                "administrator authority is inconsistent; the staged password "
                "was not applied"
            )
            return BootstrapOutcome.REFUSED
        if (
            user.password_verifier is not None
            and verify_password(user.password_verifier, password).valid
        ):
            return BootstrapOutcome.UNCHANGED
        require_durable_commit(db)
        user.password_verifier = hash_password(password)
        revoke_sessions(db, datetime.now(UTC), LoginSession.user_id == user.id)
        _LOGGER.warning(
            "administrator password replaced from the staged file; every "
            "session and CLI token was revoked"
        )
        return BootstrapOutcome.REWRITTEN
