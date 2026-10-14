"""A claim never outlives its owner; a claim whose owner has ended is released
or adopted by the next reconcile or admission that meets it.

Every claim table in ``claim_registry.CLAIMS`` has a case here. Each case
holds the scarce resource with a claim whose owner has ended (cancelled,
failed, succeeded, superseded, crashed) or never existed, runs the production
reconcile or admission that meets it, and then asserts behaviour only: the
claim no longer holds the resource, and a fresh request that needs that
resource is admitted. A wrong implementation that keeps such a claim (a
blocker that waits or refuses for ever) fails here.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import DistributionObject, LifecycleState, ReservationState
from vonk_control import journal_models
from vonk_control.agent_jobs import AgentJobService
from vonk_control.agent_jobs.contracts import CLAIM_LEASE_SECONDS
from vonk_control.artifact_lifecycle import (
    ArtifactIdentity,
    require_reference_open,
    reserve_removal,
)
from vonk_control.attempt_residues import AttemptResidueReconciler
from vonk_control.catalog_sync_contract import (
    CatalogSyncTrigger,
    ManagedCatalogSyncRequest,
)
from vonk_control.distribution import DistributionService, MemoryObjectSource
from vonk_control.distribution_assignment import NodeDistributionAssignment
from vonk_control.enrollment import EnrollmentService
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import (
    AgentCertificate,
    AgentCertificateRotation,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    ArtifactLifecycleGate,
    Base,
    CatalogDocumentRevision,
    FleetProfileApplication,
    Job,
    ModelCacheOperation,
    RecipeLibrarySyncRun,
    ResourceReservation,
)
from vonk_control.pki import IssuedCertificate
from vonk_control.platform_ports import ENDPOINT_HOST_PORTS
from vonk_control.recipe_operations.errors import RecipeRequestInvalid

from .agent_fences import fenced_operation
from .claim_registry import CLAIMS, NOT_CLAIMS, claim_shaped_tables
from .runtime_identity_support import claim_agent
from .signed_recipe_release import signed_recipe_releases
from .test_profile_port_claims import _load, _ready_profile

_HELD = (ReservationState.ACTIVE, ReservationState.PROMISED)
_ENDED = tuple(
    state.value
    for state in (
        LifecycleState.CANCELLED,
        LifecycleState.FAILED,
        LifecycleState.SUCCEEDED,
    )
)
_MISSING = "missing"
# The owner is live until the fresh request itself supersedes it.
_LIVE_PREDECESSOR = "superseded-by-fresh-request"
# A build cancelled mid-run whose Spark cleanup failed (its parent stays open).
_STOPPED_BUILD = "stopped-build-cleanup-failed"


def test_every_claim_shaped_table_is_registered() -> None:
    assert journal_models  # its tables join the metadata on import
    tables = set(Base.metadata.tables)
    shaped = claim_shaped_tables(Base.metadata)
    assert shaped - set(CLAIMS) - set(NOT_CLAIMS) == set(), (
        "register the new table in claim_registry: a claim with its owner and "
        "a case here, or NOT_CLAIMS with the reason it is not one"
    )
    assert set(CLAIMS) | set(NOT_CLAIMS) <= tables
    assert set(NOT_CLAIMS) <= shaped
    assert set(_CASES) == set(CLAIMS)


def _sessions(engine: Engine) -> sessionmaker:
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _port_promise(tmp_path: Path, engine: Engine, ending: str) -> None:
    """A profile load's port promise, when it is the last free endpoint port."""

    sessions, profiles, planner, profile, api, headers, nodes, installed = (
        _ready_profile(tmp_path, engine)
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    held, _review = _load(profile, api, headers)
    now = lifecycle._clock()
    with sessions.begin() as session:
        claims = tuple(
            session.scalars(
                select(ResourceReservation).where(ResourceReservation.owner_id == held)
            )
        )
        (port,) = {claim.resource_key for claim in claims if claim.kind == "port"}
        # Every other endpoint port is held by a live installation.
        for node_id in nodes:
            for other in {str(item) for item in ENDPOINT_HOST_PORTS} - {port}:
                session.add(
                    ResourceReservation(
                        node_id=node_id,
                        kind="port",
                        resource_key=other,
                        amount_bytes=0,
                        owner_kind="installation",
                        owner_id=installed,
                        state=ReservationState.ACTIVE,
                        plan_digest="d" * 64,
                        created_at=now,
                    )
                )
        if ending == _MISSING:
            for claim in claims:
                claim.owner_id = str(uuid.uuid4())
        elif ending != _LIVE_PREDECESSOR:
            application = session.get(FleetProfileApplication, held)
            assert application is not None
            application.state = ending
    wanted = profile
    if ending == _LIVE_PREDECESSOR:
        with sessions() as session:
            recipe = session.scalar(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == "recipe"
                )
            )
            assert recipe is not None
        wanted = profiles.create(
            FleetProfileInput.model_validate(
                {
                    "name": "Another profile on the same Sparks",
                    "assignments": [
                        {
                            "recipe_selector": f"{recipe.publisher}/{recipe.slug}",
                            "spark_ids": list(nodes),
                            "desired_state": "running",
                        }
                    ],
                }
            ),
            actor="admin",
        )

    AttemptResidueReconciler(
        sessions,
        abandon_never_installed=lifecycle.abandon_never_installed,
        removal=lifecycle,
        clock=lifecycle._clock,
    ).tick()

    fresh, _review = _load(wanted, api, headers)
    with sessions() as session:
        holders = set(
            session.scalars(
                select(ResourceReservation.owner_id).where(
                    ResourceReservation.kind == "port",
                    ResourceReservation.resource_key == port,
                    ResourceReservation.state.in_(_HELD),
                )
            )
        )
    assert holders == {fresh}


def _build_memory_claim(tmp_path: Path, engine: Engine, ending: str) -> None:
    """A build's memory claim, when the build stopped and its cleanup failed."""

    from .test_stopped_build_claims import stopped_build_releases_its_memory

    fresh = stopped_build_releases_its_memory(tmp_path, engine)
    assert fresh.state in {"queued", "running"}


def _reservation(tmp_path: Path, engine: Engine, ending: str) -> None:
    if ending == _STOPPED_BUILD:
        _build_memory_claim(tmp_path, engine, ending)
    else:
        _port_promise(tmp_path, engine, ending)


def _removal_gate(tmp_path: Path, engine: Engine, ending: str) -> None:
    """A model removal's deletion fence, when the removal ended without clearing it."""

    sessions = _sessions(engine)
    now = datetime.now(UTC)
    identity = ArtifactIdentity("model-set", uuid.uuid4().hex * 2)
    owner = str(uuid.uuid4())
    with sessions.begin() as session:
        if ending != _MISSING:
            session.add(
                ModelCacheOperation(
                    id=owner,
                    request_key=str(uuid.uuid4()),
                    kind="remove",
                    state=ending,
                    payload={},
                    progress={},
                    actor="operator",
                    created_at=now,
                    updated_at=now,
                    completed_at=now,
                )
            )
        reserve_removal(
            session,
            (identity,),
            owner_kind="model-cache-operation",
            owner_id=owner,
            fence=str(uuid.uuid4()),
            now=now,
        )
    service = ModelCacheService(
        sessions, tmp_path / "model-cache", reserve_bytes=0, fixture_sources=True
    )

    service.reconcile_removal_gates()

    with sessions.begin() as session:
        require_reference_open(session, (identity,), now=now)
        gate = session.get(ArtifactLifecycleGate, (identity.kind, identity.sha256))
        assert gate is not None and gate.removal_owner_id is None


def _sync_slot(tmp_path: Path, engine: Engine, ending: str) -> None:
    """The managed-library sync slot, when the sync holding it crashed."""

    from .test_catalog_sync import _empty_result, _fixture, _sync

    sessions, catalog, reader, _item = _fixture(tmp_path, engine=engine)
    sync = _sync(sessions, catalog, reader)
    started = sync._clock() - timedelta(hours=1)
    with sessions.begin() as session:
        crashed = RecipeLibrarySyncRun(
            request_key=str(uuid.uuid4()),
            trigger=CatalogSyncTrigger.AUTOMATIC,
            state=LifecycleState.RUNNING,
            active_slot="managed-recipes",
            repository="CarstVaartjes/vonk-forge-recipes",
            result=_empty_result().model_dump(mode="json"),
            actor="test",
            created_at=started,
            started_at=started,
            heartbeat_at=started,
        )
        session.add(crashed)

    fresh = sync.sync(
        ManagedCatalogSyncRequest(
            request_key=str(uuid.uuid4()),
            trigger=CatalogSyncTrigger.MANUAL,
            actor="test",
        )
    )

    assert fresh.completed_at is not None
    with sessions() as session:
        row = session.get(RecipeLibrarySyncRun, crashed.id)
        assert row is not None and row.active_slot is None


def _agent_mutation_turn(tmp_path: Path, engine: Engine, ending: str) -> None:
    """A node's mutation turn, when the agent order holding it lost its attempt."""

    from .test_agent_jobs_postgres import COMMIT, NODE_A, NODE_B, STOP_PAYLOAD, Clock
    from .test_agent_jobs_postgres import parent as parent_job

    sessions = _sessions(engine)
    clock = Clock()
    with sessions.begin() as session:
        for node_id in (NODE_A, NODE_B):
            session.add(
                AgentNode(node_id=node_id, state="active", workload_intent_ordinal=1)
            )
        session.flush()
        session.add(
            AgentCertificate(
                serial="serial-a",
                node_id=NODE_A,
                not_before=clock.now - timedelta(seconds=1),
                not_after=clock.now + timedelta(hours=3),
                fingerprint="fingerprint-serial-a",
            )
        )
    jobs = AgentJobService(sessions, clock=clock)
    held = jobs.enqueue(
        parent_job(sessions, clock).id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
    )
    assert claim_agent(jobs, NODE_A, "serial-a") is not None
    if ending == _MISSING:
        with sessions.begin() as session:
            session.execute(
                delete(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == held.id
                )
            )
    else:
        clock.advance(seconds=CLAIM_LEASE_SECONDS + 1)
    fresh = jobs.enqueue(
        parent_job(sessions, clock).id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
    )

    jobs.reconcile_orders()

    claimed = claim_agent(jobs, NODE_A, "serial-a")
    assert claimed is not None and fenced_operation(sessions, claimed).id == fresh.id


def _distribution_grant(tmp_path: Path, engine: Engine, ending: str) -> None:
    """A node's grant for a plan, when no operation owns the plan any more."""

    sessions = _sessions(engine)
    now = datetime.now(UTC)
    node_id = "spk_" + "a" * 32
    image = "sha256:" + "e" * 64
    held, wanted = (
        DistributionObject(
            name="weights/model.bin", sha256=digit * 64, bytes=10, kind="model"
        )
        for digit in "ab"
    )
    with sessions.begin() as session:
        session.add(AgentNode(node_id=node_id, state="active"))
    source = MemoryObjectSource({"a" * 64: b"x" * 10, "b" * 64: b"y" * 10})
    source.register_artifact_set("d" * 64, (held,))
    source.register_artifact_set("9" * 64, (wanted,))
    source.register_runtime_image(image, "c" * 64)
    distribution = DistributionService(source, clock=lambda: now, sessions=sessions)

    def grant(
        artifact_set: str, item: DistributionObject
    ) -> NodeDistributionAssignment:
        return NodeDistributionAssignment.parse(
            {
                "assignment_id": str(uuid.uuid4()),
                "plan_digest": "f" * 64,
                "generation": 1,
                "node_id": node_id,
                "expires_at": (now + timedelta(hours=1)).isoformat(),
                "model_artifact_set_sha256": artifact_set,
                "objects": [item.to_mapping()],
                "oci_image_digest": image,
                "oci_image_config_digest": "sha256:" + "9" * 64,
                "oci_archive_sha256": "c" * 64,
            }
        )

    distribution.register(grant("d" * 64, held))
    fresh = grant("9" * 64, wanted)

    AttemptResidueReconciler(
        sessions, abandon_never_installed=lambda _id: None, clock=lambda: now
    ).tick()

    distribution.register(fresh)
    assert (
        distribution.authorize(node_id=node_id, plan_digest="f" * 64).assignment_id
        == fresh.assignment_id
    )


def _certificate_rotation(tmp_path: Path, engine: Engine, ending: str) -> None:
    """A node's renewal slot, when the process renewing it died mid-issuance."""

    from .test_enrollment import NODE_ID, Clock, RecordingAuthority, csr, enroll

    sessions = _sessions(engine)
    clock = Clock()
    authority = RecordingAuthority()
    source = enroll(EnrollmentService(sessions, authority, clock=clock))
    authority.renew_error = SystemExit("process death after the provider request")
    with pytest.raises(SystemExit):
        EnrollmentService(sessions, authority, clock=clock).renew(
            NODE_ID, source.serial, csr()
        )
    with sessions() as session:
        assert session.get(AgentCertificateRotation, NODE_ID) is not None
    authority.renew_error = None
    clock.advance(seconds=int(timedelta(minutes=5).total_seconds()) + 1)
    restarted = EnrollmentService(sessions, authority, clock=clock)

    restarted.reconcile_revocations()

    renewed = restarted.renew(NODE_ID, source.serial, csr())
    assert isinstance(renewed, IssuedCertificate)


def _image_build(tmp_path: Path, engine: Engine, ending: str) -> None:
    """A builder's build of one exact input, when the build Job ended."""

    from .test_build_cancellation_recovery import _services

    sessions, _builds, operations, _storage, _now, _node, _revision, plan = _services(
        tmp_path, engine
    )
    held = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="operator",
        request_id=str(uuid.uuid4()),
    )
    with sessions.begin() as session:
        if ending == _MISSING:
            session.execute(
                delete(AgentOperation).where(AgentOperation.parent_job_id == held.id)
            )
            session.execute(delete(Job).where(Job.id == held.id))
        else:
            job = session.get(Job, held.id)
            assert job is not None
            job.state = ending

    fresh = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="operator",
        request_id=str(uuid.uuid4()),
    )
    assert fresh.id != held.id


_Case = Callable[[Path, Engine, str], None]
_CASES: dict[str, tuple[_Case, tuple[str, ...]]] = {
    "resource_reservations": (
        _reservation,
        (
            *_ENDED,
            LifecycleState.SUPERSEDED.value,
            _MISSING,
            _LIVE_PREDECESSOR,
            _STOPPED_BUILD,
        ),
    ),
    "artifact_lifecycle_gates": (_removal_gate, (*_ENDED, _MISSING)),
    "recipe_library_sync_runs": (_sync_slot, ("crashed",)),
    "agent_operation_attempts": (_agent_mutation_turn, ("lease-lapsed", _MISSING)),
    "artifact_distribution_assignments": (_distribution_grant, ("no-owner",)),
    "agent_certificate_rotations": (_certificate_rotation, ("crashed",)),
    "recipe_builds": (_image_build, (*_ENDED[:2], _MISSING)),
}
# Gaps the guard found. Each is strict: it fails here once the gap is fixed.
_GAPS: dict[str, str] = {}


@pytest.mark.parametrize(
    ("table", "ending"),
    [
        pytest.param(
            table,
            ending,
            id=f"{table}-{ending}",
            marks=[
                pytest.mark.xfail(
                    strict=True, raises=RecipeRequestInvalid, reason=_GAPS[table]
                )
            ]
            if table in _GAPS
            else [],
        )
        for table, (_case, endings) in _CASES.items()
        for ending in endings
    ],
)
@pytest.mark.usefixtures("damaged_json_rows", signed_recipe_releases.__name__)
def test_a_claim_whose_owner_ended_never_blocks_a_fresh_request(
    tmp_path: Path, postgres_engine: Engine, table: str, ending: str
) -> None:
    case, _endings = _CASES[table]
    case(tmp_path, postgres_engine, ending)
