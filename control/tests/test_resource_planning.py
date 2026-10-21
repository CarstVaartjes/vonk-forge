import json

from vonk_agent_protocol import ResourceBlockerCode
from vonk_control.owner_liveness import OwnerKind, OwnerRef
from vonk_control.resource_planning import (
    PLATFORM_MEMORY_FLOOR_BYTES,
    CapacitySnapshot,
    EffectiveResourceSettings,
    MemoryClaimHolder,
    MemoryReservationTotals,
    ParallelismSettings,
    PlannedStopRelease,
    ResourceEvidence,
    UnknownRunMemoryResidual,
    classify_preparation_effects,
    memory_capacity_snapshot,
    plan_capacity,
    plan_resource_preflight,
    resolve_effective_settings,
    resource_demand,
)


def _recipe_settings(
    *, context: int | None = 32_768, concurrency: int | None = 1
) -> dict[str, object]:
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


def _evidence(*, context_bytes_per_token: int | None = 2) -> ResourceEvidence:
    return ResourceEvidence(
        weights_bytes=100,
        runtime_overhead_bytes=20,
        baseline_context_tokens=32_768,
        baseline_concurrency=1,
        context_bytes_per_token=context_bytes_per_token,
        concurrency_bytes_per_request=50,
        evidence_state="measured",
        evidence_digest="a" * 64,
    )


def test_text_settings_drive_demand_and_identity() -> None:
    small = resolve_effective_settings(_recipe_document(_recipe_settings()))
    large = resolve_effective_settings(
        _recipe_document(_recipe_settings(context=65_536, concurrency=3))
    )
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
                "parallelism": {
                    "tensor": 1,
                    "pipeline": 1,
                    "data": 1,
                    "backend": "local",
                },
            },
        }
    )
    assert result.allowed and result.settings is not None
    demand = resource_demand(result.settings, _evidence())
    assert demand.allowed and demand.total_bytes == 120


def test_parallelism_has_one_topology_authority_and_duplicate_is_blocked() -> None:
    result = resolve_effective_settings(
        {
            "settings": {"kind": "job", "parallelism": {"world_size": 99}},
            "topology": {
                "node_count": 1,
                "parallelism": {
                    "tensor": 1,
                    "pipeline": 1,
                    "data": 1,
                    "backend": "local",
                },
            },
        }
    )
    assert not result.allowed
    assert result.settings is None
    repaired = resolve_effective_settings(_recipe_document(_recipe_settings()))
    assert repaired.allowed and repaired.settings is not None


def test_dimension_product_must_match_node_count() -> None:
    result = resolve_effective_settings(
        {
            "settings": {"kind": "job", "knobs": {}},
            "topology": {
                "node_count": 2,
                "parallelism": {
                    "tensor": 1,
                    "pipeline": 1,
                    "data": 1,
                    "backend": "local",
                },
            },
        }
    )
    assert not result.allowed
    assert result.settings is None
    repaired = resolve_effective_settings(_recipe_document(_recipe_settings()))
    assert repaired.allowed and repaired.settings is not None


def test_capacity_only_uses_explicit_planned_stop_release() -> None:
    settings = EffectiveResourceSettings(
        "generation", 32_768, 1, None, ParallelismSettings(1, 1, 1, 1, "local")
    )
    demand = resource_demand(settings, _evidence())
    capacity = [
        CapacitySnapshot(
            "rank-0",
            "unified",
            1_000,
            600,
            100,
            "measured",
            "b" * 64,
            unmaterialized_bytes=100,
        )
    ]
    without = plan_capacity({"rank-0": demand}, capacity, memory_floor_bytes=200)
    with_stop = plan_capacity(
        {"rank-0": demand},
        capacity,
        [PlannedStopRelease("old", "rank-0", "unified-memory", 100, True, "c" * 64)],
        memory_floor_bytes=200,
    )
    assert not without.allowed
    assert with_stop.allowed and with_stop.stop_before_prepare
    assert with_stop.nodes[0].current_free_after_bytes == 180
    assert with_stop.nodes[0].after_stop_free_after_bytes == 280


def test_unknown_run_residual_uses_full_upper_bound_and_warns_when_safe() -> None:
    settings = resolve_effective_settings(
        _recipe_document(_recipe_settings(context=65_536))
    ).settings
    assert settings is not None
    demand = resource_demand(
        settings,
        ResourceEvidence(
            weights_bytes=40,
            runtime_overhead_bytes=None,
            declared_total_bytes=60,
            baseline_context_tokens=32_768,
            baseline_concurrency=1,
            evidence_state="declared",
        ),
    )
    assert demand.allowed
    assert demand.total_bytes == 60

    residual = UnknownRunMemoryResidual(
        run_id="old-run",
        run_generation=4,
        reservation_kind="unified-memory",
        maximum_bytes=15,
    )
    reservations = MemoryReservationTotals(
        {"unified-memory": 15}, {}, {"unified-memory": (residual,)}
    )
    # Free=85, demand=60, reserve=5, and the exact active claim may have any
    # residual from 0 through its 15-byte peak. The safe upper bound fits.
    capacity = memory_capacity_snapshot(
        "rank-0",
        "unified",
        host=(100, 85),
        accelerator=(100, 85),
        reservations=reservations,
        memory_pool="shared",
        evidence_state="fresh",
        evidence_digest="a" * 64,
    )
    plan = plan_capacity({"rank-0": demand}, [capacity], memory_floor_bytes=5)
    assert plan.allowed
    assert plan.nodes[0].current_free_after_bytes is None
    assert plan.nodes[0].unknown_run_residuals == (residual,)
    warning = next(
        reason
        for reason in plan.nodes[0].reasons
        if reason.code == "resource.resident_usage_unknown"
    )
    assert warning.severity == "warning"


def test_possible_zero_resident_use_refuses_when_only_the_peak_upper_bound_fails() -> (
    None
):
    settings = resolve_effective_settings(
        _recipe_document(_recipe_settings(context=65_536))
    ).settings
    assert settings is not None
    demand = resource_demand(
        settings,
        ResourceEvidence(
            weights_bytes=40,
            runtime_overhead_bytes=None,
            declared_total_bytes=60,
            baseline_context_tokens=32_768,
            baseline_concurrency=1,
            evidence_state="declared",
        ),
    )
    residual = UnknownRunMemoryResidual(
        run_id="old-run",
        run_generation=4,
        reservation_kind="unified-memory",
        maximum_bytes=15,
    )
    capacity = memory_capacity_snapshot(
        "rank-0",
        "unified",
        host=(100, 70),
        accelerator=(100, 70),
        reservations=MemoryReservationTotals(
            {"unified-memory": 15}, {}, {"unified-memory": (residual,)}
        ),
        memory_pool="shared",
        evidence_state="fresh",
    )
    plan = plan_capacity({"rank-0": demand}, [capacity], memory_floor_bytes=5)
    assert not plan.allowed
    assert plan.nodes[0].current_free_after_bytes is None
    assert plan.nodes[0].unknown_run_residuals == (residual,)
    blocker = next(
        reason
        for reason in plan.nodes[0].reasons
        if reason.code == "resource.resident_usage_unknown"
    )
    assert blocker.severity == "blocker"


def test_fresh_aggregate_shortfall_remains_a_physical_capacity_blocker() -> None:
    settings = resolve_effective_settings(
        _recipe_document(_recipe_settings(context=65_536))
    ).settings
    assert settings is not None
    demand = resource_demand(
        settings,
        ResourceEvidence(
            weights_bytes=40,
            runtime_overhead_bytes=None,
            declared_total_bytes=60,
            baseline_context_tokens=32_768,
            baseline_concurrency=1,
            evidence_state="declared",
        ),
    )
    residual = UnknownRunMemoryResidual(
        run_id="old-run",
        run_generation=4,
        reservation_kind="unified-memory",
        maximum_bytes=15,
    )
    capacity = memory_capacity_snapshot(
        "rank-0",
        "unified",
        host=(100, 60),
        accelerator=(100, 60),
        reservations=MemoryReservationTotals(
            {"unified-memory": 15}, {}, {"unified-memory": (residual,)}
        ),
        memory_pool="shared",
        evidence_state="fresh",
    )
    plan = plan_capacity({"rank-0": demand}, [capacity], memory_floor_bytes=5)
    assert not plan.allowed


def _declared_demand(total: int):
    settings = resolve_effective_settings(
        _recipe_document(_recipe_settings(context=65_536))
    ).settings
    assert settings is not None
    return resource_demand(
        settings,
        ResourceEvidence(
            weights_bytes=total,
            runtime_overhead_bytes=None,
            declared_total_bytes=total,
            baseline_context_tokens=32_768,
            baseline_concurrency=1,
            evidence_state="declared",
        ),
    )


def _idle_capacity(total: int, free: int, *, claimed: int = 0):
    return memory_capacity_snapshot(
        "rank-0",
        "unified",
        host=(total, free),
        accelerator=(total, free),
        reservations=MemoryReservationTotals(
            {"unified-memory": claimed} if claimed else {}, {}, {}
        ),
        memory_pool="shared",
        evidence_state="fresh",
    )


def test_idle_spark_refuses_peak_that_displaces_the_host_floor() -> None:
    # Catches the idle-node exception that admitted an oversized declared peak.
    floor = PLATFORM_MEMORY_FLOOR_BYTES
    available = 126_000_000_000
    # Fresh admission succeeds once the capacity shortfall clears.
    for headroom in (0, -1, 0):
        plan = plan_capacity(
            {"rank-0": _declared_demand(available - floor - headroom)},
            [_idle_capacity(128_000_000_000, available)],
            memory_floor_bytes=floor,
        )
        assert plan.allowed == (headroom == 0)
        assert plan.nodes[0].current_free_after_bytes == floor + headroom


def test_a_shortfall_held_by_a_live_build_names_it_and_clears_with_it() -> None:
    # Production 2026-10-15: an idle Spark (3% used) refused a 119 GB peak while
    # a Spark build held its 30 GB claim; nothing said the wait was on it.
    build = MemoryClaimHolder(
        OwnerRef(OwnerKind.RECIPE_BUILD, "e37b4e04-9e4e-44c1-b58e-281494dc11b9"),
        30_000_000_000,
    )

    def plan(holders: tuple[MemoryClaimHolder, ...]):
        claimed = sum(holder.amount_bytes for holder in holders)
        totals = (
            MemoryReservationTotals(
                {"host-memory": claimed},
                {"host-memory": claimed},
                unmaterialized_holders_by_kind={"host-memory": holders},
            )
            if holders
            else MemoryReservationTotals({}, {})
        )
        return plan_capacity(
            {"rank-0": _declared_demand(119_063_746_765)},
            [
                memory_capacity_snapshot(
                    "rank-0",
                    "unified",
                    host=(130_663_235_584, 126_417_383_424),
                    accelerator=(130_663_235_584, 126_417_383_424),
                    reservations=totals,
                    memory_pool="shared",
                    evidence_state="fresh",
                )
            ],
            memory_floor_bytes=PLATFORM_MEMORY_FLOOR_BYTES,
        )

    held = plan((build,))
    blockers = [reason for reason in held.reasons if reason.severity == "blocker"]
    assert not held.allowed
    assert blockers
    assert all(build.owner.id in reason.detail for reason in blockers)
    assert plan(()).allowed


_SPARK_POOL = 130_663_235_584


def _blocker_codes(plan) -> list[str]:
    return [reason.code for reason in plan.reasons if reason.severity == "blocker"]


def test_a_shortfall_nothing_vonk_holds_can_clear_never_fits() -> None:
    # Production 2026-10-16: spark-3542 held no Vonk claim, 125_650_657_280
    # bytes free; the aday777 recipe's measured 122_005_303_296-byte peak left
    # 3_645_353_984 bytes before the 6 GB floor, and its load backed off 40
    # times on a capacity wait no claim, stop or run could ever clear.
    idle = plan_capacity(
        {"rank-0": _declared_demand(122_005_303_296)},
        [_idle_capacity(_SPARK_POOL, 125_650_657_280)],
        memory_floor_bytes=PLATFORM_MEMORY_FLOOR_BYTES,
    )
    assert not idle.allowed
    assert _blocker_codes(idle) == [ResourceBlockerCode.DEMAND_EXCEEDS_CAPACITY]
    (never_fits,) = (r for r in idle.reasons if r.severity == "blocker")
    assert "3645353984" in never_fits.detail

    # The same shortfall while a Vonk claim holds memory there is a wait.
    held = plan_capacity(
        {"rank-0": _declared_demand(122_005_303_296)},
        [_idle_capacity(_SPARK_POOL, 115_650_657_280, claimed=1_000_000_000)],
        memory_floor_bytes=PLATFORM_MEMORY_FLOOR_BYTES,
    )
    assert not held.allowed
    assert ResourceBlockerCode.DEMAND_EXCEEDS_CAPACITY not in _blocker_codes(held)

    # So is one while a planned stop may free it, even of unknown size.
    stopping = plan_capacity(
        {"rank-0": _declared_demand(122_005_303_296)},
        [_idle_capacity(_SPARK_POOL, 125_650_657_280)],
        [PlannedStopRelease("old", "rank-0", "unified-memory", None, True)],
        memory_floor_bytes=PLATFORM_MEMORY_FLOOR_BYTES,
    )
    assert not stopping.allowed
    assert ResourceBlockerCode.DEMAND_EXCEEDS_CAPACITY not in _blocker_codes(stopping)


def test_a_host_shortfall_inside_its_recent_noise_stays_a_wait() -> None:
    # spark-3542's idle free moved by about 0.5e9 within the hour (125.17e9 to
    # 125.67e9). The common 119_063_746_765-byte peak fits its best sample; one
    # low sample must not end that load as never fitting.
    def plan(best_free: int | None):
        capacity = memory_capacity_snapshot(
            "rank-0",
            "unified",
            host=(_SPARK_POOL, 125_000_000_000),
            accelerator=(_SPARK_POOL, 125_000_000_000),
            reservations=MemoryReservationTotals({}, {}),
            memory_pool="shared",
            evidence_state="fresh",
            recent_best_free_bytes=best_free,
        )
        return plan_capacity(
            {"rank-0": _declared_demand(119_063_746_765)},
            [capacity],
            memory_floor_bytes=PLATFORM_MEMORY_FLOOR_BYTES,
        )

    noisy = plan(125_670_000_000)
    assert not noisy.allowed
    assert _blocker_codes(noisy) == [ResourceBlockerCode.INSUFFICIENT_CAPACITY]
    assert _blocker_codes(plan(None)) == [ResourceBlockerCode.DEMAND_EXCEEDS_CAPACITY]


def test_a_demand_above_the_physical_pool_never_fits_whatever_is_held() -> None:
    plan = plan_capacity(
        {"rank-0": _declared_demand(_SPARK_POOL - 1_000_000_000)},
        [_idle_capacity(_SPARK_POOL, 90_000_000_000, claimed=30_000_000_000)],
        memory_floor_bytes=PLATFORM_MEMORY_FLOOR_BYTES,
    )
    assert not plan.allowed
    assert _blocker_codes(plan) == [ResourceBlockerCode.DEMAND_EXCEEDS_CAPACITY]


def test_declared_peak_larger_than_physical_memory_is_a_capacity_refusal() -> None:
    plan = plan_capacity(
        {"rank-0": _declared_demand(100)},
        [_idle_capacity(100, 96)],
        memory_floor_bytes=5,
    )
    assert not plan.allowed
    assert any(reason.severity == "blocker" for reason in plan.nodes[0].reasons)

    fresh = plan_capacity(
        {"rank-0": _declared_demand(100)},
        [_idle_capacity(110, 105)],
        memory_floor_bytes=5,
    )
    assert fresh.allowed


def test_unknown_capacity_evidence_still_blocks_an_idle_unverified_fit() -> None:
    capacity = memory_capacity_snapshot(
        "rank-0",
        "unified",
        host=(100, 96),
        accelerator=(100, 96),
        reservations=MemoryReservationTotals({}, {}, {}),
        memory_pool="shared",
        evidence_state="unknown",
    )
    plan = plan_capacity(
        {"rank-0": _declared_demand(100)}, [capacity], memory_floor_bytes=5
    )
    assert not plan.allowed


def test_uncertain_bound_still_refuses_actual_free_floor_and_budget_exhaustion() -> (
    None
):
    settings = resolve_effective_settings(
        _recipe_document(_recipe_settings(context=65_536))
    ).settings
    assert settings is not None
    demand = resource_demand(
        settings,
        ResourceEvidence(
            weights_bytes=40,
            runtime_overhead_bytes=None,
            declared_total_bytes=60,
            baseline_context_tokens=32_768,
            baseline_concurrency=1,
            evidence_state="declared",
        ),
    )
    assert demand.allowed

    actual_free_short = plan_capacity(
        {"rank-0": demand},
        [CapacitySnapshot("rank-0", "unified", 100, 40, 15, "fresh")],
        memory_floor_bytes=5,
    )
    assert not actual_free_short.allowed

    total_budget_short = plan_capacity(
        {"rank-0": demand},
        [CapacitySnapshot("rank-0", "unified", 100, 10, 36, "fresh")],
        memory_floor_bytes=5,
    )
    assert not total_budget_short.allowed

    invalid_envelope = resource_demand(
        settings,
        ResourceEvidence(
            weights_bytes=40,
            runtime_overhead_bytes=None,
            declared_total_bytes=-1,
            baseline_context_tokens=32_768,
            baseline_concurrency=1,
            evidence_state="declared",
        ),
    )
    assert not invalid_envelope.allowed

    invalid_measurement = resource_demand(
        settings,
        ResourceEvidence(
            weights_bytes=40,
            runtime_overhead_bytes=None,
            declared_total_bytes=60,
            baseline_context_tokens=32_768,
            baseline_concurrency=1,
            context_bytes_per_token=-1,
            evidence_state="declared",
        ),
    )
    assert not invalid_measurement.allowed

    missing_inventory = plan_capacity(
        {"rank-0": demand},
        [CapacitySnapshot("rank-0", "unified", None, None, None, "unknown")],
        memory_floor_bytes=5,
    )
    assert not missing_inventory.allowed


def test_missing_changed_text_evidence_blocks_and_effect_does_not_rebuild_image() -> (
    None
):
    settings = resolve_effective_settings(
        _recipe_document(_recipe_settings(context=65_536))
    ).settings
    assert settings is not None
    demand = resource_demand(settings, _evidence(context_bytes_per_token=None))
    assert not demand.allowed
    previous = EffectiveResourceSettings(
        "generation", 32_768, 1, None, ParallelismSettings(2, 2, 1, 1, "tcp")
    )
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


def test_production_services_use_exactly_the_platform_memory_floor() -> None:
    import ast
    from pathlib import Path

    from vonk_control.resource_planning import PLATFORM_MEMORY_FLOOR_BYTES

    source = Path(__file__).resolve().parents[1] / "src" / "vonk_control"
    services = {"RunAdmissionService", "RunSwitchOperationService"}
    seen: dict[str, int] = {}
    for path in sorted(source.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else None
            if name not in services:
                continue
            seen[name] = seen.get(name, 0) + 1
            floors = [
                item for item in node.keywords if item.arg == "memory_floor_bytes"
            ]
            # Omitting the argument takes the constant default; passing it must
            # pass the constant itself.
            for item in floors:
                assert (
                    isinstance(item.value, ast.Name)
                    and item.value.id == "PLATFORM_MEMORY_FLOOR_BYTES"
                ), (
                    f"{path.name}:{node.lineno} passes a floor other than the platform constant"
                )
    assert seen == {"RunAdmissionService": 2, "RunSwitchOperationService": 2}
    import inspect

    from vonk_control.run_admission import RunAdmissionService
    from vonk_control.run_switch_operations import RunSwitchOperationService

    for service in (RunAdmissionService, RunSwitchOperationService):
        default = inspect.signature(service).parameters["memory_floor_bytes"].default
        assert default == PLATFORM_MEMORY_FLOOR_BYTES


def test_stored_canonical_models_supply_exact_selected_weight_bytes() -> None:
    """Catches projections accepting malformed sizes or counting unused files."""
    from importlib import resources

    from vonk_control.resource_planning import memory_requirement
    from vonk_forge_contracts import ModelDefinition, RecipeDefinition

    recipe = RecipeDefinition.model_validate_json(
        resources.files("vonk_forge_contracts")
        .joinpath("examples", "recipe-source-build.json")
        .read_text()
    )
    model = ModelDefinition.model_validate_json(
        resources.files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text()
    )
    selection = recipe.models[0]
    reference = selection.model
    role = recipe.topology.roles[0]
    key = (reference.publisher, reference.slug, reference.content_sha256)
    stored_recipe = json.loads(recipe.model_dump_json())
    stored_model = json.loads(model.model_dump_json())
    need = memory_requirement(
        stored_recipe,
        role.resources.memory,
        role.name,
        {key: stored_model},
        observed_peak_bytes=None,
    )
    selected = {item.file_id for item in selection.files if role.name in item.roles}
    assert need.demand.weights_bytes == sum(
        item.size_bytes for item in model.files if item.id in selected
    )
    assert need.demand.allowed
    stored_model["files"][0]["size_bytes"] = True
    damaged = memory_requirement(
        stored_recipe,
        role.resources.memory,
        role.name,
        {key: stored_model},
        observed_peak_bytes=None,
    )
    assert not damaged.demand.allowed
    assert damaged.demand.weights_bytes is None


def test_typed_effective_selection_roundtrip_keeps_bound_identity_and_knobs() -> None:
    """Catches object duck-reading losing the already reviewed settings digest."""
    from vonk_control.run_switch_contract import EffectiveSettingsSelection

    resolved = resolve_effective_settings(_recipe_document(_recipe_settings())).settings
    assert resolved is not None
    selection = EffectiveSettingsSelection.model_validate_json(
        json.dumps(
            {
                "kind": resolved.kind,
                "context_tokens": resolved.context_tokens,
                "concurrency": resolved.concurrency,
                "max_batch_tokens": None,
                "parallelism": {
                    "world_size": 2,
                    "tensor": 2,
                    "pipeline": 1,
                    "data": 1,
                    "backend": "tcp",
                },
                "knobs": {"engine-switch": False, "engine-count": 0, "engine-note": ""},
                "change_effects": {},
                "identity_sha256": "f" * 64,
            }
        ),
        strict=True,
    )
    roundtrip = resolve_effective_settings(selection)
    assert roundtrip.allowed
    assert roundtrip.settings is not None
    assert roundtrip.settings.identity_digest == selection.identity_sha256
    assert roundtrip.settings.knobs == selection.knobs
    assert resource_demand(roundtrip.settings, _evidence()).total_bytes == 120


def test_invalid_engine_knob_and_boolean_context_remain_contract_blockers() -> None:
    """Catches raw nested values escaping canonical scalar and integer checks."""
    settings = _recipe_settings()
    settings["knobs"] = {
        "engine-owned": {"value": {"nested": True}, "change_effect": "restart"}
    }
    result = resolve_effective_settings(_recipe_document(settings))
    assert not result.allowed and result.settings is None
    settings = _recipe_settings()
    settings["context_tokens"] = {"value": True, "change_effect": "restart"}
    result = resolve_effective_settings(_recipe_document(settings))
    assert not result.allowed


def test_decision_context_participates_in_resource_settings_identity() -> None:
    settings = _recipe_settings(context=4096)
    settings["kind"] = "decision"
    first = resolve_effective_settings(_recipe_document(settings))
    assert first.allowed and first.settings is not None
    assert first.settings.context_tokens == 4096
    assert first.settings.change_effects["context_tokens"] == "reprepare"
    settings["context_tokens"] = {"value": 8192, "change_effect": "reprepare"}
    second = resolve_effective_settings(_recipe_document(settings))
    assert second.allowed and second.settings is not None
    assert first.settings.identity_digest != second.settings.identity_digest


def test_deepseek_deferral_uses_observed_host_not_recipe_reserve() -> None:
    # Wrong implementation: checking only peak+floor against total, or adding
    # recipe reserve, misses the real reason this empty-host load was refused.
    from dataclasses import replace

    from vonk_forge_contracts.memory_admission import evaluate_memory_admission

    peak = 124_000_000_000
    occupied = 4_515_061_760
    assert evaluate_memory_admission(peak, _SPARK_POOL).admissible
    assert not evaluate_memory_admission(
        peak, _SPARK_POOL, occupied_bytes=occupied
    ).admissible
    plan = plan_capacity(
        {"rank-0": _declared_demand(peak)},
        [
            replace(
                _idle_capacity(_SPARK_POOL, _SPARK_POOL - occupied),
                free_upside_bytes=21_598_208,
            )
        ],
        memory_floor_bytes=PLATFORM_MEMORY_FLOOR_BYTES,
    )
    assert not plan.allowed
    assert _blocker_codes(plan) == [ResourceBlockerCode.DEMAND_EXCEEDS_CAPACITY]
    assert "2169772032 bytes" in plan.reasons[-1].detail
