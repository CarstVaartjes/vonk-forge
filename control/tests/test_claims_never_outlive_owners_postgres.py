"""A claim never outlives its owner; a claim whose owner has ended is released
or adopted by the next reconcile or admission that meets it.

Every claim table in ``claim_registry.CLAIMS`` has a case here. Each case
holds the scarce resource with a claim whose owner has ended (cancelled,
failed, succeeded, superseded, crashed) or never existed, runs the production
reconcile or admission that meets it, and then asserts behaviour only: the
claim no longer holds the resource, and a fresh request that needs that
resource is admitted. A wrong implementation that keeps such a claim (a
blocker that waits or refuses for ever) fails here.

The rule is symmetric: each table also has a case whose owner is live, and
there the same reconcile or admission keeps the claim. A sweep that reclaims
by age alone fails that case. Whether an owner ended is
``owner_liveness.owner``: every claim names the kinds that decide it.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import (
    DistributionObject,
    LifecycleState,
    ReservationState,
    canonical_message,
)
from vonk_control import journal_models
from vonk_control.agent_jobs import AgentJobService
from vonk_control.agent_jobs.contracts import CLAIM_LEASE_SECONDS
from vonk_control.artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactReferenceUnsettled,
    require_reference_open,
    reserve_removal,
)
from vonk_control.attempt_residues import AttemptResidueReconciler
from vonk_control.catalog_sync import CatalogSyncUnsettled
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
from vonk_control.owner_liveness import OwnerKind, OwnerRef, owner
from vonk_control.pki import IssuedCertificate
from vonk_control.platform_ports import ENDPOINT_HOST_PORTS
from vonk_control.recipe_operations.errors import RecipeRequestInvalid
from vonk_control.step_ca import StepCAIssuancePending

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
    assert set(_LIVE_CASES) == set(CLAIMS)


def test_every_claim_is_ended_by_an_owner_liveness_kind(postgres_engine) -> None:
    sessions = _sessions(postgres_engine)
    tables = {
        mapper.class_.__name__: getattr(mapper.class_, "__tablename__", None)
        for mapper in Base.registry.mappers
    }
    now = datetime.now(UTC)
    with sessions() as session:
        for table, claim in CLAIMS.items():
            assert tables[claim.model] == table
            assert claim.ended
            for kind in claim.ended:
                # A kind without an arm would be "unrecognised", hence live.
                gone = OwnerRef(kind, str(uuid.uuid4()))
                assert not owner(session, gone, now).live, (table, kind)


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


def _live_port_promise(tmp_path: Path, engine: Engine) -> None:
    """A live profile load keeps its port promise through the reconcile."""

    sessions, _profiles, planner, profile, api, headers, _nodes, _installed = (
        _ready_profile(tmp_path, engine)
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    held, _review = _load(profile, api, headers)

    AttemptResidueReconciler(
        sessions,
        abandon_never_installed=lifecycle.abandon_never_installed,
        removal=lifecycle,
        clock=lifecycle._clock,
    ).tick()

    with sessions() as session:
        states = set(
            session.scalars(
                select(ResourceReservation.state).where(
                    ResourceReservation.owner_id == held,
                    ResourceReservation.kind == "port",
                )
            )
        )
    assert states and states <= set(_HELD)


def _live_removal_gate(tmp_path: Path, engine: Engine) -> None:
    """A running model removal keeps its deletion fence."""

    sessions = _sessions(engine)
    now = datetime.now(UTC)
    identity = ArtifactIdentity("model-set", uuid.uuid4().hex * 2)
    owner_id = str(uuid.uuid4())
    with sessions.begin() as session:
        session.add(
            ModelCacheOperation(
                id=owner_id,
                request_key=str(uuid.uuid4()),
                kind="remove",
                state=LifecycleState.RUNNING.value,
                payload={},
                progress={},
                actor="operator",
                created_at=now,
                updated_at=now,
            )
        )
        reserve_removal(
            session,
            (identity,),
            owner_kind="model-cache-operation",
            owner_id=owner_id,
            fence=str(uuid.uuid4()),
            now=now,
        )
    ModelCacheService(
        sessions, tmp_path / "model-cache", reserve_bytes=0, fixture_sources=True
    ).reconcile_removal_gates()

    with sessions.begin() as session, pytest.raises(ArtifactReferenceUnsettled):
        require_reference_open(session, (identity,), now=now)


def _live_sync_slot(tmp_path: Path, engine: Engine) -> None:
    """A sync that is still heartbeating keeps the slot; a fresh sync waits."""

    from .test_catalog_sync import _empty_result, _fixture, _sync

    sessions, catalog, reader, _item = _fixture(tmp_path, engine=engine)
    sync = _sync(sessions, catalog, reader)
    started = sync._clock() - timedelta(hours=1)
    with sessions.begin() as session:
        running = RecipeLibrarySyncRun(
            request_key=str(uuid.uuid4()),
            trigger=CatalogSyncTrigger.AUTOMATIC,
            state=LifecycleState.RUNNING,
            active_slot="managed-recipes",
            repository="CarstVaartjes/vonk-forge-recipes",
            result=_empty_result().model_dump(mode="json"),
            actor="test",
            created_at=started,
            started_at=started,
            heartbeat_at=sync._clock() - timedelta(minutes=1),
        )
        session.add(running)

    with pytest.raises(CatalogSyncUnsettled):
        sync.sync(
            ManagedCatalogSyncRequest(
                request_key=str(uuid.uuid4()),
                trigger=CatalogSyncTrigger.MANUAL,
                actor="test",
            )
        )

    with sessions() as session:
        row = session.get(RecipeLibrarySyncRun, running.id)
        assert row is not None and row.active_slot == "managed-recipes"


def _live_agent_mutation_turn(tmp_path: Path, engine: Engine) -> None:
    """A node's mutation turn stays with the order whose attempt is live."""

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
    fresh = jobs.enqueue(
        parent_job(sessions, clock).id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
    )

    jobs.reconcile_orders()

    claimed = claim_agent(jobs, NODE_A, "serial-a")
    assert claimed is None or fenced_operation(sessions, claimed).id != fresh.id
    with sessions() as session:
        assert owner(
            session, OwnerRef(OwnerKind.AGENT_ATTEMPT, held.id), clock.now
        ).live


def _live_distribution_grant(tmp_path: Path, engine: Engine) -> None:
    """A grant whose plan an active operation owns stays authorized."""

    sessions = _sessions(engine)
    now = datetime.now(UTC)
    node_id = "spk_" + "a" * 32
    image = "sha256:" + "e" * 64
    held = DistributionObject(
        name="weights/model.bin", sha256="a" * 64, bytes=10, kind="model"
    )
    payload = {"plan_digest": "f" * 64}
    with sessions.begin() as session:
        session.add(AgentNode(node_id=node_id, state="active"))
        session.add(
            Job(
                request_id=str(uuid.uuid4()),
                kind="artifact-distribution",
                state=LifecycleState.RUNNING.value,
                actor="operator",
                authority_revision="f" * 64,
                targets=[node_id],
                payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
                payload=payload,
                current_attempt=0,
                created_at=now,
                updated_at=now,
            )
        )
    source = MemoryObjectSource({"a" * 64: b"x" * 10})
    source.register_artifact_set("d" * 64, (held,))
    source.register_runtime_image(image, "c" * 64)
    distribution = DistributionService(source, clock=lambda: now, sessions=sessions)
    grant = NodeDistributionAssignment.parse(
        {
            "assignment_id": str(uuid.uuid4()),
            "plan_digest": "f" * 64,
            "generation": 1,
            "node_id": node_id,
            "expires_at": (now + timedelta(hours=1)).isoformat(),
            "model_artifact_set_sha256": "d" * 64,
            "objects": [held.to_mapping()],
            "oci_image_digest": image,
            "oci_image_config_digest": "sha256:" + "9" * 64,
            "oci_archive_sha256": "c" * 64,
        }
    )
    distribution.register(grant)

    AttemptResidueReconciler(
        sessions, abandon_never_installed=lambda _id: None, clock=lambda: now
    ).tick()

    assert (
        distribution.authorize(node_id=node_id, plan_digest="f" * 64).assignment_id
        == grant.assignment_id
    )


def _live_certificate_rotation(tmp_path: Path, engine: Engine) -> None:
    """A renewal the agent keeps asking for survives the revocation sweep.

    The provider's issuance is pending; the agent asks again within the
    issuance window each time. The sweep runs long after the intent was
    created, but while its renewal is still touching it.
    """

    from vonk_control.ca_issuance_contract import CertificateIssuanceBinding

    from .test_enrollment import (
        NODE_ID,
        Clock,
        RecordingAuthority,
        _fixture_certificate,
        csr,
        enroll,
    )

    class PendingAuthority(RecordingAuthority):
        def renew_node(self, node_id, public_key_pem, now, *, request):
            self.renew_request_ids.append(request.request_id)
            self._begin(request)
            raise StepCAIssuancePending("certificate.issuance_in_progress")

    sessions = _sessions(engine)
    clock = Clock()
    authority = PendingAuthority()
    service = EnrollmentService(sessions, authority, clock=clock)
    source = enroll(service)
    request = csr()
    for _poll in range(3):
        assert not isinstance(
            service.renew(NODE_ID, source.serial, request), IssuedCertificate
        )
        clock.advance(seconds=200)

    service.reconcile_revocations()

    with sessions() as session:
        intent = session.get(AgentCertificateRotation, NODE_ID)
        assert intent is not None
        binding = CertificateIssuanceBinding.model_validate(intent.provider_request)
        pending_csr = intent.csr_pem.encode("ascii")
    authority._finish(binding, _fixture_certificate(NODE_ID, pending_csr, binding))
    renewed = service.renew(NODE_ID, source.serial, request)
    assert isinstance(renewed, IssuedCertificate)
    assert set(authority.renew_request_ids) == {binding.request_id}


def _live_image_build(tmp_path: Path, engine: Engine) -> None:
    """A fresh request for the same input adopts the live build Job."""

    from .test_build_cancellation_recovery import _services

    *_rest, operations, _storage, _now, _node, _revision, plan = _services(
        tmp_path, engine
    )
    held, again = (
        operations.build(
            plan,
            build_input_sha256=plan.build_input_sha256,
            actor="operator",
            request_id=str(uuid.uuid4()),
        )
        for _request in range(2)
    )
    assert again.id == held.id


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


_LIVE_CASES: dict[str, Callable[[Path, Engine], None]] = {
    "resource_reservations": _live_port_promise,
    "artifact_lifecycle_gates": _live_removal_gate,
    "recipe_library_sync_runs": _live_sync_slot,
    "agent_operation_attempts": _live_agent_mutation_turn,
    "artifact_distribution_assignments": _live_distribution_grant,
    "agent_certificate_rotations": _live_certificate_rotation,
    "recipe_builds": _live_image_build,
}


@pytest.mark.parametrize("table", sorted(_LIVE_CASES))
@pytest.mark.usefixtures("damaged_json_rows", signed_recipe_releases.__name__)
def test_a_claim_whose_owner_is_live_is_kept(
    tmp_path: Path, postgres_engine: Engine, table: str
) -> None:
    _LIVE_CASES[table](tmp_path, postgres_engine)
