"""The state words the CLI recognises, in the core vocabulary.

The CLI ships on its own and does not import the Controller's contract package, so
this is its one copy of the words (a test keeps it equal to the contract).  The
Controller names a wait for a person ``needs-operator``; a Controller older than the
rename still sends ``waiting-for-operator``, which the CLI accepts for one release.
"""

from __future__ import annotations

NEEDS_OPERATOR = "needs-operator"
LEGACY_NEEDS_OPERATOR = "waiting-for-operator"
#: A wait for a person, under either spelling.
OPERATOR_WAIT_STATES = frozenset({NEEDS_OPERATOR, LEGACY_NEEDS_OPERATOR})

# -- artifact jobs ---------------------------------------------------------------

#: The preparation stages of an artifact job that has not been submitted.
DRAFT = "draft"
READY = "ready"
#: The old spelling of "a cancel is under way" (now ``cancel_requested_at``).
LEGACY_CANCELLING = "cancelling"
ENDED_STATES = frozenset({"succeeded", "failed", "cancelled"})
#: Every lifecycle state an artifact job may report, and the old ones.
ARTIFACT_JOB_STATES = frozenset(
    {
        "queued",
        "running",
        "backoff",
        "observing",
        NEEDS_OPERATOR,
        LEGACY_NEEDS_OPERATOR,
        LEGACY_CANCELLING,
        *ENDED_STATES,
    }
)


def preparation(job: dict[str, object]) -> str | None:
    """``draft`` or ``ready`` while a job is prepared: its field, or an old state word."""

    stage = job.get("preparation")
    if isinstance(stage, str):
        return stage
    state = job.get("state")
    return state if isinstance(state, str) and state in {DRAFT, READY} else None


def cancel_pending(job: dict[str, object]) -> bool:
    """A cancel was requested and the job has not ended (either spelling)."""

    state = job.get("state")
    if state == LEGACY_CANCELLING:
        return True
    return job.get("cancel_requested_at") is not None and state not in ENDED_STATES


def lifecycle_state(job: dict[str, object]) -> object:
    """The job's state for messages: its stage while preparing, else its state."""

    return preparation(job) or job.get("state")


#: Artifact-job states a CLI user reconnects to (the job is still in flight).
ARTIFACT_JOB_IN_FLIGHT = frozenset(
    {"queued", "running", "backoff", "observing", LEGACY_CANCELLING}
)

# -- operations ------------------------------------------------------------------

#: What an older Controller called an update batch that ended with some children
#: done (now ``failed`` with ``partial`` set) and a cache operation that retries
#: (now ``backoff``).
LEGACY_PARTIAL = "partial"
#: An accepted cancel: still being driven (``observing``, or the old
#: ``cancelling``) or already ended.
CANCEL_ACCEPTED_STATES = frozenset({"observing", LEGACY_CANCELLING, "cancelled"})
