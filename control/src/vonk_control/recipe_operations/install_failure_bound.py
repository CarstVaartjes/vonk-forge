"""The bound on relaunching an install that keeps failing the same way.

Owner decision 2026-11-03: three identical definite failures of the same
identity end as a failure with the stored evidence; a content change (a new
plan digest, which covers the image digest) starts a fresh streak.  The streak
is read from the install Jobs themselves, newest first: a success, a
cancellation or a wait breaks it, so nothing is stored or reset.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    WAIT_REASON_DISPOSITION,
    AgentFailureResult,
    LifecycleState,
)
from vonk_agent_protocol import AgentOperation as WireAgentOperation

from ..failure_classification import is_definite
from ..models import Job
from ..recipe_lifecycle_contract import (
    LifecycleCodeFailureResult,
    RecipeOperationResult,
)
from .interfaces import RecipeOperationView

#: Identical definite failures of one plan digest after which it is not relaunched.
IDENTICAL_EXIT_BOUND = 3

_Signature = frozenset[tuple[str, str, str, str]]


def _signature(job: Job, result: object) -> _Signature | None:
    """What a failed install did, or ``None`` when any of it may still clear."""

    if job.state != LifecycleState.FAILED.value:
        return None
    if not isinstance(result, RecipeOperationResult) or not result.failed_nodes:
        return None
    signature: set[tuple[str, str, str, str]] = set()
    for node_id in result.failed_nodes:
        evidence = result.node_evidence.get(node_id)
        if isinstance(evidence, LifecycleCodeFailureResult):
            signature.add((node_id, "code", evidence.code, ""))
        elif isinstance(evidence, AgentFailureResult):
            if evidence.uncertain or (
                evidence.failure_kind is not None
                and not is_definite(evidence.failure_kind)
            ):
                return None
            if evidence.wait_reason is not None and not is_definite(
                WAIT_REASON_DISPOSITION[evidence.wait_reason]
            ):
                return None
            signature.add(
                (
                    node_id,
                    str(evidence.failure_kind or ""),
                    str(evidence.error_code or ""),
                    evidence.stage or "",
                )
            )
        else:
            return None
    return frozenset(signature)


def exhausted_install(
    session: Session,
    installation_id: str,
    plan_digest: str,
    view: Callable[..., RecipeOperationView],
) -> RecipeOperationView | None:
    """The newest failed install when the last attempts of this plan all failed
    the same definite way, else ``None`` (the install may be launched)."""

    recent = session.scalars(
        select(Job)
        .where(
            Job.kind == WireAgentOperation.RECIPE_INSTALL.value,
            Job.owner_id == installation_id,
            Job.payload["plan_digest"].as_string() == plan_digest,
        )
        .order_by(Job.updated_at.desc(), Job.id)
        .limit(IDENTICAL_EXIT_BOUND)
    ).all()
    if len(recent) < IDENTICAL_EXIT_BOUND:
        return None
    signatures = {
        _signature(job, view(job, session=session).lifecycle_result) for job in recent
    }
    if len(signatures) != 1 or None in signatures:
        return None
    return view(recent[0], session=session)
