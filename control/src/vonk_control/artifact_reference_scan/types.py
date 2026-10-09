"""Artifact reference scan: types."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from vonk_agent_protocol import (
    LifecycleState,
    RunState,
)

from .. import job_states
from ..artifact_lifecycle import (
    ArtifactIdentity,
)
from ..machine_states import INSTALLATION_ACTIVE

_ACTIVE_PROFILE_APPLICATIONS = job_states.words(
    LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.NEEDS_OPERATOR
)


_ACTIVE_RUN_SWITCH_JOBS = job_states.words(
    LifecycleState.QUEUED,
    LifecycleState.RUNNING,
    LifecycleState.BACKOFF,
    LifecycleState.NEEDS_OPERATOR,
)


_ACTIVE_INSTALLATIONS = INSTALLATION_ACTIVE


_ACTIVE_RUNS = (RunState.STARTING, RunState.RUNNING, RunState.STOPPING)


_ACTIVE_ARTIFACT_JOBS = job_states.words(
    LifecycleState.QUEUED,
    LifecycleState.RUNNING,
    LifecycleState.BACKOFF,
    LifecycleState.NEEDS_OPERATOR,
    LifecycleState.OBSERVING,
)


@dataclass(frozen=True, slots=True)
class ArtifactReferenceFinding:
    """One validated existing owner of an exact managed artifact identity."""

    asset: ArtifactIdentity
    owner_kind: str
    owner_id: str
    state: str
    classification: Literal["saved-reference", "active-work"]
    detail: str
    reason: str


def _finding(
    kind: Literal["model-set", "model-object", "runtime-image"],
    digest: str,
    *,
    owner_kind: str,
    owner_id: str,
    state: str,
    classification: Literal["saved-reference", "active-work"],
    detail: str,
    reason: str,
) -> ArtifactReferenceFinding:
    return ArtifactReferenceFinding(
        asset=ArtifactIdentity(kind, digest),
        owner_kind=owner_kind,
        owner_id=owner_id,
        state=state,
        classification=classification,
        detail=detail,
        reason=reason,
    )


def _reason_projection(
    findings: Mapping[str, tuple[ArtifactReferenceFinding, ...]],
) -> dict[str, tuple[str, ...]]:
    return {
        digest: tuple(
            sorted(
                {
                    finding.reason
                    for finding in values
                    if finding.owner_kind != "fleet-profile"
                }
            )
        )
        for digest, values in findings.items()
    }
