"""Profile and recipe-update mutations do not serialize on the users table."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from vonk_control.browser_auth import bootstrap_administrator
from vonk_control.fleet_profiles import FleetProfileService
from vonk_control.models import Base, User
from vonk_control.recipe_update_batches import RecipeUpdateBatches


@pytest.fixture
def sessions(postgres_engine):
    Base.metadata.create_all(postgres_engine)
    factory = sessionmaker(postgres_engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(User(subject="admin", role="administrator"))
        session.add(User(subject="operator", role="operator"))
    return factory


def _authorizers():
    return (FleetProfileService._authorize, RecipeUpdateBatches._authorize)


@pytest.mark.parametrize("authorize", _authorizers())
def test_concurrent_mutations_do_not_wait_on_each_other(sessions, authorize) -> None:
    """Two in-flight mutations hold their transactions open together; the second
    never waits on the first (it did, on the users table lock)."""
    with sessions.begin() as first, sessions.begin() as second:
        second.execute(text("SET LOCAL lock_timeout = '1s'"))
        authorize(first, "admin")
        authorize(second, "admin")


@pytest.mark.parametrize("authorize", _authorizers())
def test_mutation_is_not_blocked_by_an_unrelated_users_writer(
    sessions, authorize
) -> None:
    """A row-level change of another user does not hold a mutation."""
    with sessions.begin() as writer, sessions.begin() as mutation:
        writer.query(User).filter(User.subject == "operator").update({"role": "viewer"})
        mutation.execute(text("SET LOCAL lock_timeout = '1s'"))
        authorize(mutation, "admin")


def test_the_bootstrap_still_waits_for_another_user_authority_writer(
    sessions,
) -> None:
    """The security fence kept: the administrator bootstrap excludes another
    writer of users, including absent rows."""
    with sessions.begin() as clear:
        clear.execute(text("DELETE FROM users"))
    pool = ThreadPoolExecutor(max_workers=1)
    with sessions.begin() as holder:
        holder.execute(text("LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE"))
        bootstrap = pool.submit(bootstrap_administrator, sessions, "pw-" + "x" * 20)
        # Prove the wait by behaviour: a backend is blocked on a lock while the
        # holder still has it, and the bootstrap has not returned.
        for _ in range(300):
            if holder.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' AND datname = current_database()"
                )
            ):
                break
            time.sleep(0.1)
        else:
            pytest.fail("the bootstrap never waited for the users lock")
        assert not bootstrap.done()
    assert bootstrap.result(timeout=30).status == "created"
    pool.shutdown()
