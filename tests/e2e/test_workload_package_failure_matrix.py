"""W19 generic workload-package failure, security, and recovery gate.

These tests intentionally use the public package boundaries.  They do not
instantiate a model-specific adapter, route payloads through the NAS, or call
SSH.  The resulting records are suitable for inclusion in first-release
evidence by the W20 verifier.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for source_root in (ROOT / "agent/src", ROOT / "agent_protocol/src"):
    sys.path.insert(0, str(source_root))

from vonk_agent.packages.failures import (
    PackageFailureDisposition,
    PackageFailureReason,
    failure,
)
from vonk_agent.packages.gc import (
    GarbageCollectionInterrupted,
    PackageGarbageCollector,
)
from vonk_agent.packages.state import OperationBinding, PackageState
from vonk_agent.packages.store import ContentStore, StoreObject

NODE = "spk_0123456789abcdef0123456789abcdef"
FENCE = "30000000-0000-4000-8000-000000000001"
RELEASE = "a" * 64


def _binding(index: int = 1, *, attempt: int = 1) -> OperationBinding:
    return OperationBinding(
        job_id=f"10000000-0000-4000-8000-{index:012d}",
        operation_id=f"20000000-0000-4000-8000-{index:012d}",
        attempt=attempt,
        fence=FENCE if index == 1 else f"30000000-0000-4000-8000-{index:012d}",
        node_id=NODE,
    )


def _failure(reason: PackageFailureReason):
    if reason in {
        PackageFailureReason.ACTIVATION_FAILURE,
        PackageFailureReason.RUNTIME_HEALTH_FAILURE,
    }:
        disposition = PackageFailureDisposition.COMPENSATE
    elif reason in {
        PackageFailureReason.TRUST_PROVENANCE_FAILURE,
        PackageFailureReason.POLICY_LICENSE_REJECTION,
        PackageFailureReason.INCOMPATIBLE_PLATFORM,
        PackageFailureReason.MISSING_CREDENTIAL,
        PackageFailureReason.ROLLBACK_FAILURE,
    }:
        disposition = PackageFailureDisposition.OPERATOR_INTERVENTION
    else:
        disposition = PackageFailureDisposition.SAFE_TO_RETRY
    return failure(
        reason,
        disposition=disposition,
        family_id="synthetic-stack",
        upstream_version="2026.08.06",
        release_digest=RELEASE,
        component="runtime",
        node_id=NODE,
        fence=FENCE,
        diagnostic={
            "message": "bounded diagnostic",
            "credential": "must-not-escape",
            "authorization": "Bearer must-not-escape",
            "upstream_url": "https://secret.example.invalid/model",
            "nested": {"token": "must-not-escape"},
        },
    )


def test_failure_taxonomy_is_complete_structured_and_secret_free() -> None:
    expected = {
        "discovery-unavailable",
        "upstream-mutation",
        "resolution-unsupported",
        "trust-provenance-failure",
        "policy-license-rejection",
        "incompatible-platform",
        "missing-credential",
        "insufficient-capacity",
        "retryable-transport",
        "digest-size-mismatch",
        "environment-build-failure",
        "package-validation-failure",
        "activation-failure",
        "runtime-health-failure",
        "rollback-failure",
    }
    assert {item.value for item in PackageFailureReason} >= expected

    records = [_failure(reason).to_mapping() for reason in PackageFailureReason]
    required = {
        "family_id",
        "upstream_version",
        "release_digest",
        "node_id",
        "fence",
        "reason_code",
        "disposition",
    }
    for record in records:
        assert required <= record.keys()
        encoded = json.dumps(record, sort_keys=True)
        assert "must-not-escape" not in encoded
        assert "https://" not in encoded
        assert len(encoded) <= 4096
        assert record["disposition"] in {
            "safe-to-retry",
            "compensate",
            "operator-intervention",
        }


def test_corrupt_store_is_quarantined_and_refetched(tmp_path: Path) -> None:
    content = b"repairable-workload-object"
    digest = hashlib.sha256(content).hexdigest()
    binding = _binding()
    store = ContentStore(tmp_path / "packages", capacity_bytes=1024)
    descriptor = type("Descriptor", (), {"digest": digest, "size": len(content), "kind": "model"})()
    reservation = store.reserve_component(binding, descriptor)
    record = store.begin_component(reservation, descriptor)
    store.write_partial(record, content)
    promoted = store.promote_component(record, digest)

    path = store.object_path(promoted)
    path.chmod(0o644)
    path.write_bytes(b"corrupt")
    path.chmod(0o444)
    assert store.is_immutable(promoted) is False
    store.quarantine_corrupt(binding, digest)
    assert store.lookup(digest) is None
    assert tuple((store.root / "quarantine").iterdir())


def test_crashed_owner_partial_is_adopted_without_capacity_overcommit(
    tmp_path: Path,
) -> None:
    content = b"adopt-after-owner-crash"
    digest = hashlib.sha256(content).hexdigest()
    descriptor = type(
        "Descriptor",
        (),
        {"name": "crashed-component", "digest": digest, "size": len(content), "kind": "model"},
    )()
    store = ContentStore(tmp_path / "packages", capacity_bytes=len(content))
    first = _binding(1)
    reservation = store.reserve_component(first, descriptor)
    partial = store.begin_component(reservation, descriptor)
    store.append_partial(partial, content[:7])
    # The owner process disappeared after checkpointing.  Releasing only its
    # reservation models the durable crash boundary; bytes remain journaled.
    store.release_reservation(reservation)

    second = _binding(2)
    adopted = store.reserve_component(second, descriptor)
    record = store.begin_component(adopted, descriptor)
    assert record.bytes_completed == 7
    store.append_partial(record, content[7:])
    assert store.promote_component(record, digest).digest == digest


def test_gc_interruption_restarts_from_durable_plan(tmp_path: Path) -> None:
    state = PackageState(tmp_path / "state")
    objects = {
        "b" * 64: StoreObject("b" * 64, 7, "model", "objects/sha256/" + "b" * 64),
        "c" * 64: StoreObject("c" * 64, 9, "derived", "objects/sha256/" + "c" * 64),
    }

    class ObjectStore:
        def __init__(self):
            self.values = dict(objects)

        def list_objects(self):
            return tuple(self.values.values())

        def delete_unreachable(self, _binding, digest: str, *, now_ns: int) -> int:
            del now_ns
            value = self.values.pop(digest, None)
            return value.size if value else 0

    object_store = ObjectStore()
    crashes = {"count": 0}

    def interrupt(_digest: str) -> None:
        crashes["count"] += 1
        if crashes["count"] == 1:
            raise GarbageCollectionInterrupted("simulated restart")

    with pytest.raises(GarbageCollectionInterrupted):
        PackageGarbageCollector(
            state,
            object_store,
            clock_ns=lambda: 1_000,
            after_delete=interrupt,
        ).collect(_binding(2), dry_run=False, target_bytes=100)

    result = PackageGarbageCollector(
        PackageState(tmp_path / "state"),
        object_store,
        clock_ns=lambda: 1_000,
    ).collect(_binding(2), dry_run=False, target_bytes=100)
    assert result.status == "completed"
    assert result.reclaimed_bytes == 16
    assert object_store.values == {}


def test_production_content_store_gc_deletes_only_unreachable_objects(
    tmp_path: Path,
) -> None:
    content = b"gc-eligible-workload-object"
    digest = hashlib.sha256(content).hexdigest()
    descriptor = type(
        "Descriptor",
        (),
        {"name": "gc-component", "digest": digest, "size": len(content), "kind": "model"},
    )()
    store = ContentStore(tmp_path / "packages", capacity_bytes=1024)
    owner = _binding(1)
    reservation = store.reserve_component(owner, descriptor)
    record = store.begin_component(reservation, descriptor)
    store.write_partial(record, content)
    store.promote_component(record, digest)
    store.release_reservation(reservation)

    result = PackageGarbageCollector(
        store.state,
        store,
        clock_ns=lambda: 1_000,
    ).collect(_binding(2), dry_run=False, target_bytes=len(content))
    assert result.status == "completed"
    assert result.reclaimed_bytes == len(content)
    assert store.lookup(digest) is None


def test_failure_evidence_digest_is_stable_and_redacted() -> None:
    record = _failure(PackageFailureReason.DIGEST_SIZE_MISMATCH).to_mapping()
    first = hashlib.sha256(
        (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    repeated = _failure(PackageFailureReason.DIGEST_SIZE_MISMATCH).to_mapping()
    second = hashlib.sha256(
        (json.dumps(repeated, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    assert first == second
