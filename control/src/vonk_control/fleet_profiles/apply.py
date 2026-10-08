"""Apply for Fleet profiles."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from vonk_agent_protocol import (
    InvalidRequestReason,
    LifecycleState,
    SupersedeCode,
    canonical_message,
)
from vonk_agent_protocol.agent_words import (
    ProfileOperationKind,
)

from .. import job_states
from ..admission_locking import (
    AdmissionLockBusy,
    acquire_admission_keys,
    is_admission_contention,
    node_admission_key,
)
from ..categorized_errors import MissingRecord
from ..fleet_profile_contract import (
    FleetProfileApplicationProgress,
    FleetProfileApplicationView,
    FleetProfilePreview,
)
from ..lifecycle.evidence import Residue
from ..models import AgentNode, FleetProfile, FleetProfileApplication
from ..strict_json import read_stored_model
from .assessment_support import (
    _deferral_code,
    _preview_blockers,
    _profile_preview_is_waitable,
    _storage_wait_of,
    _storage_wait_of_preview,
)
from .contracts import (
    FleetProfileAdmissionBusy,
    FleetProfileAdmissionEffectBusy,
    FleetProfileAdmissionStorageError,
    FleetProfileConflict,
    FleetProfileInvalid,
    FleetProfilePermissionDenied,
    FleetProfileReviewStale,
    FleetProfileStalePlanConflict,
    _FleetProfileSupersededIntentConflict,
)
from .persistence import (
    _application_order_key,
    _newer_profile_intent_overlaps,
    _owns_pending_admission,
    _persisted_profile_plan,
    _persisted_profile_progress,
)
from .projection_support import (
    _aware,
)

if TYPE_CHECKING:
    from .service import FleetProfileService
    from .service import FleetProfileService as _FleetProfileService


class FleetProfileService:
    def _prepare_pending_admission(
        self, application_id: str, plan: FleetProfilePreview
    ) -> None:
        """Fence older workload effects before retrying the full admission lock."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        if self._switch_adapter is None:
            return
        execution_nodes = tuple(
            sorted({node_id for step in plan.steps for node_id in step.node_ids})
        )
        if not execution_nodes:
            return
        now = _aware(self._clock())
        try:
            with self._sessions.begin() as session:
                row = session.get(
                    FleetProfileApplication,
                    application_id,
                    with_for_update={"nowait": True},
                )
                if row is None:
                    return
                progress = _persisted_profile_progress(row)
                if not _owns_pending_admission(row, progress):
                    return
                # Continue accepted intent under platform authority. Selection
                # and exact-plan fences below still reject superseded effects.
                profile = session.get(FleetProfile, row.profile_id)
                if profile is None:
                    raise MissingRecord(
                        row.profile_id, reason=InvalidRequestReason.NOT_FOUND
                    )
                if row.selection_generation is not None:
                    progress = _persisted_profile_progress(row)
                    if not self._application_is_current_selection(
                        session, row, progress
                    ):
                        raise _FleetProfileSupersededIntentConflict(
                            "Selected Fleet profile was replaced before admission resumed"
                        )
                else:
                    self._validate_draft_review_identity(
                        session,
                        profile,
                        profile_digest=plan.profile_digest,
                        assignments=tuple(plan.resolved_assignments),
                    )
                acquire_admission_keys(
                    session,
                    tuple(node_admission_key(node_id) for node_id in execution_nodes),
                    holder="profile-workload-fence",
                )
                nodes = tuple(
                    session.scalars(
                        select(AgentNode)
                        .where(AgentNode.node_id.in_(execution_nodes))
                        .order_by(AgentNode.node_id)
                        .with_for_update(nowait=True)
                    )
                )
                if tuple(node.node_id for node in nodes) != execution_nodes:
                    raise FleetProfileStalePlanConflict(
                        "Pending profile workload scope changed before fencing"
                    )
                if _newer_profile_intent_overlaps(
                    session,
                    _application_order_key(session, row),
                    set(execution_nodes),
                ):
                    raise _FleetProfileSupersededIntentConflict(
                        "Pending profile intent was superseded by a later accepted request"
                    )
                ordinal = progress.workload_intent_ordinal
                if ordinal is None:
                    ordinal = max(node.workload_intent_ordinal for node in nodes) + 1
                    for node in nodes:
                        node.workload_intent_ordinal = ordinal
                elif any(node.workload_intent_ordinal != ordinal for node in nodes):
                    raise _FleetProfileSupersededIntentConflict(
                        "Pending profile workload intent was superseded before recovery"
                    )
                self._switch_adapter.request_superseded_workload_cancellation_in_session(
                    session, execution_nodes, ordinal, now
                )
                # Retire older parked profile applications while the newer
                # intent owns the node fence. Waiting applications have no
                # issued effects; leaving them eligible lets their retry loop
                # contend with the newer intent and starve admission.
                for prior in session.scalars(
                    select(FleetProfileApplication)
                    .where(
                        FleetProfileApplication.id != application_id,
                        FleetProfileApplication.state.in_(
                            job_states.words(
                                LifecycleState.QUEUED,
                                LifecycleState.RUNNING,
                                LifecycleState.NEEDS_OPERATOR,
                            )
                        ),
                    )
                    .order_by(FleetProfileApplication.id)
                    .with_for_update(nowait=True)
                ):
                    prior_progress = _persisted_profile_progress(prior)
                    prior_plan = _persisted_profile_plan(prior)
                    if isinstance(prior_plan, Residue):
                        continue
                    prior_scope = {
                        node_id
                        for step in prior_plan.steps
                        for node_id in step.node_ids
                    }
                    if not prior_scope & set(execution_nodes):
                        continue
                    if (
                        _owns_pending_admission(prior, prior_progress)
                        and prior_progress.intended_profile is not None
                    ):
                        # Pending receipts can be inserted without taking the
                        # node fence, so check ordering again after locking the
                        # row. Retire older unbound receipts even when they
                        # never received an ordinal.
                        try:
                            prior_order = _application_order_key(session, prior)
                        except FleetProfileConflict:
                            # Broken retry lineage cannot grant a receipt
                            # ordering authority. Preserve the pre-existing
                            # ordinal rule for malformed siblings instead.
                            prior_order = None
                        if (
                            prior_order is not None
                            and prior_order > _application_order_key(session, row)
                        ):
                            raise _FleetProfileSupersededIntentConflict(
                                "A later accepted profile intent owns the workload scope"
                            )
                        if prior_order is None:
                            prior_ordinal = prior_progress.workload_intent_ordinal
                            if prior_ordinal is None or prior_ordinal >= ordinal:
                                continue
                        self._lifecycle.supersede(
                            prior,
                            "Profile order was replaced before admission by a later "
                            "scoped intent",
                            now,
                            code=SupersedeCode.SUPERSEDED_BY_INTENT,
                            by=application_id,
                            session=session,
                        )
                        continue
                    prior_ordinal = prior_progress.workload_intent_ordinal
                    if prior_ordinal is None or prior_ordinal >= ordinal:
                        continue
                    self._lifecycle.supersede(
                        prior,
                        "Profile order was replaced before admission by a later "
                        "scoped intent",
                        now,
                        code=SupersedeCode.SUPERSEDED_BY_INTENT,
                        by=application_id,
                        session=session,
                    )
                progress_data = progress.model_dump(mode="json")
                if progress.workload_intent_ordinal is None:
                    progress_data["workload_intent_ordinal"] = ordinal
                row.progress = read_stored_model(
                    FleetProfileApplicationProgress,
                    canonical_message(progress_data),
                    strict=True,
                    from_json=True,
                ).model_dump(mode="json")
                row.updated_at = now
        except AdmissionLockBusy as error:
            raise FleetProfileAdmissionEffectBusy(
                "Profile admission is waiting for the active workload owner to finish; the Controller will retry automatically."
            ) from error
        except OperationalError as error:
            if is_admission_contention(error):
                raise FleetProfileAdmissionEffectBusy(
                    "Profile admission is waiting for the active workload owner to finish; the Controller will retry automatically."
                ) from None
            raise

    def apply(
        self,
        profile_id: str,
        *,
        request_key: str,
        actor: str,
        reviewed_effects_digest: str | None = None,
    ) -> FleetProfileApplicationView:
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        replay = self._load_replay(profile_id, request_key=request_key, actor=actor)
        if replay is not None:
            return replay
        pending: FleetProfileApplicationView | None = None
        try:
            preview = self.preview(profile_id)
            if (
                reviewed_effects_digest is not None
                and preview.effects_digest != reviewed_effects_digest
            ):
                # Refused before anything is persisted: the caller reviews the
                # current plan and asks again, with the same or a new key.
                raise FleetProfileReviewStale(
                    "The profile plan changed since it was reviewed; review the "
                    "current plan and load again."
                )
            if not preview.allowed:
                if not _profile_preview_is_waitable(preview):
                    raise FleetProfileInvalid(
                        "Fleet profile intent contains a security or contract blocker",
                        reason=InvalidRequestReason.CONFLICT,
                    )
                pending = self._create_pending_application(
                    preview,
                    request_key=request_key,
                    actor=actor,
                    operation_kind=ProfileOperationKind.APPLY.value,
                    select_profile=True,
                )
                blockers = _preview_blockers(preview) + self._request_preparations(
                    preview, actor=actor, application_id=pending.id
                )
                return self._defer_pending_application(
                    pending.id,
                    "Waiting for current Fleet conditions: "
                    + "; ".join(item.code for item in blockers[:8])
                    + ".",
                    blockers=blockers,
                    storage=_storage_wait_of_preview(preview),
                    wait_for_space=True,
                )
            # A review that planned an eviction is admitted: start it now.
            self._request_storage(preview)
            pending = self._create_pending_application(
                preview,
                request_key=request_key,
                actor=actor,
                operation_kind=ProfileOperationKind.APPLY.value,
                select_profile=True,
            )
            # Acceptance already has a durable owner and immutable reviewed
            # intent. Try admission once; a busy lock is retried by that owner,
            # rather than repeating SQL and sleeping inside the HTTP request.
            try:
                return self._queue_application(
                    preview,
                    request_key=request_key,
                    actor=actor,
                    operation_kind=ProfileOperationKind.APPLY.value,
                    pending_application_id=pending.id,
                )
            except FleetProfileAdmissionBusy as busy:
                return self._defer_pending_application(
                    pending.id,
                    "Profile admission is busy"
                    + (
                        f" ({busy.holder} holds a selected Spark)"
                        if busy.holder
                        else ""
                    )
                    + "; the Controller will retry automatically.",
                    retry_delay=timedelta(0),
                )
            except (
                FleetProfileAdmissionEffectBusy,
                FleetProfileAdmissionStorageError,
            ) as error:
                return self._defer_pending_application(
                    pending.id,
                    str(error),
                    retry_delay=timedelta(seconds=60)
                    if isinstance(error, FleetProfileAdmissionStorageError)
                    else timedelta(0),
                    code=_deferral_code(error),
                    storage=_storage_wait_of(error),
                )
        except (
            FleetProfileConflict,
            FleetProfilePermissionDenied,
            KeyError,
        ) as error:
            if pending is not None:
                if isinstance(error, _FleetProfileSupersededIntentConflict):
                    self._finish_pending_admission(
                        pending.id,
                        state=LifecycleState.SUPERSEDED,
                        reason=str(error),
                        code=SupersedeCode.SUPERSEDED_BY_INTENT,
                    )
                else:
                    self._discard_pending_application(pending.id)
            if isinstance(error, FleetProfilePermissionDenied):
                raise
            # Another identical submission can commit after our first lookup.
            # Its accepted receipt wins over a newly stale preview or a busy
            # admission boundary; this read never refreshes the approved intent.
            replay = self._load_replay(profile_id, request_key=request_key, actor=actor)
            if replay is not None:
                if replay.state in job_states.words(LifecycleState.NEEDS_OPERATOR):
                    self._discard_pending_application(replay.id)
                    raise
                return replay
            raise
