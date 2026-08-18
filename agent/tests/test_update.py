from __future__ import annotations

import hashlib
import json
import struct
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from vonk_agent.deadlines import MonotonicDeadline
from vonk_agent.update import (
    ActivationAuthorization,
    AgentArtifact,
    AgentReleaseIdentity,
    AgentRollbackCommand,
    AgentUpdateCommand,
    AgentUpdateError,
    AgentUpdater,
    AuthorizationSignature,
    LocalSupervisor,
    ORASAgentTransport,
    PendingActivation,
    PlatformAgentTrust,
    PlatformAuthorizationEvidence,
    PlatformTUFRouteFetcher,
    RollbackAuthorization,
    SupervisorActivationRequest,
    SupervisorRollbackRequest,
    SupervisorSlotState,
)

OPERATION_ID = "22222222-2222-4222-8222-222222222222"
FENCE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _platform_target(platform_version: str, target_sha256: str) -> str:
    return f"platform/releases/{platform_version}/{target_sha256}.json"


def _signature() -> AuthorizationSignature:
    return AuthorizationSignature(key_id="7" * 64, value="8" * 128)


def _authorization(
    artifact: AgentArtifact,
    release: AgentReleaseIdentity,
    *,
    target_sha256: str = "9" * 64,
    target_name: str | None = None,
    operation_id: str = OPERATION_ID,
    fence: str = FENCE,
    expires_at: int | None = None,
) -> ActivationAuthorization:
    fixed_expiry = expires_at or int(time.time()) + 300
    return ActivationAuthorization(
        architecture=artifact.architecture,
        oci_manifest_digest=artifact.oci_manifest_digest,
        payload_name=artifact.payload_name,
        payload_sha256=artifact.payload_sha256,
        payload_size=artifact.payload_size,
        platform_version=release.platform_version,
        build_digest=release.build_digest,
        platform_target_name=target_name
        or _platform_target(release.platform_version, target_sha256),
        platform_target_sha256=target_sha256,
        tuf_targets_version=7,
        previous_slot="A",
        previous_sha256="1" * 64,
        target_slot="B",
        node_id="spk_0123456789abcdef0123456789abcdef",
        attempt=1,
        claim_deadline=fixed_expiry,
        previous_generation=1,
        operation_id=operation_id,
        fence=fence,
        expires_at=fixed_expiry,
    )


def test_activation_receipt_requires_exact_versioned_platform_target_identity() -> None:
    artifact, release = _inputs(_elf())
    target_sha256 = "9" * 64

    authorization = _authorization(artifact, release, target_sha256=target_sha256)

    assert authorization.platform_target_name == _platform_target(
        release.platform_version, target_sha256
    )
    for invalid_name in (
        "platform-release.json",
        _platform_target("1.2.1", target_sha256),
        _platform_target(release.platform_version, "0" * 64),
        f"platform/releases/{release.platform_version}/../../escape.json",
        f"platform/releases/{release.platform_version}/{'A' * 64}.json",
    ):
        with pytest.raises(AgentUpdateError, match="activation authorization"):
            _authorization(
                artifact,
                release,
                target_sha256=target_sha256,
                target_name=invalid_name,
            )


def _elf(machine: int = 183, size: int = 4096) -> bytes:
    header = bytearray(max(size, 64))
    header[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HH", header, 16, 2, machine)
    return bytes(header)


def _platform_manifest(payload_sha256: str, payload_size: int) -> bytes:
    def artifact(name: str, digest: str) -> dict[str, object]:
        return {
            "name": name,
            "provenance_sha256": "d" * 64,
            "reference": f"registry.example/vonk-forge/releases@sha256:{digest}",
            "sbom_sha256": "e" * 64,
            "sha256": digest,
            "size": 1024,
        }

    document = {
        "agents": [
            {
                "architecture": "linux-arm64",
                "artifact": artifact("agent-linux-arm64", "a" * 64),
                "payload": {
                    "name": "vonk-agent",
                    "sha256": payload_sha256,
                    "size": payload_size,
                },
                "protocol": {"maximum": 2, "minimum": 1},
            }
        ],
        "build_digest": "sha256:" + "c" * 64,
        "deployment_bundle": {
            "layer_digest": "sha256:" + "a" * 64,
            "layer_media_type": "application/vnd.vonk-forge.control-deployment.v1.tar",
            "layer_size": 4096,
            "manifest_digest": "sha256:" + "b" * 64,
            "manifest_media_type": "application/vnd.oci.image.manifest.v1+json",
            "manifest_size": 1024,
            "reference": (
                "registry.example/vonk-forge/control-deployment@sha256:" + "b" * 64
            ),
        },
        "host_updater_abi": {"maximum": 2, "minimum": 1},
        "control": {
            "assets": [artifact("web", "f" * 64)],
            "config_version": 1,
            "images": {
                "api": artifact("api", "f" * 64),
                "worker": artifact("worker", "f" * 64),
            },
            "protocol": {"maximum": 1, "minimum": 1},
        },
        "database": {
            "contract_revision": None,
            "expand_revision": "0010_update_rollouts",
            "predecessor_compatible": True,
        },
        "platform_version": "1.2.3",
        "rollback": {
            "predecessors": [
                {
                    "build_digest": "sha256:" + "b" * 64,
                    "deployment_bundle_digest": "sha256:" + "a" * 64,
                    "release_digest": "sha256:" + "d" * 64,
                    "target_name": _platform_target("1.2.2", "e" * 64),
                    "target_sha256": "e" * 64,
                }
            ]
        },
        "schema_version": 2,
        "supervisors": [
            {
                "architecture": "linux-arm64",
                "artifact": artifact("supervisor-linux-arm64", "f" * 64),
                "payload": {"name": "supervisor", "sha256": "f" * 64, "size": 4096},
            }
        ],
        "tooling": [
            {
                "architecture": "linux-arm64",
                "artifact": artifact("tooling-linux-arm64", "f" * 64),
                "payload": {"name": "tooling", "sha256": "f" * 64, "size": 4096},
            }
        ],
    }
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()


def test_agent_update_command_binds_only_digest_addressed_signed_payload() -> None:
    payload = {
        "artifact": {
            "architecture": "linux-arm64",
            "oci_manifest_digest": "sha256:" + "a" * 64,
            "payload_name": "vonk-agent",
            "payload_sha256": "b" * 64,
            "payload_size": 4096,
        },
        "release": {
            "build_digest": "sha256:" + "c" * 64,
            "platform_version": "1.2.3",
            "protocol_maximum": 2,
            "protocol_minimum": 1,
        },
        "receipt": {
            "architecture": "linux-arm64",
            "attempt": 1,
            "build_digest": "sha256:" + "c" * 64,
            "claim_deadline": int(time.time()) + 300,
            "expires_at": int(time.time()) + 300,
            "fence": FENCE,
            "node_id": "spk_0123456789abcdef0123456789abcdef",
            "oci_manifest_digest": "sha256:" + "a" * 64,
            "operation_id": OPERATION_ID,
            "payload_name": "vonk-agent",
            "platform_target_name": _platform_target("1.2.3", "d" * 64),
            "platform_target_sha256": "d" * 64,
            "platform_version": "1.2.3",
            "previous_sha256": "e" * 64,
            "previous_generation": 1,
            "previous_slot": "A",
            "sha256": "b" * 64,
            "size": 4096,
            "target_slot": "B",
            "tuf_targets_version": 7,
        },
        "signature": {
            "algorithm": "ed25519",
            "key_id": "7" * 64,
            "value": "8" * 128,
        },
    }
    payload["receipt"]["expires_at"] = payload["receipt"]["claim_deadline"]

    command = AgentUpdateCommand.parse(payload)

    assert command.artifact.payload_name == "vonk-agent"
    assert command.artifact.payload_sha256 == "b" * 64
    assert command.artifact.payload_size == 4096
    assert command.authorization.operation_id == OPERATION_ID
    assert "reference" not in json.dumps(payload)
    assert "/" not in command.artifact.payload_name
    for invalid_name in ("bin/vonk-agent", "../vonk-agent", "/tmp/vonk-agent"):
        invalid = json.loads(json.dumps(payload))
        invalid["artifact"]["payload_name"] = invalid_name
        with pytest.raises(AgentUpdateError, match="payload"):
            AgentUpdateCommand.parse(invalid)


def test_agent_payload_limit_matches_stable_supervisor() -> None:
    common = {
        "architecture": "linux-arm64",
        "oci_manifest_digest": "sha256:" + "a" * 64,
        "payload_name": "vonk-agent",
        "payload_sha256": "b" * 64,
    }

    assert AgentArtifact(**common, payload_size=268435456).payload_size == 268435456
    with pytest.raises(AgentUpdateError, match="payload size"):
        AgentArtifact(**common, payload_size=268435457)


def test_platform_tuf_authorizes_exact_manifest_and_installed_payload() -> None:
    raw = _platform_manifest("b" * 64, 4096)
    target_sha256 = hashlib.sha256(raw).hexdigest()
    target_name = _platform_target("1.2.3", target_sha256)

    class Trust:
        def __init__(self) -> None:
            self.refreshes = 0
            self.names: list[str] = []

        def refresh_and_trusted_target(self, name: str):
            self.refreshes += 1
            self.names.append(name)
            return (
                SimpleNamespace(
                    data=raw,
                    length=len(raw),
                    name=name,
                    sha256=hashlib.sha256(raw).hexdigest(),
                ),
                7,
            )

    class DeadlineSetter:
        def __init__(self) -> None:
            self.deadlines: list[MonotonicDeadline] = []

        def set_deadline(self, deadline: MonotonicDeadline) -> None:
            self.deadlines.append(deadline)

    trust = Trust()
    setter = DeadlineSetter()
    artifact = AgentArtifact(
        architecture="linux-arm64",
        oci_manifest_digest="sha256:" + "a" * 64,
        payload_name="vonk-agent",
        payload_sha256="b" * 64,
        payload_size=4096,
    )
    release = AgentReleaseIdentity(
        platform_version="1.2.3",
        build_digest="sha256:" + "c" * 64,
        protocol_minimum=1,
        protocol_maximum=2,
    )
    deadline = MonotonicDeadline.bind(datetime.now(UTC) + timedelta(seconds=30))

    evidence = PlatformAgentTrust(trust, setter).authorize(
        artifact, release, target_name, deadline
    )

    assert trust.refreshes == 1
    assert trust.names == [target_name]
    assert setter.deadlines == [deadline]
    assert evidence.target_sha256 == target_sha256
    assert evidence.targets_version == 7
    with pytest.raises(AgentUpdateError, match="signed platform release"):
        PlatformAgentTrust(trust, setter).authorize(
            replace(artifact, payload_sha256="9" * 64),
            release,
            target_name,
            deadline,
        )


def test_platform_tuf_route_adapter_maps_only_strict_versioned_agent_targets() -> None:
    class Fetcher:
        def __init__(self) -> None:
            self.urls: list[str] = []
            self.deadlines: list[MonotonicDeadline] = []

        def fetch(self, url: str):
            self.urls.append(url)
            return iter((b"trusted",))

        def set_deadline(self, deadline: MonotonicDeadline) -> None:
            self.deadlines.append(deadline)

    delegate = Fetcher()
    adapter = PlatformTUFRouteFetcher(
        delegate, control_origin="https://control.example:8443"
    )
    deadline = MonotonicDeadline.bind(datetime.now(UTC) + timedelta(seconds=30))
    adapter.set_deadline(deadline)
    target_name = _platform_target("1.2.3", "a" * 64)

    assert b"".join(
        adapter.fetch(
            "https://control.example:8443/platform/metadata/timestamp.json"
        )
    ) == b"trusted"
    assert b"".join(
        adapter.fetch(
            "https://control.example:8443/platform/targets/" + target_name
        )
    ) == b"trusted"
    assert delegate.urls == [
        "https://control.example:8443/agent/v1/tuf/metadata/timestamp.json",
        "https://control.example:8443/agent/v1/tuf/targets/" + target_name,
    ]
    assert delegate.deadlines == [deadline]
    for invalid_url in (
        "https://control.example:8443/platform/targets/platform-release.json",
        "https://control.example:8443/platform/targets/../root.json",
        "https://control.example:8443/platform/targets/platform/releases/1.2.3/%2e%2e.json",
    ):
        with pytest.raises(AgentUpdateError, match="route"):
            list(adapter.fetch(invalid_url))


def test_oras_pull_uses_exact_digest_and_selects_only_signed_payload(
    tmp_path: Path,
) -> None:
    content = _elf()
    artifact = AgentArtifact(
        architecture="linux-arm64",
        oci_manifest_digest="sha256:" + "a" * 64,
        payload_name="vonk-agent",
        payload_sha256=hashlib.sha256(content).hexdigest(),
        payload_size=len(content),
    )

    class Client:
        def __init__(self) -> None:
            self.calls = []

        def pull(self, descriptor, destination, deadline) -> None:
            self.calls.append((descriptor, destination, deadline))
            (destination / "vonk-agent").write_bytes(content)

    client = Client()
    deadline = MonotonicDeadline.bind(datetime.now(UTC) + timedelta(seconds=30))
    destination = tmp_path / "candidate"

    ORASAgentTransport(
        client,
        registry_origin="https://registry.example",
        repository="vonk-forge/releases",
        architecture="linux-arm64",
    ).fetch(artifact, destination, deadline)

    descriptor, _root, passed_deadline = client.calls[0]
    assert destination.read_bytes() == content
    assert descriptor.target_name == "vonk-agent"
    assert descriptor.target_digest == artifact.payload_sha256
    assert descriptor.target_length == artifact.payload_size
    assert descriptor.oci_manifest_digest == artifact.oci_manifest_digest
    assert passed_deadline is deadline


class FakeTrust:
    def __init__(self) -> None:
        self.authorized = True
        self.calls: list[
            tuple[AgentArtifact, AgentReleaseIdentity, str, MonotonicDeadline]
        ] = []

    def authorize(
        self,
        artifact: AgentArtifact,
        release: AgentReleaseIdentity,
        platform_target_name: str,
        deadline: MonotonicDeadline,
    ) -> PlatformAuthorizationEvidence:
        self.calls.append((artifact, release, platform_target_name, deadline))
        if not self.authorized:
            raise AgentUpdateError("platform release is not authorized")
        return PlatformAuthorizationEvidence("9" * 64, 7)


class FakeTransport:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.fail = False
        self.destinations: list[tuple[Path, MonotonicDeadline]] = []

    def fetch(
        self,
        artifact: AgentArtifact,
        destination: Path,
        deadline: MonotonicDeadline,
    ) -> None:
        self.destinations.append((destination, deadline))
        destination.write_bytes(self.content[: len(self.content) // 2])
        if self.fail:
            raise AgentUpdateError("download interrupted")
        with destination.open("ab") as stream:
            stream.write(self.content[len(self.content) // 2 :])


class FakeSupervisor:
    def __init__(self) -> None:
        self.state = SupervisorSlotState(
            active_slot="A",
            previous_slot=None,
            status="stable",
            slot_sha256={"A": "1" * 64, "B": None},
        )
        self.requests: list[SupervisorActivationRequest] = []

    def inspect(self) -> SupervisorSlotState:
        return self.state

    def request_activation(self, request: SupervisorActivationRequest) -> None:
        self.requests.append(request)

    def request_rollback(self, request) -> PendingActivation:
        return PendingActivation(
            previous_slot="B",
            target_slot="A",
            artifact_sha256="1" * 64,
            platform_version="1.0.0",
            build_digest="sha256:" + "2" * 64,
            status="pending-rollback",
        )


def _inputs(content: bytes) -> tuple[AgentArtifact, AgentReleaseIdentity]:
    digest = hashlib.sha256(content).hexdigest()
    return (
        AgentArtifact(
            architecture="linux-arm64",
            oci_manifest_digest="sha256:" + "f" * 64,
            payload_name="vonk-agent",
            payload_sha256=digest,
            payload_size=len(content),
        ),
        AgentReleaseIdentity(
            platform_version="1.2.0",
            build_digest=f"sha256:{digest}",
            protocol_minimum=1,
            protocol_maximum=2,
        ),
    )


def _updater(tmp_path: Path, content: bytes):
    trust = FakeTrust()
    transport = FakeTransport(content)
    supervisor = FakeSupervisor()
    updater = AgentUpdater(
        architecture="linux-arm64",
        protocol_version=1,
        staging_root=tmp_path / "staging",
        trust=trust,
        transport=transport,
        supervisor=supervisor,
        available_bytes=lambda: 1024 * 1024,
    )
    return updater, trust, transport, supervisor


def test_agent_update_plans_and_stages_only_the_inactive_slot(tmp_path: Path) -> None:
    content = _elf()
    updater, trust, _transport, supervisor = _updater(tmp_path, content)
    artifact, release = _inputs(content)
    authorization = _authorization(artifact, release)
    signature = _signature()

    plan = updater.plan(artifact, release, authorization, signature)
    pending = updater.apply(plan)

    assert plan.previous_slot == "A"
    assert plan.target_slot == "B"
    assert pending.target_slot == "B"
    assert pending.previous_slot == "A"
    assert pending.platform_version == "1.2.0"
    assert [(item[0], item[1], item[2]) for item in trust.calls] == [
        (artifact, release, authorization.platform_target_name),
        (artifact, release, authorization.platform_target_name),
    ]
    assert all(type(item[3]) is MonotonicDeadline for item in trust.calls)
    installed = tmp_path / "staging" / f"{artifact.payload_sha256}.agent"
    assert installed.read_bytes() == content
    assert installed.stat().st_mode & 0o777 == 0o500
    assert installed.stat().st_nlink == 1
    assert not list(installed.parent.glob("*.partial"))
    assert supervisor.requests == [
        SupervisorActivationRequest(authorization, signature)
    ]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("architecture", "architecture"),
        ("protocol", "protocol"),
        ("digest", "payload digest"),
        ("space", "disk space"),
        ("pending", "stable"),
    ],
)
def test_agent_update_rejects_incompatible_or_unsafe_plan(
    tmp_path: Path, change: str, message: str
) -> None:
    content = _elf()
    updater, _trust, _transport, supervisor = _updater(tmp_path, content)
    artifact, release = _inputs(content)
    if change == "architecture":
        artifact = AgentArtifact(**{**artifact.__dict__, "architecture": "linux-x86_64"})
    elif change == "protocol":
        release = AgentReleaseIdentity(**{**release.__dict__, "protocol_minimum": 2})
    elif change == "digest":
        with pytest.raises(AgentUpdateError, match=message):
            AgentArtifact(**{**artifact.__dict__, "payload_sha256": "invalid"})
        return
    elif change == "space":
        updater._available_bytes = lambda: 1
    else:
        supervisor.state = SupervisorSlotState(
            active_slot="A",
            previous_slot="B",
            status="pending",
            slot_sha256={"A": "1" * 64, "B": "2" * 64},
        )

    with pytest.raises(AgentUpdateError, match=message):
        updater.plan(
            artifact,
            release,
            _authorization(artifact, release),
            _signature(),
        )


def test_interrupted_agent_download_never_publishes_activation(tmp_path: Path) -> None:
    content = _elf()
    updater, _trust, transport, supervisor = _updater(tmp_path, content)
    artifact, release = _inputs(content)
    plan = updater.plan(
        artifact,
        release,
        _authorization(artifact, release),
        _signature(),
    )
    transport.fail = True

    with pytest.raises(AgentUpdateError, match="interrupted"):
        updater.apply(plan)

    assert supervisor.requests == []
    assert not (tmp_path / "run/activation-request.json").exists()
    assert not (
        tmp_path / "staging" / f"{artifact.payload_sha256}.agent"
    ).exists()


def test_agent_rejects_receipt_that_disagrees_with_tuf_or_claim_fence(
    tmp_path: Path,
) -> None:
    content = _elf()
    updater, _trust, _transport, supervisor = _updater(tmp_path, content)
    artifact, release = _inputs(content)
    wrong_target = _authorization(artifact, release, target_sha256="0" * 64)

    with pytest.raises(AgentUpdateError, match="signed update plan"):
        updater.plan(artifact, release, wrong_target, _signature())

    authorization = _authorization(artifact, release)
    command = AgentUpdateCommand(artifact, release, authorization, _signature())
    deadline = MonotonicDeadline.bind(datetime.now(UTC) + timedelta(seconds=30))
    with pytest.raises(AgentUpdateError, match="operation fence"):
        updater.execute(
            command,
            deadline,
            "33333333-3333-4333-8333-333333333333",
            FENCE,
        )
    assert supervisor.requests == []


def test_agent_rejects_receipt_with_wrong_local_tuf_targets_version(
    tmp_path: Path,
) -> None:
    content = _elf()
    updater, _trust, _transport, _supervisor = _updater(tmp_path, content)
    artifact, release = _inputs(content)
    authorization = replace(
        _authorization(artifact, release),
        tuf_targets_version=8,
    )

    with pytest.raises(AgentUpdateError, match="signed update plan"):
        updater.plan(artifact, release, authorization, _signature())


def test_local_supervisor_writes_only_typed_activation_request(tmp_path: Path) -> None:
    runtime_root = tmp_path / "run"
    runtime_root.mkdir(mode=0o700)
    supervisor = LocalSupervisor(
        state_path=tmp_path / "state.json",
        runtime_root=runtime_root,
        slot_root=tmp_path / "slots",
    )
    artifact = AgentArtifact(
        architecture="linux-arm64",
        oci_manifest_digest="sha256:" + "3" * 64,
        payload_name="vonk-agent",
        payload_sha256="1" * 64,
        payload_size=4096,
    )
    release = AgentReleaseIdentity(
        platform_version="1.2.3",
        build_digest="sha256:" + "2" * 64,
        protocol_minimum=1,
        protocol_maximum=2,
    )
    authorization = _authorization(artifact, release)
    request = SupervisorActivationRequest(
        authorization=authorization,
        signature=_signature(),
    )

    supervisor.request_activation(request)

    assert json.loads((runtime_root / "activation-request.json").read_text()) == {
            "authorization": {
                "architecture": "linux-arm64",
                "attempt": 1,
                "build_digest": "sha256:" + "2" * 64,
                "claim_deadline": authorization.expires_at,
                "expires_at": authorization.expires_at,
                "fence": FENCE,
                "node_id": "spk_0123456789abcdef0123456789abcdef",
            "oci_manifest_digest": "sha256:" + "3" * 64,
            "operation_id": OPERATION_ID,
            "payload_name": "vonk-agent",
            "platform_target_name": _platform_target("1.2.3", "9" * 64),
            "platform_target_sha256": "9" * 64,
            "platform_version": "1.2.3",
                "previous_sha256": "1" * 64,
                "previous_generation": 1,
            "previous_slot": "A",
            "sha256": "1" * 64,
            "size": 4096,
            "target_slot": "B",
            "tuf_targets_version": 7,
        },
        "schema_version": 2,
        "signature": {
            "algorithm": "ed25519",
            "key_id": "7" * 64,
            "value": "8" * 128,
        },
    }


def rollback_authorization() -> RollbackAuthorization:
    return RollbackAuthorization(
        action="operator-rollback",
        node_id="spk_0123456789abcdef0123456789abcdef",
        attempt=1,
        claim_deadline=2_000_000_000,
        current_generation=2,
        current_slot="B",
        current_sha256="2" * 64,
        operation_id=OPERATION_ID,
        fence=FENCE,
        expires_at=2_000_000_000,
    )


def test_supervisor_rollback_request_is_signed_and_typed() -> None:
    request = SupervisorRollbackRequest(
        rollback_authorization(),
        AuthorizationSignature("7" * 64, "8" * 128),
    )

    assert request.to_mapping() == {
        "authorization": rollback_authorization().to_mapping(),
        "schema_version": 2,
        "signature": AuthorizationSignature("7" * 64, "8" * 128).to_mapping(),
    }


@pytest.mark.parametrize("field", ("attempt", "claim_deadline"))
def test_rollback_command_rejects_boolean_integer_bindings(field: str) -> None:
    receipt = rollback_authorization().to_mapping()
    receipt[field] = True

    with pytest.raises(AgentUpdateError, match="rollback authorization"):
        AgentRollbackCommand.parse(
            {
                "receipt": receipt,
                "signature": AuthorizationSignature(
                    "7" * 64, "8" * 128
                ).to_mapping(),
            }
        )


def test_local_supervisor_requests_only_recorded_previous_slot(tmp_path: Path) -> None:
    state_path = tmp_path / "state/state.json"
    runtime_root = tmp_path / "run"
    slot_root = tmp_path / "slots"
    state_path.parent.mkdir()
    runtime_root.mkdir(mode=0o700)
    (slot_root / "A").mkdir(parents=True)
    state = {
        "activation_deadline": None,
        "active_slot": "B",
        "boot_attempts": 0,
        "expected_sha256": "2" * 64,
        "generation": 2,
        "previous_slot": "A",
        "rollback_performed": False,
        "schema_version": 1,
        "slot_sha256": {"A": "1" * 64, "B": "2" * 64},
        "status": "stable",
    }
    state_path.write_text(
        json.dumps(state, sort_keys=True, separators=(",", ":"))
        + "\n"
    )
    state_path.chmod(0o644)
    identity = {
        "build_digest": "sha256:" + "a" * 64,
        "platform_version": "1.0.0",
        "schema_version": 1,
        "sha256": "1" * 64,
    }
    (slot_root / "A/identity.json").write_text(
        json.dumps(identity, sort_keys=True, separators=(",", ":"))
        + "\n"
    )
    (slot_root / "A/identity.json").chmod(0o444)
    supervisor = LocalSupervisor(
        state_path=state_path,
        runtime_root=runtime_root,
        slot_root=slot_root,
    )

    signed = SupervisorRollbackRequest(
        rollback_authorization(), AuthorizationSignature("7" * 64, "8" * 128)
    )
    pending = supervisor.request_rollback(signed)

    assert pending.target_slot == "A"
    request = json.loads((runtime_root / "rollback-request.json").read_text())
    assert request == {
        "authorization": rollback_authorization().to_mapping(),
        "schema_version": 2,
        "signature": AuthorizationSignature("7" * 64, "8" * 128).to_mapping(),
    }
