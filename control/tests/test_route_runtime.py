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
    return AtomicRouteBundlePublisher(tmp_path / "runtime", clock=lambda: NOW, **kwargs)


def _publish(publisher, **kwargs):
    inputs = {
        "authority_id": RECIPE_ROUTE_AUTHORITY_ID,
        "plan_digest": "a" * 64,
        "evidence_set_digest": "b" * 64,
        "routes": _encoded({"schema_version": 2, "routes": {}}),
        "litellm": AtomicRouteBundlePublisher.empty_litellm(),
        "expires_at": NOW + timedelta(seconds=150),
    }
    return publisher.publish_compiled(**(inputs | kwargs))


def _supervisor(monkeypatch, root):
    monkeypatch.syspath_prepend(str(ROOT / "agent_protocol/src/vonk_agent_protocol"))
    spec = importlib.util.spec_from_file_location(
        "route_test_supervisor", ROOT / "deploy/compose/litellm/config_supervisor.py"
    )
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
    bundle = verify_active_route_bundle(root, clock=lambda: NOW)
    assert bundle.marker == marker
    supervisor = _supervisor(monkeypatch, root)
    request = supervisor._active_request(now=NOW)
    assert request is not None
    assert request.activation_sha256 == marker.digest
    assert request.marker == marker.model_dump()
    manifest = (root / "generations" / marker.directory / "manifest.json").read_bytes()
    assert manifest == _encoded(marker.manifest_document())
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
        verify_active_route_bundle(root, clock=lambda: NOW)
    assert _supervisor(monkeypatch, root)._active_request(now=NOW) is None


@pytest.mark.parametrize("filename", ["manifest.json", "routes.json", "litellm.json"])
def test_corrupt_generation_fails_closed(tmp_path, monkeypatch, filename):
    marker = _publish(_publisher(tmp_path))
    root = tmp_path / "runtime"
    (root / "generations" / marker.directory / filename).write_bytes(b"{}\n")
    with pytest.raises(RouteRuntimeError, match="checksum"):
        verify_active_route_bundle(root, clock=lambda: NOW)
    assert _supervisor(monkeypatch, root)._active_request(now=NOW) is None


def test_noncanonical_and_expired_marker_fail_closed(tmp_path, monkeypatch):
    marker = _publish(_publisher(tmp_path))
    root = tmp_path / "runtime"
    activation = root / "activation.json"
    activation.write_text(json.dumps(marker.model_dump(), indent=2))
    with pytest.raises(RouteRuntimeError, match="canonical"):
        verify_active_route_bundle(root, clock=lambda: NOW)
    assert _supervisor(monkeypatch, root)._active_request(now=NOW) is None
    activation.write_bytes(marker.canonical_bytes())
    with pytest.raises(RouteRuntimeError, match="lease"):
        verify_active_route_bundle(root, clock=lambda: NOW + timedelta(seconds=151))
    assert (
        _supervisor(monkeypatch, root)._active_request(now=NOW + timedelta(seconds=151))
        is None
    )


def test_renewal_and_empty_publication_keep_monotonic_generations_and_ack(tmp_path):
    acknowledged = []
    publisher = _publisher(tmp_path, await_supervisor_ack=acknowledged.append)
    first = _publish(publisher)
    renewed = _publish(publisher, expires_at=NOW + timedelta(seconds=200))
    empty = _publish(publisher, state="maintenance")
    assert [m.generation for m in acknowledged] == [1, 2, 3]
    assert acknowledged == [first, renewed, empty]
    assert publisher.inspect() == empty
    assert empty.state == "maintenance"


def test_validation_failure_preserves_previous_activation(tmp_path):
    marker = _publish(_publisher(tmp_path))
    rejecting = _publisher(tmp_path, validate_litellm=lambda _: False)
    with pytest.raises(RouteRuntimeError, match="validation"):
        _publish(rejecting)
    assert _publisher(tmp_path).inspect() == marker


def test_ack_failure_is_reported_and_exact_persisted_marker_can_be_inspected(tmp_path):
    def unavailable(marker):
        raise RuntimeError("supervisor unavailable")

    with pytest.raises(RouteRuntimeError, match="acknowledgement"):
        _publish(_publisher(tmp_path, await_supervisor_ack=unavailable))
    assert _publisher(tmp_path).inspect().generation == 1


def test_restart_verifies_exact_marker_and_renews_with_next_generation(tmp_path):
    first = _publish(_publisher(tmp_path))
    restarted = _publisher(tmp_path)
    assert restarted.inspect(expected=first) == first
    assert _publish(restarted).generation == 2
    with pytest.raises(RouteRuntimeError, match="expected"):
        restarted.inspect(expected=first)


def test_concurrent_writers_serialize_generation_allocation(tmp_path):
    with ThreadPoolExecutor(max_workers=2) as pool:
        markers = list(pool.map(lambda _: _publish(_publisher(tmp_path)), range(2)))
    assert sorted(m.generation for m in markers) == [1, 2]
    assert _publisher(tmp_path).inspect().generation == 2


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
    assert _publisher(tmp_path).inspect().generation == 2


def test_symlink_lock_and_generation_are_rejected(tmp_path):
    publisher = _publisher(tmp_path)
    outside = tmp_path / "outside"
    outside.touch()
    (tmp_path / "runtime/.publication.lock").symlink_to(outside)
    with pytest.raises(RouteRuntimeError, match="lock"):
        _publish(publisher)


def test_existing_update_boundary_still_fences_current_publication(tmp_path):
    publisher = _publisher(tmp_path)
    publisher.claim_update_boundary("e" * 64)
    with pytest.raises(RouteRuntimeError, match="fenced"):
        _publish(publisher)
    publisher.release_update_boundary("e" * 64)
    assert _publish(publisher).generation == 1


def test_control_accepts_only_a_recent_ack_for_the_exact_marker(tmp_path: Path) -> None:
    marker = _publish(_publisher(tmp_path))
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
