from __future__ import annotations

import asyncio
import base64
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from vonk_control.auth import Actor
from vonk_control.model_primitives import Base
from vonk_control.models.fleet import LoginSession, User
from vonk_control.security.browser_auth import (
    BootstrapOutcome,
    BrowserAuthenticationError,
    BrowserAuthService,
    BrowserLoginBusyError,
    bootstrap_administrator,
)
from vonk_control.security.passwords import hash_password, verify_password

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

    def source() -> str:
        return next(iterator)

    return BrowserAuthService(
        sessions,
        clock=clock,
        token_source=source,
    )


def add_admin(
    sessions: sessionmaker[Session],
    *,
    disabled_at: datetime | None = None,
    verifier: str | object | None = DEFAULT_VERIFIER,
) -> User:
    user = User(
        subject="admin",
        role="administrator",
        disabled_at=disabled_at,
        password_verifier=(
            hash_password(ADMIN_PASSWORD) if verifier is DEFAULT_VERIFIER else verifier
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


def test_bootstrap_administrator_creates_once_and_verifies_on_restart(
    sessions: sessionmaker[Session],
) -> None:
    assert bootstrap_administrator(sessions, ADMIN_PASSWORD) == (
        BootstrapOutcome.CREATED
    )
    assert bootstrap_administrator(sessions, ADMIN_PASSWORD) == (
        BootstrapOutcome.UNCHANGED
    )

    with sessions() as db:
        user = db.scalar(select(User))
    assert user is not None
    assert user.subject == "admin"
    assert user.role == "administrator"
    assert user.password_verifier is not None
    assert verify_password(user.password_verifier, ADMIN_PASSWORD).valid
    service = browser_auth(sessions, Clock(), opaque(33), opaque(34))
    issued = service.login("admin", ADMIN_PASSWORD)
    assert service.resolve(issued.token).actor == Actor("admin", "administrator")


def test_a_changed_staged_password_replaces_the_old_and_revokes_sessions(
    sessions: sessionmaker[Session],
) -> None:
    """Catches the crash-loop on a mismatch and a reset that leaves old logins."""
    bootstrap_administrator(sessions, ADMIN_PASSWORD)
    service = browser_auth(sessions, Clock(), opaque(35), opaque(36))
    issued = service.login("admin", ADMIN_PASSWORD)

    assert bootstrap_administrator(sessions, "a new staged password") == (
        BootstrapOutcome.REWRITTEN
    )

    with pytest.raises(BrowserAuthenticationError):
        service.resolve(issued.token)
    with pytest.raises(BrowserAuthenticationError):
        service.login("admin", ADMIN_PASSWORD)
    fresh = browser_auth(sessions, Clock(), opaque(37), opaque(38))
    assert fresh.login("admin", "a new staged password").identity.actor == Actor(
        "admin", "administrator"
    )


def test_a_short_staged_password_keeps_the_stored_one(
    sessions: sessionmaker[Session],
) -> None:
    """Catches a hand-edited short password being adopted or ending startup."""
    bootstrap_administrator(sessions, ADMIN_PASSWORD)

    assert bootstrap_administrator(sessions, "x" * 11) == BootstrapOutcome.REFUSED

    service = browser_auth(sessions, Clock(), opaque(39), opaque(40))
    assert service.login("admin", ADMIN_PASSWORD).identity.actor.subject == "admin"


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
    assert bootstrap_administrator(sessions, ADMIN_PASSWORD) == (
        BootstrapOutcome.REFUSED
    )
    with sessions() as db:
        assert tuple(db.scalars(select(User.subject))) == ("other-admin",)


def test_login_issues_independent_session_and_csrf_tokens(
    sessions: sessionmaker[Session],
) -> None:
    """Reusing one raw value for session and CSRF tokens must not be possible."""
    add_admin(sessions)
    service = browser_auth(sessions, Clock(), opaque(27), opaque(28))

    issued = service.login("admin", ADMIN_PASSWORD)

    assert issued.token == opaque(27)
    assert issued.csrf == opaque(28)
    assert issued.token != issued.csrf


@pytest.mark.parametrize(("field", "value"), [("role", "viewer"), ("subject", "other")])
def test_resolution_rejects_session_when_exact_browser_authority_changes(
    sessions: sessionmaker[Session], field: str, value: str
) -> None:
    """Returning a changed subject or role would retain browser authority."""
    add_admin(sessions)
    service = browser_auth(sessions, Clock(), opaque(29), opaque(30))
    issued = service.login("admin", ADMIN_PASSWORD)
    with sessions.begin() as db:
        user = db.scalar(select(User).where(User.subject == "admin"))
        assert user is not None
        setattr(user, field, value)

    with pytest.raises(BrowserAuthenticationError):
        service.resolve(issued.token)


def _threaded_sessions() -> sessionmaker[Session]:
    # admit() verifies on a worker thread: one in-memory database for all.
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _admitting_service(
    sessions: sessionmaker[Session],
    *,
    password_check: Callable[[str, str], bool] | None = None,
    failure_delay: Callable[[], Awaitable[None]] | None = None,
    lock_wait_seconds: float = 30.0,
) -> BrowserAuthService:
    tokens = iter(opaque(byte) for byte in range(40, 60))
    return BrowserAuthService(
        sessions,
        clock=Clock(),
        token_source=lambda: next(tokens),
        password_check=password_check,
        failure_delay=failure_delay,
        lock_wait_seconds=lock_wait_seconds,
    )


async def _until(condition: Callable[[], bool]) -> None:
    for _ in range(3000):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was never reached")


def test_only_a_failed_admission_waits_out_the_failure_delay() -> None:
    """Delaying every login, or none, would either slow the owner or a guesser."""
    sessions = _threaded_sessions()
    add_admin(sessions)
    delays: list[None] = []

    async def delay() -> None:
        delays.append(None)

    service = _admitting_service(sessions, failure_delay=delay)

    async def scenario() -> None:
        for _ in range(3):
            with pytest.raises(BrowserAuthenticationError):
                await service.admit("admin", "wrong password")
        assert len(delays) == 3
        issued = await service.admit("admin", ADMIN_PASSWORD)
        assert service.resolve(issued.token).actor == Actor("admin", "administrator")
        assert len(delays) == 3

    asyncio.run(scenario())


def test_verifications_run_one_at_a_time_and_waiters_hold_no_thread() -> None:
    """Parallel Argon2 runs, or logins parked on worker threads, exhaust the API."""
    sessions = _threaded_sessions()
    add_admin(sessions)
    release = threading.Event()
    guard = threading.Lock()
    running = [0]
    peak = [0]
    entered = [0]

    def slow_check(_verifier: str, password: str) -> bool:
        with guard:
            running[0] += 1
            entered[0] += 1
            peak[0] = max(peak[0], running[0])
        assert release.wait(timeout=30)
        with guard:
            running[0] -= 1
        return password == ADMIN_PASSWORD

    async def no_delay() -> None:
        return None

    service = _admitting_service(
        sessions, password_check=slow_check, failure_delay=no_delay
    )

    async def scenario() -> None:
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=2)
        )
        flood = [
            asyncio.create_task(service.admit("admin", f"wrong {index}"))
            for index in range(4)
        ]
        owner = asyncio.create_task(service.admit("admin", ADMIN_PASSWORD))
        await _until(lambda: entered[0] == 1)
        # One worker runs the blocked verification; the four waiters hold
        # none, so the second worker is still free for any other request.
        probe = asyncio.to_thread(lambda: "free")
        assert await asyncio.wait_for(probe, timeout=30) == "free"
        assert entered[0] == 1
        release.set()
        outcomes = await asyncio.gather(*flood, return_exceptions=True)
        issued = await owner
        assert all(isinstance(o, BrowserAuthenticationError) for o in outcomes)
        assert service.resolve(issued.token).actor == Actor("admin", "administrator")

    asyncio.run(scenario())
    assert entered[0] == 5
    assert peak[0] == 1


def test_a_pending_failure_delay_does_not_queue_the_next_login() -> None:
    """Holding the verification lock through the delay would queue the owner."""
    sessions = _threaded_sessions()
    add_admin(sessions)

    async def scenario() -> None:
        delaying = asyncio.Event()
        finish = asyncio.Event()

        async def delay() -> None:
            delaying.set()
            await finish.wait()

        service = _admitting_service(sessions, failure_delay=delay)
        failed = asyncio.create_task(service.admit("admin", "wrong password"))
        await asyncio.wait_for(delaying.wait(), timeout=30)
        issued = await asyncio.wait_for(
            service.admit("admin", ADMIN_PASSWORD), timeout=30
        )
        assert service.resolve(issued.token).actor == Actor("admin", "administrator")
        assert not failed.done()
        finish.set()
        with pytest.raises(BrowserAuthenticationError):
            await failed

    asyncio.run(scenario())


def test_a_login_waiting_past_the_deadline_is_busy_and_its_retry_succeeds() -> None:
    """An unbounded wait queues the owner behind a flood; a refusal must heal."""
    sessions = _threaded_sessions()
    add_admin(sessions)
    release = threading.Event()
    entered = threading.Event()

    def held_check(_verifier: str, password: str) -> bool:
        entered.set()
        assert release.wait(timeout=30)
        return password == ADMIN_PASSWORD

    async def no_delay() -> None:
        return None

    service = _admitting_service(
        sessions,
        password_check=held_check,
        failure_delay=no_delay,
        lock_wait_seconds=0.05,
    )

    async def scenario() -> None:
        holder = asyncio.create_task(service.admit("admin", "wrong password"))
        await _until(entered.is_set)
        with pytest.raises(BrowserLoginBusyError):
            await service.admit("admin", ADMIN_PASSWORD)
        release.set()
        with pytest.raises(BrowserAuthenticationError):
            await holder
        issued = await service.admit("admin", ADMIN_PASSWORD)
        assert service.resolve(issued.token).actor == Actor("admin", "administrator")

    asyncio.run(scenario())
