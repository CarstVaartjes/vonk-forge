"""The one table that turns a progress phase or a wait into plain words.

Every client reads it from a generated file (``cli_states_generated.py`` for the
CLI, ``vocabulary.generated.ts`` for the web), so no client spells a phase of its
own.  A phase this table does not know is shown as sent.
"""

from __future__ import annotations

from collections.abc import Mapping

from vonk_agent_protocol.agent_words import (
    TRANSFER_PHASES,
    ProfileChildPhase,
    ProfileReportedPhase,
    ProgressPhase,
)
from vonk_agent_protocol.lifecycle_vocabulary import WaitReason

_COPYING = "Copying model files to the Sparks"
_DOWNLOADING = "Downloading the model"
_IMAGE = "Downloading the container image"
_BUILDING = "Building the container image"
_INSTALLING = "Installing the runtime"
_STARTING = "Starting on the Sparks"
_VERIFYING = "Checking the result"
_PREPARING = "Preparing the load"
_STOPPING = "Stopping the running workload"
_CLEANING = "Cleaning up"
_QUEUED = "Waiting in the queue"
_REMOVING = "Removing the old install"

PHASE_WORDS: Mapping[str, str] = {
    ProgressPhase.TRANSFER.value: _COPYING,
    ProgressPhase.COPYING.value: _COPYING,
    ProfileChildPhase.TARGET_COPY.value: _COPYING,
    ProgressPhase.UPLOADING.value: "Uploading files",
    ProgressPhase.DOWNLOADING.value: _DOWNLOADING,
    ProgressPhase.MODEL_DOWNLOAD.value: _DOWNLOADING,
    ProfileChildPhase.CONTAINER_DOWNLOAD.value: _IMAGE,
    ProgressPhase.PULLING.value: _IMAGE,
    ProfileChildPhase.CONTAINER_BUILD.value: _BUILDING,
    ProgressPhase.BUILDING.value: _BUILDING,
    ProfileChildPhase.RUNTIME_INSTALL.value: _INSTALLING,
    ProgressPhase.INSTALLING.value: _INSTALLING,
    ProgressPhase.RECONCILING_INSTALLATION.value: "Checking the installation",
    ProfileChildPhase.START.value: _STARTING,
    ProgressPhase.STARTING.value: _STARTING,
    ProfileChildPhase.FINAL_VERIFY.value: _VERIFYING,
    ProfileReportedPhase.FINAL_VERIFY.value: _VERIFYING,
    ProfileChildPhase.VERIFY.value: _VERIFYING,
    ProgressPhase.VERIFYING.value: _VERIFYING,
    ProfileChildPhase.PREPARE.value: _PREPARING,
    ProgressPhase.PREPARING.value: _PREPARING,
    ProgressPhase.FINALIZING.value: "Finishing up",
    ProfileChildPhase.STOP.value: _STOPPING,
    ProgressPhase.STOPPING.value: _STOPPING,
    ProfileChildPhase.UNINSTALL.value: _REMOVING,
    ProgressPhase.UNINSTALLING.value: _REMOVING,
    ProfileChildPhase.CLEANUP.value: _CLEANING,
    ProgressPhase.RECLAIMING.value: _CLEANING,
    ProgressPhase.QUEUED.value: _QUEUED,
    ProgressPhase.PENDING.value: _QUEUED,
    ProgressPhase.WAITING.value: _QUEUED,
    ProgressPhase.UPDATING.value: "Updating",
    ProgressPhase.EXECUTING.value: "Working",
    ProgressPhase.COMPLETED.value: "Finished",
    ProgressPhase.FAILED.value: "Failed",
}

#: A wait in plain words.  ``DEPENDENCY_PENDING`` has none here: it reads as the
#: step the work waits behind, so a client resolves it through ``PHASE_WORDS``.
WAIT_WORDS: Mapping[str, str] = {
    WaitReason.SPARK_BUSY.value: "the Spark's current operation to finish",
}

#: Phases in which bytes move, as a profile load or an operation names them: a
#: bar, a rate and an ETA mean something there.
BYTE_PHASES: tuple[str, ...] = tuple(
    sorted(
        {phase.value for phase in TRANSFER_PHASES}
        | {
            ProgressPhase.MODEL_DOWNLOAD.value,
            ProgressPhase.PULLING.value,
            ProfileChildPhase.TARGET_COPY.value,
            ProfileChildPhase.CONTAINER_DOWNLOAD.value,
        }
    )
)
