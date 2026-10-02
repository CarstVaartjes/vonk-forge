"""Adaptive bound on concurrent model-download HTTP streams.

Every network stream of the Controller model cache (a sequential file or one
byte range of a large file) holds one permit while its body is read. The number
of permits climbs while aggregate throughput still grows and falls back when it
does not, or when the provider answers 429 or 5xx. The governor never touches
file contents: it counts bytes already being written, so there is no extra
hashing, and a stream that loses its permit simply resumes from its partial file.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

WINDOW_SECONDS = 10.0
LOG_INTERVAL_SECONDS = 60.0
# Throughput must grow by this fraction for another step up to be worth it.
GROWTH_FRACTION = 0.10
PLATEAU_HOLD_WINDOWS = 6
RAMP_STEP = 2
MIN_STREAMS = 1


@dataclass(frozen=True, slots=True)
class StreamStatus:
    limit: int
    active: int
    bytes_per_second: float
    reason: str


class StreamGovernor:
    def __init__(
        self,
        max_streams: int,
        *,
        initial_streams: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_streams < MIN_STREAMS:
            raise ValueError("stream limit must be positive")
        self._max = max_streams
        self._clock = clock
        self._cond = threading.Condition()
        self._limit = min(max_streams, initial_streams or 8)
        self._active = 0
        self._peak_active = 0
        self._window_started = clock()
        self._window_bytes = 0
        self._last_rate: float | None = None
        self._previous_limit: int | None = None
        self._hold_windows = 0
        self._rate = 0.0
        self._reason = "ramping"
        self._log_started = self._window_started
        self._log_bytes = 0

    @property
    def max_streams(self) -> int:
        return self._max

    def status(self) -> StreamStatus:
        with self._cond:
            return StreamStatus(self._limit, self._active, self._rate, self._reason)

    @contextmanager
    def stream(self, should_stop: Callable[[], bool]) -> Iterator[None]:
        """Hold one stream permit; raises InterruptedError if stopped while waiting."""
        with self._cond:
            while True:
                if should_stop():
                    raise InterruptedError(
                        "model download stopped; partial files preserved"
                    )
                now = self._clock()
                self._evaluate(now)
                if self._active < self._limit:
                    break
                self._cond.wait(timeout=1.0)
            self._active += 1
            self._peak_active = max(self._peak_active, self._active)
        try:
            yield
        finally:
            with self._cond:
                self._active -= 1
                self._cond.notify_all()

    def record_bytes(self, count: int) -> None:
        with self._cond:
            self._window_bytes += count
            self._log_bytes += count
            self._evaluate(self._clock())

    def throttled(self, retry_after_seconds: float | None, reason: str) -> None:
        """Back off after a provider 429/5xx: halve the limit and hold the ramp."""
        with self._cond:
            now = self._clock()
            self._limit = max(MIN_STREAMS, self._limit // 2)
            self._previous_limit = None
            self._last_rate = None
            self._hold_windows = PLATEAU_HOLD_WINDOWS
            # Retry-After itself is honoured by the service's provider
            # cooldown, which refuses every Hugging Face request until it ends;
            # here the halved limit and the hold keep the ramp from resuming
            # at full width the moment the cooldown lifts.
            self._reason = reason
            self._reset_window(now)
            _LOGGER.warning(
                "model download throttled: %s; streams limited to %d (retry after %ss)",
                reason,
                self._limit,
                retry_after_seconds
                if retry_after_seconds is not None
                else "unspecified",
            )
            self._cond.notify_all()

    def tick(self) -> None:
        """Evaluate and log even while no stream is moving bytes."""
        with self._cond:
            self._evaluate(self._clock())

    def _reset_window(self, now: float) -> None:
        self._window_started = now
        self._window_bytes = 0
        self._peak_active = self._active

    def _evaluate(self, now: float) -> None:
        elapsed = now - self._window_started
        if now - self._log_started >= LOG_INTERVAL_SECONDS:
            span = now - self._log_started
            if self._active or self._log_bytes:
                _LOGGER.info(
                    "model downloads: %.1f MB/s aggregate, %d active streams, "
                    "stream limit %d of %d (%s)",
                    self._log_bytes / span / 1e6,
                    self._active,
                    self._limit,
                    self._max,
                    self._reason,
                )
            self._log_started = now
            self._log_bytes = 0
        if elapsed < WINDOW_SECONDS:
            return
        rate = self._window_bytes / elapsed
        self._rate = rate
        saturated = self._peak_active >= self._limit
        if self._hold_windows > 0:
            self._hold_windows -= 1
            if self._hold_windows == 0:
                self._last_rate = None
                self._reason = "ramping"
        elif self._previous_limit is not None:
            # A step up was taken last window: keep it only if it paid off.
            baseline = self._last_rate or 0.0
            if rate < baseline * (1 + GROWTH_FRACTION):
                self._limit = self._previous_limit
                self._hold_windows = PLATEAU_HOLD_WINDOWS
                self._reason = "plateau: more streams did not raise throughput"
            elif saturated and self._limit < self._max:
                self._step_up(rate)
            else:
                self._last_rate = rate
                self._previous_limit = None
        elif saturated and self._limit < self._max and rate > 0:
            self._step_up(rate)
        elif self._limit >= self._max:
            self._reason = "at stream cap"
        self._reset_window(now)
        self._cond.notify_all()

    def _step_up(self, rate: float) -> None:
        self._previous_limit = self._limit
        self._last_rate = rate
        self._limit = min(self._max, self._limit + RAMP_STEP)
        self._reason = "ramping"
