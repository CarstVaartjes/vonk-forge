from __future__ import annotations

import hashlib
import importlib.util
import json
import multiprocessing
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from vonk_agent_protocol import UnknownError, WaitReason
from vonk_agent_protocol.route_activation import ActivationMarker
from vonk_control.litellm import render_empty_config
from vonk_control.route_runtime import (
    RECIPE_ROUTE_AUTHORITY_ID,
    AtomicRouteBundlePublisher,
    FileSupervisorAcknowledger,
    RouteRuntimeError,
    verify_active_route_bundle,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


def _encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _publisher(tmp_path, **kwargs):
    return AtomicRouteBundlePublisher(tmp_path / "runtime", **kwargs)


def _publish(
    publisher,
    *,
    state="published",
    authority_id=RECIPE_ROUTE_AUTHORITY_ID,
    plan_digest="a" * 64,
    evidence_set_digest="b" * 64,
    routes=None,
):
    """Drive one activation through the steps the recipe route adapter uses."""

    publisher._identity(authority_id, plan_digest, evidence_set_digest)
    with publisher._locked() as uncertainty:
        assert uncertainty is None
        current = publisher._read_marker(optional=True, verify_files=True)
        assert not isinstance(current, UnknownError)
        generation = (current.generation if current is not None else 0) + 1
        marker = publisher._activate(
            generation=generation,
            state=state,
            authority_id=authority_id,
            plan_digest=plan_digest,
            evidence_set_digest=evidence_set_digest,
            routes=routes
            or _encoded(
                {
                    "generation": generation,
                    "routes": {},
                    "schema_version": 2,
                    "state": state,
                }
            ),
            litellm=render_empty_config(),
        )
        assert not isinstance(marker, UnknownError)
        assert isinstance(marker, ActivationMarker)
        uncertainty = publisher._require_supervisor_ack(marker)
        assert uncertainty is None
        return marker


def _supervisor(monkeypatch, root):
    monkeypatch.syspath_prepend(str(ROOT / "agent_protocol/src/vonk_agent_protocol"))
    spec = importlib.util.spec_from_file_location(
        "route_test_supervisor", ROOT / "deploy/compose/litellm/config_supervisor.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ACTIVATION", root / "activation.json")
    monkeypatch.setattr(module, "GENERATIONS", root / "generations")
    return module


def test_actual_publisher_bytes_are_accepted_by_reader_and_supervisor(
    tmp_path, monkeypatch
):
    marker = _publish(_publisher(tmp_path))
    root = tmp_path / "runtime"
    assert (root / "activation.json").read_bytes() == marker.canonical_bytes()
    assert marker.schema_version == 2
    assert marker.authority_id == RECIPE_ROUTE_AUTHORITY_ID
    assert "reconciliation_id" not in marker.model_dump()
    bundle = verify_active_route_bundle(root)
    assert not isinstance(bundle, UnknownError)
    assert bundle.marker == marker
    supervisor = _supervisor(monkeypatch, root)
    request = supervisor._active_request()
    assert request is not None
    assert request.activation_sha256 == marker.digest
    assert request.marker == marker.model_dump()
    manifest = (root / "generations" / marker.directory / "manifest.json").read_bytes()
    assert manifest == marker.manifest_document().canonical_bytes()
    assert hashlib.sha256(manifest).hexdigest() == marker.manifest_sha256


@pytest.mark.parametrize(
    "mutation",
    [
        {"schema_version": 1},
        {"schema_version": 2.0},
        {"schema_version": True},
        {"generation": True},
        {"generation": "1"},
        {"generation": 0},
        {"authority_id": "not-a-uuid"},
        {"plan_digest": "z" * 64},
        {"unknown": True},
        {"reconciliation_id": RECIPE_ROUTE_AUTHORITY_ID},
    ],
)
def test_reader_and_supervisor_reject_invalid_or_retired_markers(
    tmp_path, monkeypatch, mutation
):
    marker = _publish(_publisher(tmp_path))
    root = tmp_path / "runtime"
    document = marker.model_dump() | mutation
    (root / "activation.json").write_bytes(_encoded(document))
    with pytest.raises(RouteRuntimeError):
        verify_active_route_bundle(root)
    assert _supervisor(monkeypatch, root)._active_request() is None


@pytest.mark.parametrize("filename", ["manifest.json", "routes.json", "litellm.json"])
def test_corrupt_generation_fails_closed(tmp_path, monkeypatch, filename):
    marker = _publish(_publisher(tmp_path))
    root = tmp_path / "runtime"
    (root / "generations" / marker.directory / filename).write_bytes(b"{}\n")
    with pytest.raises(RouteRuntimeError, match="checksum"):
        verify_active_route_bundle(root)
    assert _supervisor(monkeypatch, root)._active_request() is None


def test_noncanonical_marker_fails_closed(tmp_path, monkeypatch):
    marker = _publish(_publisher(tmp_path))
    root = tmp_path / "runtime"
    activation = root / "activation.json"
    activation.write_text(json.dumps(marker.model_dump(), indent=2))
    with pytest.raises(RouteRuntimeError, match="canonical"):
        verify_active_route_bundle(root)
    assert _supervisor(monkeypatch, root)._active_request() is None


def test_republication_and_empty_publication_keep_monotonic_generations_and_ack(
    tmp_path,
):
    acknowledged = []
    publisher = _publisher(tmp_path, await_supervisor_ack=acknowledged.append)
    first = _publish(publisher)
    second = _publish(publisher, plan_digest="c" * 64, evidence_set_digest="c" * 64)
    empty = _publish(publisher, state="maintenance")
    assert [m.generation for m in acknowledged] == [1, 2, 3]
    assert acknowledged == [first, second, empty]
    assert publisher.inspect() == empty
    assert empty.state == "maintenance"


def test_validation_failure_preserves_previous_activation(tmp_path):
    marker = _publish(_publisher(tmp_path))
    rejecting = _publisher(tmp_path, validate_litellm=lambda _: False)
    with pytest.raises(RouteRuntimeError, match="validation"):
        _publish(rejecting)
    assert _inspected(_publisher(tmp_path)) == marker


def test_ack_failure_is_reported_and_exact_persisted_marker_can_be_inspected(tmp_path):
    def unavailable(marker):
        raise RuntimeError("supervisor unavailable")

    publisher = _publisher(tmp_path, await_supervisor_ack=unavailable)
    marker = _publish(_publisher(tmp_path))
    observed = publisher._require_supervisor_ack(marker)
    assert isinstance(observed, UnknownError)
    assert observed.reason is WaitReason.RUNTIME_EFFECT_UNCONFIRMED
    assert _inspected(_publisher(tmp_path)).generation == 1


def test_restart_verifies_exact_marker_and_publishes_next_generation(tmp_path):
    first = _publish(_publisher(tmp_path))
    restarted = _publisher(tmp_path)
    assert _inspected(restarted, expected=first) == first
    assert _publish(restarted).generation == 2
    assert _inspected(restarted, expected=first).generation == 2
    assert _publish(restarted).generation == 3


def test_concurrent_writers_serialize_generation_allocation(tmp_path):
    with ThreadPoolExecutor(max_workers=2) as pool:
        markers = list(pool.map(lambda _: _publish(_publisher(tmp_path)), range(2)))
    assert sorted(m.generation for m in markers) == [1, 2]
    assert _inspected(_publisher(tmp_path)).generation == 2


def _process_publish(path):
    _publish(_publisher(Path(path)))


def test_filesystem_lock_serializes_independent_processes(tmp_path):
    context = multiprocessing.get_context("spawn")
    workers = [
        context.Process(target=_process_publish, args=(str(tmp_path),))
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=15)
        assert worker.exitcode == 0
    assert _inspected(_publisher(tmp_path)).generation == 2


def test_symlink_lock_and_generation_are_rejected(tmp_path):
    publisher = _publisher(tmp_path)
    outside = tmp_path / "outside"
    outside.touch()
    (tmp_path / "runtime/.publication.lock").symlink_to(outside)
    with pytest.raises(RouteRuntimeError, match="lock"):
        _publish(publisher)


def test_control_accepts_only_a_recent_ack_for_the_exact_marker(tmp_path: Path) -> None:
    marker = _publish(_publisher(tmp_path))
    ack_path = tmp_path / "supervisor/ack.json"
    ack_path.parent.mkdir()
    acknowledgement = {
        "acknowledged_at": NOW.isoformat(),
        "activation_sha256": marker.digest,
        "child_pid": 123,
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
    observed = mismatched(marker)
    assert isinstance(observed, UnknownError)
    assert observed.reason is WaitReason.RUNTIME_EFFECT_UNCONFIRMED


@pytest.mark.parametrize("ack_after", [90, 150])
def test_delayed_supervisor_ack_uses_the_shared_activation_budget(
    tmp_path, monkeypatch, ack_after
):
    from types import SimpleNamespace

    marker = _publish(_publisher(tmp_path))
    supervisor = _supervisor(monkeypatch, tmp_path / "runtime")
    ack_path = tmp_path / "supervisor/ack.json"
    monkeypatch.setattr(supervisor, "ACK", ack_path)
    request = supervisor._active_request()
    child = SimpleNamespace(pid=123, poll=lambda: None)
    elapsed = [0.0]

    def sleep(seconds):
        elapsed[0] += seconds
        if elapsed[0] >= ack_after:
            supervisor._write_ack(
                request, child, now=NOW + timedelta(seconds=elapsed[0])
            )

    FileSupervisorAcknowledger(
        ack_path,
        clock=lambda: NOW + timedelta(seconds=elapsed[0]),
        monotonic=lambda: elapsed[0],
        sleep=sleep,
        poll_seconds=1,
    )(marker)
    assert elapsed[0] == ack_after


@pytest.mark.parametrize("failure", ["generation", "stale", "unknown"])
def test_longer_ack_budget_still_rejects_invalid_authority(tmp_path, failure):
    from vonk_agent_protocol.route_activation import SupervisorAcknowledgement

    marker = _publish(_publisher(tmp_path))
    path = tmp_path / "ack.json"
    elapsed = [0.0]

    def sleep(seconds):
        elapsed[0] += seconds
        if elapsed[0] < 90:
            return
        ack = SupervisorAcknowledgement(
            schema_version=1,
            acknowledged_at=NOW.isoformat(),
            activation_sha256=marker.digest,
            child_pid=123,
            generation=marker.generation,
            litellm_sha256=marker.litellm_sha256,
            state=marker.state,
        ).model_dump()
        if failure != "stale":
            ack["acknowledged_at"] = (NOW + timedelta(seconds=elapsed[0])).isoformat()
        if failure == "generation":
            ack["generation"] += 1
        if failure == "unknown":
            ack["unexpected"] = None
        path.write_bytes(_encoded(ack))

    observed = FileSupervisorAcknowledger(
        path,
        clock=lambda: NOW + timedelta(seconds=elapsed[0]),
        monotonic=lambda: elapsed[0],
        sleep=sleep,
        poll_seconds=1,
    )(marker)
    assert isinstance(observed, UnknownError)
    assert observed.reason is WaitReason.RUNTIME_EFFECT_UNCONFIRMED


def _verified_bundle(root):
    bundle = verify_active_route_bundle(root)
    assert not isinstance(bundle, UnknownError)
    return bundle


def _inspected(publisher, **kwargs):
    marker = publisher.inspect(**kwargs)
    assert not isinstance(marker, UnknownError)
    return marker
