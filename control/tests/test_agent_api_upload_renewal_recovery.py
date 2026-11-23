"""Agent API: upload renewal recovery."""

from __future__ import annotations

import json
import os
from datetime import timedelta
from http import HTTPStatus
from importlib.resources import files
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from hypothesis import example, given
from hypothesis import strategies as st
from sqlalchemy import event, select
from vonk_agent_protocol.agent_words import (
    AgentClientDecision,
)
from vonk_agent_protocol.contracts import AgentOperation as OperationKind
from vonk_agent_protocol.contracts import canonical_message
from vonk_agent_protocol.error_response import BoundedErrorResponse
from vonk_agent_protocol.http_failure import HttpTransient, TransientReason
from vonk_agent_protocol.inventory import (
    HostSettings,
    InventoryRequest,
)
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState, WaitReason
from vonk_agent_protocol.outcome import UnknownOutcomeError
from vonk_agent_protocol.reason_codes.admission import ControllerErrorCode
from vonk_agent_protocol.recipe_observations import RecipeRunObservationsWire
from vonk_agent_protocol.state_machines import RouteState
from vonk_control.agent_api.common import (
    _ENROLLMENT_TOKEN,
)
from vonk_control.agent_api.enrollment import _scan_enrollment_grants
from vonk_control.models.execution import RecipeRun, RunNode
from vonk_control.models.fleet import AgentCertificate
from vonk_control.models.operations import AgentOperation

from .agent_api_support import (
    NODE_A,
    NODE_C,
    STOP_PAYLOAD,
    _csr_for,
    _damage_storage_entry,
    agent_headers,
    make_agent_system,
    parent,
    upgrade_payload,
)
from .agent_jobs_support import canonical_start_payload


@pytest.mark.parametrize(
    "failure", ["denied-open", "denied-mkdir", "transient", "outside"]
)
def test_upload_storage_failure_preserves_agent_authority(
    tmp_path, monkeypatch, caplog, failure
):
    # Catches NAS storage faults incorrectly sending an agent to re-enrollment.
    from fastapi import HTTPException
    from vonk_control.agent_api.common import _prepare_recipe_image_upload

    root = tmp_path / "managed"
    outside = tmp_path / "outside"
    root.mkdir()
    if failure == "outside":
        outside.mkdir()
        (outside / ".identity.upload").write_bytes(b"untouched")
        root.rmdir()
        root.symlink_to(outside, target_is_directory=True)
    else:

        def unavailable(*args, **kwargs):
            raise (
                PermissionError("denied")
                if failure.startswith("denied")
                else OSError("I/O unavailable")
            )

        if failure == "denied-mkdir":
            monkeypatch.setattr(Path, "mkdir", unavailable)
        else:
            monkeypatch.setattr("vonk_control.agent_api.common.os.open", unavailable)
    with pytest.raises(HTTPException) as refused:
        _prepare_recipe_image_upload(root, "identity")
    assert refused.value.status_code == 503
    assert refused.value.headers is not None
    assert refused.value.headers["retry-after"]
    assert (
        HttpTransient.model_validate_json(json.dumps(refused.value.detail)).reason
        == TransientReason.STORAGE_UNAVAILABLE
    )
    if failure != "transient":
        assert "operator attention" in caplog.text
    if failure == "outside":
        assert (outside / ".identity.upload").read_bytes() == b"untouched"
        root.unlink()
    monkeypatch.undo()
    descriptor, temporary = _prepare_recipe_image_upload(root, "identity")
    os.close(descriptor)
    assert temporary.is_file() and not temporary.is_symlink()


@pytest.mark.parametrize("damage", ["symlink", "fifo"])
def test_upload_storage_changed_entry_retries_then_repairs(
    tmp_path, monkeypatch, damage
):
    # Catches a swap after cleanup producing a terminal 409 or blocking on a FIFO.
    from fastapi import HTTPException
    from vonk_control.agent_api import common

    outside = tmp_path / "outside"
    outside.write_bytes(b"untouched")
    original_remove = common._remove_unsafe_storage_entry

    def swap_after_removal(entry):
        original_remove(entry)
        _damage_storage_entry(entry, outside, damage)

    with monkeypatch.context() as patch:
        patch.setattr(common, "_remove_unsafe_storage_entry", swap_after_removal)
        with pytest.raises(HTTPException) as unavailable:
            common._prepare_recipe_image_upload(tmp_path, "identity")
    assert unavailable.value.status_code == 503
    assert unavailable.value.headers is not None
    assert unavailable.value.headers["retry-after"]
    assert (
        HttpTransient.model_validate_json(json.dumps(unavailable.value.detail)).reason
        == TransientReason.STORAGE_UNAVAILABLE
    )
    descriptor, temporary = common._prepare_recipe_image_upload(tmp_path, "identity")
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(b"verified archive")
    assert temporary.read_bytes() == b"verified archive"
    assert not temporary.is_symlink()
    assert outside.read_bytes() == b"untouched"


def test_publication_permission_failure_retries_then_succeeds(
    tmp_path, monkeypatch, caplog
):
    # Catches publication permission failures incorrectly revoking agent authority.
    from fastapi import HTTPException
    from vonk_control.agent_api import common

    temporary = tmp_path / ".identity.upload"
    temporary.write_bytes(b"verified archive")
    destination = tmp_path / "image"

    def denied(*args, **kwargs):
        raise PermissionError("denied")

    with monkeypatch.context() as patch:
        patch.setattr(common.os, "replace", denied)
        with pytest.raises(HTTPException) as unavailable:
            common._commit_recipe_image_upload(
                temporary, destination, expected_bytes=16
            )
    assert unavailable.value.status_code == 503
    assert unavailable.value.headers is not None
    assert unavailable.value.headers["retry-after"]
    assert (
        HttpTransient.model_validate_json(json.dumps(unavailable.value.detail)).reason
        == TransientReason.STORAGE_UNAVAILABLE
    )
    assert "operator attention" in caplog.text
    common._commit_recipe_image_upload(temporary, destination, expected_bytes=16)
    assert destination.read_bytes() == b"verified archive"


@pytest.mark.parametrize("failure", ["stat", "repair"])
def test_artifact_permission_failure_retries_after_storage_recovers(
    agent_system, monkeypatch, caplog, failure
):
    # Catches storage stat/repair failures triggering re-enrollment instead of recovery.
    from vonk_control.agent_api import common

    client, services, _, clock = agent_system
    digest = "a" * 64
    path = services.artifact_root / digest
    if failure == "repair":
        path.mkdir()
    else:
        path.write_bytes(b"artifact")
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "agent.upgrade.v1",
        "a" * 64,
        upgrade_payload(digest, len(b"artifact")),
    )
    original = (
        common.os.stat if failure == "stat" else common._remove_unsafe_storage_entry
    )

    def denied(entry, *args, **kwargs):
        if entry == path:
            raise PermissionError("denied")
        return original(entry, *args, **kwargs)

    with monkeypatch.context() as patch:
        if failure == "stat":
            patch.setattr(common.os, "stat", denied)
        else:
            patch.setattr(common, "_remove_unsafe_storage_entry", denied)
        unavailable = client.get(
            f"/agent/artifacts/{digest}", headers=agent_headers(NODE_A, "serial-a")
        )
    assert unavailable.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert unavailable.headers["retry-after"]
    assert (
        HttpTransient.model_validate_json(unavailable.text).reason
        == TransientReason.STORAGE_UNAVAILABLE
    )
    assert "operator attention" in caplog.text
    if failure == "repair":
        path.rmdir()
        path.write_bytes(b"artifact")
    assert (
        client.get(
            f"/agent/artifacts/{digest}", headers=agent_headers(NODE_A, "serial-a")
        ).status_code
        == 200
    )


def test_display_memory_agent_vectors_reobserve_through_http_store_and_cli(
    agent_system, capsys
):
    """Catches lost JSON enum evidence, sticky advice, or inventing unknown bytes."""
    from vonk_agent_protocol.reason_codes.profiles import ProjectionCode
    from vonk_control.fleet_projection.service import FleetProjection

    from cluster_profiles.cli_render.dispatch import render_payload

    client, services, _, clock = agent_system
    vectors = json.loads(
        files("vonk_agent_protocol").joinpath("vectors/display-memory.json").read_text()
    )
    projection = FleetProjection(services.sessions, clock=clock)
    baseline = InventoryRequest.model_validate_json(
        files("vonk_agent_protocol")
        .joinpath("vectors/inventory-wifi-link-v1.json")
        .read_bytes()
    )
    # Known default -> unavailable read -> firmware ready -> minimum after reboot.
    for index in (0, 2, 3, 1):
        host = HostSettings.model_validate_json(canonical_message(vectors[index]))
        clock.now += timedelta(seconds=2)
        payload = baseline.model_copy(
            update={"observed_at": clock.now, "host_settings": host}
        )
        response = client.post(
            "/agent/inventory",
            headers={
                **agent_headers(NODE_A, "serial-a"),
                "content-type": "application/json",
            },
            content=canonical_message(payload),
        )
        assert response.status_code == 204, response.text
        node = next(node for node in projection.read().nodes if node.id == NODE_A)
        assert node.inventory is not None
        assert node.inventory.host_settings == host
        advice = [
            warning
            for warning in node.warnings
            if warning.code == ProjectionCode.HOST_DISPLAY_MEMORY
        ]
        assert bool(advice) == (index in (0, 3))
        render_payload(
            node.model_dump(mode="json", exclude_none=True), "fleet", action="detail"
        )
        captured = capsys.readouterr()
        output = captured.out + captured.err
        observation = host.display_memory
        assert observation is not None
        if observation.carveout_bytes is None:
            assert "Display memory: reserved unknown" in output
        if observation.soc_firmware_version is not None:
            assert f"0x{observation.soc_firmware_version:08x}" in output
        assert ("fwupdmgr update" in output) == (index == 0)
        assert ("Display Reserved Memory to 0.5 GB" in output) == bool(advice)
        assert ("0.5 GB (fixed)" in output) == bool(advice)


def test_claim_stale_recipe_run_consumer_waits_then_admits(
    tmp_path,
    postgres_engine,
) -> None:
    """A lost run fence in the plain claim transaction must not become HTTP 500."""
    from .recipe_routes_support import setup

    client, services, _, clock = make_agent_system(tmp_path, engine=postgres_engine)
    routes, _, _, run_id = setup(tmp_path / "routes", engine=postgres_engine)

    # The spent-start result consumer shares _claim_once's plain transaction.
    # Like consume_agent_result's child-only write, it must fence the run too.
    def consume(session, _operation, _attempt, _message):
        run = session.get(RecipeRun, run_id)
        assert run is not None
        run.version += 1

    services.operations.set_result_consumer(consume)
    payload = canonical_start_payload(start_deadline=clock.now + timedelta(minutes=30))
    operation = services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        OperationKind.RECIPE_START,
        "a" * 64,
        payload,
    )
    headers = agent_headers(NODE_A, "serial-a")
    assert client.post("/agent/claim", headers=headers).status_code == 200
    clock.now += timedelta(minutes=31)

    conflicts = []

    def concurrent_writer(session, _context, _instances):
        if conflicts or not any(isinstance(row, RecipeRun) for row in session.dirty):
            return
        conflicts.append(run_id)
        with routes.sessions.begin() as winner:
            run = winner.get(RecipeRun, run_id)
            assert run is not None
            run.route_error = "concurrent accepted decision"

    event.listen(services.sessions, "before_flush", concurrent_writer)
    try:
        response = client.post("/agent/claim", headers=headers)
    finally:
        event.remove(services.sessions, "before_flush", concurrent_writer)
    assert conflicts == [run_id]
    assert response.status_code == 503
    assert response.headers["x-vonk-error-code"] == ControllerErrorCode.UNAVAILABLE
    assert int(response.headers["retry-after"]) > 0
    problem = BoundedErrorResponse.model_validate_json(response.content)
    assert problem.context is not None
    assert problem.context.decision == AgentClientDecision.RETRY
    assert problem.context.retryable is True
    with services.sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None and stored.state == LifecycleState.RUNNING
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        OperationKind.RECIPE_STOP,
        "a" * 64,
        STOP_PAYLOAD,
    )
    assert client.post("/agent/claim", headers=headers).status_code == 200


def test_recipe_observation_contention_waits_then_redelivery_commits(
    tmp_path,
    postgres_engine,
) -> None:
    """Continued contention must roll back observations and acknowledge redelivery."""
    from .recipe_routes_support import setup

    client, services, _, clock = make_agent_system(tmp_path, engine=postgres_engine)
    routes, _, _, run_id = setup(tmp_path / "routes", engine=postgres_engine)
    with services.sessions.begin() as session:
        rank = session.scalar(select(RunNode).where(RunNode.run_id == run_id).limit(1))
        assert rank is not None
        rank.node_id = NODE_A
        rank.updated_at = clock.now - timedelta(seconds=1)
        rank.observed_run_generation = None
        rank.observation_process_running = None
        rank.observation_observed_at = None
        run = session.get(RecipeRun, run_id)
        assert run is not None
        run.route_state = RouteState.WITHDRAWN
        run.next_action_at = clock.now
        generation = run.run_generation
    body = RecipeRunObservationsWire.model_validate(
        {
            "observed_at": clock.now,
            "runs": [
                {
                    "run_id": run_id,
                    "run_generation": generation,
                    "process_running": True,
                    "endpoint_ready": True,
                }
            ],
        }
    ).model_dump(mode="json")
    conflicts = []

    def concurrent_writer(session, _context, _instances):
        if not any(isinstance(row, RecipeRun) for row in session.dirty):
            return
        conflicts.append(run_id)
        with routes.sessions.begin() as winner:
            run = winner.get(RecipeRun, run_id)
            assert run is not None
            run.route_error = f"concurrent decision {len(conflicts)}"

    event.listen(services.sessions, "before_flush", concurrent_writer)
    try:
        response = client.post(
            "/agent/recipe-runs/observations",
            headers=agent_headers(NODE_A, "serial-a"),
            json=body,
        )
    finally:
        event.remove(services.sessions, "before_flush", concurrent_writer)
    assert len(conflicts) == 2  # transition re-read once, then yielded Stale.
    assert response.status_code == 503
    assert response.headers["x-vonk-error-code"] == ControllerErrorCode.UNAVAILABLE
    assert int(response.headers["retry-after"]) > 0
    with services.sessions() as session:
        rank = session.scalar(select(RunNode).where(RunNode.node_id == NODE_A))
        assert rank is not None and rank.observation_observed_at is None
    delivered = client.post(
        "/agent/recipe-runs/observations",
        headers=agent_headers(NODE_A, "serial-a"),
        json=body,
    )
    assert delivered.status_code == 204
    with services.sessions() as session:
        rank = session.scalar(select(RunNode).where(RunNode.node_id == NODE_A))
        assert rank is not None and rank.observation_process_running is True
        assert rank.observed_run_generation == generation


@pytest.mark.parametrize("action", ["succeed", "fail"])
def test_result_scope_changed_yields_without_outer_replay(
    agent_system, monkeypatch, action
):
    """SCOPE_CHANGED already exhausted transition's fresh decision; do not replay it."""
    _, services, _, _ = agent_system
    calls = []
    refused = UnknownOutcomeError("run changed", reason=WaitReason.SCOPE_CHANGED)

    def finish(*args, **kwargs):
        calls.append(args)
        raise refused

    monkeypatch.setattr(services.operations, "_finish", finish)
    with pytest.raises(UnknownOutcomeError) as caught:
        if action == "succeed":
            services.operations.succeed("unused-fence", {})
        else:
            services.operations.fail("unused-fence", "invalid result")
    assert caught.value is refused
    assert len(calls) == 1


def _renewable_source(services) -> None:
    with services.sessions.begin() as session:
        source = session.scalar(
            select(AgentCertificate).where(AgentCertificate.serial == "serial-a")
        )
        assert source is not None
        source.serial = "101"
        source.fingerprint = "fingerprint-101"


def _renewal_body() -> dict[str, str]:
    return {"node_id": NODE_A, "csr": _csr_for(NODE_A).decode()}


def test_internal_error_during_renewal_is_a_wait_not_a_fatal_refusal(
    agent_system, monkeypatch
) -> None:
    # Wrong implementation: any ValueError answered 403, a security verdict the
    # Spark backs off on for 30-600 s, for a Controller-side bug.
    client, services, _, _ = agent_system
    _renewable_source(services)

    def broken(*_args, **_kwargs):
        raise ValueError("internal decode failure")

    monkeypatch.setattr(services.enrollment, "renew", broken)
    response = client.post(
        "/agent/renew", headers=agent_headers(NODE_A, "101"), json=_renewal_body()
    )
    assert response.status_code == 503
    assert 0 < int(response.headers["Retry-After"]) <= 300


def test_real_denial_during_renewal_stays_a_403(agent_system, monkeypatch) -> None:
    from vonk_control.enrollment.types import EnrollmentDenied

    client, services, _, _ = agent_system
    _renewable_source(services)

    def denied(*_args, **_kwargs):
        raise EnrollmentDenied("certificate is revoked")

    monkeypatch.setattr(services.enrollment, "renew", denied)
    response = client.post(
        "/agent/renew", headers=agent_headers(NODE_A, "101"), json=_renewal_body()
    )
    assert response.status_code == 403


def test_renewal_heals_after_one_internal_error(agent_system, monkeypatch) -> None:
    client, services, _, _ = agent_system
    _renewable_source(services)
    real = services.enrollment.renew
    calls = []

    def flaky(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise ValueError("transient internal failure")
        return real(*args, **kwargs)

    monkeypatch.setattr(services.enrollment, "renew", flaky)
    first = client.post(
        "/agent/renew", headers=agent_headers(NODE_A, "101"), json=_renewal_body()
    )
    assert first.status_code == 503
    again = client.post(
        "/agent/renew", headers=agent_headers(NODE_A, "101"), json=_renewal_body()
    )
    assert len(calls) == 2
    assert again.status_code == 200


def test_expired_renewal_internal_error_waits_and_heals(tmp_path, monkeypatch) -> None:
    from .test_enrollment import RecoveryAuthority, csr, enroll, expired_proof

    client, services, _codec, clock = make_agent_system(
        tmp_path, authority=RecoveryAuthority()
    )
    enrollment = services.enrollment
    assert enrollment is not None
    key = ed25519.Ed25519PrivateKey.generate()
    issued = enroll(enrollment, node_id=NODE_C, request=csr(NODE_C, key=key))
    clock.now = issued.not_after + timedelta(days=1)
    proof = expired_proof(key, NODE_C, issued.serial, csr(NODE_C), clock.now)
    real = enrollment.renew_expired
    calls = []

    def flaky(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise ValueError("transient internal failure")
        return real(*args, **kwargs)

    monkeypatch.setattr(enrollment, "renew_expired", flaky)
    headers = {"content-type": "application/json"}
    first = client.post(
        "/agent/renew/expired", content=proof.model_dump_json(), headers=headers
    )
    assert first.status_code == 503
    again = client.post(
        "/agent/renew/expired", content=proof.model_dump_json(), headers=headers
    )
    assert again.status_code == 200


_GRANT_KEYS = [
    '"grant_token"',
    '"gr\\u0061nt_token"',
    '"grant\\u005ftoken"',
    '"csr"',
    '"grant_tokens"',
]


_TOKEN_TEXT = st.text("ABCxyz019_-", min_size=43, max_size=43).map(json.dumps)


_JSON_LEAF = st.one_of(
    _TOKEN_TEXT,
    st.text(max_size=5).map(json.dumps),
    st.just("null"),
    st.integers().map(str),
    st.just('"\\\\"'),
)


_WHITESPACE = st.sampled_from(["", " ", "\n"])


def _json_containers(inner):
    array = st.lists(inner, max_size=3).map(lambda items: "[" + ",".join(items) + "]")
    obj = st.lists(
        st.tuples(st.sampled_from(_GRANT_KEYS), _WHITESPACE, inner), max_size=4
    ).map(
        lambda pairs: (
            "{" + ",".join(f"{key}{ws}:{ws}{value}" for key, ws, value in pairs) + "}"
        )
    )
    return st.one_of(array, obj)


_ENROLLMENT_JSON = st.recursive(_JSON_LEAF, _json_containers, max_leaves=12)


def _grant_pairs(text: str) -> list[list[tuple[str, object]]] | None:
    """Every object's key/value pairs as json.loads sees them; the root is last."""
    objects: list[list[tuple[str, object]]] = []

    def collect(pairs: list[tuple[str, object]]) -> dict[str, object]:
        objects.append(pairs)
        return dict(pairs)

    try:
        body = json.loads(text, object_pairs_hook=collect)
    except (ValueError, RecursionError):
        return None
    return objects if isinstance(body, dict) else None


@pytest.mark.property
@given(
    _ENROLLMENT_JSON,
    st.one_of(
        st.none(), st.tuples(st.integers(0, 200), st.sampled_from('",:{}[]\\ a'))
    ),
)
@example(
    '{"grant_token":"' + "A" * 43 + '","gr\\u0061nt_token":"' + "B" * 43 + '"}', None
)
@example('[{"grant_token":"' + "A" * 43 + '"}]', None)
def test_enrollment_scan_agrees_with_json_loads(text: str, mutation) -> None:
    if mutation is not None and text:
        index = mutation[0] % len(text)
        text = text[:index] + mutation[1] + text[index + 1 :]
    scan = _scan_enrollment_grants(text.encode("utf-8"))
    assert len(set(scan.tokens)) == len(scan.tokens)
    assert all(_ENROLLMENT_TOKEN.fullmatch(token) for token in scan.tokens)
    objects = _grant_pairs(text)
    if objects is None:
        return
    # The caller refuses a body unless this count is exactly one, so it must
    # equal the root-level grant_token pairs json.loads collapses to one.
    assert scan.top_level_keys == sum(key == "grant_token" for key, _ in objects[-1])
    discovered = {
        value
        for pairs in objects
        for key, value in pairs
        if key == "grant_token"
        and isinstance(value, str)
        and _ENROLLMENT_TOKEN.fullmatch(value)
    }
    assert set(scan.tokens) == discovered


@pytest.mark.property
@given(st.binary(max_size=2048))
@example(b"")
@example(b'{"grant_token":"')
def test_enrollment_scan_never_raises_on_arbitrary_bytes(raw: bytes) -> None:
    scan = _scan_enrollment_grants(raw)
    assert all(_ENROLLMENT_TOKEN.fullmatch(token) for token in scan.tokens)
