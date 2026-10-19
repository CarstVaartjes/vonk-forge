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
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import Table, case, inspect, or_, select, tuple_
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import WaitOwnerKind

from .logging import log_event, redact_text
from .models import Base
from .wait_contract import WaitEvidence

_LOGGER = logging.getLogger(__name__)
type RowKey = tuple[str | int, ...]
type AdvanceWait = Callable[[RowKey, WaitEvidence], object]


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
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if batch_limit < 1:
            raise ValueError("due wait batch must be positive")
        self._sessions = sessions
        self._sources = tuple(sources)
        if len({source.table.name for source in sources}) != len(sources):
            raise ValueError("a wait table must have one advancement binding")
        self._clock = clock
        if pass_budget_seconds <= 0:
            raise ValueError("due wait observation budget must be positive")
        self._limit = batch_limit
        self._pass_budget = pass_budget_seconds
        self._monotonic = monotonic
        self._owner_cursor = 0
        self._row_cursors: dict[str, RowKey] = {}

    def tick(self) -> int:
        """Return rows examined; this is never a measure of work completed.

        Stop between handlers when the scheduler's observation budget is spent.
        Each handler retains its own transport/transaction deadline. Owner and
        row cursors move before it runs, so a slow owner yields the next pass.
        """
        examined = 0
        visited: dict[str, set[RowKey]] = {}
        now = self._clock()
        deadline = self._monotonic() + self._pass_budget
        # No row gets a second turn in this pass, even if its handler cannot
        # move its clock. Rotate owners as well when the batch is smaller than
        # the owner inventory. Cursor movement is never measured progress.
        exhausted: set[str] = set()
        while self._sources and examined < self._limit and self._monotonic() < deadline:
            source = self._sources[self._owner_cursor]
            self._owner_cursor = (self._owner_cursor + 1) % len(self._sources)
            table = source.table
            if table.name in exhausted:
                if len(exhausted) == len(self._sources):
                    break
                continue
            keys = tuple(table.primary_key.columns)
            mapper = inspect(source.model)
            due = mapper.attrs.next_action_at.columns[0]
            statement = select(*keys, table.c.wait_evidence, due).where(
                or_(table.c.wait_evidence.is_not(None), due <= now),
                or_(due <= now, due.is_(None)),
            )
            cursor = self._row_cursors.get(table.name)
            if cursor is not None:
                statement = statement.order_by(
                    case((tuple_(*keys) > tuple_(*cursor), 0), else_=1)
                )
            statement = statement.order_by(*keys).limit(1)
            # Read one raw identity at a time. A corrupt JSON decoder in one
            # row cannot abort materialization of another row's batch.
            selected = False
            try:
                with self._sessions() as session:
                    candidate = session.execute(statement).one_or_none()
                if candidate is None:
                    exhausted.add(table.name)
                    continue
                key = cast(RowKey, tuple(candidate[: len(keys)]))
                seen = visited.setdefault(table.name, set())
                if key in seen:
                    exhausted.add(table.name)
                    continue
                seen.add(key)
                self._row_cursors[table.name] = key
                examined += 1
                selected = True
                evidence, next_check = candidate[-2:]
                if not isinstance(evidence, WaitEvidence) or next_check is None:
                    raise ValueError(
                        "persisted wait requires typed evidence and a due clock"
                    )
                if evidence.owner_kind not in source.kinds:
                    raise ValueError(
                        "persisted wait kind does not belong to its storage owner"
                    )
                source.advance(key, evidence)
            except Exception as error:  # noqa: BLE001 - isolate one row/owner observation
                log_event(
                    _LOGGER,
                    "worker.due_wait_failed",
                    service="control-worker",
                    owner=table.name,
                    row=repr(self._row_cursors.get(table.name)),
                    error=type(error).__name__,
                    message=redact_text(error),
                )
                # A failed selection has no trustworthy identity. Do not spin
                # on the table; the next worker pass retries the observation.
                if not selected:
                    exhausted.add(table.name)
        return examined
