from vonk_control.resource_planning import (
    CapacitySnapshot,
    EffectiveResourceSettings,
    ParallelismSettings,
    PlannedStopRelease,
    ResourceEvidence,
    classify_preparation_effects,
    plan_capacity,
    plan_resource_preflight,
    resolve_effective_settings,
    resource_demand,
)


def _recipe_settings(*, context: int | None = 32_768, concurrency: int | None = 1) -> dict[str, object]:
    settings: dict[str, object] = {
        "kind": "generation",
        "knobs": {},
    }
    if context is not None:
        settings["context_tokens"] = {"value": context, "change_effect": "reprepare"}
    if concurrency is not None:
        settings["concurrency"] = {"value": concurrency, "change_effect": "restart"}
    return settings


def _recipe_document(settings: dict[str, object]) -> dict[str, object]:
    return {
        "settings": settings,
        "topology": {
            "node_count": 2,
            "parallelism": {
                "tensor": 2,
                "pipeline": 1,
                "data": 1,
                "backend": "tcp",
            },
        },
    }


def _evidence(**overrides: object) -> ResourceEvidence:
    values: dict[str, object] = {
        "weights_bytes": 100,
        "runtime_overhead_bytes": 20,
        "baseline_context_tokens": 32_768,
        "baseline_concurrency": 1,
        "context_bytes_per_token": 2,
        "concurrency_bytes_per_request": 50,
        "evidence_state": "measured",
        "evidence_digest": "a" * 64,
    }
    values.update(overrides)
    return ResourceEvidence(**values)


def test_text_settings_drive_demand_and_identity() -> None:
    small = resolve_effective_settings(_recipe_document(_recipe_settings()))
    large = resolve_effective_settings(_recipe_document(_recipe_settings(context=65_536, concurrency=3)))
    assert small.allowed and large.allowed
    assert small.settings is not None and large.settings is not None
    first = resource_demand(small.settings, _evidence())
    second = resource_demand(large.settings, _evidence())
    assert first.total_bytes == 120
    assert second.total_bytes == 65_756
    assert first.total_bytes < second.total_bytes
    assert small.settings.identity_digest != large.settings.identity_digest


def test_non_text_settings_may_omit_context_concurrency_and_batch() -> None:
    result = resolve_effective_settings(
        {
            "settings": {"kind": "job", "knobs": {}},
            "topology": {
                "node_count": 1,
                "parallelism": {"tensor": 1, "pipeline": 1, "data": 1, "backend": "local"},
            },
        }
    )
    assert result.allowed and result.settings is not None
    demand = resource_demand(result.settings, _evidence())
    assert demand.allowed and demand.total_bytes == 120


def test_parallelism_is_derived_from_topology_and_duplicate_is_blocked() -> None:
    result = resolve_effective_settings(
        {
            "settings": {"kind": "job", "parallelism": {"world_size": 99}},
            "topology": {
                "node_count": 1,
                "parallelism": {"tensor": 1, "pipeline": 1, "data": 1, "backend": "local"},
            },
        }
    )
    assert not result.allowed
    assert any(reason.code == "resource.parallelism_duplicate" for reason in result.reasons)


def test_capacity_only_uses_explicit_planned_stop_release() -> None:
    settings = EffectiveResourceSettings(
        "generation", 32_768, 1, None, ParallelismSettings(1, 1, 1, 1, "local")
    )
    demand = resource_demand(settings, _evidence())
    capacity = [CapacitySnapshot("rank-0", "unified", 1_000, 600, 100, "measured", "b" * 64)]
    without = plan_capacity({"rank-0": demand}, capacity, memory_floor_bytes=200)
    with_stop = plan_capacity(
        {"rank-0": demand}, capacity,
        [PlannedStopRelease("old", "rank-0", "unified-memory", 100, True, "c" * 64)],
        memory_floor_bytes=200,
    )
    assert not without.allowed
    assert with_stop.allowed and with_stop.stop_before_prepare
    assert with_stop.nodes[0].current_free_after_bytes == 180
    assert with_stop.nodes[0].after_stop_free_after_bytes == 280


def test_missing_changed_text_evidence_blocks_and_effect_does_not_rebuild_image() -> None:
    settings = resolve_effective_settings(_recipe_document(_recipe_settings(context=65_536))).settings
    assert settings is not None
    demand = resource_demand(settings, _evidence(context_bytes_per_token=None))
    assert not demand.allowed
    previous = EffectiveResourceSettings("generation", 32_768, 1, None, ParallelismSettings(2, 2, 1, 1, "tcp"))
    decision = classify_preparation_effects(previous, settings)
    assert decision.effect == "reprepare"
    assert decision.requires_reprepare
    assert not decision.requires_rebuild


def test_composed_preflight_returns_settings_demand_and_capacity() -> None:
    result = plan_resource_preflight(
        _recipe_document(_recipe_settings()),
        {"rank-0": _evidence()},
        [CapacitySnapshot("rank-0", "unified", 1_000, 600, 100, "measured", "b" * 64)],
        memory_floor_bytes=100,
    )
    assert result.allowed
    assert result.settings is not None
    assert result.demands["rank-0"].total_bytes == 120
