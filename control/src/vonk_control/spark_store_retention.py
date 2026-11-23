"""Project current load demand and authoritative references to Spark collectors."""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import LifecycleState, StorageDemandCode, WaitReason
from vonk_agent_protocol.inventory import StoreRetentionRequest

from .agent_operation_facts import aware
from .fleet_model_removal_contract import (
    FLEET_REMOVAL_DEADLINE,
    STORE_RETENTION_MAX_AGE,
    FleetRemovalDisposition,
)
from .fleet_profile_contract.applications import FleetProfileApplicationProgress
from .lifecycle.evidence import Residue
from .logging import log_event
from .model_cache.contract import CacheOperationKind
from .model_cache.persistence import _operation_removal
from .models.cache import ArtifactLifecycleGate, ModelCacheOperation, RecipeBuild
from .models.fleet import FleetProfileApplication
from .strict_json import read_stored_model
from .unused_storage_collection.evidence import _Evidence
from .unused_storage_collection.protected_objects import protected_model_objects

# An inventory arrives every two minutes. A stopped writer cannot keep demanding
# space or authorizing deletion indefinitely.
RETENTION_MAX_AGE = STORE_RETENTION_MAX_AGE
_LOGGER = logging.getLogger(__name__)


def _store_retention_request(
    session: Session, node_id: str, now: datetime
) -> StoreRetentionRequest | None:
    evidence = _Evidence.read(session, now)
    if evidence.pointers is None or not evidence.application_references_readable:
        return None
    # Saved profiles pin the NAS cache, not disposable Spark copies. Local
    # installation plans/hard links protect installed content; this projection
    # protects live work before its local installation exists.
    objects = protected_model_objects(session, evidence.operations)
    images = set(evidence.operations)
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
    remove: set[str] = set()
    expires_at = now + RETENTION_MAX_AGE
    for operation in session.scalars(
        select(ModelCacheOperation).where(
            ModelCacheOperation.kind == CacheOperationKind.REMOVE,
            ModelCacheOperation.state.in_(
                (LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.BACKOFF)
            ),
            ModelCacheOperation.created_at > now - FLEET_REMOVAL_DEADLINE,
        )
    ):
        payload = _operation_removal(operation)
        if isinstance(payload, Residue):
            # A valid JSON shape is not proof of the accepted deletion scope.
            # The worker reports this owner's damaged intent; it authorizes no
            # remote effects while unrelated requests keep progressing.
            continue
        if (
            payload.fleet is None
            or payload.cancellation is not None
            or int(payload.fleet.deadline_at.timestamp()) <= int(now.timestamp())
        ):
            continue
        for target in payload.fleet.effects.targets:
            if target.node_id == node_id:
                expires_at = min(expires_at, payload.fleet.deadline_at)
                remove.update(
                    item.object.sha256
                    for item in target.objects
                    if item.disposition == FleetRemovalDisposition.REMOVE
                    and (
                        gate := session.get(
                            ArtifactLifecycleGate, ("model-object", item.object.sha256)
                        )
                    )
                    is not None
                    and gate.removal_owner_kind == "model-cache-operation"
                    and gate.removal_owner_id == operation.id
                    and gate.removal_fence == payload.removal_fence
                    and gate.removal_authority_expires_at is not None
                    and aware(gate.removal_authority_expires_at)
                    == payload.fleet.deadline_at
                )
    return StoreRetentionRequest(
        remove_model_objects=sorted(remove - objects),
        issued_at=int(now.timestamp()),
        expires_at=int(expires_at.timestamp()),
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
