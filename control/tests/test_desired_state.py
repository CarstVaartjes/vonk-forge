from __future__ import annotations

import ctypes
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import uuid
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import canonical_message
from vonk_control.agent_jobs import AgentJobService
from vonk_control.desired_state import (
    CurrentWorkloadState,
    DesiredStateObservation,
    DesiredStateResolver,
    durable_desired_state_observations,
)
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Base,
    Job,
    Observation,
    Reconciliation,
)
from vonk_control.presence import ManagementAddressPolicy
from vonk_control.repository import RepositoryService
from vonk_control.route_runtime import (
    AcceptedEndpointEvidence,
    AtomicRouteBundlePublisher,
    RouteBundleRequest,
    endpoint_evidence_digest,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
DEFINITION_HASH = "d" * 64
REQUIRED_CAPABILITIES = (
    "release.install",
    "workload.health",
    "workload.prepare",
    "workload.start",
    "workload.stop",
    "workload.verify",
)
AGENT_CAPABILITIES = ("node.probe", *REQUIRED_CAPABILITIES)


def _provide_linux_process_apis(monkeypatch: pytest.MonkeyPatch) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if not hasattr(os, "memfd_create"):
        create_memfd = libc.memfd_create
        create_memfd.argtypes = [ctypes.c_char_p, ctypes.c_uint]
        create_memfd.restype = ctypes.c_int

        def memfd_create(name: str, flags: int = 0) -> int:
            ctypes.set_errno(0)
            descriptor = create_memfd(os.fsencode(name), flags)
            if descriptor < 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error), name)
            return descriptor

        monkeypatch.setattr(os, "memfd_create", memfd_create, raising=False)
    if not hasattr(os, "pidfd_open"):
        open_pidfd = libc.pidfd_open
        open_pidfd.argtypes = [ctypes.c_int, ctypes.c_uint]
        open_pidfd.restype = ctypes.c_int

        def pidfd_open(pid: int, flags: int = 0) -> int:
            ctypes.set_errno(0)
            descriptor = open_pidfd(pid, flags)
            if descriptor < 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error), pid)
            return descriptor

        monkeypatch.setattr(os, "pidfd_open", pidfd_open, raising=False)
    if not hasattr(signal, "pidfd_send_signal"):
        send_pidfd_signal = libc.pidfd_send_signal
        send_pidfd_signal.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint,
        ]
        send_pidfd_signal.restype = ctypes.c_int

        def pidfd_send_signal(
            pidfd: int,
            sig: int,
            siginfo: None = None,
            flags: int = 0,
        ) -> None:
            if siginfo is not None:
                raise TypeError("siginfo must be None")
            ctypes.set_errno(0)
            if send_pidfd_signal(pidfd, sig, None, flags) != 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))

        monkeypatch.setattr(
            signal, "pidfd_send_signal", pidfd_send_signal, raising=False
        )
    for module, values in (
        (os, {"MFD_CLOEXEC": 0x0001, "MFD_ALLOW_SEALING": 0x0002}),
        (
            fcntl,
            {
                "F_ADD_SEALS": 1033,
                "F_GET_SEALS": 1034,
                "F_SEAL_SEAL": 0x0001,
                "F_SEAL_SHRINK": 0x0002,
                "F_SEAL_GROW": 0x0004,
                "F_SEAL_WRITE": 0x0008,
            },
        ),
    ):
        for name, value in values.items():
            if not hasattr(module, name):
                monkeypatch.setattr(module, name, value, raising=False)


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=os.environ
        | {
            "GIT_AUTHOR_NAME": "Resolver Test",
            "GIT_AUTHOR_EMAIL": "resolver@example.invalid",
            "GIT_COMMITTER_NAME": "Resolver Test",
            "GIT_COMMITTER_EMAIL": "resolver@example.invalid",
        },
    ).stdout.strip()


def _node_id(index: int) -> str:
    return f"spk_{index:032x}"


def _gate_id(index: int) -> str:
    return f"gate:{_node_id(index)}:node.probe"


def _fleet_toml(count: int, *, reversed_nodes: bool = False) -> str:
    indexes = list(range(count))
    if reversed_nodes:
        indexes.reverse()
    lines = ["schema_version = 2", ""]
    for index in indexes:
        node_id = _node_id(index)
        lines.extend(
            [
                f'[nodes."{node_id}"]',
                f'display_name = "Node {index}"',
                f'hostname = "node-{index}.example.invalid"',
                'lifecycle = "ready"',
                "",
                f'[nodes."{node_id}".management]',
                f'host = "node-{index}.example.invalid"',
                'user = "operator"',
                "port = 22",
                "",
                f'[nodes."{node_id}".labels]',
                'pool = "default"',
                "",
            ]
        )
    return "\n".join(lines)


def _profile_toml(count: int, *, definition_hash: str = DEFINITION_HASH) -> str:
    start_order = "workers-before-entrypoint" if count > 1 else "independent"
    stop_order = "entrypoint-before-workers" if count > 1 else "independent"
    return f'''schema_version = 2
id = "inference"
accepted_evidence = "inventory/reports/inference.json"
workloads = ["model"]

[[requirements]]
workload = "model"
definition_hash = "{definition_hash}"
node_count = {count}
min_memory_bytes = 100
min_disk_bytes = 200
exclusive = true
distributed_supported = true

[requirements.required_labels]
pool = "default"

[endpoints]
chat = "model"

[quotas.chat]
requests_per_minute = 30
tokens_per_minute = 10000

[lifecycle]
start_order = "{start_order}"
stop_order = "{stop_order}"
'''


def _workload_toml(
    *,
    definition_hash: str = DEFINITION_HASH,
    distributed_supported: bool = True,
    adapter: str = "node-runtime-v1",
) -> str:
    distributed = str(distributed_supported).lower()
    return f'''schema_version = 2
id = "model"
adapter = "{adapter}"
definition_hash = "{definition_hash}"
conflicts = []
distributed_supported = {distributed}
'''


def _topology(count: int) -> dict[str, object]:
    nodes = [_node_id(index) for index in range(count)]
    links: list[dict[str, object]] = []
    if count > 1:
        links.append(
            {
                "id": "fabric",
                "kind": "switched-rdma",
                "accepted": True,
                "endpoints": [
                    {"node_id": node_id, "interface": f"fabric{index}"}
                    for index, node_id in enumerate(nodes)
                ],
            }
        )
    return {"schema_version": 1, "nodes": nodes, "links": links}


def _release(
    *,
    definition_hash: str = DEFINITION_HASH,
    operations: tuple[str, ...] = REQUIRED_CAPABILITIES,
    adapter: str = "node-runtime-v1",
) -> dict[str, object]:
    requests: dict[str, dict[str, object]] = {}
    for operation in operations:
        if operation == "release.install":
            continue
        action = operation.removeprefix("workload.")
        request: dict[str, object] = {
            "schema_version": 1,
            "workload_id": "model",
            "release_digest": "a" * 64,
            "adapter_id": adapter,
        }
        if action == "prepare":
            request["profile_digest"] = "c" * 64
        elif action == "start":
            request["preparation_digest"] = "e" * 64
        elif action == "verify":
            request["expected_digest"] = "f" * 64
        requests[action] = request
    return {
        "schema_version": 1,
        "workload_id": "model",
        "definition_hash": definition_hash,
        "release_request": {
            "schema_version": 1,
            "target_name": "model",
            "oci_manifest_digest": "sha256:" + "9" * 64,
            "target_digest": "a" * 64,
            "provenance_digest": "b" * 64,
            "adapter_id": adapter,
        },
        "workload_requests": requests,
        "endpoint": {"scheme": "http", "port": 8000, "path": "/v1"},
    }


def _repository(
    tmp_path: Path,
    count: int,
    *,
    reversed_nodes: bool = False,
    requirement_hash: str = DEFINITION_HASH,
    workload_hash: str = DEFINITION_HASH,
    release_hash: str = DEFINITION_HASH,
    operations: tuple[str, ...] = REQUIRED_CAPABILITIES,
    workload_distributed_supported: bool = True,
    workload_adapter: str = "node-runtime-v1",
) -> tuple[RepositoryService, str, dict[str, bytes]]:
    root = tmp_path / "repository"
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    documents = {
        "inventory/fleet.toml": _fleet_toml(
            count, reversed_nodes=reversed_nodes
        ).encode(),
        "inventory/topology.json": json.dumps(
            _topology(count), sort_keys=not reversed_nodes, separators=(",", ":")
        ).encode(),
        "inventory/reports/inference.json": b'{"accepted":true,"schema_version":1}',
        "config/cluster-profiles/inference.toml": _profile_toml(
            count, definition_hash=requirement_hash
        ).encode(),
        "config/workloads/model.toml": _workload_toml(
            definition_hash=workload_hash,
            distributed_supported=workload_distributed_supported,
            adapter=workload_adapter,
        ).encode(),
        "manifests/releases/model.json": json.dumps(
            _release(
                definition_hash=release_hash,
                operations=operations,
                adapter=workload_adapter,
            ),
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    }
    for name, content in documents.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "desired state")
    return RepositoryService(root), _git(root, "rev-parse", "HEAD"), documents


def _observations(count: int) -> tuple[DesiredStateObservation, ...]:
    return tuple(
        DesiredStateObservation(
            node_id=_node_id(index),
            observed_at=NOW - timedelta(seconds=index),
            healthy=True,
            memory_available_bytes=1_000,
            disk_available_bytes=2_000,
            occupied=False,
            agent_state="active",
            protocol_version=1,
            capabilities=AGENT_CAPABILITIES,
            compute_occupancy="clean",
        )
        for index in range(count)
    )


def _managed_group(
    count: int,
    *,
    workload_id: str = "model",
    release_digest: str = "a" * 64,
    nodes: tuple[str, ...] | None = None,
    profile_digest: str = "c" * 64,
    preparation_digest: str = "e" * 64,
) -> CurrentWorkloadState:
    placement = nodes or tuple(_node_id(index) for index in range(count))
    return CurrentWorkloadState(
        workload_id,
        release_digest,
        "node-runtime-v1",
        nodes=placement,
        entrypoint_node_id=placement[0],
        definition_hash=DEFINITION_HASH,
        profile_digest=profile_digest,
        preparation_digest=preparation_digest,
        start_order="workers-before-entrypoint" if count > 1 else "independent",
        stop_order="entrypoint-before-workers" if count > 1 else "independent",
    )


def _persisted_start_plan(
    node_id: str,
) -> tuple[dict[str, object], str, dict[str, object], str, dict[str, object]]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "workload_id": "model",
        "release_digest": "8" * 64,
        "adapter_id": "node-runtime-v1",
        "preparation_digest": "c" * 64,
    }
    payload_digest = hashlib.sha256(canonical_message(payload)).hexdigest()
    graph_node: dict[str, object] = {
        "operation_id": "model:node:workload.start",
        "node_id": node_id,
        "workload_id": "model",
        "kind": "workload.start",
        "dependencies": [],
        "compensation_kind": "workload.stop",
        "payload_digest": payload_digest,
    }
    graph: dict[str, object] = {
        "schema_version": 1,
        "base_commit": "a" * 40,
        "targets": [node_id],
        "nodes": [graph_node],
    }
    graph_digest = hashlib.sha256(canonical_message(graph)).hexdigest()
    resolved: dict[str, object] = {
        "commit": "a" * 40,
        "targets": [node_id],
        "placements": {"model": [node_id]},
        "routes": {},
        "releases": {},
        "workload_groups": {
            "model": {
                "nodes": [node_id],
                "entrypoint_node_id": node_id,
                "release_digest": "8" * 64,
                "adapter_id": "node-runtime-v1",
                "definition_hash": DEFINITION_HASH,
                "profile_digest": "c" * 64,
                "preparation_digest": "c" * 64,
                "lifecycle": {
                    "start_order": "independent",
                    "stop_order": "independent",
                },
            }
        },
        "input_digests": {"fleet": "f" * 64},
        "fleet_evidence_digest": "e" * 64,
        "operation_graph": graph,
        "operation_payloads": {"model:node:workload.start": payload},
        "agent_protocol_range": [1, 1],
    }
    plan_digest = hashlib.sha256(canonical_message(resolved)).hexdigest()
    return graph, graph_digest, resolved, plan_digest, payload


def _resolve(
    repository: RepositoryService,
    commit: str,
    observations: tuple[DesiredStateObservation, ...],
):
    return DesiredStateResolver(repository, clock=lambda: NOW).resolve(
        commit, "inference", observations
    )


@pytest.mark.parametrize("count", [1, 2, 16])
def test_resolves_one_two_and_sixteen_nodes_from_exact_repository_objects(
    tmp_path: Path, count: int
) -> None:
    repository, commit, documents = _repository(tmp_path, count)

    plan = _resolve(repository, commit, _observations(count))

    targets = tuple(_node_id(index) for index in range(count))
    assert plan.commit == commit
    assert plan.targets == targets
    assert plan.placements == {"model": targets}
    assert plan.agent_protocol_range == (1, 1)
    assert plan.routes == {
        "chat": {
            "workload_id": "model",
            "nodes": targets,
            "entrypoint_node_id": targets[0],
            "scheme": "http",
            "port": 8000,
            "path": "/v1",
            "quota": {
                "requests_per_minute": 30,
                "tokens_per_minute": 10000,
            },
            "quota_digest": hashlib.sha256(
                canonical_message(
                    {
                        "requests_per_minute": 30,
                        "tokens_per_minute": 10000,
                    }
                )
            ).hexdigest(),
        }
    }
    assert plan.workload_groups == {
        "model": {
            "nodes": targets,
            "entrypoint_node_id": targets[0],
            "release_digest": "a" * 64,
            "adapter_id": "node-runtime-v1",
            "definition_hash": DEFINITION_HASH,
            "profile_digest": "c" * 64,
            "preparation_digest": "e" * 64,
            "lifecycle": {
                "start_order": (
                    "workers-before-entrypoint" if count > 1 else "independent"
                ),
                "stop_order": (
                    "entrypoint-before-workers" if count > 1 else "independent"
                ),
            },
        }
    }
    assert plan.input_digests == {
        path: hashlib.sha256(content).hexdigest()
        for path, content in sorted(documents.items())
    }
    assert plan.operation_graph.reconciliation_id
    assert len(plan.operation_graph.nodes) == count * 6
    assert {node.kind for node in plan.operation_graph.nodes} == {
        "node.probe",
        "release.install",
        "workload.prepare",
        "workload.start",
        "workload.health",
        "workload.verify",
    }
    assert all(
        node.operation_id in plan.operation_payloads
        and len(node.payload_digest) == 64
        for node in plan.operation_graph.nodes
    )


def test_planner_route_publishes_with_the_exact_canonical_quota_digest(
    tmp_path: Path,
) -> None:
    """Adding file-format whitespace to quota hashing rejects real planner output."""
    repository, commit, _documents = _repository(tmp_path, 1)
    plan = _resolve(repository, commit, _observations(1))
    node_id = _node_id(0)
    operation_id = f"model:{node_id}:workload.verify"
    verify_digest = "e" * 64
    address = "10.0.0.42"
    reconciliation_id = str(uuid.uuid4())
    evidence_digest = endpoint_evidence_digest(
        node_id=node_id,
        address=address,
        observed_at=NOW,
        operation_id=operation_id,
        verify_evidence_digest=verify_digest,
    )
    request = RouteBundleRequest(
        reconciliation_id=reconciliation_id,
        plan_digest=plan.digest,
        evidence_set_digest="f" * 64,
        routes=plan.routes,
        endpoints={
            node_id: AcceptedEndpointEvidence(
                node_id=node_id,
                address=address,
                observed_at=NOW,
                operation_id=operation_id,
                verify_evidence_digest=verify_digest,
                evidence_digest=evidence_digest,
            )
        },
        expires_at=NOW + timedelta(seconds=120),
    )

    marker = AtomicRouteBundlePublisher(
        tmp_path / "route-runtime",
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=lambda: NOW,
    ).publish(request)

    assert marker.reconciliation_id == reconciliation_id
    assert marker.plan_digest == plan.digest


def test_every_emitted_payload_is_accepted_by_the_exact_agent_parser(
    tmp_path: Path,
) -> None:
    repository, commit, _ = _repository(tmp_path, 2)
    plan = _resolve(repository, commit, _observations(2))
    agent_source = Path(__file__).parents[2] / "agent" / "src"
    sys.path.insert(0, str(agent_source))
    try:
        from vonk_agent.releases import ReleaseRequest
        from vonk_agent.workloads import WorkloadAction, WorkloadRequest

        for node in plan.operation_graph.nodes:
            payload = plan.operation_payloads[node.operation_id]
            if node.kind == "node.probe":
                assert payload == {"require_active_nvidia_compute_processes": 0}
                continue
            if node.kind == "release.install":
                ReleaseRequest.parse(payload)
            else:
                action = WorkloadAction(node.kind.removeprefix("workload."))
                WorkloadRequest.parse(action, payload)
            assert node.payload_digest == hashlib.sha256(
                canonical_message(payload)
            ).hexdigest()
    finally:
        sys.path.remove(str(agent_source))


def test_generated_workload_graph_executes_through_production_agent_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _provide_linux_process_apis(monkeypatch)
    repository, commit, _ = _repository(tmp_path / "repository", 1)
    plan = _resolve(repository, commit, _observations(1))
    agent_source = Path(__file__).parents[2] / "agent" / "src"
    sys.path.insert(0, str(agent_source))
    try:
        from vonk_agent.operations import OperationContext, OperationRegistry
        from vonk_agent.releases import ReleaseDescriptor
        from vonk_agent.state import AgentStateStore
        from vonk_agent.workloads import CompiledAdapterPolicy, WorkloadOperations
        from vonk_agent_protocol import AgentClaim, AgentOperation

        release_digest = "a" * 64
        release_root = tmp_path / "releases" / release_digest
        executable = release_root / "bin/runtime-adapter"
        executable.parent.mkdir(parents=True)
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "statuses = {'prepare': 'prepared', 'start': 'started', "
            "'stop': 'stopped', 'health': 'healthy', 'verify': 'verified'}\n"
            "print(json.dumps({'schema_version': 1, "
            "'status': statuses[sys.argv[1]], 'evidence_digest': '8' * 64, "
            "'job_id': sys.argv[sys.argv.index('--job-id') + 1], "
            "'operation_id': sys.argv[sys.argv.index('--operation-id') + 1], "
            "'attempt': int(sys.argv[sys.argv.index('--attempt') + 1]), "
            "'fence': sys.argv[sys.argv.index('--fence') + 1]}))\n"
        )
        executable.chmod(0o500)
        descriptor = ReleaseDescriptor.parse(
            {
                "schema_version": 1,
                "target_name": "model",
                "target_digest": release_digest,
                "target_length": executable.stat().st_size,
                "registry_origin": "https://registry.example.invalid",
                "repository": "vonk/releases",
                "oci_manifest_digest": "sha256:" + "9" * 64,
                "provenance_digest": "b" * 64,
                "adapter_id": "node-runtime-v1",
                "adapter_version": "1.0.0",
                "architecture": "linux-arm64",
                "agent_min_version": "0.1.0",
                "agent_max_version": "0.1.0",
                "protocol_min_version": 1,
                "protocol_max_version": 1,
                "members": [
                    {
                        "path": "bin/runtime-adapter",
                        "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                        "size": executable.stat().st_size,
                        "mode": 0o500,
                        "uid": os.geteuid(),
                        "gid": os.getegid(),
                    }
                ],
            }
        )
        (release_root / ".install-receipt.json").write_text(
            json.dumps(
                {"schema_version": 1, "release": descriptor.to_mapping()},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        (release_root / ".install-receipt.json").chmod(0o400)

        class Trust:
            def authorize(self, request, deadline):
                return descriptor

        workloads = WorkloadOperations._for_test(
            tmp_path / "releases",
            {
                "node-runtime-v1": CompiledAdapterPolicy(
                    "node-runtime-v1",
                    "bin/runtime-adapter",
                    2,
                    64 * 1024,
                    allow_unprivileged_test_files=True,
                )
            },
            Trust(),
        )

        class NeverProbe:
            def collect(self, deadline):
                raise AssertionError("workload graph reached probe dispatch")

        registry = OperationRegistry()
        statuses = []
        for index, node in enumerate(plan.operation_graph.nodes):
            if not node.kind.startswith("workload."):
                continue
            payload = plan.operation_payloads[node.operation_id]
            claim = AgentClaim(
                schema_version=1,
                job_id=str(uuid.uuid4()),
                operation_id=str(uuid.uuid4()),
                attempt=1,
                fence=str(uuid.uuid4()),
                node_id=node.node_id,
                operation=AgentOperation(node.kind),
                base_commit=commit,
                payload_digest=node.payload_digest,
                payload=payload,
                deadline=datetime.now(UTC) + timedelta(seconds=5),
            )
            executed = registry.execute(
                claim,
                OperationContext(
                    node.node_id,
                    AgentStateStore(tmp_path / f"agent-state-{index}"),
                    NeverProbe(),
                    workloads=workloads,
                ),
            )
            assert executed.result.state == "succeeded", executed.result.result
            statuses.append(executed["evidence"]["status"])
        assert statuses == ["prepared", "started", "healthy", "verified"]
    finally:
        sys.path.remove(str(agent_source))


def test_start_and_stop_dependencies_follow_lifecycle_order(tmp_path: Path) -> None:
    repository, commit, _ = _repository(tmp_path, 2)
    plan = _resolve(
        repository,
        commit,
        tuple(
            replace(
                observation,
                occupied=True,
                compute_occupancy="managed",
                memory_total_bytes=4_000,
                disk_total_bytes=8_000,
                current_workloads=(
                    _managed_group(2, release_digest="8" * 64),
                ),
            )
            for observation in _observations(2)
        ),
    )
    head, worker = plan.targets
    graph = plan.operation_graph

    assert graph.dependencies(f"model:{head}:workload.start") == (
        f"model:{head}:workload.prepare",
        f"model:{worker}:workload.start",
    )
    assert graph.dependencies(f"model:{head}:workload.stop") == ()
    assert graph.dependencies(f"model:{worker}:workload.stop") == (
        f"model:{head}:workload.stop",
    )
    probes = tuple(f"gate:{node_id}:node.probe" for node_id in (head, worker))
    for probe_id in probes:
        assert graph.dependencies(probe_id) == (
            f"model:{head}:workload.stop",
            f"model:{worker}:workload.stop",
        )
        assert plan.operation_payloads[probe_id] == {
            "require_active_nvidia_compute_processes": 0
        }
    assert graph.dependencies(f"model:{head}:release.install") == (
        *probes,
    )
    assert plan.operation_payloads[f"model:{head}:workload.stop"] == {
        "schema_version": 1,
        "workload_id": "model",
        "release_digest": "8" * 64,
        "adapter_id": "node-runtime-v1",
    }


def test_fresh_deploy_omits_stop_and_upgrade_stops_only_current_release(
    tmp_path: Path,
) -> None:
    repository, commit, _ = _repository(tmp_path, 1)
    fresh = _resolve(repository, commit, _observations(1))
    assert "workload.stop" not in {node.kind for node in fresh.operation_graph.nodes}
    fresh_probe = f"gate:{_node_id(0)}:node.probe"
    assert fresh.operation_graph.dependencies(fresh_probe) == ()
    assert fresh.operation_graph.dependencies(
        f"model:{_node_id(0)}:release.install"
    ) == (fresh_probe,)

    current = replace(
        _observations(1)[0],
        occupied=True,
        compute_occupancy="managed",
        memory_total_bytes=4_000,
        disk_total_bytes=8_000,
        current_workloads=(
            _managed_group(1, release_digest="8" * 64),
        ),
    )
    upgrade = _resolve(repository, commit, (current,))
    stop = next(
        node for node in upgrade.operation_graph.nodes if node.kind == "workload.stop"
    )
    assert upgrade.operation_payloads[stop.operation_id]["release_digest"] == "8" * 64
    assert upgrade.operation_payloads[stop.operation_id]["release_digest"] != "a" * 64


def test_fully_occupied_managed_node_can_replace_workload_using_total_capacity(
    tmp_path: Path,
) -> None:
    repository, _, _ = _repository(tmp_path, 1)
    root = repository.root
    profile = root / "config/cluster-profiles/inference.toml"
    profile.write_text(
        profile.read_text()
        .replace('workloads = ["model"]', 'workloads = ["replacement"]')
        .replace('workload = "model"', 'workload = "replacement"')
        .replace('chat = "model"', 'chat = "replacement"')
    )
    workload = root / "config/workloads/model.toml"
    replacement = root / "config/workloads/replacement.toml"
    replacement.write_text(workload.read_text().replace('id = "model"', 'id = "replacement"'))
    workload.unlink()
    release = root / "manifests/releases/model.json"
    replacement_release = root / "manifests/releases/replacement.json"
    replacement_release.write_text(
        release.read_text()
        .replace('"workload_id":"model"', '"workload_id":"replacement"')
        .replace('"target_name":"model"', '"target_name":"replacement"')
    )
    release.unlink()
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "replace workload")
    commit = _git(root, "rev-parse", "HEAD")
    observation = replace(
        _observations(1)[0],
        occupied=True,
        compute_occupancy="managed",
        memory_total_bytes=4_000,
        disk_total_bytes=8_000,
        current_workloads=(
            _managed_group(1, release_digest="8" * 64),
        ),
    )

    plan = _resolve(repository, commit, (observation,))

    node_id = observation.node_id
    stop_id = f"model:{node_id}:workload.stop"
    install_id = f"replacement:{node_id}:release.install"
    assert plan.placements == {"replacement": (node_id,)}
    assert plan.operation_payloads[stop_id] == {
        "schema_version": 1,
        "workload_id": "model",
        "release_digest": "8" * 64,
        "adapter_id": "node-runtime-v1",
    }
    assert plan.operation_graph.dependencies(install_id) == (_gate_id(0),)
    assert plan.operation_graph.dependencies(_gate_id(0)) == (stop_id,)


def test_reclaimable_occupied_node_without_total_capacity_fails_closed(
    tmp_path: Path,
) -> None:
    repository, commit, _ = _repository(tmp_path, 1)
    observation = replace(
        _observations(1)[0],
        occupied=True,
        compute_occupancy="managed",
        current_workloads=(
            _managed_group(1, release_digest="8" * 64),
        ),
    )

    with pytest.raises(ValueError, match="total capacity"):
        _resolve(repository, commit, (observation,))


def test_unmanaged_occupancy_is_never_reclaimed_for_desired_placement(
    tmp_path: Path,
) -> None:
    repository, commit, _ = _repository(tmp_path, 1)
    observation = replace(
        _observations(1)[0],
        occupied=True,
        compute_occupancy="unmanaged",
        memory_total_bytes=4_000,
        disk_total_bytes=8_000,
        current_workloads=(
            CurrentWorkloadState(
                "external", "8" * 64, "node-runtime-v1", managed=False
            ),
        ),
    )

    with pytest.raises(ValueError, match="insufficient eligible nodes"):
        _resolve(repository, commit, (observation,))


def test_nonexclusive_desired_requirement_is_rejected_even_with_unmanaged_occupancy(
    tmp_path: Path,
) -> None:
    repository, _, _ = _repository(tmp_path, 1)
    profile = repository.root / "config/cluster-profiles/inference.toml"
    profile.write_text(profile.read_text().replace("exclusive = true", "exclusive = false"))
    _git(repository.root, "add", ".")
    _git(repository.root, "commit", "-qm", "request co-location")
    commit = _git(repository.root, "rev-parse", "HEAD")
    observation = replace(
        _observations(1)[0],
        occupied=True,
        compute_occupancy="unmanaged",
        current_workloads=(
            CurrentWorkloadState(
                "external", "8" * 64, "node-runtime-v1", managed=False
            ),
        ),
    )

    with pytest.raises(ValueError, match="exclusive"):
        _resolve(repository, commit, (observation,))


@pytest.mark.parametrize("mixed_unmanaged", [False, True])
def test_current_co_resident_groups_are_rejected(
    tmp_path: Path, mixed_unmanaged: bool
) -> None:
    repository, commit, _ = _repository(tmp_path, 1)
    other = CurrentWorkloadState(
        "external",
        "8" * 64,
        "node-runtime-v1",
        managed=not mixed_unmanaged,
        **(
            {
                "nodes": (_node_id(0),),
                "entrypoint_node_id": _node_id(0),
                "definition_hash": DEFINITION_HASH,
                "profile_digest": "c" * 64,
                "preparation_digest": "e" * 64,
                "start_order": "independent",
                "stop_order": "independent",
            }
            if not mixed_unmanaged
            else {}
        ),
    )
    with pytest.raises(ValueError, match="co-resident"):
        observation = replace(
            _observations(1)[0],
            occupied=True,
            compute_occupancy="managed",
            memory_total_bytes=4_000,
            disk_total_bytes=8_000,
            current_workloads=(_managed_group(1), other),
        )
        _resolve(repository, commit, (observation,))


def test_scale_up_restarts_every_member_after_stopping_the_complete_old_group(
    tmp_path: Path,
) -> None:
    repository, commit, _ = _repository(tmp_path, 2)
    head = replace(
        _observations(2)[0],
        occupied=True,
        compute_occupancy="managed",
        memory_total_bytes=4_000,
        disk_total_bytes=8_000,
        current_workloads=(
            _managed_group(1),
        ),
    )

    plan = _resolve(repository, commit, (head, _observations(2)[1]))

    worker = _node_id(1)
    kinds_by_node = {
        node_id: {
            item.kind
            for item in plan.operation_graph.nodes
            if item.node_id == node_id
        }
        for node_id in plan.targets
    }
    assert plan.placements == {"model": (_node_id(0), worker)}
    assert kinds_by_node[_node_id(0)] == {
        "node.probe",
        "workload.stop",
        "release.install",
        "workload.prepare",
        "workload.start",
        "workload.health",
        "workload.verify",
    }
    assert kinds_by_node[worker] == {
        "node.probe",
        "release.install",
        "workload.prepare",
        "workload.start",
        "workload.health",
        "workload.verify",
    }
    stop_id = f"model:{_node_id(0)}:workload.stop"
    assert plan.operation_graph.dependencies(
        f"model:{worker}:release.install"
    ) == (_gate_id(0), _gate_id(1))
    assert plan.operation_graph.dependencies(_gate_id(0)) == (stop_id,)
    assert plan.operation_graph.dependencies(_gate_id(1)) == (stop_id,)


def test_scale_down_stops_all_old_members_then_restarts_desired_singleton(
    tmp_path: Path,
) -> None:
    repository, _, _ = _repository(tmp_path, 2)
    profile = repository.root / "config/cluster-profiles/inference.toml"
    profile.write_text(profile.read_text().replace("node_count = 2", "node_count = 1"))
    _git(repository.root, "add", ".")
    _git(repository.root, "commit", "-qm", "scale down")
    commit = _git(repository.root, "rev-parse", "HEAD")
    current = tuple(
        replace(
            observation,
            occupied=True,
            compute_occupancy="managed",
            memory_total_bytes=4_000,
            disk_total_bytes=8_000,
            current_workloads=(
                _managed_group(2),
            ),
        )
        for observation in _observations(2)
    )

    plan = _resolve(repository, commit, current)

    assert plan.placements == {"model": (_node_id(0),)}
    assert plan.routes["chat"]["entrypoint_node_id"] == _node_id(0)
    stop_ids = tuple(
        f"model:{_node_id(index)}:workload.stop" for index in range(2)
    )
    assert {
        node.operation_id
        for node in plan.operation_graph.nodes
        if node.kind == "workload.stop"
    } == set(stop_ids)
    assert plan.operation_graph.dependencies(stop_ids[1]) == (stop_ids[0],)
    assert plan.operation_graph.dependencies(
        f"model:{_node_id(0)}:release.install"
    ) == (_gate_id(0), _gate_id(1))
    assert plan.operation_graph.dependencies(_gate_id(0)) == stop_ids
    assert plan.operation_graph.dependencies(_gate_id(1)) == stop_ids
    assert plan.targets == (_node_id(0), _node_id(1))


def test_nonlexical_old_entrypoint_uses_persisted_old_role_for_stop_order(
    tmp_path: Path,
) -> None:
    repository, _, _ = _repository(tmp_path, 2)
    commit = _git(repository.root, "rev-parse", "HEAD")
    old_nodes = (_node_id(1), _node_id(0))
    current = tuple(
        replace(
            observation,
            occupied=True,
            compute_occupancy="managed",
            memory_total_bytes=4_000,
            disk_total_bytes=8_000,
            current_workloads=(_managed_group(2, nodes=old_nodes),),
        )
        for observation in _observations(2)
    )

    plan = _resolve(repository, commit, current)

    old_head_stop = f"model:{_node_id(1)}:workload.stop"
    old_worker_stop = f"model:{_node_id(0)}:workload.stop"
    new_worker_start = f"model:{_node_id(1)}:workload.start"
    new_head_start = f"model:{_node_id(0)}:workload.start"
    assert plan.placements == {"model": (_node_id(0), _node_id(1))}
    assert plan.operation_graph.dependencies(old_head_stop) == ()
    assert plan.operation_graph.dependencies(old_worker_stop) == (old_head_stop,)
    assert new_worker_start in plan.operation_graph.dependencies(new_head_start)


def test_exact_complete_group_is_retained_without_stop_prepare_or_start(
    tmp_path: Path,
) -> None:
    repository, commit, _ = _repository(tmp_path, 2)
    current = tuple(
        replace(
            observation,
            occupied=True,
            compute_occupancy="managed",
            memory_total_bytes=4_000,
            disk_total_bytes=8_000,
            current_workloads=(_managed_group(2),),
        )
        for observation in _observations(2)
    )

    plan = _resolve(repository, commit, current)

    assert plan.placements == {"model": (_node_id(0), _node_id(1))}
    assert {node.kind for node in plan.operation_graph.nodes} == {
        "workload.health",
        "workload.verify",
    }


def test_atomic_group_transition_is_independent_of_observation_order(
    tmp_path: Path,
) -> None:
    repository, commit, _ = _repository(tmp_path, 2)
    observations = tuple(
        replace(
            observation,
            occupied=True,
            compute_occupancy="managed",
            memory_total_bytes=4_000,
            disk_total_bytes=8_000,
            current_workloads=(
                _managed_group(2, release_digest="8" * 64),
            ),
        )
        for observation in _observations(2)
    )

    first = _resolve(repository, commit, observations)
    second = _resolve(repository, commit, tuple(reversed(observations)))

    assert first.workload_groups == second.workload_groups
    assert first.operation_graph.nodes == second.operation_graph.nodes
    assert first.operation_payloads == second.operation_payloads


def test_single_managed_group_total_capacity_is_not_double_counted(
    tmp_path: Path,
) -> None:
    repository, _, _ = _repository(tmp_path, 1)
    root = repository.root
    workload = root / "config/workloads/model.toml"
    (root / "config/workloads/replacement.toml").write_text(
        workload.read_text().replace('id = "model"', 'id = "replacement"')
    )
    release = root / "manifests/releases/model.json"
    (root / "manifests/releases/replacement.json").write_text(
        release.read_text()
        .replace('"workload_id":"model"', '"workload_id":"replacement"')
        .replace('"target_name":"model"', '"target_name":"replacement"')
    )
    (root / "config/cluster-profiles/inference.toml").write_text(
        f'''schema_version = 2
id = "inference"
accepted_evidence = "inventory/reports/inference.json"
workloads = ["model", "replacement"]

[[requirements]]
workload = "model"
definition_hash = "{DEFINITION_HASH}"
node_count = 1
required_labels = {{pool = "default"}}
min_memory_bytes = 100
min_disk_bytes = 200
exclusive = true
distributed_supported = true

[[requirements]]
workload = "replacement"
definition_hash = "{DEFINITION_HASH}"
node_count = 1
required_labels = {{pool = "default"}}
min_memory_bytes = 100
min_disk_bytes = 200
exclusive = true
distributed_supported = true

[endpoints]
chat = "model"
other = "replacement"

[quotas.chat]
requests_per_minute = 30
tokens_per_minute = 10000

[quotas.other]
requests_per_minute = 30
tokens_per_minute = 10000

[lifecycle]
start_order = "independent"
stop_order = "independent"
'''
    )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "request two exclusive workloads")
    commit = _git(root, "rev-parse", "HEAD")
    observation = replace(
        _observations(1)[0],
        occupied=True,
        compute_occupancy="managed",
        memory_total_bytes=4_000,
        disk_total_bytes=8_000,
        current_workloads=(_managed_group(1),),
    )

    with pytest.raises(ValueError, match="insufficient eligible nodes"):
        _resolve(repository, commit, (observation,))


def test_same_node_profile_digest_change_restarts_complete_group(
    tmp_path: Path,
) -> None:
    repository, _, _ = _repository(tmp_path, 1)
    release = repository.root / "manifests/releases/model.json"
    release.write_text(
        release.read_text().replace(
            '"profile_digest":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"',
            '"profile_digest":"4444444444444444444444444444444444444444444444444444444444444444"',
        )
    )
    _git(repository.root, "add", ".")
    _git(repository.root, "commit", "-qm", "change profile digest")
    commit = _git(repository.root, "rev-parse", "HEAD")
    current = replace(
        _observations(1)[0],
        occupied=True,
        compute_occupancy="managed",
        memory_total_bytes=4_000,
        disk_total_bytes=8_000,
        current_workloads=(_managed_group(1),),
    )

    plan = _resolve(repository, commit, (current,))

    stop_id = f"model:{_node_id(0)}:workload.stop"
    install_id = f"model:{_node_id(0)}:release.install"
    assert plan.operation_graph.dependencies(install_id) == (_gate_id(0),)
    assert plan.operation_graph.dependencies(_gate_id(0)) == (stop_id,)
    assert plan.workload_groups["model"]["profile_digest"] == "4" * 64


def test_preferred_node_move_tears_down_old_node_before_starting_new_node(
    tmp_path: Path,
) -> None:
    repository, _, _ = _repository(tmp_path, 2)
    profile = repository.root / "config/cluster-profiles/inference.toml"
    profile.write_text(
        profile.read_text()
        .replace("node_count = 2", "node_count = 1")
        .replace(
            "distributed_supported = true",
            f'distributed_supported = true\npreferred_node_ids = ["{_node_id(1)}"]',
        )
    )
    _git(repository.root, "add", ".")
    _git(repository.root, "commit", "-qm", "move workload")
    commit = _git(repository.root, "rev-parse", "HEAD")
    old = replace(
        _observations(2)[0],
        occupied=True,
        compute_occupancy="managed",
        memory_total_bytes=4_000,
        disk_total_bytes=8_000,
        current_workloads=(
            _managed_group(1),
        ),
    )

    plan = _resolve(repository, commit, (old, _observations(2)[1]))

    stop_id = f"model:{_node_id(0)}:workload.stop"
    install_id = f"model:{_node_id(1)}:release.install"
    assert plan.placements == {"model": (_node_id(1),)}
    assert plan.operation_graph.dependencies(install_id) == (
        _gate_id(0),
        _gate_id(1),
    )
    assert plan.operation_graph.dependencies(_gate_id(0)) == (stop_id,)
    assert plan.operation_graph.dependencies(_gate_id(1)) == (stop_id,)
    assert plan.routes["chat"]["entrypoint_node_id"] == _node_id(1)


def test_profile_workload_removal_stops_durable_workload_without_orphaning_it(
    tmp_path: Path,
) -> None:
    repository, _, _ = _repository(tmp_path, 1)
    profile = repository.root / "config/cluster-profiles/inference.toml"
    profile.write_text(
        '''schema_version = 2
id = "inference"
accepted_evidence = "inventory/reports/inference.json"
workloads = []
requirements = []

[endpoints]

[quotas]

[lifecycle]
start_order = "independent"
stop_order = "independent"
'''
    )
    _git(repository.root, "add", ".")
    _git(repository.root, "commit", "-qm", "remove workload")
    commit = _git(repository.root, "rev-parse", "HEAD")
    current = replace(
        _observations(1)[0],
        occupied=True,
        compute_occupancy="managed",
        memory_total_bytes=4_000,
        disk_total_bytes=8_000,
        current_workloads=(
            _managed_group(1),
        ),
    )

    plan = _resolve(repository, commit, (current,))

    assert plan.placements == {}
    assert plan.routes == {}
    assert plan.releases == {}
    assert plan.targets == (_node_id(0),)
    assert tuple(node.kind for node in plan.operation_graph.nodes) == (
        "workload.stop",
        "node.probe",
    )
    assert plan.operation_graph.dependencies(
        f"gate:{_node_id(0)}:node.probe"
    ) == (f"model:{_node_id(0)}:workload.stop",)
    assert plan.operation_payloads[f"model:{_node_id(0)}:workload.stop"][
        "release_digest"
    ] == "a" * 64


def test_rejects_unreviewed_workload_adapter(tmp_path: Path) -> None:
    repository, commit, _ = _repository(
        tmp_path, 1, workload_adapter="repository-agent"
    )

    with pytest.raises(ValueError, match="reviewed adapter"):
        _resolve(repository, commit, _observations(1))


def test_rejects_distributed_profile_when_workload_disallows_distribution(
    tmp_path: Path,
) -> None:
    repository, commit, _ = _repository(
        tmp_path, 2, workload_distributed_supported=False
    )

    with pytest.raises(ValueError, match="distributed support"):
        _resolve(repository, commit, _observations(2))


def test_reordered_repository_tables_and_observations_keep_placement_and_graph_stable(
    tmp_path: Path,
) -> None:
    first_repository, first_commit, _ = _repository(tmp_path / "first", 16)
    second_repository, second_commit, _ = _repository(
        tmp_path / "second", 16, reversed_nodes=True
    )

    first = _resolve(first_repository, first_commit, _observations(16))
    second = _resolve(
        second_repository, second_commit, tuple(reversed(_observations(16)))
    )

    assert first.placements == second.placements
    assert first.targets == second.targets
    assert first.operation_graph.nodes == second.operation_graph.nodes
    assert first.operation_payloads == second.operation_payloads


@pytest.mark.parametrize(
    ("requirement_hash", "workload_hash", "release_hash", "message"),
    [
        ("e" * 64, DEFINITION_HASH, DEFINITION_HASH, "profile definition hash"),
        (DEFINITION_HASH, "e" * 64, DEFINITION_HASH, "profile definition hash"),
        (DEFINITION_HASH, DEFINITION_HASH, "e" * 64, "release definition hash"),
    ],
)
def test_rejects_mismatched_repository_hash_cross_references(
    tmp_path: Path,
    requirement_hash: str,
    workload_hash: str,
    release_hash: str,
    message: str,
) -> None:
    repository, commit, _ = _repository(
        tmp_path,
        1,
        requirement_hash=requirement_hash,
        workload_hash=workload_hash,
        release_hash=release_hash,
    )

    with pytest.raises(ValueError, match=message):
        _resolve(repository, commit, _observations(1))


def test_rejects_missing_profile_workload_reference(tmp_path: Path) -> None:
    repository, commit, _ = _repository(tmp_path, 1)
    root = repository.root
    profile = root / "config/cluster-profiles/inference.toml"
    profile.write_text(profile.read_text().replace('workloads = ["model"]', 'workloads = ["missing"]'))
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "missing reference")
    commit = _git(root, "rev-parse", "HEAD")

    with pytest.raises(ValueError, match="profile workload reference"):
        _resolve(repository, commit, _observations(1))


def test_rejects_stale_observation_and_insufficient_capacity(tmp_path: Path) -> None:
    repository, commit, _ = _repository(tmp_path, 1)
    current = _observations(1)[0]

    with pytest.raises(ValueError, match="stale"):
        _resolve(
            repository,
            commit,
            (replace(current, observed_at=NOW - timedelta(minutes=6)),),
        )
    with pytest.raises(ValueError, match="insufficient eligible nodes"):
        _resolve(repository, commit, (replace(current, memory_available_bytes=99),))


@pytest.mark.parametrize(
    ("observation", "message"),
    [
        (replace(_observations(1)[0], agent_state="offline"), "connected"),
        (replace(_observations(1)[0], protocol_version=2), "protocol"),
        (
            replace(
                _observations(1)[0],
                capabilities=tuple(
                    item
                    for item in AGENT_CAPABILITIES
                    if item != "workload.verify"
                ),
            ),
            "capabilities",
        ),
    ],
)
def test_rejects_disconnected_or_incompatible_agents(
    tmp_path: Path, observation: DesiredStateObservation, message: str
) -> None:
    repository, commit, _ = _repository(tmp_path, 1)

    with pytest.raises(ValueError, match=message):
        _resolve(repository, commit, (observation,))


def test_rejects_release_operations_outside_closed_agent_registry(
    tmp_path: Path,
) -> None:
    repository, commit, _ = _repository(
        tmp_path, 1, operations=REQUIRED_CAPABILITIES + ("agent.update",)
    )

    with pytest.raises(ValueError, match="release operations"):
        _resolve(repository, commit, _observations(1))


def test_agent_must_advertise_zero_compute_probe_capability(
    tmp_path: Path,
) -> None:
    repository, commit, _ = _repository(tmp_path, 1)
    observation = replace(
        _observations(1)[0],
        capabilities=tuple(
            item for item in AGENT_CAPABILITIES if item != "node.probe"
        ),
    )

    with pytest.raises(ValueError, match="capabilities"):
        _resolve(repository, commit, (observation,))


def test_resolution_is_pinned_to_commit_not_mutable_checkout(tmp_path: Path) -> None:
    repository, commit, documents = _repository(tmp_path, 1)
    (repository.root / "inventory/fleet.toml").write_text("schema_version = 999\n")

    plan = _resolve(repository, commit, _observations(1))

    assert plan.input_digests["inventory/fleet.toml"] == hashlib.sha256(
        documents["inventory/fleet.toml"]
    ).hexdigest()


def test_durable_projection_joins_latest_health_with_agent_compatibility(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'observations.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    node_id = _node_id(0)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=node_id,
                state="active",
                protocol_version=1,
                capabilities=list(AGENT_CAPABILITIES),
                last_seen_at=NOW - timedelta(seconds=1),
            )
        )
        session.add_all(
            [
                Observation(
                    node_id=node_id,
                    kind="health",
                    payload={"status": "critical"},
                    observed_at=NOW - timedelta(minutes=1),
                ),
                Observation(
                    node_id=node_id,
                    kind="health",
                    payload={
                        "status": "healthy",
                        "memory_available_bytes": 1_000,
                        "disk_available_bytes": 2_000,
                    },
                    observed_at=NOW - timedelta(seconds=2),
                ),
            ]
        )

    assert durable_desired_state_observations(sessions) == (
        DesiredStateObservation(
            node_id=node_id,
            observed_at=NOW - timedelta(seconds=2),
            healthy=True,
            memory_available_bytes=1_000,
            disk_available_bytes=2_000,
            occupied=False,
            agent_state="active",
            protocol_version=1,
            capabilities=AGENT_CAPABILITIES,
        ),
    )


@pytest.mark.parametrize(
    ("count", "occupancy", "eligible"),
    ((0, "clean", True), (2, "unmanaged", False), (None, "unknown", False)),
)
def test_production_probe_compute_evidence_controls_placement(
    tmp_path: Path, count: int | None, occupancy: str, eligible: bool
) -> None:
    repository, commit, _ = _repository(tmp_path, 1)
    engine = create_engine(f"sqlite:///{tmp_path / 'probe-occupancy.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    node_id = _node_id(0)
    probe_result = {
        "status": "ok",
        "evidence": {
            "vonk_forge": {
                "schema_version": 1,
                "memory": {"available_bytes": 1_000, "total_bytes": 4_000},
                "storage": {"available_bytes": 2_000, "total_bytes": 8_000},
                "accelerator": {
                    "available": True,
                    "active_nvidia_compute_processes": count,
                },
            },
            "nvidia": {"tools": {}},
        },
    }
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=node_id,
                state="active",
                protocol_version=1,
                capabilities=list(AGENT_CAPABILITIES),
                last_seen_at=NOW - timedelta(seconds=1),
            )
        )
        session.add(
            Observation(
                node_id=node_id,
                kind="health",
                payload=AgentJobService._probe_health(probe_result),
                observed_at=NOW - timedelta(seconds=2),
            )
        )

    projected = durable_desired_state_observations(sessions)

    assert projected[0].compute_occupancy == occupancy
    resolver = DesiredStateResolver(repository, clock=lambda: NOW)
    if eligible:
        assert resolver.resolve(commit, "inference", projected).placements == {
            "model": (node_id,)
        }
    else:
        with pytest.raises(ValueError, match="insufficient eligible nodes"):
            resolver.resolve(commit, "inference", projected)


def test_durable_projection_derives_occupancy_from_completed_start_evidence(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'active.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    node_id = _node_id(0)
    graph, graph_digest, resolved, plan_digest, payload = _persisted_start_plan(
        node_id
    )
    payload_digest = hashlib.sha256(canonical_message(payload)).hexdigest()
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=node_id,
                state="active",
                protocol_version=1,
                capabilities=list(AGENT_CAPABILITIES),
                last_seen_at=NOW - timedelta(seconds=1),
            )
        )
        session.add(
            Observation(
                node_id=node_id,
                kind="health",
                payload={
                    "status": "healthy",
                    "active_nvidia_compute_processes": 1,
                    "compute_occupancy": "active",
                    "memory_available_bytes": 1_000,
                    "memory_total_bytes": 4_000,
                    "disk_available_bytes": 2_000,
                    "disk_total_bytes": 8_000,
                },
                observed_at=NOW - timedelta(seconds=2),
            )
        )
        session.add(
            Reconciliation(
                id="reconciliation",
                base_commit="a" * 40,
                status="succeeded",
                summary={},
                graph=graph,
                graph_digest=graph_digest,
                plan_digest=plan_digest,
                resolved_plan=resolved,
                current_phase="completed",
                completion_generation=1,
                created_at=NOW - timedelta(seconds=4),
            )
        )
        session.add(
            Job(
                id="job",
                request_id="request",
                kind="reconcile",
                state="succeeded",
                actor="administrator",
                base_commit="a" * 40,
                targets=[node_id],
                payload_digest="e" * 64,
                payload={"reconciliation_id": "forged-json-hint"},
                reconciliation_id="reconciliation",
                current_attempt=1,
                created_at=NOW - timedelta(seconds=4),
                updated_at=NOW - timedelta(seconds=3),
            )
        )
        session.add(
            AgentOperation(
                id="operation",
                parent_job_id="job",
                node_id=node_id,
                kind="workload.start",
                payload_digest=payload_digest,
                payload=payload,
                base_commit="a" * 40,
                state="succeeded",
                current_attempt=1,
                created_at=NOW - timedelta(seconds=4),
                updated_at=NOW - timedelta(seconds=3),
            )
        )
        session.add(
            AgentOperationAttempt(
                id="attempt",
                operation_id="operation",
                attempt=1,
                fence="fence",
                lease_deadline=NOW,
                agent_certificate_serial="serial",
                state="succeeded",
                result={
                    "status": "ok",
                    "evidence": {
                        "status": "started",
                        "action": "start",
                        "workload_id": "model",
                        "release_digest": "8" * 64,
                        "evidence_digest": "f" * 64,
                    },
                },
            )
        )

    projected = durable_desired_state_observations(sessions)
    assert projected[0].occupied is True
    assert projected[0].compute_occupancy == "managed"
    assert projected[0].current_workloads == (
        _managed_group(
            1,
            release_digest="8" * 64,
            preparation_digest="c" * 64,
        ),
    )
    repository, commit, _ = _repository(tmp_path, 1)
    assert _resolve(repository, commit, projected).placements == {
        "model": (node_id,)
    }


@pytest.mark.parametrize(
    "corruption",
    (
        "entrypoint",
        "lifecycle",
        "definition",
        "profile",
        "resolved-graph",
        "stored-graph",
        "base-commit",
        "graph-digest",
        "plan-digest",
        "payload-digest",
    ),
)
def test_durable_projection_authenticates_complete_plan_before_group_replay(
    tmp_path: Path, corruption: str
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / f'{corruption}.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    node_id = _node_id(0)
    graph, graph_digest, resolved, plan_digest, _ = _persisted_start_plan(node_id)
    graph = deepcopy(graph)
    resolved = deepcopy(resolved)
    base_commit = "a" * 40
    if corruption == "entrypoint":
        resolved["workload_groups"]["model"]["entrypoint_node_id"] = _node_id(1)
    elif corruption == "lifecycle":
        resolved["workload_groups"]["model"]["lifecycle"]["start_order"] = "workers-first"
    elif corruption == "definition":
        resolved["workload_groups"]["model"]["definition_hash"] = "e" * 64
    elif corruption == "profile":
        resolved["workload_groups"]["model"]["profile_digest"] = "e" * 64
    elif corruption == "resolved-graph":
        resolved["operation_graph"]["targets"] = []
    elif corruption == "stored-graph":
        graph["targets"] = []
    elif corruption == "base-commit":
        base_commit = "b" * 40
    elif corruption == "graph-digest":
        graph_digest = "0" * 64
    elif corruption == "plan-digest":
        plan_digest = "0" * 64
    else:
        resolved["operation_payloads"]["model:node:workload.start"][
            "preparation_digest"
        ] = "e" * 64
        plan_digest = hashlib.sha256(canonical_message(resolved)).hexdigest()
    with sessions.begin() as session:
        session.add(
            Reconciliation(
                id="reconciliation",
                base_commit=base_commit,
                status="succeeded",
                summary={},
                graph=graph,
                graph_digest=graph_digest,
                plan_digest=plan_digest,
                resolved_plan=resolved,
                current_phase="completed",
                completion_generation=1,
                created_at=NOW,
            )
        )

    with pytest.raises((TypeError, ValueError), match="persisted resolved plan"):
        durable_desired_state_observations(sessions)


def test_durable_projection_fails_closed_on_completed_graph_without_operation_evidence(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'drift.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    node_id = _node_id(0)
    graph, graph_digest, resolved, plan_digest, _ = _persisted_start_plan(node_id)
    with sessions.begin() as session:
        session.add(
            Reconciliation(
                id="reconciliation",
                base_commit="a" * 40,
                status="succeeded",
                summary={},
                graph=graph,
                graph_digest=graph_digest,
                plan_digest=plan_digest,
                resolved_plan=resolved,
                current_phase="completed",
                completion_generation=1,
                created_at=NOW,
            )
        )

    with pytest.raises(ValueError, match="operation evidence"):
        durable_desired_state_observations(sessions)


def test_durable_projection_rejects_legacy_workload_evidence_without_generation(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    node_id = _node_id(0)
    with sessions.begin() as session:
        session.add(
            Reconciliation(
                id="legacy-reconciliation",
                base_commit="a" * 40,
                status="succeeded",
                summary={},
                graph={
                    "schema_version": 1,
                    "base_commit": "a" * 40,
                    "targets": [node_id],
                    "nodes": [
                        {
                            "operation_id": "legacy-start",
                            "node_id": node_id,
                            "workload_id": "model",
                            "kind": "workload.start",
                            "dependencies": [],
                            "compensation_kind": "workload.stop",
                            "payload_digest": "d" * 64,
                        }
                    ],
                },
                graph_digest="d" * 64,
                current_phase="completed",
                completion_generation=None,
                created_at=NOW,
            )
        )

    with pytest.raises(ValueError, match="causal completion generation"):
        durable_desired_state_observations(sessions)


def test_durable_projection_fails_closed_on_unaccepted_successful_mutation(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'unaccepted.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    node_id = _node_id(0)
    with sessions.begin() as session:
        session.add(
            Reconciliation(
                id="failed-reconciliation",
                base_commit="a" * 40,
                status="failed",
                summary={},
                graph={
                    "schema_version": 1,
                    "base_commit": "a" * 40,
                    "targets": [node_id],
                    "nodes": [],
                },
                graph_digest="d" * 64,
                current_phase="failed",
                created_at=NOW,
            )
        )
        session.add(
            Job(
                id="failed-job",
                request_id="failed-request",
                kind="reconcile",
                state="failed",
                actor="administrator",
                base_commit="a" * 40,
                targets=[node_id],
                payload_digest="e" * 64,
                payload={"reconciliation_id": "forged-json-hint"},
                reconciliation_id="failed-reconciliation",
                current_attempt=1,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            AgentOperation(
                id="unaccepted-start",
                parent_job_id="failed-job",
                node_id=node_id,
                kind="workload.start",
                payload_digest="f" * 64,
                payload={},
                base_commit="a" * 40,
                state="succeeded",
                current_attempt=1,
                created_at=NOW,
                updated_at=NOW,
            )
        )

    with pytest.raises(ValueError, match="unaccepted workload mutation"):
        durable_desired_state_observations(sessions)
