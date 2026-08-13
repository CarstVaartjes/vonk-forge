from __future__ import annotations

import base64
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
import vonk_control.browser_auth as browser_auth_module
import vonk_control.db as db_module
import vonk_control.offline as offline_module
import vonk_control.settings as settings_module
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
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


def test_rotate_admin_revokes_every_active_session(
    sessions: sessionmaker[Session],
) -> None:
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

    assert service.rotate_admin(
        hash_password("new administrator password")
    ) == BootstrapResult("rotated")
    with sessions() as db:
        rows = db.scalars(select(LoginSession)).all()
    assert len(rows) == 2
    assert all(row.revoked_at is not None for row in rows)
    assert all(row.revoked_at.replace(tzinfo=UTC) == NOW for row in rows)
    with pytest.raises(BrowserAuthenticationError):
        service.resolve(first.token)
    with pytest.raises(BrowserAuthenticationError):
        service.resolve(second.token)


@pytest.mark.parametrize("conflict", ["second-administrator", "admin-wrong-role"])
def test_rotate_admin_rejects_non_unique_authority_without_mutation(
    sessions: sessionmaker[Session], conflict: str
) -> None:
    """Rotation must use the exact same admin/administrator authority as bootstrap."""
    old_verifier = hash_password(ADMIN_PASSWORD)
    new_verifier = hash_password("synthetic replacement administrator password")
    with sessions.begin() as db:
        admin = User(
            subject="admin",
            role="operator" if conflict == "admin-wrong-role" else "administrator",
            disabled_at=None,
            password_verifier=old_verifier,
        )
        db.add(admin)
        db.flush()
        db.add(
            LoginSession(
                user_id=admin.id,
                digest=sha256(b"synthetic-authority-conflict-session").hexdigest(),
                expires_at=NOW + timedelta(hours=1),
            )
        )
        if conflict == "second-administrator":
            db.add(
                User(
                    subject="synthetic-other-administrator",
                    role="administrator",
                    disabled_at=None,
                    password_verifier=hash_password("synthetic other password"),
                )
            )
    service = browser_auth(sessions, Clock(), opaque(33), opaque(34))

    with pytest.raises(BrowserAuthenticationError):
        service.rotate_admin(new_verifier)

    with sessions() as db:
        persisted_admin = db.scalar(select(User).where(User.subject == "admin"))
        login = db.scalar(select(LoginSession))
    assert persisted_admin is not None
    assert login is not None
    assert persisted_admin.password_verifier == old_verifier
    assert login.revoked_at is None


def test_bootstrap_admin_is_exact_and_idempotent(
    sessions: sessionmaker[Session],
) -> None:
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
    limiter = LoginRateLimiter(
        token_signing_key=b"test-token-signing-key", clock=monotonic
    )
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
    limiter = LoginRateLimiter(
        token_signing_key=b"test-token-signing-key", clock=monotonic
    )
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
    for _ in range(4):
        admin = limiter.reserve("admin")
        other = limiter.reserve("other-subject")
        assert admin is not None and other is not None
        limiter.fail(admin)
        limiter.fail(other)
    other = limiter.reserve("other-subject")
    assert other is not None
    limiter.fail(other)
    successful = limiter.reserve("admin")
    assert successful is not None

    limiter.succeed(successful)

    assert limiter.reserve("admin") is not None
    assert limiter.reserve("other-subject") is None


def test_throttle_evicts_expired_subject_state_before_admission() -> None:
    """Retaining expired subject entries would eventually exhaust the bounded map."""
    monotonic = MonotonicClock()
    limiter = LoginRateLimiter(
        token_signing_key=b"test-token-signing-key", clock=monotonic
    )
    attempt = limiter.reserve("expired-subject")
    assert attempt is not None
    limiter.fail(attempt)
    assert len(limiter._subjects) == 1

    monotonic.value = 300.0

    assert limiter.reserve("new-subject") is not None
    assert len(limiter._subjects) == 1


def test_throttle_never_retains_a_submitted_subject_in_plaintext() -> None:
    """Replacing keyed identifiers with raw subject keys would expose login input."""
    subject = "unkeyed-submitted-subject"
    limiter = LoginRateLimiter(
        token_signing_key=b"test-token-signing-key", clock=MonotonicClock()
    )

    attempt = limiter.reserve(subject)
    assert attempt is not None
    limiter.fail(attempt)

    assert subject not in limiter._subjects
    assert subject not in repr(limiter)


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


@pytest.mark.parametrize(("field", "value"), (("role", "viewer"), ("subject", "other")))
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


def test_limiter_reservations_bound_inflight_attempts_for_one_subject() -> None:
    """Splitting admission from failure recording would admit a sixth in-flight attempt."""
    limiter = LoginRateLimiter(
        token_signing_key=b"test-token-signing-key", clock=MonotonicClock()
    )

    attempts = _concurrently_reserve(limiter, ("admin",) * 6)

    assert sum(attempt is not None for attempt in attempts) == 5


def test_limiter_reservations_bound_inflight_attempts_globally() -> None:
    """Splitting admission from failure recording would admit 21 global attempts."""
    limiter = LoginRateLimiter(
        token_signing_key=b"test-token-signing-key", clock=MonotonicClock()
    )

    attempts = _concurrently_reserve(
        limiter, tuple(f"concurrent-{index}" for index in range(21))
    )

    assert sum(attempt is not None for attempt in attempts) == 20


def test_limiter_rejects_the_1025th_tracked_subject() -> None:
    """Growing past 1,024 keyed subjects would permit unbounded retention."""
    limiter = LoginRateLimiter(
        token_signing_key=b"test-token-signing-key",
        clock=MonotonicClock(),
        maximum_global_failures=1_025,
    )

    attempts = [limiter.reserve(f"subject-{index}") for index in range(1_024)]

    assert all(attempt is not None for attempt in attempts)
    assert limiter.reserve("subject-over-limit") is None
    assert len(limiter._subjects) == 1_024
    for attempt in attempts:
        assert attempt is not None
        limiter.succeed(attempt)


@pytest.fixture(scope="module")
def postgres_engine() -> Engine:
    if shutil.which("docker") is None:
        pytest.skip("Docker is required for PostgreSQL browser-auth locking tests")
    try:
        container = subprocess.check_output(
            [
                "docker",
                "run",
                "--rm",
                "-d",
                "-e",
                "POSTGRES_PASSWORD=postgres",
                "-p",
                "127.0.0.1::5432",
                "postgres:16",
            ],
            text=True,
        ).strip()
    except subprocess.CalledProcessError as error:
        pytest.skip(f"disposable PostgreSQL is unavailable: {error}")
    try:
        port = subprocess.check_output(
            [
                "docker",
                "inspect",
                "-f",
                '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}',
                container,
            ],
            text=True,
        ).strip()
        engine = create_engine(
            f"postgresql+psycopg://postgres:postgres@127.0.0.1:{port}/postgres"
        )
        for _ in range(100):
            try:
                with engine.connect():
                    break
            except (OSError, SQLAlchemyError):
                time.sleep(0.1)
        else:
            pytest.skip("disposable PostgreSQL did not become ready")
        yield engine
        engine.dispose()
    finally:
        subprocess.run(["docker", "stop", container], check=False, capture_output=True)


def test_postgres_rotation_revokes_a_login_serialized_with_its_user_lock(
    postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An old-password login concurrent with rotation must leave no active session."""
    Base.metadata.drop_all(postgres_engine)
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    add_admin(sessions)
    service = browser_auth(sessions, Clock(), opaque(31), opaque(32))
    verification_started = threading.Event()
    release_verification = threading.Event()
    rotation_select_started = threading.Event()
    login_results: list[object] = []
    rotation_results: list[object] = []
    verify = browser_auth_module.verify_password

    def paused_verify(verifier: str, password: str):
        verification_started.set()
        assert release_verification.wait(timeout=5)
        return verify(verifier, password)

    def observe_rotation_select(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        if (
            threading.current_thread().name == "browser-auth-rotation"
            and "FROM users" in statement
        ):
            rotation_select_started.set()

    def login() -> None:
        try:
            login_results.append(service.login("admin", ADMIN_PASSWORD))
        except BaseException as error:  # noqa: BLE001 - thread reports test failure
            login_results.append(error)

    def rotate() -> None:
        try:
            rotation_results.append(
                service.rotate_admin(hash_password("rotated password"))
            )
        except BaseException as error:  # noqa: BLE001 - thread reports test failure
            rotation_results.append(error)

    monkeypatch.setattr(browser_auth_module, "verify_password", paused_verify)
    event.listen(postgres_engine, "before_cursor_execute", observe_rotation_select)
    try:
        login_thread = threading.Thread(target=login, name="browser-auth-login")
        rotation_thread = threading.Thread(target=rotate, name="browser-auth-rotation")
        login_thread.start()
        assert verification_started.wait(timeout=5)
        rotation_thread.start()
        assert rotation_select_started.wait(timeout=5)
        release_verification.set()
        login_thread.join(timeout=5)
        rotation_thread.join(timeout=5)
    finally:
        release_verification.set()
        event.remove(postgres_engine, "before_cursor_execute", observe_rotation_select)

    assert not login_thread.is_alive()
    assert not rotation_thread.is_alive()
    assert len(login_results) == 1
    assert isinstance(login_results[0], browser_auth_module.IssuedBrowserSession)
    assert rotation_results == [BootstrapResult("rotated")]
    with sessions() as db:
        rows = db.scalars(select(LoginSession)).all()
    assert len(rows) == 1
    assert rows[0].revoked_at is not None


@pytest.mark.parametrize("operation", ["bootstrap", "rotation"])
def test_postgres_browser_authority_writer_blocks_offline_admin_insertion(
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
) -> None:
    """Every browser authority mutation must exclude a concurrent admin insert."""
    Base.metadata.drop_all(postgres_engine)
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    if operation == "rotation":
        add_admin(sessions)
    service = browser_auth(sessions, Clock(), opaque(35), opaque(36))
    browser_read_finished = threading.Event()
    release_browser_writer = threading.Event()
    offline_database_attempted = threading.Event()
    browser_results: list[object] = []
    offline_results: list[object] = []

    def observe_statements(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        thread = threading.current_thread().name
        if thread == "browser-authority-writer" and "FROM users" in statement:
            browser_read_finished.set()
            assert release_browser_writer.wait(timeout=5)
        if thread == "offline-authority-writer" and (
            "LOCK TABLE users" in statement or "INSERT INTO users" in statement
        ):
            offline_database_attempted.set()

    def mutate_browser_authority() -> None:
        try:
            if operation == "bootstrap":
                result = service.bootstrap_admin(hash_password(ADMIN_PASSWORD))
            else:
                result = service.rotate_admin(hash_password("replacement password"))
            browser_results.append(result)
        except BaseException as error:  # noqa: BLE001 - report thread failure
            browser_results.append(error)

    def create_offline_admin() -> None:
        offline_results.append(
            offline_module.main(
                [
                    "--state-path",
                    str(tmp_path / "state"),
                    "create-admin",
                    "--subject",
                    "synthetic-offline-administrator",
                ]
            )
        )

    monkeypatch.setattr(db_module, "build_engine", lambda _url: postgres_engine)
    monkeypatch.setattr(
        settings_module.Settings,
        "from_env_and_secrets",
        classmethod(
            lambda _cls: SimpleNamespace(database_url=str(postgres_engine.url))
        ),
    )
    monkeypatch.setattr(
        offline_module, "require_offline", lambda *_args, **_kwargs: nullcontext()
    )
    event.listen(postgres_engine, "before_cursor_execute", observe_statements)
    try:
        browser_thread = threading.Thread(
            target=mutate_browser_authority, name="browser-authority-writer"
        )
        offline_thread = threading.Thread(
            target=create_offline_admin, name="offline-authority-writer"
        )
        browser_thread.start()
        assert browser_read_finished.wait(timeout=5)
        offline_thread.start()
        assert offline_database_attempted.wait(timeout=5)
        offline_thread.join(timeout=0.5)
        offline_was_blocked = offline_thread.is_alive()
        release_browser_writer.set()
        browser_thread.join(timeout=5)
        offline_thread.join(timeout=5)
    finally:
        release_browser_writer.set()
        event.remove(postgres_engine, "before_cursor_execute", observe_statements)

    assert not browser_thread.is_alive()
    assert not offline_thread.is_alive()
    assert offline_was_blocked
    expected = "created" if operation == "bootstrap" else "rotated"
    assert browser_results == [BootstrapResult(expected)]
    assert offline_results == [0]


def test_postgres_offline_admin_writer_blocks_bootstrap_until_its_commit(
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Offline create-admin must hold the same authority lock as bootstrap."""
    Base.metadata.drop_all(postgres_engine)
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    service = browser_auth(sessions, Clock(), opaque(37), opaque(38))
    offline_insert_started = threading.Event()
    release_offline_insert = threading.Event()
    browser_database_attempted = threading.Event()
    offline_results: list[object] = []
    browser_results: list[object] = []

    def observe_statements(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        thread = threading.current_thread().name
        if thread == "offline-authority-holder" and "INSERT INTO users" in statement:
            offline_insert_started.set()
            assert release_offline_insert.wait(timeout=5)
        if thread == "blocked-browser-bootstrap" and (
            "LOCK TABLE users" in statement or "FROM users" in statement
        ):
            browser_database_attempted.set()

    def create_offline_admin() -> None:
        offline_results.append(
            offline_module.main(
                [
                    "--state-path",
                    str(tmp_path / "state"),
                    "create-admin",
                    "--subject",
                    "synthetic-offline-administrator",
                ]
            )
        )

    def bootstrap() -> None:
        try:
            browser_results.append(
                service.bootstrap_admin(hash_password(ADMIN_PASSWORD))
            )
        except BaseException as error:  # noqa: BLE001 - report thread failure
            browser_results.append(error)

    monkeypatch.setattr(db_module, "build_engine", lambda _url: postgres_engine)
    monkeypatch.setattr(
        settings_module.Settings,
        "from_env_and_secrets",
        classmethod(
            lambda _cls: SimpleNamespace(database_url=str(postgres_engine.url))
        ),
    )
    monkeypatch.setattr(
        offline_module, "require_offline", lambda *_args, **_kwargs: nullcontext()
    )
    event.listen(postgres_engine, "before_cursor_execute", observe_statements)
    try:
        offline_thread = threading.Thread(
            target=create_offline_admin, name="offline-authority-holder"
        )
        browser_thread = threading.Thread(
            target=bootstrap, name="blocked-browser-bootstrap"
        )
        offline_thread.start()
        assert offline_insert_started.wait(timeout=5)
        browser_thread.start()
        assert browser_database_attempted.wait(timeout=5)
        browser_thread.join(timeout=0.5)
        browser_was_blocked = browser_thread.is_alive()
        release_offline_insert.set()
        offline_thread.join(timeout=5)
        browser_thread.join(timeout=5)
    finally:
        release_offline_insert.set()
        event.remove(postgres_engine, "before_cursor_execute", observe_statements)

    assert not offline_thread.is_alive()
    assert not browser_thread.is_alive()
    assert browser_was_blocked
    assert offline_results == [0]
    assert len(browser_results) == 1
    assert isinstance(browser_results[0], BrowserAuthenticationError)


def _concurrently_reserve(
    limiter: LoginRateLimiter, subjects: tuple[str, ...]
) -> list[object]:
    start = threading.Barrier(len(subjects) + 1)
    reserved = threading.Barrier(len(subjects) + 1)
    release = threading.Event()
    attempts: list[object] = []
    attempts_lock = threading.Lock()

    def reserve(subject: str) -> None:
        start.wait(timeout=5)
        attempt = limiter.reserve(subject)
        with attempts_lock:
            attempts.append(attempt)
        reserved.wait(timeout=5)
        assert release.wait(timeout=5)
        if attempt is not None:
            limiter.fail(attempt)

    threads = [
        threading.Thread(target=reserve, args=(subject,)) for subject in subjects
    ]
    for thread in threads:
        thread.start()
    start.wait(timeout=5)
    reserved.wait(timeout=5)
    release.set()
    for thread in threads:
        thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)
    return attempts
