"""One plain phrase and one subject per operation, for Activity and every reader.

The phrase is derived from the operation's kind and state; the subject is the
workload alias of the run the operation acts on, read from the operation's own
``run_id`` linkage in one batched lookup per page. Both are display facts:
nothing admits, retries or gates on them.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from vonk_agent_protocol.agent_operations import AgentOperation as AgentOperationKind
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState
from vonk_control.models.execution import RecipeRun
from vonk_control.models.operations import AgentOperation

_KIND_PHRASES: dict[str, str] = {
    AgentOperationKind.RUNTIME_PREFLIGHT: "Runtime check",
    AgentOperationKind.AGENT_UPGRADE: "Agent upgrade",
    AgentOperationKind.ARTIFACT_DISTRIBUTION: "Model distribution",
    AgentOperationKind.RECIPE_BUILD: "Image build",
    AgentOperationKind.RECIPE_BUILD_CLEANUP: "Build cleanup",
    AgentOperationKind.RECIPE_INSTALL: "Recipe install",
    AgentOperationKind.RECIPE_START: "Recipe start",
    AgentOperationKind.RECIPE_JOB_RUN: "Recipe job",
    AgentOperationKind.RECIPE_STOP: "Recipe stop",
    AgentOperationKind.RECIPE_UNINSTALL: "Recipe removal",
    AgentOperationKind.RECIPE_RECONCILE: "Recipe check",
}

#: Every stored operation state is a ``LifecycleState`` (jobs, agent operations,
#: model-cache, profile applications and artifact jobs all adopt it), plus the
#: model-cache operator word ``accepted``. A state whose own word is plain reads as
#: itself; only the others are listed. An unknown state reads as its own words.
_STATE_WORDS: dict[str, str] = {
    LifecycleState.OBSERVING: "being checked",
    LifecycleState.BACKOFF: "retrying",
    LifecycleState.SUCCEEDED: "finished",
    LifecycleState.SUPERSEDED: "replaced",
    LifecycleState.NEEDS_OPERATOR: "waiting for operator",
}


def _words(value: str) -> str:
    """A kind or state as lowercase words, without a trailing schema version."""

    text = value.strip().lower()
    parts = list(text.replace(".", " ").replace("_", " ").replace("-", " ").split())
    if len(parts) > 1 and parts[-1][0] == "v" and parts[-1][1:].isdigit():
        parts.pop()
    return " ".join(parts)


#: What Activity says for a row whose stored history cannot be read.
UNREADABLE_SUMMARY = "Operation history is unreadable"


def operation_summary(kind: str, state: str) -> str:
    """ "Recipe job running", "Recipe start finished"; an unknown kind or state reads as its own words."""

    phrase = _KIND_PHRASES.get(kind) or _words(kind).capitalize() or "Operation"
    word = _STATE_WORDS.get(state) or _words(state) or "recorded"
    return f"{phrase} {word}"


def workload_aliases(
    session: Session, operations: Sequence[AgentOperation]
) -> dict[str, str]:
    """Operation id to the alias of the run it acts on: one joined query for the page."""

    if not operations:
        return {}
    return dict(
        session.execute(
            select(AgentOperation.id, RecipeRun.alias)
            .join(
                RecipeRun, RecipeRun.id == AgentOperation.payload["run_id"].as_string()
            )
            .where(AgentOperation.id.in_([operation.id for operation in operations]))
        ).all()
    )
