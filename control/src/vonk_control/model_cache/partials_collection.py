"""Collect unfinished model downloads nothing is writing any more.

An unfinished download is not a model file: the keep-until-space-is-short rule
belongs to published objects only. A ``partials/<owner>`` directory, where the
owner is a set digest or ``repair-<transfer_id>``, is removed once no live
model-cache operation owns it and nothing under it changed for the grace
period. A cancelled or failed download therefore keeps its resume bytes for a day,
then a new request downloads again. A directory without a set row follows the
same rule, because the filesystem is listed, not the database.

Removal takes the transfer's own non-blocking artifact locks first. A busy lock
means a writer is alive: the directory is skipped and the next pass asks again.
No lock is held across a SQL transaction.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import time
from collections.abc import Callable
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from ..catalog_revision_collection import GRACE, INTERVAL
from ..logging import log_event
from ..model_cache_states import LIVE
from ..models import ModelCacheOperation
from ..terminal_history_collection import BACKLOG_PAUSE, PASS_BUDGET_SECONDS
from .errors import ModelCacheStorageRefused, _ArtifactWriterBusy

if TYPE_CHECKING:
    from .service import ModelCacheService

_LOGGER = logging.getLogger(__name__)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PART = re.compile(r"^([0-9a-f]{64})\.part$")


class ModelPartialsCollector:
    """Removes abandoned ``partials/`` directories, a bounded pass per tick."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        cache: ModelCacheService,
        root: Path,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._sessions = sessions
        self._cache = cache
        self._partials = root / "partials"
        self._clock = clock
        self._due_at: datetime | None = None

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
        """Return the directories removed and whether the budget left some."""

        deadline = time.monotonic() + PASS_BUDGET_SECONDS
        try:
            names = sorted(entry.name for entry in os.scandir(self._partials))
        except FileNotFoundError:
            return 0, False
        removed = 0
        for name in names:
            if time.monotonic() >= deadline:
                return removed, True
            try:
                removed += self._collect_one(name)
            except (OSError, ModelCacheStorageRefused) as error:
                # One unreadable directory never stops the others; next pass.
                log_event(
                    _LOGGER,
                    "model_partials.item_deferred",
                    service="control-worker",
                    code=type(error).__name__,
                )
        return removed, False

    def _collect_one(self, name: str) -> int:
        if not self._abandoned(name):
            return 0
        try:
            with ExitStack() as locks:
                for digest in self._writer_digests(name):
                    locks.enter_context(self._cache._model_storage_lock(digest))
                if _DIGEST.match(name):
                    locks.enter_context(
                        self._cache._model_storage_lock(name, model_set=True)
                    )
                # Owners and age are read again under the locks: an operation
                # that started in between wins, and removal needs no transaction.
                if not self._abandoned(name):
                    return 0
                self._cache._remove_model_partial_set(name)
        except _ArtifactWriterBusy:
            return 0
        return 1

    def _abandoned(self, name: str) -> bool:
        """A directory no live operation owns and nothing changed in for a day."""

        newest = self._newest_change(name)
        if newest is None:
            return False
        if self._clock().timestamp() - newest < GRACE.total_seconds():
            return False
        return name not in self._owners()

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

    def _writer_digests(self, name: str) -> list[str]:
        try:
            return sorted(
                match.group(1)
                for child in os.listdir(self._partials / name)
                if (match := _PART.match(child))
            )
        except FileNotFoundError:
            return []

    def _owners(self) -> set[str]:
        """Partials directories named by operations that have not ended."""

        with self._sessions() as session:
            live = session.execute(
                select(
                    ModelCacheOperation.id, ModelCacheOperation.artifact_set_sha256
                ).where(ModelCacheOperation.state.in_(LIVE))
            ).all()
        return {
            self._cache._partial_owner(operation_id, set_digest)
            for operation_id, set_digest in live
            if set_digest is not None
        }
