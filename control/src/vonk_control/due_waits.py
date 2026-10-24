"""Bounded, fair selection for the existing owners' persisted wait clocks.

This is an observation backstop, not another scheduler: cursors are disposable,
clocks and intent stay with their owners, and only their normal advancement
functions can change them. Selection holds no transaction across a handler.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import cast

from sqlalchemy import (
    String,
    Table,
    case,
    func,
    inspect,
    literal,
    or_,
    select,
    tuple_,
)
from sqlalchemy import (
    cast as sql_cast,
)
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import Disposition, WaitOwnerKind

from .failure_classification import disposition, error_detail, wait_reason
from .logging import log_event, redact_text
from .models import Base
from .models.waits import ScheduledWaitOwner
from .persisted_waits import owner_identity
from .recovery_policy import WAIT_STEP
from .strict_json import stored_document_detail
from .wait_contract import WaitEvidence

_LOGGER = logging.getLogger(__name__)
type RowKey = tuple[str | int, ...]
type AdvanceWait = Callable[[RowKey, WaitOwnerKind], object]


@dataclass(frozen=True)
class WaitSource:
    model: type[Base]
    kinds: frozenset[WaitOwnerKind]
    advance: AdvanceWait

    @property
    def table(self) -> Table:
        return cast(Table, self.model.__table__)


class DueWaitBackstop:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        sources: Sequence[WaitSource],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        batch_limit: int = 16,
        pass_budget_seconds: float = 1,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if batch_limit < 1:
            raise ValueError("due wait batch must be positive")
        self._sessions = sessions
        self._sources = tuple(sources)
        self._table_kinds: dict[str, frozenset[WaitOwnerKind]] = {}
        self._first_source: dict[str, int] = {}
        for index, source in enumerate(sources):
            known = self._table_kinds.get(source.table.name, frozenset())
            if known.intersection(source.kinds):
                raise ValueError("a wait kind must have one advancement binding")
            self._table_kinds[source.table.name] = known | source.kinds
            self._first_source.setdefault(source.table.name, index)
        self._clock = clock
        if pass_budget_seconds <= 0:
            raise ValueError("due wait observation budget must be positive")
        self._limit = batch_limit
        self._pass_budget = pass_budget_seconds
        self._monotonic = monotonic or time.monotonic
        self._owner_cursor = 0
        self._row_cursors: dict[int, RowKey] = {}
        self._failures: dict[tuple[str, RowKey], bytes] = {}
        self._retry_at: dict[tuple[str, RowKey], datetime] = {}

    def tick(self) -> int:
        """Return rows examined; this is never a measure of work completed.

        Stop between handlers when the scheduler's observation budget is spent.
        Each handler retains its own transport/transaction deadline. Owner and
        row cursors move before it runs, so a slow owner yields the next pass.
        """
        examined = 0
        visited: dict[int, set[RowKey]] = {}
        now = self._clock()
        self._retry_at = {key: due for key, due in self._retry_at.items() if due > now}
        deadline = self._monotonic() + self._pass_budget
        # No row gets a second turn in this pass, even if its handler cannot
        # move its clock. Rotate owners as well when the batch is smaller than
        # the owner inventory. Cursor movement is never measured progress.
        exhausted: set[int] = set()
        while self._sources and examined < self._limit and self._monotonic() < deadline:
            index = self._owner_cursor
            source = self._sources[index]
            self._owner_cursor = (self._owner_cursor + 1) % len(self._sources)
            table = source.table
            if index in exhausted:
                if len(exhausted) == len(self._sources):
                    break
                continue
            keys = tuple(table.primary_key.columns)
            mapper = inspect(source.model)
            due = mapper.attrs.next_action_at.columns[0]
            # Read JSON as text so validation cannot lose the row identity.
            raw_evidence = func.nullif(sql_cast(table.c.wait_evidence, String), "null")
            state = table.c.get("state", literal(None, String))
            statement = select(*keys, raw_evidence, due, state).where(
                or_(raw_evidence.is_not(None), due <= now),
                or_(due <= now, due.is_(None)),
            )
            if source.kinds != self._table_kinds[table.name]:
                kind = table.c.wait_evidence["owner_kind"].as_string()
                belongs = kind.in_(source.kinds)
                if index == self._first_source[table.name]:
                    # One owner also observes malformed/unassigned evidence;
                    # it cannot delay other kinds sharing this physical table.
                    belongs = or_(
                        belongs,
                        kind.is_(None),
                        kind.not_in(self._table_kinds[table.name]),
                    )
                statement = statement.where(belongs)
            cursor = self._row_cursors.get(index)
            if cursor is not None:
                statement = statement.order_by(
                    case((tuple_(*keys) > tuple_(*cursor), 0), else_=1)
                )
            statement = statement.order_by(*keys).limit(1)
            # Read one raw identity at a time. A corrupt JSON decoder in one
            # row cannot abort materialization of another row's batch.
            selected = False
            fingerprint: bytes | None = None
            try:
                with self._sessions() as session:
                    candidate = session.execute(statement).one_or_none()
                if candidate is None:
                    exhausted.add(index)
                    continue
                key = cast(RowKey, tuple(candidate[: len(keys)]))
                seen = visited.setdefault(index, set())
                if key in seen:
                    exhausted.add(index)
                    continue
                seen.add(key)
                self._row_cursors[index] = key
                examined += 1
                selected = True
                retry_key = (table.name, key)
                retry_at = self._retry_at.get(retry_key)
                if retry_at is not None and now < retry_at:
                    continue
                self._retry_at.pop(retry_key, None)
                raw, next_check, _state = candidate[-3:]
                fingerprint = sha256(repr(candidate[-3:]).encode()).digest()
                if raw is None:
                    # Ordinary due work, including rows predating wait custody,
                    # still belongs to its existing normal advancement path.
                    kind = next(iter(source.kinds))
                    if len(self._table_kinds[table.name]) > 1:
                        with self._sessions() as session:
                            owner = session.get(source.model, key)
                            if owner is None:
                                continue
                            _, kind = owner_identity(cast(ScheduledWaitOwner, owner))
                else:
                    evidence = WaitEvidence.model_validate_json(raw)
                    if next_check is None:
                        raise ValueError("persisted wait requires a due clock")
                    if evidence.owner.id != str(key[-1]):
                        raise ValueError(
                            "persisted wait identity does not match its storage owner"
                        )
                    if evidence.owner_kind not in source.kinds:
                        raise ValueError(
                            "persisted wait kind does not belong to its storage owner"
                        )
                    kind = evidence.owner_kind
                advance = next(
                    binding.advance
                    for binding in self._sources
                    if binding.table.name == table.name and kind in binding.kinds
                )
                advance(key, kind)
                self._failures.pop((table.name, key), None)
            except Exception as error:  # noqa: BLE001 - isolate one row/owner observation
                if disposition(error) in {Disposition.WAIT, Disposition.OBSERVE}:
                    retry_at = self._clock() + timedelta(seconds=WAIT_STEP)
                    if selected:
                        self._retry_at[(table.name, key)] = retry_at
                    log_event(
                        _LOGGER,
                        "worker.due_wait.wait",
                        service="control-worker",
                        owner=table.name,
                        row=repr(self._row_cursors.get(index)),
                        reason=wait_reason(error),
                        detail=error_detail(error),
                        retry_at=retry_at if selected else None,
                    )
                    if not selected:
                        exhausted.add(index)
                    continue
                if fingerprint is not None:
                    failure_key = (table.name, key)
                    if self._failures.get(failure_key) == fingerprint:
                        continue
                    self._failures[failure_key] = fingerprint
                log_event(
                    _LOGGER,
                    "worker.due_wait_failed",
                    service="control-worker",
                    owner=table.name,
                    row=repr(self._row_cursors.get(index)),
                    error=type(error).__name__,
                    message=stored_document_detail(error) or redact_text(error),
                )
                # A failed selection has no trustworthy identity. Do not spin
                # on the table; the next worker pass retries the observation.
                if not selected:
                    exhausted.add(index)
        return examined
