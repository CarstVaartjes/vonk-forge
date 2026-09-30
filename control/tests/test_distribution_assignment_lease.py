"""Distribution grants follow the transfer that uses them, not the plan's age.

A Run/Switch plan can be accepted hours before its target copy starts (image
preparation, compile retries), and a failed switch is retried by a successor
that re-plans to the same plan digest. The durable (plan digest, node) grant
must serve both: fresh when the transfer registers it, and renewed or reclaimed
by the successor instead of refusing it as "already bound".
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from vonk_agent_protocol import DistributionObject
from vonk_control.distribution import (
    DistributionError,
    DistributionService,
    MemoryObjectSource,
)
from vonk_control.distribution_executor import DurableDistributionPhaseExecutor
from vonk_control.models import AgentNode, ArtifactDistributionAssignment, Job

from .test_agent_api import NODE_A, NODE_B, agent_system  # noqa: F401
from .test_distribution_executor import _phase, _plan


def _register_grant_in_process(database_url, mapping, now):
    """Independent worker: PostgreSQL owns coordination across processes."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from vonk_control.distribution_assignment import NodeDistributionAssignment

    assignment = NodeDistributionAssignment.parse(mapping)
    source = MemoryObjectSource(
        {item.sha256: b"x" * item.bytes for item in assignment.objects}
    )
    source.register_artifact_set(
        assignment.model_artifact_set_sha256, assignment.objects
    )
    source.register_runtime_image(
        assignment.oci_image_digest, assignment.oci_archive_sha256
    )
    engine = create_engine(database_url)
    try:
        service = DistributionService(
            source,
            clock=lambda: datetime.fromisoformat(now),
            sessions=sessionmaker(engine),
        )
        service.register(assignment)
        return service.authorize(
            node_id=assignment.node_id, plan_digest=assignment.plan_digest
        ).assignment_id
    finally:
        engine.dispose()


def test_concurrent_processes_reclaim_a_grant_without_mutating_its_identity(
    tmp_path, postgres_engine
):
    # Break caught: renewal/reclaim updates a primary key instead of retiring
    # the old identity. A database guard models the immutable grant boundary;
    # two separate workers must converge on the same successor atomically.
    from concurrent.futures import ProcessPoolExecutor
    from multiprocessing import get_context

    from .test_agent_api import make_agent_system

    system = make_agent_system(tmp_path, engine=postgres_engine)
    transfer = _transfer(system)
    transfer.execute(transfer.clock.now)
    old = transfer.distribution.authorize(node_id=NODE_A, plan_digest="f" * 64)
    transfer.clock.now += timedelta(hours=2)
    successor = old.model_copy(
        update={
            "assignment_id": str(uuid4()),
            "expires_at": transfer.clock.now + timedelta(hours=1),
        }
    )
    with postgres_engine.begin() as connection:
        connection.exec_driver_sql("""
            CREATE FUNCTION forbid_grant_identity_update() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NEW.id <> OLD.id THEN RAISE EXCEPTION 'grant identity is immutable'; END IF;
            RETURN NEW; END $$
        """)
        connection.exec_driver_sql("""
            CREATE TRIGGER immutable_grant_identity BEFORE UPDATE ON artifact_distribution_assignments
            FOR EACH ROW EXECUTE FUNCTION forbid_grant_identity_update()
        """)
    arguments = (
        postgres_engine.url.render_as_string(hide_password=False),
        successor.to_mapping(),
        transfer.clock.now.isoformat(),
    )
    with ProcessPoolExecutor(max_workers=2, mp_context=get_context("spawn")) as pool:
        attempts = [
            pool.submit(_register_grant_in_process, *arguments) for _ in range(2)
        ]
        assert [attempt.result(timeout=15) for attempt in attempts] == [
            successor.assignment_id
        ] * 2
    with transfer.services.sessions() as session:
        assert session.get(ArtifactDistributionAssignment, old.assignment_id) is None
        row = session.get(ArtifactDistributionAssignment, successor.assignment_id)
        assert row is not None and row.state == "active"


MODEL = DistributionObject(
    name="weights/model.bin", sha256="a" * 64, bytes=10, kind="model"
)
ARCHIVE = DistributionObject(
    name="image.oci.tar", sha256="c" * 64, bytes=11, kind="oci-archive"
)


def _transfer(agent_system):  # noqa: F811
    _client, services, _tokens, clock = agent_system
    with services.sessions.begin() as session:
        for node_id in (NODE_A, NODE_B):
            node = session.get(AgentNode, node_id)
            assert node is not None
            node.workload_intent_ordinal = 7
    source = MemoryObjectSource({"a" * 64: b"x" * 10, "c" * 64: b"z" * 11})
    source.register_artifact_set("d" * 64, (MODEL,))
    source.register_runtime_image("sha256:" + "e" * 64, ARCHIVE.sha256)
    distribution = DistributionService(source, clock=clock, sessions=services.sessions)

    class Executor(DurableDistributionPhaseExecutor):
        def _model_objects(self, _plan, _progress):
            return (MODEL,), "d" * 64, 10

        def _archive(self, _plan, **_kwargs):
            return ARCHIVE

    executor = Executor(
        services.sessions, services.operations, distribution, clock=clock
    )
    phase = _phase(kind="transfer", node_ids=[NODE_A, NODE_B], index=0)

    def plan(generated_at: datetime):
        return _plan(
            preparation=None,
            storage=SimpleNamespace(artifact_digests=["a" * 64]),
            image_digest=None,
            build=SimpleNamespace(oci_layout_sha256="c" * 64, image_bytes=11),
            recipe_build_id=None,
            recipe_revision_id=None,
            generated_at=generated_at,
            # A successor re-plans the same effects: the digest excludes
            # generated_at, so both switches name the same grant.
            plan_digest="f" * 64,
            mapping=None,
        )

    def execute(accepted_at: datetime):
        return executor.execute(
            plan(accepted_at),
            phase,
            item_index=0,
            actor="test",
            request_key=str(uuid4()),
            progress={
                "workload_intent_ordinal": 7,
                "phase_results": [
                    {
                        "build_id": str(uuid4()),
                        "image_digest": "sha256:" + "e" * 64,
                        "oci_layout_sha256": "c" * 64,
                        "image_bytes": 11,
                    }
                ],
            },
        )

    return SimpleNamespace(
        services=services,
        clock=clock,
        distribution=distribution,
        execute=execute,
    )


def test_plan_accepted_hours_before_its_copy_still_grants_the_transfer(
    agent_system,  # noqa: F811
) -> None:
    """The GLM switch was accepted the evening before its target copy began."""

    transfer = _transfer(agent_system)
    started = transfer.execute(transfer.clock.now - timedelta(hours=3))
    assert started.operation_id is not None
    for node_id in (NODE_A, NODE_B):
        grant = transfer.distribution.authorize(node_id=node_id, plan_digest="f" * 64)
        assert grant.expires_at > transfer.clock.now


def test_successor_switch_reclaims_the_failed_switch_grant(
    agent_system,  # noqa: F811
) -> None:
    """A retried profile re-plans to the same digest and must bind again."""

    transfer = _transfer(agent_system)
    first = transfer.execute(transfer.clock.now)
    assert first.operation_id is not None
    with transfer.services.sessions.begin() as session:
        child = session.get(Job, first.operation_id)
        assert child is not None
        child.state = "failed"
    # The failed switch's grants lapse (or were marked expired on a refused
    # request) while the Controller retries the profile.
    transfer.clock.now += timedelta(hours=2)
    with pytest.raises(DistributionError, match="expired"):
        transfer.distribution.authorize(node_id=NODE_A, plan_digest="f" * 64)

    successor = transfer.execute(transfer.clock.now)
    assert successor.operation_id not in {None, first.operation_id}
    for node_id in (NODE_A, NODE_B):
        grant = transfer.distribution.authorize(node_id=node_id, plan_digest="f" * 64)
        assert grant.expires_at > transfer.clock.now
    with transfer.services.sessions() as session:
        rows = list(session.scalars(select(ArtifactDistributionAssignment)))
    assert sorted((row.node_id, row.state) for row in rows) == [
        (NODE_A, "active"),
        (NODE_B, "active"),
    ]


def test_successor_while_the_grant_is_still_live_renews_it(
    agent_system,  # noqa: F811
) -> None:
    transfer = _transfer(agent_system)
    transfer.execute(transfer.clock.now)
    transfer.clock.now += timedelta(minutes=20)
    successor = transfer.execute(transfer.clock.now)
    assert successor.operation_id is not None
    grant = transfer.distribution.authorize(node_id=NODE_A, plan_digest="f" * 64)
    assert grant.expires_at > transfer.clock.now + timedelta(minutes=50)


def test_a_live_grant_for_different_bytes_is_still_refused(
    agent_system,  # noqa: F811
) -> None:
    """Renewal is for the same grant; different live bytes stay fail-closed."""

    transfer = _transfer(agent_system)
    transfer.execute(transfer.clock.now)
    other = DistributionObject(
        name="weights/model.bin", sha256="b" * 64, bytes=10, kind="model"
    )
    transfer.distribution.source.register_artifact_set("9" * 64, (other,))
    grant = transfer.distribution.authorize(node_id=NODE_A, plan_digest="f" * 64)
    changed = grant.parse(
        {
            **grant.to_mapping(),
            "model_artifact_set_sha256": "9" * 64,
            "objects": [other.to_mapping(), ARCHIVE.to_mapping()],
        }
    )
    with pytest.raises(DistributionError, match="already bound"):
        transfer.distribution.register(changed)
