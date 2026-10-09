"""Agent upgrades: errors."""

from __future__ import annotations

import re

from vonk_agent_protocol import (
    InvalidRequestError,
    InvalidRequestReason,
    SecurityRefusalError,
    SecurityRefusalReason,
    UnknownOutcomeError,
    WaitReason,
)

_NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")


def _conflict_detail(detail: str, spark_id: str | None) -> str:
    if spark_id is None:
        return detail
    return (
        f"Spark {spark_id} {detail}"
        if _NODE_ID.fullmatch(spark_id) is not None
        else "agent upgrade target is invalid"
    )


class AgentUpgradeConflict(RuntimeError):
    """An agent upgrade plan is invalid, stale, or not safely executable.

    The refusal text is surfaced to an operator, so a Spark is named only by its
    canonical identifier.  Any other ``spark_id`` is a stored row value and is
    replaced rather than echoed.  Raise one of the categorized subclasses.
    """

    def __init__(self, detail: str, *, spark_id: str | None = None) -> None:
        super().__init__(_conflict_detail(detail, spark_id))


class AgentUpgradeInvalid(InvalidRequestError, AgentUpgradeConflict):
    """The request, plan or manifest is malformed or conflicts with the request."""

    def __init__(
        self,
        detail: str,
        *,
        spark_id: str | None = None,
        reason: InvalidRequestReason | None = None,
    ) -> None:
        InvalidRequestError.__init__(
            self, _conflict_detail(detail, spark_id), reason=reason
        )


#: Pause before the second and third release fetch (seconds): a release is
#: published in a few steps, so a half-published channel settles within them.
_RELEASE_PAUSES = (0.5, 2.0)


class AgentUpgradeUnavailable(UnknownOutcomeError, AgentUpgradeConflict):
    """Release or stored evidence is unavailable or moved: observe and retry."""

    def __init__(
        self,
        detail: str,
        *,
        spark_id: str | None = None,
        reason: WaitReason | None = None,
    ) -> None:
        UnknownOutcomeError.__init__(
            self, _conflict_detail(detail, spark_id), reason=reason
        )


class AgentUpgradeRefused(SecurityRefusalError, AgentUpgradeConflict):
    """The release evidence fails its identity or signature checks."""

    def __init__(
        self,
        detail: str,
        *,
        spark_id: str | None = None,
        reason: SecurityRefusalReason | None = None,
    ) -> None:
        SecurityRefusalError.__init__(
            self, _conflict_detail(detail, spark_id), reason=reason
        )


class AgentUpgradeRetryLater(UnknownOutcomeError, AgentUpgradeConflict):
    """The release channel did not answer consistently (a release is being
    published).  Nothing was persisted; the caller asks again and converges."""

    def __init__(
        self,
        detail: str,
        *,
        spark_id: str | None = None,
        reason: WaitReason | None = WaitReason.OBSERVATION_UNAVAILABLE,
    ) -> None:
        UnknownOutcomeError.__init__(
            self, _conflict_detail(detail, spark_id), reason=reason
        )
