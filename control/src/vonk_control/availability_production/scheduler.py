from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from sqlalchemy import select
from vonk_agent_protocol import (
    InvalidRequestReason,
)

from ..categorized_errors import InvalidValue
from ..models.jobs import Job
from ..recipe_image_availability.contracts import OPERATION_KIND, REMOVE_OPERATION_KIND
from ..recipe_image_availability.service import RecipeImageAvailabilityService
from ..recipe_update_batches.contract import UPDATE_KIND
from ..worker.memory_contract import WorkerMemoryComponent


class RecipeImageAvailabilityScheduler:
    """Dispatch durable image claims on a bounded, independent executor."""

    def __init__(
        self,
        service: RecipeImageAvailabilityService,
        *,
        max_workers: int = 4,
        owner_id: str | None = None,
    ) -> None:
        if not 1 <= max_workers <= 16:
            raise InvalidValue(
                "availability scheduler parallelism is invalid",
                reason=InvalidRequestReason.OUT_OF_RANGE,
            )
        self._service = service
        self._max_workers = max_workers
        self._executor = ThreadPoolExecutor(
            # One coordination slot cannot consume the slots its image
            # children need. It admits/observes one child, then releases.
            max_workers=max_workers + 1,
            thread_name_prefix="vonk-recipe-image-availability",
        )
        self._owner_id = owner_id or f"availability-{uuid.uuid4().hex}"
        self._futures: set[Future[None]] = set()
        self._update_future: Future[None] | None = None
        self._last_busy_note = 0.0
        self._lock = threading.Lock()
        self._closed = False

    @property
    def executor(self) -> ThreadPoolExecutor:
        """Expose the owned executor for lifecycle tests and diagnostics."""

        return self._executor

    def tick(self, operation_id: str | None = None) -> int:
        """Claim and submit due work without waiting for image I/O or builds."""

        kind = None
        if operation_id is not None:
            with self._service._sessions() as session:
                kind = session.scalar(select(Job.kind).where(Job.id == operation_id))
            if kind is None:
                return 0
        with self._lock:
            if self._closed:
                return 0
            if operation_id is None:
                self._service.reconcile_cancellations(limit=self._max_workers)
                self._service.advance_removals(limit=1)
            elif kind == OPERATION_KIND:
                self._service._reconcile_availability_cancellation(operation_id)
            elif kind == REMOVE_OPERATION_KIND:
                return int(self._service._advance_recipe_removal(operation_id))
            elif kind == UPDATE_KIND:
                self._service._updates.reconcile_cancellations(
                    limit=1, requested_id=operation_id
                )
            submitted = 0
            if kind in (None, UPDATE_KIND) and (
                self._update_future is None or self._update_future.done()
            ):
                update_claim = self._service.claim_update(self._owner_id, operation_id)
                if update_claim is not None:
                    self._update_future = self._executor.submit(
                        self._service.run_update_claim, update_claim
                    )
                    submitted += 1
            if kind == UPDATE_KIND:
                return submitted
            self._futures = {future for future in self._futures if not future.done()}
            capacity = self._max_workers - len(self._futures)
            if capacity <= 0:
                # Every worker is busy: queued work says so instead of looking idle.
                if time.monotonic() - self._last_busy_note >= 5:
                    self._last_busy_note = time.monotonic()
                    self._service.note_waiting_for_worker(self._max_workers)
                return submitted
            claims = self._service.claim_pending(
                limit=capacity,
                owner_id=self._owner_id,
                operation_id=operation_id,
            )
            for claim in claims:
                self._futures.add(self._executor.submit(self._run, claim))
            return len(claims) + submitted

    def _run(self, claim: Any) -> None:
        self._service.run_claim(claim)

    def memory_footprint(self) -> dict[WorkerMemoryComponent, int]:
        with self._lock:
            return {WorkerMemoryComponent.IMAGE_PREPARATION_FUTURES: len(self._futures)}

    def close(self) -> None:
        """Stop dispatch and release threads; durable claims remain restartable."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
