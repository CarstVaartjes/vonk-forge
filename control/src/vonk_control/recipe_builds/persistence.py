"""Recipe builds: persistence concerns."""

from __future__ import annotations

import copy
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    LifecycleState,
    RecipeBuildCode,
    ReservationState,
    SecurityRefusalReason,
    WaitReason,
)

from ..admission_locking import (
    AdmissionLockBusy,
    AdmissionRowLock,
    acquire_admission_keys,
    lock_admission_rows,
    node_admission_key,
)
from ..disk_reservations import outstanding_disk_reservation_bytes
from ..models import (
    AgentNode,
    CatalogDocumentRevision,
    RecipeBuild,
    ResourceReservation,
)
from ..recipe_build_receipts import CompletedRecipeBuild
from ..recipe_execution_contract import (
    RecipeExecutionContractError,
    build_plan_document,
    parse_stored_build_plan,
    parse_stored_build_policy,
)
from .common import (
    _BUILD_OBSERVATION_DELAYS,
    _OCI_DIGEST,
    _SHA256,
    BUILD_ARTIFACT_FORMAT,
    RecipeBuildAdmissionBusy,
    RecipeBuildInvalid,
    RecipeBuildPlan,
    RecipeBuildRefused,
    RecipeBuildUnknown,
    _available_build_memory,
    _build_disk_envelope,
    _build_disk_reserve,
    _reopen_build_attempt,
    _valid_succeeded_receipt,
    _validate_builder,
)

if TYPE_CHECKING:
    from .service import RecipeBuildService


def persist_plan_in_session(
    self: RecipeBuildService, session: Session, plan: RecipeBuildPlan, *, now: datetime
) -> RecipeBuildPlan:
    """Persist a prepared plan using the caller's transaction.

    This helper intentionally never opens another transaction.  It may be
    called while the availability parent and builder rows are locked.
    """
    policy_document = plan.policy_report
    if not isinstance(policy_document, dict):
        raise RecipeBuildUnknown(
            RecipeBuildCode.PLAN_INVALID,
            "prepared source build policy is unavailable",
            reason=WaitReason.STALE_PLAN,
        )
    try:
        policy = parse_stored_build_policy(policy_document)
    except RecipeExecutionContractError as error:
        raise RecipeBuildUnknown(
            RecipeBuildCode.PLAN_INVALID,
            "prepared source build policy is invalid" + error.detail,
            reason=WaitReason.STALE_PLAN,
        ) from error
    if policy.prebuilt_image is None:
        try:
            acquire_admission_keys(
                session,
                (node_admission_key(plan.builder_node_id),),
                holder="recipe-build",
            )
            locked = lock_admission_rows(
                session,
                (
                    AdmissionRowLock(
                        "build-builder-node",
                        AgentNode,
                        select(AgentNode).where(
                            AgentNode.node_id == plan.builder_node_id
                        ),
                    ),
                ),
            )
            node = next(iter(locked["build-builder-node"]), None)
        except AdmissionLockBusy as error:
            raise RecipeBuildAdmissionBusy() from error
        if node is None:
            raise RecipeBuildUnknown(
                RecipeBuildCode.NODE_UNKNOWN,
                "builder GPU node is unknown",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        _validate_builder(node)
        if policy.builder_binary_digest != node.binary_digest:
            raise RecipeBuildUnknown(
                RecipeBuildCode.RUNTIME_CHANGED,
                "builder runtime identity changed",
                reason=WaitReason.SCOPE_CHANGED,
            )
    existing = session.scalar(
        select(RecipeBuild).where(
            RecipeBuild.recipe_revision_id == plan.recipe_revision_id,
            RecipeBuild.builder_node_id == plan.builder_node_id,
            RecipeBuild.build_input_sha256 == plan.build_input_sha256,
        )
    )
    if (
        existing is not None
        and existing.state == "succeeded"
        and _valid_succeeded_receipt(existing)
        and not self._succeeded_build_available(existing)
    ):
        _reopen_build_attempt(existing, now=now)
    if existing is None:
        # Reusable image bytes are keyed by executable inputs, not by
        # editorial recipe provenance. Only a succeeded receipt may cross
        # a revision boundary.
        candidates = session.scalars(
            select(RecipeBuild)
            .where(
                RecipeBuild.builder_node_id == plan.builder_node_id,
                RecipeBuild.build_input_sha256 == plan.build_input_sha256,
                RecipeBuild.state == "succeeded",
            )
            .order_by(RecipeBuild.updated_at.desc(), RecipeBuild.id.desc())
        )
        existing = next(
            (
                candidate
                for candidate in candidates
                if self._succeeded_build_available(candidate)
            ),
            None,
        )
    payload = build_plan_document(copy.deepcopy(plan.agent_payload))
    if existing is None:
        existing = RecipeBuild(
            id=plan.build_id,
            recipe_revision_id=plan.recipe_revision_id,
            builder_node_id=plan.builder_node_id,
            source_bundle_sha256=plan.source_bundle_sha256,
            build_input_sha256=plan.build_input_sha256,
            state="planned",
            policy_report=copy.deepcopy(policy_document),
            plan=copy.deepcopy(payload),
            created_at=now,
            updated_at=now,
        )
        session.add(existing)
        session.flush()
    elif existing.recipe_revision_id == plan.recipe_revision_id:
        try:
            payload = build_plan_document(existing.plan)
            parse_stored_build_policy(existing.policy_report)
        except RecipeExecutionContractError:
            # Restore this disposable projection from the freshly compiled
            # exact input identity. The dispatched operation retains its own
            # accepted plan/attempt authority; repairing this index neither
            # replaces that execution nor cancels another consumer.
            payload["build_id"] = existing.id
            existing.plan = copy.deepcopy(payload)
            existing.policy_report = copy.deepcopy(policy_document)
            if existing.state == LifecycleState.FAILED.value or (
                existing.state == LifecycleState.SUCCEEDED.value
                and not _valid_succeeded_receipt(existing)
            ):
                _reopen_build_attempt(existing, now=now)
        else:
            if existing.state == "failed":
                # A cancelled or failed attempt must not block the rebuild
                # the operator asked for.  The stored envelope is still
                # exact, so keep it and return the row to a clean planned
                # attempt rather than reusing a terminal row.
                _reopen_build_attempt(existing, now=now)
    else:
        payload["build_id"] = existing.id
        payload["recipe_revision_id"] = plan.recipe_revision_id
        payload["recipe_content_sha256"] = plan.recipe_content_sha256
    try:
        payload = build_plan_document(payload)
    except RecipeExecutionContractError as error:
        raise RecipeBuildUnknown(
            RecipeBuildCode.PLAN_INVALID,
            "stored source build plan is invalid" + error.detail,
            reason=WaitReason.STALE_PLAN,
        ) from error
    return RecipeBuildPlan(
        build_id=existing.id,
        recipe_revision_id=plan.recipe_revision_id,
        recipe_content_sha256=plan.recipe_content_sha256,
        builder_node_id=plan.builder_node_id,
        source_bundle_sha256=plan.source_bundle_sha256,
        build_input_sha256=plan.build_input_sha256,
        agent_payload=payload,
        policy_report=copy.deepcopy(policy_document),
    )


def record_success(
    self: RecipeBuildService,
    build_id: str,
    *,
    build_input_sha256: str,
    image_digest: str,
    oci_layout_sha256: str,
    image_bytes: int,
    now: datetime,
) -> CompletedRecipeBuild:
    """Re-observe a damaged completion index within a finite attempt budget."""
    last_error: RecipeBuildUnknown | None = None
    for delay in _BUILD_OBSERVATION_DELAYS:
        if delay:
            self._sleep(delay)
        try:
            return _record_success_once(
                self,
                build_id,
                build_input_sha256=build_input_sha256,
                image_digest=image_digest,
                oci_layout_sha256=oci_layout_sha256,
                image_bytes=image_bytes,
                now=now,
            )
        except RecipeBuildUnknown as error:
            last_error = error
    assert last_error is not None
    raise last_error


def _record_success_once(
    self: RecipeBuildService,
    build_id: str,
    *,
    build_input_sha256: str,
    image_digest: str,
    oci_layout_sha256: str,
    image_bytes: int,
    now: datetime,
) -> CompletedRecipeBuild:
    if (
        _SHA256.fullmatch(build_input_sha256) is None
        or _OCI_DIGEST.fullmatch(image_digest) is None
        or _SHA256.fullmatch(oci_layout_sha256) is None
        or not isinstance(image_bytes, int)
        or isinstance(image_bytes, bool)
        or image_bytes < 1
    ):
        raise RecipeBuildInvalid(
            RecipeBuildCode.EVIDENCE_INVALID, "build result evidence is invalid"
        )
    with self._sessions.begin() as session:
        build = session.get(RecipeBuild, build_id, with_for_update=True)
        if build is None:
            # The derived index is disposable. Verified ingress completion still
            # describes these bytes; the request-led planner reindexes storage.
            return CompletedRecipeBuild(
                build_id, image_digest, oci_layout_sha256, image_bytes
            )
        if build.build_input_sha256 != build_input_sha256:
            raise RecipeBuildUnknown(
                RecipeBuildCode.INPUT_MISMATCH,
                "build result does not match its inputs",
            )
        # The current accepted input binds the completion. An old failed or
        # succeeded label is not an independent content-verification authority.
        build.state = LifecycleState.SUCCEEDED.value
        build.image_digest = image_digest
        build.oci_layout_sha256 = oci_layout_sha256
        build.image_bytes = image_bytes
        build.error = None
        build.updated_at = now
    return CompletedRecipeBuild(build_id, image_digest, oci_layout_sha256, image_bytes)


def reserve_in_session(
    self: RecipeBuildService,
    session: Session,
    plan: RecipeBuildPlan,
    *,
    now: datetime,
    request_id: str | None = None,
) -> None:
    try:
        acquire_admission_keys(
            session,
            (node_admission_key(plan.builder_node_id),),
            holder="recipe-build",
        )
        self._reserve_in_session(session, plan, now=now, request_id=request_id)
    except AdmissionLockBusy as error:
        raise RecipeBuildAdmissionBusy() from error
    except RecipeBuildUnknown:
        raise
    except RecipeBuildRefused:
        raise
    except ValueError as error:
        raise RecipeBuildUnknown(
            RecipeBuildCode.CAPACITY_CONTRACT_INVALID, str(error)
        ) from error
    except OperationalError as error:
        code = getattr(error.orig, "sqlstate", None) or getattr(
            error.orig, "pgcode", None
        )
        if code in {"55P03", "40P01", "40001", "57014"}:
            raise RecipeBuildAdmissionBusy() from error
        if code in {"28000", "28P01", "42501"}:
            raise RecipeBuildRefused(
                SecurityRefusalReason.PERMISSION_DENIED.value,
                "build admission authority access was denied",
                reason=SecurityRefusalReason.PERMISSION_DENIED,
            ) from error
        raise RecipeBuildUnknown(
            RecipeBuildCode.CAPACITY_CONTRACT_INVALID,
            "build admission observation is unavailable",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        ) from error


def _reserve_in_session(
    self: RecipeBuildService,
    session: Session,
    plan: RecipeBuildPlan,
    *,
    now: datetime,
    request_id: str | None,
) -> None:
    locked = lock_admission_rows(
        session,
        (
            AdmissionRowLock(
                "build-builder-node",
                AgentNode,
                select(AgentNode).where(AgentNode.node_id == plan.builder_node_id),
            ),
            AdmissionRowLock(
                "build-recipe-revision",
                CatalogDocumentRevision,
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.id == plan.recipe_revision_id
                ),
            ),
            AdmissionRowLock(
                "build-recipe-build",
                RecipeBuild,
                select(RecipeBuild).where(RecipeBuild.id == plan.build_id),
            ),
        ),
    )
    node = next(iter(locked["build-builder-node"]), None)
    revision = next(iter(locked["build-recipe-revision"]), None)
    build = next(iter(locked["build-recipe-build"]), None)
    if (
        revision is None
        or revision.kind != "recipe"
        or revision.state != "active"
        or revision.content_digest != plan.recipe_content_sha256
    ):
        raise RecipeBuildUnknown(
            RecipeBuildCode.DEPENDENCIES_STALE, "exact recipe dependencies changed"
        )
    if build is None:
        raise RecipeBuildUnknown(
            RecipeBuildCode.PLAN_INVALID,
            "stored build identity is invalid",
            reason=WaitReason.STALE_PLAN,
        )
    try:
        stored_policy = parse_stored_build_policy(build.policy_report)
        parse_stored_build_plan(build.plan)
        requested_plan = parse_stored_build_plan(plan.agent_payload)
    except RecipeExecutionContractError as error:
        raise RecipeBuildUnknown(
            RecipeBuildCode.PLAN_INVALID,
            "stored source build envelope is invalid" + error.detail,
            reason=WaitReason.STALE_PLAN,
        ) from error
    expected_binary_digest = stored_policy.builder_binary_digest
    expected_format = stored_policy.artifact_format
    if (
        build.builder_node_id != plan.builder_node_id
        or build.build_input_sha256 != plan.build_input_sha256
        or expected_format != BUILD_ARTIFACT_FORMAT
        or requested_plan.build_id != plan.build_id
        or requested_plan.build_input_sha256 != plan.build_input_sha256
    ):
        raise RecipeBuildUnknown(
            RecipeBuildCode.PLAN_INVALID,
            "stored build identity is invalid",
            reason=WaitReason.STALE_PLAN,
        )
    try:
        snapshot = self._inventory.latest(
            plan.builder_node_id, now=now, maximum_age=self._inventory_max_age
        )
    except KeyError as error:
        raise RecipeBuildUnknown(
            RecipeBuildCode.INVENTORY_MISSING,
            "fresh builder inventory is unavailable",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        ) from error
    if node is None:
        raise RecipeBuildUnknown(
            RecipeBuildCode.NODE_UNKNOWN,
            "builder GPU node is unknown",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    _validate_builder(node)
    if node.binary_digest != expected_binary_digest:
        raise RecipeBuildUnknown(
            RecipeBuildCode.RUNTIME_CHANGED,
            "builder runtime identity changed",
            reason=WaitReason.SCOPE_CHANGED,
        )
    if snapshot.stale:
        raise RecipeBuildUnknown(
            RecipeBuildCode.INVENTORY_STALE,
            "builder inventory is stale",
            reason=WaitReason.STALE_PLAN,
        )
    plan_payload = build_plan_document(requested_plan)
    limits = plan_payload.get("limits")
    source_bytes = plan_payload.get("source_bundle_bytes")
    if not isinstance(limits, dict) or not isinstance(source_bytes, int):
        raise RecipeBuildUnknown(
            RecipeBuildCode.PLAN_INVALID,
            "build plan is invalid",
            reason=WaitReason.STALE_PLAN,
        )
    temporary_bytes = limits.get("temporary_bytes")
    memory_bytes = limits.get("memory_bytes")
    output_bytes = limits.get("output_bytes")
    base_image_storage_bytes = plan_payload.get("base_image_storage_bytes")
    if (
        not isinstance(temporary_bytes, int)
        or not isinstance(memory_bytes, int)
        or not isinstance(output_bytes, int)
        or not isinstance(base_image_storage_bytes, int)
    ):
        raise RecipeBuildUnknown(
            RecipeBuildCode.PLAN_INVALID,
            "build plan is invalid",
            reason=WaitReason.STALE_PLAN,
        )
    disk_bytes = _build_disk_envelope(
        base_image_bytes=base_image_storage_bytes,
        temporary_bytes=temporary_bytes,
        source_bytes=source_bytes,
        output_bytes=output_bytes,
    )
    if snapshot.disk_free_bytes - outstanding_disk_reservation_bytes(
        session, plan.builder_node_id, inventory_observed_at=snapshot.observed_at
    ) < disk_bytes + _build_disk_reserve(snapshot.disk_total_bytes):
        raise RecipeBuildUnknown(
            RecipeBuildCode.INSUFFICIENT_DISK, "builder disk capacity changed"
        )
    if (
        _available_build_memory(
            session, snapshot, build=build, request_id=request_id, lock=True
        )
        < memory_bytes
    ):
        raise RecipeBuildUnknown(
            RecipeBuildCode.INSUFFICIENT_MEMORY, "builder memory capacity changed"
        )
    session.add_all(
        (
            ResourceReservation(
                node_id=plan.builder_node_id,
                kind="disk",
                resource_key=plan.build_input_sha256,
                amount_bytes=disk_bytes,
                owner_kind="recipe-build",
                owner_id=plan.build_id,
                state=ReservationState.ACTIVE,
                plan_digest=plan.build_input_sha256,
                created_at=now,
            ),
            ResourceReservation(
                node_id=plan.builder_node_id,
                kind="host-memory",
                resource_key=plan.build_input_sha256,
                amount_bytes=memory_bytes,
                owner_kind="recipe-build",
                owner_id=plan.build_id,
                state=ReservationState.ACTIVE,
                plan_digest=plan.build_input_sha256,
                created_at=now,
            ),
        )
    )
