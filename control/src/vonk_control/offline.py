"""Host-local maintenance and encrypted backup primitives."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path, PurePosixPath

from tuf.ngclient import Urllib3Fetcher

from cluster_profiles.deployment_bundle import (
    DeploymentBundleError,
    extract_deployment_bundle,
    verify_deployment_bundle,
)
from cluster_profiles.platform_release import PlatformRelease, PlatformReleaseError
from cluster_profiles.update_trust import UpdateTrust, UpdateTrustError

from .generation_launch import (
    GenerationReleaseIdentity,
    SelectionRuntime,
    selected_compose_environment,
)
from .host_backup import BackupError, HostBackupBoundary
from .host_commands import BoundedCommandRunner, CommandPolicy, HostCommandError
from .host_state import (
    GenerationReceipt,
    HostGenerationStore,
    HostOperationLock,
    HostOperationPlan,
    HostStateConflict,
    SelectedGeneration,
    SelectionReceipt,
)
from .oci_bundle import OciBundleError, OciBundleSource
from .upgrade import (
    AmbiguousMigrationError,
    ControlGenerationPlan,
    ControlUpgrade,
    PhaseObservation,
    ProbeDisposition,
    UpgradeConflict,
    UpgradeError,
    UpgradePhase,
    UpgradeRecoveryRequired,
)

_IDENTIFIER = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_SITE_ENVIRONMENT = 256 * 1024
_MAX_PROBE_OUTPUT = 64 * 1024
_PROBE_POLICY = CommandPolicy(30, _MAX_PROBE_OUTPUT, 16 * 1024)
_SERVICE_POLICY = CommandPolicy(120, 1024 * 1024, 1024 * 1024)
_ENVIRONMENT = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"}
_MAINTENANCE_LOG_SERVICES = frozenset(
    {
        "caddy",
        "control-api",
        "control-worker",
        "grafana",
        "hermes-agent",
        "litellm",
        "postgres",
        "prometheus",
        "registry",
        "step-ca",
        "tailscale-configurator",
        "tailscale-gateway",
    }
)
_MAINTENANCE_ACTIONS = (
    "status",
    "logs",
    "tailscale-status",
    "tailscale-serve-status",
    "tailscale-serve-config",
    "step-ca-health",
)


class OfflineConflict(RuntimeError):
    pass


# These small, content-addressed helpers are retained as the portable
# acceptance boundary used by ``scripts/accept-control-recovery``.  The
# production HostBackupBoundary below adds generation identity, age
# encryption, and host ownership checks; the helpers intentionally expose no
# host paths or command output and are useful for a disposable recovery drill.
def _portable_backup_files(paths: Sequence[Path]) -> list[tuple[str, bytes]]:
    collected: list[tuple[str, bytes]] = []
    for source in paths:
        source = Path(source)
        if source.is_symlink() or not source.exists():
            raise BackupError("backup source is unsafe or missing")
        if source.is_file():
            collected.append((source.name, source.read_bytes()))
            continue
        for child in sorted(source.rglob("*")):
            if child.is_symlink():
                raise BackupError("backup source contains a symlink")
            if child.is_file():
                collected.append(
                    (f"{source.name}/{child.relative_to(source).as_posix()}", child.read_bytes())
                )
    return collected


def _portable_transform(command: Sequence[str], content: bytes, action: str) -> bytes:
    if not command or not all(isinstance(item, str) and item for item in command):
        raise BackupError(f"external {action} command is required")
    try:
        completed = subprocess.run(
            tuple(command), input=content, capture_output=True, check=False, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BackupError(f"external {action} command failed") from error
    if completed.returncode != 0:
        raise BackupError(f"external {action} command failed")
    return completed.stdout


def _portable_archive(entries: Sequence[tuple[str, bytes]]) -> bytes:
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:") as bundle:
        for name, content in entries:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o600
            info.mtime = 0
            bundle.addfile(info, io.BytesIO(content))
    return archive.getvalue()


def create_backup(
    database_dump: Path,
    config_paths: Sequence[Path],
    output: Path,
    *,
    encrypt_command: Sequence[str],
) -> None:
    """Create a deterministic disposable backup through a fixed transform."""
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise BackupError("backup output must be a new path")
    entries = sorted(_portable_backup_files((Path(database_dump), *config_paths)))
    hashes = {name: hashlib.sha256(content).hexdigest() for name, content in entries}
    manifest = json.dumps(
        {"format": "vonk-control-backup-v1", "files": hashes},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii") + b"\n"
    encrypted = _portable_transform(
        encrypt_command,
        _portable_archive([*entries, ("manifest.json", manifest)]),
        "encryption",
    )
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        output, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
    )
    try:
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(encrypted)
            destination.flush()
            os.fsync(destination.fileno())
    except Exception:
        try:
            output.unlink()
        except OSError:
            pass
        raise


def _portable_verified_files(
    backup: Path, decrypt_command: Sequence[str]
) -> tuple[dict[str, object], dict[str, bytes]]:
    decrypted = _portable_transform(decrypt_command, Path(backup).read_bytes(), "decryption")
    try:
        with tarfile.open(fileobj=io.BytesIO(decrypted), mode="r:") as bundle:
            members = bundle.getmembers()
            files: dict[str, bytes] = {}
            for member in members:
                path = PurePosixPath(member.name)
                if (
                    member.issym()
                    or member.islnk()
                    or member.isdir()
                    or path.is_absolute()
                    or ".." in path.parts
                    or not member.isfile()
                    or member.name in files
                ):
                    raise BackupError("backup archive contains an unsafe member")
                source = bundle.extractfile(member)
                if source is None:
                    raise BackupError("backup archive member is unreadable")
                files[member.name] = source.read()
        manifest_raw = files.pop("manifest.json")
        manifest = json.loads(manifest_raw)
        expected = manifest.get("files") if isinstance(manifest, dict) else None
        if manifest.get("format") != "vonk-control-backup-v1" or not isinstance(expected, dict):
            raise BackupError("backup manifest is invalid")
        if set(expected) != set(files) or any(
            not isinstance(name, str)
            or not isinstance(digest, str)
            or digest != hashlib.sha256(files[name]).hexdigest()
            for name, digest in expected.items()
        ):
            raise BackupError("backup checksum verification failed")
        canonical_entries = [*((name, files[name]) for name in sorted(files)), ("manifest.json", manifest_raw)]
        if decrypted != _portable_archive(canonical_entries):
            raise BackupError("backup archive is not canonical or was modified")
        return manifest, files
    except BackupError:
        raise
    except (OSError, tarfile.TarError, TypeError, ValueError, KeyError) as error:
        raise BackupError("backup archive or manifest is unreadable") from error


def inspect_backup(backup: Path, *, decrypt_command: Sequence[str]) -> dict[str, object]:
    return _portable_verified_files(backup, decrypt_command)[0]


def extract_backup(
    backup: Path, destination: Path, *, decrypt_command: Sequence[str]
) -> dict[str, object]:
    manifest, files = _portable_verified_files(backup, decrypt_command)
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise BackupError("restore staging destination must not already exist")
    destination.mkdir(parents=True, mode=0o700)
    try:
        for name, content in files.items():
            target = destination.joinpath(*PurePosixPath(name).parts)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(
                target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return manifest


def _canonical(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise UpgradeConflict("upgrade evidence is not canonical") from error


def _read_stable_file(path: Path, *, label: str, maximum: int) -> bytes:
    path = Path(path)
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
    except OSError as error:
        raise UpgradeConflict(f"{label} is missing or unsafe") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(before.st_mode) & 0o022
            or not 1 <= before.st_size <= maximum
        ):
            raise UpgradeConflict(f"{label} is unsafe")
        raw = bytearray()
        while len(raw) <= maximum:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
        identity = lambda value: (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )
        if len(raw) != before.st_size or identity(before) != identity(after):
            raise UpgradeConflict(f"{label} changed while being read")
        return bytes(raw)
    except OSError as error:
        raise UpgradeConflict(f"{label} cannot be read safely") from error
    finally:
        os.close(descriptor)


def _parse_site_environment(raw: bytes) -> dict[str, str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise UpgradeConflict("site configuration is invalid") from error
    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if (
            not separator
            or re.fullmatch(r"[A-Z][A-Z0-9_]{0,126}", name) is None
            or name in values
            or "\x00" in value
            or "\n" in value
            or len(value) > 8192
        ):
            raise UpgradeConflict("site configuration is invalid")
        values[name] = value
    if not values:
        raise UpgradeConflict("site configuration is invalid")
    return values


def _parse_compose_ps(raw: bytes) -> dict[str, dict[str, str]]:
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UpgradeConflict("running container probe is invalid") from error
    items = parsed if isinstance(parsed, list) else [parsed]
    result: dict[str, dict[str, str]] = {}
    for item in items:
        if not isinstance(item, dict):
            raise UpgradeConflict("running container probe is invalid")
        service = item.get("Service")
        container_id = item.get("ID")
        image = item.get("Image")
        state = item.get("State")
        if (
            service not in {"control-api", "control-worker"}
            or service in result
            or not isinstance(container_id, str)
            or _CONTAINER_ID.fullmatch(container_id) is None
            or not isinstance(image, str)
            or not isinstance(state, str)
        ):
            raise UpgradeConflict("running container probe is invalid")
        result[service] = {
            "container_id": container_id,
            "image": image,
            "status": "running" if state.lower() == "running" else "stopped",
        }
    return result


def _parse_container_environment(raw: bytes) -> dict[str, str]:
    try:
        values = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UpgradeConflict("container identity probe is invalid") from error
    if not isinstance(values, list) or len(values) > 512:
        raise UpgradeConflict("container identity probe is invalid")
    allowed = {
        "VONK_CONTROL_GENERATION_ID",
        "VONK_CONTROL_PROCESS_IMAGE",
        "VONK_CONTROL_START_NONCE",
        "VONK_DATABASE_REVISION",
        "VONK_PLATFORM_BUILD_DIGEST",
        "VONK_PLATFORM_RELEASE_DIGEST",
        "VONK_PLATFORM_VERSION",
    }
    result: dict[str, str] = {}
    for item in values:
        if not isinstance(item, str):
            raise UpgradeConflict("container identity probe is invalid")
        name, separator, value = item.partition("=")
        if separator and name in allowed:
            if name in result:
                raise UpgradeConflict("container identity probe is invalid")
            result[name] = value
    if set(result) != allowed:
        raise UpgradeConflict("container identity probe is incomplete")
    return result


def _selected_matches_plan(
    generation: SelectedGeneration, plan: ControlGenerationPlan
) -> bool:
    # A rollback plan carries the current refreshed TUF targets version while the
    # immutable predecessor receipt retains the snapshot version at its commit.
    return (
        generation.generation_id == plan.generation_id
        and generation.platform_target_name == plan.platform_target_name
        and generation.platform_target_sha256 == plan.platform_target_sha256
        and generation.release_digest == plan.release_digest
        and generation.build_digest == plan.build_digest
        and generation.platform_version == plan.platform_version
        and generation.deployment_bundle_digest == plan.deployment_bundle_digest
        and generation.api_image == plan.api_image
        and generation.worker_image == plan.worker_image
        and generation.database_revision == plan.database_revision
    )


def _write_new_file(path: Path, content: bytes, *, mode: int) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        mode,
    )
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("upgrade receipt write was incomplete")
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_private_directory(path: Path, label: str) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as error:
        raise UpgradeConflict(f"{label} cannot be created safely") from error
    try:
        metadata = path.lstat()
    except OSError as error:
        raise UpgradeConflict(f"{label} is unsafe") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise UpgradeConflict(f"{label} is unsafe")


def _bundle_image_references(
    raw: bytes, plan: ControlGenerationPlan
) -> tuple[str, ...]:
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            member = archive.getmember("images.lock.json")
            source = archive.extractfile(member)
            if source is None or member.size > 1024 * 1024:
                raise ValueError("images lock")
            document = json.loads(source.read(1024 * 1024 + 1))
    except (
        KeyError,
        OSError,
        tarfile.TarError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        raise UpgradeConflict("deployment image lock is invalid") from error
    images = document.get("images") if isinstance(document, dict) else None
    if not isinstance(images, dict) or not images:
        raise UpgradeConflict("deployment image lock is invalid")
    references = {plan.api_image, plan.worker_image}
    for name, reference in images.items():
        if (
            not isinstance(name, str)
            or _IDENTIFIER.fullmatch(name) is None
            or not isinstance(reference, str)
            or re.fullmatch(r"[^\s]{1,1900}@sha256:[0-9a-f]{64}", reference) is None
        ):
            raise UpgradeConflict("deployment image lock is invalid")
        references.add(reference)
    return tuple(sorted(references))


class HostUpgradeBoundary:
    """Fixed-argv Docker/Compose boundary for a NAS control-host upgrade."""

    def __init__(
        self,
        *,
        state_root: Path,
        compose_file: Path,
        recipients_file: Path,
        identity_file: Path | None = None,
        site_environment_file: Path | None = None,
        health_url: str,
        runner: BoundedCommandRunner | None = None,
        backup_boundary: HostBackupBoundary | None = None,
        generation_store: HostGenerationStore | None = None,
        bundle_source: OciBundleSource | None = None,
    ) -> None:
        self._state_root = Path(state_root)
        self._compose_file = Path(compose_file)
        self._site_environment_file = (
            Path(site_environment_file)
            if site_environment_file is not None
            else self._compose_file.parent / ".env"
        )
        self._health_url = health_url
        self._runner = runner or BoundedCommandRunner()
        self._command_policy = CommandPolicy(600, 1024 * 1024, 1024 * 1024)
        self._generation_store = generation_store or HostGenerationStore(
            self._state_root,
            self._state_root.parent / "control-identity",
        )
        self._bundle_source = bundle_source
        self._backup_boundary = backup_boundary or HostBackupBoundary(
            state_root=self._state_root,
            recipients_file=Path(recipients_file),
            identity_file=None if identity_file is None else Path(identity_file),
            compose_environment=self._site_environment_if_available(),
            control_identity_root=self._generation_store.identity_root,
            runner=self._runner,
        )
        self._environment: dict[str, str] = {}
        active = self._generation_store.load_active()
        if (
            active is not None
            and (self._compose_file.is_symlink() or not self._compose_file.is_file())
        ):
            raise BackupError("Compose file is unsafe or missing")

    def database_revision(self) -> str:
        """Return the exact Alembic revision through the selected generation."""

        selected = self._generation_store.load_active()
        if selected is None:
            return "uninitialized"
        result = self._run_at_generation(
            selected,
            (
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
            policy=_PROBE_POLICY,
        )
        try:
            revision = result.decode("ascii").strip()
        except UnicodeDecodeError as error:
            raise UpgradeConflict("database revision probe was invalid") from error
        if _IDENTIFIER.fullmatch(revision) is None:
            raise UpgradeConflict("database revision probe was invalid")
        return revision

    def site_configuration_digest(self) -> str:
        """Hash stable root-controlled Compose inputs without returning them."""

        raw = _read_stable_file(
            self._site_environment_file,
            label="site configuration",
            maximum=_MAX_SITE_ENVIRONMENT,
        )
        values = _parse_site_environment(raw)
        file_digests: dict[str, dict[str, str]] = {}
        for name, value in values.items():
            if not name.endswith("_FILE"):
                continue
            path = Path(value)
            if not path.is_absolute():
                raise UpgradeConflict("site file reference is not absolute")
            content = _read_stable_file(
                path,
                label=f"site file {name}",
                maximum=_MAX_SITE_ENVIRONMENT,
            )
            file_digests[name] = {
                "path": str(path),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        if not file_digests:
            return "sha256:" + hashlib.sha256(raw).hexdigest()
        return "sha256:" + hashlib.sha256(
            _canonical(
                {
                    "environment_sha256": hashlib.sha256(raw).hexdigest(),
                    "file_digests": file_digests,
                }
            )
        ).hexdigest()

    def running_control_identities(self) -> dict[str, object]:
        """Return an allowlisted identity view of selected API/worker containers."""

        selected = self._generation_store.load_active()
        if selected is None:
            return {
                service: {"status": "absent"}
                for service in ("control-api", "control-worker")
            }
        raw = self._run_at_generation(
            selected,
            ("ps", "--format", "json", "control-api", "control-worker"),
            policy=_PROBE_POLICY,
        )
        containers = _parse_compose_ps(raw)
        identities: dict[str, object] = {}
        for service in ("control-api", "control-worker"):
            item = containers.get(service)
            if item is None:
                identities[service] = {"status": "absent"}
                continue
            container_id = item["container_id"]
            env_raw = self._run(
                (
                    "/usr/bin/docker",
                    "inspect",
                    "--type",
                    "container",
                    "--format",
                    "{{json .Config.Env}}",
                    container_id,
                ),
                policy=_PROBE_POLICY,
            )
            environment = _parse_container_environment(env_raw)
            expected_image = (
                selected.api_image
                if service == "control-api"
                else selected.worker_image
            )
            identity = {
                "build_digest": environment.get("VONK_PLATFORM_BUILD_DIGEST"),
                "database_revision": environment.get("VONK_DATABASE_REVISION"),
                "generation_id": environment.get("VONK_CONTROL_GENERATION_ID"),
                "image": environment.get("VONK_CONTROL_PROCESS_IMAGE"),
                "platform_version": environment.get("VONK_PLATFORM_VERSION"),
                "release_digest": environment.get("VONK_PLATFORM_RELEASE_DIGEST"),
                "start_nonce": environment.get("VONK_CONTROL_START_NONCE"),
                "status": item["status"],
            }
            if (
                item["image"] != expected_image
                or identity
                != {
                    "build_digest": selected.build_digest,
                    "database_revision": selected.database_revision,
                    "generation_id": selected.generation_id,
                    "image": expected_image,
                    "platform_version": selected.platform_version,
                    "release_digest": selected.release_digest,
                    "start_nonce": identity["start_nonce"],
                    "status": "running",
                }
                or not isinstance(identity["start_nonce"], str)
                or re.fullmatch(r"[0-9a-f]{64}", identity["start_nonce"]) is None
            ):
                raise UpgradeConflict("running control identity is inconsistent")
            identities[service] = identity
        return identities

    def probe_phase(
        self, phase: UpgradePhase, plan: ControlGenerationPlan
    ) -> PhaseObservation:
        """Observe one exact durable effect without mutating host state."""

        try:
            return self._probe_phase(phase, plan)
        except AmbiguousMigrationError:
            return PhaseObservation(ProbeDisposition.AMBIGUOUS, {})
        except (
            BackupError,
            HostStateConflict,
            HostCommandError,
            UpgradeError,
            OSError,
        ):
            return PhaseObservation(ProbeDisposition.CONFLICT, {})

    def _probe_phase(
        self, phase: UpgradePhase, plan: ControlGenerationPlan
    ) -> PhaseObservation:
        if phase is UpgradePhase.BUNDLE_IMAGES_ACQUIRED:
            bundle_root = self._bundle_directory(plan)
            if not bundle_root.exists() and not bundle_root.is_symlink():
                return PhaseObservation(ProbeDisposition.ABSENT, {})
            raw, receipt = self._load_bundle(plan)
            verified = verify_deployment_bundle(raw, plan.deployment_bundle)
            if (
                receipt.get("archive_sha256") != verified.archive_sha256
                or receipt.get("manifest_sha256") != verified.manifest_sha256
                or receipt.get("layer_digest") != plan.deployment_bundle_digest
            ):
                return PhaseObservation(ProbeDisposition.CONFLICT, {})
            references = _bundle_image_references(raw, plan)
            if receipt.get("images") != list(references):
                return PhaseObservation(ProbeDisposition.CONFLICT, {})
            if not all(self._image_is_available(reference) for reference in references):
                return PhaseObservation(ProbeDisposition.PARTIAL, {})
            return PhaseObservation(
                ProbeDisposition.EXACT,
                {
                    "bundle_archive_sha256": verified.archive_sha256,
                    "bundle_manifest_sha256": verified.manifest_sha256,
                    "deployment_bundle_digest": plan.deployment_bundle_digest,
                    "images_sha256": hashlib.sha256(
                        _canonical(list(references))
                    ).hexdigest(),
                },
            )
        if phase is UpgradePhase.GENERATION_STAGED:
            staging = self._staging_path(plan)
            receipt = self._load_phase_receipt(staging / "staging.json")
            if receipt is None:
                return PhaseObservation(ProbeDisposition.ABSENT, {})
            expected = {
                "bundle_digest": plan.deployment_bundle_digest,
                "generation_id": plan.generation_id,
                "plan_digest": plan.plan_digest,
                "rendered_compose_sha256": receipt.get("rendered_compose_sha256"),
                "schema_version": 1,
            }
            rendered_digest = expected["rendered_compose_sha256"]
            if (
                receipt != expected
                or not isinstance(rendered_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", rendered_digest) is None
            ):
                return PhaseObservation(ProbeDisposition.CONFLICT, {})
            rendered = _read_stable_file(
                staging / "compose.rendered.yaml",
                label="rendered Compose model",
                maximum=4 * 1024 * 1024,
            )
            if hashlib.sha256(rendered).hexdigest() != rendered_digest:
                return PhaseObservation(ProbeDisposition.CONFLICT, {})
            return PhaseObservation(ProbeDisposition.EXACT, receipt)
        if phase is UpgradePhase.BACKUP_COMPLETED:
            if plan.previous_generation is None:
                return PhaseObservation(
                    ProbeDisposition.EXACT,
                    {"backup": "skipped", "reason": "first-install"},
                )
            receipt = self._backup_boundary.probe(plan.operation_id)
            if receipt is None:
                return PhaseObservation(ProbeDisposition.ABSENT, {})
            if (
                receipt.operation_id != plan.operation_id
                or receipt.generation_id != plan.previous_generation
                or receipt.generation_receipt_sha256
                != plan.current_generation_receipt_sha256
            ):
                return PhaseObservation(ProbeDisposition.CONFLICT, {})
            return PhaseObservation(ProbeDisposition.EXACT, receipt.document())
        if phase is UpgradePhase.SERVICES_STOPPED_DATABASE_MIGRATED:
            if plan.previous_generation is None:
                receipt = self._load_phase_receipt(
                    self._staging_path(plan) / "migration.json"
                )
                if receipt == {
                    "database_revision": plan.database_revision,
                    "schema_version": 1,
                }:
                    return PhaseObservation(ProbeDisposition.EXACT, receipt)
                return PhaseObservation(ProbeDisposition.ABSENT, {})
            revision = self.database_revision()
            identities = self.running_control_identities()
            stopped = all(
                isinstance(identities.get(service), Mapping)
                and identities[service].get("status") == "absent"  # type: ignore[index]
                for service in ("control-api", "control-worker")
            )
            if revision == plan.database_revision and stopped:
                return PhaseObservation(
                    ProbeDisposition.EXACT,
                    {"database_revision": revision, "services": "stopped"},
                )
            if revision not in {plan.current_database_revision, plan.database_revision}:
                return PhaseObservation(ProbeDisposition.AMBIGUOUS, {})
            return PhaseObservation(ProbeDisposition.PARTIAL, {})
        if phase is UpgradePhase.CANDIDATE_READY:
            try:
                candidate = self._generation_store.load_candidate(plan.operation_id)
            except HostStateConflict:
                return PhaseObservation(ProbeDisposition.ABSENT, {})
            if candidate != self._host_plan(plan):
                # CandidateProjection and HostOperationPlan are different types;
                # compare their canonical public identity documents instead.
                candidate_values = getattr(candidate, "__dict__", {})
                expected_values = self._host_plan(plan).__dict__
                if any(
                    candidate_values.get(key) != value
                    for key, value in expected_values.items()
                ):
                    return PhaseObservation(ProbeDisposition.CONFLICT, {})
            try:
                readiness = self._readiness_probe(
                    self._candidate_container_name(plan.operation_id),
                    self._selected_from_plan(plan),
                    mode="preselection",
                    start_nonce=plan.start_nonce,
                    operation_id=plan.operation_id,
                )
            except BackupError:
                return PhaseObservation(ProbeDisposition.PARTIAL, {})
            return PhaseObservation(ProbeDisposition.EXACT, readiness)
        if phase is UpgradePhase.GENERATION_COMMITTED:
            final = self._state_root / "generations" / plan.generation_id
            if not final.exists() and not final.is_symlink():
                staged = self._probe_phase(UpgradePhase.GENERATION_STAGED, plan)
                if staged.disposition is ProbeDisposition.EXACT:
                    return PhaseObservation(ProbeDisposition.ABSENT, {})
                return PhaseObservation(ProbeDisposition.CONFLICT, {})
            generation = self._selected_for_generation(plan.generation_id)
            if not _selected_matches_plan(generation, plan):
                return PhaseObservation(ProbeDisposition.CONFLICT, {})
            return PhaseObservation(
                ProbeDisposition.EXACT,
                {
                    "generation_id": generation.generation_id,
                    "generation_receipt_sha256": generation.generation_receipt_sha256,
                },
            )
        if phase is UpgradePhase.BACKUP_RESTORED:
            backup = self._backup_boundary.probe(plan.operation_id)
            if backup is None or plan.previous_generation is None:
                return PhaseObservation(ProbeDisposition.ABSENT, {})
            predecessor = self._selected_for_generation(plan.previous_generation)
            if self._backup_boundary.probe_restore(plan.operation_id) is None:
                return PhaseObservation(ProbeDisposition.ABSENT, {})
            restored = self._backup_boundary.load_restore_exact(
                backup, predecessor, plan.operation_id
            )
            return PhaseObservation(ProbeDisposition.EXACT, restored.document())
        if phase is UpgradePhase.PREDECESSOR_VERIFIED:
            generation = self._selected_for_generation(plan.generation_id)
            if not _selected_matches_plan(generation, plan):
                return PhaseObservation(ProbeDisposition.CONFLICT, {})
            return PhaseObservation(
                ProbeDisposition.EXACT,
                {
                    "generation_id": generation.generation_id,
                    "generation_receipt_sha256": generation.generation_receipt_sha256,
                },
            )
        if phase in {
            UpgradePhase.GENERATION_SELECTED,
            UpgradePhase.PREDECESSOR_SELECTED,
        }:
            expected = (
                plan.previous_generation
                if phase is UpgradePhase.PREDECESSOR_SELECTED
                else plan.generation_id
            )
            active = self._generation_store.load_active()
            if active is None:
                return PhaseObservation(ProbeDisposition.ABSENT, {})
            if active.generation_id != expected:
                return PhaseObservation(ProbeDisposition.CONFLICT, {})
            if phase is UpgradePhase.GENERATION_SELECTED:
                exact_selection = (
                    _selected_matches_plan(active, plan)
                    and active.operation_id == plan.operation_id
                    and active.plan_digest == plan.plan_digest
                    and active.previous_generation == plan.previous_generation
                )
            else:
                original_predecessor = (
                    active.generation_receipt_sha256
                    == plan.current_generation_receipt_sha256
                    and active.selection_receipt_sha256
                    == plan.current_selection_receipt_sha256
                    and active.projection_sequence == plan.current_projection_sequence
                )
                compensated_selection = (
                    active.operation_id == plan.operation_id
                    and active.plan_digest == plan.plan_digest
                    and active.previous_generation == plan.generation_id
                )
                exact_selection = original_predecessor or compensated_selection
            if not exact_selection:
                return PhaseObservation(ProbeDisposition.CONFLICT, {})
            return PhaseObservation(
                ProbeDisposition.EXACT,
                {
                    "generation_id": active.generation_id,
                    "selection_receipt_sha256": active.selection_receipt_sha256,
                    "projection_sequence": active.projection_sequence,
                },
            )
        if phase in {
            UpgradePhase.SERVICES_STARTED,
            UpgradePhase.PREDECESSOR_SERVICES_STARTED,
        }:
            identities = self.running_control_identities()
            if any(
                not isinstance(identities.get(service), Mapping)
                or identities[service].get("status") != "running"  # type: ignore[index]
                for service in ("control-api", "control-worker")
            ):
                return PhaseObservation(ProbeDisposition.ABSENT, {})
            return PhaseObservation(
                ProbeDisposition.EXACT,
                {
                    "running_identities_sha256": "sha256:"
                    + hashlib.sha256(_canonical(identities)).hexdigest()
                },
            )
        if phase in {
            UpgradePhase.WORKER_READY,
            UpgradePhase.PREDECESSOR_WORKER_READY,
            UpgradePhase.COMPLETED,
            UpgradePhase.ROLLED_BACK,
            UpgradePhase.FAILED,
        }:
            active = self._generation_store.load_active()
            expected_generation = (
                plan.previous_generation
                if phase
                in {
                    UpgradePhase.PREDECESSOR_WORKER_READY,
                    UpgradePhase.FAILED,
                }
                else plan.generation_id
            )
            if active is None:
                return PhaseObservation(ProbeDisposition.ABSENT, {})
            if active.generation_id != expected_generation:
                return PhaseObservation(ProbeDisposition.CONFLICT, {})
            nonce = self._load_selected_runtime_nonce(
                plan.operation_id, active.generation_id
            )
            if nonce is None:
                return PhaseObservation(ProbeDisposition.ABSENT, {})
            try:
                readiness = self._readiness_probe(
                    self._selected_api_container(active),
                    active,
                    mode="selected",
                    start_nonce=nonce,
                    operation_id=None,
                )
            except BackupError:
                return PhaseObservation(ProbeDisposition.PARTIAL, {})
            if phase in {
                UpgradePhase.COMPLETED,
                UpgradePhase.ROLLED_BACK,
                UpgradePhase.FAILED,
            } and not self._candidate_is_clean(plan.operation_id):
                return PhaseObservation(ProbeDisposition.PARTIAL, {})
            return PhaseObservation(ProbeDisposition.EXACT, readiness)
        if phase in {
            UpgradePhase.SERVICES_STOPPED,
            UpgradePhase.COMPENSATION_SERVICES_STOPPED,
        }:
            identities = self.running_control_identities()
            if all(
                isinstance(identities.get(service), Mapping)
                and identities[service].get("status") == "absent"  # type: ignore[index]
                for service in ("control-api", "control-worker")
            ):
                return PhaseObservation(ProbeDisposition.EXACT, {"services": "stopped"})
            return PhaseObservation(ProbeDisposition.PARTIAL, {})
        if phase is UpgradePhase.CANDIDATE_CLEANED:
            if self._candidate_is_clean(plan.operation_id):
                return PhaseObservation(ProbeDisposition.EXACT, {"candidate": "absent"})
            return PhaseObservation(ProbeDisposition.PARTIAL, {})
        # Acquisition, staging, backup, migration, readiness, commit and restore
        # each gain their exact receipts in the next focused TDD slice.
        return PhaseObservation(ProbeDisposition.ABSENT, {})

    def perform_phase(self, phase: UpgradePhase, plan: ControlGenerationPlan) -> None:
        """Perform one fixed phase; arbitrary argv is never accepted."""

        try:
            self._perform_phase(phase, plan)
        except (AmbiguousMigrationError, UpgradeConflict, UpgradeError):
            raise
        except HostStateConflict as error:
            raise UpgradeConflict("upgrade host state changed") from error
        except (
            BackupError,
            DeploymentBundleError,
            HostCommandError,
            OciBundleError,
            OSError,
        ) as error:
            raise UpgradeError("upgrade phase command failed safely") from error

    def _perform_phase(self, phase: UpgradePhase, plan: ControlGenerationPlan) -> None:

        if phase is UpgradePhase.BUNDLE_IMAGES_ACQUIRED:
            self._acquire_bundle(plan)
            return
        if phase is UpgradePhase.GENERATION_STAGED:
            self._stage_generation(plan)
            return
        if phase is UpgradePhase.BACKUP_COMPLETED:
            active = self._generation_store.load_active()
            if active is None and plan.previous_generation is None:
                return
            if active is None:
                raise UpgradeConflict("backup predecessor selection changed")
            if active.generation_id != plan.previous_generation:
                raise UpgradeConflict("backup predecessor selection changed")
            self._backup_boundary.create_upgrade_backup(active, plan.operation_id)
            return
        if phase is UpgradePhase.SERVICES_STOPPED_DATABASE_MIGRATED:
            active = self._generation_store.load_active()
            if active is None:
                revision = "uninitialized"
                candidate = self._selected_from_plan(plan)
                migration = self._load_phase_receipt(
                    self._staging_path(plan) / "migration.json"
                )
                if migration == {
                    "database_revision": plan.database_revision,
                    "schema_version": 1,
                }:
                    return
                self._run_at_staging(
                    plan,
                    ("up", "-d", "postgres"),
                    environment=self._compose_environment(
                        candidate,
                        start_nonce=plan.start_nonce,
                        mode="preselection",
                        operation_id=plan.operation_id,
                    ),
                    policy=_SERVICE_POLICY,
                )
            else:
                self._run_at_generation(
                    active,
                    ("stop", "control-worker", "control-api"),
                    policy=_SERVICE_POLICY,
                )
                revision = self.database_revision()
                if revision == plan.database_revision:
                    return
                if revision != plan.current_database_revision:
                    raise AmbiguousMigrationError(
                        "database is neither at predecessor nor target revision"
                    )
            candidate = self._selected_from_plan(plan)
            try:
                self._run_at_staging(
                    plan,
                    (
                        "run",
                        "--rm",
                        "--no-deps",
                        "control-api",
                        "python",
                        "-m",
                        "alembic",
                        "upgrade",
                        plan.database_revision,
                    ),
                    environment=self._compose_environment(
                        candidate,
                        start_nonce=plan.start_nonce,
                        mode="preselection",
                        operation_id=plan.operation_id,
                    ),
                    policy=CommandPolicy(600, 1024 * 1024, 1024 * 1024),
                )
            except BackupError as error:
                raise AmbiguousMigrationError(
                    "database migration outcome is unknown"
                ) from error
            _write_new_file(
                self._staging_path(plan) / "migration.json",
                _canonical(
                    {
                        "database_revision": plan.database_revision,
                        "schema_version": 1,
                    }
                ),
                mode=0o400,
            )
            return
        if phase is UpgradePhase.CANDIDATE_READY:
            self._generation_store.project_candidate(self._host_plan(plan))
            candidate = self._selected_from_plan(plan)
            name = self._candidate_container_name(plan.operation_id)
            self._remove_container(name)
            environment = self._compose_environment(
                candidate,
                start_nonce=plan.start_nonce,
                mode="preselection",
                operation_id=plan.operation_id,
            )
            self._run_at_staging(
                plan,
                (
                    "run",
                    "-d",
                    "--no-deps",
                    "--name",
                    name,
                    "--env",
                    "VONK_CONTROL_STARTUP_MODE=preselection",
                    "--env",
                    f"VONK_CONTROL_OPERATION_ID={plan.operation_id}",
                    "--env",
                    f"VONK_CONTROL_START_NONCE={plan.start_nonce}",
                    "--env",
                    f"VONK_CONTROL_PROCESS_IMAGE={plan.api_image}",
                    "control-api",
                ),
                environment=environment,
                policy=_SERVICE_POLICY,
            )
            self._wait_for_readiness(
                name,
                candidate,
                mode="preselection",
                start_nonce=plan.start_nonce,
                operation_id=plan.operation_id,
            )
            return
        if phase is UpgradePhase.GENERATION_COMMITTED:
            self._generation_store.commit_generation(
                self._staging_path(plan),
                self._host_plan(plan).generation_receipt(),
            )
            return
        if phase in {
            UpgradePhase.GENERATION_SELECTED,
            UpgradePhase.PREDECESSOR_SELECTED,
        }:
            generation_id = (
                plan.previous_generation
                if phase is UpgradePhase.PREDECESSOR_SELECTED
                else plan.generation_id
            )
            if generation_id is None:
                raise UpgradeConflict("selection generation is unavailable")
            receipt = self._generation_store.load_generation(generation_id)
            previous = self._generation_store.load_active()
            selection = SelectionReceipt.for_generation(
                receipt,
                operation_id=plan.operation_id,
                plan_digest=plan.plan_digest,
                previous_generation=(
                    previous.generation_id if previous is not None else None
                ),
            )
            self._generation_store.select(selection)
            return
        if phase is UpgradePhase.BACKUP_RESTORED:
            if plan.previous_generation is None:
                raise UpgradeConflict("compensation predecessor is unavailable")
            backup = self._backup_boundary.probe(plan.operation_id)
            if backup is None:
                raise UpgradeError("compensation backup is unavailable")
            predecessor = self._selected_for_generation(plan.previous_generation)
            with self._backup_boundary.verify_for_restore(backup) as verified:
                self._backup_boundary.restore_for_compensation(
                    verified, predecessor, plan.operation_id
                )
            return
        if phase in {
            UpgradePhase.SERVICES_STARTED,
            UpgradePhase.PREDECESSOR_SERVICES_STARTED,
        }:
            generation_id = (
                plan.previous_generation
                if phase is UpgradePhase.PREDECESSOR_SERVICES_STARTED
                else plan.generation_id
            )
            if generation_id is None:
                raise UpgradeConflict("predecessor generation is unavailable")
            generation = self._selected_for_generation(generation_id)
            nonce = self._selected_runtime_nonce(plan.operation_id, generation_id)
            self._run_at_generation(
                generation,
                ("up", "-d", "--remove-orphans"),
                start_nonce=nonce,
                policy=_SERVICE_POLICY,
            )
            return
        if phase in {
            UpgradePhase.SERVICES_STOPPED,
            UpgradePhase.COMPENSATION_SERVICES_STOPPED,
        }:
            active = self._generation_store.load_active()
            if active is not None:
                self._run_at_generation(
                    active,
                    ("stop", "control-worker", "control-api"),
                    policy=_SERVICE_POLICY,
                )
            return
        if phase is UpgradePhase.CANDIDATE_CLEANED:
            self._remove_container(self._candidate_container_name(plan.operation_id))
            self._generation_store.remove_candidate(plan.operation_id)
            return
        if phase in {
            UpgradePhase.PREDECESSOR_VERIFIED,
            UpgradePhase.WORKER_READY,
            UpgradePhase.PREDECESSOR_WORKER_READY,
            UpgradePhase.ROLLED_BACK,
            UpgradePhase.FAILED,
        }:
            if phase in {
                UpgradePhase.WORKER_READY,
                UpgradePhase.PREDECESSOR_WORKER_READY,
            }:
                active = self._require_active()
                nonce = self._selected_runtime_nonce(
                    plan.operation_id, active.generation_id
                )
                self._wait_for_readiness(
                    self._selected_api_container(active),
                    active,
                    mode="selected",
                    start_nonce=nonce,
                    operation_id=None,
                )
            return
        if phase is UpgradePhase.COMPLETED:
            self._remove_container(self._candidate_container_name(plan.operation_id))
            self._generation_store.remove_candidate(plan.operation_id)
            return
        raise UpgradeError(f"upgrade phase is not implemented: {phase.value}")

    def _require_active(self) -> SelectedGeneration:
        selected = self._generation_store.load_active()
        if selected is None:
            raise UpgradeConflict("an active control generation is required")
        return selected

    def maintenance(
        self,
        action: str,
        *,
        service: str | None = None,
        since_minutes: int = 30,
    ) -> dict[str, object]:
        """Run one allowlisted operation against the selected immutable generation."""

        selected = self._require_active()
        if action != "logs" and service is not None:
            raise UpgradeConflict("maintenance service is not allowed for this action")
        if action == "logs":
            if service not in _MAINTENANCE_LOG_SERVICES:
                raise UpgradeConflict("maintenance service is not allowed")
            if (
                isinstance(since_minutes, bool)
                or not isinstance(since_minutes, int)
                or not 1 <= since_minutes <= 1440
            ):
                raise UpgradeConflict("maintenance time range is invalid")
            arguments = (
                "logs",
                "--no-color",
                f"--since={since_minutes}m",
                service,
            )
        elif action == "status":
            arguments = ("ps", "--all", "--format", "json")
        elif action == "tailscale-status":
            arguments = (
                "exec",
                "-T",
                "tailscale-gateway",
                "tailscale",
                "--socket=/var/run/tailscale/tailscaled.sock",
                "status",
                "--json",
            )
        elif action == "tailscale-serve-status":
            arguments = (
                "exec",
                "-T",
                "tailscale-gateway",
                "tailscale",
                "--socket=/var/run/tailscale/tailscaled.sock",
                "serve",
                "status",
                "--json",
            )
        elif action == "tailscale-serve-config":
            arguments = (
                "exec",
                "-T",
                "tailscale-gateway",
                "tailscale",
                "--socket=/var/run/tailscale/tailscaled.sock",
                "serve",
                "get-config",
                "--all",
            )
        elif action == "step-ca-health":
            arguments = (
                "exec",
                "-T",
                "step-ca",
                "step",
                "ca",
                "health",
                "--ca-url",
                "https://127.0.0.1:9000",
                "--root",
                "/run/secrets/root_ca.crt",
            )
        else:
            raise UpgradeConflict("maintenance action is not allowed")

        def execute(generation: SelectedGeneration) -> bytes:
            return self._run_at_generation(
                generation,
                arguments,
                policy=CommandPolicy(300, 1024 * 1024, 1024 * 1024),
            )

        output = execute(selected)
        try:
            rendered = output.decode("utf-8")
        except UnicodeDecodeError as error:
            raise UpgradeConflict("maintenance output is invalid") from error
        return {
            "action": action,
            "generation_id": selected.generation_id,
            "mode": "diagnostic",
            "output": rendered,
        }

    def _selected_for_generation(self, generation_id: str) -> SelectedGeneration:
        active = self._generation_store.load_active()
        if active is not None and active.generation_id == generation_id:
            return active
        receipt = self._generation_store.load_generation(generation_id)
        if not isinstance(receipt, GenerationReceipt):
            raise HostStateConflict("generation receipt is invalid")
        receipt_sha = hashlib.sha256(_canonical(receipt.document())).hexdigest()
        return SelectedGeneration(
            projection_kind="active",
            operation_id="generation-probe",
            plan_digest="sha256:" + "0" * 64,
            **receipt.__dict__,
            previous_generation=None,
            generation_receipt_sha256=receipt_sha,
            selection_receipt_sha256="0" * 64,
            projection_sequence=1,
        )

    def _selected_runtime_nonce(self, operation_id: str, generation_id: str) -> str:
        if (
            _IDENTIFIER.fullmatch(operation_id) is None
            or _IDENTIFIER.fullmatch(generation_id) is None
        ):
            raise UpgradeConflict("selected runtime identity is invalid")
        launches = self._state_root / "runtime-launches"
        _ensure_private_directory(launches, "runtime launch directory")
        operation = launches / operation_id
        _ensure_private_directory(operation, "runtime launch operation")
        path = operation / f"{generation_id}.json"
        if path.exists() and not path.is_symlink():
            document = json.loads(
                _read_stable_file(path, label="selected runtime", maximum=4096)
            )
            if (
                not isinstance(document, dict)
                or document.get("schema_version") != 1
                or document.get("generation_id") != generation_id
                or not isinstance(document.get("start_nonce"), str)
                or re.fullmatch(r"[0-9a-f]{64}", document["start_nonce"]) is None
            ):
                raise UpgradeConflict("selected runtime receipt is invalid")
            return document["start_nonce"]
        nonce = secrets.token_hex(32)
        raw = _canonical(
            {
                "generation_id": generation_id,
                "schema_version": 1,
                "start_nonce": nonce,
            }
        )
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.write(descriptor, raw)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(operation)
        return nonce

    def _load_selected_runtime_nonce(
        self, operation_id: str, generation_id: str
    ) -> str | None:
        if (
            _IDENTIFIER.fullmatch(operation_id) is None
            or _IDENTIFIER.fullmatch(generation_id) is None
        ):
            raise UpgradeConflict("selected runtime identity is invalid")
        path = (
            self._state_root
            / "runtime-launches"
            / operation_id
            / f"{generation_id}.json"
        )
        if not path.exists() and not path.is_symlink():
            return None
        document = json.loads(
            _read_stable_file(path, label="selected runtime", maximum=4096)
        )
        nonce = document.get("start_nonce") if isinstance(document, dict) else None
        if (
            not isinstance(document, dict)
            or document.get("schema_version") != 1
            or document.get("generation_id") != generation_id
            or not isinstance(nonce, str)
            or re.fullmatch(r"[0-9a-f]{64}", nonce) is None
        ):
            raise UpgradeConflict("selected runtime receipt is invalid")
        return nonce

    def _run_at_generation(
        self,
        generation: SelectedGeneration,
        arguments: tuple[str, ...],
        *,
        start_nonce: str | None = None,
        policy: CommandPolicy,
    ) -> bytes:
        root = self._state_root / "generations" / generation.generation_id
        compose = root / "compose.yaml"
        _read_stable_file(
            compose, label="generation Compose file", maximum=4 * 1024 * 1024
        )
        nonce = start_nonce if start_nonce is not None else "0" * 64
        environment = self._site_environment()
        environment.update(
            selected_compose_environment(
                GenerationReleaseIdentity(
                    generation_id=generation.generation_id,
                    database_revision=generation.database_revision,
                    platform_version=generation.platform_version,
                    release_digest=generation.release_digest,
                    build_digest=generation.build_digest,
                    api_image=generation.api_image,
                    worker_image=generation.worker_image,
                ),
                SelectionRuntime.selected(nonce),
            )
        )
        environment["CONTROL_IDENTITY_PATH"] = str(self._generation_store.identity_root)
        return self._run(
            (
                "/usr/bin/docker",
                "compose",
                *self._compose_arguments(root, compose, environment),
                *arguments,
            ),
            environment=environment,
            cwd=root,
            policy=policy,
        )

    def _site_environment(self) -> dict[str, str]:
        return _parse_site_environment(
            _read_stable_file(
                self._site_environment_file,
                label="site configuration",
                maximum=_MAX_SITE_ENVIRONMENT,
            )
        )

    def _site_environment_if_available(self) -> dict[str, str]:
        if not self._site_environment_file.exists():
            return {}
        return self._site_environment()

    def _compose_arguments(
        self,
        root: Path,
        compose: Path,
        environment: Mapping[str, str],
    ) -> tuple[str, ...]:
        del root, environment
        return ("--file", str(compose))

    @staticmethod
    def _host_plan(plan: ControlGenerationPlan) -> HostOperationPlan:
        return HostOperationPlan(
            operation_id=plan.operation_id,
            plan_digest=plan.plan_digest,
            generation_id=plan.generation_id,
            platform_target_name=plan.platform_target_name,
            platform_target_sha256=plan.platform_target_sha256,
            tuf_targets_version=plan.tuf_targets_version,
            release_digest=plan.release_digest,
            build_digest=plan.build_digest,
            platform_version=plan.platform_version,
            deployment_bundle_digest=plan.deployment_bundle_digest,
            api_image=plan.api_image,
            worker_image=plan.worker_image,
            database_revision=plan.database_revision,
        )

    def _selected_from_plan(self, plan: ControlGenerationPlan) -> SelectedGeneration:
        receipt = self._host_plan(plan).generation_receipt()
        return SelectedGeneration(
            projection_kind="active",
            operation_id=plan.operation_id,
            plan_digest=plan.plan_digest,
            **receipt.__dict__,
            previous_generation=plan.previous_generation,
            generation_receipt_sha256=hashlib.sha256(
                _canonical(receipt.document())
            ).hexdigest(),
            selection_receipt_sha256="0" * 64,
            projection_sequence=1,
        )

    def _bundle_directory(self, plan: ControlGenerationPlan) -> Path:
        digest = plan.deployment_bundle_digest.removeprefix("sha256:")
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise UpgradeConflict("deployment bundle digest is invalid")
        return self._state_root / "bundles" / f"sha256-{digest}"

    def _load_bundle(
        self, plan: ControlGenerationPlan
    ) -> tuple[bytes, dict[str, object]]:
        root = self._bundle_directory(plan)
        raw = _read_stable_file(
            root / "bundle.tar",
            label="deployment bundle",
            maximum=64 * 1024 * 1024,
        )
        receipt = self._load_phase_receipt(root / "bundle.json")
        if receipt is None:
            raise UpgradeConflict("deployment bundle receipt is missing")
        return raw, receipt

    @staticmethod
    def _load_phase_receipt(path: Path) -> dict[str, object] | None:
        if not path.exists() and not path.is_symlink():
            return None
        raw = _read_stable_file(path, label="phase receipt", maximum=64 * 1024)
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UpgradeConflict("phase receipt is invalid") from error
        if not isinstance(document, dict) or raw != _canonical(document):
            raise UpgradeConflict("phase receipt is not canonical")
        return document

    def _acquire_bundle(self, plan: ControlGenerationPlan) -> None:
        self._generation_store.initialize()
        bundles = self._state_root / "bundles"
        bundles.mkdir(mode=0o700, exist_ok=True)
        work = self._state_root / "oci-work"
        work.mkdir(mode=0o700, exist_ok=True)
        destination = self._bundle_directory(plan)
        if destination.exists() or destination.is_symlink():
            raw, receipt = self._load_bundle(plan)
            verified = verify_deployment_bundle(raw, plan.deployment_bundle)
            references = _bundle_image_references(raw, plan)
            if receipt != {
                "archive_sha256": verified.archive_sha256,
                "images": list(references),
                "layer_digest": plan.deployment_bundle_digest,
                "manifest_sha256": verified.manifest_sha256,
                "schema_version": 1,
            }:
                raise UpgradeConflict("deployment bundle receipt is inconsistent")
        else:
            source = self._bundle_source
            if source is None:
                source = OciBundleSource(
                    oras_path=Path("/usr/bin/oras"),
                    work_directory=work,
                    runner=self._runner,
                    required_free_bytes=plan.required_bytes,
                )
            try:
                raw = source.fetch(plan.deployment_bundle)
                verified = verify_deployment_bundle(raw, plan.deployment_bundle)
            except (OciBundleError, DeploymentBundleError) as error:
                raise UpgradeError("deployment bundle acquisition failed") from error
            references = _bundle_image_references(raw, plan)
            destination.mkdir(mode=0o700)
            try:
                _write_new_file(destination / "bundle.tar", raw, mode=0o400)
                _write_new_file(
                    destination / "bundle.json",
                    _canonical(
                        {
                            "archive_sha256": verified.archive_sha256,
                            "images": list(references),
                            "layer_digest": plan.deployment_bundle_digest,
                            "manifest_sha256": verified.manifest_sha256,
                            "schema_version": 1,
                        }
                    ),
                    mode=0o400,
                )
            except Exception:
                for path in (destination / "bundle.json", destination / "bundle.tar"):
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                try:
                    destination.rmdir()
                except OSError:
                    pass
                raise
        for reference in references:
            if not self._image_is_available(reference):
                self._run(
                    ("/usr/bin/docker", "pull", reference),
                    policy=CommandPolicy(900, 1024 * 1024, 1024 * 1024),
                )

    def _staging_path(self, plan: ControlGenerationPlan) -> Path:
        return self._state_root / "generations" / f".{plan.generation_id}.staging"

    def _stage_generation(self, plan: ControlGenerationPlan) -> None:
        raw, _receipt = self._load_bundle(plan)
        verified = verify_deployment_bundle(raw, plan.deployment_bundle)

        def populate(destination: Path) -> None:
            extract_deployment_bundle(raw, destination, verified)
            generation = self._selected_from_plan(plan)
            environment = self._compose_environment(
                generation,
                start_nonce=plan.start_nonce,
                mode="preselection",
                operation_id=plan.operation_id,
            )
            rendered = self._run(
                (
                    "/usr/bin/docker",
                    "compose",
                    "--file",
                    str(destination / "compose.yaml"),
                    "config",
                ),
                environment=environment,
                cwd=destination,
                policy=_SERVICE_POLICY,
            )
            if not 1 <= len(rendered) <= 4 * 1024 * 1024:
                raise UpgradeError("rendered Compose model is invalid")
            rendered_digest = hashlib.sha256(rendered).hexdigest()
            _write_new_file(destination / "compose.rendered.yaml", rendered, mode=0o400)
            _write_new_file(
                destination / "staging.json",
                _canonical(
                    {
                        "bundle_digest": plan.deployment_bundle_digest,
                        "generation_id": plan.generation_id,
                        "plan_digest": plan.plan_digest,
                        "rendered_compose_sha256": rendered_digest,
                        "schema_version": 1,
                    }
                ),
                mode=0o400,
            )

        self._generation_store.prepare_staging(plan.generation_id, populate)

    def _compose_environment(
        self,
        generation: SelectedGeneration,
        *,
        start_nonce: str,
        mode: str,
        operation_id: str | None,
    ) -> dict[str, str]:
        environment = self._site_environment()
        environment.update(
            selected_compose_environment(
                GenerationReleaseIdentity(
                    generation_id=generation.generation_id,
                    database_revision=generation.database_revision,
                    platform_version=generation.platform_version,
                    release_digest=generation.release_digest,
                    build_digest=generation.build_digest,
                    api_image=generation.api_image,
                    worker_image=generation.worker_image,
                ),
                SelectionRuntime.selected(start_nonce),
            )
        )
        environment["CONTROL_IDENTITY_PATH"] = str(self._generation_store.identity_root)
        if mode == "preselection":
            if operation_id is None:
                raise UpgradeConflict("candidate operation ID is unavailable")
            environment["VONK_CONTROL_STARTUP_MODE"] = "preselection"
            environment["VONK_CONTROL_OPERATION_ID"] = operation_id
        return environment

    def _run_at_generation_with_environment(
        self,
        generation: SelectedGeneration,
        arguments: tuple[str, ...],
        environment: Mapping[str, str],
        *,
        policy: CommandPolicy,
    ) -> bytes:
        root = self._state_root / "generations" / generation.generation_id
        compose = root / "compose.yaml"
        _read_stable_file(
            compose, label="generation Compose file", maximum=4 * 1024 * 1024
        )
        return self._run(
            (
                "/usr/bin/docker",
                "compose",
                *self._compose_arguments(root, compose, environment),
                *arguments,
            ),
            environment=dict(environment),
            cwd=root,
            policy=policy,
        )

    def _run_at_staging(
        self,
        plan: ControlGenerationPlan,
        arguments: tuple[str, ...],
        *,
        environment: Mapping[str, str],
        policy: CommandPolicy,
    ) -> bytes:
        root = self._staging_path(plan)
        compose = root / "compose.yaml"
        _read_stable_file(
            compose, label="staged generation Compose file", maximum=4 * 1024 * 1024
        )
        return self._run(
            (
                "/usr/bin/docker",
                "compose",
                *self._compose_arguments(root, compose, environment),
                *arguments,
            ),
            environment=dict(environment),
            cwd=root,
            policy=policy,
        )

    @staticmethod
    def _candidate_container_name(operation_id: str) -> str:
        if _IDENTIFIER.fullmatch(operation_id) is None:
            raise UpgradeConflict("candidate operation ID is invalid")
        return "vonk-forge-candidate-" + operation_id

    def _remove_container(self, name: str) -> None:
        try:
            self._run(
                ("/usr/bin/docker", "rm", "--force", name),
                policy=_SERVICE_POLICY,
            )
        except BackupError:
            # Docker's absent-container result is idempotent for this fixed name.
            return

    def _candidate_is_clean(self, operation_id: str) -> bool:
        candidate_path = (
            Path(self._generation_store.identity_root)
            / "candidates"
            / f"{operation_id}.json"
        )
        if candidate_path.exists() or candidate_path.is_symlink():
            # Existing unsafe or exact candidate state is not terminal cleanup.
            self._generation_store.load_candidate(operation_id)
            return False
        name = self._candidate_container_name(operation_id)
        raw = self._run(
            (
                "/usr/bin/docker",
                "ps",
                "--all",
                "--filter",
                f"name=^/{name}$",
                "--format",
                "{{.Names}}",
            ),
            policy=_PROBE_POLICY,
        )
        try:
            names = raw.decode("ascii").splitlines()
        except UnicodeDecodeError as error:
            raise UpgradeConflict("candidate container probe is invalid") from error
        if any(item != name for item in names) or len(names) > 1:
            raise UpgradeConflict("candidate container probe is invalid")
        return not names

    def _image_is_available(self, reference: str) -> bool:
        try:
            raw = self._run(
                (
                    "/usr/bin/docker",
                    "image",
                    "inspect",
                    "--format",
                    "{{json .RepoDigests}}",
                    reference,
                ),
                policy=_PROBE_POLICY,
            )
        except BackupError:
            return False
        try:
            digests = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UpgradeConflict("local image identity probe is invalid") from error
        expected_digest = reference.rsplit("@", 1)[-1]
        if not isinstance(digests, list) or any(
            not isinstance(item, str) for item in digests
        ):
            raise UpgradeConflict("local image identity probe is invalid")
        return any(item.rsplit("@", 1)[-1] == expected_digest for item in digests)

    def _selected_api_container(self, generation: SelectedGeneration) -> str:
        raw = self._run_at_generation(
            generation,
            ("ps", "-q", "control-api"),
            policy=_PROBE_POLICY,
        )
        try:
            container_id = raw.decode("ascii").strip()
        except UnicodeDecodeError as error:
            raise UpgradeConflict("selected API container probe is invalid") from error
        if _CONTAINER_ID.fullmatch(container_id) is None:
            raise UpgradeConflict("selected API container is unavailable")
        return container_id

    def _readiness_probe(
        self,
        container: str,
        generation: SelectedGeneration,
        *,
        mode: str,
        start_nonce: str,
        operation_id: str | None,
    ) -> dict[str, object]:
        script = (
            "import json,urllib.request;"
            "r=urllib.request.urlopen('http://127.0.0.1:8000/internal/v1/generation/readiness',timeout=3);"
            "print(json.dumps(json.load(r),sort_keys=True,separators=(',',':')))"
        )
        raw = self._run(
            ("/usr/bin/docker", "exec", container, "python", "-c", script),
            policy=CommandPolicy(10, _MAX_PROBE_OUTPUT, 16 * 1024),
        )
        try:
            evidence = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UpgradeConflict("generation readiness probe is invalid") from error
        expected = {
            "build_digest": generation.build_digest,
            "database_revision": generation.database_revision,
            "generation_id": generation.generation_id,
            "mode": mode,
            "release_digest": generation.release_digest,
            "start_nonce": start_nonce,
            "status": "ready",
        }
        if mode == "preselection":
            if operation_id is None:
                raise UpgradeConflict("candidate readiness operation is unavailable")
            expected["operation_id"] = operation_id
        if not isinstance(evidence, dict) or any(
            evidence.get(key) != value for key, value in expected.items()
        ):
            raise UpgradeConflict("generation readiness identity is inconsistent")
        return expected

    def _wait_for_readiness(
        self,
        container: str,
        generation: SelectedGeneration,
        *,
        mode: str,
        start_nonce: str,
        operation_id: str | None,
    ) -> dict[str, object]:
        deadline = time.monotonic() + 90
        while True:
            try:
                return self._readiness_probe(
                    container,
                    generation,
                    mode=mode,
                    start_nonce=start_nonce,
                    operation_id=operation_id,
                )
            except (BackupError, UpgradeConflict):
                if time.monotonic() >= deadline:
                    raise UpgradeError("generation readiness deadline elapsed")
                time.sleep(1)

    def control_is_running(self) -> bool:
        output = self._run(
            (*self._compose(), "ps", "--status", "running", "--services")
        )
        services = set(output.decode("utf-8").splitlines())
        return bool(services & {"control-api", "control-worker"})

    def available_bytes(self) -> int:
        existing = self._state_root
        while not existing.exists():
            if existing.parent == existing:
                raise BackupError("upgrade state filesystem is unavailable")
            existing = existing.parent
        return shutil.disk_usage(existing).free

    def pull(self, references: tuple[str, ...]) -> None:
        for reference in references:
            self._run(("/usr/bin/docker", "pull", reference))

    def render_compose(self, environment: dict[str, str]) -> bytes:
        self._environment = dict(environment)
        return self._run((*self._compose(), "config"), environment=self._environment)

    def backup(self, generation_id: str) -> dict[str, object]:
        selected = self._generation_store.load_active()
        if selected is None:
            raise BackupError("an active selected generation is required for backup")
        return asdict(
            self._backup_boundary.create_upgrade_backup(selected, generation_id)
        )

    def stop_worker(self) -> None:
        self._run(
            (*self._compose(), "stop", "control-worker"), environment=self._environment
        )

    def migrate(self, revision: str) -> None:
        try:
            self._run(
                (
                    *self._compose(),
                    "run",
                    "--rm",
                    "control-api",
                    "python",
                    "-m",
                    "alembic",
                    "upgrade",
                    revision,
                ),
                environment=self._environment,
            )
        except BackupError as error:
            raise AmbiguousMigrationError(
                "database migration command failed and its outcome is unknown"
            ) from error

    def start_api(self, generation_path: Path) -> None:
        self._environment = _read_generation_environment(generation_path)
        self._run(
            (*self._compose(), "up", "-d", "control-api"), environment=self._environment
        )

    def readiness(self) -> dict[str, object]:
        for attempt in range(30):
            if _api_running(self._health_url):
                return {"status": "ready", "probe": "caddy", "attempt": attempt + 1}
            time.sleep(1)
        from .upgrade import UpgradeReadinessError

        raise UpgradeReadinessError("candidate control API readiness deadline elapsed")

    def start_worker(self) -> None:
        self._run(
            (*self._compose(), "up", "-d", "control-worker"),
            environment=self._environment,
        )

    def stop_api(self) -> None:
        self._run(
            (*self._compose(), "stop", "control-api"), environment=self._environment
        )

    def restore_generation(self, generation_path: Path) -> None:
        self._environment = _read_generation_environment(generation_path)
        self._run(
            (*self._compose(), "up", "-d", "control-api", "control-worker"),
            environment=self._environment,
        )

    def _compose(self) -> tuple[str, ...]:
        return ("/usr/bin/docker", "compose", "--file", str(self._compose_file))

    def _run(
        self,
        argv: Sequence[str],
        *,
        environment: dict[str, str] | None = None,
        cwd: Path | None = None,
        policy: CommandPolicy | None = None,
    ) -> bytes:
        exact_environment = {
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            **(environment or {}),
        }
        try:
            return self._runner.run(
                tuple(argv),
                cwd=self._compose_file.parent if cwd is None else cwd,
                env=exact_environment,
                policy=self._command_policy if policy is None else policy,
            ).stdout
        except HostCommandError as error:
            raise BackupError("upgrade command failed") from error


class _PlanOnlyUpgradeBoundary:
    def __getattr__(self, name: str):
        raise UpgradeConflict(f"dry-run boundary cannot execute {name}")


def require_offline(
    state_path: Path,
    *,
    probe: Callable[[], bool],
    owner_uid: int = 0,
) -> HostOperationLock:
    lock = HostOperationLock(state_path, owner_uid=owner_uid)
    try:
        lock.__enter__()
    except HostStateConflict as error:
        raise OfflineConflict("another offline maintenance operation is active") from error
    if probe():
        lock.__exit__()
        raise OfflineConflict("control plane is running; stop API and worker first")
    return lock


def _api_running(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _read_generation_environment(generation_path: Path) -> dict[str, str]:
    path = generation_path / "platform.env"
    if path.is_symlink() or not path.is_file():
        raise BackupError("control generation environment is unsafe or missing")
    expected = {
        "CONTROL_API_IMAGE",
        "CONTROL_WORKER_IMAGE",
        "VONK_PLATFORM_BUILD_DIGEST",
        "VONK_PLATFORM_RELEASE_DIGEST",
        "VONK_PLATFORM_VERSION",
    }
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.partition("=")
        if not separator or name not in expected or name in values or not value:
            raise BackupError("control generation environment is invalid")
        values[name] = value
    if set(values) != expected:
        raise BackupError("control generation environment is incomplete")
    return values


def _load_trusted_release(path: Path, state_root: Path) -> PlatformRelease:
    bootstrap_name = "VONK_PLATFORM_TUF_ROOT"
    bootstrap_value = os.environ.get(bootstrap_name, "")
    metadata_url = os.environ.get("VONK_PLATFORM_TUF_METADATA_URL", "")
    target_url = os.environ.get("VONK_PLATFORM_TUF_TARGET_URL", "")
    if not bootstrap_value:
        raise BackupError(f"{bootstrap_name} is required for upgrade --apply")
    bootstrap = Path(bootstrap_value)
    if bootstrap.is_symlink() or not bootstrap.is_file():
        raise BackupError(f"{bootstrap_name} must name a regular non-symlink file")
    if not metadata_url:
        raise BackupError(
            "VONK_PLATFORM_TUF_METADATA_URL is required for upgrade --apply"
        )
    if not target_url:
        raise BackupError("VONK_PLATFORM_TUF_TARGET_URL is required for upgrade --apply")
    if path.is_symlink() or not path.is_file():
        raise BackupError("platform release target is unsafe or missing")
    trust_root = state_root / "platform-tuf"
    trust = UpdateTrust(
        metadata_root=trust_root / "metadata",
        target_root=trust_root / "targets",
        metadata_base_url=metadata_url,
        target_base_url=target_url,
        bootstrap_root=bootstrap.read_bytes(),
        fetcher=Urllib3Fetcher(),
    )
    trust.refresh()
    target = trust.trusted_target(path.name)
    if target.data != path.read_bytes():
        raise BackupError("platform release differs from its TUF-authorized target")
    return PlatformRelease.load(path)


class _TrustedReleaseSource:
    def __init__(self, trust: UpdateTrust) -> None:
        self._trust = trust

    def refresh(self, target_name: str) -> tuple[bytes, int]:
        target, version = self._trust.refresh_and_trusted_target(target_name)
        return target.data, version


def _load_release_source(state_root: Path) -> _TrustedReleaseSource:
    bootstrap_name = "VONK_PLATFORM_TUF_ROOT"
    bootstrap_value = os.environ.get(bootstrap_name, "")
    metadata_url = os.environ.get("VONK_PLATFORM_TUF_METADATA_URL", "")
    target_url = os.environ.get("VONK_PLATFORM_TUF_TARGET_URL", "")
    if not bootstrap_value:
        raise BackupError(f"{bootstrap_name} is required")
    bootstrap = Path(bootstrap_value)
    if bootstrap.is_symlink() or not bootstrap.is_file():
        raise BackupError(f"{bootstrap_name} must name a regular non-symlink file")
    if not metadata_url:
        raise BackupError("VONK_PLATFORM_TUF_METADATA_URL is required")
    if not target_url:
        raise BackupError("VONK_PLATFORM_TUF_TARGET_URL is required")
    trust_root = state_root / "tuf"
    return _TrustedReleaseSource(
        UpdateTrust(
            metadata_root=trust_root / "metadata",
            target_root=trust_root / "targets",
            metadata_base_url=metadata_url,
            target_base_url=target_url,
            bootstrap_root=bootstrap.read_bytes(),
            fetcher=Urllib3Fetcher(),
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vonk-control-offline")
    parser.add_argument(
        "--state-path", type=Path, default=Path("/srv/vonk-forge/control-host")
    )
    parser.add_argument(
        "--identity-path",
        type=Path,
        default=Path("/srv/vonk-forge/control-identity"),
    )
    parser.add_argument("--health-url", default="https://127.0.0.1/api/v1/healthz")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    commands.add_parser("init")
    commands.add_parser("migrate")
    admin = commands.add_parser("create-admin")
    admin.add_argument("--subject", required=True)
    upgrade = commands.add_parser("upgrade")
    upgrade.add_argument("--target-name", required=True)
    upgrade.add_argument("--apply", action="store_true")
    recover = commands.add_parser("recover")
    recover.add_argument("--apply", action="store_true")
    rollback = commands.add_parser("rollback")
    rollback.add_argument("--generation", required=True)
    rollback.add_argument("--apply", action="store_true")
    maintenance = commands.add_parser("maintenance")
    maintenance.add_argument("action", choices=_MAINTENANCE_ACTIONS)
    maintenance.add_argument("--service", choices=sorted(_MAINTENANCE_LOG_SERVICES))
    maintenance.add_argument("--since-minutes", type=int, default=30)
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            print(json.dumps({"api_running": _api_running(args.health_url)}))
            return 0
        if args.command in {"upgrade", "recover", "rollback", "maintenance"}:
            site_environment = os.environ.get(
                "VONK_CONTROL_SITE_ENV_FILE", str(args.state_path / "site.env")
            )
            if not Path(site_environment).is_absolute():
                raise BackupError(
                    "VONK_CONTROL_SITE_ENV_FILE must name an absolute root-owned file"
                )
            recipients = os.environ.get("VONK_BACKUP_RECIPIENTS_FILE", "")
            identity = os.environ.get("VONK_BACKUP_IDENTITY_FILE", "")
            needs_backup_credentials = bool(
                getattr(args, "apply", False)
                and args.command in {"upgrade", "recover", "rollback"}
            )
            if needs_backup_credentials and (
                not recipients or not Path(recipients).is_absolute()
            ):
                raise BackupError(
                    "VONK_BACKUP_RECIPIENTS_FILE must name an absolute root-owned file"
                )
            if needs_backup_credentials and (
                not identity or not Path(identity).is_absolute()
            ):
                raise BackupError(
                    "VONK_BACKUP_IDENTITY_FILE must name an absolute root-owned file"
                )
            active = HostGenerationStore(
                args.state_path, args.identity_path
            ).load_active()
            compose_file = (
                args.state_path / "generations" / active.generation_id / "compose.yaml"
                if active is not None
                else args.state_path / "bootstrap-unavailable" / "compose.yaml"
            )
            boundary = HostUpgradeBoundary(
                state_root=args.state_path,
                compose_file=compose_file,
                recipients_file=(
                    Path(recipients)
                    if recipients
                    else args.state_path / "backup-recipients.unavailable"
                ),
                identity_file=Path(identity) if identity else None,
                site_environment_file=Path(site_environment),
                health_url=args.health_url,
                generation_store=HostGenerationStore(
                    args.state_path, args.identity_path
                ),
            )
            if args.command == "maintenance":
                print(
                    json.dumps(
                        boundary.maintenance(
                            args.action,
                            service=args.service,
                            since_minutes=args.since_minutes,
                        ),
                        sort_keys=True,
                    )
                )
                return 0
            release_source = _load_release_source(args.state_path)
            service = ControlUpgrade(
                args.state_path,
                boundary,
                release_source=release_source,
                identity_root=args.identity_path,
            )
            if args.command == "upgrade":
                plan = service.plan(args.target_name)
                if not args.apply:
                    print(json.dumps({**asdict(plan), "mode": "plan"}, sort_keys=True))
                    return 0
                print(json.dumps(asdict(service.apply(plan)), sort_keys=True))
                return 0
            if args.command == "recover":
                if not args.apply:
                    raise BackupError("recover requires explicit --apply")
                print(json.dumps(asdict(service.recover()), sort_keys=True))
                return 0
            plan = service.rollback_plan(args.generation)
            if not args.apply:
                print(
                    json.dumps(
                        {**asdict(plan), "mode": "plan"},
                        sort_keys=True,
                    )
                )
                return 0
            print(json.dumps(asdict(service.rollback(plan)), sort_keys=True))
            return 0
        lock = require_offline(
            args.state_path, probe=lambda: _api_running(args.health_url)
        )
        with lock:
            if args.command == "init":
                args.state_path.mkdir(parents=True, exist_ok=True, mode=0o700)
                return 0
            if args.command == "migrate":
                from alembic import command
                from alembic.config import Config

                from .settings import Settings

                settings = Settings.from_env_and_secrets()
                config = Config(Path(__file__).resolve().parents[2] / "alembic.ini")
                config.set_main_option("sqlalchemy.url", settings.database_url)
                command.upgrade(config, "head")
                return 0
            if args.command == "create-admin":
                from .db import build_engine, session_factory
                from .models import User
                from .settings import Settings
                from .user_authority import serialize_user_authority

                settings = Settings.from_env_and_secrets()
                sessions = session_factory(build_engine(settings.database_url))
                with sessions.begin() as session:
                    serialize_user_authority(session)
                    session.add(User(subject=args.subject, role="administrator"))
                return 0
            raise BackupError(f"unsupported offline command: {args.command}")
    except OfflineConflict as error:
        print(f"vonk-control-offline: {error}", file=__import__("sys").stderr)
        return 3
    except UpgradeRecoveryRequired as error:
        print(f"vonk-control-offline: {error}", file=__import__("sys").stderr)
        return 4
    except (
        BackupError,
        OSError,
        PlatformReleaseError,
        UpdateTrustError,
        UpgradeError,
    ) as error:
        print(f"vonk-control-offline: {error}", file=__import__("sys").stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
