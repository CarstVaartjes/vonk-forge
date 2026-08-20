from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from dataclasses import asdict
from pathlib import Path

import pytest
from vonk_control import host_backup as host_backup_module
from vonk_control import offline
from vonk_control.host_backup import (
    BackupError,
    BackupReceipt,
    BackupSource,
    HostBackupBoundary,
    RestoreReceipt,
)
from vonk_control.host_commands import (
    ArtifactPolicy,
    ArtifactReceipt,
    CommandPolicy,
    CommandResult,
    HostCommandError,
)
from vonk_control.host_state import SelectedGeneration

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        view = view[written:]


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.age_input = b""
        self.restore_input = b""
        self.fail_age = False
        self.database_revision = "0011_update_rollouts"

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        policy: CommandPolicy,
    ) -> CommandResult:
        self.calls.append({"argv": argv, "cwd": cwd, "env": env, "command": policy})
        return CommandResult(0, (self.database_revision + "\n").encode(), b"", 0.01)

    def stream(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        source_fd: int | None,
        sink_fd: int,
        command: CommandPolicy,
        artifact: ArtifactPolicy,
    ) -> ArtifactReceipt:
        self.calls.append(
            {
                "argv": argv,
                "cwd": cwd,
                "env": env,
                "source_fd": source_fd,
                "command": command,
                "artifact": artifact,
            }
        )
        if argv[0] == "/usr/bin/docker" and source_fd is None:
            content = b"postgres-custom-dump"
        elif argv[0] == "/usr/bin/docker":
            assert source_fd is not None
            chunks = []
            while chunk := os.read(source_fd, 64 * 1024):
                chunks.append(chunk)
            self.restore_input = b"".join(chunks)
            self.database_revision = "0011_update_rollouts"
            content = b""
        elif "--decrypt" in argv:
            assert source_fd is not None
            while os.read(source_fd, 64 * 1024):
                pass
            content = self.age_input
        else:
            assert source_fd is not None
            chunks: list[bytes] = []
            while chunk := os.read(source_fd, 64 * 1024):
                chunks.append(chunk)
            self.age_input = b"".join(chunks)
            content = b"age-v1\n" + hashlib.sha256(self.age_input).digest()
            if self.fail_age:
                _write_all(sink_fd, b"partial")
                raise HostCommandError("nonzero exit")
        _write_all(sink_fd, content)
        os.fsync(sink_fd)
        return ArtifactReceipt(len(content), hashlib.sha256(content).hexdigest())


class ControlCommandRunner(RecordingRunner):
    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        policy: CommandPolicy,
    ) -> CommandResult:
        self.calls.append({"argv": argv, "cwd": cwd, "env": env, "command": policy})
        return CommandResult(0, b"", b"", 0.01)


def _selected(generation_json: bytes) -> SelectedGeneration:
    return SelectedGeneration(
        projection_kind="active",
        operation_id="installed-operation",
        plan_digest=f"sha256:{SHA_A}",
        generation_id="gen-a",
        platform_target_name=f"platform/releases/1.2.0/{SHA_B}.json",
        platform_target_sha256=SHA_B,
        tuf_targets_version=7,
        release_digest=f"sha256:{SHA_B}",
        build_digest=f"sha256:{SHA_C}",
        platform_version="1.2.0",
        deployment_bundle_digest=f"sha256:{SHA_D}",
        api_image=f"ghcr.io/example/api@sha256:{SHA_A}",
        worker_image=f"ghcr.io/example/worker@sha256:{SHA_B}",
        database_revision="0011_update_rollouts",
        previous_generation=None,
        generation_receipt_sha256=hashlib.sha256(generation_json).hexdigest(),
        selection_receipt_sha256=SHA_A,
        projection_sequence=1,
    )


def _boundary(
    tmp_path: Path,
    *,
    runner: RecordingRunner | None = None,
) -> tuple[HostBackupBoundary, RecordingRunner, SelectedGeneration]:
    state_root = tmp_path / "control-host"
    generation = state_root / "generations/gen-a"
    generation.mkdir(parents=True, mode=0o700)
    state_root.chmod(0o700)
    (state_root / "generations").chmod(0o700)
    generation_json = b'{"receipt_kind":"selection","schema_version":1}\n'
    (generation / "generation.json").write_bytes(generation_json)
    (generation / "generation.json").chmod(0o400)
    (generation / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (generation / "compose.yaml").chmod(0o444)
    assets = generation / "assets"
    assets.mkdir(mode=0o700)
    (assets / "Caddyfile").write_text("admin off\n", encoding="utf-8")
    (assets / "Caddyfile").chmod(0o444)

    site = tmp_path / "site"
    site.mkdir(mode=0o700)
    (site / "platform.env").write_text("NAS_LAN_IP=10.0.0.2\n", encoding="utf-8")
    (site / "platform.env").chmod(0o600)
    recipients = tmp_path / "backup-recipients.txt"
    recipients.write_text("age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq\n")
    recipients.chmod(0o400)
    identity = tmp_path / "backup-identity.txt"
    identity.write_text("AGE-SECRET-KEY-1EXAMPLE\n")
    identity.chmod(0o400)
    actual_runner = runner or RecordingRunner()
    boundary = HostBackupBoundary(
        state_root=state_root,
        recipients_file=recipients,
        identity_file=identity,
        site_sources=(BackupSource("site", site),),
        runner=actual_runner,
        command_policy=CommandPolicy(30, 0, 4096),
        artifact_policy=ArtifactPolicy(8 * 1024 * 1024, 0),
    )
    return boundary, actual_runner, _selected(generation_json)


def test_upgrade_backup_uses_fixed_commands_and_canonical_allowlisted_archive(
    tmp_path: Path,
) -> None:
    boundary, runner, generation = _boundary(tmp_path)

    receipt = boundary.create_upgrade_backup(generation, "upgrade-operation")

    generation_path = tmp_path / "control-host/generations/gen-a"
    assert [call["argv"] for call in runner.calls] == [
        (
            "/usr/bin/docker",
            "compose",
            "--file",
            str(generation_path / "compose.yaml"),
            "exec",
            "--no-TTY",
            "postgres",
            "pg_dump",
            "--format=custom",
            "--username=control",
            "--dbname=control",
        ),
        (
            "/usr/bin/age",
            "--encrypt",
            "--recipients-file",
            str(
                tmp_path
                / "control-host/backups/.upgrade-operation.staging/recipients.txt"
            ),
        ),
    ]
    assert all(call["cwd"] == generation_path for call in runner.calls)
    assert runner.calls[1]["env"] == {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
    }
    assert runner.calls[0]["env"].items() >= {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
    }.items()

    with tarfile.open(fileobj=io.BytesIO(runner.age_input), mode="r:") as archive:
        members = archive.getmembers()
        contents = {
            member.name: archive.extractfile(member).read()  # type: ignore[union-attr]
            for member in members
        }
    assert [member.name for member in members] == [
        "database.dump",
        "generation/assets/Caddyfile",
        "generation/compose.yaml",
        "generation/generation.json",
        "site/platform.env",
        "manifest.json",
    ]
    assert all(member.mode == 0o600 and member.mtime == 0 for member in members)
    manifest = json.loads(contents.pop("manifest.json"))
    assert manifest == {
        "files": {
            name: hashlib.sha256(content).hexdigest()
            for name, content in contents.items()
        },
        "format": "vonk-control-backup-v2",
        "generation_id": "gen-a",
        "operation_id": "upgrade-operation",
    }
    output = tmp_path / "control-host" / receipt.relative_path
    assert (
        output.read_bytes() == b"age-v1\n" + hashlib.sha256(runner.age_input).digest()
    )
    assert output.stat().st_mode & 0o777 == 0o600
    assert receipt == BackupReceipt(
        schema_version=1,
        operation_id="upgrade-operation",
        generation_id="gen-a",
        generation_receipt_sha256=generation.generation_receipt_sha256,
        relative_path="backups/upgrade-operation.age",
        byte_count=39,
        sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
        archive_manifest_sha256=hashlib.sha256(
            (
                json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
        ).hexdigest(),
        recipients_sha256=hashlib.sha256(
            (tmp_path / "backup-recipients.txt").read_bytes()
        ).hexdigest(),
    )
    sidecar = output.with_suffix(".receipt.json")
    assert sidecar.read_bytes() == (
        json.dumps(asdict(receipt), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("ascii")
    assert sidecar.stat().st_mode & 0o777 == 0o400
    assert boundary.probe("upgrade-operation") == receipt
    assert boundary.load_exact(generation, "upgrade-operation") == receipt
    assert not list(output.parent.glob(".*.partial"))


def test_backup_compose_uses_canonical_graph_and_exact_generation_environment(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "control-host"
    generation = state_root / "generations/gen-a"
    generation.mkdir(parents=True, mode=0o700)
    state_root.chmod(0o700)
    (state_root / "generations").chmod(0o700)
    generation_json = b'{"receipt_kind":"selection","schema_version":1}\n'
    (generation / "generation.json").write_bytes(generation_json)
    (generation / "generation.json").chmod(0o400)
    (generation / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (generation / "compose.yaml").chmod(0o444)
    recipients = tmp_path / "recipients"
    identity = tmp_path / "identity"
    recipients.write_text("age1test\n", encoding="utf-8")
    identity.write_text("AGE-SECRET-KEY-test\n", encoding="utf-8")
    recipients.chmod(0o400)
    identity.chmod(0o400)
    runner = RecordingRunner()
    boundary = HostBackupBoundary(
        state_root=state_root,
        recipients_file=recipients,
        identity_file=identity,
        runner=runner,
        command_policy=CommandPolicy(30, 0, 4096),
        artifact_policy=ArtifactPolicy(8 * 1024 * 1024, 0),
        compose_environment={"COMPOSE_PROJECT_NAME": "vonk-forge-control"},
        compose_overlays=(),
        control_identity_root=tmp_path / "control-identity",
    )

    boundary.create_upgrade_backup(_selected(generation_json), "upgrade-operation")

    docker_call = runner.calls[0]
    assert docker_call["argv"][:5] == (
        "/usr/bin/docker",
        "compose",
        "--file",
        str(generation / "compose.yaml"),
        "exec",
    )
    assert docker_call["env"]["COMPOSE_PROJECT_NAME"] == "vonk-forge-control"
    assert docker_call["env"]["CONTROL_API_IMAGE"] == _selected(
        generation_json
    ).api_image
    assert docker_call["env"]["VONK_CONTROL_GENERATION_ID"] == "gen-a"
    assert docker_call["env"]["VONK_CONTROL_START_NONCE"] == "0" * 64
    assert docker_call["env"]["CONTROL_IDENTITY_PATH"] == str(
        tmp_path / "control-identity"
    )
def test_backup_failure_removes_every_incomplete_artifact(tmp_path: Path) -> None:
    runner = RecordingRunner()
    runner.fail_age = True
    boundary, _, generation = _boundary(tmp_path, runner=runner)

    with pytest.raises(BackupError, match="backup command failed"):
        boundary.create_upgrade_backup(generation, "upgrade-operation")

    backups = tmp_path / "control-host/backups"
    assert backups.exists()
    assert list(backups.iterdir()) == []


def test_backup_retry_recovers_exact_pair_after_publish_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary, runner, generation = _boundary(tmp_path)
    real_fsync_directory = host_backup_module._fsync_directory
    fsyncs = 0

    class SimulatedCrash(BaseException):
        pass

    def crash_after_artifact_publish(path: Path) -> None:
        nonlocal fsyncs
        real_fsync_directory(path)
        if path.name == "backups":
            fsyncs += 1
            if fsyncs == 2:
                raise SimulatedCrash

    monkeypatch.setattr(
        host_backup_module, "_fsync_directory", crash_after_artifact_publish
    )
    with pytest.raises(SimulatedCrash):
        boundary.create_upgrade_backup(generation, "upgrade-operation")

    backup = tmp_path / "control-host/backups/upgrade-operation.age"
    sidecar = backup.with_suffix(".receipt.json")
    assert backup.is_file()
    assert sidecar.is_file()

    monkeypatch.setattr(host_backup_module, "_fsync_directory", real_fsync_directory)
    runner.calls.clear()
    recovered = boundary.create_upgrade_backup(generation, "upgrade-operation")

    assert recovered == boundary.load_exact(generation, "upgrade-operation")
    assert runner.calls == []


def test_backup_retry_finishes_receipt_first_publication_without_commands(
    tmp_path: Path,
) -> None:
    boundary, runner, generation = _boundary(tmp_path)
    receipt = boundary.create_upgrade_backup(generation, "upgrade-operation")
    backups = tmp_path / "control-host/backups"
    artifact = backups / "upgrade-operation.age"
    staging = backups / ".upgrade-operation.staging"
    staging.mkdir(mode=0o700)
    artifact.rename(staging / "encrypted.partial")
    runner.calls.clear()

    recovered = boundary.create_upgrade_backup(generation, "upgrade-operation")

    assert recovered == receipt
    assert artifact.is_file()
    assert not staging.exists()
    assert runner.calls == []


def test_backup_retry_discards_prepublication_staging_and_rebuilds(
    tmp_path: Path,
) -> None:
    boundary, runner, generation = _boundary(tmp_path)
    staging = tmp_path / "control-host/backups/.upgrade-operation.staging"
    staging.parent.mkdir(mode=0o700)
    staging.mkdir(mode=0o700)
    (staging / "database.dump").write_bytes(b"interrupted")
    (staging / "database.dump").chmod(0o600)

    receipt = boundary.create_upgrade_backup(generation, "upgrade-operation")

    assert receipt.operation_id == "upgrade-operation"
    assert not staging.exists()
    assert len(runner.calls) == 2


def test_backup_retry_replaces_artifact_only_orphan(
    tmp_path: Path,
) -> None:
    boundary, runner, generation = _boundary(tmp_path)
    first = boundary.create_upgrade_backup(generation, "upgrade-operation")
    sidecar = tmp_path / "control-host/backups/upgrade-operation.receipt.json"
    sidecar.unlink()
    runner.calls.clear()

    replacement = boundary.create_upgrade_backup(generation, "upgrade-operation")

    assert replacement == first
    assert sidecar.is_file()
    assert len(runner.calls) == 2


def test_backup_retry_clears_stale_staging_beside_complete_pair(
    tmp_path: Path,
) -> None:
    boundary, runner, generation = _boundary(tmp_path)
    receipt = boundary.create_upgrade_backup(generation, "upgrade-operation")
    staging = tmp_path / "control-host/backups/.upgrade-operation.staging"
    staging.mkdir(mode=0o700)
    (staging / "archive.tar").write_bytes(b"stale")
    (staging / "archive.tar").chmod(0o600)
    runner.calls.clear()

    assert boundary.create_upgrade_backup(generation, "upgrade-operation") == receipt
    assert not staging.exists()
    assert runner.calls == []


def test_backup_cancellation_preserves_receipt_first_staging_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary, runner, generation = _boundary(tmp_path)
    real_fsync_directory = host_backup_module._fsync_directory

    class SimulatedCancellation(BaseException):
        pass

    def cancel_after_sidecar(path: Path) -> None:
        real_fsync_directory(path)
        if path.name == "backups":
            raise SimulatedCancellation

    monkeypatch.setattr(host_backup_module, "_fsync_directory", cancel_after_sidecar)
    with pytest.raises(SimulatedCancellation):
        boundary.create_upgrade_backup(generation, "upgrade-operation")

    staging = tmp_path / "control-host/backups/.upgrade-operation.staging"
    assert (staging / "encrypted.partial").is_file()
    assert (tmp_path / "control-host/backups/upgrade-operation.receipt.json").is_file()

    monkeypatch.setattr(host_backup_module, "_fsync_directory", real_fsync_directory)
    runner.calls.clear()
    recovered = boundary.create_upgrade_backup(generation, "upgrade-operation")
    assert recovered.operation_id == "upgrade-operation"
    assert runner.calls == []


def test_backup_probe_fails_closed_on_partial_or_noncanonical_pair(
    tmp_path: Path,
) -> None:
    boundary, _, generation = _boundary(tmp_path)
    assert boundary.probe("upgrade-operation") is None
    with pytest.raises(BackupError, match="not complete"):
        boundary.load_exact(generation, "upgrade-operation")

    receipt = boundary.create_upgrade_backup(generation, "upgrade-operation")
    sidecar = tmp_path / "control-host/backups/upgrade-operation.receipt.json"
    sidecar.chmod(0o600)
    sidecar.write_bytes(
        json.dumps(asdict(receipt), sort_keys=True, indent=2).encode("ascii") + b"\n"
    )
    sidecar.chmod(0o400)

    with pytest.raises(BackupError, match="canonical"):
        boundary.probe("upgrade-operation")


def test_backup_retry_rejects_changed_generation_or_recipients(
    tmp_path: Path,
) -> None:
    boundary, runner, generation = _boundary(tmp_path)
    boundary.create_upgrade_backup(generation, "upgrade-operation")
    runner.calls.clear()
    recipients = tmp_path / "backup-recipients.txt"
    recipients.chmod(0o600)
    recipients.write_text("age1different\n")
    recipients.chmod(0o400)

    with pytest.raises(BackupError, match="exact inputs"):
        boundary.create_upgrade_backup(generation, "upgrade-operation")

    assert runner.calls == []


def test_load_exact_reopens_generation_receipt(
    tmp_path: Path,
) -> None:
    boundary, _, generation = _boundary(tmp_path)
    boundary.create_upgrade_backup(generation, "upgrade-operation")
    receipt_path = tmp_path / "control-host/generations/gen-a/generation.json"
    receipt_path.chmod(0o600)
    receipt_path.write_text('{"tampered":true}\n')
    receipt_path.chmod(0o400)

    with pytest.raises(BackupError, match="generation receipt"):
        boundary.load_exact(generation, "upgrade-operation")


def test_compensation_restore_uses_fixed_commands_and_is_idempotent(
    tmp_path: Path,
) -> None:
    boundary, runner, generation = _boundary(tmp_path)
    backup = boundary.create_upgrade_backup(generation, "upgrade-operation")
    site = tmp_path / "site/platform.env"
    site.write_text("NAS_LAN_IP=changed\n")
    runner.calls.clear()

    with boundary.verify_for_restore(backup) as verified:
        restored = boundary.restore_for_compensation(
            verified,
            generation,
            "compensate-operation",
        )

    generation_path = tmp_path / "control-host/generations/gen-a"
    assert [call["argv"] for call in runner.calls] == [
        (
            "/usr/bin/age",
            "--decrypt",
            "--identity",
            str(
                tmp_path
                / "control-host/backups/.restore-compensate-operation.staging/identity.txt"
            ),
        ),
        (
            "/usr/bin/docker",
            "compose",
            "--file",
            str(generation_path / "compose.yaml"),
            "exec",
            "--no-TTY",
            "postgres",
            "pg_restore",
            "--clean",
            "--if-exists",
            "--exit-on-error",
            "--single-transaction",
            "--username=control",
            "--dbname=control",
        ),
        (
            "/usr/bin/docker",
            "compose",
            "--file",
            str(generation_path / "compose.yaml"),
            "exec",
            "--no-TTY",
            "postgres",
            "psql",
            "--username=control",
            "--dbname=control",
            "--tuples-only",
            "--no-align",
            "--command",
            "SELECT version_num FROM alembic_version",
        ),
    ]
    assert runner.calls[-1]["command"] == CommandPolicy(30, 256, 4096)
    assert runner.restore_input == b"postgres-custom-dump"
    assert site.read_text() == "NAS_LAN_IP=10.0.0.2\n"
    assert restored == RestoreReceipt(
        schema_version=1,
        operation_id="compensate-operation",
        backup_operation_id="upgrade-operation",
        backup_sha256=backup.sha256,
        backup_byte_count=backup.byte_count,
        generation_id="gen-a",
        generation_receipt_sha256=generation.generation_receipt_sha256,
        database_revision=generation.database_revision,
        archive_manifest_sha256=backup.archive_manifest_sha256,
        identity_sha256=hashlib.sha256(
            (tmp_path / "backup-identity.txt").read_bytes()
        ).hexdigest(),
        site_state_sha256=hashlib.sha256(
            (
                json.dumps(
                    {
                        "site/platform.env": hashlib.sha256(
                            b"NAS_LAN_IP=10.0.0.2\n"
                        ).hexdigest()
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
        ).hexdigest(),
    )
    assert boundary.probe_restore("compensate-operation") == restored

    runner.calls.clear()
    with boundary.verify_for_restore(backup) as verified:
        repeated = boundary.restore_for_compensation(
            verified,
            generation,
            "compensate-operation",
        )
    assert repeated == restored
    assert len(runner.calls) == 1
    assert "psql" in runner.calls[0]["argv"]


def test_compensation_restore_retry_recovers_after_receipt_publish_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary, runner, generation = _boundary(tmp_path)
    backup = boundary.create_upgrade_backup(generation, "upgrade-operation")
    runner.calls.clear()
    real_fsync_directory = host_backup_module._fsync_directory

    class SimulatedCrash(BaseException):
        pass

    def crash_after_receipt_publish(path: Path) -> None:
        real_fsync_directory(path)
        if path.name == "backups":
            raise SimulatedCrash

    monkeypatch.setattr(
        host_backup_module, "_fsync_directory", crash_after_receipt_publish
    )
    with (
        boundary.verify_for_restore(backup) as verified,
        pytest.raises(SimulatedCrash),
    ):
        boundary.restore_for_compensation(
            verified,
            generation,
            "compensate-operation",
        )

    assert runner.restore_input == b"postgres-custom-dump"
    monkeypatch.setattr(host_backup_module, "_fsync_directory", real_fsync_directory)
    runner.calls.clear()
    with boundary.verify_for_restore(backup) as verified:
        recovered = boundary.restore_for_compensation(
            verified,
            generation,
            "compensate-operation",
        )

    assert recovered == boundary.load_restore_exact(
        backup,
        generation,
        "compensate-operation",
    )
    assert len(runner.calls) == 2
    assert all("psql" in call["argv"] for call in runner.calls)


def test_compensation_restore_consumes_verified_inode_after_path_replacement(
    tmp_path: Path,
) -> None:
    boundary, runner, generation = _boundary(tmp_path)
    backup = boundary.create_upgrade_backup(generation, "upgrade-operation")
    artifact = tmp_path / "control-host" / backup.relative_path

    with boundary.verify_for_restore(backup) as verified:
        replacement = artifact.with_suffix(".replacement")
        replacement.write_bytes(b"untrusted replacement")
        replacement.chmod(0o600)
        replacement.replace(artifact)
        runner.calls.clear()
        restored = boundary.restore_for_compensation(
            verified,
            generation,
            "compensate-operation",
        )

    assert restored.backup_sha256 == backup.sha256
    assert runner.restore_input == b"postgres-custom-dump"


def test_compensation_restore_repairs_site_drift_instead_of_trusting_receipt(
    tmp_path: Path,
) -> None:
    boundary, runner, generation = _boundary(tmp_path)
    backup = boundary.create_upgrade_backup(generation, "upgrade-operation")
    with boundary.verify_for_restore(backup) as verified:
        boundary.restore_for_compensation(
            verified,
            generation,
            "compensate-operation",
        )
    site = tmp_path / "site/platform.env"
    site.write_text("drifted\n")
    (tmp_path / "site/unexpected").write_text("extra\n")
    runner.calls.clear()

    with boundary.verify_for_restore(backup) as verified:
        boundary.restore_for_compensation(
            verified,
            generation,
            "compensate-operation",
        )

    assert site.read_text() == "NAS_LAN_IP=10.0.0.2\n"
    assert not (tmp_path / "site/unexpected").exists()
    assert any("pg_restore" in call["argv"] for call in runner.calls)


def test_compensation_restore_discards_interrupted_internal_staging(
    tmp_path: Path,
) -> None:
    boundary, runner, generation = _boundary(tmp_path)
    backup = boundary.create_upgrade_backup(generation, "upgrade-operation")
    staging = tmp_path / "control-host/backups/.restore-compensate-operation.staging"
    staging.mkdir(mode=0o700)
    (staging / "archive.tar").write_bytes(b"interrupted")
    (staging / "archive.tar").chmod(0o600)
    runner.calls.clear()

    with boundary.verify_for_restore(backup) as verified:
        boundary.restore_for_compensation(
            verified,
            generation,
            "compensate-operation",
        )

    assert not staging.exists()
    assert runner.restore_input == b"postgres-custom-dump"


def test_compensation_restore_repairs_database_revision_drift(
    tmp_path: Path,
) -> None:
    boundary, runner, generation = _boundary(tmp_path)
    backup = boundary.create_upgrade_backup(generation, "upgrade-operation")
    with boundary.verify_for_restore(backup) as verified:
        boundary.restore_for_compensation(
            verified,
            generation,
            "compensate-operation",
        )
    runner.database_revision = "target-revision"
    runner.calls.clear()

    with boundary.verify_for_restore(backup) as verified:
        boundary.restore_for_compensation(
            verified,
            generation,
            "compensate-operation",
        )

    assert runner.database_revision == generation.database_revision
    assert any("pg_restore" in call["argv"] for call in runner.calls)


def test_compensation_restore_cleans_external_stage_after_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary, _, generation = _boundary(tmp_path)
    backup = boundary.create_upgrade_backup(generation, "upgrade-operation")
    site = tmp_path / "site/platform.env"
    real_replace = host_backup_module.os.replace
    failed = False

    def fail_site_replace(source: Path, destination: Path) -> None:
        nonlocal failed
        if Path(destination) == site and not failed:
            failed = True
            raise OSError("injected replace failure")
        real_replace(source, destination)

    monkeypatch.setattr(host_backup_module.os, "replace", fail_site_replace)
    with (
        boundary.verify_for_restore(backup) as verified,
        pytest.raises(BackupError, match="compensation restore failed"),
    ):
        boundary.restore_for_compensation(
            verified,
            generation,
            "compensate-operation",
        )
    assert not (tmp_path / "site/.platform.env.restore-compensate-operation").exists()

    monkeypatch.setattr(host_backup_module.os, "replace", real_replace)
    with boundary.verify_for_restore(backup) as verified:
        boundary.restore_for_compensation(
            verified,
            generation,
            "compensate-operation",
        )
    assert site.read_text() == "NAS_LAN_IP=10.0.0.2\n"


def test_restore_archive_is_iterated_without_materializing_all_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary, _, generation = _boundary(tmp_path)
    backup = boundary.create_upgrade_backup(generation, "upgrade-operation")
    monkeypatch.setattr(
        tarfile.TarFile,
        "getmembers",
        lambda _self: (_ for _ in ()).throw(AssertionError("must stream members")),
    )

    with boundary.verify_for_restore(backup) as verified:
        restored = boundary.restore_for_compensation(
            verified,
            generation,
            "compensate-operation",
        )

    assert restored.backup_sha256 == backup.sha256


def test_backup_rejects_untrusted_recipients_and_source_links(tmp_path: Path) -> None:
    boundary, _, generation = _boundary(tmp_path)
    recipients = tmp_path / "backup-recipients.txt"
    recipients.chmod(0o600)
    with pytest.raises(BackupError, match="recipients file"):
        boundary.create_upgrade_backup(generation, "upgrade-operation")

    recipients.chmod(0o400)
    site_file = tmp_path / "site/platform.env"
    outside = tmp_path / "outside"
    outside.write_text("secret\n")
    site_file.unlink()
    site_file.symlink_to(outside)
    with pytest.raises(BackupError, match="symlink"):
        boundary.create_upgrade_backup(generation, "upgrade-operation-2")


@pytest.mark.parametrize(
    "prefixes",
    [
        ("database.dump/child",),
        ("manifest.json/child",),
        ("generation/child",),
        ("site", "site/config"),
        ("site/config", "site"),
    ],
)
def test_backup_rejects_reserved_or_overlapping_source_namespaces(
    tmp_path: Path,
    prefixes: tuple[str, ...],
) -> None:
    source = tmp_path / "source"
    source.write_text("data")

    with pytest.raises(ValueError, match="source prefixes"):
        HostBackupBoundary(
            state_root=tmp_path / "control-host",
            recipients_file=tmp_path / "recipients",
            site_sources=tuple(BackupSource(prefix, source) for prefix in prefixes),
        )


def test_verify_for_restore_requires_exact_owner_only_receipt(tmp_path: Path) -> None:
    boundary, _, generation = _boundary(tmp_path)
    receipt = boundary.create_upgrade_backup(generation, "upgrade-operation")

    with boundary.verify_for_restore(receipt) as verified:
        assert verified.path == tmp_path / "control-host" / receipt.relative_path
        assert verified.byte_count == receipt.byte_count
        assert verified.sha256 == receipt.sha256
        assert (
            os.read(verified.fileno(), receipt.byte_count) == verified.path.read_bytes()
        )

    backup = tmp_path / "control-host" / receipt.relative_path
    backup.write_bytes(backup.read_bytes() + b"tampered")
    with pytest.raises(BackupError, match="receipt"):
        boundary.verify_for_restore(receipt)


def test_verified_backup_keeps_the_verified_inode_open(tmp_path: Path) -> None:
    boundary, _, generation = _boundary(tmp_path)
    receipt = boundary.create_upgrade_backup(generation, "upgrade-operation")
    backup = tmp_path / "control-host" / receipt.relative_path
    original = backup.read_bytes()

    with boundary.verify_for_restore(receipt) as verified:
        replacement = backup.with_suffix(".replacement")
        replacement.write_bytes(b"different")
        replacement.chmod(0o600)
        replacement.replace(backup)
        assert os.read(verified.fileno(), len(original)) == original


def test_backup_rejects_unbound_generation_or_operation_identifier(
    tmp_path: Path,
) -> None:
    boundary, _, generation = _boundary(tmp_path)
    mismatched = SelectedGeneration(
        **{**generation.__dict__, "generation_receipt_sha256": SHA_D}
    )

    with pytest.raises(BackupError, match="generation receipt"):
        boundary.create_upgrade_backup(mismatched, "upgrade-operation")
    with pytest.raises(BackupError, match="operation ID"):
        boundary.create_upgrade_backup(generation, "../escape")


def test_host_upgrade_commands_use_absolute_docker_and_minimal_environment(
    tmp_path: Path,
) -> None:
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {}\n", encoding="utf-8")
    recipients = tmp_path / "recipients"
    recipients.write_text("age1recipient\n")
    recipients.chmod(0o400)
    runner = ControlCommandRunner()
    boundary = offline.HostUpgradeBoundary(
        state_root=tmp_path / "control-host",
        compose_file=compose,
        recipients_file=recipients,
        health_url="https://127.0.0.1/healthz",
        runner=runner,  # type: ignore[arg-type]
    )

    boundary.pull((f"ghcr.io/example/api@sha256:{SHA_A}",))

    assert runner.calls == [
        {
            "argv": (
                "/usr/bin/docker",
                "pull",
                f"ghcr.io/example/api@sha256:{SHA_A}",
            ),
            "cwd": tmp_path,
            "env": {
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": "/usr/bin:/bin",
            },
            "command": CommandPolicy(600, 1024 * 1024, 1024 * 1024),
        }
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        ("upgrade", "--release", "release.json", "--backup-script", "/tmp/script"),
        ("rollback", "--generation", "gen-a", "--backup-script", "/tmp/script"),
        (
            "backup",
            "--database-dump",
            "dump",
            "--output",
            "backup.age",
            "--encrypt-command",
            "sh -c anything",
        ),
        ("inspect-backup", "backup.age", "--decrypt-command", "sh -c anything"),
        (
            "extract-backup",
            "backup.age",
            "restore",
            "--decrypt-command",
            "sh -c anything",
        ),
    ],
)
def test_offline_cli_offers_no_mutable_backup_or_transform_command(
    arguments: tuple[str, ...],
) -> None:
    with pytest.raises(SystemExit) as error:
        offline.main(list(arguments))

    assert error.value.code == 2
