"""Assignment persistence for Fleet profiles."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from sqlalchemy.orm import Session

from ..artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactLifecycleError,
    lock_reference_gates,
    require_reference_open,
)
from ..artifact_reference_scan import require_model_sets_open
from ..catalog_revision_contract import RecipeRevisionProjection, read_recipe_projection
from ..fleet_profile_contract import FleetProfileAssignmentInput, FleetProfilePreview
from ..lifecycle.evidence import Residue
from ..model_cache.persistence import _operation_removal
from ..models import ModelCacheOperation
from .contracts import (
    FleetProfileAdmissionEffectBusy,
)
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
        projections: dict[str, RecipeRevisionProjection] = {}
        referenced: set[str] = set()
        for value in values:
            resolved = self._recipe_document(session, value.recipe_selector)
            if isinstance(resolved, Residue):
                # A well-formed selector already resolved to catalog identity.
                # Missing active bytes are local evidence, not malformed input.
                assignments.append(value)
                notes.append(
                    f"{value.recipe_selector}: catalog evidence is unavailable"
                )
                continue
            document, revision = resolved
            try:
                if value.recipe_selector not in projections:
                    projections[value.recipe_selector] = read_recipe_projection(
                        revision
                    )
                definition = projections[value.recipe_selector]
            except ValueError:
                # Keep authoring intent while the catalog owner repairs evidence.
                assignments.append(value)
                notes.append(
                    f"{value.recipe_selector}: revision {revision.id} is unreadable"
                )
                continue
            # Saved profiles are references too. Use the same object gates as
            # fleet removal, even when the NAS has no artifact set for a model.
            # A logical profile may supersede an older NAS-only removal through
            # the existing load path; only fleet removal adds this draft guard.
            # This short reference transaction never spans remote work.
            digests = {
                reference.content_sha256 for reference in definition.models
            } - referenced
            from ..model_cache.fleet_removal import _model_objects

            try:
                objects: set[str] = set()
                for digest in sorted(digests):
                    named, _shared = _model_objects(
                        session, digest, include_shared=False
                    )
                    objects.update(named)
                identities = tuple(
                    ArtifactIdentity("model-object", digest)
                    for digest in sorted(objects)
                )
                gates = lock_reference_gates(session, identities, now=self._clock())
                fleet_owned: list[ArtifactIdentity] = []
                for gate in gates:
                    if gate.removal_owner_id is None:
                        continue
                    owner = session.get(ModelCacheOperation, gate.removal_owner_id)
                    payload = None if owner is None else _operation_removal(owner)
                    if gate.removal_authority_expires_at is not None or (
                        payload is not None
                        and not isinstance(payload, Residue)
                        and payload.fleet is not None
                    ):
                        fleet_owned.append(
                            ArtifactIdentity("model-object", gate.artifact_sha256)
                        )
                require_reference_open(session, fleet_owned, now=self._clock())
            except ArtifactLifecycleError as error:
                raise FleetProfileAdmissionEffectBusy(error.detail) from error
            referenced.update(digests)
            # Every option is saved with an explicit value: the operator's
            # choice, or the recipe default where none was made or offered.
            choices, replaced = _effective_option_choices(
                definition, value.option_choices
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
