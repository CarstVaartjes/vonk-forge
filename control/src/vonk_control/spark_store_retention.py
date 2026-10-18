"""Project current load demand and authoritative references to Spark collectors."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from vonk_agent_protocol import LifecycleState, StorageDemandCode, WaitReason
from vonk_agent_protocol.inventory import StoreRetentionRequest
from vonk_forge_contracts import document_sha256, read_model

from .fleet_profile_contract import FleetProfileApplicationProgress
from .logging import log_event
from .model_cache_contract import CacheManifest
from .models import (
    CatalogDocumentRevision,
    FleetProfileApplication,
    ModelCacheSet,
    RecipeBuild,
)
from .strict_json import read_stored_model
from .unused_storage_collection.evidence import _Evidence

# An inventory arrives every two minutes. A stopped writer cannot keep demanding
# space or authorizing deletion indefinitely.
RETENTION_MAX_AGE = timedelta(minutes=10)
_LOGGER = logging.getLogger(__name__)


def _store_retention_request(
    session: Session, node_id: str, now: datetime
) -> StoreRetentionRequest | None:
    evidence = _Evidence.read(session, now)
    if evidence.pointers is None or not evidence.application_references_readable:
        return None
    references = evidence.operations | evidence.pinned | evidence.pointed_models
    objects = set(references)
    for document in session.scalars(
        select(ModelCacheSet.manifest).where(
            or_(
                ModelCacheSet.artifact_set_sha256.in_(references),
                ModelCacheSet.model_content_sha256.in_(references),
            )
        )
    ):
        manifest = read_stored_model(CacheManifest, document, from_json=True)
        objects.update(artifact.sha256 for artifact in manifest.artifacts)
    # A reusable cache set keeps its first requester's provenance. A pinned
    # editorial successor can name the same files under a different model ID;
    # the accepted model definitions own those file identities, not that index.
    pending = set(references)
    visited: set[str] = set()
    required_dependencies: set[str] = set()
    # This walk ends with the finite set of persisted definitions; cycles
    # cannot revisit a content identity. It never waits or retries a missing row.
    while pending:
        batch, pending = pending - visited, set()
        unresolved = required_dependencies & batch
        for revision in session.scalars(
            select(CatalogDocumentRevision)
            .where(
                CatalogDocumentRevision.kind == "model",
                CatalogDocumentRevision.content_digest.in_(batch),
            )
            .execution_options(yield_per=1)
        ):
            digest = revision.content_digest
            if digest is None:
                raise ValueError("model reference has no content identity")
            unresolved.discard(digest)
            visited.add(digest)
            definition = read_model(revision.document)
            if document_sha256(revision.document) != digest:
                raise ValueError("model reference document identity does not match")
            objects.update(file.sha256 for file in definition.files)
            dependencies = {item.content_sha256 for item in definition.dependencies}
            required_dependencies.update(dependencies)
            pending.update(dependencies - visited)
        if unresolved:
            raise ValueError("model dependency definition is unavailable")
    images = set(evidence.operations | evidence.pointed_archives)
    # Docker receipts name manifest digests; NAS retention names layout digests.
    # Resolve that content identity, including builds reused by recipe successors.
    for digest in session.scalars(
        select(RecipeBuild.image_digest).where(
            RecipeBuild.oci_layout_sha256.in_(images),
            RecipeBuild.image_digest.is_not(None),
        )
    ):
        if digest is not None:
            images.add(digest.removeprefix("sha256:"))
    required = 0
    for application in session.scalars(
        select(FleetProfileApplication).where(
            FleetProfileApplication.state.in_(
                (LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.BACKOFF)
            ),
            FleetProfileApplication.updated_at >= now - RETENTION_MAX_AGE,
        )
    ):
        progress = read_stored_model(
            FleetProfileApplicationProgress, application.progress, from_json=True
        )
        if progress.cancellation is not None:
            continue
        for blocker in progress.blockers:
            if (
                node_id in blocker.node_ids
                and blocker.code in set(StorageDemandCode)
                and blocker.storage is not None
            ):
                required = max(required, blocker.storage.required_free_bytes)
    return StoreRetentionRequest(
        issued_at=int(now.timestamp()),
        expires_at=int((now + RETENTION_MAX_AGE).timestamp()),
        required_free_bytes=required,
        protected_model_objects=sorted(value for value in objects if _digest(value)),
        protected_image_addresses=sorted(value for value in images if _digest(value)),
    )


def _digest(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def store_retention_request(
    session: Session, node_id: str, now: datetime
) -> StoreRetentionRequest | None:
    try:
        request = _store_retention_request(session, node_id, now)
    except ValueError:
        request = None
    if request is None:
        # An unreadable reference is unknown, not permission to collect; the
        # mandatory capacity inventory is still accepted.
        log_event(
            _LOGGER,
            "unused_storage.references_unavailable",
            service="control-api",
            node_id=node_id,
            code=WaitReason.OBSERVATION_UNAVAILABLE,
            detail="Controller reference scan unavailable; cache collection retries at the next inventory",
        )
    return request
