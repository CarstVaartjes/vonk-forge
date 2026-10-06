"""The lifecycle and outcome vocabulary shared by Python, Rust and TypeScript.

This module is the one definition of the closed words the Controller's lifecycle
core, the agent's results and the CI ratchets speak.  ``scripts/export-agent-wire-schema``
publishes it through :class:`LifecycleVocabulary` into ``wire.json``, and
``scripts/generate-agent-wire`` turns that into Rust types, so no consumer
spells one of these words by hand.

The stored ``state`` of a lifecycle subject speaks :class:`LifecycleState`.  The
retired spellings (``waiting-for-operator``, ``cancelling``, ``waiting``,
``partial``, ``expired``) live in :data:`STATE_ALIASES` and nowhere else: a
reader of an old row adopts it through :func:`adopt_state`, a caller that still
sends one is understood through :func:`input_state`, for one release.  The agent
wire uses :class:`AgentResultState`; unknown effects are ``observing``.
:data:`LEGACY_WAIT_STATE` names only the retired spelling adopted on read.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any, NamedTuple

from .agent_words import FailureStage, HostHelperResponseStatus, ProgressPhase
from .state_machines import (
    AssetAvailability,
    CatalogSyncState,
    CertificateState,
    DesiredAssignmentState,
    DistributionAssignmentState,
    EndpointState,
    EnrollmentGrantState,
    GatewayRouteState,
    InstallationNodeState,
    InstallationState,
    ModelCacheOperatorStatus,
    ModelFileState,
    ObservedAssignmentState,
    PlacementInstallState,
    PlacementLoadState,
    ReservationState,
    RoutePublicationState,
    RouteState,
    RunState,
)
from .wire_model import WireEnum, WireModel


class LifecycleState(WireEnum):
    """The nine lifecycle states: the one vocabulary a stored ``state`` speaks.

    ``superseded`` is a definite, non-failed end: a newer request replaced the
    work, so nothing is left for anyone to do.  The legacy spellings
    (``waiting``, ``partial``, ``cancelling``, ``expired``,
    ``waiting-for-operator``) are *aliases*: see :data:`STATE_ALIASES`, the one
    table that says what each of them means, per subject.
    """

    QUEUED = "queued"
    RUNNING = "running"
    OBSERVING = "observing"
    BACKOFF = "backoff"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    NEEDS_OPERATOR = "needs-operator"


class AgentResultState(WireEnum):
    """The state words of an agent result on the wire.

    Unknown effects are observed by the Controller with bounded retries. The
    retired operator-wait spelling is adopted only when reading old receipts.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    OBSERVING = "observing"

    @classmethod
    def _missing_(cls, value: object) -> AgentResultState | None:
        return (
            RETIRED_RESULT_STATE_SPELLINGS.get(value)
            if isinstance(value, str)
            else None
        )


class LifecycleEffect(WireEnum):
    """What is known about the real-world effect of the work."""

    UNKNOWN = "unknown"
    NONE = "none"
    ISSUED = "issued"
    ESTABLISHED = "established"
    STOPPED = "stopped"


class OutcomeKind(WireEnum):
    """What an executor reported, as the lifecycle core sees it.

    ``done``, ``cancelled`` and a ``failed`` that is not retryable are
    *definite*: the executor says what happened, and the row ends there.
    ``unknown`` (and a retryable failure) says the effect may or may not have
    happened, which the core resolves by observing it.  On the agent wire a
    cancellation is a definite ``failed`` outcome with the
    ``operation_cancelled`` code.
    """

    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class StopOutcome(WireEnum):
    """Whether an idempotent stop confirmed that the effect is gone."""

    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"


class LifecycleEventKind(WireEnum):
    """The kinds of event the pure transition function accepts."""

    SUBMITTED = "submitted"
    CLAIMED = "claimed"
    HEARTBEAT = "heartbeat"
    REPORTED = "reported"
    LEASE_LAPSED = "lease-lapsed"
    CANCEL_REQUESTED = "cancel-requested"
    OBSERVED = "observed"
    OPERATOR_ACTION = "operator-action"
    TICK = "tick"


class OperatorActionName(WireEnum):
    """The operator actions a row can advertise and the core accepts."""

    RESUME = "resume"
    RETIRE = "retire"
    RETRY = "retry"
    STOP = "stop"


class OperatorSurface(WireEnum):
    """The real surfaces behind an advertised action in the blocker allowlist."""

    RESUME = "resume"
    RETIRE = "retire"
    RETRY = "retry"
    STOP = "stop"
    AUTOMATIC = "automatic"


#: The retired operator-wait spelling, accepted only by read adoption.
LEGACY_WAIT_STATE = "waiting-for-operator"


class BlockerCategory(WireEnum):
    """The categories of the blocker allowlist (fail-closed raises)."""

    SECURITY_EDGE = "security-edge"
    INPUT_VALIDATION = "input-validation"
    ALREADY_RETRIED = "already-retried"
    BOOKKEEPING_DEBT = "bookkeeping-debt"


class WaitVerdict(WireEnum):
    """The verdicts of the blocker allowlist for an operator wait."""

    KEEP = "KEEP"
    SELF_HEAL = "SELF-HEAL"
    FIX_ACTION = "FIX-ACTION"
    DERIVED = "DERIVED"


class LifecycleSubject(WireEnum):
    """The persisted models whose ``state`` the lifecycle core owns."""

    JOB = "Job"
    JOB_ATTEMPT = "JobAttempt"
    AGENT_OPERATION = "AgentOperation"
    AGENT_OPERATION_ATTEMPT = "AgentOperationAttempt"
    MODEL_CACHE_OPERATION = "ModelCacheOperation"
    ARTIFACT_JOB = "ArtifactJob"
    FLEET_PROFILE_APPLICATION = "FleetProfileApplication"


class StateAlias(WireEnum):
    """The retired spellings of a stored lifecycle state.

    They are accepted as input for one release (API filters, CLI arguments) and
    adopted when an old row is read; nothing writes them any more.  This is the
    only place the words may be spelled: the vocabulary ratchet allows them
    nowhere else.
    """

    WAITING_FOR_OPERATOR = "waiting-for-operator"
    CANCELLING = "cancelling"
    WAITING = "waiting"
    PARTIAL = "partial"
    EXPIRED = "expired"


#: Retired result spellings are adopted on reads, never emitted or published as enum members.
RETIRED_RESULT_STATE_SPELLINGS: Mapping[str, AgentResultState] = {
    StateAlias.WAITING_FOR_OPERATOR.value: AgentResultState.OBSERVING,
}


class ObservationCause(WireEnum):
    """Why an attempt is being observed rather than settled.

    An attempt that ended without a definite answer is ``observing``; this says
    what left it unanswered.  ``reported-unknown``: the executor said it could not
    confirm the effect.  ``lease-lapsed``: the executor stopped reporting.  The old
    spellings carried this in the state word itself (``waiting-for-operator`` and
    ``expired``), which is why an adopted attempt also yields its cause.
    """

    REPORTED_UNKNOWN = "reported-unknown"
    LEASE_LAPSED = "lease-lapsed"


class ArtifactPreparation(WireEnum):
    """The stages of an artifact job before it is submitted.

    These are preparation, not execution: a job is ``draft`` while its inputs are
    uploaded and ``ready`` once they are complete.  Its lifecycle ``state`` begins
    at ``queued`` on submit and is absent until then.  The old spelling kept both
    in the one ``state`` word, which is why :func:`legacy_preparation` exists.
    """

    DRAFT = "draft"
    READY = "ready"


def legacy_preparation(stored: str | None) -> ArtifactPreparation | None:
    """The preparation stage an old artifact-job ``state`` word meant, else ``None``."""

    try:
        return ArtifactPreparation(stored) if stored is not None else None
    except ValueError:
        return None


class AdoptedState(NamedTuple):
    """What a stored word means in the core vocabulary.

    ``cancel_requested`` is true for a word that said "a cancel is under way":
    the row is non-terminal and the kind records the cancel request
    (``cancel_requested_at``) in its own storage.
    """

    state: LifecycleState
    cancel_requested: bool = False


_NEEDS_OPERATOR = AdoptedState(LifecycleState.NEEDS_OPERATOR)
_CANCELLING = AdoptedState(LifecycleState.OBSERVING, cancel_requested=True)
_WAITING = AdoptedState(LifecycleState.OBSERVING)
#: Work that stopped part-way and is retried by itself, not yet settled.
_PARTIAL = AdoptedState(LifecycleState.BACKOFF)
#: A job that lapsed without an answer is over: nothing will report for it.
_JOB_EXPIRED = AdoptedState(LifecycleState.FAILED)
#: An attempt whose lease lapsed was interrupted, not refused: what it did is
#: unknown, so it is observed, and the operation decides what happens next.  The
#: same holds for an attempt the agent reported as ``waiting-for-operator`` (its
#: old word for "I could not confirm the effect"): an attempt is a record of one
#: try, never a wait for a person, so it too is observed.
_ATTEMPT_EXPIRED = AdoptedState(LifecycleState.OBSERVING)
_ATTEMPT_UNKNOWN = AdoptedState(LifecycleState.OBSERVING)

#: What each retired spelling means, per subject whose stored ``state`` the core
#: owns, and only for the words that subject has ever stored (its CHECK
#: constraint or its writers).  A word two subjects share may mean different
#: things (an expired *attempt* is observed, an expired *job* is over), which is
#: why the table is per subject.  Words outside the core vocabulary that a kind
#: keeps on purpose (``draft``) are not here, and a subject does not adopt a word
#: it never stored.
STATE_ALIASES: Mapping[LifecycleSubject, Mapping[StateAlias, AdoptedState]] = {
    # The generic job table is shared by many kinds, each of which spelled
    # "waiting to retry" or "being cancelled" its own way.
    LifecycleSubject.JOB: {
        StateAlias.WAITING_FOR_OPERATOR: _NEEDS_OPERATOR,
        StateAlias.WAITING: _WAITING,
        StateAlias.CANCELLING: _CANCELLING,
        StateAlias.PARTIAL: _PARTIAL,
        StateAlias.EXPIRED: _JOB_EXPIRED,
    },
    LifecycleSubject.JOB_ATTEMPT: {
        StateAlias.WAITING_FOR_OPERATOR: _ATTEMPT_UNKNOWN,
        StateAlias.EXPIRED: _ATTEMPT_EXPIRED,
    },
    LifecycleSubject.AGENT_OPERATION: {
        StateAlias.WAITING_FOR_OPERATOR: _NEEDS_OPERATOR,
    },
    LifecycleSubject.AGENT_OPERATION_ATTEMPT: {
        StateAlias.WAITING_FOR_OPERATOR: _ATTEMPT_UNKNOWN,
        StateAlias.EXPIRED: _ATTEMPT_EXPIRED,
    },
    LifecycleSubject.MODEL_CACHE_OPERATION: {
        StateAlias.PARTIAL: _PARTIAL,
    },
    LifecycleSubject.ARTIFACT_JOB: {
        StateAlias.WAITING_FOR_OPERATOR: _NEEDS_OPERATOR,
        StateAlias.CANCELLING: _CANCELLING,
    },
    LifecycleSubject.FLEET_PROFILE_APPLICATION: {
        StateAlias.WAITING_FOR_OPERATOR: _NEEDS_OPERATOR,
        # The state of the application's cancellation intent (its progress document):
        # a cancel being driven is observed.
        StateAlias.CANCELLING: _CANCELLING,
    },
}

#: The cause an old attempt spelling carried (see :class:`ObservationCause`).
ATTEMPT_ALIAS_CAUSE: Mapping[StateAlias, ObservationCause] = {
    StateAlias.WAITING_FOR_OPERATOR: ObservationCause.REPORTED_UNKNOWN,
    StateAlias.EXPIRED: ObservationCause.LEASE_LAPSED,
}


def legacy_observation_cause(stored: str) -> ObservationCause | None:
    """The cause an attempt's old state word meant, or ``None`` for any other word."""

    try:
        return ATTEMPT_ALIAS_CAUSE.get(StateAlias(stored))
    except ValueError:
        return None


#: What an alias means when the subject is not known (an API filter, a CLI
#: argument).  Activity lists jobs, so ``expired`` means what it means for a job.
INPUT_ALIASES: Mapping[StateAlias, LifecycleState] = {
    StateAlias.WAITING_FOR_OPERATOR: LifecycleState.NEEDS_OPERATOR,
    StateAlias.CANCELLING: LifecycleState.OBSERVING,
    StateAlias.WAITING: LifecycleState.OBSERVING,
    StateAlias.PARTIAL: LifecycleState.BACKOFF,
    StateAlias.EXPIRED: LifecycleState.FAILED,
}

#: The core states a row can be in once it ended.
TERMINAL_LIFECYCLE_STATES: frozenset[LifecycleState] = frozenset(
    {
        LifecycleState.SUCCEEDED,
        LifecycleState.FAILED,
        LifecycleState.CANCELLED,
        LifecycleState.SUPERSEDED,
    }
)


#: A kind of the generic job table that spells a word differently from the rest of
#: the table.  A recipe update batch ends ``partial`` when some of its children
#: succeeded and some failed: a definite failed outcome, not a retry (which is what
#: ``partial`` means to the image-availability jobs that share the table).
JOB_KIND_ALIASES: Mapping[str, Mapping[StateAlias, AdoptedState]] = {
    "recipe.cache.update.v2": {
        StateAlias.PARTIAL: AdoptedState(LifecycleState.FAILED),
    },
}


def _aliases(
    subject: LifecycleSubject, kind: str | None
) -> Mapping[StateAlias, AdoptedState]:
    rows = STATE_ALIASES.get(subject, {})
    if subject is LifecycleSubject.JOB and kind in JOB_KIND_ALIASES:
        return {**rows, **JOB_KIND_ALIASES[kind]}
    return rows


def adopt_state(
    subject: LifecycleSubject, stored: str, kind: str | None = None
) -> AdoptedState | None:
    """The core meaning of a stored ``state`` word, or ``None`` for a foreign word.

    Every reader of a stored lifecycle state goes through this one function: a
    word of the core vocabulary is itself, a retired spelling is adopted by the
    subject's alias row, and a word the subject keeps on purpose outside the
    vocabulary (``draft``) is ``None`` and read by its owner.
    """

    try:
        return AdoptedState(LifecycleState(stored))
    except ValueError:
        pass
    try:
        alias = StateAlias(stored)
    except ValueError:
        return None
    return _aliases(subject, kind).get(alias)


#: The states of a row that has not ended.
LIVE_LIFECYCLE_STATES: frozenset[LifecycleState] = frozenset(LifecycleState) - (
    TERMINAL_LIFECYCLE_STATES
)


def stored_words(
    subject: LifecycleSubject,
    states: Iterable[LifecycleState],
    kind: str | None = None,
) -> tuple[str, ...]:
    """Every word a row of ``subject`` may carry for any of ``states``.

    The core words themselves, then the retired spellings that adopt into them.
    A query that selects rows by state uses this, so a row written before the
    rename is found as well as one written after it, and nothing spells a word.
    ``kind`` names the kind of a generic job when the query is scoped to one, whose
    own spelling of a word may differ (see :data:`JOB_KIND_ALIASES`).
    """

    wanted = frozenset(states)
    words = [state.value for state in LifecycleState if state in wanted]
    words.extend(
        alias.value
        for alias, adopted in _aliases(subject, kind).items()
        if adopted.state in wanted
    )
    return tuple(words)


def live_words(subject: LifecycleSubject) -> tuple[str, ...]:
    """Every stored word of a row of ``subject`` that has not ended."""

    return stored_words(subject, LIVE_LIFECYCLE_STATES)


def is_state(
    subject: LifecycleSubject,
    stored: str | None,
    *states: LifecycleState,
    kind: str | None = None,
) -> bool:
    """Whether a stored word means one of ``states`` (adopting an old spelling)."""

    if stored is None:
        return False
    adopted = adopt_state(subject, stored, kind)
    return adopted is not None and adopted.state in states


def is_live(subject: LifecycleSubject, stored: str | None) -> bool:
    """Whether a stored word means a row that has not ended."""

    if stored is None:
        return False
    adopted = adopt_state(subject, stored)
    return adopted is not None and adopted.state in LIVE_LIFECYCLE_STATES


def check_words(
    subject: LifecycleSubject, states: Iterable[LifecycleState]
) -> tuple[str, ...]:
    """The words a subject's CHECK constraint admits: its states and their aliases."""

    return stored_words(subject, states)


def state_adopter(
    subject: LifecycleSubject, kind: str | None = None
) -> Callable[[Any], Any]:
    """A pydantic ``BeforeValidator`` that adopts a retired spelling of ``subject``.

    A contract model that carries a stored state (a persisted document, an API
    view built from a row) validates a row written before the rename as the
    word it means now; anything else passes through for the field to judge.
    ``kind`` scopes the adoption to one kind of generic job (see
    :data:`JOB_KIND_ALIASES`).
    """

    def adopt(value: Any) -> Any:
        if isinstance(value, str):
            adopted = adopt_state(subject, value, kind)
            if adopted is not None:
                return adopted.state
        return value

    return adopt


def input_state(word: str) -> LifecycleState | None:
    """A state named by a caller: a core word, or a retired spelling (one release)."""

    try:
        return LifecycleState(word)
    except ValueError:
        pass
    try:
        return INPUT_ALIASES[StateAlias(word)]
    except (ValueError, KeyError):
        return None


class StateWriteKind(WireEnum):
    """The shapes of a lifecycle state write the writers ratchet recognises."""

    ATTRIBUTE = "attribute"
    DICT_ITEM = "dict-item"
    BULK_UPDATE = "bulk-update"
    CONSTRUCTOR = "constructor"
    HELPER_CALL = "helper-call"


class MigrationStep(WireEnum):
    """The migration steps of the blocker audit (section 5.6) that retire a writer."""

    STEP_2 = "step-2"
    STEP_3 = "step-3"
    STEP_4 = "step-4"
    STEP_5 = "step-5"
    STEP_6 = "step-6"
    STEP_7 = "step-7"


class ErrorCategory(WireEnum):
    """The only three things a lifecycle adapter may raise or report.

    ``security-refusal`` and ``invalid-request`` are decided at submit time and
    fail closed.  Everything else is ``unknown``: it is observed and reconciled,
    never parked.  The blocker allowlist's ``already-retried`` and
    ``bookkeeping-debt`` families are both ``unknown`` (see
    :func:`error_category_of`).
    """

    SECURITY_REFUSAL = "security-refusal"
    INVALID_REQUEST = "invalid-request"
    UNKNOWN = "unknown"


_BLOCKER_TO_ERROR = {
    BlockerCategory.SECURITY_EDGE: ErrorCategory.SECURITY_REFUSAL,
    BlockerCategory.INPUT_VALIDATION: ErrorCategory.INVALID_REQUEST,
    BlockerCategory.ALREADY_RETRIED: ErrorCategory.UNKNOWN,
    BlockerCategory.BOOKKEEPING_DEBT: ErrorCategory.UNKNOWN,
}


def error_category_of(category: BlockerCategory | str) -> ErrorCategory:
    """The contract error category of a blocker allowlist category."""

    return _BLOCKER_TO_ERROR[BlockerCategory(category)]


class WaitReason(WireEnum):
    """Typed reason codes for an effect that cannot be confirmed (the *unknown* kind).

    Each code names the one fact the executor could not establish.  The free
    text of a report is for people; the Controller decides on this code.
    """

    OPERATION_NOT_ENABLED = "operation-not-enabled"
    UPGRADE_AWAITING_IDENTITY = "agent-upgrade-awaiting-identity"
    AGENT_RESTART_INTERRUPTED = "agent-restart-interrupted"
    STOP_UNCONFIRMED = "stop-unconfirmed"
    CLEANUP_UNCONFIRMED = "cleanup-unconfirmed"
    STOP_METADATA_UNCONFIRMED = "stop-metadata-unconfirmed"
    RETAINED_IDENTITY_MISMATCH = "retained-identity-mismatch"
    MODEL_CUSTODY_UNCONFIRMED = "model-custody-unconfirmed"
    RUNTIME_EFFECT_UNCONFIRMED = "runtime-effect-unconfirmed"
    JOB_STOP_UNCONFIRMED = "job-stop-unconfirmed"
    JOB_STATE_UNCERTAIN = "job-state-uncertain"
    LEASE_LAPSED = "lease-lapsed"
    REPORT_UNCERTAIN = "report-uncertain"
    OBSERVATION_UNAVAILABLE = "observation-unavailable"
    RECEIPT_MISSING = "receipt-missing"
    STALE_PLAN = "stale-plan"
    SCOPE_CHANGED = "scope-changed"
    LEGACY_UNCLASSIFIED = "legacy-unclassified"


class InvalidRequestReason(WireEnum):
    """Closed reason codes of an invalid request (submit-time input validation)."""

    MALFORMED = "malformed"
    OUT_OF_RANGE = "out-of-range"
    LIMIT_EXCEEDED = "limit-exceeded"
    UNKNOWN_FIELD = "unknown-field"
    INCOMPLETE = "incomplete"
    IMMUTABLE = "immutable"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"
    NOT_FOUND = "not-found"
    NOT_READY = "not-ready"
    SUPERSEDED = "superseded"
    UNSUPPORTED = "unsupported"


class SecurityRefusalReason(WireEnum):
    """Closed reason codes of a security refusal: a real security boundary.

    Authentication and authorization, identity and certificate expiry, node
    revocation, enrollment, signed package metadata, host-helper authority,
    tombstone fencing, credential denial and the digest-bound destructive-effect
    checks of Run/Switch.  ``failure_classification`` derives its code set from
    this enum, so the Controller and the contract cannot disagree.
    """

    HTTP_401 = "401"
    HTTP_403 = "403"
    AGENT_CERTIFICATE_ROTATION_CONFLICT = "agent.certificate.rotation.conflict"
    AGENT_ENROLLMENT_SUBMIT_REJECTED = "agent.enrollment.submit.rejected"
    AGENT_IDENTITY_MISMATCH = "agent.identity_mismatch"
    AGENT_TOMBSTONE_FENCED = "agent.tombstone_fenced"
    CATALOG_AUTHENTICATION_REQUIRED = "catalog.authentication_required"
    CONTROLLER_AUTHENTICATION_REQUIRED = "controller.authentication_required"
    CONTROLLER_FLEET_ENROLLMENT_DENIED = "controller.fleet.enrollment_denied"
    CONTROLLER_REQUEST_REJECTED = "controller.request_rejected"
    DIGEST_MISMATCH = "digest_verification_failed"
    DISTRIBUTION_REVOKED = "distribution.revoked"
    FORBIDDEN = "forbidden"
    GRANT_INVALID = "grant_invalid"
    GRANT_NODE_MISMATCH = "grant_node_mismatch"
    GRANT_UNAUTHORIZED = "grant_unauthorized"
    HELPER_AUTHORIZATION_INVALID = "helper.authorization_invalid"
    HELPER_GRANT_INVALID = "helper_grant_invalid"
    HELPER_GRANT_NODE_MISMATCH = "helper_grant_node_mismatch"
    HELPER_GRANT_UNAUTHORIZED = "helper_grant_unauthorized"
    HELPER_OPERATION_INVALID_ARTIFACT = "helper_operation_invalid_artifact"
    HELPER_PEER_IDENTITY_INVALID = "helper_peer_identity_invalid"
    HELPER_REQUEST_INSTALLATION_IDENTITY_INVALID = (
        "helper_request_installation_identity_invalid"
    )
    HELPER_REQUEST_PLAN_BINDING_INVALID = "helper_request_plan_binding_invalid"
    HELPER_REQUEST_REPLAYED = "helper_request_replayed"
    HELPER_RUNTIME_IMAGE_IDENTITY_INVALID = "helper_runtime_image_identity_invalid"
    HOST_HELPER_AUTHORITY_DENIED = "host_helper.authority_denied"
    LOCAL_IDENTITY_EXPIRED = "local.identity_expired"
    LOCAL_IDENTITY_FAILED = "local.identity_failed"
    MODEL_CACHE_CREDENTIALS_DENIED = "model_cache.credentials_denied"
    MODEL_CACHE_CREDENTIALS_INVALID = "model_cache.credentials_invalid"
    MODEL_CACHE_SOURCE_ACCESS_DENIED = "model_cache.source_access_denied"
    OPERATION_INVALID_ARTIFACT = "operation_invalid_artifact"
    PEER_IDENTITY_INVALID = "peer_identity_invalid"
    PERMISSION_DENIED = "permission_denied"
    RECIPE_UPDATE_AUTHORITY_DENIED = "recipe_update.authority_denied"
    REQUEST_REPLAYED = "request_replayed"
    RUN_SWITCH_ARTIFACT_DIGEST_VERIFICATION_FAILED = (
        "run-switch.artifact-digest-verification-failed"
    )
    RUN_SWITCH_CLEANUP_NAS_EVICTION_FORBIDDEN = (
        "run-switch.cleanup-nas-eviction-forbidden"
    )
    RUN_SWITCH_CLEANUP_RECLAIMED_DIGEST_NOT_PLANNED = (
        "run-switch.cleanup-reclaimed-digest-not-planned"
    )
    RUN_SWITCH_RUNTIME_IMAGE_PREPARATION_DIGEST_MISMATCH = (
        "run-switch.runtime-image-preparation-digest-mismatch"
    )
    RUNTIME_IMAGE_AUTHORIZATION_INVALID = "runtime_image.authorization_invalid"
    RUNTIME_IMAGE_AUTHORIZATION_REVOKED = "runtime_image.authorization_revoked"
    RUNTIME_IMAGE_IDENTITY_INVALID = "runtime_image_identity_invalid"
    STALE_FENCE = "stale_fence"
    TUF_METADATA_INVALID = "tuf.metadata_invalid"
    TUF_SIGNATURE_INVALID = "tuf.signature_invalid"
    UNAUTHORIZED = "unauthorized"
    UNSAFE_PATH = "unsafe_path"


#: Suffixes of the same security families, so a new producer of an existing
#: boundary is classified without editing the enum.
SECURITY_REFUSAL_SUFFIXES: tuple[str, ...] = (
    ".authentication_required",
    ".authorization_invalid",
    ".authorization_revoked",
    ".authority_denied",
    ".enrollment_denied",
    ".identity_expired",
    ".node_revoked",
    ".permission_denied",
    ".signature_invalid",
    ".tombstone_fenced",
)


class FailureCode(WireEnum):
    """Closed codes of a definite failed outcome reported by the agent."""

    OPERATION_FAILED = "operation_failed"
    OPERATION_CANCELLED = "operation_cancelled"
    AGENT_UPGRADE_FAILED = "agent_upgrade_failed"
    ARTIFACT_DISTRIBUTION_FAILED = "artifact_distribution_failed"
    RECIPE_BUILD_FAILED = "recipe_build_failed"
    RECIPE_JOB_RUN_FAILED = "recipe_job_run_failed"
    RECIPE_INSTALL_FAILED = "recipe_install_failed"
    RECIPE_START_FAILED = "recipe_start_failed"
    RECIPE_STOP_FAILED = "recipe_stop_failed"
    RECIPE_UNINSTALL_FAILED = "recipe_uninstall_failed"
    RUNTIME_OBSERVATION_UNAVAILABLE = "runtime_observation_unavailable"
    INSTALLATION_RECONCILIATION_BUSY = "installation_reconciliation_busy"
    RECIPE_RECONCILIATION_DEPENDENCY_UNAVAILABLE = (
        "recipe_reconciliation_dependency_unavailable"
    )
    #: A container this agent cannot prove it owns occupies the exact name a start
    #: needs. It is left untouched; the start waits for the name to be free.
    RETAINED_CONTAINER_FOREIGN = "retained_container_foreign"


class RunAdmissionCode(WireEnum):
    """The typed codes a run admission names for a refusal, blocker or wait.

    ``capacity_busy`` is lock contention only.  Every other reason an admission
    must wait or is refused carries its own member, so a waiting operation shows
    the real cause.  The retryable blockers (a plan that may become admissible
    by itself) are a subset the Controller derives from these members.
    """

    PLAN_INVALID = "run.plan_invalid"
    PLAN_STALE = "run.plan_stale"
    DEPENDENCIES_STALE = "run.dependencies_stale"
    CAPACITY_BUSY = "run.capacity_busy"
    TARGET_MEMBERSHIP_CHANGED = "run.target_membership_changed"
    MAPPING_NOT_READY = "run.mapping_not_ready"
    INVENTORY_MISSING = "run.inventory_missing"
    STALE_INVENTORY = "run.stale_inventory"
    INSUFFICIENT_MEMORY = "run.insufficient_memory"
    PORT_OCCUPIED = "run.port_occupied"
    RENDEZVOUS_PORT_OCCUPIED = "run.rendezvous_port_occupied"
    UNRECONCILED_LOST_RANK = "run.unreconciled_lost_rank"
    NOT_INSTALLED = "run.not_installed"
    FABRIC_ADDRESS_MISSING = "run.fabric_address_missing"
    FABRIC_ADDRESS_DUPLICATE = "run.fabric_address_duplicate"


class ResourceBlockerCode(WireEnum):
    """The capacity-fit codes the resource planner gives a node that cannot fit.

    ``insufficient`` is the family prefix a run admission maps onto
    ``run.insufficient_memory``; the planner itself names the exact
    ``insufficient_capacity*`` member.
    """

    CAPACITY_UNKNOWN = "resource.capacity_unknown"
    INSUFFICIENT = "resource.insufficient"
    INSUFFICIENT_CAPACITY = "resource.insufficient_capacity"
    INSUFFICIENT_CAPACITY_AFTER_STOP = "resource.insufficient_capacity_after_stop"
    INSUFFICIENT_RESERVATION_BUDGET = "resource.insufficient_reservation_budget"
    RESIDENT_USAGE_UNKNOWN = "resource.resident_usage_unknown"


class LifecycleVocabulary(WireModel):
    """Carrier that publishes every vocabulary enum into the wire schema.

    The model is never sent: it exists so the schema exporter, the Rust
    generator and the OpenAPI/TypeScript generators emit each closed word set
    from this one module.
    """

    state: LifecycleState
    agent_result_state: AgentResultState
    effect: LifecycleEffect
    outcome_kind: OutcomeKind
    stop_outcome: StopOutcome
    event_kind: LifecycleEventKind
    operator_action: OperatorActionName
    operator_surface: OperatorSurface
    blocker_category: BlockerCategory
    wait_verdict: WaitVerdict
    lifecycle_subject: LifecycleSubject
    state_alias: StateAlias
    observation_cause: ObservationCause
    artifact_preparation: ArtifactPreparation
    state_write_kind: StateWriteKind
    migration_step: MigrationStep
    error_category: ErrorCategory
    wait_reason: WaitReason
    invalid_request_reason: InvalidRequestReason
    security_refusal_reason: SecurityRefusalReason
    failure_code: FailureCode
    run_admission_code: RunAdmissionCode
    resource_blocker_code: ResourceBlockerCode
    installation_state: InstallationState
    installation_node_state: InstallationNodeState
    distribution_assignment_state: DistributionAssignmentState
    run_state: RunState
    route_state: RouteState
    route_publication_state: RoutePublicationState
    certificate_state: CertificateState
    enrollment_grant_state: EnrollmentGrantState
    model_file_state: ModelFileState
    catalog_sync_state: CatalogSyncState
    reservation_state: ReservationState
    gateway_route_state: GatewayRouteState
    desired_assignment_state: DesiredAssignmentState
    endpoint_state: EndpointState
    observed_assignment_state: ObservedAssignmentState
    asset_availability: AssetAvailability
    placement_install_state: PlacementInstallState
    placement_load_state: PlacementLoadState
    model_cache_operator_status: ModelCacheOperatorStatus
    progress_phase: ProgressPhase
    failure_stage: FailureStage
    host_helper_response_status: HostHelperResponseStatus


__all__ = [
    "ATTEMPT_ALIAS_CAUSE",
    "INPUT_ALIASES",
    "JOB_KIND_ALIASES",
    "LEGACY_WAIT_STATE",
    "LIVE_LIFECYCLE_STATES",
    "SECURITY_REFUSAL_SUFFIXES",
    "STATE_ALIASES",
    "TERMINAL_LIFECYCLE_STATES",
    "AdoptedState",
    "AgentResultState",
    "ArtifactPreparation",
    "BlockerCategory",
    "ErrorCategory",
    "FailureCode",
    "InvalidRequestReason",
    "LifecycleEffect",
    "LifecycleEventKind",
    "LifecycleState",
    "LifecycleSubject",
    "LifecycleVocabulary",
    "MigrationStep",
    "ObservationCause",
    "OperatorActionName",
    "OperatorSurface",
    "OutcomeKind",
    "ResourceBlockerCode",
    "RunAdmissionCode",
    "SecurityRefusalReason",
    "StateAlias",
    "StateWriteKind",
    "StopOutcome",
    "WaitReason",
    "WaitVerdict",
    "adopt_state",
    "check_words",
    "error_category_of",
    "input_state",
    "is_live",
    "is_state",
    "legacy_observation_cause",
    "legacy_preparation",
    "live_words",
    "state_adopter",
    "stored_words",
]
