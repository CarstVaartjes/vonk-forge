"""Contracts for Fleet profiles."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Literal, Protocol

from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    InvalidRequestError,
    InvalidRequestReason,
    ProfileReasonCode,
    SecurityRefusalError,
    SecurityRefusalReason,
    SupersedeCode,
    UnknownOutcomeError,
    WaitReason,
)

from ..fleet_profile_contract import (
    FleetProfileAssignment,
    FleetProfileDefinition,
    FleetProfileEffects,
    FleetProfileIntendedConfiguration,
    FleetProfilePreview,
    FleetProfileReason,
    FleetProfileSupersedeCode,
)
from ..lifecycle.evidence import Residue
from ..operation_blockers import OperationBlocker
from ..preparation_contract import RuntimeImageIdentity
from ..run_switch_contract import RunSwitchAssessment
from ..run_switch_operations import RunSwitchOperationConflict
from ..storage_demands import StorageRelief
from .dependencies import RETRY_SUPERSEDE, RETRY_WAIT

if TYPE_CHECKING:
    from .service import FleetProfileService


class PreparationStarter(Protocol):
    """Start (or find) the durable preparation of one exact recipe revision.

    The Controller prepares what a load needs by itself: the model download and
    the runtime image build.  The starter is idempotent for one revision and
    returns the reasons the preparation is not finished yet, so the load can
    say what it is waiting for.
    """

    def __call__(
        self, recipe_revision_id: str, *, actor: str, application_id: str | None = None
    ) -> Sequence[OperationBlocker]: ...


class StorageReliefProvider(Protocol):
    """Ask for free disk on one Spark for a load that was refused for lack of it.

    Returns the named reason to show on the waiting load, or ``None`` when the
    Spark's free space cannot be read or already covers the request.
    """

    def __call__(
        self,
        node_id: str,
        required_free_bytes: int,
        *,
        source: str,
        subject: str,
        reason: str,
    ) -> StorageRelief | None: ...


class PreparationCanceller(Protocol):
    """Cancel the pending preparation one exact recipe revision still runs.

    Returns the ids of the operations it asked to cancel. A preparation another
    accepted consumer still needs is left alone by its owner.
    """

    def __call__(
        self,
        recipe_revision_id: str,
        *,
        actor: str,
        reason: str,
        application_id: str | None = None,
    ) -> Sequence[str]: ...


class _AssessmentProvider(Protocol):  # noqa: PYI046 -- used by service composition
    def __call__(
        self,
        session: Session,
        assignment: FleetProfileAssignment,
        expected_nodes: tuple[str, ...],
        /,
        *,
        allow_pending_cache_rebuild: bool,
        expected_runtime_image: RuntimeImageIdentity | None,
        excluded_profile_application_ids: tuple[str, ...],
    ) -> RunSwitchAssessment | Residue: ...


@dataclass(frozen=True)
class _ProfileControlEffects:
    """One SQL-owned reconciliation projection for review and admission."""

    states: dict[str, FleetProfileService._AssignmentState]
    effects: FleetProfileEffects
    changed_nodes: set[str]
    unavailable_assignment_ids: set[str]
    switch_needed: bool
    reasons: list[FleetProfileReason]


@dataclass(frozen=True)
class _SelectedProfileSnapshot:
    generation: int
    profile_id: str
    profile_revision: int
    application_id: str
    roster_node_ids: tuple[str, ...]
    roster_digest: str
    actor: str
    intended: FleetProfileIntendedConfiguration
    plan: FleetProfilePreview


class FleetProfileConflict(RuntimeError):
    """A Fleet profile is invalid, stale, or cannot be safely applied.

    ``retry_disposition`` says what an automatic retry does with it, and every
    subclass declares it: ``RETRY_WAIT`` parks for retry, ``RETRY_SUPERSEDE``
    (the default for an untyped conflict, which is a state check that waiting
    cannot change) ends the application ``superseded`` with ``supersede_code``.
    """

    retry_disposition: ClassVar[str] = RETRY_SUPERSEDE
    supersede_code: ClassVar[FleetProfileSupersedeCode] = (
        SupersedeCode.EFFECTS_CHANGED_DURING_ADMISSION
    )


class FleetProfileInvalid(InvalidRequestError, FleetProfileConflict):
    """The request, or what it names, is out of contract or conflicts with the
    stored profile state; the caller corrects it and asks again."""

    retry_disposition = RETRY_SUPERSEDE


class FleetProfileUnavailable(UnknownOutcomeError, FleetProfileConflict):
    """Persisted bookkeeping or evidence that cannot settle the outcome here."""

    retry_disposition = RETRY_SUPERSEDE


class FleetProfileUnsupportedStore(InvalidRequestError, RuntimeError):
    """The database dialect cannot hold Fleet profile selection."""


class FleetProfileChildPlanBlocked(UnknownOutcomeError, RunSwitchOperationConflict):
    """The exact accepted child waits for a recoverable admission observation."""

    retry_disposition = RETRY_WAIT

    def __init__(
        self,
        *args: object,
        reason: WaitReason = WaitReason.OBSERVATION_UNAVAILABLE,
    ) -> None:
        super().__init__(*args, reason=reason)


class FleetProfileAdmissionBusy(UnknownOutcomeError, FleetProfileConflict):
    """A transient admission owner must finish before the plan can be bound.

    ``holder`` names the kind of work that holds the Spark's admission lock when
    that is known, so the wait says what it is waiting for.
    """

    code = ProfileReasonCode.ADMISSION_BUSY
    retry_disposition = RETRY_WAIT

    def __init__(
        self,
        message: str,
        *,
        holder: str | None = None,
        reason: WaitReason = WaitReason.OBSERVATION_UNAVAILABLE,
    ) -> None:
        super().__init__(message, reason=reason)
        self.holder = holder


class FleetProfileAdmissionStorageError(UnknownOutcomeError, FleetProfileConflict):
    """A persisted intent awaits correction of a database constraint failure."""

    retry_disposition = RETRY_WAIT

    def __init__(
        self,
        *args: object,
        reason: WaitReason = WaitReason.OBSERVATION_UNAVAILABLE,
    ) -> None:
        super().__init__(*args, reason=reason)


class FleetProfileAdmissionEffectBusy(UnknownOutcomeError, FleetProfileConflict):
    """A live effect owner must finish before a superseding plan can bind.

    ``shortfalls`` holds ``(node_id, free_bytes_needed)`` when the wait is for
    disk on named Sparks that eviction may free: the parked load then asks the
    storage collector for exactly that, so the wait ends by itself, or in a
    typed refusal when nothing more can be freed.
    """

    code = ProfileReasonCode.ADMISSION_EFFECT_BUSY
    retry_disposition = RETRY_WAIT

    def __init__(
        self,
        *args: object,
        reason: WaitReason = WaitReason.OBSERVATION_UNAVAILABLE,
        shortfalls: tuple[tuple[str, int], ...] = (),
    ) -> None:
        super().__init__(*args, reason=reason)
        self.shortfalls = shortfalls


class FleetProfileResourceRecheckUnavailable(FleetProfileAdmissionEffectBusy):
    """The resource recheck under the admission fence failed for a named cause."""

    code = ProfileReasonCode.RESOURCE_RECHECK_UNAVAILABLE
    retry_disposition = RETRY_WAIT


class FleetProfileStalePlanConflict(InvalidRequestError, FleetProfileConflict):
    """Admission refused because the caller's reviewed plan is no longer current."""

    code = ProfileReasonCode.STALE_PLAN
    retry_disposition = RETRY_SUPERSEDE
    supersede_code = SupersedeCode.EFFECTS_CHANGED_DURING_ADMISSION

    def __init__(
        self,
        *args: object,
        reason: InvalidRequestReason = InvalidRequestReason.SUPERSEDED,
    ) -> None:
        super().__init__(*args, reason=reason)


class FleetProfileReviewStale(FleetProfileStalePlanConflict):
    """The reviewed effects differ from the current plan; nothing was accepted."""

    code = ProfileReasonCode.REVIEW_STALE
    retry_disposition = RETRY_SUPERSEDE
    supersede_code = SupersedeCode.EFFECTS_CHANGED_DURING_ADMISSION


class _FleetProfileSupersededIntentConflict(FleetProfileStalePlanConflict):
    """A later accepted intent owns an overlapping workload effect scope."""

    retry_disposition = RETRY_SUPERSEDE
    supersede_code = SupersedeCode.SUPERSEDED_BY_INTENT


class FleetProfileSelectionLost(FleetProfileStalePlanConflict):
    """A retry has no current selection to continue (it moved or was replaced).

    Waiting cannot give it one back; the retry's parent no longer owns the
    selected profile, so adopting it would undo a newer load.
    """

    code = ProfileReasonCode.SELECTION_LOST
    retry_disposition = RETRY_SUPERSEDE
    supersede_code = SupersedeCode.EFFECTS_CHANGED_DURING_ADMISSION


class FleetProfileAssetReservationConflict(FleetProfileConflict):
    """A profile asset could not be reserved right now; a later attempt may."""

    code = ProfileReasonCode.ASSET_RESERVATION_UNAVAILABLE
    retry_disposition = RETRY_WAIT


class FleetProfilePermissionDenied(SecurityRefusalError, PermissionError):
    """Current user authority cannot authorize this profile request."""

    def __init__(
        self,
        *args: object,
        reason: SecurityRefusalReason = SecurityRefusalReason.PERMISSION_DENIED,
    ) -> None:
        super().__init__(*args, reason=reason)


class _FleetProfileRecoveryBindingConflict(UnknownOutcomeError, FleetProfileConflict):
    """Recovery cannot adopt the currently available artifact identity."""

    retry_disposition = RETRY_WAIT

    def __init__(
        self,
        *args: object,
        reason: WaitReason = WaitReason.OBSERVATION_UNAVAILABLE,
    ) -> None:
        super().__init__(*args, reason=reason)


class SavedProfileDocument(FleetProfileDefinition):
    """Content identity of one persisted saved profile, without live projections."""

    schema_version: Literal[2] = 2
    id: str
    number: int
    revision: int
