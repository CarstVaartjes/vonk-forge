"""Construction failure isolation; one nonblocking owner and bounded retry rate.

Facades bind methods without constructing dependencies. Only construction is
retried: effects of an invoked method remain owned by its existing reconciler.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from threading import Event, Lock, Thread
from typing import Any, cast

from fastapi import HTTPException

from .capability_contract import (
    CapabilityAvailability,
    CapabilityReason,
    CapabilityStatus,
    CapabilityUnavailableReply,
    ControllerCapability,
)


class RecoveringService[T]:
    def __init__(
        self,
        capability: ControllerCapability,
        service_type: type[T],
        factory: Callable[[], T],
        clock: Callable[[], datetime],
        check: Callable[[T], bool] | None = None,
        initialize: Callable[[T], object] | None = None,
        construction_timeout_seconds: float = 35.0,
    ):
        self._type = service_type
        self._factory = factory
        self._clock = clock
        self._check = check
        self._initialize = initialize
        self._initialized = False
        self._value: T | None = None
        self._lock = Lock()
        self._construction_timeout = construction_timeout_seconds
        self._generation = 0
        self._construction_deadline: datetime | None = None
        self._delay = 1.0
        self._next_health_check: datetime | None = None
        self.status = CapabilityStatus(
            capability=capability,
            availability=CapabilityAvailability.UNAVAILABLE,
            reason=CapabilityReason.INITIALIZING,
        )

    def attempt_construction(self, *, wait: bool = True) -> None:
        # Only ownership metadata is locked. A factory/initializer/probe must
        # never keep the owner locked while it performs blocking I/O.
        with self._lock:
            now = self._clock()
            if self._construction_deadline is not None:
                if now < self._construction_deadline:
                    return
                self._generation += 1
                self._construction_deadline = None
                self._unavailable(CapabilityReason.DEPENDENCY_UNAVAILABLE, now)
            if (
                self.status.next_attempt_at is not None
                and now < self.status.next_attempt_at
            ):
                return
            if self.status.availability == CapabilityAvailability.AVAILABLE and (
                self._check is None
                or (
                    self._next_health_check is not None
                    and now < self._next_health_check
                )
            ):
                return
            self._generation += 1
            generation = self._generation
            self._construction_deadline = now + timedelta(
                seconds=self._construction_timeout
            )
            if self.status.availability == CapabilityAvailability.UNAVAILABLE:
                self.status = CapabilityStatus(
                    capability=self.status.capability,
                    availability=CapabilityAvailability.UNAVAILABLE,
                    reason=CapabilityReason.INITIALIZING,
                    next_attempt_at=self._construction_deadline,
                )
            done = Event()
            value = self._value
            initialized = self._initialized
        try:
            Thread(
                target=self._construct,
                args=(generation, value, initialized, done),
                daemon=True,
            ).start()
        except Exception:  # noqa: BLE001 -- dispatch failure releases this attempt
            with self._lock:
                if self._generation == generation:
                    self._construction_deadline = None
                    self._unavailable(
                        CapabilityReason.DEPENDENCY_UNAVAILABLE, self._clock()
                    )
            return
        if wait and not done.wait(timeout=self._construction_timeout):
            with self._lock:
                if self._generation == generation:
                    self._generation += 1
                    self._construction_deadline = None
                    self._unavailable(
                        CapabilityReason.DEPENDENCY_UNAVAILABLE, self._clock()
                    )

    def _unavailable(self, reason: CapabilityReason, now: datetime) -> None:
        self.status = CapabilityStatus(
            capability=self.status.capability,
            availability=CapabilityAvailability.UNAVAILABLE,
            reason=reason,
            next_attempt_at=now + timedelta(seconds=self._delay),
        )
        self._delay = min(60.0, self._delay * 2)

    def _construct(
        self, generation: int, value: T | None, initialized: bool, done: Event
    ) -> None:
        reason: CapabilityReason | None = None
        try:
            value = value if value is not None else self._factory()
            if not initialized:
                if self._initialize is not None:
                    self._initialize(value)
                initialized = True
            if self._check is not None and not self._check(value):
                reason = CapabilityReason.DEPENDENCY_UNAVAILABLE
        except Exception as error:  # noqa: BLE001 -- isolate construction from the process
            reason = (
                CapabilityReason.CONFIGURATION_INVALID
                if isinstance(error, (ValueError, TypeError))
                else CapabilityReason.STORAGE_UNAVAILABLE
                if isinstance(error, OSError)
                else CapabilityReason.DEPENDENCY_UNAVAILABLE
            )
        with self._lock:
            if self._generation == generation:
                self._construction_deadline = None
                now = self._clock()
                self._value = value
                self._initialized = initialized
                if reason is not None:
                    self._unavailable(reason, now)
                else:
                    self._value = value
                    self._initialized = True
                    self._delay = 1.0
                    self._next_health_check = now + timedelta(seconds=30)
                    self.status = CapabilityStatus(
                        capability=self.status.capability,
                        availability=CapabilityAvailability.AVAILABLE,
                    )
        done.set()

    def require_service(self) -> T:
        self.attempt_construction(wait=False)
        if (
            self._value is None
            or self.status.availability != CapabilityAvailability.AVAILABLE
        ):
            raise HTTPException(
                status_code=503,
                detail=CapabilityUnavailableReply(
                    capability=self.status.capability,
                    reason=self.status.reason or CapabilityReason.INITIALIZING,
                ),
            )
        return self._value

    def __getattr__(self, name: str) -> Any:
        # Methods captured by another service during wiring stay lazy too.
        if callable(getattr(self._type, name, None)):

            def invoke(*args: Any, **kwargs: Any) -> Any:
                if name == "close":
                    return (
                        None
                        if self._value is None
                        else getattr(self._value, name)(*args, **kwargs)
                    )
                return getattr(self.require_service(), name)(*args, **kwargs)

            # Like a bound method: the owner stays inspectable (e.g. its root).
            vars(invoke)["__self__"] = self
            return invoke
        return getattr(self.require_service(), name)


class CapabilityRegistry:
    def __init__(self, *, clock: Callable[[], datetime] | None = None):
        self._clock = clock or (lambda: datetime.now(UTC))
        self._services: list[RecoveringService[Any]] = []
        self._stopping = False
        self._tasks: set[asyncio.Task[None]] = set()
        self._timers: list[asyncio.TimerHandle] = []

    def guard[T](
        self,
        capability: ControllerCapability,
        service_type: type[T],
        factory: Callable[[], T],
        *,
        check: Callable[[T], bool] | None = None,
        initialize: Callable[[T], object] | None = None,
    ) -> T:
        return cast(
            T,
            self.provider(
                capability, service_type, factory, check=check, initialize=initialize
            ),
        )

    def provider[T](
        self,
        capability: ControllerCapability,
        service_type: type[T],
        factory: Callable[[], T],
        *,
        check: Callable[[T], bool] | None = None,
        initialize: Callable[[T], object] | None = None,
    ) -> RecoveringService[T]:
        service = RecoveringService(
            capability,
            service_type,
            factory,
            self._clock,
            check=check,
            initialize=initialize,
        )
        self._services.append(service)
        return service

    def statuses(self) -> list[CapabilityStatus]:
        # Observation never performs construction or changes retry state.
        return [service.status for service in self._services]

    def retry_due(self) -> None:
        for service in self._services:
            service.attempt_construction()

    def start_recovery(self) -> None:
        self._stopping = False
        for service in self._services:
            self._schedule(service)

    def _schedule(self, service: RecoveringService[Any]) -> None:
        if self._stopping or (
            service.status.availability == CapabilityAvailability.AVAILABLE
            and service._check is None
        ):
            return
        loop = asyncio.get_running_loop()
        timer = loop.call_later(1.0, self._dispatch_capability_retry, service)
        # Only the pending timer per capability is retained.
        self._timers = [
            handle
            for handle in self._timers
            if not handle.cancelled() and handle.when() > loop.time()
        ]
        self._timers.append(timer)

    def _dispatch_capability_retry(self, service: RecoveringService[Any]) -> None:
        if self._stopping:
            return
        task = asyncio.create_task(self._retry_one(service))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _retry_one(self, service: RecoveringService[Any]) -> None:
        # Dispatch performs no blocking constructor I/O. The generation deadline
        # fences late results and the next scheduler tick releases expired ownership.
        try:
            service.attempt_construction(wait=False)
        finally:
            self._schedule(service)

    async def stop_recovery(self) -> None:
        self._stopping = True
        for timer in self._timers:
            asyncio.TimerHandle.cancel(timer)
        tasks = tuple(self._tasks)
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=5.0)
            except TimeoutError:
                pass
