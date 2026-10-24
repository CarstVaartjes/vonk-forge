"""Single-flight storage bytes observation, independent of the automatic owner."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta

from .model_cache_contract import (
    ModelCacheUsage,
    ModelStorageObservation,
    ModelStorageObservationReason,
    ModelStorageSnapshot,
)
from .settings import RECIPE_LIBRARY_SYNC_INTERVAL_SECONDS

STORAGE_OBSERVATION_DEADLINE_SECONDS = RECIPE_LIBRARY_SYNC_INTERVAL_SECONDS


class LibraryStorageObserver:
    def __init__(
        self,
        provider: Callable[[], Mapping[str, ModelCacheUsage]] | None,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._provider = provider
        self._clock = clock
        self._lock = threading.Lock()
        self._snapshot = ModelStorageSnapshot()
        self._thread: threading.Thread | None = None
        self._started_at: datetime | None = None

    def refresh(self) -> None:
        if self._provider is None:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._started_at = self._clock()
            self._thread = threading.Thread(
                target=self._observe, args=(self._started_at,), daemon=True
            )
            self._thread.start()

    def _observe(self, started_at: datetime) -> None:
        assert self._provider is not None
        try:
            usage = self._provider()
            snapshot = ModelStorageSnapshot(
                observation=ModelStorageObservation(
                    storage_observed_at=started_at,
                ),
                usage=dict(usage),
            )
        except Exception as error:  # noqa: BLE001 - isolate storage observation
            snapshot = ModelStorageSnapshot(
                observation=self._snapshot.observation.model_copy(
                    update={
                        "storage_reason": ModelStorageObservationReason.FAILED,
                        "storage_error_type": type(error).__name__,
                    }
                ),
                usage=self._snapshot.usage,
            )
        with self._lock:
            self._snapshot = snapshot
            self._started_at = None

    def snapshot(self) -> ModelStorageSnapshot:
        with self._lock:
            snapshot = self._snapshot
            started = self._started_at
        observation = snapshot.observation
        now = self._clock()
        if started is not None:
            deadline = started + timedelta(seconds=STORAGE_OBSERVATION_DEADLINE_SECONDS)
            observation = observation.model_copy(
                update={"storage_deadline_at": deadline}
            )
            if now >= deadline:
                observation = observation.model_copy(
                    update={
                        "storage_reason": ModelStorageObservationReason.DEADLINE_EXCEEDED,
                        # The hang is this walk's reason, never an earlier error.
                        "storage_error_type": None,
                    }
                )
        elif (
            observation.storage_reason is None
            and observation.storage_observed_at is not None
            and now
            >= observation.storage_observed_at
            + timedelta(
                seconds=RECIPE_LIBRARY_SYNC_INTERVAL_SECONDS
                + STORAGE_OBSERVATION_DEADLINE_SECONDS
            )
        ):
            observation = observation.model_copy(
                update={
                    "storage_reason": ModelStorageObservationReason.STALE,
                }
            )
        result = snapshot.model_copy()
        result.observation = observation
        return result
