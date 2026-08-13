from __future__ import annotations

import base64
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from vonk_control.auth import Actor
from vonk_control.browser_auth import (
    BootstrapResult,
    BrowserAuthenticationError,
    BrowserAuthenticationThrottledError,
    BrowserAuthService,
    LoginRateLimiter,
)
from vonk_control.models import Base, LoginSession, User
from vonk_control.passwords import hash_password


ADMIN_PASSWORD = "correct horse battery staple"
NOW = datetime(2026, 8, 13, 9, 30, tzinfo=UTC)
DEFAULT_VERIFIER = object()


def opaque(byte: int) -> str:
    return base64.urlsafe_b64encode(bytes([byte]) * 32).decode("ascii").rstrip("=")


class Clock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class MonotonicClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


@pytest.fixture
def sessions() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def browser_auth(
    sessions: sessionmaker[Session],
    clock: Clock,
    *tokens: str,
) -> BrowserAuthService:
    iterator = iter(tokens)
    source: Callable[[], str] = lambda: next(iterator)
    return BrowserAuthService(
        sessions,
        token_signing_key=b"test-token-signing-key",
        clock=clock,
        token_source=source,
    )


def add_admin(
    sessions: sessionmaker[Session],
    *,
    disabled_at: datetime | None = None,
    verifier: str | None | object = DEFAULT_VERIFIER,
) -> User:
    user = User(
        subject="admin",
        role="administrator",
        disabled_at=disabled_at,
        password_verifier=(
            hash_password(ADMIN_PASSWORD)
            if verifier is DEFAULT_VERIFIER
            else verifier
        ),
    )
    with sessions.begin() as db:
        db.add(user)
    return user


def test_login_persists_only_a_digest_and_logout_revokes_the_session(
    sessions: sessionmaker[Session],
) -> None:
    """Removing digest-only storage or revocation must break browser sessions."""
    add_admin(sessions)
    clock = Clock()
    service = browser_auth(sessions, clock, opaque(1), opaque(2))

    issued = service.login("admin", ADMIN_PASSWORD)
    with sessions() as db:
        row = db.scalar(select(LoginSession))
    assert row is not None
    assert row.digest == sha256(issued.token.encode()).hexdigest()
    assert issued.token not in repr(row.__dict__)
    assert issued.token not in repr(issued)
    assert issued.csrf not in repr(issued)
    assert service.resolve(issued.token).actor == Actor("admin", "administrator")

    service.logout(issued.token)

    with pytest.raises(BrowserAuthenticationError):
        service.resolve(issued.token)


def test_sessions_expire_exactly_twelve_hours_after_issue(
    sessions: sessionmaker[Session],
) -> None:
    """A sliding or longer session lifetime must not authenticate at 12 hours."""
    add_admin(sessions)
    clock = Clock()
    service = browser_auth(sessions, clock, opaque(3), opaque(4))

    issued = service.login("admin", ADMIN_PASSWORD)
    assert issued.identity.expires_at == NOW + timedelta(hours=12)
    clock.value = issued.identity.expires_at

    with pytest.raises(BrowserAuthenticationError):
        service.resolve(issued.token)


def test_disabled_user_cannot_log_in(sessions: sessionmaker[Session]) -> None:
    """Dropping disabled-user checks must not issue a browser session."""
    add_admin(sessions, disabled_at=NOW)
    service = browser_auth(sessions, Clock(), opaque(5), opaque(6))

    with pytest.raises(BrowserAuthenticationError) as error:
        service.login("admin", ADMIN_PASSWORD)

    assert ADMIN_PASSWORD not in str(error.value)


def test_user_without_password_verifier_cannot_log_in(
    sessions: sessionmaker[Session],
) -> None:
    """Treating a missing verifier as valid must not issue a browser session."""
    add_admin(sessions, verifier=None)
    service = browser_auth(sessions, Clock(), opaque(7), opaque(8))

    with pytest.raises(BrowserAuthenticationError):
        service.login("admin", ADMIN_PASSWORD)


def test_malformed_token_is_rejected_before_session_lookup(
    sessions: sessionmaker[Session],
) -> None:
    """Accepting padded or non-base64url tokens would expand the cookie grammar."""
    add_admin(sessions)
    service = browser_auth(sessions, Clock(), opaque(9), opaque(10))

    with pytest.raises(BrowserAuthenticationError):
        service.resolve("not a base64url token=")


def test_digest_mismatch_does_not_resolve_a_session(
    sessions: sessionmaker[Session],
) -> None:
    """Using any digest other than the submitted token digest must fail closed."""
    user = add_admin(sessions)
    presented = opaque(11)
    with sessions.begin() as db:
        db.add(
            LoginSession(
                user_id=user.id,
                digest=sha256(opaque(12).encode()).hexdigest(),
                expires_at=NOW + timedelta(hours=1),
            )
        )
    service = browser_auth(sessions, Clock(), opaque(13), opaque(14))

    with pytest.raises(BrowserAuthenticationError):
        service.resolve(presented)


def test_login_cleanup_removes_only_a_bounded_batch_of_expired_sessions(
    sessions: sessionmaker[Session],
) -> None:
    """Unbounded expired-row cleanup must not turn one login into unbounded work."""
    user = add_admin(sessions)
    with sessions.begin() as db:
        db.add_all(
            [
                LoginSession(
                    user_id=user.id,
                    digest=sha256(f"expired-{index}".encode()).hexdigest(),
                    expires_at=NOW - timedelta(seconds=1),
                )
                for index in range(101)
            ]
        )
    service = browser_auth(sessions, Clock(), opaque(15), opaque(16))

    service.login("admin", ADMIN_PASSWORD)

    with sessions() as db:
        expired = db.scalars(
            select(LoginSession).where(LoginSession.expires_at < NOW)
        ).all()
    assert len(expired) == 1


def test_rotate_admin_revokes_every_active_session(sessions: sessionmaker[Session]) -> None:
    """Changing an administrator verifier must invalidate every prior session."""
    add_admin(sessions)
    clock = Clock()
    service = browser_auth(
        sessions,
        clock,
        opaque(17),
        opaque(18),
        opaque(19),
        opaque(20),
    )
    first = service.login("admin", ADMIN_PASSWORD)
    second = service.login("admin", ADMIN_PASSWORD)

    assert service.rotate_admin(hash_password("new administrator password")) == BootstrapResult(
        "rotated"
    )
    with sessions() as db:
        rows = db.scalars(select(LoginSession)).all()
    assert len(rows) == 2
    assert all(row.revoked_at is not None for row in rows)
    assert all(row.revoked_at.replace(tzinfo=UTC) == NOW for row in rows)
    with pytest.raises(BrowserAuthenticationError):
        service.resolve(first.token)
    with pytest.raises(BrowserAuthenticationError):
        service.resolve(second.token)


def test_bootstrap_admin_is_exact_and_idempotent(sessions: sessionmaker[Session]) -> None:
    """Changing the bootstrap identity or verifier must not silently replace authority."""
    service = browser_auth(sessions, Clock(), opaque(21), opaque(22))
    verifier = hash_password(ADMIN_PASSWORD)

    assert service.bootstrap_admin(verifier) == BootstrapResult("created")
    assert service.bootstrap_admin(verifier) == BootstrapResult("unchanged")
    with sessions() as db:
        user = db.scalar(select(User))
    assert user is not None
    assert (user.subject, user.role, user.password_verifier) == (
        "admin",
        "administrator",
        verifier,
    )


def test_bootstrap_admin_rejects_conflicting_administrator_authority(
    sessions: sessionmaker[Session],
) -> None:
    """A different administrator subject must block creation of the exact admin row."""
    with sessions.begin() as db:
        db.add(
            User(
                subject="other-admin",
                role="administrator",
                disabled_at=None,
                password_verifier=hash_password(ADMIN_PASSWORD),
            )
        )
    service = browser_auth(sessions, Clock(), opaque(23), opaque(24))

    with pytest.raises(BrowserAuthenticationError):
        service.bootstrap_admin(hash_password(ADMIN_PASSWORD))


def test_throttle_rejects_the_sixth_failed_attempt_for_one_subject(
    sessions: sessionmaker[Session],
) -> None:
    """Allowing a sixth failure in five minutes must stop before password work."""
    add_admin(sessions)
    monotonic = MonotonicClock()
    limiter = LoginRateLimiter(token_signing_key=b"test-token-signing-key", clock=monotonic)
    service = BrowserAuthService(
        sessions,
        token_signing_key=b"test-token-signing-key",
        clock=Clock(),
        token_source=lambda: opaque(25),
        rate_limiter=limiter,
    )

    for _ in range(5):
        with pytest.raises(BrowserAuthenticationError):
            service.login("admin", "wrong password")

    with pytest.raises(BrowserAuthenticationThrottledError) as error:
        service.login("admin", ADMIN_PASSWORD)
    assert str(error.value) == "authentication temporarily unavailable"


def test_throttle_rejects_a_new_subject_after_twenty_global_failures(
    sessions: sessionmaker[Session],
) -> None:
    """Dropping the global failure window must not permit the twenty-first attempt."""
    add_admin(sessions)
    monotonic = MonotonicClock()
    limiter = LoginRateLimiter(token_signing_key=b"test-token-signing-key", clock=monotonic)
    service = BrowserAuthService(
        sessions,
        token_signing_key=b"test-token-signing-key",
        clock=Clock(),
        token_source=lambda: opaque(26),
        rate_limiter=limiter,
    )

    for index in range(20):
        with pytest.raises(BrowserAuthenticationError):
            service.login(f"unknown-{index}", "wrong password")

    with pytest.raises(BrowserAuthenticationThrottledError):
        service.login("new-subject", "wrong password")


def test_throttle_success_clears_only_its_subject_failure_state() -> None:
    """Clearing every subject after one success would weaken independent limits."""
    limiter = LoginRateLimiter(
        token_signing_key=b"test-token-signing-key", clock=MonotonicClock()
    )
    for _ in range(5):
        assert limiter.admit("admin")
        assert limiter.record_failure("admin")
        assert limiter.admit("other-subject")
        assert limiter.record_failure("other-subject")

    limiter.record_success("admin")

    assert limiter.admit("admin")
    assert not limiter.admit("other-subject")


def test_throttle_evicts_expired_subject_state_before_admission() -> None:
    """Retaining expired subject entries would eventually exhaust the bounded map."""
    monotonic = MonotonicClock()
    limiter = LoginRateLimiter(token_signing_key=b"test-token-signing-key", clock=monotonic)
    assert limiter.record_failure("expired-subject")
    assert len(limiter._subjects) == 1

    monotonic.value = 300.0

    assert limiter.admit("new-subject")
    assert len(limiter._subjects) == 0


def test_throttle_never_retains_a_submitted_subject_in_plaintext() -> None:
    """Replacing keyed identifiers with raw subject keys would expose login input."""
    subject = "unkeyed-submitted-subject"
    limiter = LoginRateLimiter(
        token_signing_key=b"test-token-signing-key", clock=MonotonicClock()
    )

    assert limiter.record_failure(subject)

    assert subject not in limiter._subjects
    assert subject not in repr(limiter)

