from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from vonk_agent_protocol import canonical_message
from vonk_control.distribution_executor.identity import DistributionIdentity
from vonk_control.inventory_repository import (
    INVENTORY_MAX_AGE,
    InventoryRepository,
    NodeInventoryReport,
    spark_model_objects,
)
from vonk_control.run_switch_operations import (
    DatabaseRunSwitchArtifactInspector,
    RunSwitchOperationService,
)

from .test_recipe_operations import NOW, installed_recipe
from .test_run_switch_operations import (
    ModelCacheManifestProvider,
    RecordingArtifactExecutor,
    _request,
    setup_services,
)


def test_missing_artifact_authority_does_not_poison_repaired_inspection(
    tmp_path,
) -> None:
    inspector = DatabaseRunSwitchArtifactInspector()

    # No provider is a blocker the plan observes again, never an exception.
    inspection = inspector.inspect(
        None,  # type: ignore[arg-type]
        model_content_sha256="a" * 64,
        recipe_revision_id="recipe-revision",
        node_ids=("spk_" + "b" * 32,),
        retention="retain",
        now=datetime(2026, 9, 6, tzinfo=UTC),
    )

    assert inspection.required_bytes is None
    assert inspection.missing_nas_bytes is None
    assert inspection.missing_spark_bytes is None
    assert inspection.artifact_set_sha256 is None
    sessions, _lifecycle, _queue, _mapping, _build, nodes = setup_services(tmp_path)
    provider = ModelCacheManifestProvider(missing_nas_bytes=0)
    inspector.bind_model_cache(provider)
    with sessions() as session:
        repaired = inspector.inspect(
            session,
            model_content_sha256="a" * 64,
            recipe_revision_id="recipe-revision",
            node_ids=(nodes[0],),
            retention="retain",
            now=datetime(2026, 9, 6, tzinfo=UTC),
        )
    assert not repaired.blockers
    assert (
        repaired.artifact_set_sha256
        == provider.resolve_artifact_set(model_content_sha256="a" * 64).digest
    )
    assert repaired.missing_nas_bytes == 0
    assert repaired.missing_spark_bytes == repaired.artifact_set_bytes
    assert repaired.required_bytes == repaired.artifact_set_bytes


def _report_store(sessions, node_id: str, objects) -> None:
    """The agent's next inventory, ingested like any other, with its store scan."""

    InventoryRepository(sessions, clock=lambda: NOW).record(
        NodeInventoryReport.model_validate_json(
            canonical_message(
                {
                    "node_id": node_id,
                    "observed_at": NOW + timedelta(seconds=1),
                    "disk_total_bytes": 10_000,
                    "disk_free_bytes": 8_000,
                    "host_memory_total_bytes": 10_000,
                    "host_memory_free_bytes": 8_000,
                    "gpu_memory_total_bytes": 10_000,
                    "gpu_memory_free_bytes": 8_000,
                    "gpu_count": 1,
                    "artifact_store_read_only": False,
                    "capabilities": (
                        "runtime.vonk.v1",
                        "recipe.image.pull.v1",
                        "recipe.operations.v1",
                    ),
                    "memory_pool": "shared",
                    "schema_version": 1,
                    "nvidia_driver_version": "unknown",
                    "container_runtime_version": "unknown",
                    "model_objects": objects,
                }
            )
        )
    )


@pytest.mark.parametrize("scan", ["held", "empty", "unknown"])
def test_rerun_of_an_installed_workload_copies_only_what_the_spark_lacks(
    tmp_path, scan: str
) -> None:
    """Spark coverage is the agent's own store scan: a Spark that reports the
    model's files is a cached target, so a re-run plans no target copy. One
    that lacks them, or reports no scan, gets the copy, which the agent dedups."""

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    provider = ModelCacheManifestProvider(missing_nas_bytes=0)
    request = _request(sessions, nodes[0])
    manifest = provider.resolve_artifact_set(
        model_content_sha256=request.model_content_sha256
    )
    held = scan == "held"
    _report_store(
        sessions,
        nodes[0],
        None
        if scan == "unknown"
        else [
            {"sha256": spec.sha256, "bytes": spec.expected_bytes}
            for spec in manifest.artifacts
        ]
        if held
        else [],
    )
    service = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: NOW,
        artifact_phase_executor=RecordingArtifactExecutor(),
        model_cache=provider,
        build_archive_available=lambda _digest, _size: True,
        memory_floor_bytes=50,
    )

    plan = service.preview(request, actor="admin")

    assert plan.allowed, [reason.code for reason in plan.blockers]
    copies = [phase for phase in plan.phases if phase.subphase == "target-copy"]
    cached = DistributionIdentity._cached_targets(plan, (nodes[0],))
    missing = 0 if held else manifest.expected_bytes
    assert plan.storage.missing_spark_bytes_by_node == {nodes[0]: missing}
    assert bool(copies) is not held
    assert cached == ((nodes[0],) if held else ())
    with sessions() as session:
        # A scan older than the freshness window says nothing any more.
        assert (
            spark_model_objects(
                session, nodes[0], now=NOW + INVENTORY_MAX_AGE + timedelta(seconds=1)
            )
            is None
        )
