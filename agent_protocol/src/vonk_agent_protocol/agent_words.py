"""The words the Spark agent and the Controller speak about how far work has got.

Three closed word sets, published through ``LifecycleVocabulary`` and generated
into Rust, OpenAPI and TypeScript, so neither side spells one by hand:

* :class:`ProgressPhase` is what ``OperationProgress.phase`` and
  ``OperationMemberProgress.phase`` say the work is doing.  The agent and the
  Controller both write members of this one set (the agent while it downloads,
  builds or installs; the Controller while it projects a cache, a distribution or
  an update).  Where both already spelled a word the same way it is kept.
* :class:`FailureStage` is the step a failed or unconfirmed operation stopped at
  (``OutcomeEvidence.stage``): the agent's own, stable, secret-free step names.
* :class:`HostHelperResponseStatus` is the verdict word of a privileged-helper
  reply.

The wire fields stay strings: an older agent sends free text (``download``,
``verify``) and a newer one may send a word this release does not know.  The
Controller reads a phase through :func:`adopt_progress_phase`, the one adapter
that maps a retired spelling to its member; a word no member spells reads as
``None`` and is shown as reported, never refused.
"""

from __future__ import annotations

from collections.abc import Mapping

from .wire_model import WireEnum


class ProgressPhase(WireEnum):
    """What an operation is doing, as the measured progress names it."""

    QUEUED = "queued"
    PENDING = "pending"
    WAITING = "waiting"
    PREPARING = "preparing"
    DOWNLOADING = "downloading"
    MODEL_DOWNLOAD = "model-download"
    VERIFYING = "verifying"
    FINALIZING = "finalizing"
    BUILDING = "building"
    PULLING = "pulling"
    TRANSFER = "transfer"
    COPYING = "copying"
    UPLOADING = "uploading"
    INSTALLING = "installing"
    RECONCILING_INSTALLATION = "reconciling-installation"
    STARTING = "starting"
    STOPPING = "stopping"
    UNINSTALLING = "uninstalling"
    RECLAIMING = "reclaiming"
    UPDATING = "updating"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"


#: Phases in which bytes move: a rate and an ETA mean something.  ``model-download``
#: is the Controller's name for the child that fetches a model, not a transfer the
#: measurement rates.
TRANSFER_PHASES: frozenset[ProgressPhase] = frozenset(
    {
        ProgressPhase.DOWNLOADING,
        ProgressPhase.TRANSFER,
        ProgressPhase.COPYING,
        ProgressPhase.UPLOADING,
    }
)

#: Phases in which the work is not running yet and no rate is expected.
WAITING_PHASES: frozenset[ProgressPhase] = frozenset(
    {ProgressPhase.QUEUED, ProgressPhase.PENDING, ProgressPhase.WAITING}
)

#: The spellings an older agent or Controller wrote for a phase that a member now
#: names.  This is the one legacy adapter for a phase; nothing writes these any
#: more.
RETIRED_PROGRESS_PHASE_SPELLINGS: Mapping[str, ProgressPhase] = {
    "download": ProgressPhase.DOWNLOADING,
    "verify": ProgressPhase.VERIFYING,
    "cleanup": ProgressPhase.RECLAIMING,
    "prepare": ProgressPhase.PREPARING,
    "upload": ProgressPhase.UPLOADING,
    "transferring": ProgressPhase.TRANSFER,
    "distribution": ProgressPhase.TRANSFER,
    "update": ProgressPhase.UPDATING,
    "complete": ProgressPhase.COMPLETED,
}


def adopt_progress_phase(word: str) -> ProgressPhase | None:
    """The member that names a phase read from an agent or a stored row.

    A current spelling and a retired one both map to their member.  A word this
    release does not know (a newer agent's, or free text) is ``None``: the phase
    is shown as reported and counts as neither a transfer nor a wait.
    """

    try:
        return ProgressPhase(word)
    except ValueError:
        return RETIRED_PROGRESS_PHASE_SPELLINGS.get(word)


class FailureStage(WireEnum):
    """The step an operation stopped at: a short, stable, secret-free word."""

    AGENT_RESTART = "agent-restart"
    AGENT_UPGRADE_INSTALLED = "agent-upgrade-installed"
    ARTIFACT_DISTRIBUTION = "artifact-distribution"
    BASE_IMAGE_IMPORT = "base-image-import"
    BOUNDED_BUILD_PROCESS = "bounded-build-process"
    EGRESS_ADDRESS = "egress-address"
    EGRESS_IMAGE_IMPORT = "egress-image-import"
    EGRESS_NETWORK_CREATE = "egress-network-create"
    EGRESS_READINESS = "egress-readiness"
    EGRESS_SERVICE_START = "egress-service-start"
    HELPER_RUNTIME_RECONCILIATION = "helper-runtime-reconciliation"
    HELPER_RUNTIME_RECONCILIATION_LOCK = "helper-runtime-reconciliation-lock"
    IMAGE_BUILD = "image-build"
    IMAGE_UPLOAD = "image-upload"
    IMAGE_VERIFICATION = "image-verification"
    INSTALLATION_CHECKPOINT_STORAGE = "installation-checkpoint-storage"
    INSTALLATION_DIRECTORY = "installation-directory"
    INSTALLATION_METADATA = "installation-metadata"
    INSTALLATION_PATH = "installation-path"
    INSTALLATION_RECEIPT = "installation-receipt"
    INSTALLATION_RECONCILIATION_LOCK = "installation-reconciliation-lock"
    INSTALLATION_REMOVAL = "installation-removal"
    INSTALLATION_VALIDATION = "installation-validation"
    JOB_CANCEL_STOP = "job-cancel-stop"
    JOB_INPUTS = "job-inputs"
    JOB_STATE = "job-state"
    JOB_STOP = "job-stop"
    LIFECYCLE_METADATA = "lifecycle-metadata"
    MODEL_CUSTODY = "model-custody"
    MODEL_MATERIALIZATION = "model-materialization"
    OBSERVATION_IDENTITY = "observation-identity"
    OUTPUT_STORAGE = "output-storage"
    RETAINED_CONTAINER = "retained-container"
    RUN_STORAGE = "run-storage"
    RUNTIME_ADAPTER = "runtime-adapter"
    RUNTIME_CACHE = "runtime-cache"
    RUNTIME_CACHE_CLEANUP = "runtime-cache-cleanup"
    RUNTIME_METADATA = "runtime-metadata"
    RUNTIME_PROJECTION = "runtime-projection"
    SOURCE_BUNDLE_FETCH = "source-bundle-fetch"
    STOP = "stop"
    STOP_CLEANUP = "stop-cleanup"
    STOP_METADATA = "stop-metadata"
    STOP_PLAN = "stop-plan"
    UNKNOWN = "unknown"


class HostHelperResponseStatus(WireEnum):
    """The verdict a privileged-helper reply carries."""

    REJECTED = "rejected"
    PACKAGE_INSTALLED = "package-installed"
    PACKAGE_ACTIVATION_CONFIRMED = "package-activation-confirmed"
    CONTAINER_RUNTIME_REQUEST_EXECUTED = "container-runtime-request-executed"
    CONTAINER_RUNTIME_STOP_UNCERTAIN = "container-runtime-stop-uncertain"


__all__ = [
    "RETIRED_PROGRESS_PHASE_SPELLINGS",
    "TRANSFER_PHASES",
    "WAITING_PHASES",
    "FailureStage",
    "HostHelperResponseStatus",
    "ProgressPhase",
    "adopt_progress_phase",
]
