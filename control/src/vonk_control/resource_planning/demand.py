"""Resource planning: demand."""

from __future__ import annotations

import json
from collections.abc import Mapping

from vonk_agent_protocol import (
    ResourcePlanningCode,
    ResourceTerm,
    ResourceTermProblem,
    resource_term_code,
)
from vonk_forge_contracts import ModelDefinition

from ..bounded_json import require_integer
from ..resource_planning_contract import (
    ResourceRecipeProjection,
)
from .identity import _is_digest
from .reasons import _reason
from .settings import resolve_effective_settings
from .types import (
    EffectiveResourceSettings,
    ResourceDemand,
    ResourceEvidence,
    ResourceReason,
    SettingsResolution,
)


def _selected_model_bytes(
    recipe_document: Mapping[str, object],
    model_documents: Mapping[
        tuple[str, str, str], ModelDefinition | Mapping[str, object]
    ]
    | None,
    role_name: str,
) -> int | None:
    if not model_documents:
        return None
    try:
        recipe = ResourceRecipeProjection.model_validate_json(
            json.dumps(recipe_document, allow_nan=False), strict=True
        )
        total = 0
        selected_any = False
        for selection in recipe.models:
            reference = selection.model
            raw_model = model_documents.get(
                (reference.publisher, reference.slug, reference.content_sha256)
            )
            if raw_model is None:
                return None
            model = (
                raw_model
                if isinstance(raw_model, ModelDefinition)
                else ModelDefinition.model_validate_json(
                    json.dumps(raw_model, allow_nan=False), strict=True
                )
            )
            by_id = {file.id: file for file in model.files}
            selected_ids = {
                item.file_id for item in selection.files if role_name in item.roles
            }
            for file_id in selected_ids:
                file = by_id.get(file_id)
                if file is None:
                    return None
                total += file.size_bytes
                selected_any = True
        return total if selected_any else None
    except (TypeError, ValueError):
        return None


def _resource_evidence(
    recipe_document: Mapping[str, object],
    role_name: str,
    model_documents: Mapping[
        tuple[str, str, str], ModelDefinition | Mapping[str, object]
    ]
    | None,
    declared_total_bytes: int,
    settings: EffectiveResourceSettings | None,
) -> ResourceEvidence:
    model_bytes = _selected_model_bytes(recipe_document, model_documents, role_name)
    return ResourceEvidence(
        weights_bytes=model_bytes,
        runtime_overhead_bytes=None,
        declared_total_bytes=declared_total_bytes if model_bytes is not None else None,
        baseline_context_tokens=settings.context_tokens
        if settings is not None
        else None,
        baseline_concurrency=settings.concurrency if settings is not None else None,
        baseline_batch_tokens=settings.batch_tokens if settings is not None else None,
        evidence_state="declared" if model_bytes is not None else "unknown",
    )


def resource_demand(
    settings: EffectiveResourceSettings | object,
    evidence: ResourceEvidence,
    *,
    node_id: str | None = None,
) -> ResourceDemand:
    resolution = (
        SettingsResolution(settings, ())
        if isinstance(settings, EffectiveResourceSettings)
        else resolve_effective_settings(settings)
    )
    reasons = list(resolution.reasons)
    if resolution.settings is None:
        return ResourceDemand(
            None, None, None, None, None, None, "unknown", tuple(reasons)
        )
    selected = resolution.settings
    declared_bound = (
        evidence.declared_total_bytes
        if type(evidence.declared_total_bytes) is int
        and evidence.declared_total_bytes >= 0
        else None
    )
    uncertain: list[str] = []
    if evidence.declared_total_bytes is not None and declared_bound is None:
        reasons.append(
            _reason(
                ResourcePlanningCode.EVIDENCE_INVALID,
                "Declared recipe-role memory envelope is invalid.",
                node_id=node_id,
            )
        )
    if evidence.evidence_state in {"unknown", "stale"}:
        if declared_bound is None:
            reasons.append(
                _reason(
                    ResourcePlanningCode.EVIDENCE_UNKNOWN,
                    "Memory evidence is missing or stale and no declared role bound is available.",
                    node_id=node_id,
                )
            )
        else:
            uncertain.append(f"{evidence.evidence_state} evidence")
    if evidence.evidence_digest is not None and not _is_digest(
        evidence.evidence_digest
    ):
        reasons.append(
            _reason(
                ResourcePlanningCode.EVIDENCE_INVALID,
                "Memory evidence digest is invalid.",
                node_id=node_id,
            )
        )
    for name, item in (
        ("weights_bytes", evidence.weights_bytes),
        ("runtime_overhead_bytes", evidence.runtime_overhead_bytes),
    ):
        if item is None:
            if declared_bound is None:
                reasons.append(
                    _reason(
                        ResourcePlanningCode.EVIDENCE_UNKNOWN,
                        f"{name} is missing and no declared role bound is available.",
                        node_id=node_id,
                    )
                )
            else:
                uncertain.append(name)
        elif type(item) is not int or item < 0:
            reasons.append(
                _reason(
                    ResourcePlanningCode.EVIDENCE_INVALID,
                    f"{name} is invalid; resource evidence cannot be trusted.",
                    node_id=node_id,
                )
            )
    context = _term(
        ResourceTerm.CONTEXT,
        selected.context_tokens,
        evidence.baseline_context_tokens,
        evidence.context_bytes_per_token,
        evidence.supported_context_tokens,
        node_id,
        required=selected.context_tokens is not None,
    )
    concurrency = _term(
        ResourceTerm.CONCURRENCY,
        selected.concurrency,
        evidence.baseline_concurrency,
        evidence.concurrency_bytes_per_request,
        evidence.supported_concurrency,
        node_id,
        required=selected.concurrency is not None,
    )
    batch = _term(
        ResourceTerm.BATCH,
        selected.batch_tokens,
        evidence.baseline_batch_tokens,
        evidence.batch_bytes_per_token,
        evidence.supported_batch_tokens,
        node_id,
        required=selected.batch_tokens is not None,
    )
    terms: list[tuple[int | None, tuple[ResourceReason, ...]]] = []
    for name, term in (
        ("context", context),
        ("concurrency", concurrency),
        ("batch", batch),
    ):
        if term[0] is None and declared_bound is not None:
            if term[1] and all(
                reason.code.endswith(("_unknown", "_unsupported")) for reason in term[1]
            ):
                uncertain.append(f"{name} estimate")
                terms.append((0, ()))
            else:
                terms.append(term)
                reasons.extend(term[1])
        else:
            terms.append(term)
            reasons.extend(term[1])
    total: int | None = None
    if not reasons and all(isinstance(term[0], int) for term in terms):
        base = declared_bound
        if (
            base is None
            and type(evidence.weights_bytes) is int
            and type(evidence.runtime_overhead_bytes) is int
        ):
            base = evidence.weights_bytes + evidence.runtime_overhead_bytes
        if base is not None:
            total = (
                base
                + require_integer(terms[0][0], "context")
                + require_integer(terms[1][0], "concurrency")
                + require_integer(terms[2][0], "batch")
            )
    if uncertain and declared_bound is not None and total is not None:
        reasons.append(
            _reason(
                ResourcePlanningCode.ESTIMATE_UNCERTAIN,
                f"Forecast {total} bytes from the declared recipe-role memory envelope ({declared_bound} bytes); {', '.join(dict.fromkeys(uncertain))} is unavailable, so actual demand may exceed this bound.",
                severity="warning",
                node_id=node_id,
            )
        )
    return ResourceDemand(
        evidence.weights_bytes if type(evidence.weights_bytes) is int else None,
        evidence.runtime_overhead_bytes
        if type(evidence.runtime_overhead_bytes) is int
        else None,
        context[0],
        concurrency[0],
        batch[0],
        total,
        evidence.evidence_state,
        tuple(reasons),
    )


def _term(
    name: ResourceTerm,
    value: int | None,
    baseline: int | None,
    coefficient: int | None,
    supported: tuple[int, int] | None,
    node_id: str | None,
    *,
    required: bool,
) -> tuple[int | None, tuple[ResourceReason, ...]]:
    if value is None:
        return (
            (0, ())
            if not required
            else (
                None,
                (
                    _reason(
                        resource_term_code(name, ResourceTermProblem.UNKNOWN),
                        f"Effective {name.value} setting is unavailable; capacity cannot be predicted.",
                        node_id=node_id,
                    ),
                ),
            )
        )
    if baseline is not None and (type(baseline) is not int or baseline < 0):
        return None, (
            _reason(
                resource_term_code(name, ResourceTermProblem.EVIDENCE_INVALID),
                f"Measured baseline evidence for {name.value} is invalid.",
                node_id=node_id,
            ),
        )
    if supported is not None and (
        len(supported) != 2
        or any(type(item) is not int or item < 0 for item in supported)
        or supported[0] > supported[1]
    ):
        return None, (
            _reason(
                resource_term_code(name, ResourceTermProblem.EVIDENCE_INVALID),
                f"Declared supported range for {name.value} is invalid.",
                node_id=node_id,
            ),
        )
    if supported is not None and (value < supported[0] or value > supported[1]):
        return None, (
            _reason(
                resource_term_code(name, ResourceTermProblem.UNSUPPORTED),
                f"Effective {name.value} setting is outside the declared supported range.",
                node_id=node_id,
            ),
        )
    if baseline is None:
        return None, (
            _reason(
                resource_term_code(name, ResourceTermProblem.EVIDENCE_UNKNOWN),
                f"No baseline evidence is declared for effective {name.value}.",
                node_id=node_id,
            ),
        )
    if value == baseline:
        return 0, ()
    if coefficient is None:
        return None, (
            _reason(
                resource_term_code(name, ResourceTermProblem.EVIDENCE_UNKNOWN),
                f"No evidence supports changing effective {name.value} from its measured baseline.",
                node_id=node_id,
            ),
        )
    if type(coefficient) is not int or coefficient < 0:
        return None, (
            _reason(
                resource_term_code(name, ResourceTermProblem.EVIDENCE_INVALID),
                f"Measured coefficient for {name.value} is invalid.",
                node_id=node_id,
            ),
        )
    return max(0, value - baseline) * coefficient, ()
