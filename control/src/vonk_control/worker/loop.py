from __future__ import annotations

import hashlib
import logging
import time
import traceback
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass

from vonk_agent_protocol import (
    LifecycleEventKind,
    UnknownOutcomeError,
    WaitReason,
)

from ..bounded_retry import bounded_attempts
from ..coordinator_failures import CoordinatorFailures
from ..failure_classification import disposition
from ..jobs.queue import JobService
from ..logging import log_event, redact_text
from ..operator_receipt_contract import ReceiptOwner
from .liveness import _READINESS_BEAT_SECONDS
from .memory_contract import WorkerMemoryComponent

_LOGGER = logging.getLogger("vonk-control-worker")

#: A step that takes longer is named by the over-budget alarm.
_STEP_BUDGET_SECONDS = 10.0


def _background_source_name(service: Callable[[], object]) -> str:
    """Stable custody across optional-service changes and worker reconstruction."""
    module = getattr(service, "__module__", type(service).__module__)
    name = getattr(service, "__qualname__", type(service).__qualname__)
    code = getattr(service, "__code__", None)
    identity = f"background:{module}.{name}:{getattr(code, 'co_firstlineno', 0)}"
    return (
        identity
        if len(identity) <= 128
        else f"background:{hashlib.sha256(identity.encode()).hexdigest()}"
    )


@dataclass(frozen=True)
class HandlerRequest(Mapping[str, object]):
    job_id: str
    kind: str
    payload: Mapping[str, object]
    authority_revision: str
    targets: tuple[str, ...]

    def __getitem__(self, key: str) -> object:
        return self.payload[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.payload)

    def __len__(self) -> int:
        return len(self.payload)


Handler = Callable[[HandlerRequest], Mapping[str, object]]


class Worker:
    def __init__(
        self,
        jobs: JobService,
        worker_id: str,
        handlers: Mapping[str, Handler],
        *,
        coordinator_failures: CoordinatorFailures,
        housekeeping: Callable[[], object] | None = None,
        artifact_housekeeping: Callable[[], object] | None = None,
        recipes=None,
        model_cache: object | None = None,
        background_services: Sequence[Callable[[], object]] = (),
        background_closers: Sequence[Callable[[], object]] = (),
        loop_heartbeat: Callable[[], object] | None = None,
        memory_sources: Sequence[object] = (),
        due_waits: Callable[[], object] | None = None,
        step_heartbeat: Callable[[str], object] | None = None,
    ) -> None:
        self._jobs = jobs
        self._worker_id = worker_id
        self._handlers = dict(handlers)
        self._due_waits = due_waits
        self._housekeeping = housekeeping
        self._artifact_housekeeping = artifact_housekeeping
        self._recipes = recipes
        self._model_cache = model_cache
        self._background_services = tuple(background_services)
        self._background_closers = tuple(background_closers)
        self._loop_heartbeat = loop_heartbeat
        self._memory_sources = tuple(memory_sources)
        self._source_cursor = 0
        self._closed = False
        self._failures = coordinator_failures
        self._cadence_at: dict[str, float] = {}
        self._step_heartbeat = step_heartbeat
        self._readiness_at = time.monotonic()
        # The steps running now, outermost first (a redrive runs inside the
        # due-wait step), so the watchdog names the innermost one.
        self._running_steps: list[str] = []

    def close(self) -> None:
        """Close worker-owned executors while leaving durable work resumable."""

        if self._closed:
            return
        self._closed = True
        closers = list(self._background_closers)
        if self._model_cache is not None:
            close = getattr(self._model_cache, "close", None)
            if callable(close):
                closers.append(close)
        for closer in closers:
            for _attempt in bounded_attempts():
                try:
                    closer()
                    break
                except Exception as error:  # noqa: BLE001 - bounded shutdown for each owner
                    log_event(
                        _LOGGER,
                        "worker.shutdown_checkpoint_deferred",
                        service="control-worker",
                        message=redact_text(error),
                        resume="next shutdown attempt or Controller restart",
                    )

    def memory_footprint(self) -> dict[WorkerMemoryComponent, int]:
        """Sizes of the long-lived collections the worker's services hold.

        Each source exposes ``memory_footprint()``; one that fails is skipped so
        the report still carries the rest.
        """

        sizes: dict[WorkerMemoryComponent, int] = {}
        for source in self._memory_sources:
            footprint: Callable[[], Mapping[WorkerMemoryComponent, int]] | None = (
                getattr(source, "memory_footprint", None)
            )
            if not callable(footprint):
                continue
            try:
                sizes.update(footprint())
            except Exception:
                _LOGGER.warning("memory footprint source failed", exc_info=True)
        return sizes

    def run_once(self) -> bool:
        # Housekeeping runs before all work, so one failing maintenance task
        # must not deny the heartbeat and every source their turn.  Each task
        # is contained and reported; the outer loop retries it next pass.
        for name, task in self._housekeeping_table():
            if name != LifecycleEventKind.HEARTBEAT:  # last, after the sources
                self._run_housekeeping(
                    name, task, minimum_interval=1 if name == "due-waits" else 0
                )
        sources = self._source_table()
        if self._source_cursor >= len(sources):
            self._source_cursor = 0
        advanced = False
        for offset in range(len(sources)):
            index = (self._source_cursor + offset) % len(sources)
            name, source = sources[index]
            if not self._failures.ready(name):
                continue
            try:
                progressed = self._timed(name, source)
            except Exception as error:  # noqa: BLE001 - isolate one owner, report its fault
                # A source that keeps failing must not starve the others: the
                # failure stays visible, the turn moves on, and the failed
                # source is retried once its backoff passes (ready()).
                self._failures.failed(name, error, disposition(error))
                log_event(
                    _LOGGER,
                    "worker.source_failed",
                    service="control-worker",
                    source=name,
                    error=type(error).__name__,
                    message=redact_text(error),
                    traceback=redact_text(traceback.format_exc()),
                )
                # Drain due waits after every step: a due owner waits one step,
                # not a full rotation behind slow sources.
                self._run_housekeeping("due-waits", self._due_waits, minimum_interval=1)
                continue
            self._failures.succeeded(name)
            self._run_housekeeping("due-waits", self._due_waits, minimum_interval=1)
            if progressed:
                self._source_cursor = (index + 1) % len(sources)
                advanced = True
                break
        self._readiness_at = time.monotonic()
        self._run_housekeeping(LifecycleEventKind.HEARTBEAT, self._loop_heartbeat)
        return advanced

    def _timed[Result](self, name: str, call: Callable[[], Result]) -> Result:
        """Run one step in the pass thread and time it.

        The watchdog beats first, naming the step, so only one step that
        outlives its timeout restarts the worker, never a long pass of short
        steps. A step slower than the budget is named by an alarm (observation
        only: it is scheduled like any other step). A long pass writes the
        readiness heartbeat between steps, before naming the next one.
        """
        if (
            self._loop_heartbeat is not None
            and time.monotonic() - self._readiness_at >= _READINESS_BEAT_SECONDS
        ):
            self._readiness_at = time.monotonic()
            self._run_housekeeping(LifecycleEventKind.HEARTBEAT, self._loop_heartbeat)
        self._running_steps.append(name)
        self._beat(name)
        started = time.monotonic()
        try:
            result = call()
        finally:
            self._running_steps.pop()
            if self._running_steps:
                self._beat(self._running_steps[-1])
        elapsed = time.monotonic() - started
        if elapsed > _STEP_BUDGET_SECONDS:
            log_event(
                _LOGGER,
                "worker.step_over_budget",
                service="control-worker",
                step=name,
                budget_seconds=_STEP_BUDGET_SECONDS,
                elapsed_seconds=round(elapsed, 1),
            )
        return result

    def _beat(self, step: str) -> None:
        if self._step_heartbeat is not None:
            self._step_heartbeat(step)

    def _run_housekeeping(
        self,
        name: str,
        task: Callable[[], object] | None,
        *,
        minimum_interval: float = 0,
    ) -> None:
        if task is None:
            return
        key = f"housekeeping:{name}"
        now = time.monotonic()
        if now < self._cadence_at.get(key, now) or not self._failures.ready(key):
            return
        try:
            self._timed(key, task)
        except Exception as error:  # noqa: BLE001 - isolate one owner, report its fault
            self._failures.failed(key, error, disposition(error))
            log_event(
                _LOGGER,
                "worker.housekeeping_failed",
                service="control-worker",
                task=name,
                error=type(error).__name__,
                message=redact_text(error),
                traceback=redact_text(traceback.format_exc()),
            )
        else:
            self._failures.succeeded(key)
            if minimum_interval:
                self._cadence_at[key] = now + minimum_interval

    def _source_table(self) -> list[tuple[str, Callable[[], bool]]]:
        """Every work source by its durable custody name, in dispatch order."""
        sources: list[tuple[str, Callable[[], bool]]] = []
        if self._recipes is not None:
            sources.append(("recipes", self._recipes.tick))
        if self._model_cache is not None:
            sources.append(("model-cache", self._run_model_cache))
        for service in self._background_services:
            sources.append(
                (
                    _background_source_name(service),
                    lambda service=service: bool(service()),
                )
            )
        sources.append(("jobs", self._run_generic))
        return sources

    def _housekeeping_table(self) -> list[tuple[str, Callable[[], object] | None]]:
        return [
            ("due-waits", self._due_waits),
            (ReceiptOwner.TELEMETRY, self._housekeeping),
            ("artifact", self._artifact_housekeeping),
            (LifecycleEventKind.HEARTBEAT, self._loop_heartbeat),
        ]

    def redrive_coordinator(self, source: str) -> None:
        if source.startswith("housekeeping:"):
            name = source.removeprefix("housekeeping:")
            task = dict(self._housekeeping_table()).get(name)
            # The due backstop is already executing; its outer caller owns recovery.
            if task is None:
                self._failures.succeeded(source)
            elif name != "due-waits":
                self._run_housekeeping(name, task)
            return
        task = dict(self._source_table()).get(source)
        if task is None:
            self._failures.succeeded(source)
            return
        if not self._failures.ready(source):
            return
        try:
            self._timed(source, task)
        except Exception as error:  # noqa: BLE001 - same source custody as normal dispatch
            self._failures.failed(source, error, disposition(error))
        else:
            self._failures.succeeded(source)

    def _run_model_cache(self) -> bool:
        # The model cache is consumed structurally, like the distribution
        # executor does: either entry point is optional, so probe both rather
        # than assume one shape.
        tick = getattr(self._model_cache, "tick", None)
        if callable(tick):
            return bool(tick())
        run_pending = getattr(self._model_cache, "run_pending", None)
        if callable(run_pending):
            return bool(run_pending(limit=1))
        raise UnknownOutcomeError(
            "model cache observation entry point is unavailable",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )

    def _run_generic(self, job_id: str | None = None) -> bool:
        attempt = self._jobs.claim(
            self._worker_id, 30, kinds=tuple(self._handlers), job_id=job_id
        )
        if attempt is None:
            return False
        handler = self._handlers[attempt.kind]
        try:
            result = handler(
                HandlerRequest(
                    attempt.job_id,
                    attempt.kind,
                    attempt.payload,
                    attempt.authority_revision,
                    attempt.targets,
                )
            )
        except Exception as error:  # noqa: BLE001 - isolate one owner, report its fault
            self._jobs.fail(attempt, f"{type(error).__name__}: {error}")
        else:
            self._jobs.succeed(attempt, result)
        return True
