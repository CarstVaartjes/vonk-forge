"""Recipe choices for Fleet profiles."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from vonk_agent_protocol import DesiredAssignmentState, InvalidRequestReason
from vonk_agent_protocol.agent_words import ProfileDocumentState

from ..catalog_revision_contract import read_catalog_document
from ..fleet_profile_contract import (
    FleetProfileAssignment,
    FleetProfileAssignmentInput,
    FleetProfileNode,
)
from ..lifecycle.evidence import BookkeepingReason, Residue, retire_as_unknown
from ..model_cache_contract import CacheResolution
from ..models import CatalogDocument, CatalogDocumentRevision, FleetProfile
from ..recipe_runtime_specs import recipe_topology
from .contracts import (
    FleetProfileInvalid,
    FleetProfileStalePlanConflict,
)
from .persistence import _stored_recipe
from .projection_support import (
    _choice_id,
    _digest,
    _effective_option_choices,
    _expanded_roles,
    _profile_document,
)

if TYPE_CHECKING:
    from .service import FleetProfileService
    from .service import FleetProfileService as _FleetProfileService


class FleetProfileService:
    @staticmethod
    def _next_profile_number(session: Session) -> int:
        """Allocate the next stable user profile number without renumbering."""

        maximum = session.scalar(select(func.max(FleetProfile.number)))
        return max(1, int(maximum or 0) + 1)

    @staticmethod
    def _recipe_identity(session: Session, selector: str) -> tuple[str, str] | Residue:
        """Resolve the exact current identities without loading artifact documents.

        A recipe that has no active revision (every revision retired or removed)
        is a :class:`Residue`: a saved profile that names it shows the choice as
        needing attention and the catalog sync replaces the recipe in place.
        """

        normalized_selector = selector.strip().casefold()
        if normalized_selector.count("/") != 1:
            raise FleetProfileInvalid(
                "recipe selector must use canonical publisher/slug form",
                reason=InvalidRequestReason.MALFORMED,
            )
        publisher, slug = normalized_selector.split("/", 1)
        candidates = tuple(
            session.scalars(
                select(CatalogDocument.id)
                .where(
                    CatalogDocument.kind == "recipe",
                    CatalogDocument.publisher == publisher,
                    CatalogDocument.slug == slug,
                )
                .order_by(
                    CatalogDocument.publisher, CatalogDocument.slug, CatalogDocument.id
                )
            )
        )
        if len(candidates) != 1:
            raise FleetProfileInvalid(
                "recipe selector is not an exact unique active recipe",
                reason=InvalidRequestReason.CONFLICT,
            )
        document = candidates[0]
        revisions = tuple(
            session.scalars(
                select(CatalogDocumentRevision)
                .where(
                    CatalogDocumentRevision.document_id == document,
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.state == ProfileDocumentState.ACTIVE.value,
                )
                .order_by(
                    CatalogDocumentRevision.revision_number.desc(),
                    CatalogDocumentRevision.created_at.desc(),
                    CatalogDocumentRevision.id.desc(),
                )
                .limit(16)
            )
        )
        if not revisions:
            return retire_as_unknown(
                "recipe",
                selector,
                BookkeepingReason.EVIDENCE_UNAVAILABLE,
                "the recipe has no active catalog revision",
            )
        # The newest revision this Controller can read; when none is readable
        # the newest one is kept so the choice can report what needs attention.
        for revision in revisions:
            try:
                read_catalog_document(revision)
            except ValueError:
                continue
            return document, revision.id
        return document, revisions[0].id

    @classmethod
    def _recipe_document(
        cls, session: Session, selector: str
    ) -> tuple[CatalogDocument, CatalogDocumentRevision] | Residue:
        cls = _typing_cast("type[_FleetProfileService]", cls)  # noqa: PLW0642 -- assembled mixin interface
        for _ in range(2):
            identity = cls._recipe_identity(session, selector)
            if isinstance(identity, Residue):
                return identity
            document_id, revision_id = identity
            document = session.get(CatalogDocument, document_id)
            revision = session.get(CatalogDocumentRevision, revision_id)
            if document is not None and revision is not None:
                return document, revision
            # The catalog changed between the two reads: resolve it again.
        return retire_as_unknown(
            "recipe",
            selector,
            BookkeepingReason.EVIDENCE_MISMATCH,
            "the recipe catalog changed during resolution",
        )

    @staticmethod
    def _recipe_selector(document: CatalogDocument) -> str:
        return f"{document.publisher}/{document.slug}"

    def _resolve_choice(
        self, session: Session, choice: FleetProfileAssignmentInput
    ) -> (
        tuple[CatalogDocument, CatalogDocumentRevision, CacheResolution | None]
        | Residue
    ):
        """Resolve one choice once for both presentation and execution.

        The cache is evidence about what is already prepared, never a gate: when
        the resolver fails, answers with an invalid contract, or answers for a
        revision other than the one selected, the choice resolves without cache
        evidence (shown as unknown) and is prepared when it is loaded.
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        resolved = self._recipe_document(session, choice.recipe_selector)
        if isinstance(resolved, Residue):
            return resolved
        document, revision = resolved
        cache: CacheResolution | None = None
        if self._cache_resolver is not None:
            try:
                candidate = self._cache_resolver(
                    recipe_identity=document.id,
                    model_variant=choice.model_variant,
                    exact_revision_id=revision.id,
                )
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                retire_as_unknown(
                    "profile-cache-resolution",
                    document.id,
                    BookkeepingReason.EVIDENCE_UNAVAILABLE,
                    f"{type(error).__name__}: {error}",
                )
                return document, revision, None
            cache = candidate
            chosen_id = candidate.recipe.recipe_revision_id
            # The resolver is asked for the current head revision, so a
            # different revision back is a resolution defect, not an older
            # cached substitute.  Its evidence is not used (the profile is never
            # bound to bytes the operator did not select) and the choice
            # resolves without cache evidence.
            if chosen_id != revision.id:
                retire_as_unknown(
                    "profile-cache-resolution",
                    document.id,
                    BookkeepingReason.EVIDENCE_MISMATCH,
                    "the cache resolver answered for another recipe revision",
                )
                return document, revision, None
        return document, revision, cache

    @staticmethod
    def _assignment_selector(choice: FleetProfileAssignmentInput) -> str:
        if choice.assignment_name is not None:
            return choice.assignment_name
        value = choice.recipe_selector.lower().replace("/", "-").replace(" ", "-")
        value = "".join(
            character if character.isalnum() or character in "-_." else "-"
            for character in value
        )
        value = value.strip("-_.") or "assignment"
        return value[:63].rstrip("-_.") or "assignment"

    @staticmethod
    def _choices(row: FleetProfile) -> tuple[FleetProfileAssignmentInput, ...]:
        """Every saved choice belongs to the exact definition, including on review."""
        from .service import FleetProfileService

        return tuple(FleetProfileService._definition(row).assignments)

    def _validate_draft_review_identity(
        self,
        session: Session,
        profile: FleetProfile,
        *,
        profile_digest: str,
        assignments: tuple[FleetProfileAssignment, ...],
    ) -> None:
        """Recheck the saved draft and exact recipe heads bound by a review."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        if _digest(_profile_document(profile)) != profile_digest:
            raise FleetProfileStalePlanConflict(
                "Fleet profile changed during application admission; review again"
            )
        assignment_by_id = {item.id: item for item in assignments}
        choices = self._choices(profile)
        if set(assignment_by_id) != {_choice_id(choice) for choice in choices}:
            raise FleetProfileStalePlanConflict(
                "Profile assignment set changed during admission; review again"
            )
        for choice in choices:
            identity = self._recipe_identity(session, choice.recipe_selector)
            assignment = assignment_by_id[_choice_id(choice)]
            if isinstance(identity, Residue) or (
                assignment.recipe_id,
                assignment.recipe_revision_id,
            ) != tuple(identity):
                raise FleetProfileStalePlanConflict(
                    "Profile recipe head changed during admission; review again"
                )

    def _execution_assignments(
        self, session: Session, row: FleetProfile
    ) -> tuple[FleetProfileAssignment, ...]:
        """Resolve logical choices into strict, load-bound assignments.

        A choice may be an incomplete distributed draft.  The generated rank
        mapping remains deterministic so preview can report the topology
        blocker; it is never submitted unless the recipe's exact topology
        accepts the complete group.
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        result: list[FleetProfileAssignment] = []
        for choice in self._choices(row):
            resolved = self._resolve_choice(session, choice)
            if isinstance(resolved, Residue):
                # The recipe resolves to no active revision: it is not part of the
                # resolved set, and the preview names it as a reason that waits for
                # the catalog sync (it never loads a profile without it silently).
                continue
            document, revision, _cache = resolved
            topology = recipe_topology(_stored_recipe(revision))
            option_choices, _notes = _effective_option_choices(
                _stored_recipe(revision),
                choice.option_choices,
            )
            roles = _expanded_roles(topology)
            nodes = [
                FleetProfileNode(
                    node_id=node_id,
                    rank=rank,
                    role=roles[rank][0] if rank < len(roles) else "unresolved",
                    endpoint_owner=(roles[rank][1] if rank < len(roles) else rank == 0),
                )
                for rank, node_id in enumerate(choice.spark_ids)
            ]
            alias = (
                self._assignment_selector(choice)
                if choice.desired_state == DesiredAssignmentState.RUNNING
                else None
            )
            result.append(
                FleetProfileAssignment(
                    id=_choice_id(choice),
                    recipe_revision_id=revision.id,
                    topology_name=topology.name,
                    desired_state=choice.desired_state,
                    alias=alias,
                    nodes=nodes,
                    option_choices=option_choices,
                    recipe_id=document.id,
                    recipe_title=document.title,
                    model_title=self._model_title(session, _stored_recipe(revision)),
                )
            )
        return tuple(result)
