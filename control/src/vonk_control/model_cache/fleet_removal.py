"""Request-led fleet removal using the existing NAS, uninstall and store owners."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, cast

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    ArtifactLifecycleCode,
    Disposition,
    InstallationState,
    InvalidRequestReason,
    LifecycleState,
    ModelCacheCode,
)
from vonk_agent_protocol.inventory import ModelStoreObject
from vonk_forge_contracts import ModelDefinition

from ..admission_locking import (
    AdmissionRowLock,
    ConstraintReplay,
    admitting,
    lock_admission_rows,
)
from ..agent_operation_facts import aware
from ..artifact_lifecycle import ArtifactReferenceUnverified
from ..cache_removal_review import (
    CacheAssetDisposition,
    CacheRemovalBlocker,
    CacheRemovalReview,
    CacheRemovalReviewContent,
    seal_cache_removal_review,
)
from ..catalog_queries import active_head_revision
from ..catalog_revision_contract import read_catalog_document
from ..fleet_model_removal_contract import (
    FLEET_REMOVAL_DEADLINE,
    FleetModelRemovalCheckpoint,
    FleetModelRemovalEffects,
    FleetRemovalInstallation,
    FleetRemovalObject,
    FleetRemovalObservationIssue,
    FleetRemovalTarget,
)
from ..fleet_model_removal_contract import (
    FleetRemovalDisposition as D,
)
from ..fleet_model_removal_contract import (
    FleetRemovalReason as R,
)
from ..inventory_repository import MAX_INVENTORY_FUTURE_SKEW
from ..lifecycle.evidence import (
    BookkeepingReason,
    Residue,
    read_or_rebuild,
    retire_as_unknown,
)
from ..model_cache_contract import CacheManifest, CacheOperationKind
from ..models import (
    AgentNode,
    ArtifactLifecycleGate,
    CatalogDocumentRevision,
    InstallationNode,
    Job,
    ModelCacheOperation,
    ModelCacheSet,
    NodeInventorySnapshot,
    RecipeInstallation,
)
from ..recipe_execution_contract import parse_stored_installation_plan
from ..recipe_operations.errors import RecipeRequestInvalid
from ..strict_json import read_stored_model
from ..unused_storage_collection.evidence import _Evidence
from ..unused_storage_collection.references import (
    _installation_kept,
    _installation_pointing,
)
from .errors import ModelCacheConflictInvalid
from .persistence import _operation_removal, _store_operation_payload
from .source_helpers import _model_selector, _request_key
from .views import CacheOperationView

if TYPE_CHECKING:
    from .service import ModelCacheService

INVENTORY_MAX_AGE = timedelta(minutes=5)


def _model_objects(
    session: Session,
    digest: str,
    *,
    observation_issues: list[FleetRemovalObservationIssue] | None = None,
    include_shared: bool = True,
) -> tuple[set[str], set[str]]:
    target_objects: set[str] = set()
    shared: set[str] = set()
    issues: list[FleetRemovalObservationIssue] = []
    revisions = select(CatalogDocumentRevision, active_head_revision()).where(
        CatalogDocumentRevision.kind == "model"
    )
    cache_sets = select(ModelCacheSet)
    if not include_shared:
        # A profile acquires references only to this exact model. Unrelated
        # damaged evidence matters to deletion, never to acquiring this pin.
        revisions = revisions.where(CatalogDocumentRevision.content_digest == digest)
        cache_sets = cache_sets.where(ModelCacheSet.model_content_sha256 == digest)
    for revision, current_head in session.execute(revisions):
        model = read_or_rebuild(
            kind=CatalogDocumentRevision.__tablename__,
            subject=revision.id,
            read=partial(read_catalog_document, revision),
        )
        if isinstance(model, Residue):
            # Catalog heads for other content are not live byte references.
            # Old source identities can retain heads indefinitely, even after
            # replacement by a different slug. Cache manifests, installed plans
            # and profile references independently protect their live bytes.
            # Only damage to the requested head makes its objects unknown.
            if current_head and revision.content_digest == digest:
                issues.append(_observation_issue(model, note=model.note))
            continue
        assert isinstance(model, ModelDefinition)
        objects = {file.sha256 for file in model.files}
        if revision.content_digest == digest:
            target_objects.update(objects)
        else:
            shared.update(objects)
    for row in session.scalars(cache_sets):
        manifest = read_or_rebuild(
            kind=ModelCacheSet.__tablename__,
            subject=row.artifact_set_sha256,
            read=partial(
                read_stored_model, CacheManifest, row.manifest, from_json=True
            ),
        )
        if isinstance(manifest, Residue):
            issues.append(_observation_issue(manifest))
            continue
        for artifact in manifest.artifacts:
            if artifact.model_content_sha256 == digest or (
                artifact.model_content_sha256 is None
                and row.model_content_sha256 == digest
            ):
                target_objects.add(artifact.sha256)
            else:
                shared.add(artifact.sha256)
    if observation_issues is not None:
        observation_issues.extend(issues)
    elif issues:
        # Mutating consumers require complete references; review collects the
        # same typed unknowns without granting authority from missing evidence.
        raise ArtifactReferenceUnverified(
            ArtifactLifecycleCode.REFERENCE_SCAN_FAILED,
            issues[0].detail,
            retryable=True,
        )
    return target_objects, shared


def _observation_issue(
    residue: Residue, *, note: str = ""
) -> FleetRemovalObservationIssue:
    # Only callers with safe, input-free diagnostics supply a note.
    note = f"{note} " if note else ""
    return FleetRemovalObservationIssue(
        kind=residue.kind,
        subject=residue.subject,
        reason=residue.reason,
        detail=(
            f"{residue.kind} {residue.subject}: {residue.reason}. "
            f"{note}Refresh owner evidence and review again."
        ),
    )


class FleetRemovalMixin:
    def _fleet_effects(
        self, session: Session, digest: str, *, shared_objects: set[str] | None = None
    ) -> FleetModelRemovalEffects:
        cache = cast("ModelCacheService", self)
        evidence = _Evidence.read(session, cache._clock())
        readable = (
            evidence.pointers is not None and evidence.application_references_readable
        )
        issues: list[FleetRemovalObservationIssue] = []
        target_objects, shared = _model_objects(
            session, digest, observation_issues=issues
        )
        if shared_objects is not None:
            shared_objects.update(shared)
        # Provenance is not identity. Include every cached set of this model,
        # including variants whose executable plan uses a subset of its files.
        sets = tuple(
            session.scalars(
                select(ModelCacheSet).where(
                    ModelCacheSet.model_content_sha256 == digest
                )
            )
        )
        names = target_objects | {digest} | {row.artifact_set_sha256 for row in sets}
        protected = bool(
            names & (evidence.operations | evidence.pointed_models | evidence.pinned)
        )
        keep_detail = (
            "A saved or loaded profile, live operation or pin names this model."
        )
        if not readable or issues:
            protected = True
            keep_detail = "Model content references are unavailable; review will recheck owner evidence."
        installations: dict[str, list[FleetRemovalInstallation]] = {}
        lifecycle = cache._fleet_lifecycle
        for installation in session.scalars(
            select(RecipeInstallation)
            .where(RecipeInstallation.state != InstallationState.UNINSTALLED)
            .order_by(RecipeInstallation.id)
        ):
            plan = read_or_rebuild(
                kind=RecipeInstallation.__tablename__,
                subject=installation.id,
                read=partial(
                    parse_stored_installation_plan,
                    installation.plan,
                    for_uninstall=True,
                ),
            )
            if isinstance(plan, Residue):
                issues.append(_observation_issue(plan))
                protected = True
                keep_detail = "Installation model references are unavailable; refresh owner evidence."
                continue
            models = {installation.model_content_sha256}
            models.update(
                artifact.model.content_sha256
                for compiled in plan.compiled_execution_plans.values()
                for artifact in compiled.artifacts
            )
            if digest not in models:
                continue
            detail = _installation_kept(session, installation.id, evidence)
            saved = _installation_pointing(
                session, installation.id, evidence, evidence.pointers
            )
            if saved:
                detail = "saved profiles: " + ", ".join(saved)
            uninstall = (
                None
                if lifecycle is None
                else read_or_rebuild(
                    kind=RecipeInstallation.__tablename__,
                    subject=installation.id,
                    read=partial(
                        lifecycle._uninstall_plan_in_session,
                        session,
                        installation.id,
                        lock=False,
                        preserve_model_files=True,
                    ),
                )
            )
            if isinstance(uninstall, Residue):
                issues.append(_observation_issue(uninstall))
                uninstall = None
            if uninstall is None:
                detail = "Installation removal owner unavailable."
            elif not uninstall.allowed:
                detail = "; ".join(item.detail for item in uninstall.blockers)
            if detail is not None:
                protected = True
            effect = FleetRemovalInstallation(
                installation_id=installation.id,
                plan_digest=None if uninstall is None else uninstall.plan_digest,
                disposition=D.REMOVE if detail is None else D.RETAIN,
                reason=R.UNUSED if detail is None else R.REFERENCED,
                detail="Unused installation; remove through uninstall."
                if detail is None
                else detail,
            )
            for node_id in session.scalars(
                select(InstallationNode.node_id)
                .where(InstallationNode.installation_id == installation.id)
                .order_by(InstallationNode.node_id)
            ):
                installations.setdefault(node_id, []).append(effect)
        targets: list[FleetRemovalTarget] = []
        for node_id in session.scalars(
            select(AgentNode.node_id)
            .where(AgentNode.revoked_at.is_(None))
            .order_by(AgentNode.node_id)
        ):
            snapshot = session.scalar(
                select(NodeInventorySnapshot)
                .where(NodeInventorySnapshot.node_id == node_id)
                .order_by(NodeInventorySnapshot.observed_at.desc())
                .limit(1)
            )
            available = (
                snapshot is not None
                and snapshot.model_objects is not None
                and cache._clock() - INVENTORY_MAX_AGE
                <= aware(snapshot.observed_at)
                <= cache._clock() + MAX_INVENTORY_FUTURE_SKEW
            )
            snapshot_objects = None if snapshot is None else snapshot.model_objects
            objects = (
                []
                if not available or snapshot is None
                else read_or_rebuild(
                    kind=NodeInventorySnapshot.__tablename__,
                    subject=node_id,
                    read=partial(
                        TypeAdapter(list[ModelStoreObject]).validate_json,
                        json.dumps(snapshot_objects),
                        strict=True,
                    ),
                )
            )
            if isinstance(objects, Residue):
                issues.append(_observation_issue(objects))
                available, objects = False, []
            elif not available:
                issues.append(
                    _observation_issue(
                        retire_as_unknown(
                            NodeInventorySnapshot.__tablename__,
                            node_id,
                            BookkeepingReason.EVIDENCE_UNAVAILABLE,
                            "A fresh model-object inventory is required.",
                        )
                    )
                )
            references_unknown = any(
                issue.kind != NodeInventorySnapshot.__tablename__ for issue in issues
            )
            effects = [
                FleetRemovalObject(
                    object=obj,
                    disposition=D.RETAIN
                    if protected or obj.sha256 in shared
                    else D.REMOVE,
                    reason=R.OBSERVATION_UNAVAILABLE
                    if references_unknown
                    else R.REFERENCED
                    if protected
                    else R.SHARED
                    if obj.sha256 in shared
                    else R.UNUSED,
                    detail=keep_detail
                    if protected
                    else "Another model names this content."
                    if obj.sha256 in shared
                    else "Exclusively owned model object; evict through store retention.",
                )
                for obj in objects
                if obj.sha256 in target_objects
            ]
            targets.append(
                FleetRemovalTarget(
                    node_id=node_id,
                    objects=effects,
                    installations=installations.get(node_id, []),
                    observation_available=available,
                    reclaimable_bytes=sum(
                        item.object.bytes
                        for item in effects
                        if item.disposition == D.REMOVE
                    )
                    if available
                    else None,
                )
            )
        return FleetModelRemovalEffects(
            nas_remove=not protected,
            nas_reason=R.OBSERVATION_UNAVAILABLE
            if any(
                issue.kind != NodeInventorySnapshot.__tablename__ for issue in issues
            )
            else R.REFERENCED
            if protected
            else R.UNUSED,
            nas_detail=keep_detail
            if protected
            else "Remove unshared NAS objects through model cache removal.",
            targets=targets,
            observation_issues=issues,
        )

    def review_fleet_model_removal(self, selector: str) -> CacheRemovalReview:
        cache = cast("ModelCacheService", self)
        review = cache.review_model_removal(selector)
        shared: set[str] = set()
        with cache._session() as session:
            effects = cache._fleet_effects(
                session, review.target_identity, shared_objects=shared
            )
        assets = [
            asset.model_copy(
                update={
                    "disposition": CacheAssetDisposition.RETAIN_REFERENCED
                    if not effects.nas_remove
                    else CacheAssetDisposition.RETAIN_SHARED
                    if asset.sha256 in shared
                    else asset.disposition
                }
            )
            for asset in review.assets
        ]
        candidates = [
            asset.available_bytes
            for asset in assets
            if asset.kind == "model-object"
            and asset.disposition == CacheAssetDisposition.REMOVE
        ]
        reclaimable = (
            None
            if effects.nas_reason == R.OBSERVATION_UNAVAILABLE
            or any(value is None for value in candidates)
            else sum(value for value in candidates if value is not None)
        )
        return seal_cache_removal_review(
            CacheRemovalReviewContent.model_validate(
                {
                    **review.model_dump(
                        exclude={
                            "review_digest",
                            "fleet",
                            "assets",
                            "nas_reclaimable_bytes",
                            "blockers",
                        }
                    ),
                    "fleet": effects,
                    "blockers": [
                        *review.blockers,
                        *(
                            CacheRemovalBlocker(
                                code=ArtifactLifecycleCode.REFERENCE_SCAN_FAILED,
                                detail=issue.detail,
                                retryable=True,
                            )
                            for issue in effects.observation_issues
                        ),
                    ],
                    "assets": assets,
                    "nas_reclaimable_bytes": reclaimable,
                }
            )
        )

    def remove_fleet_model_selector(
        self, selector: str, *, actor: str, request_key: str, review_digest: str
    ) -> CacheOperationView:
        cache = cast("ModelCacheService", self)
        key = _request_key(request_key)
        selector = _model_selector(selector).casefold()
        with cache._session() as session:
            existing = session.scalar(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.request_key == key
                )
            )
            if existing is not None:
                payload = _operation_removal(existing)
                if (
                    isinstance(payload, Residue)
                    or payload.fleet is None
                    or payload.review_digest != review_digest
                ):
                    raise ModelCacheConflictInvalid(
                        ModelCacheCode.REQUEST_KEY_REUSED,
                        "Request key belongs to another removal scope or review.",
                    )
                return cache._replay_model_removal(
                    existing,
                    actor=actor,
                    selector=selector,
                    fleet=True,
                    review_digest=review_digest,
                )
        review = cache.review_fleet_model_removal(selector)
        if review.review_digest != review_digest:
            raise ModelCacheConflictInvalid(
                ArtifactLifecycleCode.REFERENCE_IDENTITY_MISMATCH,
                "Fleet removal effects changed; a fresh review is available.",
            )
        effects = review.fleet
        assert effects is not None
        # Missing evidence is retained in the review, not permission to infer
        # an empty remote cache. Acceptance requires an exact complete scope.
        if effects.observation_issues or any(
            not target.observation_available for target in effects.targets
        ):
            raise ModelCacheConflictInvalid(
                ArtifactLifecycleCode.REFERENCE_SCAN_FAILED,
                "Fleet evidence is unavailable; refresh owner evidence and review again.",
            )
        selected = tuple(
            sorted(
                asset.sha256
                for asset in review.assets
                if asset.kind == "model-set" and effects.nas_remove
            )
        )
        with cache._session() as session:
            expected_scope = cache._model_removal_scope_for_sets(session, selected)
            _, shared = _model_objects(session, review.target_identity)
            reviewed_objects = {
                asset.sha256
                for asset in review.assets
                if asset.kind == "model-object" and effects.nas_remove
            }
            reviewed_deletes = {
                asset.sha256
                for asset in review.assets
                if asset.kind == "model-object"
                and asset.disposition == CacheAssetDisposition.REMOVE
            }
            if (
                set(expected_scope.selected_objects) != reviewed_objects
                or set(expected_scope.delete_objects) - shared != reviewed_deletes
            ):
                raise ModelCacheConflictInvalid(
                    ArtifactLifecycleCode.REFERENCE_IDENTITY_MISMATCH,
                    "NAS removal membership changed; a fresh review is available.",
                )
        try:
            with admitting(), cache._lock, cache._session(write=True) as session:

                def verify(session: Session, selected: tuple[str, ...]) -> None:
                    current = cache._fleet_effects(session, review.target_identity)
                    _, current_shared = _model_objects(session, review.target_identity)
                    if (
                        current != effects
                        or set(expected_scope.delete_objects) - current_shared
                        != reviewed_deletes
                    ):
                        raise ModelCacheConflictInvalid(
                            ArtifactLifecycleCode.REFERENCE_IDENTITY_MISMATCH,
                            "Fleet removal references changed; a fresh review is available.",
                        )

                operation = cache._accept_model_removal(
                    session,
                    actor=actor,
                    request_key=key,
                    selector=selector,
                    model_content_sha256=review.target_identity,
                    selected_sets=selected,
                    expected_scope=expected_scope,
                    review_digest=review_digest,
                    verify=verify,
                    defer_scope=False,
                    fleet=FleetModelRemovalCheckpoint(
                        effects=effects,
                        deadline_at=cache._clock() + FLEET_REMOVAL_DEADLINE,
                    ),
                )
                operation_id = operation.id
        except ConstraintReplay as duplicate:
            return duplicate.replay(
                lambda: cache._model_removal_by_request(
                    key,
                    actor=actor,
                    selector=selector,
                    fleet=True,
                    review_digest=review_digest,
                )
            )
        return cache.get_operation(operation_id)

    def _advance_fleet_removal(self, operation_id: str, *, now: datetime) -> bool:
        cache = cast("ModelCacheService", self)
        with cache._session() as session:
            row = session.get(ModelCacheOperation, operation_id)
            if row is None or row.state not in {
                LifecycleState.QUEUED,
                LifecycleState.RUNNING,
                LifecycleState.BACKOFF,
            }:
                return True
            payload = _operation_removal(row)
            if isinstance(payload, Residue) or payload.fleet is None:
                return True
            if row.next_action_at is not None and aware(row.next_action_at) > now:
                return False
            accepted_at, actor, key = aware(row.created_at), row.actor, row.request_key
            checkpoint = payload.fleet.model_copy(deep=True)
            owned = all(
                (gate := session.get(ArtifactLifecycleGate, ("model-object", digest)))
                is not None
                and gate.removal_owner_kind == "model-cache-operation"
                and gate.removal_owner_id == row.id
                and gate.removal_fence == payload.removal_fence
                and gate.removal_authority_expires_at is not None
                and aware(gate.removal_authority_expires_at) == checkpoint.deadline_at
                for digest in checkpoint.removal_objects
            )
        if not owned:
            cache._defer_model_removal(
                operation_id,
                detail="Fleet removal lost its exact object authority; a fresh review is required.",
                cause=Disposition.FAIL,
            )
            return False
        if now >= checkpoint.deadline_at:
            cache._defer_model_removal(
                operation_id,
                detail="Fleet removal deadline expired; unfinished effects remain visible in its receipt.",
                cause=Disposition.FAIL,
            )
            return False
        if checkpoint.effects.nas_remove:
            with cache._session() as session:
                current_effects = cache._fleet_effects(
                    session, payload.model_content_sha256 or ""
                )
            if current_effects.observation_issues:
                cache._defer_model_removal(
                    operation_id,
                    detail=current_effects.observation_issues[0].detail,
                    cause=Disposition.WAIT,
                )
                return False
            # Our own unfinished uninstall is a dependency; its job deliberately
            # protects bytes. Current profile/pin/workload protection is final.
            if not current_effects.nas_remove and not any(
                target.installations for target in current_effects.targets
            ):
                cache._defer_model_removal(
                    operation_id,
                    detail=current_effects.nas_detail,
                    cause=Disposition.FAIL,
                )
                return False
        lifecycle = cache._fleet_lifecycle
        pending: str | None = None
        seen: set[str] = set()
        # Uninstalls of the same model share content references. Observe one
        # existing child before starting its sibling, so the parent's children
        # never compete with themselves and no slot is held while waiting.
        for target in checkpoint.effects.targets:
            if pending is not None:
                break
            for effect in target.installations:
                if pending is not None:
                    break
                installation_id = effect.installation_id
                if (
                    effect.disposition != D.REMOVE
                    or installation_id in seen
                    or installation_id in checkpoint.completed_installations
                ):
                    continue
                seen.add(installation_id)
                with cache._session() as session:
                    installation = session.get(RecipeInstallation, installation_id)
                    if (
                        installation is not None
                        and installation.state == InstallationState.UNINSTALLED
                    ):
                        checkpoint.completed_installations.append(installation_id)
                        continue
                if lifecycle is None:
                    pending = "Waiting for the installation removal owner."
                    continue
                child_key = str(uuid.uuid5(uuid.UUID(key), installation_id))
                with cache._session() as session:
                    child = session.scalar(
                        select(Job).where(Job.request_id == child_key)
                    )
                    if child is not None:
                        if child.state in {
                            LifecycleState.FAILED,
                            LifecycleState.CANCELLED,
                            LifecycleState.NEEDS_OPERATOR,
                        }:
                            cache._defer_model_removal(
                                operation_id,
                                detail=f"Uninstall {child.id} ended {child.state}; inspect its receipt before a fresh request.",
                                cause=Disposition.FAIL,
                            )
                            return False
                        pending = f"Waiting for uninstall {child.id}; its existing worker owns recovery."
                        continue
                # Re-plan on conflict, once per worker pass. New references
                # shrink the permitted effects; they never expand consent.
                plan = lifecycle.preview_uninstall(
                    installation_id, preserve_model_files=True
                )
                if not plan.allowed:
                    pending = "Installation is now protected: " + "; ".join(
                        item.detail for item in plan.blockers
                    )
                    continue

                def unused(
                    session: Session, installation_id: str = installation_id
                ) -> None:
                    evidence = _Evidence.read(session, now)
                    why = _installation_kept(session, installation_id, evidence)
                    if why is not None or _installation_pointing(
                        session, installation_id, evidence, evidence.pointers
                    ):
                        raise ModelCacheConflictInvalid(
                            ModelCacheCode.REMOVAL_REFERENCED,
                            why or "A saved profile names the installation.",
                        )

                try:
                    lifecycle.uninstall(
                        installation_id,
                        plan_digest=plan.plan_digest,
                        actor=actor,
                        request_id=child_key,
                        unattended_guard=unused,
                        preserve_model_files=True,
                    )
                except RecipeRequestInvalid as error:
                    if error.typed_reason != InvalidRequestReason.SUPERSEDED:
                        raise
                    pending = f"Uninstall {installation_id} changed during admission; next attempt derives a fresh plan."
                    continue
                pending = f"Waiting for uninstall {installation_id} and exact Spark confirmation."
        if pending is None:
            with cache._session() as session:
                for target in checkpoint.effects.targets:
                    if target.node_id in checkpoint.confirmed_nodes:
                        continue
                    snapshot = session.scalar(
                        select(NodeInventorySnapshot)
                        .where(NodeInventorySnapshot.node_id == target.node_id)
                        .order_by(NodeInventorySnapshot.observed_at.desc())
                        .limit(1)
                    )
                    wanted = {
                        item.object.sha256
                        for item in target.objects
                        if item.disposition == D.REMOVE
                    }
                    if not wanted:
                        checkpoint.confirmed_nodes.append(target.node_id)
                        continue
                    if (
                        snapshot is None
                        or snapshot.model_objects is None
                        or aware(snapshot.observed_at)
                        <= accepted_at + MAX_INVENTORY_FUTURE_SKEW
                        or aware(snapshot.observed_at) < now - INVENTORY_MAX_AGE
                    ):
                        pending = f"Waiting for fresh model-store evidence from Spark {target.node_id}; next inventory resumes removal."
                        continue
                    observed = TypeAdapter(list[ModelStoreObject]).validate_json(
                        json.dumps(snapshot.model_objects), strict=True
                    )
                    if wanted & {item.sha256 for item in observed}:
                        pending = f"Waiting for Spark {target.node_id} store retention to evict the reviewed objects; next inventory confirms removal."
                    else:
                        checkpoint.confirmed_nodes.append(target.node_id)
        with admitting(), cache._session(write=True) as session:
            locked = lock_admission_rows(
                session,
                (
                    AdmissionRowLock(
                        "operation",
                        ModelCacheOperation,
                        select(ModelCacheOperation).where(
                            ModelCacheOperation.id == operation_id
                        ),
                    ),
                ),
            )
            rows = locked["operation"]
            row = None if not rows else cast(ModelCacheOperation, rows[0])
            if row is None or row.state not in {
                LifecycleState.QUEUED,
                LifecycleState.RUNNING,
                LifecycleState.BACKOFF,
            }:
                return False
            current = _operation_removal(row)
            if isinstance(current, Residue):
                return False
            _store_operation_payload(
                row,
                CacheOperationKind.REMOVE,
                current.model_copy(update={"fleet": checkpoint}),
            )
        if pending is not None:
            cache._defer_model_removal(
                operation_id, detail=pending, cause=Disposition.WAIT
            )
            return False
        return True
