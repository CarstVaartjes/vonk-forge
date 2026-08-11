from __future__ import annotations

import hashlib
import importlib
import json
import multiprocessing
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from vonk_agent_protocol import canonical_message
from vonk_control.presence import ManagementAddressPolicy
from vonk_control.route_runtime import (
    AcceptedEndpointEvidence,
    AtomicRouteBundlePublisher,
    FileSupervisorAcknowledger,
    RouteBundleRequest,
    RouteRuntimeError,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
NODE = "spk_" + "1" * 32
RECONCILIATION_ID = "bb7aac18-edbf-4cc1-bafd-15e282557c53"
PLAN_DIGEST = "a" * 64
EVIDENCE_SET_DIGEST = "b" * 64


def _routes() -> dict[str, object]:
    return {
        "chat": {
            "workload_id": "model",
            "nodes": [NODE],
            "entrypoint_node_id": NODE,
            "scheme": "http",
            "port": 8000,
            "path": "/v1",
            "quota": {
                "requests_per_minute": 30,
                "tokens_per_minute": 10_000,
            },
            "quota_digest": hashlib.sha256(
                canonical_message(
                    {
                        "requests_per_minute": 30,
                        "tokens_per_minute": 10_000,
                    }
                )
            ).hexdigest(),
        }
    }


def _endpoint_digest(
    *, address: str, observed_at: datetime, verify_evidence_digest: str
) -> str:
    content = {
        "address": address,
        "node_id": NODE,
        "observed_at": observed_at.isoformat(),
        "operation_id": f"model:{NODE}:workload.verify",
        "schema_version": 1,
        "verify_evidence_digest": verify_evidence_digest,
    }
    return hashlib.sha256(
        (json.dumps(content, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()


def _request(
    *, address: str = "10.0.0.42", observed_at: datetime = NOW
) -> RouteBundleRequest:
    verify_evidence_digest = "c" * 64
    return RouteBundleRequest(
        reconciliation_id=RECONCILIATION_ID,
        plan_digest=PLAN_DIGEST,
        evidence_set_digest=EVIDENCE_SET_DIGEST,
        routes=_routes(),
        endpoints={
            NODE: AcceptedEndpointEvidence(
                node_id=NODE,
                address=address,
                observed_at=observed_at,
                operation_id=f"model:{NODE}:workload.verify",
                verify_evidence_digest=verify_evidence_digest,
                evidence_digest=_endpoint_digest(
                    address=address,
                    observed_at=observed_at,
                    verify_evidence_digest=verify_evidence_digest,
                ),
            )
        },
        expires_at=NOW + timedelta(seconds=150),
    )


def _publisher(tmp_path: Path, **kwargs) -> AtomicRouteBundlePublisher:
    return AtomicRouteBundlePublisher(
        tmp_path / "runtime",
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=lambda: NOW,
        **kwargs,
    )


def test_bundle_stages_structured_routes_litellm_and_manifest_before_one_marker(
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path)

    marker = publisher.publish(_request())

    assert marker.reconciliation_id == RECONCILIATION_ID
    assert marker.plan_digest == PLAN_DIGEST
    assert marker.evidence_set_digest == EVIDENCE_SET_DIGEST
    directory = tmp_path / "runtime/generations" / marker.directory
    routes = json.loads((directory / "routes.json").read_bytes())
    config = json.loads((directory / "litellm.json").read_bytes())
    manifest = json.loads((directory / "manifest.json").read_bytes())
    activation_path = tmp_path / "runtime/activation.json"
    assert (tmp_path / "runtime").stat().st_mode & 0o777 == 0o750
    assert (tmp_path / "runtime/generations").stat().st_mode & 0o777 == 0o750
    assert directory.stat().st_mode & 0o777 == 0o750
    assert activation_path.stat().st_mode & 0o777 == 0o640
    assert (directory / "routes.json").stat().st_mode & 0o777 == 0o640
    activation = json.loads(activation_path.read_bytes())

    assert routes == {
        "generation": 1,
        "routes": {
            "chat": {
                "address": "10.0.0.42",
                "evidence_digest": _endpoint_digest(
                    address="10.0.0.42",
                    observed_at=NOW,
                    verify_evidence_digest="c" * 64,
                ),
                "node_id": NODE,
                "observed_at": NOW.isoformat(),
                "operation_id": f"model:{NODE}:workload.verify",
                "path": "/v1",
                "port": 8000,
                "scheme": "http",
                "verify_evidence_digest": "c" * 64,
            }
        },
        "schema_version": 1,
        "state": "published",
    }
    assert config["model_list"][0]["litellm_params"] == {
        "api_base": "http://10.0.0.42:8000/v1",
        "api_key": "os.environ/LITELLM_UPSTREAM_KEY",
        "model": "openai/chat",
        "rpm": 30,
        "tpm": 10_000,
    }
    assert config["general_settings"]["disable_admin_ui"] is False
    assert config["general_settings"]["store_model_in_db"] is False
    assert activation == {
        **manifest,
        "directory": marker.directory,
        "manifest_sha256": marker.manifest_sha256,
    }
    assert (
        activation["routes_sha256"]
        == hashlib.sha256((directory / "routes.json").read_bytes()).hexdigest()
    )
    assert (
        activation["litellm_sha256"]
        == hashlib.sha256((directory / "litellm.json").read_bytes()).hexdigest()
    )
    assert marker.canonical_bytes() == activation_path.read_bytes()
    assert marker.digest == hashlib.sha256(activation_path.read_bytes()).hexdigest()
    assert publisher.inspect(expected=marker) == marker


def test_routes_are_derived_only_from_exact_entrypoint_and_bounded_address_evidence(
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path)
    bad_routes = _routes()
    bad_routes["chat"]["entrypoint_node_id"] = "spk_" + "2" * 32

    with pytest.raises(RouteRuntimeError, match="entrypoint"):
        publisher.publish(
            _request().__class__(**{**_request().__dict__, "routes": bad_routes})
        )
    with pytest.raises(RouteRuntimeError, match="management"):
        publisher.publish(_request(address="192.0.2.9"))
    assert not (tmp_path / "runtime/activation.json").exists()


def test_address_cannot_be_relabelled_with_an_accepted_endpoint_digest(
    tmp_path: Path,
) -> None:
    request = _request()
    original = request.endpoints[NODE]
    relabelled = original.__class__(
        node_id=NODE,
        address="10.0.0.43",
        observed_at=original.observed_at,
        operation_id=original.operation_id,
        verify_evidence_digest=original.verify_evidence_digest,
        evidence_digest=original.evidence_digest,
    )

    with pytest.raises(RouteRuntimeError, match="binding"):
        _publisher(tmp_path).publish(
            request.__class__(**{**request.__dict__, "endpoints": {NODE: relabelled}})
        )


def test_route_lease_cannot_outlive_endpoint_freshness(tmp_path: Path) -> None:
    request = _request(observed_at=NOW - timedelta(seconds=299))

    with pytest.raises(RouteRuntimeError, match="freshness"):
        _publisher(tmp_path).publish(request)


def test_runtime_rejects_a_side_effecting_pre_marker_apply_hook(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="apply"):
        _publisher(tmp_path, apply=lambda _directory: None)


def test_explicit_entrypoint_is_authoritative_even_when_nodes_have_another_order(
    tmp_path: Path,
) -> None:
    request = _request()
    routes = _routes()
    routes["chat"]["nodes"] = ["spk_" + "2" * 32, NODE]

    marker = _publisher(tmp_path).publish(
        request.__class__(**{**request.__dict__, "routes": routes})
    )

    assert marker.state == "published"


def test_concurrent_same_publication_is_one_idempotent_generation(
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path)
    barrier = threading.Barrier(2)
    markers = []

    def publish() -> None:
        barrier.wait(timeout=5)
        markers.append(publisher.publish(_request()))

    threads = [threading.Thread(target=publish) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(markers) == 2
    assert markers[0] == markers[1]
    assert markers[0].generation == 1
    assert len(list((tmp_path / "runtime/generations").iterdir())) == 1


def test_every_publication_waits_for_an_exact_live_supervisor_ack(
    tmp_path: Path,
) -> None:
    acknowledged = []
    publisher = _publisher(
        tmp_path,
        await_supervisor_ack=acknowledged.append,
    )

    first = publisher.publish(_request())
    second = publisher.publish(_request())

    assert first == second
    assert acknowledged == [first, second]


def test_control_accepts_only_a_recent_ack_for_the_exact_marker(tmp_path: Path) -> None:
    marker = _publisher(tmp_path).publish(_request())
    ack_path = tmp_path / "supervisor/ack.json"
    ack_path.parent.mkdir()
    acknowledgement = {
        "acknowledged_at": NOW.isoformat(),
        "activation_sha256": marker.digest,
        "child_pid": 123,
        "expires_at": marker.expires_at,
        "generation": marker.generation,
        "litellm_sha256": marker.litellm_sha256,
        "schema_version": 1,
        "state": marker.state,
    }
    ack_path.write_bytes(
        (
            json.dumps(acknowledgement, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
    )
    FileSupervisorAcknowledger(ack_path, clock=lambda: NOW)(marker)

    acknowledgement["activation_sha256"] = "f" * 64
    ack_path.write_bytes(
        (
            json.dumps(acknowledgement, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
    )
    moments = iter((0.0, 1.0))
    mismatched = FileSupervisorAcknowledger(
        ack_path,
        clock=lambda: NOW,
        timeout_seconds=0.5,
        poll_seconds=0.1,
        monotonic=lambda: next(moments),
        sleep=lambda _seconds: None,
    )
    with pytest.raises(RouteRuntimeError, match="timed out"):
        mismatched(marker)


def test_restart_after_activation_before_ack_reuses_the_exact_request(
    tmp_path: Path,
) -> None:
    def crash_before_ack(_marker):
        raise RuntimeError("supervisor unavailable")

    with pytest.raises(RouteRuntimeError, match="acknowledgement"):
        _publisher(
            tmp_path,
            await_supervisor_ack=crash_before_ack,
        ).publish(_request())

    acknowledged = []
    marker = _publisher(
        tmp_path,
        await_supervisor_ack=acknowledged.append,
    ).publish(_request())

    assert marker.generation == 1
    assert acknowledged == [marker]


def test_multiprocess_publication_is_serialized_to_one_generation(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(2)
    results = context.Queue()

    def publish() -> None:
        barrier.wait(timeout=5)
        marker = _publisher(tmp_path).publish(_request())
        results.put((marker.generation, marker.digest))

    processes = [context.Process(target=publish) for _ in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)

    assert [process.exitcode for process in processes] == [0, 0]
    receipts = [results.get(timeout=2) for _ in processes]
    assert receipts[0] == receipts[1]
    assert receipts[0][0] == 1


def test_validation_or_activation_failure_retains_previous_exact_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = _publisher(tmp_path)
    previous = publisher.publish(_request())
    active = tmp_path / "runtime/activation.json"
    previous_bytes = active.read_bytes()
    replacement = _request().__class__(
        **{**_request().__dict__, "evidence_set_digest": "d" * 64}
    )

    rejecting = _publisher(tmp_path, validate_litellm=lambda _content: False)
    with pytest.raises(RouteRuntimeError, match="LiteLLM validation"):
        rejecting.publish(replacement)
    assert active.read_bytes() == previous_bytes

    original_replace = __import__("os").replace

    def fail_marker(source, target):
        if Path(target) == active:
            raise OSError("crash before activation")
        original_replace(source, target)

    monkeypatch.setattr("vonk_control.route_runtime.os.replace", fail_marker)
    with pytest.raises(RouteRuntimeError, match="activation"):
        publisher.publish(replacement)
    assert active.read_bytes() == previous_bytes
    monkeypatch.undo()
    assert (
        AtomicRouteBundlePublisher(
            tmp_path / "runtime",
            management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
            clock=lambda: NOW,
        ).inspect(expected=previous)
        == previous
    )


def test_crash_before_files_leaves_no_generation_or_activation(tmp_path: Path) -> None:
    publisher = _publisher(tmp_path, validate_routes=lambda _content: False)

    with pytest.raises(RouteRuntimeError, match="route validation"):
        publisher.publish(_request())

    assert list((tmp_path / "runtime/generations").iterdir()) == []
    assert not (tmp_path / "runtime/activation.json").exists()


def test_restart_after_marker_before_db_ack_reuses_exact_receipt(
    tmp_path: Path,
) -> None:
    marker = _publisher(tmp_path).publish(_request())

    restarted = AtomicRouteBundlePublisher(
        tmp_path / "runtime",
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=lambda: NOW,
    )

    assert restarted.publish(_request()) == marker
    assert len(list((tmp_path / "runtime/generations").iterdir())) == 1


def test_restart_inspection_rejects_tampering_wrong_expectation_and_expired_lease(
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path)
    marker = publisher.publish(_request())
    restarted = AtomicRouteBundlePublisher(
        tmp_path / "runtime",
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=lambda: NOW,
    )
    wrong = marker.__class__(**{**marker.__dict__, "plan_digest": "d" * 64})
    with pytest.raises(RouteRuntimeError, match="expected"):
        restarted.inspect(expected=wrong)

    activation = tmp_path / "runtime/activation.json"
    exact = activation.read_bytes()
    activation.write_text(json.dumps(json.loads(exact), indent=2))
    with pytest.raises(RouteRuntimeError, match="canonical"):
        restarted.inspect(expected=marker)
    activation.write_bytes(exact)

    config = tmp_path / "runtime/generations" / marker.directory / "litellm.json"
    config.write_bytes(b'{"model_list":[{"unsafe":true}]}\n')
    with pytest.raises(RouteRuntimeError, match="checksum"):
        restarted.inspect(expected=marker)

    clean_root = tmp_path / "clean"
    clean = AtomicRouteBundlePublisher(
        clean_root,
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=lambda: NOW,
    )
    clean_marker = clean.publish(_request())
    expired = AtomicRouteBundlePublisher(
        clean_root,
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=lambda: NOW + timedelta(seconds=151),
    )
    with pytest.raises(RouteRuntimeError, match="lease"):
        expired.inspect(expected=clean_marker)


def test_withdrawal_activates_an_empty_fail_closed_bundle(tmp_path: Path) -> None:
    publisher = _publisher(tmp_path)
    publisher.publish(_request())

    marker = publisher.withdraw(
        reconciliation_id=RECONCILIATION_ID,
        plan_digest=PLAN_DIGEST,
        targets=(NODE,),
        reason="reconciliation in progress bearer-secret",
    )

    directory = tmp_path / "runtime/generations" / marker.directory
    routes = json.loads((directory / "routes.json").read_bytes())
    config = json.loads((directory / "litellm.json").read_bytes())
    assert marker.state == "maintenance"
    assert routes["state"] == "maintenance"
    assert routes["routes"] == {}
    assert routes["targets"] == [NODE]
    assert "bearer-secret" not in routes["reason"]
    assert config["model_list"] == []


def test_update_boundary_fences_normal_publication_until_exact_release(
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path)
    publisher.publish(_request())
    boundary_key = "d" * 64

    publisher.claim_update_boundary(boundary_key)

    with pytest.raises(RouteRuntimeError, match="update boundary"):
        publisher.publish(_request(address="10.0.0.43"))
    keyed = publisher.publish(
        _request(address="10.0.0.43"),
        update_boundary_key=boundary_key,
    )
    assert keyed.generation == 2
    with pytest.raises(RouteRuntimeError, match="different update boundary"):
        publisher.claim_update_boundary("e" * 64)
    with pytest.raises(RouteRuntimeError, match="update boundary key"):
        publisher.release_update_boundary("e" * 64)

    publisher.release_update_boundary(boundary_key)

    restored = publisher.publish(_request())
    assert restored.generation == 3


def test_update_boundary_rejects_unkeyed_disjoint_reconciliation_withdrawal(
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path)
    exact = publisher.publish(_request())
    publisher.claim_update_boundary("d" * 64)

    with pytest.raises(RouteRuntimeError, match="update boundary"):
        publisher.withdraw(
            reconciliation_id="f89f180b-0c52-4c77-b983-663d16dd3aa7",
            plan_digest="f" * 64,
            targets=(NODE,),
            reason="disjoint reconciliation maintenance",
        )
    assert publisher.inspect() == exact


def test_exact_update_boundary_can_force_a_fresh_generation_before_expiry(
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path)
    first = publisher.publish(_request())
    publisher.claim_update_boundary("d" * 64)

    renewed = publisher.publish(
        _request(),
        update_boundary_key="d" * 64,
        renew_update_boundary=True,
    )

    assert renewed.generation == first.generation + 1
    with pytest.raises(RouteRuntimeError, match="requires its exact fence"):
        publisher.publish(_request(), renew_update_boundary=True)


def test_update_boundary_publication_compare_and_swap_is_atomic_and_idempotent(
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path)
    before = publisher.publish(_request())
    key = "d" * 64
    publisher.claim_update_boundary(key)

    desired = publisher.publish(
        _request(address="10.0.0.43"),
        update_boundary_key=key,
        expected_current_digest=before.digest,
    )
    retried = publisher.publish(
        _request(address="10.0.0.43"),
        update_boundary_key=key,
        expected_current_digest=before.digest,
    )
    assert retried == desired

    maintenance = publisher.withdraw(
        reconciliation_id=RECONCILIATION_ID,
        plan_digest=PLAN_DIGEST,
        targets=(NODE,),
        reason="authority lost",
        update_boundary_key=key,
        expected_current_digest=desired.digest,
    )
    with pytest.raises(RouteRuntimeError, match="compare-and-swap"):
        publisher.publish(
            _request(address="10.0.0.44"),
            update_boundary_key=key,
            expected_current_digest=desired.digest,
        )
    assert publisher.inspect() == maintenance


def test_commit_pinned_hermes_policy_selects_only_accepted_published_routes(
    tmp_path: Path,
) -> None:
    hermes_routes = importlib.import_module("vonk_control.hermes_routes")
    route_runtime = importlib.import_module("vonk_control.route_runtime")
    files = {
        "config/hermes-agent-policy.toml": (
            b"schema_version = 1\n"
            b'alias = "hermes-agent"\n'
            b"local_only = true\n\n"
            b"[[candidates]]\n"
            b'workload = "model"\n'
            b"priority = 1\n"
            b'minimum_maturity = "accepted"\n'
        ),
        "inventory/reports/model-definitions.json": json.dumps(
            {
                "definitions": [
                    {"id": "model", "maturity": "accepted"},
                    {"id": "planned-model", "maturity": "planned"},
                ]
            }
        ).encode(),
    }
    policy = hermes_routes.RepositoryHermesRoutePolicy(
        tmp_path,
        repository_reader=lambda _commit, path: files[path],
    )
    published = route_runtime.PublishedRoute(
        alias="chat",
        workload_id="model",
        api_base="http://10.0.0.42:8000/v1",
        requests_per_minute=30,
        tokens_per_minute=10_000,
    )

    deployments = policy.deployments("a" * 40, (published,))

    assert len(deployments) == 1
    assert deployments[0].model_name == "hermes-agent"
    assert deployments[0].workload == "model"
    assert deployments[0].priority == 1
    assert deployments[0].api_base == "http://10.0.0.42:8000/v1"


def test_atomic_bundle_includes_commit_pinned_hermes_group_or_fails_closed(
    tmp_path: Path,
) -> None:
    hermes_routes = importlib.import_module("vonk_control.hermes_routes")
    report = {
        "definitions": [{"id": "model", "maturity": "accepted"}],
    }
    files = {
        "config/hermes-agent-policy.toml": (
            b"schema_version = 1\n"
            b'alias = "hermes-agent"\n'
            b"local_only = true\n\n"
            b"[[candidates]]\n"
            b'workload = "model"\n'
            b"priority = 1\n"
            b'minimum_maturity = "accepted"\n'
        ),
        "inventory/reports/model-definitions.json": json.dumps(report).encode(),
    }
    policy = hermes_routes.RepositoryHermesRoutePolicy(
        tmp_path,
        repository_reader=lambda _commit, path: files[path],
    )
    request = _request().__class__(**{**_request().__dict__, "base_commit": "a" * 40})
    publisher = _publisher(
        tmp_path,
        litellm_deployments=policy.deployments,
    )

    marker = publisher.publish(request)
    config = json.loads(
        (
            tmp_path / "runtime/generations" / marker.directory / "litellm.json"
        ).read_bytes()
    )

    assert [row["model_name"] for row in config["model_list"]] == [
        "chat",
        "hermes-agent",
    ]
    assert config["model_list"][1]["litellm_params"] == {
        "api_base": "http://10.0.0.42:8000/v1",
        "api_key": "os.environ/LITELLM_UPSTREAM_KEY",
        "model": "openai/model",
        "order": 1,
        "rpm": 30,
        "tpm": 10_000,
    }
    assert config["router_settings"]["allowed_fails"] == 0
    assert config["router_settings"]["num_retries"] == 1

    files["inventory/reports/model-definitions.json"] = b'{"definitions":[]}'
    replacement = request.__class__(
        **{**request.__dict__, "evidence_set_digest": "d" * 64}
    )
    with pytest.raises(RouteRuntimeError, match="Hermes repository policy"):
        publisher.publish(replacement)
    assert publisher.inspect(expected=marker) == marker
