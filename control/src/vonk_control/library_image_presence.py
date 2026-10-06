"""Bounded, shared answers to "is this runtime image stored?" for the Library.

The Library only reports whether an image is stored; it never gates on it. Each
answer costs a manifest read and a size check of every layer on the Controller's
image store, which is network storage in production, and a Library read asks
about one image per recipe. So the Library asks once per distinct image, asks
concurrently, waits at most a fixed budget, and keeps recent answers for a few
seconds. An image whose answer did not arrive in time is ``unknown`` in that
response: the probe finishes in the background and the next read has it.

The checks that decide whether work may run (install, load, preparation) call
the image store directly and never read this index.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Collection, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Literal

from vonk_agent_protocol import RuntimeImageCode

#: Stored images are keyed by their address (manifest digest) and stored size.
ImageKey = tuple[str, int]

_UNREADABLE_CODE = RuntimeImageCode.ARCHIVE_UNAVAILABLE
_MAX_REMEMBERED = 8192


@dataclass(frozen=True, slots=True)
class ImagePresence:
    state: Literal["present", "absent", "unreadable", "unknown"]
    #: The safe error code of an unreadable image.
    code: str | None = None


UNKNOWN = ImagePresence("unknown")


def _probe_result(probe: Callable[[str, int], bool], key: ImageKey) -> ImagePresence:
    try:
        stored = probe(*key)
    except Exception as error:  # noqa: BLE001 - one unreadable image never fails the Library
        code = getattr(error, "code", None)
        return ImagePresence(
            "unreadable", code if isinstance(code, str) else _UNREADABLE_CODE
        )
    return ImagePresence("present" if stored else "absent")


class ImagePresenceIndex:
    """Concurrent, deduplicated, time-bounded image presence lookups."""

    def __init__(
        self,
        probe: Callable[[str, int], bool],
        *,
        present_ttl_seconds: float = 0.0,
        absent_ttl_seconds: float = 0.0,
        workers: int = 8,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._probe = probe
        self._present_ttl = present_ttl_seconds
        self._absent_ttl = absent_ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._remembered: dict[ImageKey, tuple[ImagePresence, float]] = {}
        self._in_flight: dict[ImageKey, Future[ImagePresence]] = {}
        self._pool = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="library-image-probe"
        )

    def _ttl(self, presence: ImagePresence) -> float:
        return self._present_ttl if presence.state == "present" else self._absent_ttl

    def _remember(self, key: ImageKey, presence: ImagePresence) -> None:
        ttl = self._ttl(presence)
        with self._lock:
            self._in_flight.pop(key, None)
            if ttl <= 0:
                return
            if len(self._remembered) >= _MAX_REMEMBERED:
                now = self._clock()
                self._remembered = {
                    item: entry
                    for item, entry in self._remembered.items()
                    if entry[1] > now
                }
                if len(self._remembered) >= _MAX_REMEMBERED:
                    return
            self._remembered[key] = (presence, self._clock() + ttl)

    def _run(self, key: ImageKey) -> ImagePresence:
        presence = _probe_result(self._probe, key)
        self._remember(key, presence)
        return presence

    def lookup(
        self, wanted: Collection[ImageKey], *, budget_seconds: float
    ) -> Mapping[ImageKey, ImagePresence]:
        """Answer for every distinct key; keys not answered in budget are unknown."""

        answers: dict[ImageKey, ImagePresence] = {}
        pending: dict[ImageKey, Future[ImagePresence]] = {}
        now = self._clock()
        with self._lock:
            for key in set(wanted):
                remembered = self._remembered.get(key)
                if remembered is not None and remembered[1] > now:
                    answers[key] = remembered[0]
                    continue
                future = self._in_flight.get(key)
                if future is None:
                    future = self._pool.submit(self._run, key)
                    self._in_flight[key] = future
                pending[key] = future
        if pending:
            wait(pending.values(), timeout=max(0.0, budget_seconds))
        for key, future in pending.items():
            answers[key] = future.result() if future.done() else UNKNOWN
        return answers


__all__ = ["UNKNOWN", "ImageKey", "ImagePresence", "ImagePresenceIndex"]
