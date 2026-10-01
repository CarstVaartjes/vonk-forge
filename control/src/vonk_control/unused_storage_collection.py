"""Remove the three kinds of storage nothing uses any more.

Storage that no profile points to, nothing runs, nothing used in the last day
and no operation names is dead weight, and it can always be fetched again.
Once an hour this collector finds it and removes it, in the order that lets the
next kind clear in a later sweep:

1. **Spark installations** (state ``installed``). Removal is a real uninstall
   on the Sparks, queued through the same lifecycle as ``vonkctl recipe
   uninstall``. Its model files go with it unless another installation on that
   Spark still needs them.
2. **Runtime image receipts** in the NAS ``image-cache``. Removing a receipt
   releases the image; the hourly image store collector then reclaims the
   blobs nothing else names.
3. **Model files** in the NAS model cache, removed through the model cache's
   durable, fenced removal.

Nothing is removed while it is

* pointed to by a saved profile (loaded or not): an installation by the
  profile's recipe selector resolving to its revision on its Sparks, an image or
  model by the newest revision of every recipe the catalog still offers;
* running, or installed for a workload that has not stopped;
* used in the last 24 hours, which includes a recipe superseded, a profile
  edited, or a model, image or installation touched in that time;
* named by a live or recently finished operation (searched in the stored JSON,
  as the catalog revision collector does).

``installation_policy`` is not consulted. ``keep-cached`` only stops a profile
*load* from removing installations out of its scope; it never promised to keep
what nothing points to. This sweep removes that after the grace period.

Every removal is re-proven while the Sparks (or artifact gates) are locked, so
a load that starts after the sweep looked is never raced: the uninstall takes
no new workload intent (it supersedes nothing and leaves recovery of a running
workload alone) and is refused when an operation targets its Sparks. One item
that cannot be removed is kept for the next sweep and never stops the rest.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import TypeAdapter
from sqlalchemy import or_, select, union
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import canonical_message

from .artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactLifecycleError,
    lock_reference_gates,
)
from .artifact_reference_scan import (
    model_set_reference_findings,
    runtime_image_reference_findings,
)
from .attempt_residues import _OWNER_JOB_KINDS
from .catalog_revision_collection import (
    GRACE,
    INTERVAL,
    live_tokens,
    operation_tokens,
    pinned_by_heads,
)
from .fleet_profile_contract import FleetProfileAssignmentInput
from .logging import log_event
from .models import (
    ArtifactDistributionAssignment,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    CatalogRecipeModelReference,
    FleetProfile,
    FleetProfileApplication,
    InstallationNode,
    Job,
    ModelCacheSet,
    ModelCacheSetArtifact,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    RuntimeImageAuthorization,
)
from .oci_image_store import IMAGE_CACHE_DIRECTORY
from .recipe_action_plans import UninstallPlan
from .runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    RuntimeImagePreparationError,
)

_LOGGER = logging.getLogger(__name__)
ACTOR = "system:storage-sweep"
# One sweep stops here and the rest continues on the next worker pass.
SWEEP_BUDGET_SECONDS = 30.0
# A failed run holds nothing; any other state but stopped may still be on a Spark.
_DEAD_RUNS = ("stopped", "failed")
_RECEIPT_SUFFIX = ".receipt.json"
_ASSIGNMENTS = TypeAdapter(list[FleetProfileAssignmentInput])


class _Kept(Exception):
    """Something still uses it (or proof is missing); try again next sweep."""


class UnusedModelRemoval(Protocol):
    """The model cache's unattended, fenced removal seam."""

    def accept_unused_removal(
        self,
        model_content_sha256: str,
        *,
        actor: str,
        request_key: str,
        verify: Callable[[Session, tuple[str, ...]], None],
    ) -> object: ...


class InstallationRemoval(Protocol):
    """The recipe lifecycle's uninstall, as the sweep uses it."""

    def preview_uninstall(self, installation_id: str) -> UninstallPlan: ...

    def uninstall(
        self,
        installation_id: str,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        unattended_guard: Callable[[Session], None] | None = None,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class Swept:
    installations: int
    images: int
    models: int
    kept: dict[str, int]


@dataclass(frozen=True, slots=True)
class _Evidence:
    """Everything that can keep something, read in one snapshot."""

    now: datetime
    cutoff: datetime
    live: frozenset[str]
    operations: frozenset[str]
    pinned: frozenset[str]
    head_digests: frozenset[str]
    head_revisions: frozenset[str]
    bound_models: frozenset[str]
    # Newest active recipe revision of each document: id and creation time.
    newest: Mapping[str, tuple[str, datetime, int]]
    # (publisher, slug) -> Spark sets of the saved profile assignments naming it.
    # None when a profile cannot be read: nothing can then be proven unused.
    pointers: Mapping[tuple[str, str], tuple[frozenset[str], ...]] | None
    profile_edited: datetime | None
    owned_nodes: frozenset[str]
    active_scopes: tuple[str, ...]
    recent_sets: frozenset[str]
    recent_archives: frozenset[str]
    head_archives: frozenset[str]

    @classmethod
    def read(cls, session: Session, now: datetime) -> _Evidence:
        cutoff = now - GRACE
        revisions = session.execute(
            select(
                CatalogDocumentRevision.id,
                CatalogDocumentRevision.document_id,
                CatalogDocumentRevision.revision_number,
                CatalogDocumentRevision.created_at,
            ).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
        newest: dict[str, tuple[str, datetime, int]] = {}
        for revision_id, document_id, number, created in revisions:
            known = newest.get(document_id)
            if known is None or number > known[2]:
                newest[document_id] = (revision_id, _utc(created), number)
        head_ids = frozenset(
            value
            for value in session.scalars(
                union(
                    select(CatalogDocumentHead.active_revision_id).where(
                        CatalogDocumentHead.kind == "recipe"
                    ),
                    select(CatalogDocumentHead.candidate_revision_id).where(
                        CatalogDocumentHead.kind == "recipe"
                    ),
                )
            )
            if value is not None
        ) | frozenset(revision_id for revision_id, _created, _number in newest.values())
        head_digests = frozenset(
            session.scalars(
                select(CatalogDocumentRevision.content_digest).where(
                    CatalogDocumentRevision.id.in_(head_ids)
                )
            )
        )
        bound = frozenset(
            session.scalars(
                select(CatalogRecipeModelReference.model_content_digest).where(
                    CatalogRecipeModelReference.recipe_revision_id.in_(head_ids)
                )
            )
        )
        edited = session.scalar(
            select(FleetProfile.updated_at)
            .order_by(FleetProfile.updated_at.desc())
            .limit(1)
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
            canonical_message(plan).decode()
            for plan in session.scalars(
                select(FleetProfileApplication.plan).where(
                    FleetProfileApplication.state.in_(("queued", "running"))
                )
            )
        )
        return cls(
            now=now,
            cutoff=cutoff,
            live=live_tokens(session, now),
            operations=operation_tokens(session, now),
            pinned=pinned_by_heads(session),
            head_digests=head_digests,
            head_revisions=head_ids,
            bound_models=bound,
            newest=newest,
            pointers=_profile_pointers(session),
            profile_edited=_utc(edited) if edited is not None else None,
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
                session.scalars(
                    select(RuntimeImageAuthorization.oci_archive_sha256).where(
                        RuntimeImageAuthorization.authorized_at > cutoff
                    )
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
            head_archives=frozenset(
                session.scalars(
                    select(RuntimeImageAuthorization.oci_archive_sha256).where(
                        RuntimeImageAuthorization.recipe_revision_id.in_(head_ids)
                    )
                )
            )
            | frozenset(
                value
                for value in session.scalars(
                    select(RecipeBuild.oci_layout_sha256).where(
                        RecipeBuild.recipe_revision_id.in_(head_ids)
                    )
                )
                if value is not None
            ),
        )


class UnusedStorageCollector:
    """Hourly sweep of unused installations, image receipts and model files."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
        lifecycle: InstallationRemoval,
        image_cache_root: Path | None = None,
        model_cache: UnusedModelRemoval | None = None,
        budget_seconds: float = SWEEP_BUDGET_SECONDS,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._lifecycle = lifecycle
        self._images = (
            FilesystemRuntimeImageStorage(image_cache_root)
            if image_cache_root is not None
            else None
        )
        self._image_cache = (
            image_cache_root / IMAGE_CACHE_DIRECTORY
            if image_cache_root is not None
            else None
        )
        self._model_cache = model_cache
        self._budget_seconds = budget_seconds
        self._due_at: datetime | None = None

    def tick(self) -> bool:
        """Sweep at most once per interval; True when anything was removed."""

        now = self._clock()
        if self._due_at is not None and now < self._due_at:
            return False
        self._due_at = now + INTERVAL
        try:
            result = self.collect()
        except SQLAlchemyError as error:
            # A database fault proves nothing unused; try again next interval.
            log_event(
                _LOGGER,
                "unused_storage.sweep_failed",
                service="control-worker",
                code=type(error).__name__,
            )
            return False
        return bool(result.installations or result.images or result.models)

    def collect(self) -> Swept:
        now = self._clock()
        deadline = time.monotonic() + self._budget_seconds
        with self._sessions() as session:
            evidence = _Evidence.read(session, now)
        kept: Counter[str] = Counter()
        installations = self._installations(evidence, deadline, kept)
        images = self._image_receipts(evidence, deadline, kept)
        models = self._models(evidence, deadline, kept)
        if any("sweep budget" in reason for reason in kept):
            self._due_at = now  # continue on the next worker pass
        if installations or images or models or kept:
            log_event(
                _LOGGER,
                "unused_storage.swept",
                service="control-worker",
                installations_removed=installations,
                image_receipts_removed=images,
                models_removed=models,
                kept=dict(kept),
            )
        return Swept(installations, images, models, dict(kept))

    # -- installations ------------------------------------------------------

    def _installations(
        self, evidence: _Evidence, deadline: float, kept: Counter[str]
    ) -> int:
        with self._sessions() as session:
            candidates = list(
                session.scalars(
                    select(RecipeInstallation.id)
                    .where(RecipeInstallation.state == "installed")
                    .order_by(RecipeInstallation.updated_at, RecipeInstallation.id)
                )
            )
            reasons = {
                installation_id: _installation_kept(session, installation_id, evidence)
                for installation_id in candidates
            }
        removed = 0
        for installation_id in candidates:
            reason = reasons[installation_id]
            if reason is not None:
                kept[f"installation: {reason}"] += 1
                continue
            if time.monotonic() > deadline:
                kept["installation: sweep budget"] += 1
                continue
            try:
                self._uninstall(installation_id)
            except _Kept as why:
                kept[f"installation: {why}"] += 1
                continue
            except (
                KeyError,
                RuntimeError,
                TypeError,
                ValueError,
                SQLAlchemyError,
            ) as error:
                kept[f"installation: refused {_code(error)}"] += 1
                log_event(
                    _LOGGER,
                    "unused_storage.refused",
                    service="control-worker",
                    kind="installation",
                    record_id=installation_id,
                    code=_code(error),
                    detail=str(error)[:256],
                )
                continue
            removed += 1
            log_event(
                _LOGGER,
                "unused_storage.removing",
                service="control-worker",
                kind="installation",
                record_id=installation_id,
            )
        return removed

    def _uninstall(self, installation_id: str) -> None:
        plan = self._lifecycle.preview_uninstall(installation_id)
        if not plan.allowed:
            code = plan.blockers[0].code if plan.blockers else "not allowed"
            raise _Kept(f"uninstall blocked ({code})")

        def still_unused(session: Session) -> None:
            reason = _installation_kept(
                session,
                installation_id,
                _Evidence.read(session, self._clock()),
            )
            if reason is not None:
                raise _Kept(reason)

        self._lifecycle.uninstall(
            installation_id,
            plan_digest=plan.plan_digest,
            actor=ACTOR,
            request_id=str(uuid.uuid4()),
            unattended_guard=still_unused,
        )

    # -- runtime image receipts ---------------------------------------------

    def _image_receipts(
        self, evidence: _Evidence, deadline: float, kept: Counter[str]
    ) -> int:
        if self._images is None or self._image_cache is None:
            return 0
        removed = 0
        for path in sorted(self._image_cache.glob(f"*{_RECEIPT_SUFFIX}")):
            archive = path.name.removesuffix(_RECEIPT_SUFFIX)
            if len(archive) != 64 or any(c not in "0123456789abcdef" for c in archive):
                continue
            reason = _image_kept(archive, evidence, _mtime(path))
            if reason is not None:
                kept[f"image receipt: {reason}"] += 1
                continue
            if time.monotonic() > deadline:
                kept["image receipt: sweep budget"] += 1
                continue
            try:
                self._remove_receipt(archive, path)
            except _Kept as why:
                kept[f"image receipt: {why}"] += 1
                continue
            except (
                OSError,
                RuntimeError,
                ValueError,
                SQLAlchemyError,
                ArtifactLifecycleError,
                RuntimeImagePreparationError,
            ) as error:
                kept[f"image receipt: refused {_code(error)}"] += 1
                log_event(
                    _LOGGER,
                    "unused_storage.refused",
                    service="control-worker",
                    kind="image-receipt",
                    record_id=archive,
                    code=_code(error),
                    detail=str(error)[:256],
                )
                continue
            removed += 1
            log_event(
                _LOGGER,
                "unused_storage.removed",
                service="control-worker",
                kind="image-receipt",
                record_id=archive,
            )
        return removed

    def _remove_receipt(self, archive: str, path: Path) -> None:
        """Remove one receipt under its publication lock and reference gate.

        The artifact lock is taken first and never waited for; inside it, one
        short transaction holds the image's reference gate (a consumer that
        wants the image meanwhile gets a busy answer and retries), proves the
        image unused again and unlinks the receipt. A load that wants the
        image afterwards prepares it again, as for any cache miss.
        """

        assert self._images is not None
        now = self._clock()
        with (
            self._images.publication_lock(archive),
            self._sessions.begin() as session,
        ):
            rows = lock_reference_gates(
                session, (ArtifactIdentity("runtime-image", archive),), now=now
            )
            if any(row.removal_owner_id is not None for row in rows):
                raise _Kept("another removal owns it")
            evidence = _Evidence.read(session, now)
            reason = _image_kept(archive, evidence, _mtime(path))
            if reason is None and runtime_image_reference_findings(
                session, (archive,)
            ).get(archive):
                reason = "referenced"
            if reason is not None:
                raise _Kept(reason)
            self._images.remove_published(archive)

    # -- model files --------------------------------------------------------

    def _models(self, evidence: _Evidence, deadline: float, kept: Counter[str]) -> int:
        if self._model_cache is None:
            return 0
        with self._sessions() as session:
            rows = session.execute(
                select(
                    ModelCacheSet.artifact_set_sha256,
                    ModelCacheSet.model_content_sha256,
                    ModelCacheSet.recipe_revision_sha256,
                    ModelCacheSet.last_accessed_at,
                    ModelCacheSet.updated_at,
                )
            ).all()
            by_model: dict[str, list[str]] = {}
            unidentified = 0
            for set_digest, model_digest, *_rest in rows:
                if model_digest is None:
                    unidentified += 1
                else:
                    by_model.setdefault(model_digest, []).append(set_digest)
            reasons = {
                digest: _model_kept(session, digest, evidence)
                for digest in sorted(by_model)
            }
        if unidentified:
            kept["model: no model identity"] += unidentified
        removed = 0
        for digest in sorted(by_model):
            reason = reasons[digest]
            if reason is not None:
                kept[f"model: {reason}"] += 1
                continue
            if time.monotonic() > deadline:
                kept["model: sweep budget"] += 1
                continue
            try:
                self._remove_model(digest)
            except _Kept as why:
                kept[f"model: {why}"] += 1
                continue
            except (
                RuntimeError,
                ValueError,
                SQLAlchemyError,
                ArtifactLifecycleError,
            ) as error:
                kept[f"model: refused {_code(error)}"] += 1
                log_event(
                    _LOGGER,
                    "unused_storage.refused",
                    service="control-worker",
                    kind="model",
                    record_id=digest,
                    code=_code(error),
                    detail=str(error)[:256],
                )
                continue
            removed += 1
            log_event(
                _LOGGER,
                "unused_storage.removing",
                service="control-worker",
                kind="model",
                record_id=digest,
            )
        return removed

    def _remove_model(self, digest: str) -> None:
        assert self._model_cache is not None

        def verify(session: Session, sets: tuple[str, ...]) -> None:
            reason = _model_kept(
                session, digest, _Evidence.read(session, self._clock())
            )
            if reason is None and any(
                model_set_reference_findings(session, sets).values()
            ):
                reason = "referenced"
            if reason is not None:
                raise _Kept(reason)

        self._model_cache.accept_unused_removal(
            digest, actor=ACTOR, request_key=str(uuid.uuid4()), verify=verify
        )


# -- the rules, one function per kind -----------------------------------------


def _installation_kept(
    session: Session, installation_id: str, evidence: _Evidence
) -> str | None:
    """Why an installation stays, or ``None`` when it is unused."""

    installation = session.get(RecipeInstallation, installation_id)
    if installation is None or installation.state != "installed":
        return "not installed"
    revision = session.execute(
        select(
            CatalogDocumentRevision.document_id,
            CatalogDocumentRevision.publisher,
            CatalogDocumentRevision.slug,
        ).where(CatalogDocumentRevision.id == installation.recipe_revision_id)
    ).one_or_none()
    newest = evidence.newest.get(revision.document_id) if revision else None
    if revision is None or newest is None:
        return "recipe unavailable"
    nodes = {
        node_id: _utc(updated)
        for node_id, updated in session.execute(
            select(InstallationNode.node_id, InstallationNode.updated_at).where(
                InstallationNode.installation_id == installation_id
            )
        )
    }
    if evidence.pointers is None:
        return "profiles unreadable"
    if newest[0] == installation.recipe_revision_id and any(
        nodes.keys() & sparks
        for sparks in evidence.pointers.get(
            (revision.publisher.casefold(), revision.slug.casefold()), ()
        )
    ):
        return "profile"
    runs = session.execute(
        select(RecipeRun.id, RecipeRun.state, RecipeRun.updated_at).where(
            RecipeRun.installation_id == installation_id
        )
    ).all()
    if any(state not in _DEAD_RUNS for _id, state, _at in runs):
        return "running"
    if any(state == "failed" for _id, state, _at in runs):
        # Uninstall refuses a run that was never stopped; nothing here stops one.
        return "run not stopped"
    cutoff = evidence.cutoff
    stamps = [_utc(installation.updated_at), *nodes.values()]
    stamps.extend(_utc(updated) for _id, _state, updated in runs)
    if newest[0] != installation.recipe_revision_id:
        # Superseded: no profile resolves to it, and its grace starts there.
        stamps.append(newest[1])
    elif evidence.profile_edited is not None:
        # An edit may have just dropped the assignment that used it.
        stamps.append(evidence.profile_edited)
    if any(stamp > cutoff for stamp in stamps):
        return "recent use"
    if (
        installation_id in evidence.operations
        or any(run_id in evidence.operations for run_id, _state, _at in runs)
        or not evidence.owned_nodes.isdisjoint(nodes)
        or any(
            node_id in scope for scope in evidence.active_scopes for node_id in nodes
        )
    ):
        return "live operation"
    return None


def _model_kept(session: Session, digest: str, evidence: _Evidence) -> str | None:
    """Why a model's cached files stay, or ``None`` when no recipe needs them."""

    sets = session.execute(
        select(
            ModelCacheSet.artifact_set_sha256,
            ModelCacheSet.recipe_revision_sha256,
            ModelCacheSet.last_accessed_at,
            ModelCacheSet.updated_at,
        ).where(ModelCacheSet.model_content_sha256 == digest)
    ).all()
    if (
        digest in evidence.pinned
        or digest in evidence.bound_models
        or any(recipe in evidence.head_digests for _s, recipe, _a, _u in sets)
    ):
        return "current recipe"
    names = {digest} | {set_digest for set_digest, _r, _a, _u in sets}
    names.update(
        session.scalars(
            select(ModelCacheSetArtifact.artifact_sha256).where(
                ModelCacheSetArtifact.artifact_set_sha256.in_(names)
            )
        )
    )
    if any(
        _utc(accessed) > evidence.cutoff or _utc(updated) > evidence.cutoff
        for _s, _r, accessed, updated in sets
    ) or any(set_digest in evidence.recent_sets for set_digest, *_ in sets):
        return "recent use"
    if not evidence.live.isdisjoint(names):
        return "live operation"
    return None


def _image_kept(
    archive: str, evidence: _Evidence, modified: datetime | None
) -> str | None:
    """Why an image receipt stays, or ``None`` when no recipe needs the image."""

    if archive in evidence.head_archives or archive in evidence.pinned:
        return "current recipe"
    if (
        modified is None
        or modified > evidence.cutoff
        or archive in evidence.recent_archives
    ):
        return "recent use"
    if archive in evidence.live:
        return "live operation"
    return None


# -- helpers --------------------------------------------------------------------


def _profile_pointers(
    session: Session,
) -> dict[tuple[str, str], tuple[frozenset[str], ...]] | None:
    """The Spark sets each saved profile assigns to each recipe, or ``None``.

    A profile that cannot be read as the current contract leaves nothing
    provably unused, so the caller keeps everything an assignment might name.
    """

    found: dict[tuple[str, str], list[frozenset[str]]] = {}
    try:
        for assignments in session.scalars(select(FleetProfile.assignments)):
            for assignment in _ASSIGNMENTS.validate_json(
                canonical_message(assignments), strict=True
            ):
                publisher, separator, slug = assignment.recipe_selector.partition("/")
                if not separator:
                    return None
                found.setdefault((publisher.casefold(), slug.casefold()), []).append(
                    frozenset(assignment.spark_ids)
                )
    except (TypeError, ValueError):
        return None
    return {key: tuple(value) for key, value in found.items()}


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    except OSError:
        return None


def _code(error: Exception) -> str:
    return str(getattr(error, "code", type(error).__name__))


def _utc(value: datetime) -> datetime:
    # SQLite returns naive timestamps for timezone-aware columns.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


__all__ = ["ACTOR", "GRACE", "INTERVAL", "Swept", "UnusedStorageCollector"]
