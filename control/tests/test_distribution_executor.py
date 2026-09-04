from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from vonk_agent_protocol import DistributionObject
from vonk_control.distribution import DistributionService, MemoryVerifiedObjectSource
from vonk_control.distribution_executor import DurableDistributionPhaseExecutor
from vonk_control.models import AgentOperation, AgentOperationAttempt, Job

from .test_agent_api import NODE_A, NODE_B, agent_system


def _target(node: str, *, image: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        node_id=node,
        state="ready",
        verified_sha256=("e" * 64 if image else "b" * 64),
        imported_image_digest=("sha256:" + "d" * 64 if image else None),
        verified_at=datetime.now(UTC),
    )


def test_complete_two_node_distribution_is_a_verified_skip() -> None:
    nodes = ("spk_" + "a" * 32, "spk_" + "b" * 32)
    preparation = SimpleNamespace(
        model=SimpleNamespace(artifact_set_sha256="b" * 64, artifact_set_bytes=0, targets=[_target(node) for node in nodes]),
        runtime_image=SimpleNamespace(
            image_digest="sha256:" + "d" * 64,
            oci_layout_sha256="e" * 64,
            image_bytes=0,
            targets=[_target(node, image=True) for node in nodes],
        ),
    )
    plan = SimpleNamespace(
        preparation=preparation,
        storage=SimpleNamespace(artifact_digests=["c" * 64]),
        image_digest="sha256:" + "d" * 64,
        build=SimpleNamespace(oci_layout_sha256="e" * 64, image_bytes=11),
        recipe_build_id=None,
    )
    phase = SimpleNamespace(kind="transfer", node_ids=list(nodes), index=0)
    executor = DurableDistributionPhaseExecutor(None, None, None, clock=lambda: datetime.now(UTC))
    result = executor.execute(plan, phase, item_index=0, actor="test", request_key="00000000-0000-4000-8000-000000000001", progress={})
    assert result.operation_id is None
    assert result.result == {"skipped": True, "verified": False, "verified_digests": ["c" * 64], "verified_image_digest": "sha256:" + "d" * 64, "verified_oci_layout_sha256": "e" * 64, "cached_nodes": list(nodes), "cached_target_totals": {node: 0 for node in nodes}}


def test_partial_child_replays_and_aggregates_cached_target(agent_system) -> None:
    _client, services, _tokens, clock = agent_system
    model = DistributionObject("weights/model.bin", "a" * 64, 10, "model")
    config = DistributionObject("config/tokenizer.json", "b" * 64, 5, "model")
    archive = DistributionObject("image.oci.tar", "c" * 64, 11, "oci-archive")
    source = MemoryVerifiedObjectSource({"a" * 64: b"x" * 10, "b" * 64: b"y" * 5, "c" * 64: b"z" * 11})
    source.register_artifact_set("d" * 64, (model, config))
    source.register_runtime_image("sha256:" + "e" * 64, archive.sha256)
    distribution = DistributionService(source, clock=clock, sessions=services.sessions)
    executor = DurableDistributionPhaseExecutor(services.sessions, services.operations, distribution, clock=clock)
    executor._model_objects = lambda _plan: (model, config)
    executor._archive = lambda _plan, **_kwargs: archive
    targets = [
        SimpleNamespace(node_id=NODE_A, state="preparing", verified_sha256=None, imported_image_digest=None, verified_at=None),
            SimpleNamespace(node_id=NODE_B, state="ready", verified_sha256="d" * 64, imported_image_digest=None, verified_at=datetime.now(UTC)),
    ]
    image_targets = [
        SimpleNamespace(node_id=NODE_A, state="preparing", verified_sha256=None, imported_image_digest=None, verified_at=None),
            SimpleNamespace(node_id=NODE_B, state="ready", verified_sha256="c" * 64, imported_image_digest="sha256:" + "e" * 64, verified_at=datetime.now(UTC)),
    ]
    preparation = SimpleNamespace(
        model=SimpleNamespace(artifact_set_sha256="d" * 64, artifact_set_bytes=15, targets=targets),
            runtime_image=SimpleNamespace(image_digest="sha256:" + "e" * 64, oci_layout_sha256="c" * 64, image_bytes=11, build_id=None, targets=image_targets),
    )
    plan = SimpleNamespace(
        preparation=preparation,
        storage=SimpleNamespace(artifact_digests=["a" * 64, "b" * 64]),
        image_digest=None,
        build=SimpleNamespace(oci_layout_sha256=None, image_bytes=None),
        recipe_build_id=None,
        recipe_revision_id=None,
        generated_at=clock.now,
        plan_digest="f" * 64,
        mapping=None,
    )
    phase = SimpleNamespace(kind="transfer", node_ids=[NODE_A, NODE_B], index=0)
    build_progress = {"phase_results": [{"build_id": str(uuid4()), "image_digest": "sha256:" + "e" * 64, "oci_layout_sha256": "c" * 64, "image_bytes": 11}]}
    first = executor.execute(plan, phase, item_index=0, actor="test", request_key="00000000-0000-4000-8000-000000000001", progress=build_progress)
    assert first.operation_id is not None
    with services.sessions.begin() as session:
        child = session.get(Job, first.operation_id)
        assert child is not None
        operation = next(iter(child.payload["assignments"].values()))
        assert operation["assignment_id"]
        stored = session.query(AgentOperation).filter_by(parent_job_id=child.id).one()
        assert stored.payload["distribution_assignment"]["assignment_id"] == operation["assignment_id"]
        stored.state = "succeeded"
        stored.current_attempt = 1
        session.add(AgentOperationAttempt(
            operation_id=stored.id,
            attempt=1,
            fence=str(uuid4()),
            lease_deadline=clock.now,
            agent_certificate_serial="serial-a",
            state="succeeded",
            progress={"bytes": 26, "total_bytes": 26},
            result={
                "verified": True,
                "verified_digests": ["a" * 64, "b" * 64],
                "verified_image_digest": "sha256:" + "e" * 64,
                "imported_image_digest": "sha256:" + "e" * 64,
                "verified_oci_layout_sha256": "c" * 64,
            },
        ))
    view = executor.get(first.operation_id)
    assert view.state == "succeeded"
    assert [member["node_id"] for member in view.result["members"]] == [NODE_A, NODE_B]
    assert view.result["progress"]["completed_bytes"] == 52
    assert view.result["progress"]["total_bytes"] == 52
    replay = executor.execute(plan, phase, item_index=0, actor="test", request_key="00000000-0000-4000-8000-000000000001", progress=build_progress)
    assert replay.operation_id == first.operation_id
    verify = executor.execute(plan, SimpleNamespace(kind="verify", node_ids=[NODE_A, NODE_B], index=1), item_index=0, actor="test", request_key="00000000-0000-4000-8000-000000000001", progress={"cached_nodes": [NODE_B], "evidence": view.result["evidence"]})
    assert verify.result["verified"] is True


def test_partial_child_failure_is_projected_after_aggregation(agent_system) -> None:
    _client, services, _tokens, clock = agent_system
    source = MemoryVerifiedObjectSource()
    distribution = DistributionService(source, clock=clock, sessions=services.sessions)
    executor = DurableDistributionPhaseExecutor(services.sessions, services.operations, distribution, clock=clock)
    # The failure path is intentionally checked at the durable projection
    # boundary; no fabricated verification receipt can make it succeed.
    with services.sessions.begin() as session:
        child = Job(
            id=str(uuid4()), request_id=str(uuid4()), kind="artifact-distribution", state="queued",
            actor="test", authority_revision="f" * 64, targets=[NODE_A], payload_digest="0" * 64,
            payload={"cached_nodes": [], "target_totals": {NODE_A: 26}}, result=None,
            created_at=clock.now, updated_at=clock.now,
        )
        session.add(child)
        session.flush()
        services.operations.enqueue_in_session(session, child.id, NODE_A, "artifact.distribution.v1", "f" * 64, {"schema_version": 1, "plan_digest": "f" * 64}, operation_id=str(uuid4()))
        operation = session.query(AgentOperation).filter_by(parent_job_id=child.id).one()
        operation.state = "failed"
        operation.current_attempt = 1
        session.add(AgentOperationAttempt(
            operation_id=operation.id, attempt=1, fence=str(uuid4()), lease_deadline=clock.now,
            agent_certificate_serial="serial-a", state="failed", progress={"bytes": 2, "total_bytes": 26},
            result={"reason": "digest mismatch"},
        ))
        child_id = child.id
    view = executor.get(child_id)
    assert view.state == "failed"
    assert view.result["members"][0]["error"] == "digest mismatch"
