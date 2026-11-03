"""A running container nothing owns is stopped under a grant, never kept.

The Controller signs the stop of a Spark's own container when it wants the run
on no live claim: not wanted on that Spark and owned by no live operation. The
disposition answer reads the same rule, so the two can never disagree.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from vonk_agent_protocol import SignedHostHelperGrant
from vonk_agent_protocol.host_helper import (
    ExecuteContainerRuntimeRequestOperation,
    host_helper_grant_signing_bytes,
)
from vonk_control.host_helper_authority import (
    HostHelperAuthorityError,
    HostHelperGrantIssuer,
    HostRuntimeAuthorityService,
)
from vonk_control.models import AgentCertificate, Job, RecipeRun, RunNode

from .test_agent_api import NODE_A, NODE_B, agent_headers, agent_system  # noqa: F401

pytestmark = pytest.mark.usefixtures("damaged_json_rows")

INSTALLATION = "c0000000-0000-4000-8000-00000000000c"
GENERATION = 4


@pytest.fixture
def world(agent_system):  # noqa: F811
    client, services, _, clock = agent_system
    signer = HostHelperGrantIssuer(ed25519.Ed25519PrivateKey.generate(), clock=clock)
    object.__setattr__(
        services,
        "host_runtime_authority",
        HostRuntimeAuthorityService(services.sessions, signer, clock=clock),
    )
    return client, services, clock, signer


def _run(
    services,
    clock,
    *,
    state: str | None,
    on: tuple[str, ...] = (NODE_A,),
    live_operation: bool = False,
    remapped: bool = False,
) -> str:
    """One run row (none when ``state`` is None) assigned to the nodes ``on``."""

    run_id = str(uuid.uuid4())
    if state is not None:
        with services.sessions.begin() as session:
            session.add(
                RecipeRun(
                    id=run_id,
                    installation_id=str(uuid.uuid4()),
                    mapping_id=str(uuid.uuid4()),
                    mapping_generation=1,
                    run_generation=GENERATION,
                    alias="orphan",
                    plan_digest="1" * 64,
                    plan={"schema_version": 1},
                    state=state,
                    route_state="withdrawn",
                    actor="admin",
                    created_at=clock.now,
                    updated_at=clock.now,
                )
            )
            session.flush()
            for rank, node_id in enumerate(on):
                session.add(
                    RunNode(
                        run_id=run_id,
                        node_id=node_id,
                        rank=rank,
                        role="worker",
                        state=state,
                        port=8000 + rank,
                        reserved_memory_bytes=0,
                        updated_at=clock.now,
                    )
                )
    if remapped and state is not None:
        # The installation moved on: a newer run now holds it on node A.
        with services.sessions.begin() as session:
            first = session.get(RecipeRun, run_id)
            assert first is not None
            session.add(
                RecipeRun(
                    id=str(uuid.uuid4()),
                    installation_id=first.installation_id,
                    mapping_id=str(uuid.uuid4()),
                    mapping_generation=2,
                    run_generation=GENERATION + 1,
                    alias="orphan",
                    plan_digest="1" * 64,
                    plan={"schema_version": 1},
                    state="running",
                    route_state="withdrawn",
                    actor="admin",
                    created_at=clock.now,
                    updated_at=clock.now,
                )
            )
    if live_operation:
        with services.sessions.begin() as session:
            session.add(
                Job(
                    id=str(uuid.uuid4()),
                    request_id=str(uuid.uuid4()),
                    kind="recipe.stop",
                    state="running",
                    actor="admin",
                    authority_revision="r1",
                    targets=[NODE_A],
                    payload_digest="2" * 64,
                    payload={"owner_kind": "run", "owner_id": run_id},
                    created_at=clock.now,
                    updated_at=clock.now,
                )
            )
    return run_id


def _grant(client, run_id: str, *, node: str = NODE_A, **overrides: object):
    serial = "serial-a" if node == NODE_A else "serial-b"
    body = {
        "fence": str(uuid.uuid4()),
        "expires_in_seconds": 30,
        "action": "stop",
        "request_sha256": hashlib.sha256(b"request").hexdigest(),
        "stop_plan_sha256": hashlib.sha256(b"stop").hexdigest(),
        "run_generation": GENERATION,
        "runtime_run_id": run_id,
        "runtime_target_id": run_id,
        "runtime_installation_id": INSTALLATION,
    } | overrides
    return client.post(
        f"/agent/recipe-runs/{run_id}/orphan-stop-grant",
        headers=agent_headers(node, serial),
        json=body,
    )


def _unowned(client, run_id: str, node: str) -> bool:
    serial = "serial-a" if node == NODE_A else "serial-b"
    response = client.get(
        f"/agent/recipe-runs/{run_id}/disposition",
        headers=agent_headers(node, serial),
    )
    assert response.status_code == 204
    return "x-vonk-recipe-run-disposition" in response.headers


@pytest.mark.parametrize(
    "state, on, remapped",
    [
        pytest.param(None, (NODE_A,), False, id="run-row-deleted"),
        pytest.param("failed", (NODE_A,), True, id="failed-installation-moved-on"),
        pytest.param("stopped", (NODE_A,), False, id="stopped"),
        pytest.param("failed", (NODE_B,), False, id="failed-rank-removed"),
        pytest.param("running", (NODE_B,), False, id="running-elsewhere-only"),
    ],
)
def test_grant_is_issued_for_a_container_no_claim_owns(
    world, state, on, remapped
) -> None:
    client, services, clock, signer = world
    run_id = _run(services, clock, state=state, on=on, remapped=remapped)
    fence = str(uuid.uuid4())

    response = _grant(client, run_id, fence=fence)

    assert response.status_code == 200, response.text
    grant = SignedHostHelperGrant.parse(response.json()["grant"])
    operation = grant.claims.operation
    assert isinstance(operation, ExecuteContainerRuntimeRequestOperation)
    assert (
        operation.action,
        operation.fence,
        operation.runtime_run_id,
        operation.runtime_target_id,
        operation.runtime_installation_id,
        operation.run_generation,
    ) == ("stop", fence, run_id, run_id, INSTALLATION, GENERATION)
    assert grant.claims.node_id == NODE_A
    signer.public_key.verify(
        bytes.fromhex(grant.signature.value),
        host_helper_grant_signing_bytes(grant.claims),
    )


@pytest.mark.parametrize(
    "state", ["planned", "starting", "running", "stopping", "lost"]
)
def test_grant_is_refused_for_a_run_wanted_here(world, state) -> None:
    client, services, clock, _ = world
    run_id = _run(services, clock, state=state)

    response = _grant(client, run_id)

    assert response.status_code == 409
    assert "grant" not in response.json()


@pytest.mark.parametrize("state", ["failed", "stopped", None])
def test_grant_is_refused_while_a_live_operation_owns_the_run(world, state) -> None:
    client, services, clock, _ = world
    run_id = _run(services, clock, state=state, live_operation=True)

    assert _grant(client, run_id).status_code == 409


@pytest.mark.parametrize(
    "state, assigned",
    [
        (state, assigned)
        for state in (
            None,
            "planned",
            "starting",
            "running",
            "stopping",
            "lost",
            "failed",
            "stopped",
        )
        for assigned in ((NODE_A,), (NODE_B,), (NODE_A, NODE_B))
    ],
)
def test_disposition_and_grant_agree_for_every_node_and_run(
    world, state, assigned
) -> None:
    # One rule: a run is granted a stop exactly when its disposition is unowned.
    from vonk_control.untracked_installations import run_wanted_here

    client, services, clock, _ = world
    run_id = _run(services, clock, state=state, on=assigned)
    for node in (NODE_A, NODE_B):
        with services.sessions() as session:
            wanted = run_wanted_here(session, node, run_id)
        assert _unowned(client, run_id, node) is (not wanted)
        assert (_grant(client, run_id, node=node).status_code == 200) is (not wanted)


def test_grant_binding_must_name_this_run_and_a_stop(world) -> None:
    client, services, clock, _ = world
    run_id = _run(services, clock, state=None)

    other = str(uuid.uuid4())
    assert _grant(client, run_id, runtime_run_id=other).status_code == 422
    # A job container carries the run's label but is named by its job id.
    assert _grant(client, run_id, runtime_target_id=other).status_code == 422
    assert _grant(client, run_id, action="start").status_code == 422
    assert _grant(client, "not-a-run").status_code == 422


def test_grant_needs_the_active_node_certificate(world) -> None:
    _, services, clock, _ = world
    run_id = _run(services, clock, state=None)
    service = services.host_runtime_authority
    assert service is not None
    issue = service.issue_orphan_stop_grant
    kwargs = {
        "node_id": NODE_A,
        "run_id": run_id,
        "fence": str(uuid.uuid4()),
        "request_sha256": "a" * 64,
        "stop_plan_sha256": "b" * 64,
        "run_generation": GENERATION,
        "runtime_target_id": run_id,
        "runtime_installation_id": INSTALLATION,
    }
    # Node B's certificate cannot sign node A's stop.
    with pytest.raises(HostHelperAuthorityError):
        issue(certificate_serial="serial-b", **kwargs)
    with services.sessions.begin() as session:
        certificate = session.get(AgentCertificate, "serial-a")
        assert certificate is not None
        certificate.not_after = clock.now - timedelta(seconds=1)
    with pytest.raises(HostHelperAuthorityError):
        issue(certificate_serial="serial-a", **kwargs)
