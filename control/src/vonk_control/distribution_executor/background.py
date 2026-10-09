"""Collect abandoned image preparations independently of parent phase polling."""

from __future__ import annotations

from concurrent.futures import Future
from datetime import timedelta
from threading import RLock
from typing import TYPE_CHECKING, cast

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from vonk_agent_protocol import LifecycleState

from ..logging import redact_text
from ..models import AgentNode, Job
from ..run_switch_contract import RunSwitchRuntimeImageResult
from ..run_switch_operations.constants import _FINAL_VERIFICATION_MAX_SECONDS
from ..run_switch_operations.planning_helpers import _run_switch_payload
from ..run_switch_operations.result_helpers import _parse_persisted_result
from ..stored_json import read_row_column

if TYPE_CHECKING:
    from .preparation import CompositeDistributionPhaseExecutor


class BackgroundPreparations:
    """The parent publication fence remains the authority for running effects."""

    _runtime_image_lock: RLock

    def _background_completed(
        self, future: Future[RunSwitchRuntimeImageResult | None]
    ) -> None:
        executor = cast("CompositeDistributionPhaseExecutor", self)
        with self._runtime_image_lock:
            executor._runtime_image_inflight.discard(future)
        # Database observation belongs to the worker tick, outside the future
        # callback: completion can race the transaction that submitted it.

    def reconcile_background(self) -> bool:
        """Cancel queued obsolete work and collect results even after parent end.

        Running filesystem work cannot be killed safely in a Python thread. Its
        publication callback revalidates the exact parent/phase/intent; removing
        its future here cannot permit an obsolete result to publish. Other
        preparations have separate executor slots and parent-owned deadlines.
        """
        from .preparation import _LOGGER

        executor = cast("CompositeDistributionPhaseExecutor", self)
        with self._runtime_image_lock:
            pending = tuple(executor._runtime_image_futures.items())
        removed = False
        for key, submitted in pending:
            request_key, _phase, _item = key
            future, _started = submitted
            try:
                with executor._sessions() as session:
                    parent = session.scalar(
                        select(Job).where(Job.request_id == request_key)
                    )
                    payload = (
                        _run_switch_payload(parent) if parent is not None else None
                    )
                    progress = (
                        _parse_persisted_result(read_row_column(parent, "result"))
                        if parent is not None
                        else None
                    )
                    active = parent is not None and parent.state in {
                        LifecycleState.QUEUED,
                        LifecycleState.RUNNING,
                        LifecycleState.OBSERVING,
                    }
                    if active and parent is not None:
                        now = executor._clock()
                        deadline = (
                            progress.recovery_deadline_at
                            if progress is not None
                            else None
                        ) or (
                            parent.created_at
                            + timedelta(seconds=_FINAL_VERIFICATION_MAX_SECONDS)
                        )
                        if deadline.tzinfo is None:
                            deadline = deadline.replace(tzinfo=now.tzinfo)
                        active = now < deadline
                    if (
                        active
                        and parent is not None
                        and payload is not None
                        and progress is not None
                    ):
                        nodes = tuple(
                            session.scalars(
                                select(AgentNode).where(
                                    AgentNode.node_id.in_(parent.targets)
                                )
                            )
                        )
                        active = (
                            progress.cancellation is None
                            and len(nodes) == len(parent.targets)
                            and all(
                                node.workload_intent_ordinal
                                == payload.workload_intent_ordinal
                                for node in nodes
                            )
                        )
                    # An unreadable live projection is observed by the parent;
                    # its immutable deadline ends it, never a new publication.
                    if active:
                        continue
            except (SQLAlchemyError, OSError, TypeError, ValueError) as error:
                _LOGGER.warning(
                    "background parent observation unavailable: %s", redact_text(error)
                )
                continue
            with self._runtime_image_lock:
                if executor._runtime_image_futures.get(key) is not submitted:
                    continue
                del executor._runtime_image_futures[key]
            future.cancel()
            if future.done() and not future.cancelled():
                error = future.exception()
                if error is not None:
                    _LOGGER.warning(
                        "abandoned image preparation ended: %s", redact_text(error)
                    )
            removed = True
        return removed

    def end_background(self, request_key: str) -> None:
        """Parent-end hook: detach tasks immediately; publication stays fenced."""
        executor = cast("CompositeDistributionPhaseExecutor", self)
        with self._runtime_image_lock:
            keys = tuple(
                key for key in executor._runtime_image_futures if key[0] == request_key
            )
            futures = tuple(executor._runtime_image_futures.pop(key)[0] for key in keys)
        for future in futures:
            future.cancel()
