"""Storage sweep evidence and evidence."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from vonk_agent_protocol import canonical_message

from ..artifact_reference_scan import _selector_revisions
from ..attempt_residues import _OWNER_JOB_KINDS
from ..catalog_revision_collection import GRACE, live_tokens, operation_tokens, tokens
from ..models import (
    ArtifactDistributionAssignment,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    CatalogRecipeModelReference,
    FleetProfileApplication,
    FleetProfileSelection,
    Job,
    RecipeBuild,
)
from ..revision_images import revision_archives
from .common import _waits_for_storage
from .references import (
    _UNFINISHED_LOADS,
    _applied_revision_ids,
    _profile_pointers,
    _utc,
)


@dataclass(frozen=True, slots=True)
class _Evidence:
    """Everything that can keep something, read in one snapshot."""

    now: datetime
    cutoff: datetime
    live: frozenset[str]
    #: Named by an operation that has not ended; one that just finished only
    #: makes its installation recently used (removed last), never in use.
    operations: frozenset[str]
    #: Digests the newest revisions of the recipes a saved profile names mention.
    pinned: frozenset[str]
    pointed_digests: frozenset[str]
    pointed_models: frozenset[str]
    pointed_archives: frozenset[str]
    # Newest active recipe revision of each document: id, creation time, number
    # and content digest (what an installation is compared on).
    newest: Mapping[str, tuple[str, datetime, int, str]]
    # (publisher, slug) -> Spark sets of the saved profile assignments naming it.
    # None when a profile cannot be read: nothing can then be proven unused.
    pointers: Mapping[tuple[str, str], tuple[tuple[str, frozenset[str]], ...]] | None
    # The same for the loaded (selected) profile alone: its installations never go.
    loaded_pointers: (
        Mapping[tuple[str, str], tuple[tuple[str, frozenset[str]], ...]] | None
    )
    owned_nodes: frozenset[str]
    active_scopes: tuple[str, ...]
    recent_sets: frozenset[str]
    recent_archives: frozenset[str]
    application_references_readable: bool

    @classmethod
    def read(cls, session: Session, now: datetime) -> _Evidence:
        cutoff = now - GRACE
        revisions = session.execute(
            select(
                CatalogDocumentRevision.id,
                CatalogDocumentRevision.document_id,
                CatalogDocumentRevision.revision_number,
                CatalogDocumentRevision.created_at,
                CatalogDocumentRevision.content_digest,
            ).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
        newest: dict[str, tuple[str, datetime, int, str]] = {}
        for revision_id, document_id, number, created, digest in revisions:
            known = newest.get(document_id)
            if known is None or number > known[2]:
                newest[document_id] = (revision_id, _utc(created), number, digest)
        pointers = _profile_pointers(session)
        selected = session.scalar(select(FleetProfileSelection.profile_id))
        loaded_pointers = (
            {}
            if selected is None
            else _profile_pointers(session, only_profile_id=selected)
        )
        head_ids = {item[0] for item in newest.values()}
        if pointers is not None:
            for publisher, slug in pointers:
                head_ids.update(
                    revision.id
                    for revision in _selector_revisions(session, f"{publisher}/{slug}")
                )
        # A load that is queued, admission-waiting or running needs the exact
        # revisions its plan resolved, whatever the saved profiles say now (a
        # sweep rewrites its profile between steps).  Unlike the Spark scopes
        # below, a load waiting for storage still needs its assets.
        applied_ids = _applied_revision_ids(session)
        application_references_readable = applied_ids is not None
        applied_ids = applied_ids or frozenset()
        head_ids.update(applied_ids)
        for column in (
            CatalogDocumentHead.active_revision_id,
            CatalogDocumentHead.candidate_revision_id,
        ):
            head_ids.update(
                value
                for value in session.scalars(
                    select(column).where(CatalogDocumentHead.kind == "recipe")
                )
                if value is not None
            )
        # What a profile points to is the newest revision of the recipes it
        # names. A recipe nobody named is offered by the catalog but is no
        # more than a download away, so it can go when space is short.
        pointed = [
            row
            for row in session.execute(
                select(
                    CatalogDocumentRevision.id,
                    CatalogDocumentRevision.publisher,
                    CatalogDocumentRevision.slug,
                    CatalogDocumentRevision.content_digest,
                    CatalogDocumentRevision.document,
                ).where(CatalogDocumentRevision.id.in_(head_ids))
            )
            if pointers is None
            or row.id in applied_ids
            or (row.publisher.casefold(), row.slug.casefold()) in pointers
        ]
        pointed_ids = frozenset(row.id for row in pointed)
        pinned = tokens(row.document for row in pointed)
        bound = frozenset(
            session.scalars(
                select(CatalogRecipeModelReference.model_content_digest).where(
                    CatalogRecipeModelReference.recipe_revision_id.in_(pointed_ids)
                )
            )
        )
        owned = frozenset(
            node_id
            for targets in session.scalars(
                select(Job.targets).where(
                    Job.kind.in_(_OWNER_JOB_KINDS),
                    Job.state.in_(("queued", "running")),
                )
            )
            if isinstance(targets, list)
            for node_id in targets
            if isinstance(node_id, str)
        )
        scopes = tuple(
            canonical_message(application.plan).decode()
            for application in session.scalars(
                select(FleetProfileApplication).where(
                    FleetProfileApplication.state.in_(_UNFINISHED_LOADS)
                )
            )
            if not _waits_for_storage(application)
        )
        return cls(
            now=now,
            application_references_readable=application_references_readable,
            cutoff=cutoff,
            live=live_tokens(session, now),
            operations=operation_tokens(session, now, recent=False),
            pinned=pinned,
            pointed_digests=frozenset(row.content_digest for row in pointed),
            pointed_models=bound | pinned,
            # What a pointed recipe can run is every image its document built,
            # including a predecessor's build that a successor reuses.
            pointed_archives=revision_archives(session, pointed_ids)
            | frozenset(
                value
                for value in session.scalars(
                    select(RecipeBuild.oci_layout_sha256).where(
                        RecipeBuild.recipe_revision_id.in_(pointed_ids)
                    )
                )
                if value is not None
            )
            | pinned,
            newest=newest,
            pointers=pointers,
            loaded_pointers=loaded_pointers,
            owned_nodes=owned,
            active_scopes=scopes,
            recent_sets=frozenset(
                session.scalars(
                    select(ArtifactDistributionAssignment.model_artifact_set_sha256)
                    .where(ArtifactDistributionAssignment.updated_at > cutoff)
                    .distinct()
                )
            ),
            recent_archives=frozenset(
                session.scalars(
                    select(ArtifactDistributionAssignment.oci_archive_sha256)
                    .where(ArtifactDistributionAssignment.updated_at > cutoff)
                    .distinct()
                )
            )
            | frozenset(
                value
                for value in session.scalars(
                    select(RecipeBuild.oci_layout_sha256).where(
                        or_(
                            RecipeBuild.updated_at > cutoff,
                            RecipeBuild.state.in_(("planned", "building")),
                        )
                    )
                )
                if value is not None
            ),
        )


@dataclass(frozen=True, slots=True)
class _Pressure:
    """One Spark or filesystem that is short of free space."""

    scope: str
    node_id: str | None
    free: int
    total: int
    observed_at: datetime | None
    #: Bytes missing for the refused work (or the low-space line) to be met.
    shortfall: int
    #: The shortfall plus a reserve, so the next request does not refuse again.
    need: int
    why: str
    #: Work was refused for this space, so removing too little would not help it.
    demanded: bool


@dataclass(frozen=True, slots=True)
class _Item:
    """Something unused that can be removed, and what that is expected to free."""

    kind: str  # "installation", "model" or "image"
    key: str
    #: Installations sharing one model on a Spark are removed together.
    members: tuple[str, ...]
    last_used: datetime
    recent: bool
    freeable: int
    #: Saved profiles pointing at it: evicted after everything no profile points to.
    profiles: tuple[str, ...] = ()
    #: The shared model objects (file digest, bytes) its installations link, and
    #: the bytes only they hold (runtime files, private copies). ``freeable`` is
    #: what removing it frees *after the items ahead of it in eviction order*: a
    #: shared object frees only with the last installation that links it.
    objects: tuple[tuple[str, int], ...] = ()
    private: int = 0


@dataclass(slots=True)
class _Outcome:
    scope: str
    why: str
    shortfall: int
    outcome: str
    estimated_freed: int = 0
    removed: Counter[str] = field(default_factory=Counter)
    freeable: int = 0

    def line(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "why": self.why,
            "needed_bytes": self.shortfall,
            "freeable_bytes": self.freeable,
            "estimated_freed_bytes": self.estimated_freed,
            "outcome": self.outcome,
            "installations_removed": self.removed["installation"],
            "image_receipts_removed": self.removed["image"],
            "models_removed": self.removed["model"],
        }


@dataclass(frozen=True, slots=True)
class _Round:
    """What a pass promised to free, to compare with what the next one measures."""

    estimated: int
    free_before: int
