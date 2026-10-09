"""Recipe update batches: service."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.orm import Session, sessionmaker

from ..lifecycle.recipe_update_batch import RecipeUpdateBatchAdapter

if TYPE_CHECKING:
    from ..recipe_image_availability import RecipeImageAvailabilityService

from .activity import ActivityMixin
from .cancellation import CancellationMixin
from .execution import ExecutionMixin
from .projection import ProjectionMixin
from .requests import RequestsMixin


class RecipeUpdateBatches(
    RequestsMixin, ProjectionMixin, CancellationMixin, ActivityMixin, ExecutionMixin
):
    def __init__(
        self, owner: RecipeImageAvailabilityService, sessions: sessionmaker[Session]
    ) -> None:
        self.owner = owner
        self.sessions = sessions
        self._lifecycle = RecipeUpdateBatchAdapter()
