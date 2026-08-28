"""Cross-process cadence for bounded artifact CAS reconciliation."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .logging import log_event

_LOGGER = logging.getLogger(__name__)
_STATE_VERSION = 1


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("artifact maintenance cadence clock must be timezone-aware")
    return value.astimezone(UTC)


class ArtifactMaintenanceCadence:
    """Run one bounded reconciliation per shared, durable cadence interval."""

    def __init__(
        self,
        reconcile: Callable[..., Mapping[str, object]],
        *,
        state_root: Path,
        interval_seconds: int,
        batch_limit: int,
        clock: Callable[[], datetime],
        logger: logging.Logger = _LOGGER,
    ) -> None:
        if not state_root.is_absolute():
            raise ValueError("artifact maintenance state root must be absolute")
        if type(interval_seconds) is not int or not 60 <= interval_seconds <= 604800:
            raise ValueError(
                "artifact maintenance interval must be 60 to 604800 seconds"
            )
        if type(batch_limit) is not int or not 1 <= batch_limit <= 10000:
            raise ValueError("artifact maintenance batch limit must be 1 to 10000")
        self._reconcile = reconcile
        self._state_root = state_root
        self._interval = timedelta(seconds=interval_seconds)
        self._batch_limit = batch_limit
        self._clock = clock
        self._logger = logger
        self._next_local_check_at: datetime | None = None

    def __call__(self) -> None:
        now = _aware_utc(self._clock())
        if self._next_local_check_at is not None and now < self._next_local_check_at:
            return
        try:
            self._run_if_due(now)
        except Exception as error:
            self._next_local_check_at = now + self._interval
            self._logger.exception(
                "artifact storage maintenance scheduling failed",
                extra={"failure_type": type(error).__name__},
            )

    def _run_if_due(self, now: datetime) -> None:
        self._state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._state_root.is_symlink() or not self._state_root.is_dir():
            raise ValueError("artifact maintenance state root is unsafe")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self._state_root / ".maintenance.lock", flags, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self._next_local_check_at = now + timedelta(seconds=1)
                return
            state = self._read_state()
            next_due_at = self._next_due_at(state)
            if next_due_at is None:
                # API startup already reconciles the store. Persisting the baseline
                # prevents worker restarts or replicas from resetting the cadence.
                next_due_at = now + self._interval
                self._write_state({"next_due_at": next_due_at.isoformat()})
                self._next_local_check_at = next_due_at
                return
            if now < next_due_at:
                self._next_local_check_at = next_due_at
                return

            state["last_attempt_at"] = now.isoformat()
            state["next_due_at"] = (now + self._interval).isoformat()
            state.pop("last_failure_at", None)
            state.pop("last_failure_type", None)
            self._write_state(state)
            self._next_local_check_at = now + self._interval
            try:
                result = self._reconcile(batch_limit=self._batch_limit)
            except Exception as error:
                self._logger.exception(
                    "artifact storage reconciliation failed",
                    extra={"failure_type": type(error).__name__},
                )
                state["last_failure_at"] = now.isoformat()
                state["last_failure_type"] = type(error).__name__
                self._write_state(state)
                return
            state["last_success_at"] = now.isoformat()
            self._write_state(state)
            log_event(
                self._logger,
                "artifact-storage.reconciled",
                service="control-worker",
                batch_limit=self._batch_limit,
                **dict(result),
            )
        finally:
            os.close(descriptor)

    def _read_state(self) -> dict[str, Any]:
        path = self._state_root / ".maintenance.json"
        try:
            if path.is_symlink():
                raise ValueError("artifact maintenance state is unsafe")
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        parsed = json.loads(raw)
        if not isinstance(parsed, dict) or parsed.get("version") != _STATE_VERSION:
            raise ValueError("artifact maintenance state is invalid")
        return parsed

    @staticmethod
    def _next_due_at(state: Mapping[str, Any]) -> datetime | None:
        value = state.get("next_due_at")
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("artifact maintenance next due time is invalid")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError("artifact maintenance next due time is invalid") from error
        return _aware_utc(parsed)

    def _write_state(self, state: Mapping[str, Any]) -> None:
        path = self._state_root / ".maintenance.json"
        temporary = self._state_root / f".maintenance.{uuid.uuid4().hex}.tmp"
        document = {"version": _STATE_VERSION, **state}
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(document, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(self._state_root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
