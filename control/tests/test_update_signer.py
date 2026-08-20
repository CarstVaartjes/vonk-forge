from __future__ import annotations

import hashlib
import json
import os
import socket
import struct
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from vonk_control.host_state import HostOperationPlan, SelectionReceipt
from vonk_control.settings import SettingsError, SignerSettings
from vonk_control.update_authority import UpdateAuthorizationError
from vonk_control.update_signer import (
    MAX_SIGNER_MESSAGE_BYTES,
    AdminActionGrantVerifier,
    RootUpdateSignerPolicy,
    SignerActiveControlReleaseLoader,
    SignerProtocolError,
    SignerRunningIdentity,
    UnixUpdateSignerClient,
    UpdateSignerConnectionHandler,
    check_signer_ready,
)
from vonk_control.upgrade import ActiveControlRelease, UpgradeConflict

from cluster_profiles.platform_release import PlatformRelease


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "ascii"
    )


_GRANT_KEY = ed25519.Ed25519PrivateKey.generate()
_GRANT_PUBLIC = _GRANT_KEY.public_key().public_bytes(
    serialization.Encoding.Raw,
    serialization.PublicFormat.Raw,
)
_GRANT_KEY_ID = hashlib.sha256(_GRANT_PUBLIC).hexdigest()


def _grant(
    *,
    action: str,
    rollout_id: str,
    parent_job_id: str,
    node_id: str,
    target_release_digest: str | None,
) -> dict[str, object]:
    claims = {
        "action": action,
        "expires_at": 1_800_000_000,
        "nonce": str(uuid.uuid4()),
        "node_ids": [node_id],
        "parent_job_id": parent_job_id,
        "rollout_id": rollout_id,
        "schema_version": 1,
        "target_release_digest": target_release_digest,
    }
    return {
        "claims": claims,
        "signature": {
            "algorithm": "ed25519",
            "key_id": _GRANT_KEY_ID,
            "value": _GRANT_KEY.sign(_canonical(claims)).hex(),
        },
    }


def _grant_verifier() -> AdminActionGrantVerifier:
    return AdminActionGrantVerifier(
        ed25519.Ed25519PublicKey.from_public_bytes(_GRANT_PUBLIC),
        key_id=_GRANT_KEY_ID,
        clock=lambda: datetime.fromtimestamp(1_799_999_000, tz=UTC),
    )


def _request() -> dict[str, object]:
    rollout_id = str(uuid.uuid4())
    parent_job_id = str(uuid.uuid4())
    node_id = "spk_" + "a" * 32
    return {
        "action": "agent.rollback",
        "admin_grant": _grant(
            action="agent.rollback",
            rollout_id=rollout_id,
            parent_job_id=parent_job_id,
            node_id=node_id,
            target_release_digest=None,
        ),
        "attempt": 1,
        "claim_deadline": 1_800_000_000,
        "expires_at": 1_800_000_000,
        "fence": str(uuid.uuid4()),
        "intent_id": str(uuid.uuid4()),
        "node_id": node_id,
        "operation_id": str(uuid.uuid4()),
        "parent_job_id": parent_job_id,
        "rollout_id": rollout_id,
        "schema_version": 1,
        "source": {
            "generation": 7,
            "sha256": "b" * 64,
            "slot": "B",
        },
    }


class DeterministicPolicy:
    def authorize(self, request):
        return {
            "receipt": {
                "action": request["action"],
                "operation_id": request["operation_id"],
            },
            "signature": {
                "algorithm": "ed25519",
                "key_id": "c" * 64,
                "value": "d" * 128,
            },
        }


class ActiveLoader:
    def __init__(
        self,
        *,
        release_digest: str = "sha256:" + "1" * 64,
        build_digest: str = "sha256:" + "2" * 64,
        platform_version: str = "1.2.3",
    ) -> None:
        self.calls = 0
        self.release_digest = release_digest
        self.build_digest = build_digest
        self.platform_version = platform_version

    def load(self):
        self.calls += 1
        return ActiveControlRelease(
            generation_id="gen-" + "1" * 24,
            release_digest=self.release_digest,
            build_digest=self.build_digest,
            platform_version=self.platform_version,
            api_image="registry/api@sha256:" + "3" * 64,
            worker_image="registry/worker@sha256:" + "4" * 64,
            migration_revision="0011_update_rollouts",
        )


class Authority:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def refresh_and_validate(self, payload, *, target_name):
        self.calls.append(("prepare", target_name))
        release = payload["release"]
        return SimpleNamespace(
            release=SimpleNamespace(
                build_digest=release.get("build_digest"),
                digest="sha256:" + "5" * 64,
                platform_version=release.get("platform_version"),
            ),
            target_name=target_name,
            target_sha256="6" * 64,
            targets_version=7,
        )

    def authorize(self, payload, **bindings):
        self.calls.append(("update", bindings))
        return {"receipt": bindings, "signature": {"value": "7" * 128}}

    def authorize_rollback(self, **bindings):
        self.calls.append(("rollback", bindings))
        return {"receipt": bindings, "signature": {"value": "8" * 128}}


def _platform_manifest() -> bytes:
    def artifact(name: str, digest: str) -> dict[str, object]:
        return {
            "name": name,
            "provenance_sha256": "d" * 64,
            "reference": f"registry.example/vonk-forge/{name}@sha256:{digest}",
            "sbom_sha256": "e" * 64,
            "sha256": digest,
            "size": 1024,
        }

    document = {
        "agents": [
            {
                "architecture": "linux-arm64",
                "artifact": artifact("agent-linux-arm64", "a" * 64),
                "payload": {"name": "vonk-agent", "sha256": "b" * 64, "size": 4096},
                "protocol": {"maximum": 2, "minimum": 1},
            }
        ],
        "build_digest": "sha256:" + "c" * 64,
        "deployment_bundle": {
            "layer_digest": "sha256:" + "a" * 64,
            "layer_media_type": "application/vnd.vonk-forge.control-deployment.v1.tar",
            "layer_size": 1048576,
            "manifest_digest": "sha256:" + "f" * 64,
            "manifest_media_type": "application/vnd.oci.image.manifest.v1+json",
            "manifest_size": 4096,
            "reference": "registry.example/vonk-forge/control@sha256:" + "f" * 64,
        },
        "host_updater_abi": {"maximum": 2, "minimum": 1},
        "control": {
            "assets": [artifact("web", "f" * 64)],
            "config_version": 1,
            "images": {
                "api": artifact("api", "a" * 64),
                "worker": artifact("worker", "b" * 64),
            },
            "protocol": {"maximum": 1, "minimum": 1},
        },
        "database": {
            "contract_revision": None,
            "expand_revision": "0011_update_rollouts",
            "predecessor_compatible": True,
        },
        "platform_version": "1.2.3",
        "rollback": {
            "predecessors": [
                {
                    "build_digest": "sha256:" + "0" * 64,
                    "deployment_bundle_digest": "sha256:" + "1" * 64,
                    "release_digest": "sha256:" + "2" * 64,
                    "target_name": "platform/releases/1.2.2/" + "3" * 64 + ".json",
                    "target_sha256": "3" * 64,
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
    return _canonical(document)


class ProjectionReleaseSource:
    def __init__(self, target: bytes) -> None:
        self.target = target
        self.version: int | bool = 7
        self.target_names: list[str] = []

    def refresh(self, target_name: str) -> tuple[bytes, int]:
        self.target_names.append(target_name)
        return self.target, self.version


def _active_projection(target: bytes) -> dict[str, object]:
    release = PlatformRelease.from_bytes(target)
    target_sha256 = hashlib.sha256(target).hexdigest()
    plan = HostOperationPlan(
        operation_id="operation-1",
        plan_digest="sha256:" + "8" * 64,
        generation_id="gen-" + target_sha256[:24],
        platform_target_name=(
            f"platform/releases/{release.platform_version}/{target_sha256}.json"
        ),
        platform_target_sha256=target_sha256,
        tuf_targets_version=7,
        release_digest=release.digest,
        build_digest=release.build_digest,
        platform_version=release.platform_version,
        deployment_bundle_digest=release.deployment_bundle.layer_digest,
        api_image=release.control.api_image.reference,
        worker_image=release.control.worker_image.reference,
        database_revision=release.database.expand_revision,
    )
    selection = SelectionReceipt.from_plan(
        plan,
        previous_generation=None,
    ).document()
    return {
        "generation_receipt_sha256": selection["generation_receipt_sha256"],
        "projection_kind": "active",
        "projection_sequence": 1,
        "schema_version": 1,
        "selection": selection,
        "selection_receipt_sha256": hashlib.sha256(
            _canonical(selection)
        ).hexdigest(),
    }


def _selected_generation(active: dict[str, object]) -> dict[str, object]:
    selection = active["selection"]
    assert isinstance(selection, dict)
    generation = selection["generation"]
    assert isinstance(generation, dict)
    return generation


def _refresh_active_receipt_digests(active: dict[str, object]) -> None:
    selection = active["selection"]
    assert isinstance(selection, dict)
    generation = selection["generation"]
    assert isinstance(generation, dict)
    generation_digest = hashlib.sha256(_canonical(generation)).hexdigest()
    selection["generation_receipt_sha256"] = generation_digest
    active["generation_receipt_sha256"] = generation_digest
    active["selection_receipt_sha256"] = hashlib.sha256(
        _canonical(selection)
    ).hexdigest()


def _write_active_projection(root: Path, document: dict[str, object]) -> None:
    root.mkdir(mode=0o755)
    active = root / "active.json"
    active.write_bytes(_canonical(document))
    active.chmod(0o444)


def _running_identity(target: bytes) -> SignerRunningIdentity:
    release = PlatformRelease.from_bytes(target)
    return SignerRunningIdentity(
        release_digest=release.digest,
        build_digest=release.build_digest,
        platform_version=release.platform_version,
        process_image=release.control.worker_image.reference,
    )


def test_signer_settings_require_exact_running_image_and_identity_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in (
        "VONK_AGENT_UPDATE_AUTHORITY_KEY_FILE",
        "VONK_ADMIN_GRANT_PUBLIC_KEY_FILE",
        "VONK_AGENT_TUF_BOOTSTRAP_ROOT_FILE",
    ):
        secret = tmp_path / variable.lower()
        secret.write_text("fixture")
        monkeypatch.setenv(variable, str(secret))
    identity_root = tmp_path / "control-identity"
    monkeypatch.setenv("VONK_CONTROL_IDENTITY_ROOT", str(identity_root))
    monkeypatch.setenv("VONK_PLATFORM_VERSION", "1.2.3")
    monkeypatch.setenv("VONK_PLATFORM_RELEASE_DIGEST", "sha256:" + "a" * 64)
    monkeypatch.setenv("VONK_PLATFORM_BUILD_DIGEST", "sha256:" + "b" * 64)
    monkeypatch.setenv(
        "VONK_CONTROL_PROCESS_IMAGE",
        "registry.example/vonk-forge/worker@sha256:" + "c" * 64,
    )

    settings = SignerSettings.from_env_and_secrets()

    assert settings.control_identity_root == identity_root
    assert settings.process_image.endswith("@sha256:" + "c" * 64)
    monkeypatch.setenv("VONK_CONTROL_PROCESS_IMAGE", "worker:latest")
    with pytest.raises(SettingsError, match="VONK_CONTROL_PROCESS_IMAGE"):
        SignerSettings.from_env_and_secrets()


def _serve_once(
    path: Path,
    *,
    allowed_uid: int,
    policy=None,
    healthcheck_uid: int | None = None,
):
    ready = threading.Event()
    errors: list[BaseException] = []

    def serve() -> None:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(path))
            listener.listen(1)
            ready.set()
            connection, _ = listener.accept()
            with connection:
                UpdateSignerConnectionHandler(
                    policy or DeterministicPolicy(),
                    allowed_peer_uid=allowed_uid,
                    healthcheck_peer_uid=healthcheck_uid,
                ).handle(connection)
        except (OSError, SignerProtocolError) as error:
            errors.append(error)
        finally:
            listener.close()

    thread = threading.Thread(target=serve)
    thread.start()
    assert ready.wait(2)
    return thread, errors


def _closed_without_response(connection: socket.socket) -> None:
    try:
        assert connection.recv(1024) == b""
    except ConnectionResetError:
        pass


def test_unix_signer_round_trip_is_canonical_bounded_and_deterministic(
    tmp_path: Path,
) -> None:
    request = _request()
    path = tmp_path / "signer.sock"
    thread, errors = _serve_once(path, allowed_uid=os.getuid())

    response = UnixUpdateSignerClient(path).authorize(request)

    thread.join(2)
    assert not errors
    assert set(response) == {
        "intent_id",
        "request_digest",
        "schema_version",
        "signed_payload",
    }
    assert response["schema_version"] == 1
    assert response["request_digest"] == hashlib.sha256(_canonical(request)).hexdigest()
    second_path = tmp_path / "signer-second.sock"
    second_thread, second_errors = _serve_once(second_path, allowed_uid=os.getuid())
    second = UnixUpdateSignerClient(second_path).authorize(request)
    second_thread.join(2)
    assert not second_errors
    assert second == response


def test_signer_healthcheck_completes_a_protocol_round_trip_without_signing(
    tmp_path: Path,
) -> None:
    class RejectPolicy:
        def authorize(self, _request):
            raise AssertionError("healthcheck must not invoke signing policy")

    path = tmp_path / "signer-health.sock"
    thread, errors = _serve_once(
        path,
        allowed_uid=os.getuid() + 1,
        policy=RejectPolicy(),
        healthcheck_uid=os.getuid(),
    )

    check_signer_ready(path)

    thread.join(2)
    assert not errors


def test_signer_rejects_noncanonical_and_oversized_messages(tmp_path: Path) -> None:
    for index, raw in enumerate(
        (
            json.dumps(_request(), indent=2).encode() + b"\n",
            b"{" + b"x" * MAX_SIGNER_MESSAGE_BYTES + b"}\n",
        )
    ):
        path = tmp_path / f"signer-{index}.sock"
        thread, errors = _serve_once(path, allowed_uid=os.getuid())
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.connect(str(path))
        connection.sendall(raw)
        connection.shutdown(socket.SHUT_WR)
        _closed_without_response(connection)
        connection.close()
        thread.join(2)
        assert len(errors) == 1
        assert isinstance(errors[0], SignerProtocolError)


def test_signer_rejects_unexpected_peer_uid(tmp_path: Path) -> None:
    path = tmp_path / "signer.sock"
    thread, errors = _serve_once(path, allowed_uid=os.getuid() + 1)
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.connect(str(path))
    try:
        connection.sendall(_canonical(_request()))
        connection.shutdown(socket.SHUT_WR)
    except BrokenPipeError:
        pass
    _closed_without_response(connection)
    connection.close()
    thread.join(2)

    assert len(errors) == 1
    assert isinstance(errors[0], SignerProtocolError)
    assert "peer" in str(errors[0])


def test_signer_bounds_idle_authorized_peer_time() -> None:
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    errors: list[BaseException] = []

    def handle() -> None:
        with server:
            try:
                UpdateSignerConnectionHandler(
                    DeterministicPolicy(),
                    allowed_peer_uid=os.getuid(),
                    request_timeout_seconds=0.05,
                ).handle(server)
            except SignerProtocolError as error:
                errors.append(error)

    thread = threading.Thread(target=handle)
    thread.start()
    client.sendall(b"{")
    thread.join(1)
    client.close()

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], SignerProtocolError)
    assert "timeout" in str(errors[0])


@pytest.mark.parametrize("disconnect_phase", ("receive", "send"))
def test_signer_maps_peer_disconnect_to_protocol_error(
    disconnect_phase: str,
) -> None:
    class DisconnectingConnection:
        def __init__(self) -> None:
            self.read = False

        def getsockopt(self, *_args):
            return struct.pack("3i", os.getpid(), os.getuid(), os.getgid())

        def settimeout(self, _timeout: float) -> None:
            pass

        def recv(self, _maximum: int) -> bytes:
            if disconnect_phase == "receive":
                raise ConnectionResetError("peer reset")
            if self.read:
                return b""
            self.read = True
            return _canonical(_request())

        def sendall(self, _content: bytes) -> None:
            raise BrokenPipeError("peer closed")

    with pytest.raises(SignerProtocolError, match="disconnected"):
        UpdateSignerConnectionHandler(
            DeterministicPolicy(), allowed_peer_uid=os.getuid()
        ).handle(DisconnectingConnection())  # type: ignore[arg-type]


def test_signer_rejects_recursive_request_before_socket_io(tmp_path: Path) -> None:
    recursive: dict[str, object] = {}
    recursive["self"] = recursive

    with pytest.raises(SignerProtocolError, match="canonical JSON"):
        UnixUpdateSignerClient(tmp_path / "absent.sock").authorize(recursive)


def test_signer_loads_fresh_active_projection_and_refreshes_its_exact_target(
    tmp_path: Path,
) -> None:
    target = _platform_manifest()
    identity_root = tmp_path / "control-identity"
    active = _active_projection(target)
    _write_active_projection(identity_root, active)
    source = ProjectionReleaseSource(target)
    loader = SignerActiveControlReleaseLoader(
        identity_root,
        source,
        lambda: _running_identity(target),
        owner_uid=os.geteuid(),
    )

    loaded = loader.load()
    generation = _selected_generation(active)

    assert loaded == ActiveControlRelease(
        generation_id=str(generation["generation_id"]),
        release_digest=str(generation["release_digest"]),
        build_digest="sha256:" + "c" * 64,
        platform_version="1.2.3",
        api_image="registry.example/vonk-forge/api@sha256:" + "a" * 64,
        worker_image="registry.example/vonk-forge/worker@sha256:" + "b" * 64,
        migration_revision="0011_update_rollouts",
    )
    assert source.target_names == [generation["platform_target_name"]]

    active_path = identity_root / "active.json"
    active_path.chmod(0o644)
    active["projection_kind"] = "candidate"
    active_path.write_bytes(_canonical(active))
    active_path.chmod(0o444)
    with pytest.raises(UpgradeConflict, match="active control identity"):
        loader.load()
    assert source.target_names == [generation["platform_target_name"]]


@pytest.mark.parametrize(
    "fault",
    (
        "symlink",
        "target-version",
        "targets-version",
        "boolean-targets-version",
        "running-image",
    ),
)
def test_signer_rejects_unsafe_or_mismatched_active_projection(
    tmp_path: Path,
    fault: str,
) -> None:
    target = _platform_manifest()
    identity_root = tmp_path / "control-identity"
    active = _active_projection(target)
    _write_active_projection(identity_root, active)
    source = ProjectionReleaseSource(target)
    running = _running_identity(target)
    if fault == "symlink":
        active_path = identity_root / "active.json"
        outside = tmp_path / "outside.json"
        outside.write_bytes(active_path.read_bytes())
        outside.chmod(0o444)
        active_path.unlink()
        active_path.symlink_to(outside)
    elif fault == "target-version":
        generation = _selected_generation(active)
        generation["platform_target_name"] = (
            "platform/releases/9.9.9/"
            + str(generation["platform_target_sha256"])
            + ".json"
        )
        _refresh_active_receipt_digests(active)
        active_path = identity_root / "active.json"
        active_path.chmod(0o644)
        active_path.write_bytes(_canonical(active))
        active_path.chmod(0o444)
    elif fault == "targets-version":
        _selected_generation(active)["tuf_targets_version"] = 8
        _refresh_active_receipt_digests(active)
        active_path = identity_root / "active.json"
        active_path.chmod(0o644)
        active_path.write_bytes(_canonical(active))
        active_path.chmod(0o444)
    elif fault == "boolean-targets-version":
        source.version = True
    else:
        running = SignerRunningIdentity(
            release_digest=running.release_digest,
            build_digest=running.build_digest,
            platform_version=running.platform_version,
            process_image="registry.example/vonk-forge/other@sha256:" + "0" * 64,
        )

    loader = SignerActiveControlReleaseLoader(
        identity_root,
        source,
        lambda: running,
        owner_uid=os.geteuid(),
    )

    with pytest.raises(UpgradeConflict):
        loader.load()


def test_admin_grant_public_key_rejects_hardlinked_trust_document(
    tmp_path: Path,
) -> None:
    document = tmp_path / "admin-grant-public.json"
    document.write_bytes(
        _canonical(
            {
                "algorithm": "ed25519",
                "key_id": _GRANT_KEY_ID,
                "public_key": _GRANT_PUBLIC.hex(),
                "schema_version": 1,
            }
        )
    )
    os.link(document, tmp_path / "attacker-link.json")

    with pytest.raises(UpdateAuthorizationError, match="unsafe"):
        AdminActionGrantVerifier.from_public_document(document)


def test_root_policy_verifies_active_control_and_exact_tuf_update() -> None:
    active = ActiveLoader(
        release_digest="sha256:" + "5" * 64,
        build_digest="sha256:" + "9" * 64,
        platform_version="2.0.0",
    )
    authority = Authority()
    request = _request() | {
        "action": "agent.update",
        "platform_target_name": "platform/releases/2.0.0/" + "6" * 64 + ".json",
        "expected_tuf_target_sha256": "6" * 64,
        "expected_tuf_targets_version": 7,
        "payload": {
            "artifact": {"architecture": "linux-arm64"},
            "release": {
                "build_digest": "sha256:" + "9" * 64,
                "platform_version": "2.0.0",
            },
        },
        "target_release_digest": "sha256:" + "5" * 64,
    }
    request["admin_grant"] = _grant(
        action="agent.update",
        rollout_id=str(request["rollout_id"]),
        parent_job_id=str(request["parent_job_id"]),
        node_id=str(request["node_id"]),
        target_release_digest="sha256:" + "5" * 64,
    )

    signed = RootUpdateSignerPolicy(authority, active, _grant_verifier()).authorize(
        request
    )

    assert active.calls == 1
    assert authority.calls[0] == (
        "prepare",
        "platform/releases/2.0.0/" + "6" * 64 + ".json",
    )
    assert authority.calls[1][0] == "update"
    assert signed["signature"]["value"] == "7" * 128


def test_root_policy_rejects_tuf_valid_update_not_selected_on_nas() -> None:
    request = _request() | {
        "action": "agent.update",
        "platform_target_name": "platform/releases/2.0.0/" + "6" * 64 + ".json",
        "expected_tuf_target_sha256": "6" * 64,
        "expected_tuf_targets_version": 7,
        "payload": {
            "artifact": {"architecture": "linux-arm64"},
            "release": {
                "build_digest": "sha256:" + "9" * 64,
                "platform_version": "2.0.0",
            },
        },
        "target_release_digest": "sha256:" + "5" * 64,
    }
    request["admin_grant"] = _grant(
        action="agent.update",
        rollout_id=str(request["rollout_id"]),
        parent_job_id=str(request["parent_job_id"]),
        node_id=str(request["node_id"]),
        target_release_digest="sha256:" + "5" * 64,
    )

    with pytest.raises(UpdateAuthorizationError, match="active control release"):
        RootUpdateSignerPolicy(
            Authority(), ActiveLoader(), _grant_verifier()
        ).authorize(request)


@pytest.mark.parametrize(
    "fault", ("unknown-field", "release-mismatch", "target-name")
)
def test_root_policy_rejects_arbitrary_or_tuf_mismatched_update(fault: str) -> None:
    request = _request() | {
        "action": "agent.update",
        "platform_target_name": "platform/releases/2.0.0/" + "6" * 64 + ".json",
        "expected_tuf_target_sha256": "6" * 64,
        "expected_tuf_targets_version": 7,
        "payload": {
            "artifact": {"architecture": "linux-arm64"},
            "release": {"platform_version": "2.0.0"},
        },
        "target_release_digest": "sha256:" + "5" * 64,
    }
    request["admin_grant"] = _grant(
        action="agent.update",
        rollout_id=str(request["rollout_id"]),
        parent_job_id=str(request["parent_job_id"]),
        node_id=str(request["node_id"]),
        target_release_digest="sha256:" + "5" * 64,
    )
    if fault == "unknown-field":
        request["shell"] = "id"
    elif fault == "release-mismatch":
        request["target_release_digest"] = "sha256:" + "9" * 64
    else:
        request["platform_target_name"] = "platform-release.json"

    with pytest.raises(UpdateAuthorizationError):
        RootUpdateSignerPolicy(
            Authority(), ActiveLoader(), _grant_verifier()
        ).authorize(request)


@pytest.mark.parametrize(
    "fault",
    ("missing", "tampered-action", "tampered-node", "tampered-signature"),
)
def test_root_policy_requires_exact_api_issued_admin_grant(fault: str) -> None:
    request = _request()
    if fault == "missing":
        request.pop("admin_grant")
    elif fault == "tampered-action":
        request["action"] = "agent.update"
        request["payload"] = {}
        request["platform_target_name"] = (
            "platform/releases/2.0.0/" + "6" * 64 + ".json"
        )
        request["target_release_digest"] = "sha256:" + "5" * 64
        request["expected_tuf_target_sha256"] = "6" * 64
        request["expected_tuf_targets_version"] = 7
    elif fault == "tampered-node":
        request["node_id"] = "spk_" + "b" * 32
    else:
        request["admin_grant"]["signature"]["value"] = "0" * 128

    with pytest.raises(UpdateAuthorizationError):
        RootUpdateSignerPolicy(
            Authority(), ActiveLoader(), _grant_verifier()
        ).authorize(request)
