"""Recipe update batches: service."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.orm import Session, sessionmaker

from vonk_control.lifecycle.recipe_update_batch import RecipeUpdateBatchAdapter

if TYPE_CHECKING:
    from vonk_control.recipe_image_availability.service import (
        RecipeImageAvailabilityService,
    )

from vonk_control.recipe_update_batches.activity import ActivityMixin
from vonk_control.recipe_update_batches.cancellation import CancellationMixin
from vonk_control.recipe_update_batches.execution import ExecutionMixin
from vonk_control.recipe_update_batches.projection import ProjectionMixin
from vonk_control.recipe_update_batches.requests import RequestsMixin


class RecipeUpdateBatches(
    RequestsMixin, ProjectionMixin, CancellationMixin, ActivityMixin, ExecutionMixin
):
    def __init__(
        self, owner: RecipeImageAvailabilityService, sessions: sessionmaker[Session]
    ) -> None:
        self.owner = owner
        self.sessions = sessions
        self._lifecycle = RecipeUpdateBatchAdapter()
