"""Profile and recipe-update mutations do not serialize on the users table."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from vonk_control.fleet_profiles import FleetProfileService
from vonk_control.models import Base, User
from vonk_control.recipe_update_batches import RecipeUpdateBatches
from vonk_control.user_authority import serialize_user_authority


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


def test_the_writer_fence_still_serializes_administrator_writers(sessions) -> None:
    """The security fence kept: two user-authority writers (bootstrap) exclude
    each other, including absent rows."""
    with sessions.begin() as first, sessions.begin() as second:
        serialize_user_authority(first)
        second.execute(text("SET LOCAL lock_timeout = '500ms'"))
        with pytest.raises(OperationalError):
            serialize_user_authority(second)
