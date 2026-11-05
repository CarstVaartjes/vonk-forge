"""The state words the CLI recognises, in the core vocabulary.

The CLI ships on its own and does not import the Controller's contract package, so
the words come from ``cli_states_generated``, generated from the contract by
``scripts/generate-python-vocabulary``; this module only composes them.
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
    FAILED,
    NEEDS_OPERATOR,
    OBSERVING,
    PUBLISHED,
    QUEUED,
    READY,
    RUNNING,
    SUCCEEDED,
)

__all__ = [
    "DRAFT",
    "ENDPOINT_EXPIRED",
    "ENDPOINT_INSTALLED_ONLY",
    "ENDPOINT_NOT_PUBLISHED_YET",
    "ENDPOINT_UNAVAILABLE",
    "ENDPOINT_WITHDRAWN",
    "NEEDS_OPERATOR",
    "PUBLISHED",
    "READY",
]

# -- artifact jobs ---------------------------------------------------------------

ENDED_STATES = frozenset({SUCCEEDED, FAILED, CANCELLED})
#: Every lifecycle state an artifact job may report.
ARTIFACT_JOB_STATES = frozenset(
    {QUEUED, RUNNING, BACKOFF, OBSERVING, NEEDS_OPERATOR, *ENDED_STATES}
)


def preparation(job: dict[str, object]) -> str | None:
    """``draft`` or ``ready`` while a job is prepared, else ``None``."""

    stage = job.get("preparation")
    return stage if isinstance(stage, str) else None


def cancel_pending(job: dict[str, object]) -> bool:
    """A cancel was requested and the job has not ended."""

    state = job.get("state")
    return job.get("cancel_requested_at") is not None and state not in ENDED_STATES


def lifecycle_state(job: dict[str, object]) -> object:
    """The job's state for messages: its stage while preparing, else its state."""

    return preparation(job) or job.get("state")


#: Artifact-job states a CLI user reconnects to (the job is still in flight).
ARTIFACT_JOB_IN_FLIGHT = frozenset({QUEUED, RUNNING, BACKOFF, OBSERVING})

# -- operations ------------------------------------------------------------------

#: The field of an operation that says some of it was done (a failed update).
PARTIAL_FIELD = "partial"
#: The states of a job a CLI user is told has failed.
FAILED_JOB_STATES = frozenset({FAILED})
#: An accepted cancel: still being driven (``observing``) or already ended.
CANCEL_ACCEPTED_STATES = frozenset({OBSERVING, CANCELLED})

#: The word of a route (and of an endpoint) that is published to the gateway.  The
#: CLI compares it with what the Controller sends; the contract's ``RouteState`` and
#: ``EndpointState`` spell it, and a test keeps this copy equal to them.
#: The two endpoint words the CLI explains (see the contract's ``EndpointState``).
