"""Executor."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import (
    TYPE_CHECKING,
)

from sqlalchemy.orm import Session, sessionmaker

from ..categorized_errors import (
    MissingRecord,
)
from ..cluster_mappings import (
    ClusterMappingService,
)
from ..lifecycle_preflight import LifecyclePreflight
from ..recipe_operations import (
    RecipeOperationService,
    RecipeOperationView,
)
from ..run_switch_contract import (
    RunSwitchOperation,
)
from .executor_phases import ExecutorPhasesMixin
from .executor_preflight import ExecutorPreflightMixin
from .executor_verification import ExecutorVerificationMixin
from .interfaces import RunSwitchArtifactPhaseExecutor

if TYPE_CHECKING:
    from ..distribution_executor import _ChildView


class RecipeLifecyclePhaseExecutor(
    ExecutorPhasesMixin, ExecutorPreflightMixin, ExecutorVerificationMixin
):
    def __init__(
        self,
        lifecycle: RecipeOperationService,
        sessions: sessionmaker[Session],
        mappings: ClusterMappingService,
        clock: Callable[[], datetime],
        artifact_executor: RunSwitchArtifactPhaseExecutor | None = None,
        inventory_max_age_seconds: int = 300,
    ) -> None:
        self._lifecycle = lifecycle
        self._sessions = sessions
        self._mappings = mappings
        self._clock = clock
        self._artifact_executor = artifact_executor
        self._inventory_max_age = inventory_max_age_seconds
        self._preflight: LifecyclePreflight | None = None

    def abandon(
        self, session: Session, operation_id: str, now: datetime, *, reason: str
    ) -> bool:
        """Close a parked idempotent artifact child; lifecycle children never are."""

        abandon = getattr(self._artifact_executor, "abandon", None)
        return bool(
            callable(abandon) and abandon(session, operation_id, now, reason=reason)
        )

    def get(
        self, operation_id: str
    ) -> RecipeOperationView | _ChildView | RunSwitchOperation:
        """Resolve an artifact child first, then an existing recipe child."""

        if self._artifact_executor is not None:
            try:
                child = self._artifact_executor.get(operation_id)
            except KeyError:
                child = None
            if child is not None:
                return child
        if self._lifecycle is not None:
            return self._lifecycle.get(operation_id)
        raise MissingRecord(operation_id)
