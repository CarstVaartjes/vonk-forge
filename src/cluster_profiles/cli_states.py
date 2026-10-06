"""The state words the CLI recognises, in the core vocabulary.

The CLI ships on its own and does not import the Controller's contract package, so
the words come from ``cli_states_generated``, generated from the contract by
``scripts/generate-python-vocabulary``; this module only composes them.  The
Controller names a wait for a person ``needs-operator``; a Controller older than the
rename still sends ``waiting-for-operator``, which the CLI accepts for one release.
"""

from __future__ import annotations

from .cli_states_generated import (
    BACKOFF,
    CANCELLED,
    DRAFT,
    ENDPOINT_EXPIRED,
    ENDPOINT_INSTALLED_ONLY,
    ENDPOINT_NOT_PUBLISHED_YET,
    ENDPOINT_UNAVAILABLE,
    ENDPOINT_WITHDRAWN,
    EXPIRED,
    FAILED,
    LEGACY_CANCELLING,
    LEGACY_NEEDS_OPERATOR,
    LEGACY_PARTIAL,
    NEEDS_OPERATOR,
    OBSERVING,
    PUBLISHED,
    QUEUED,
    READY,
    RUNNING,
    SUCCEEDED,
)

__all__ = [
    "ENDPOINT_EXPIRED",
    "ENDPOINT_INSTALLED_ONLY",
    "ENDPOINT_NOT_PUBLISHED_YET",
    "ENDPOINT_UNAVAILABLE",
    "ENDPOINT_WITHDRAWN",
    "LEGACY_CANCELLING",
    "LEGACY_NEEDS_OPERATOR",
    "LEGACY_PARTIAL",
    "NEEDS_OPERATOR",
    "PUBLISHED",
]

#: A wait for a person, under either spelling.
OPERATOR_WAIT_STATES = frozenset({NEEDS_OPERATOR, LEGACY_NEEDS_OPERATOR})

# -- artifact jobs ---------------------------------------------------------------

#: The preparation stages of an artifact job that has not been submitted.
#: The old spelling of "a cancel is under way" (now ``cancel_requested_at``).
ENDED_STATES = frozenset({SUCCEEDED, FAILED, CANCELLED})
#: Every lifecycle state an artifact job may report, and the old ones.
ARTIFACT_JOB_STATES = frozenset(
    {
        QUEUED,
        RUNNING,
        BACKOFF,
        OBSERVING,
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
    {QUEUED, RUNNING, BACKOFF, OBSERVING, LEGACY_CANCELLING}
)

# -- operations ------------------------------------------------------------------

#: What an older Controller called an update batch that ended with some children
#: done (now ``failed`` with ``partial`` set) and a cache operation that retries
#: (now ``backoff``).
#: The field of an operation that says some of it was done (a failed update).
PARTIAL_FIELD = "partial"
#: The states of a job a CLI user is told has failed (an expired job is over).
FAILED_JOB_STATES = frozenset({FAILED, EXPIRED})
#: An accepted cancel: still being driven (``observing``, or the old
#: ``cancelling``) or already ended.
CANCEL_ACCEPTED_STATES = frozenset({OBSERVING, LEGACY_CANCELLING, CANCELLED})

#: The word of a route (and of an endpoint) that is published to the gateway.  The
#: CLI compares it with what the Controller sends; the contract's ``RouteState`` and
#: ``EndpointState`` spell it, and a test keeps this copy equal to them.
#: The two endpoint words the CLI explains (see the contract's ``EndpointState``).
