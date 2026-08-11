from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import threading
from configparser import ConfigParser
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from packages.test_backends import OBJECT, invocation_document
from vonk_agent.package_helper import (
    Ed25519ReceiptVerifier,
    PackageHelper,
    SignedFenceAuthorizer,
    SystemdBackendLauncher,
    main,
    serve_connection,
)
from vonk_agent.package_helper_protocol import (
    HelperProtocolError,
    HelperRequest,
    HelperResponse,
    SignedObjectReceipt,
    canonical_helper_document,
    frame_helper_message,
    receive_helper_message,
)
from vonk_agent.packages.backends import BackendInvocation
from vonk_agent.packages.sandbox import SandboxPolicy
from vonk_agent_protocol.workload_packages import (
    PACKAGE_HELPER_AUTHORITY,
    PackageHelperGrantClaims,
    PackageHelperOperation,
    PackageHelperSignature,
    PackageObjectReceiptClaims,
    SignedPackageHelperGrant,
    SignedPackageObjectReceipt,
    package_helper_grant_signing_bytes,
    package_object_receipt_signing_bytes,
)

REQUEST_ID = "11111111-1111-4111-8111-111111111111"
JOB_ID = "22222222-2222-4222-8222-222222222222"
OPERATION_ID = "33333333-3333-4333-8333-333333333333"
FENCE = "44444444-4444-4444-8444-444444444444"
NODE_ID = "spk_" + "1" * 32
KEY_ID = "b" * 64
SIGNATURE = "a" * 128


def receipt_document(
    digest: str = OBJECT, size: int = 4096
) -> dict[str, object]:
    return {
        "claims": {
            "schema_version": 1,
            "authority": PACKAGE_HELPER_AUTHORITY,
            "object_digest": digest,
            "size": size,
            "relative_name": f"objects/sha256/{digest}",
        },
        "signature": {
            "algorithm": "ed25519",
            "key_id": KEY_ID,
            "value": SIGNATURE,
        },
    }


def request_document() -> dict[str, object]:
    body = {
        "schema_version": 1,
        "request_id": REQUEST_ID,
        "node_id": NODE_ID,
        "job_id": JOB_ID,
        "operation_id": OPERATION_ID,
        "attempt": 1,
        "fence": FENCE,
        "operation": "health",
        "invocation": invocation_document(),
        "receipts": [receipt_document()],
    }
    document = {"schema_version": 1, "body": body, "grant": {}}
    return _bind_request(document)


def _bind_request(
    document: dict[str, object],
    *,
    private: Ed25519PrivateKey | None = None,
    issued_at: int | None = None,
    expires_at: int | None = None,
) -> dict[str, object]:
    body = document["body"]
    assert isinstance(body, dict)
    invocation = body["invocation"]
    assert isinstance(invocation, dict)
    now = int(datetime.now(UTC).timestamp()) if issued_at is None else issued_at
    claims = PackageHelperGrantClaims(
        1,
        PACKAGE_HELPER_AUTHORITY,
        body["request_id"],
        body["node_id"],
        body["job_id"],
        body["operation_id"],
        body["attempt"],
        body["fence"],
        invocation["release_digest"],
        invocation["generation"],
        PackageHelperOperation(body["operation"]),
        hashlib.sha256(canonical_helper_document(body)).hexdigest(),
        now,
        now + 300 if expires_at is None else expires_at,
    )
    if private is None:
        key_id = KEY_ID
        signature = SIGNATURE
    else:
        key_id = hashlib.sha256(private.public_key().public_bytes_raw()).hexdigest()
        signature = private.sign(package_helper_grant_signing_bytes(claims)).hex()
    document["grant"] = SignedPackageHelperGrant(
        claims,
        PackageHelperSignature("ed25519", key_id, signature),
    ).to_mapping()
    return document


def _request(document: dict[str, object]) -> HelperRequest:
    return HelperRequest.parse(
        canonical_helper_document(_bind_request(document))
    )


def test_helper_protocol_requires_duplicate_free_canonical_json() -> None:
    raw = canonical_helper_document(request_document())
    request = HelperRequest.parse(raw)

    assert request.invocation == BackendInvocation.parse(invocation_document())
    assert request.receipts == (
        SignedPackageObjectReceipt.parse(receipt_document()),
    )

    reordered = json.dumps(request_document(), separators=(",", ":")).encode()
    duplicate = raw.replace(b'"attempt":1,', b'"attempt":1,"attempt":1,', 1)
    with pytest.raises(HelperProtocolError, match="canonical"):
        HelperRequest.parse(reordered)
    with pytest.raises(HelperProtocolError, match="duplicate"):
        HelperRequest.parse(duplicate)


class ReceiptVerifier:
    def __init__(self) -> None:
        self.checked: list[SignedObjectReceipt] = []
        self.public_key_bytes = b"r" * 32

    def verify(self, receipt: SignedObjectReceipt) -> bool:
        self.checked.append(receipt)
        return True


class FenceAuthorizer:
    def __init__(self, permitted: bool = True) -> None:
        self.permitted = permitted
        self.public_key_bytes = b"f" * 32

    def authorize(self, request: HelperRequest, request_digest: str) -> bool:
        return self.permitted and len(request_digest) == 64


class ActiveSlotBoundary:
    def verify(self) -> None:
        pass


class Launcher:
    def __init__(self) -> None:
        self.plans = []

    def launch(self, request: HelperRequest, sandbox: SandboxPolicy):
        self.plans.append((request, sandbox))
        return {
            "status": "launched",
            "evidence_digest": "c" * 64,
            "fence": request.fence,
        }


def _helper(*, permitted: bool = True, agent_uid: int = 64000):
    verifier = ReceiptVerifier()
    launcher = Launcher()
    helper = PackageHelper(
        agent_uid=agent_uid,
        sandbox=SandboxPolicy(
            workload_uid=64001,
            workload_gid=64001,
            allowed_devices=("nvidia0",),
        ),
        active_slot_verifier=ActiveSlotBoundary(),
        receipt_verifier=verifier,
        fence_authorizer=FenceAuthorizer(permitted),
        launcher=launcher,
    )
    return helper, verifier, launcher


def test_helper_rejects_non_agent_peer_before_parsing_or_launching() -> None:
    helper, verifier, launcher = _helper()

    with pytest.raises(HelperProtocolError, match="peer"):
        helper.handle(64002, b"not-json")
    assert verifier.checked == []
    assert launcher.plans == []


def test_helper_verifies_receipts_and_fence_then_returns_bound_evidence() -> None:
    helper, verifier, launcher = _helper()

    raw = helper.handle(64000, canonical_helper_document(request_document()))
    response = HelperResponse.parse(raw)

    assert response.status == "launched"
    assert response.fence == FENCE
    assert response.evidence_digest == "c" * 64
    assert len(verifier.checked) == 1
    assert launcher.plans[0][1].workload_uid == 64001


def test_helper_rejects_stale_fence_and_request_replay() -> None:
    stale, _, stale_launcher = _helper(permitted=False)
    raw = canonical_helper_document(request_document())

    with pytest.raises(HelperProtocolError, match="fence"):
        stale.handle(64000, raw)
    assert stale_launcher.plans == []

    helper, _, launcher = _helper()
    helper.handle(64000, raw)
    with pytest.raises(HelperProtocolError, match="replay"):
        helper.handle(64000, raw)
    assert len(launcher.plans) == 1


def test_helper_rejects_unsigned_receipt_and_cross_fence_launcher_result() -> None:
    class RejectingVerifier:
        def verify(self, _receipt):
            return False

    launcher = Launcher()
    helper = PackageHelper(
        agent_uid=64000,
        sandbox=SandboxPolicy(64001, 64001, allowed_devices=("nvidia0",)),
        active_slot_verifier=ActiveSlotBoundary(),
        receipt_verifier=RejectingVerifier(),
        fence_authorizer=FenceAuthorizer(),
        launcher=launcher,
    )
    with pytest.raises(HelperProtocolError, match="receipt"):
        helper.handle(64000, canonical_helper_document(request_document()))

    class WrongFenceLauncher(Launcher):
        def launch(self, request, sandbox):
            return {
                "status": "launched",
                "evidence_digest": "c" * 64,
                "fence": "55555555-5555-4555-8555-555555555555",
            }

    helper = PackageHelper(
        agent_uid=64000,
        sandbox=SandboxPolicy(64001, 64001, allowed_devices=("nvidia0",)),
        active_slot_verifier=ActiveSlotBoundary(),
        receipt_verifier=ReceiptVerifier(),
        fence_authorizer=FenceAuthorizer(),
        launcher=WrongFenceLauncher(),
    )
    with pytest.raises(HelperProtocolError, match="fence"):
        helper.handle(64000, canonical_helper_document(request_document()))


def test_signed_fence_authorizer_rejects_invalid_and_replayed_grants_after_restart(
    tmp_path: Path,
) -> None:
    private = Ed25519PrivateKey.generate()
    public_path = tmp_path / "fence-public.pem"
    public_path.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    public_path.chmod(0o644)
    replay = tmp_path / "helper-replay.sqlite3"
    document = request_document()
    document["body"]["invocation"]["network"] = {"mode": "none", "egress": []}
    _bind_request(document, private=private)
    request = HelperRequest.parse(canonical_helper_document(document))

    first = SignedFenceAuthorizer.from_file(
        public_path, replay, allow_unprivileged_test_files=True
    )
    assert first.authorize(request, request.digest) is True
    restarted = SignedFenceAuthorizer.from_file(
        public_path, replay, allow_unprivileged_test_files=True
    )
    assert restarted.authorize(request, request.digest) is False

    changed = request_document()
    changed["body"]["attempt"] = 2
    _bind_request(changed, private=private)
    changed["grant"]["signature"]["value"] = "f" * 128
    stale = HelperRequest.parse(canonical_helper_document(changed))
    assert restarted.authorize(stale, stale.digest) is False


def test_helper_accepts_body_digest_bound_by_real_signed_authorizer(
    tmp_path: Path,
) -> None:
    private = Ed25519PrivateKey.generate()
    public_path = tmp_path / "fence-public.pem"
    public_path.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    public_path.chmod(0o644)
    document = request_document()
    _bind_request(document, private=private)
    request = HelperRequest.parse(canonical_helper_document(document))
    helper = PackageHelper(
        agent_uid=64000,
        sandbox=SandboxPolicy(64001, 64001, allowed_devices=("nvidia0",)),
        active_slot_verifier=ActiveSlotBoundary(),
        receipt_verifier=ReceiptVerifier(),
        fence_authorizer=SignedFenceAuthorizer.from_file(
            public_path,
            tmp_path / "helper-replay.sqlite3",
            allow_unprivileged_test_files=True,
        ),
        launcher=Launcher(),
    )

    response = HelperResponse.parse(helper.handle(64000, request.to_bytes()))

    assert response.request_digest == request.digest


def test_signed_fence_authorizer_rejects_expired_grants_and_caps_replay_state(
    tmp_path: Path,
) -> None:
    private = Ed25519PrivateKey.generate()
    public_path = tmp_path / "fence-public.pem"
    public_path.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    public_path.chmod(0o644)
    now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)

    def signed_document(request_id: str, fence: str, expires_at: datetime):
        document = request_document()
        document["body"]["request_id"] = request_id
        document["body"]["fence"] = fence
        expiry_epoch = int(expires_at.timestamp())
        issued_epoch = min(int(now.timestamp()), expiry_epoch - 300)
        _bind_request(
            document,
            private=private,
            issued_at=issued_epoch,
            expires_at=expiry_epoch,
        )
        return HelperRequest.parse(canonical_helper_document(document))

    authorizer = SignedFenceAuthorizer.from_file(
        public_path,
        tmp_path / "bounded-replay.sqlite3",
        allow_unprivileged_test_files=True,
        clock=lambda: now,
        max_entries=1,
    )
    expired = signed_document(REQUEST_ID, FENCE, now - timedelta(seconds=1))
    assert authorizer.authorize(expired, expired.digest) is False
    first = signed_document(REQUEST_ID, FENCE, now + timedelta(minutes=5))
    assert authorizer.authorize(first, first.digest) is True
    second = signed_document(
        "55555555-5555-4555-8555-555555555555",
        "66666666-6666-4666-8666-666666666666",
        now + timedelta(minutes=5),
    )
    with pytest.raises(HelperProtocolError, match="capacity"):
        authorizer.authorize(second, second.digest)


def test_ed25519_receipt_verifier_accepts_only_exact_canonical_receipt_signature(
    tmp_path: Path,
) -> None:
    private = Ed25519PrivateKey.generate()
    public_path = tmp_path / "receipt-public.pem"
    public_path.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    public_path.chmod(0o644)
    key_id = hashlib.sha256(private.public_key().public_bytes_raw()).hexdigest()
    claims = PackageObjectReceiptClaims(
        1,
        PACKAGE_HELPER_AUTHORITY,
        OBJECT,
        4096,
        f"objects/sha256/{OBJECT}",
    )
    signature = private.sign(package_object_receipt_signing_bytes(claims)).hex()
    receipt = SignedPackageObjectReceipt(
        claims, PackageHelperSignature("ed25519", key_id, signature)
    )
    verifier = Ed25519ReceiptVerifier.from_file(
        public_path, allow_unprivileged_test_file=True
    )

    assert verifier.verify(receipt) is True
    assert (
        verifier.verify(
            SignedPackageObjectReceipt(
                PackageObjectReceiptClaims(
                    1,
                    PACKAGE_HELPER_AUTHORITY,
                    OBJECT,
                    4097,
                    f"objects/sha256/{OBJECT}",
                ),
                PackageHelperSignature("ed25519", key_id, signature),
            )
        )
        is False
    )


def test_socket_helper_uses_length_frame_without_waiting_for_peer_eof() -> None:
    agent_uid = os.geteuid()
    if agent_uid == 0:
        pytest.skip("socket peer identity test requires an unprivileged test UID")
    helper, _, _ = _helper(agent_uid=agent_uid)
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    failure: list[BaseException] = []

    def run() -> None:
        try:
            serve_connection(helper, server, timeout_seconds=1.0)
        except (HelperProtocolError, OSError) as error:
            failure.append(error)
        finally:
            server.close()

    thread = threading.Thread(target=run)
    thread.start()
    try:
        client.sendall(
            frame_helper_message(canonical_helper_document(request_document()))
        )
        response = HelperResponse.parse(
            receive_helper_message(client, timeout_seconds=1.0)
        )
        assert response.fence == FENCE
    finally:
        client.close()
        thread.join(2)
    assert failure == []


def test_concrete_launcher_uses_sealed_content_and_fixed_systemd_sandbox(
    tmp_path: Path,
) -> None:
    generations = tmp_path / "generations"
    executable = generations / ("a" * 64) / "gen-20260806-a" / "bin" / "future-adapter"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"signed future adapter")
    executable.chmod(0o500)
    digest = __import__("hashlib").sha256(executable.read_bytes()).hexdigest()
    objects = tmp_path / "objects" / "sha256"
    objects.mkdir(parents=True)
    mount_content = b"signed model content"
    mount_digest = __import__("hashlib").sha256(mount_content).hexdigest()
    mount_object = objects / mount_digest
    mount_object.write_bytes(mount_content)
    mount_object.chmod(0o400)
    document = request_document()
    document["body"]["invocation"]["network"] = {"mode": "none", "egress": []}
    document["body"]["invocation"]["mounts"] = [
        {
            "object_digest": mount_digest,
            "target": "models/primary",
            "read_only": True,
        }
    ]
    document["body"]["receipts"] = [
        receipt_document(digest, executable.stat().st_size),
        receipt_document(mount_digest, len(mount_content)),
    ]
    request = _request(document)

    class Runner:
        def __init__(self):
            self.calls = []
            self.cleaned = []

        def run(self, argv, *, pass_fds, timeout_seconds):
            metadata = os.fstat(pass_fds[1])
            assert metadata.st_uid == os.geteuid()
            assert metadata.st_gid == os.getegid()
            assert metadata.st_mode & 0o777 == 0o500
            self.calls.append((argv, pass_fds, timeout_seconds))
            return 0

        def cleanup(self, unit_name):
            self.cleaned.append(unit_name)

    runner = Runner()
    launcher = SystemdBackendLauncher(generations, objects_root=objects, runner=runner)
    if os.geteuid() == 0:
        pytest.skip("snapshot ownership test requires an unprivileged test UID")
    sandbox = SandboxPolicy(os.geteuid(), os.getegid(), allowed_devices=("nvidia0",))

    result = launcher.launch(request, sandbox)

    assert result["status"] == "launched"
    assert result["fence"] == FENCE
    argv, pass_fds, timeout = runner.calls[0]
    assert argv[0] == "/usr/bin/systemd-run"
    assert f"--uid={os.geteuid()}" in argv
    assert f"--gid={os.getegid()}" in argv
    assert "--property=NoNewPrivileges=yes" in argv
    assert "--property=CapabilityBoundingSet=" in argv
    assert "--property=AmbientCapabilities=" in argv
    assert "--property=DevicePolicy=closed" in argv
    assert argv.count("--property=PrivateNetwork=yes") == 1
    assert "--property=RuntimeMaxSec=60" in argv
    assert len(pass_fds) == 3
    mount_source = f"/proc/{os.getpid()}/fd/{pass_fds[2]}"
    assert (
        f"--property=BindReadOnlyPaths={mount_source}:"
        "/run/vonk-forge-agent/generation/models/primary"
    ) in argv
    assert timeout == 60
    assert runner.cleaned == []


def test_python_launcher_uses_signed_generation_interpreter_and_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generations = tmp_path / "generations"
    release = generations / ("a" * 64) / "gen-20260806-a"
    adapter = release / "components" / "future-adapter" / "future-adapter"
    interpreter = (
        release / "components" / "python-interpreter" / "bin" / "python3"
    )
    environment = release / "components" / "python-environment"
    adapter.parent.mkdir(parents=True)
    interpreter.parent.mkdir(parents=True)
    environment.mkdir(parents=True)
    adapter.write_bytes(b"python adapter")
    interpreter.write_bytes(b"signed python interpreter")
    adapter.chmod(0o500)
    interpreter.chmod(0o500)
    adapter_digest = hashlib.sha256(adapter.read_bytes()).hexdigest()
    interpreter_digest = hashlib.sha256(interpreter.read_bytes()).hexdigest()
    environment_digest = "d" * 64
    environment_tree_digest = hashlib.sha256(b"[]\n").hexdigest()
    files: list[dict[str, object]] = []
    for path in sorted(release.rglob("*")):
        relative = path.relative_to(release).as_posix()
        metadata = path.stat(follow_symlinks=False)
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISDIR(metadata.st_mode):
            files.append({"kind": "directory", "mode": mode, "path": relative})
        else:
            files.append(
                {
                    "digest": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "kind": "file",
                    "mode": mode,
                    "path": relative,
                    "size": metadata.st_size,
                }
            )
    root_digest = hashlib.sha256(
        json.dumps(
            files, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()
    (release / ".vonk-generation.json").write_bytes(
        canonical_helper_document(
            {
                "environment_digest": environment_digest,
                "environment_tree_digest": environment_tree_digest,
                "files": files,
                "object_digests": [adapter_digest, interpreter_digest],
                "release_digest": "a" * 64,
                "root_object_digest": root_digest,
                "schema_version": 1,
            }
        ) + b"\n"
    )
    (release / ".vonk-generation.json").chmod(0o444)
    document = request_document()
    document["body"]["invocation"] = {
        **invocation_document("python-venv"),
        "entrypoint": "components/future-adapter/future-adapter",
        "network": {"mode": "none", "egress": []},
        "mounts": [],
        "python_runtime": {
            "environment_component": "python-environment",
            "environment_digest": environment_digest,
            "environment_tree_digest": environment_tree_digest,
            "interpreter_component": "python-interpreter",
            "interpreter_component_digest": interpreter_digest,
            "interpreter_entrypoint": "bin/python3",
            "interpreter_digest": interpreter_digest,
        },
    }
    document["body"]["receipts"] = [
        receipt_document(adapter_digest, adapter.stat().st_size),
        receipt_document(interpreter_digest, interpreter.stat().st_size),
    ]
    request = _request(document)

    class Runner:
        def __init__(self):
            self.calls = []

        def run(self, argv, *, pass_fds, timeout_seconds):
            self.calls.append((argv, pass_fds, timeout_seconds))
            return 0

        def cleanup(self, unit_name):
            pass

    # The launcher still verifies and applies the dedicated workload identity;
    # this fixture keeps the test runnable under root CI as well.
    monkeypatch.setattr("vonk_agent.package_helper.os.fchown", lambda *_: None)
    monkeypatch.setattr("vonk_agent.package_helper.os.fchmod", lambda *_: None)
    runner = Runner()
    launcher = SystemdBackendLauncher(generations, runner=runner)
    result = launcher.launch(
        request,
        SandboxPolicy(os.geteuid() or 64001, os.getegid() or 64001, allowed_devices=("nvidia0",)),
    )

    assert result["status"] == "launched"
    argv, pass_fds, _timeout = runner.calls[0]
    assert "/run/vonk-forge-agent/interpreter" in argv
    assert argv[-4:] == (
        "/run/vonk-forge-agent/interpreter",
        "/run/vonk-forge-agent/entrypoint",
        "serve",
        "--port",
        "8080",
    )[-4:]
    assert "--setenv=PYTHONPATH=/run/vonk-forge-agent/generation/components/python-environment/lib/python/site-packages" in argv
    assert len(pass_fds) == 3


def test_launcher_rejects_restricted_network_before_content_or_side_effects(
    tmp_path: Path,
) -> None:
    class Runner:
        def __init__(self):
            self.calls = []

        def run(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return 0

        def cleanup(self, unit_name):
            self.calls.append(("cleanup", unit_name))

    runner = Runner()
    request = HelperRequest.parse(canonical_helper_document(request_document()))
    launcher = SystemdBackendLauncher(tmp_path / "missing", runner=runner)

    with pytest.raises(HelperProtocolError, match="network-policy boundary"):
        launcher.launch(
            request,
            SandboxPolicy(64001, 64001, allowed_devices=("nvidia0",)),
        )
    assert runner.calls == []


@pytest.mark.parametrize("backend", ["oci"])
def test_launcher_rejects_declared_non_native_backend_before_content(
    tmp_path: Path, backend: str
) -> None:
    class Runner:
        def __init__(self):
            self.calls = []

        def run(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return 0

        def cleanup(self, unit_name):
            self.calls.append(("cleanup", unit_name))

    document = request_document()
    document["body"]["invocation"]["backend"] = backend
    with pytest.raises(HelperProtocolError, match="backend invocation"):
        _request(document)


def test_launcher_cleans_failed_transient_unit(tmp_path: Path) -> None:
    generations = tmp_path / "generations"
    executable = generations / ("a" * 64) / "gen-20260806-a" / "bin" / "future-adapter"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"signed future adapter")
    executable.chmod(0o500)
    digest = __import__("hashlib").sha256(executable.read_bytes()).hexdigest()
    document = request_document()
    document["body"]["invocation"]["network"] = {"mode": "none", "egress": []}
    document["body"]["invocation"]["mounts"] = []
    document["body"]["receipts"] = [
        receipt_document(digest, executable.stat().st_size)
    ]
    request = _request(document)

    class FailingRunner:
        def __init__(self):
            self.cleaned = []

        def run(self, argv, *, pass_fds, timeout_seconds):
            raise HelperProtocolError("package backend launch timed out")

        def cleanup(self, unit_name):
            self.cleaned.append(unit_name)

    runner = FailingRunner()
    if os.geteuid() == 0:
        pytest.skip("snapshot ownership test requires an unprivileged test UID")
    launcher = SystemdBackendLauncher(generations, runner=runner)

    with pytest.raises(HelperProtocolError, match="timed out"):
        launcher.launch(
            request,
            SandboxPolicy(os.geteuid(), os.getegid(), allowed_devices=("nvidia0",)),
        )
    assert runner.cleaned == [f"vonk-workload-{REQUEST_ID}.service"]


def test_helper_cli_requires_exact_systemd_socket_activation(monkeypatch) -> None:
    monkeypatch.delenv("LISTEN_PID", raising=False)
    monkeypatch.delenv("LISTEN_FDS", raising=False)

    with pytest.raises(HelperProtocolError, match="systemd socket activation"):
        main(["--listen-fd=3"])


def test_helper_cli_help_uses_the_canonical_program_name(capsys) -> None:
    with pytest.raises(SystemExit) as exited:
        main(["--help"])

    assert exited.value.code == 0
    assert capsys.readouterr().out.startswith("usage: vonk-forge-package-helper ")


def test_helper_cli_rejects_same_grant_and_receipt_public_key(monkeypatch) -> None:
    import vonk_agent.package_helper as helper_module

    monkeypatch.setenv("LISTEN_PID", str(os.getpid()))
    monkeypatch.setenv("LISTEN_FDS", "1")
    monkeypatch.setattr(helper_module.os, "geteuid", lambda: 0)
    identities = {
        "vonk-agent": SimpleNamespace(pw_uid=64000, pw_gid=64000),
        "vonk-workload": SimpleNamespace(pw_uid=64001, pw_gid=64001),
    }
    monkeypatch.setattr(helper_module.pwd, "getpwnam", identities.__getitem__)

    class ReceiptBoundary:
        @classmethod
        def from_file(cls, _path):
            return SimpleNamespace(public_key_bytes=b"k" * 32)

    class FenceBoundary:
        @classmethod
        def from_file(cls, _key, _replay):
            return SimpleNamespace(public_key_bytes=b"k" * 32)

    monkeypatch.setattr(helper_module, "Ed25519ReceiptVerifier", ReceiptBoundary)
    monkeypatch.setattr(helper_module, "SignedFenceAuthorizer", FenceBoundary)

    with pytest.raises(HelperProtocolError, match="not distinct"):
        main(["--listen-fd=3"])


def test_helper_cli_builds_only_fixed_installed_boundaries_and_fd3(
    monkeypatch,
) -> None:
    import vonk_agent.package_helper as helper_module

    monkeypatch.setenv("LISTEN_PID", str(os.getpid()))
    monkeypatch.setenv("LISTEN_FDS", "1")
    monkeypatch.setenv("VONK_PACKAGE_HELPER_SLOT_SHA256", "d" * 64)
    monkeypatch.setattr(helper_module.os, "geteuid", lambda: 0)
    identities = {
        "vonk-agent": SimpleNamespace(pw_uid=64000, pw_gid=64000),
        "vonk-workload": SimpleNamespace(pw_uid=64001, pw_gid=64001),
    }
    monkeypatch.setattr(helper_module.pwd, "getpwnam", identities.__getitem__)
    seen: dict[str, object] = {}

    class SlotBoundary:
        def __init__(self, digest):
            seen["slot_digest"] = digest

        def verify(self):
            pass

    class ReceiptBoundary:
        @classmethod
        def from_file(cls, path):
            seen["receipt_key"] = path
            return ReceiptVerifier()

    class FenceBoundary:
        @classmethod
        def from_file(cls, key, replay):
            seen["fence_key"] = key
            seen["replay"] = replay
            return FenceAuthorizer()

    class LauncherBoundary:
        def __init__(self, root):
            seen["generation_root"] = root

        def launch(self, request, sandbox):
            raise AssertionError("listener fixture must not launch")

    class Listener:
        family = socket.AF_UNIX
        type = socket.SOCK_STREAM

        def detach(self):
            seen["detached"] = True

    def open_listener(*, fileno):
        seen["listen_fd"] = fileno
        return Listener()

    def serve(helper, listener):
        seen["helper"] = helper
        seen["listener"] = listener

    monkeypatch.setattr(helper_module, "Ed25519ReceiptVerifier", ReceiptBoundary)
    monkeypatch.setattr(helper_module, "SignedFenceAuthorizer", FenceBoundary)
    monkeypatch.setattr(helper_module, "ActiveSlotVerifier", SlotBoundary)
    monkeypatch.setattr(helper_module, "SystemdBackendLauncher", LauncherBoundary)
    monkeypatch.setattr(helper_module.socket, "socket", open_listener)
    monkeypatch.setattr(helper_module, "serve_listener", serve)

    assert main(["--listen-fd=3"]) == 0
    assert seen == {
        "receipt_key": Path("/etc/vonk-forge-agent/package-receipt-public.pem"),
        "fence_key": Path("/etc/vonk-forge-agent/package-fence-public.pem"),
        "slot_digest": "d" * 64,
        "replay": Path("/var/lib/vonk-forge-package-helper/replay.sqlite3"),
        "generation_root": Path("/var/lib/vonk-forge-agent/packages/generations"),
        "listen_fd": 3,
        "helper": seen["helper"],
        "listener": seen["listener"],
        "detached": True,
    }


def test_package_helper_units_define_one_persistent_bounded_authority() -> None:
    root = Path(__file__).parents[1] / "systemd"
    agent_unit = ConfigParser(interpolation=None, strict=False)
    socket_unit = ConfigParser(interpolation=None, strict=True)
    service_unit = ConfigParser(interpolation=None, strict=True)
    assert agent_unit.read(root / "vonk-forge-agent.service")
    assert socket_unit.read(root / "vonk-forge-package-helper.socket")
    assert service_unit.read(root / "vonk-forge-package-helper.service")

    assert socket_unit["Socket"]["Accept"] == "no"
    assert socket_unit["Socket"]["SocketMode"] == "0660"
    assert socket_unit["Socket"]["DirectoryMode"] == "0711"
    assert socket_unit["Socket"]["ListenStream"] == (
        "/run/vonk-forge-package-helper/package-helper.sock"
    )
    assert service_unit["Service"]["Type"] == "simple"
    assert set(service_unit["Unit"]["Requires"].split()) == {
        "vonk-forge-package-helper.socket",
        "vonk-forge-agent-supervisor.service",
    }
    assert service_unit["Service"]["ExecStart"] == (
        "/usr/libexec/vonk-agent-supervisor run-package-helper"
    )
    assert set(service_unit["Unit"]["PartOf"].split()) == {
        "vonk-forge-agent.service",
        "vonk-forge-agent-supervisor.service",
    }
    assert service_unit["Unit"]["After"] == "vonk-forge-agent-supervisor.service"
    assert service_unit["Service"]["NoNewPrivileges"] == "yes"
    assert service_unit["Service"]["PrivateTmp"] == "yes"
    assert agent_unit["Service"]["RuntimeDirectory"] == "vonk-forge-agent"
    assert service_unit["Service"]["RuntimeDirectory"] == (
        "vonk-forge-package-helper"
    )
    assert service_unit["Service"]["RuntimeDirectoryMode"] == "0711"
    assert service_unit["Service"]["RuntimeDirectoryPreserve"] == "yes"
    capabilities = set(service_unit["Service"]["CapabilityBoundingSet"].split())
    assert capabilities == {"CAP_CHOWN"}


def test_launcher_deadline_covers_content_verification_before_systemd(
    tmp_path: Path,
) -> None:
    document = request_document()
    document["body"]["invocation"]["network"] = {"mode": "none", "egress": []}
    document["body"]["invocation"]["mounts"] = []
    request = _request(document)
    ticks = iter((0.0, 61.0))

    class Runner:
        def run(self, *args, **kwargs):
            raise AssertionError("expired content verification must not launch")

        def cleanup(self, unit_name):
            raise AssertionError("no unit exists before content verification")

    launcher = SystemdBackendLauncher(
        tmp_path / "missing",
        runner=Runner(),
        clock=lambda: next(ticks),
    )

    with pytest.raises(HelperProtocolError, match="deadline"):
        launcher.launch(
            request,
            SandboxPolicy(64001, 64001, allowed_devices=("nvidia0",)),
        )
