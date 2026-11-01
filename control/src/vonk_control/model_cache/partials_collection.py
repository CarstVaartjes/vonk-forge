"""Collect unfinished model downloads nothing is writing any more.

An unfinished download is not a model file: the keep-until-space-is-short rule
belongs to published objects only. A ``partials/<owner>`` directory, where the
owner is a set digest or ``repair-<transfer_id>``, is removed once no live
model-cache operation owns it and 24 h passed since the later of its last change
and the last update of the operation that owned it. A cancelled or failed download
therefore keeps its resume bytes for a day from the moment it ended, then a new
request downloads again. A directory without an owning row follows its
modification time alone, because the filesystem is listed, not the database.

The pass filters by modification time without the database. Only a stale
directory costs one ownership query, read in a short read-only session under the
exclusive owner lock that every transfer writer holds shared
(``_model_storage_lock``); a busy lock means a writer is alive, so the directory
is skipped and the next pass asks again. A pass is bounded in time and resumes
from a name cursor, so directories kept at the head of the list cannot starve
the rest.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import case, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from ..catalog_revision_collection import GRACE, INTERVAL
from ..categorized_errors import InvalidValue
from ..logging import log_event
from ..model_cache_contract import CacheOperationKind
from ..model_cache_states import LIVE
from ..models import ModelCacheOperation
from ..terminal_history_collection import BACKLOG_PAUSE, PASS_BUDGET_SECONDS
from .errors import ModelCacheStorageRefused, _ArtifactWriterBusy
from .split_transfer import REPAIR_PARTIALS_PREFIX

if TYPE_CHECKING:
    from .service import ModelCacheService

_LOGGER = logging.getLogger(__name__)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _is_owner(name: str) -> bool:
    """Whether the name is one a transfer writes: a set digest or a repair."""

    if _DIGEST.match(name):
        return True
    suffix = name.removeprefix(REPAIR_PARTIALS_PREFIX)
    if suffix == name:
        return False
    try:
        uuid.UUID(suffix)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class _Ownership:
    """Whether an operation still owns a directory, and when one last moved."""

    live: bool
    last_update: float | None


class ModelPartialsCollector:
    """Removes abandoned ``partials/`` directories, a bounded pass per tick."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        cache: ModelCacheService,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._sessions = sessions
        self._cache = cache
        self._clock = clock
        self._due_at: datetime | None = None
        self._cursor: str | None = None

    def tick(self) -> bool:
        """Collect at most once per interval; never waits for a writer."""

        now = self._clock()
        if self._due_at is not None and now < self._due_at:
            return False
        self._due_at = now + INTERVAL
        try:
            removed, backlog = self.collect()
        except (SQLAlchemyError, OSError) as error:
            log_event(
                _LOGGER,
                "model_partials.scan_deferred",
                service="control-worker",
                code=type(error).__name__,
            )
            return False
        if backlog:
            self._due_at = now + BACKLOG_PAUSE
        if removed:
            log_event(
                _LOGGER,
                "model_partials.removed",
                service="control-worker",
                directories=removed,
            )
        return removed > 0

    def collect(self) -> tuple[int, bool]:
        """Return the directories removed and whether the budget left some.

        The next pass starts after the last directory this one judged.
        """

        try:
            names = sorted(entry.name for entry in os.scandir(self._partials))
        except FileNotFoundError:
            self._cursor = None
            return 0, False
        cursor = self._cursor
        if cursor is not None:
            names = [n for n in names if n > cursor] + [n for n in names if n <= cursor]
        removed = 0
        # The budget starts after the listing: a slow one still judges a directory.
        deadline = time.monotonic() + PASS_BUDGET_SECONDS
        for name in names:
            if time.monotonic() >= deadline:
                return removed, True
            self._cursor = name
            try:
                removed += self._collect_one(name)
            except (
                OSError,
                SQLAlchemyError,
                InvalidValue,
                ModelCacheStorageRefused,
            ) as error:
                # One unreadable directory or failed query never stops the
                # others; the next pass judges it again.
                log_event(
                    _LOGGER,
                    "model_partials.item_deferred",
                    service="control-worker",
                    code=type(error).__name__,
                )
        self._cursor = None
        return removed, False

    @property
    def _partials(self) -> Path:
        return self._cache._root / "partials"

    def _collect_one(self, name: str) -> int:
        newest = self._newest_change(name)
        # No database for a directory that changed within the grace period.
        if newest is None or not self._older_than_grace(newest):
            return 0
        if not _is_owner(name):
            # No writer can use this name, so no lock protects it: it goes by age.
            size = self._size(name)
            self._cache._remove_model_partial_set(name)
            log_event(
                _LOGGER,
                "model_partials.unknown_removed",
                service="control-worker",
                name=name,
                bytes=size,
            )
            return 1
        try:
            with self._cache._model_storage_lock(name, model_set=True):
                # Judged under the lock: a writer that held it shared has
                # finished, and a later one is refused until we release.
                owner = self._ownership(name)
                if owner.live or not self._older_than_grace(
                    max(newest, owner.last_update or newest)
                ):
                    return 0
                self._cache._remove_model_partial_set(name)
        except _ArtifactWriterBusy:
            return 0
        return 1

    def _size(self, name: str) -> int:
        total = 0
        for root, _directories, files in os.walk(self._partials / name):
            for file in files:
                try:
                    total += os.lstat(os.path.join(root, file)).st_size
                except OSError:
                    continue
        return total

    def _older_than_grace(self, moment: float | None) -> bool:
        if moment is None:
            return False
        return self._clock().timestamp() - moment >= GRACE.total_seconds()

    def _newest_change(self, name: str) -> float | None:
        """The latest mtime of a directory or its direct files; None if not one."""

        path = self._partials / name
        try:
            metadata = os.lstat(path)
            if not stat.S_ISDIR(metadata.st_mode):
                return None
            newest = metadata.st_mtime
            with os.scandir(path) as children:
                for child in children:
                    newest = max(newest, child.stat(follow_symlinks=False).st_mtime)
        except FileNotFoundError:
            return None
        return newest

    def _ownership(self, name: str) -> _Ownership:
        """One indexed query: do operations of this owner live, and when did one move."""

        live = func.sum(case((ModelCacheOperation.state.in_(LIVE), 1), else_=0))
        query = select(live, func.max(ModelCacheOperation.updated_at))
        if _DIGEST.match(name):
            query = query.where(ModelCacheOperation.artifact_set_sha256 == name)
        elif name.startswith(REPAIR_PARTIALS_PREFIX):
            transfer = ModelCacheOperation.payload["repair_checkpoint"]["transfer_id"]
            query = query.where(
                ModelCacheOperation.kind == CacheOperationKind.REPAIR,
                transfer.as_string() == name.removeprefix(REPAIR_PARTIALS_PREFIX),
            )
        else:
            return _Ownership(live=False, last_update=None)
        with self._sessions() as session:
            count, last = session.execute(query).one()
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        return _Ownership(
            live=bool(count), last_update=None if last is None else last.timestamp()
        )
