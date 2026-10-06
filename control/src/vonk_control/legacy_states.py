"""Startup adoption: rewrite lifecycle rows that still carry a retired state word.

Every reader already adopts an old word when it reads a row (the contract's alias
table), so this pass is a convenience, not a requirement: it makes the stored words,
and with them every API view and every selection by state, speak the core vocabulary
as soon as the Controller starts.  It is idempotent, one UPDATE per word and table,
and a row it misses is adopted lazily by its kind's adapter.

The meaning of a word comes from the contract (``vonk_agent_protocol.STATE_ALIASES``),
never from this module; what is specific here is only *where* the words live:

* the generic ``jobs`` table (``waiting`` and ``partial`` mean different things to
  different kinds, see ``JOB_KIND_ALIASES``);
* the attempts of jobs and of Spark orders (``waiting-for-operator`` and ``expired``
  become ``observing`` with the typed ``observation_cause`` that said why);
* Spark orders (an old ``waiting-for-operator`` order was a retry, an observation or a
  wait for a person: its schedule says which);
* profile applications and cache operations.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import Table, and_, update
from sqlalchemy.engine import Connection
from vonk_agent_protocol import (
    ATTEMPT_ALIAS_CAUSE,
    JOB_KIND_ALIASES,
    LifecycleState,
    LifecycleSubject,
    StateAlias,
    adopt_state,
)

from .models import (
    AgentOperation,
    AgentOperationAttempt,
    FleetProfileApplication,
    Job,
    JobAttempt,
    ModelCacheOperation,
)


def _table(model: type) -> Table:
    return cast(Table, model.__table__)  # type: ignore[attr-defined]


def _rewrite(connection: Connection, table: Table, word: str, **values: object) -> int:
    result = connection.execute(
        update(table)
        .where(table.c.state == word)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


def _adopted(
    subject: LifecycleSubject, alias: StateAlias, kind: str | None = None
) -> str:
    adopted = adopt_state(subject, alias.value, kind)
    assert adopted is not None, (subject, alias)
    return adopted.state.value


def _jobs(connection: Connection) -> int:
    jobs = _table(Job)
    rewritten = 0
    for alias in StateAlias:
        # A kind that spells the word differently first, then every other kind.
        for kind, overrides in JOB_KIND_ALIASES.items():
            if alias in overrides:
                result = connection.execute(
                    update(jobs)
                    .where(jobs.c.state == alias.value, jobs.c.kind == kind)
                    .values(state=_adopted(LifecycleSubject.JOB, alias, kind))
                    .execution_options(synchronize_session=False)
                )
                rewritten += int(result.rowcount or 0)
        if adopt_state(LifecycleSubject.JOB, alias.value) is None:
            continue
        overridden = [
            kind for kind, overrides in JOB_KIND_ALIASES.items() if alias in overrides
        ]
        condition = jobs.c.state == alias.value
        if overridden:
            condition = and_(condition, jobs.c.kind.not_in(overridden))
        result = connection.execute(
            update(jobs)
            .where(condition)
            .values(state=_adopted(LifecycleSubject.JOB, alias))
            .execution_options(synchronize_session=False)
        )
        rewritten += int(result.rowcount or 0)
    return rewritten


def _attempts(connection: Connection, model: type) -> int:
    table = _table(model)
    rewritten = 0
    for alias, cause in ATTEMPT_ALIAS_CAUSE.items():
        rewritten += _rewrite(
            connection,
            table,
            alias.value,
            state=LifecycleState.OBSERVING.value,
            observation_cause=cause.value,
        )
    return rewritten


def _orders(connection: Connection) -> int:
    """An old ``waiting-for-operator`` order: its schedule says which wait it was."""

    orders = _table(AgentOperation)
    old = StateAlias.WAITING_FOR_OPERATOR.value
    scheduled = orders.c.next_action_at.is_not(None)
    observed = and_(orders.c.observe_count.is_not(None), orders.c.observe_count > 0)
    rewritten = 0
    for condition, state in (
        (and_(scheduled, observed), LifecycleState.OBSERVING),
        (scheduled, LifecycleState.BACKOFF),
        (orders.c.next_action_at.is_(None), LifecycleState.NEEDS_OPERATOR),
    ):
        result = connection.execute(
            update(orders)
            .where(orders.c.state == old, condition)
            .values(state=state.value)
            .execution_options(synchronize_session=False)
        )
        rewritten += int(result.rowcount or 0)
    return rewritten


def adopt_legacy_states(connection: Connection) -> int:
    """Rewrite every retired lifecycle word to the core word it means; the count."""

    rewritten = _orders(connection)
    rewritten += _jobs(connection)
    rewritten += _attempts(connection, JobAttempt)
    rewritten += _attempts(connection, AgentOperationAttempt)
    for model, subject in (
        (FleetProfileApplication, LifecycleSubject.FLEET_PROFILE_APPLICATION),
        (ModelCacheOperation, LifecycleSubject.MODEL_CACHE_OPERATION),
    ):
        table = _table(model)
        for alias in StateAlias:
            if adopt_state(subject, alias.value) is None:
                continue
            rewritten += _rewrite(
                connection, table, alias.value, state=_adopted(subject, alias)
            )
    return rewritten
