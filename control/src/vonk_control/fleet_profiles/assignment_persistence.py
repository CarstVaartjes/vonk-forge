"""Assignment persistence for Fleet profiles."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from sqlalchemy.orm import Session
from vonk_agent_protocol import InvalidRequestReason

from ..artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactLifecycleError,
    require_reference_open,
)
from ..artifact_reference_scan import require_model_sets_open
from ..fleet_profile_contract import FleetProfileAssignmentInput, FleetProfilePreview
from ..lifecycle.evidence import Residue
from .contracts import (
    FleetProfileAdmissionEffectBusy,
    FleetProfileInvalid,
)
from .persistence import _stored_recipe
from .projection_support import (
    _effective_option_choices,
)

if TYPE_CHECKING:
    from .service import FleetProfileService
    from .service import FleetProfileService as _FleetProfileService


class FleetProfileService:
    def _validated_assignments(
        self, session: Session, values: Sequence[FleetProfileAssignmentInput]
    ) -> tuple[list[FleetProfileAssignmentInput], list[str]]:
        """Canonical assignments to store, and what saving replaced.

        A profile re-submits every assignment on each edit, so a choice the
        recipe stopped offering (a refreshed revision removed or renamed it)
        must not block saving the profile. It is replaced by the recipe
        default, and the notes name each replacement for the caller to show.
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        assignments: list[FleetProfileAssignmentInput] = []
        notes: list[str] = []
        for value in values:
            resolved = self._recipe_document(session, value.recipe_selector)
            if isinstance(resolved, Residue):
                # A save names its recipes: one without an active revision cannot
                # be chosen (a malformed request, refused at submit time).
                raise FleetProfileInvalid(
                    f"recipe has no active catalog revision: {value.recipe_selector}",
                    reason=InvalidRequestReason.NOT_FOUND,
                )
            document, revision = resolved
            # Every option is saved with an explicit value: the operator's
            # choice, or the recipe default where none was made or offered.
            choices, replaced = _effective_option_choices(
                _stored_recipe(revision), value.option_choices
            )
            notes.extend(f"{value.recipe_selector}: {note}" for note in replaced)
            # Save the canonical catalog selector.  This is a logical recipe
            # choice; its active revision is deliberately resolved later.
            normalized = value.model_copy(
                update={
                    "recipe_selector": self._recipe_selector(document),
                    "option_choices": choices,
                }
            )
            assignments.append(normalized)
        return assignments, notes

    @staticmethod
    def _reserve_preview_assets(
        session: Session, preview: FleetProfilePreview, *, now: datetime
    ) -> None:
        """Gate every exact asset before accepting application effects."""

        model_sets = sorted(
            {item.model.artifact_set_sha256 for item in preview.preparation_decisions}
        )
        runtime_images = sorted(
            {
                item.runtime_image.oci_layout_sha256
                for item in preview.preparation_decisions
            }
        )
        try:
            if model_sets:
                require_model_sets_open(session, model_sets, now=now)
            if runtime_images:
                require_reference_open(
                    session,
                    (
                        ArtifactIdentity("runtime-image", digest)
                        for digest in runtime_images
                    ),
                    now=now,
                )
        except ArtifactLifecycleError as error:
            # The artifact owner holds the gate (a removal or an inspection is in
            # progress): admission waits, parked and retried by its owner, and the
            # load is planned again against what is there when the gate opens.
            raise FleetProfileAdmissionEffectBusy(
                f"{error.code}: {error.detail}"
            ) from error
