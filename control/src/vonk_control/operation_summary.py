"""One plain phrase and one subject per operation, for Activity and every reader.

The phrase is derived from the operation's kind and state; the subject is the
workload alias of the run the operation acts on, read from the operation's own
``run_id`` linkage in one batched lookup per page. Both are display facts:
nothing admits, retries or gates on them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentOperation as AgentOperationKind
from vonk_agent_protocol import LifecycleState

from .models import AgentOperation, RecipeRun

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

#: States whose own word is not the plain one; every other state reads as itself.
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
    parts = [
        part
        for part in text.replace(".", " ").replace("_", " ").replace("-", " ").split()
    ]
    if len(parts) > 1 and parts[-1][0] == "v" and parts[-1][1:].isdigit():
        parts.pop()
    return " ".join(parts)


def operation_summary(kind: str, state: str) -> str:
    """ "Job running", "Recipe start completed"; an unknown kind or state reads as its own words."""

    phrase = _KIND_PHRASES.get(kind) or _words(kind).capitalize() or "Operation"
    word = _STATE_WORDS.get(state) or _words(state) or "recorded"
    return f"{phrase} {word}"


def workload_aliases(
    session: Session, operations: Sequence[AgentOperation]
) -> dict[str, str]:
    """Operation id to the alias of the run it acts on, in one query for the page."""

    run_ids = {
        operation.id: run_id
        for operation in operations
        if isinstance(operation.payload, Mapping)
        and isinstance(run_id := operation.payload.get("run_id"), str)
    }
    if not run_ids:
        return {}
    aliases = {
        run: alias
        for run, alias in session.execute(
            select(RecipeRun.id, RecipeRun.alias).where(
                RecipeRun.id.in_(set(run_ids.values()))
            )
        )
    }
    return {
        operation_id: aliases[run_id]
        for operation_id, run_id in run_ids.items()
        if run_id in aliases
    }
