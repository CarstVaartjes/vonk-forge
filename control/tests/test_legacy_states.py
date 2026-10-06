"""The startup pass rewrites every retired state word to the word it means."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Integer,
    String,
    create_engine,
    event,
    text,
)
from sqlalchemy.orm import Session
from vonk_control.legacy_states import adopt_legacy_states
from vonk_control.models import (
    AgentOperation,
    AgentOperationAttempt,
    Base,
    FleetProfileApplication,
    Job,
    JobAttempt,
    ModelCacheOperation,
)

NOW = datetime(2026, 9, 1, tzinfo=UTC)
_IDS = itertools.count(1)


def _row(model, **given):
    """A row of ``model`` with every required column filled with a dummy."""

    values: dict[str, object] = {}
    for column in model.__table__.columns:
        if column.name in given:
            values[column.name] = given[column.name]
        elif (
            column.default is not None
            or column.server_default is not None
            or column.nullable
        ):
            continue
        elif isinstance(column.type, String):
            values[column.name] = f"v{next(_IDS)}"[: column.type.length or 64]
        elif isinstance(column.type, Integer):
            values[column.name] = 1
        elif isinstance(column.type, Boolean):
            values[column.name] = False
        elif isinstance(column.type, DateTime):
            values[column.name] = NOW
        elif isinstance(column.type, JSON):
            values[column.name] = {}
        else:
            values[column.name] = 0
    values.update(given)
    return model(**values)


def _states(connection, table: str, column: str = "state") -> list[str]:
    rows = connection.execute(text(f"SELECT {column} FROM {table} ORDER BY id"))
    return [row[0] for row in rows]


def test_every_retired_word_is_rewritten_to_the_word_it_means_and_once() -> None:
    engine = create_engine("sqlite://")
    # The rows below are minimal; only their state matters to this pass.
    event.listen(
        engine,
        "connect",
        lambda connection, _record: connection.execute(
            "PRAGMA ignore_check_constraints=ON"
        ),
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for index, word in enumerate(
            ("waiting-for-operator", "waiting", "cancelling", "partial", "expired")
        ):
            session.add(_row(Job, id=f"job-{index}", state=word, kind="recipe.start"))
        session.add(
            _row(Job, id="batch", state="partial", kind="recipe.cache.update.v2")
        )
        session.add(_row(Job, id="plain", state="running", kind="recipe.start"))
        session.add(
            _row(FleetProfileApplication, id="app", state="waiting-for-operator")
        )
        session.add(_row(ModelCacheOperation, id="cache", state="partial"))
        due = NOW + timedelta(minutes=5)
        session.add(
            _row(
                AgentOperation,
                id="o-retry",
                state="waiting-for-operator",
                next_action_at=due,
                observe_count=0,
            )
        )
        session.add(
            _row(
                AgentOperation,
                id="o-observe",
                state="waiting-for-operator",
                next_action_at=due,
                observe_count=2,
            )
        )
        session.add(
            _row(
                AgentOperation,
                id="o-wait",
                state="waiting-for-operator",
                next_action_at=None,
                observe_count=0,
            )
        )
        session.add(_row(AgentOperationAttempt, id="a-lapsed", state="expired"))
        session.add(
            _row(AgentOperationAttempt, id="a-unknown", state="waiting-for-operator")
        )
        session.add(_row(AgentOperationAttempt, id="a-done", state="succeeded"))
        session.add(_row(JobAttempt, id="j-lapsed", state="expired"))
        session.commit()

    with engine.begin() as connection:
        first = adopt_legacy_states(connection)
        assert first == 14
        assert adopt_legacy_states(connection) == 0  # idempotent

        jobs = dict(connection.execute(text("SELECT id, state FROM jobs")).all())
        assert jobs == {
            "job-0": "needs-operator",
            "job-1": "observing",
            "job-2": "observing",
            "job-3": "backoff",
            "job-4": "failed",
            "batch": "failed",  # an update batch that ended partial is failed
            "plain": "running",
        }
        assert _states(connection, "fleet_profile_applications") == ["needs-operator"]
        assert _states(connection, "model_cache_operations") == ["backoff"]
        orders = dict(
            connection.execute(text("SELECT id, state FROM agent_operations")).all()
        )
        assert orders == {
            "o-retry": "backoff",
            "o-observe": "observing",
            "o-wait": "needs-operator",
        }
        attempts = {
            row[0]: (row[1], row[2])
            for row in connection.execute(
                text(
                    "SELECT id, state, observation_cause FROM agent_operation_attempts"
                )
            )
        }
        assert attempts == {
            "a-lapsed": ("observing", "lease-lapsed"),
            "a-unknown": ("observing", "reported-unknown"),
            "a-done": ("succeeded", None),
        }
        job_attempt = connection.execute(
            text("SELECT state, observation_cause FROM job_attempts")
        ).one()
        assert tuple(job_attempt) == ("observing", "lease-lapsed")
