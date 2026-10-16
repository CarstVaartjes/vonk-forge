"""The one failure-disposition table every client reads.

The Controller (``failure_classification.disposition``), the Spark agent
(``ClientError::disposition`` in Rust) and the CLI (its generated table) decide
what a failure does to its work from these rows, so a status, a failure kind or
a wait reason cannot mean "retry" on one side and "fail" on the other.

* ``WAIT``: the cause clears by itself (a busy owner, a dependency that is not
  there yet, a resource prerequisite, a peer asking to come back later).  The
  work stays, shows what it waits for, and never spends a retry.
* ``OBSERVE``: an effect may or may not have happened.  It is re-observed by
  its identity; a recurring uncertain effect is the only thing that spends the
  retry count.  A spent count ends the spending, not the work: an irreversible
  effect still unknown is lost (kept, observed at a bounded rate) until it is
  observed, reported or stopped.
* ``FAIL``: the same input fails the same way.  The work ends on the first
  occurrence and keeps the cause as evidence.
* ``SECURITY``: a real security edge refused it; terminal by design.
"""

from __future__ import annotations

from types import MappingProxyType

from .agent_words import Disposition
from .contracts import AgentFailureKind
from .lifecycle_vocabulary import WaitReason

_FAIL = Disposition.FAIL
_WAIT = Disposition.WAIT
_OBSERVE = Disposition.OBSERVE
_SECURITY = Disposition.SECURITY

#: What an agent's typed failure kind does to the order it ended.
FAILURE_KIND_DISPOSITION = MappingProxyType(
    {
        AgentFailureKind.TEMPORARY_DEPENDENCY: _WAIT,
        AgentFailureKind.RESOURCE_PREREQUISITE: _WAIT,
        AgentFailureKind.UNCERTAIN_EFFECT: _OBSERVE,
        AgentFailureKind.INVALID_AUTHORITY: _SECURITY,
        AgentFailureKind.INVALID_CONTRACT: _FAIL,
        AgentFailureKind.INTEGRITY_FAILURE: _FAIL,
    }
)

#: What an unknown outcome does.  Evidence that is unavailable, an effect nobody
#: could confirm, a plan or scope that changed: each is observed again and
#: spends the bounded retry count, so it is never retried blindly or forever;
#: an unknown irreversible effect is then lost, never failed.  Only a
#: named dependency (a feature the agent does not enable yet, an upgraded
#: agent's identity) is a wait; a busy owner declares its own ``WAIT``.
WAIT_REASON_DISPOSITION = MappingProxyType(
    {
        WaitReason.OPERATION_NOT_ENABLED: _WAIT,
        WaitReason.UPGRADE_AWAITING_IDENTITY: _WAIT,
        WaitReason.AGENT_RESTART_INTERRUPTED: _OBSERVE,
        WaitReason.STOP_UNCONFIRMED: _OBSERVE,
        WaitReason.CLEANUP_UNCONFIRMED: _OBSERVE,
        WaitReason.STOP_METADATA_UNCONFIRMED: _OBSERVE,
        WaitReason.RETAINED_IDENTITY_MISMATCH: _OBSERVE,
        WaitReason.MODEL_CUSTODY_UNCONFIRMED: _OBSERVE,
        WaitReason.RUNTIME_EFFECT_UNCONFIRMED: _OBSERVE,
        WaitReason.JOB_STOP_UNCONFIRMED: _OBSERVE,
        WaitReason.JOB_STATE_UNCERTAIN: _OBSERVE,
        WaitReason.LEASE_LAPSED: _OBSERVE,
        WaitReason.REPORT_UNCERTAIN: _OBSERVE,
        WaitReason.OBSERVATION_UNAVAILABLE: _OBSERVE,
        WaitReason.RECEIPT_MISSING: _OBSERVE,
        WaitReason.STALE_PLAN: _OBSERVE,
        WaitReason.SCOPE_CHANGED: _OBSERVE,
        WaitReason.LEGACY_UNCLASSIFIED: _OBSERVE,
    }
)

#: HTTP statuses, first match wins: ``(first, last, disposition)``.  A status
#: no row covers (a 1xx-3xx answer whose body could not be read) is observed.
HTTP_STATUS_DISPOSITIONS: tuple[tuple[int, int, Disposition], ...] = (
    (401, 401, _SECURITY),
    (403, 403, _SECURITY),
    # Too Many Requests: the peer asks for later.
    (429, 429, _WAIT),
    # Every other 4xx, a conflict included, is definite for this exact
    # request: its owner re-plans (a fresh request), never re-sends it.
    (400, 499, _FAIL),
    (500, 599, _WAIT),
)


def http_status_disposition(status: int) -> Disposition:
    """The one reading of an HTTP status, shared by every client."""

    return next(
        (
            verdict
            for first, last, verdict in HTTP_STATUS_DISPOSITIONS
            if first <= status <= last
        ),
        _OBSERVE,
    )


__all__ = [
    "FAILURE_KIND_DISPOSITION",
    "HTTP_STATUS_DISPOSITIONS",
    "WAIT_REASON_DISPOSITION",
    "Disposition",
    "http_status_disposition",
]
