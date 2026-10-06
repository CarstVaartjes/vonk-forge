from vonk_control.resource_planning import (
    ENVELOPE_EXCEEDS_CAPACITY,
    ENVELOPE_UNVERIFIED,
    CapacitySnapshot,
    EffectiveResourceSettings,
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
    assert any(
        reason.code == "resource.parallelism_duplicate" for reason in result.reasons
    )


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
    assert any(
        reason.code == "resource.parallelism_inconsistent" for reason in result.reasons
    )


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
    warning = next(reason for reason in demand.reasons if reason.severity == "warning")
    assert "Forecast 60 bytes" in warning.detail
    assert "declared recipe-role memory envelope" in warning.detail
    assert "may exceed this bound" in warning.detail

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
    assert "range 0..15 bytes" in warning.detail
    assert "full upper bound" in warning.detail


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
    assert "Capacity is unverified" in blocker.detail
    assert "Reconcile the exact run claims" in blocker.detail
    assert "resource.insufficient_capacity" not in {
        reason.code for reason in plan.nodes[0].reasons
    }


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
    assert "resource.insufficient_capacity" in {
        reason.code for reason in plan.nodes[0].reasons
    }
    assert "resource.resident_usage_unknown" not in {
        reason.code for reason in plan.nodes[0].reasons
    }


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


def test_envelope_larger_than_an_idle_spark_is_admitted_as_an_unverified_fit() -> None:
    # Peak plus floor (105) exceeds the 100-byte Spark, but no Vonk claim holds
    # memory: the declared envelope is an estimate, so the attempt is admitted.
    plan = plan_capacity(
        {"rank-0": _declared_demand(100)},
        [_idle_capacity(100, 96)],
        memory_floor_bytes=5,
    )
    assert plan.allowed
    assert plan.nodes[0].allowed
    assert not any(reason.severity == "blocker" for reason in plan.reasons)
    by_code = {reason.code: reason for reason in plan.nodes[0].reasons}
    assert by_code[ENVELOPE_UNVERIFIED].severity == "warning"
    exceeds = by_code[ENVELOPE_EXCEEDS_CAPACITY]
    assert exceeds.severity == "warning"
    assert "105 bytes" in exceeds.detail and "100-byte" in exceeds.detail
    assert not plan.nodes[0].stop_required


def test_envelope_larger_than_the_spark_with_a_vonk_claim_stays_a_capacity_wait() -> (
    None
):
    plan = plan_capacity(
        {"rank-0": _declared_demand(100)},
        [_idle_capacity(100, 90, claimed=10)],
        memory_floor_bytes=5,
    )
    assert not plan.allowed
    codes = {reason.code for reason in plan.nodes[0].reasons}
    assert ENVELOPE_UNVERIFIED not in codes
    assert ENVELOPE_EXCEEDS_CAPACITY not in codes
    assert "resource.insufficient_reservation_budget" in codes


def test_envelope_that_misses_free_memory_on_an_idle_spark_is_admitted_unverified() -> (
    None
):
    # 95 + 4 fits the 100-byte Spark; the OS leaves 96 free, no claim holds
    # memory. The shortfall is only the declared estimate, so it is admitted.
    plan = plan_capacity(
        {"rank-0": _declared_demand(95)},
        [_idle_capacity(100, 96)],
        memory_floor_bytes=4,
    )
    assert plan.allowed
    codes = {reason.code for reason in plan.nodes[0].reasons}
    assert ENVELOPE_UNVERIFIED in codes
    assert ENVELOPE_EXCEEDS_CAPACITY not in codes
    assert "resource.insufficient_capacity" not in codes


def test_idle_spark_with_less_free_memory_than_the_floor_alone_still_refuses() -> None:
    # Observed free memory is below the platform floor regardless of any
    # declared envelope: that is a measurement, not an estimate.
    plan = plan_capacity(
        {"rank-0": _declared_demand(50)},
        [_idle_capacity(100, 3)],
        memory_floor_bytes=5,
    )
    assert not plan.allowed
    assert ENVELOPE_UNVERIFIED not in {reason.code for reason in plan.nodes[0].reasons}


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
    assert any(
        reason.code == "resource.evidence_invalid" and reason.severity == "blocker"
        for reason in invalid_envelope.reasons
    )

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
    assert any(
        reason.code == "resource.context_evidence_invalid"
        and reason.severity == "blocker"
        for reason in invalid_measurement.reasons
    )

    missing_inventory = plan_capacity(
        {"rank-0": demand},
        [CapacitySnapshot("rank-0", "unified", None, None, None, "unknown")],
        memory_floor_bytes=5,
    )
    assert not missing_inventory.allowed
    assert any(
        reason.code == "resource.capacity_unknown" and reason.severity == "blocker"
        for reason in missing_inventory.reasons
    )


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
    for path in sorted(source.glob("*.py")):
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
        assert default == PLATFORM_MEMORY_FLOOR_BYTES == 2_000_000_000
