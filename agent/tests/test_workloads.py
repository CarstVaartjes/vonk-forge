from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import vonk_agent.workloads as workloads_module
from vonk_agent.deadlines import MonotonicDeadline
from vonk_agent.releases import ReleaseDescriptor
from vonk_agent.workloads import (
    CompiledAdapterPolicy,
    WorkloadAction,
    WorkloadDisposition,
    WorkloadEvidence,
    WorkloadInspection,
    WorkloadOperations,
    WorkloadRequest,
    WorkloadValidationError,
)

BASE = {
    "schema_version": 1,
    "workload_id": "deepseek-v4-flash-a",
    "release_digest": "4" * 64,
    "adapter_id": "node-runtime-v1",
}


@pytest.mark.parametrize(
    ("action", "extra"),
    [
        (WorkloadAction.PREPARE, {"profile_digest": "5" * 64}),
        (WorkloadAction.START, {"preparation_digest": "6" * 64}),
        (WorkloadAction.STOP, {}),
        (WorkloadAction.HEALTH, {}),
        (WorkloadAction.VERIFY, {"expected_digest": "7" * 64}),
    ],
)
def test_workload_requests_are_exact_and_operation_specific(action, extra) -> None:
    request = WorkloadRequest.parse(action, BASE | extra)

    assert request.action is action
    assert request.workload_id == "deepseek-v4-flash-a"
    assert request.release_digest == "4" * 64

    forbidden = BASE | extra | {"environment": {"PATH": "/tmp"}}
    with pytest.raises(WorkloadValidationError):
        WorkloadRequest.parse(action, forbidden)


def test_workload_request_rejects_adapter_commands_and_cross_action_fields() -> None:
    for payload in (
        BASE | {"command": ["docker", "run"]},
        BASE | {"adapter_id": "pkg.module:factory"},
        BASE | {"cwd": "/tmp"},
        BASE | {"preparation_digest": "6" * 64},
    ):
        with pytest.raises(WorkloadValidationError):
            WorkloadRequest.parse(WorkloadAction.PREPARE, payload)


def test_workload_evidence_and_inspection_never_carry_process_output() -> None:
    evidence = WorkloadEvidence(
        status="healthy",
        action=WorkloadAction.HEALTH,
        workload_id="deepseek-v4-flash-a",
        release_digest="4" * 64,
        evidence_digest="8" * 64,
    )
    inspection = WorkloadInspection(WorkloadDisposition.COMPLETED, evidence)

    assert evidence.to_mapping() == {
        "status": "healthy",
        "action": "health",
        "workload_id": "deepseek-v4-flash-a",
        "release_digest": "4" * 64,
        "evidence_digest": "8" * 64,
    }
    assert "stdout" not in evidence.to_mapping()
    assert inspection.evidence is evidence


def _installed_adapter(
    tmp_path: Path,
    *,
    inspect_disposition: str = "completed",
    execute_status: str | None = None,
    echo_operation_id: str | None = None,
    echo_attempt: int | None = None,
    echo_fence: str | None = None,
):
    release_digest = "4" * 64
    root = tmp_path / "releases" / release_digest
    executable = root / "bin/runtime-adapter"
    record = tmp_path / "adapter-record.json"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import hashlib, json, pathlib, sys\n"
        f"pathlib.Path({str(record)!r}).write_text(json.dumps(sys.argv))\n"
        "if sys.argv[1] == 'inspect':\n"
        f" print(json.dumps({{'schema_version': 1, 'disposition': {inspect_disposition!r}, 'evidence_digest': '8' * 64, 'job_id': sys.argv[sys.argv.index('--job-id') + 1], 'operation_id': {echo_operation_id!r} if {echo_operation_id is not None!r} else sys.argv[sys.argv.index('--operation-id') + 1], 'attempt': {echo_attempt!r} if {echo_attempt is not None!r} else int(sys.argv[sys.argv.index('--attempt') + 1]), 'fence': {echo_fence!r} if {echo_fence is not None!r} else sys.argv[sys.argv.index('--fence') + 1]}}))\n"
        "else:\n"
        f" print(json.dumps({{'schema_version': 1, 'status': {execute_status!r} if {execute_status is not None!r} else ('healthy' if sys.argv[1] == 'health' else 'completed'), 'evidence_digest': '8' * 64, 'job_id': sys.argv[sys.argv.index('--job-id') + 1], 'operation_id': {echo_operation_id!r} if {echo_operation_id is not None!r} else sys.argv[sys.argv.index('--operation-id') + 1], 'attempt': {echo_attempt!r} if {echo_attempt is not None!r} else int(sys.argv[sys.argv.index('--attempt') + 1]), 'fence': {echo_fence!r} if {echo_fence is not None!r} else sys.argv[sys.argv.index('--fence') + 1]}}))\n"
    )
    executable.chmod(0o500)
    descriptor_document = {
        "schema_version": 1,
        "target_name": "node-runtime-2026-08",
        "target_digest": release_digest,
        "target_length": executable.stat().st_size,
        "registry_origin": "https://registry.test.example",
        "repository": "vonk/releases",
        "oci_manifest_digest": "sha256:" + "1" * 64,
        "provenance_digest": "3" * 64,
        "adapter_id": "node-runtime-v1",
        "adapter_version": "1.0.0",
        "architecture": "linux-arm64",
        "agent_min_version": "0.1.0",
        "agent_max_version": "0.1.0",
        "protocol_min_version": 1,
        "protocol_max_version": 1,
        "members": [{
            "path": "bin/runtime-adapter",
            "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
            "size": executable.stat().st_size,
            "mode": 0o500,
            "uid": os.geteuid(),
            "gid": os.getegid(),
        }],
    }
    descriptor = ReleaseDescriptor.parse(descriptor_document)
    (root / ".install-receipt.json").write_text(
        json.dumps(
            {"schema_version": 1, "release": descriptor.to_mapping()},
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n"
    )
    (root / ".install-receipt.json").chmod(0o400)
    class Trust:
        def authorize(self, request, deadline):
            return descriptor

    operations = WorkloadOperations._for_test(
        tmp_path / "releases",
        {"node-runtime-v1": CompiledAdapterPolicy(
            "node-runtime-v1", "bin/runtime-adapter", 2, 64 * 1024,
            allow_unprivileged_test_files=True,
        )},
        Trust(),
    )
    return operations, record


def test_compiled_adapter_executes_fixed_action_and_returns_redacted_evidence(tmp_path: Path) -> None:
    operations, record = _installed_adapter(tmp_path)
    request = WorkloadRequest.parse(WorkloadAction.HEALTH, BASE)

    evidence = operations.execute(
        request, datetime.now(UTC) + timedelta(seconds=2),
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        1,
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )

    argv = json.loads(record.read_text())
    assert argv[0].startswith("/proc/self/fd/")
    assert argv[1:] == [
        "health", "--workload-id", "deepseek-v4-flash-a",
        "--job-id", "11111111-1111-4111-8111-111111111111",
        "--operation-id", "22222222-2222-4222-8222-222222222222",
        "--attempt", "1", "--fence", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    ]
    assert evidence.status == "healthy"
    assert evidence.evidence_digest == "8" * 64


def test_workload_reverification_receives_the_exact_operation_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations, _ = _installed_adapter(tmp_path)
    request = WorkloadRequest.parse(WorkloadAction.HEALTH, BASE)
    original_verify = workloads_module.verify_installed_release_fd
    seen = []

    def record_verify(root_fd, deadline=None):
        seen.append(deadline)
        return original_verify(root_fd)

    monkeypatch.setattr(
        workloads_module, "verify_installed_release_fd", record_verify
    )
    deadline = MonotonicDeadline(
        datetime.now(UTC) + timedelta(seconds=2), time.monotonic() + 2
    )
    operations.execute(
        request, deadline,
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        1,
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )

    assert seen == [deadline]


def test_mutating_adapter_inspection_is_exact_and_ambiguous_state_never_retries(tmp_path: Path) -> None:
    operations, record = _installed_adapter(
        tmp_path, inspect_disposition="operator-intervention"
    )
    request = WorkloadRequest.parse(
        WorkloadAction.START, BASE | {"preparation_digest": "6" * 64}
    )

    inspection = operations.inspect(
        request,
        datetime.now(UTC) + timedelta(seconds=2),
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        1,
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )

    assert inspection.disposition is WorkloadDisposition.OPERATOR_INTERVENTION
    argv = json.loads(record.read_text())
    assert argv[1:] == [
        "inspect", "--action", "start", "--workload-id",
        "deepseek-v4-flash-a", "--preparation-digest", "6" * 64,
        "--job-id", "11111111-1111-4111-8111-111111111111",
        "--operation-id", "22222222-2222-4222-8222-222222222222",
        "--attempt", "1", "--fence", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    ]


def test_compiled_adapter_rejects_unknown_adapter_and_release_mismatch(tmp_path: Path) -> None:
    operations, record = _installed_adapter(tmp_path)
    deadline = datetime.now(UTC) + timedelta(seconds=2)
    with pytest.raises(WorkloadValidationError):
        operations.execute(
            WorkloadRequest.parse(
                WorkloadAction.HEALTH, BASE | {"adapter_id": "unknown-v1"}
            ),
            deadline,
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222", 1,
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )
    with pytest.raises(WorkloadValidationError):
        operations.execute(
            WorkloadRequest.parse(
                WorkloadAction.HEALTH, BASE | {"release_digest": "9" * 64}
            ),
            deadline,
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222", 1,
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )
    assert not record.exists()


def test_production_adapter_registry_is_compiled_and_not_constructor_supplied(tmp_path: Path) -> None:
    class Trust:
        def authorize(self, request, deadline):
            raise AssertionError

    operations = WorkloadOperations(tmp_path / "releases", Trust())
    assert set(operations.adapter_ids) == {"node-runtime-v1"}
    with pytest.raises(WorkloadValidationError):
        WorkloadOperations(tmp_path / "releases", {"attacker": object()})


def test_adapter_result_rejects_duplicate_fields_and_wrong_action_status(tmp_path: Path) -> None:
    with pytest.raises(WorkloadValidationError, match="duplicate"):
        workloads_module._adapter_document(
            b'{"schema_version":1,"status":"healthy","status":"completed"}',
            {"schema_version", "status"},
        )
    operations, _ = _installed_adapter(tmp_path, execute_status="completed")
    with pytest.raises(WorkloadValidationError):
        operations.execute(
            WorkloadRequest.parse(WorkloadAction.HEALTH, BASE),
            datetime.now(UTC) + timedelta(seconds=2),
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222", 1,
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )


def test_forged_receipt_and_matching_member_are_not_signed_authorization(tmp_path: Path) -> None:
    operations, record = _installed_adapter(tmp_path)
    root = tmp_path / "releases" / ("4" * 64)
    executable = root / "bin/runtime-adapter"
    executable.chmod(0o700)
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    executable.chmod(0o500)
    receipt = root / ".install-receipt.json"
    original = json.loads(receipt.read_text())["release"]
    original["target_length"] = executable.stat().st_size
    original["members"][0]["size"] = executable.stat().st_size
    original["members"][0]["sha256"] = hashlib.sha256(
        executable.read_bytes()
    ).hexdigest()
    receipt.chmod(0o600)
    receipt.write_text(
        json.dumps(
            {"schema_version": 1, "release": original},
            sort_keys=True, separators=(",", ":"),
        ) + "\n"
    )
    receipt.chmod(0o400)

    with pytest.raises(WorkloadValidationError, match="not installed"):
        operations.execute(
            WorkloadRequest.parse(WorkloadAction.HEALTH, BASE),
            datetime.now(UTC) + timedelta(seconds=2),
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222", 1,
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )
    assert not record.exists()


@pytest.mark.parametrize(
    "override",
    [
        {"echo_operation_id": "33333333-3333-4333-8333-333333333333"},
        {"echo_attempt": 2},
        {"echo_attempt": True},
        {"echo_fence": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"},
    ],
)
def test_adapter_stale_or_cross_operation_binding_is_never_accepted(
    tmp_path: Path, override: dict[str, object]
) -> None:
    operations, _ = _installed_adapter(tmp_path, **override)
    request = WorkloadRequest.parse(WorkloadAction.HEALTH, BASE)
    with pytest.raises(WorkloadValidationError, match="binding"):
        operations.execute(
            request, datetime.now(UTC) + timedelta(seconds=2),
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222", 1,
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )

    mutation = WorkloadRequest.parse(
        WorkloadAction.START, BASE | {"preparation_digest": "6" * 64}
    )
    inspection = operations.inspect(
        mutation, datetime.now(UTC) + timedelta(seconds=2),
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222", 1,
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )
    assert inspection.disposition is WorkloadDisposition.OPERATOR_INTERVENTION


def test_inspection_does_not_mask_unexpected_programming_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations, _ = _installed_adapter(tmp_path)
    monkeypatch.setattr(
        operations,
        "_open_adapter",
        lambda *_args: (_ for _ in ()).throw(AssertionError("programming defect")),
    )

    with pytest.raises(AssertionError, match="programming defect"):
        operations.inspect(
            WorkloadRequest.parse(WorkloadAction.HEALTH, BASE),
            datetime.now(UTC) + timedelta(seconds=2),
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222",
            1,
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )
