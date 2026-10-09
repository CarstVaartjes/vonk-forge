"""Resource planning: settings."""

from __future__ import annotations

import json
from collections.abc import Mapping

from pydantic import ValidationError
from vonk_agent_protocol import (
    ResourcePlanningCode,
)
from vonk_forge_contracts import RecipeDefinition

from ..resource_planning_contract import (
    ResourceRecipeProjection,
)
from ..run_switch_contract import (
    EffectiveSettingsSelection,
    RunSwitchChangeEffect,
)
from .identity import _canonical, _digest
from .reasons import _reason
from .types import (
    _CHANGE_EFFECTS,
    Effect,
    EffectiveResourceSettings,
    ParallelismSettings,
    PreparationDecision,
    SettingsResolution,
)


def resolve_effective_settings(value: object) -> SettingsResolution:
    """Read canonical settings once, then plan from typed owned fields."""
    if isinstance(value, EffectiveResourceSettings):
        return SettingsResolution(value)
    if isinstance(value, EffectiveSettingsSelection):
        parallel = value.parallelism
        return SettingsResolution(
            EffectiveResourceSettings(
                value.kind,
                value.context_tokens,
                value.concurrency,
                value.max_batch_tokens,
                ParallelismSettings(
                    parallel.world_size,
                    parallel.tensor,
                    parallel.pipeline,
                    parallel.data,
                    parallel.backend,
                ),
                value.knobs,
                value.change_effects,
                value.identity_sha256,
            )
        )
    if isinstance(value, RecipeDefinition):
        raw = value.model_dump(mode="json")
    elif isinstance(value, Mapping):
        raw = value
    else:
        return SettingsResolution(
            None,
            (
                _reason(
                    ResourcePlanningCode.SETTINGS_UNKNOWN,
                    "Canonical effective settings are unavailable.",
                ),
            ),
        )
    try:
        recipe = ResourceRecipeProjection.model_validate_json(
            json.dumps(raw, allow_nan=False), strict=True
        )
    except ValidationError as error:
        reasons = []
        for detail in error.errors():
            location = detail["loc"]
            if "parallelism" in location and "settings" in location:
                code = ResourcePlanningCode.PARALLELISM_DUPLICATE
            elif "topology" in location:
                code = (
                    ResourcePlanningCode.PARALLELISM_UNKNOWN
                    if detail["type"] == "missing"
                    else ResourcePlanningCode.PARALLELISM_TYPE
                )
            elif "knobs" in location:
                code = ResourcePlanningCode.KNOBS_INVALID
            elif detail["type"] in {"union_tag_invalid", "union_tag_not_found"}:
                code = ResourcePlanningCode.SETTINGS_KIND_UNKNOWN
            else:
                code = ResourcePlanningCode.SETTINGS_TYPE
            reasons.append(
                _reason(
                    code,
                    "Canonical resource settings are invalid: "
                    + ".".join(str(part) for part in location),
                )
            )
        return SettingsResolution(None, tuple(dict.fromkeys(reasons)))
    except (TypeError, ValueError):
        return SettingsResolution(
            None,
            (
                _reason(
                    ResourcePlanningCode.SETTINGS_UNKNOWN,
                    "Canonical effective settings cannot be read.",
                ),
            ),
        )
    settings = recipe.settings
    parallel = recipe.topology.parallelism
    if (
        parallel.tensor * parallel.pipeline * parallel.data
        != recipe.topology.node_count
    ):
        return SettingsResolution(
            None,
            (
                _reason(
                    ResourcePlanningCode.PARALLELISM_INCONSISTENT,
                    "Topology parallelism product does not equal node_count.",
                ),
            ),
        )
    context = settings.context_tokens.value if settings.kind == "generation" else None
    concurrency = (
        settings.concurrency.value if settings.concurrency is not None else None
    )
    batch = (
        settings.max_batch_tokens.value
        if settings.kind != "job" and settings.max_batch_tokens is not None
        else None
    )
    knobs = {name: setting.value for name, setting in settings.knobs.items()}
    effects: dict[str, RunSwitchChangeEffect] = {
        name: setting.change_effect for name, setting in settings.knobs.items()
    }
    if settings.kind == "generation":
        effects["context_tokens"] = settings.context_tokens.change_effect
    if settings.concurrency is not None:
        effects["concurrency"] = settings.concurrency.change_effect
    if settings.kind != "job" and settings.max_batch_tokens is not None:
        effects["max_batch_tokens"] = settings.max_batch_tokens.change_effect
    identity = {
        "kind": settings.kind,
        "context_tokens": context,
        "concurrency": concurrency,
        "max_batch_tokens": batch,
        "parallelism": {
            "world_size": recipe.topology.node_count,
            "tensor": parallel.tensor,
            "pipeline": parallel.pipeline,
            "data": parallel.data,
            "backend": parallel.backend,
        },
        "knobs": _canonical(knobs),
    }
    return SettingsResolution(
        EffectiveResourceSettings(
            settings.kind,
            context,
            concurrency,
            batch,
            ParallelismSettings(
                recipe.topology.node_count,
                parallel.tensor,
                parallel.pipeline,
                parallel.data,
                parallel.backend,
            ),
            knobs,
            effects,
            _digest(identity),
        )
    )


def classify_preparation_effects(
    previous: EffectiveResourceSettings | object | None,
    current: EffectiveResourceSettings | object,
    *,
    parameter_effects: Mapping[str, RunSwitchChangeEffect] | None = None,
) -> PreparationDecision:
    current_resolution = (
        current
        if isinstance(current, EffectiveResourceSettings)
        else resolve_effective_settings(current).settings
    )
    if current_resolution is None:
        raise ValueError("effective settings are invalid")
    previous_resolution = (
        previous
        if isinstance(previous, EffectiveResourceSettings)
        else resolve_effective_settings(previous).settings
        if previous is not None
        else None
    )
    current_identity = current_resolution.identity()
    previous_identity = (
        previous_resolution.identity() if previous_resolution is not None else None
    )
    changed = {
        key
        for key in current_identity
        if previous_identity is None
        or current_identity[key] != previous_identity.get(key)
    }
    effects = dict(current_resolution.change_effects)
    effects.update(parameter_effects or {})
    if any(effect not in _CHANGE_EFFECTS for effect in effects.values()):
        raise ValueError("parameter change effects are invalid")
    active = {key: effect for key, effect in effects.items() if effect != "none"}
    rebuild = "rebuild" in active.values()
    reinstall = rebuild or "reinstall" in active.values() or "parallelism" in changed
    reprepare = (
        reinstall
        or bool(
            changed & {"context_tokens", "concurrency", "batch_tokens", "knobs", "kind"}
        )
        or "reprepare" in active.values()
    )
    restart = reprepare or bool(changed) or "restart" in active.values()
    effect: Effect = (
        "rebuild"
        if rebuild
        else "reinstall"
        if reinstall
        else "reprepare"
        if reprepare
        else "restart"
        if restart
        else "reuse"
    )
    return PreparationDecision(
        effect,
        tuple(sorted(changed | set(active))),
        bool(changed or active),
        restart,
        reprepare,
        reinstall,
        rebuild,
        current_resolution.identity_digest or _digest(current_identity),
    )
