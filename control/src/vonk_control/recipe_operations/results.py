"""Results for digest-bound recipe operations."""

from __future__ import annotations

from vonk_agent_protocol import (
    LifecycleState,
    RecipeOperationCode,
)

from ..lifecycle.evidence import (
    Residue,
    read_or_rebuild,
)
from ..recipe_lifecycle_contract import (
    LifecycleCodeFailureResult,
    LifecycleNodeResult,
    RecipeLifecycleResult,
    RecipeOperationProgressResult,
    parse_recipe_lifecycle_result,
)
from .errors import RecipeRequestInvalid


def _validated_result(kind: str, value: object) -> RecipeLifecycleResult:
    """Validate a lifecycle result this call composes, before it is persisted.

    Only documents the caller has just built come through here; a stored result
    is read with :func:`_recorded_result`, which never raises.
    """

    try:
        parsed = parse_recipe_lifecycle_result(kind, value)
    except (TypeError, ValueError) as error:
        raise RecipeRequestInvalid("recipe operation result is invalid") from error
    return parsed


def _recorded_result(
    kind: str, value: object, *, subject: str
) -> RecipeLifecycleResult | None:
    """A stored lifecycle result; damaged evidence is retired as unknown.

    The result is evidence of what was done, never a reason to refuse the next
    step: a result that does not parse is recorded as residue and read as absent.
    """

    if value is None:
        return None

    def read() -> RecipeLifecycleResult:
        return parse_recipe_lifecycle_result(kind, value)

    loaded = read_or_rebuild(kind="recipe.operation-result", subject=subject, read=read)
    return None if isinstance(loaded, Residue) else loaded


def _recorded_result_document(
    kind: str, value: object, *, subject: str
) -> RecipeLifecycleResult:
    """Return the canonical result for a merged write or API response."""
    result = _recorded_result(kind, value, subject=subject)
    return (
        result
        if result is not None
        else RecipeOperationProgressResult(node_evidence={})
    )


def _node_result(
    kind: str, node_id: str, evidence: object
) -> LifecycleNodeResult | None:
    try:
        result = parse_recipe_lifecycle_result(
            kind, {"node_evidence": {node_id: evidence}}
        )
    except (TypeError, ValueError):
        return None
    if (
        isinstance(result, RecipeOperationProgressResult)
        and result.node_evidence is not None
    ):
        return result.node_evidence[node_id]
    return None


_RANK_FAILED = LifecycleState.FAILED.value


def _unproven_evidence(detail: str) -> LifecycleCodeFailureResult:
    """The typed marker recorded for a node whose evidence cannot be accepted."""

    return LifecycleCodeFailureResult(
        code=RecipeOperationCode.EVIDENCE_UNPROVEN, detail=detail[:512]
    )


def _evidence_is_acceptable(kind: str, node_id: str, evidence: object) -> bool:
    """Whether the lifecycle contract accepts this node evidence for the kind."""

    try:
        parse_recipe_lifecycle_result(kind, {"node_evidence": {node_id: evidence}})
    except (TypeError, ValueError):
        return False
    return True


_AcceptedRanks = tuple[frozenset[tuple[str, int, str]], bool]
