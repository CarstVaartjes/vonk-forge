"""Disposable human-session launcher for W19 U1–U3 operator cards.

This opt-in test deliberately uses the installed CLI wheel, registered API
routes, and PostgreSQL-backed service owners. U2's later-page candidate is
prepared synchronously through the model-cache and runtime-image owners using
exact local fixture bytes and Skopeo-verified OCI content. It starts no
background worker and does not connect to a Spark.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import canonical_message
from vonk_control.agent_api import AgentApiServices
from vonk_control.agent_jobs import AgentJobService
from vonk_control.api import create_app
from vonk_control.audit import SqlAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.catalog_entities import CatalogEntityService
from vonk_control.database_authority import DatabaseAuthorityService
from vonk_control.distribution import (
    DistributionService,
    build_distribution_service_from_components,
)
from vonk_control.distribution_executor import CompositeDistributionPhaseExecutor
from vonk_control.execution_plan_service import ControllerExecutionPlanService
from vonk_control.fleet_profile_contract import (
    FleetProfileLoadRequest,
    FleetProfilePreview,
)
from vonk_control.fleet_profiles import (
    FleetProfileService,
    RunSwitchFleetProfileAdapter,
    build_production_fleet_profile_service,
)
from vonk_control.fleet_projection import FleetProjection
from vonk_control.host_helper_authority import (
    HostHelperGrantIssuer,
    HostRuntimeAuthorityService,
)
from vonk_control.install_admission import (
    InstallAdmissionService,
    authorize_installation_runtime_images,
)
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.jobs import JobService
from vonk_control.library_assessment import LibraryAssessment
from vonk_control.library_projection import LibraryProjection
from vonk_control.model_cache import ModelCacheService
from vonk_control.model_cache_contract import ModelCacheObjectReceipt
from vonk_control.models import (
    AgentNode,
    AgentNodeProfile,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    FleetProfileApplication,
    Job,
    RecipeRun,
    User,
)
from vonk_control.operation_api import durable_operation_services
from vonk_control.presence import AgentPresenceService, ManagementAddressPolicy
from vonk_control.recipe_image_availability import RecipeImageAvailabilityService
from vonk_control.recipe_operation_worker import RecipeOperationWorker
from vonk_control.recipe_operations import RecipeOperationService
from vonk_control.recipe_routes import AtomicRecipeRoutePublisher, RecipeRouteService
from vonk_control.recipe_runtime_specs import (
    compile_runtime_spec,
    resolve_recipe_entities,
)
from vonk_control.route_runtime import AtomicRouteBundlePublisher
from vonk_control.run_admission import RunAdmissionService
from vonk_control.run_switch_operations import RunSwitchOperationService
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    PulledImageEvidence,
    RuntimeImagePreparationError,
    SkopeoOCIImageTransport,
    make_runtime_image_receipt_preparer,
    resolve_persisted_runtime_image_receipt,
    runtime_image_expectations,
)
from vonk_control.source_bundles import SourceBundleStore
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256

from .test_profile_load_installed_cli import (
    _build_installed_vonkctl,
    _https_api_peer,
    _process_environment,
)
from .test_recipe_operations import NOW, RECEIPT_SIGNER, setup_services

pytest_plugins = ("tests.test_profile_load_installed_cli",)

pytestmark = [
    pytest.mark.lane,
    pytest.mark.skipif(
        "VONK_WALKTHROUGH_MODE" not in os.environ,
        reason="set VONK_WALKTHROUGH_MODE=smoke or interactive to opt in",
    ),
]

_READY_MODEL_HEADER = json.dumps(
    {"weight": {"dtype": "F16", "shape": [1], "data_offsets": [0, 2]}},
    separators=(",", ":"),
).encode("utf-8")
_READY_MODEL_HEADER += b" " * (-len(_READY_MODEL_HEADER) % 8)
_READY_MODEL_PAYLOAD = len(_READY_MODEL_HEADER).to_bytes(8, "little") + (
    _READY_MODEL_HEADER + b"\x00\x00"
)
_READY_MODEL_REVISION = "a" * 40
_READY_MODEL_SELECTOR = "walkthrough-synthetic-tiny-ready-fp16"
_READY_RECIPE_SELECTOR = "walkthrough-synthetic-vllm-b-ready"
_QWEN3_READY_RECIPE_SELECTOR = "qwen3-vllm-a-ready"
_SYNTHETIC_BASE_RECIPE_SELECTOR = "walkthrough-synthetic-vllm-a-base"
_BLOCKED_RECIPE_SELECTOR = "walkthrough-synthetic-vllm-z-blocked"
# Match the fingerprint seeded by the shared disposable-host fixture; this is
# not a physical host observation. Newly issued probe operations still travel
# through AgentJobService with typed receipts in the linked journey.
_LINKED_PREFLIGHT_FINGERPRINT = "a" * 64


@dataclass(frozen=True)
class _LinkedProfileOwners:
    """Real owners used only by the installed linked-journey facilitator."""

    sessions: sessionmaker[Session]
    agent_jobs: AgentJobService
    recipe_operations: RecipeOperationService
    run_switch_operations: RunSwitchOperationService
    profiles: FleetProfileService
    routes: RecipeRouteService
    worker: RecipeOperationWorker
    model_cache: ModelCacheService
    distribution: DistributionService
    image_inspector: SkopeoOCIImageTransport
    image_evidence: PulledImageEvidence
    target_root: Path
    events: list[str]
    clock: Callable[[], datetime]
    interactive_clock: bool
    advance_clock: Callable[[], None]


def _walkthrough_clock(
    *,
    interactive: bool,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[Callable[[], datetime], Callable[[], None]]:
    """Use a virtual clock in smoke and elapsed time in an operator shell."""

    virtual_time = [NOW]
    started_at = monotonic()

    def clock() -> datetime:
        if interactive:
            return NOW + timedelta(seconds=max(0.0, monotonic() - started_at))
        return virtual_time[0]

    def advance_clock() -> None:
        if not interactive:
            virtual_time[0] += timedelta(seconds=1)

    return clock, advance_clock


_QWEN3_BLOCKED_RECIPE_SELECTOR = "qwen3-vllm-0-blocked"
_QWEN3_REVISION = "c1899de289a04d12100db370d81485cdf75e47ca"
_QWEN3_SOURCE_URL = "https://huggingface.co/Qwen/Qwen3-0.6B"
_QWEN3_IMAGE_DIGEST = (
    "sha256:c775a5f6e778c53946ce3dc266dc4052d3ca1e18f904b4d09eed8cef1c0489ee"
)
_QWEN3_RUNTIME_REPOSITORY = "vonk-test/qwen3-vllm-cpu-runtime"
_QWEN3_RUNTIME_TAG = "vonk-test/qwen3-vllm-cpu-runtime:verified-u2-20260924"
_QWEN3_ARCHIVE_SHA256 = (
    "d7af73ab4387d996caaf7b74eea0e45886a351cb9a2cf9014f42c6cd4025516d"
)
_QWEN3_ARCHIVE_BYTES = 881_898_496
_QWEN3_PINNED_FILES = {
    ".gitattributes": (
        1570,
        "git:52373fe24473b1aa44333d318f578ae6bf04b49b",
        "git-attributes",
        "metadata",
    ),
    "LICENSE": (
        11_343,
        "git:6634c8cc3133b3848ec74b9f275acaaa1ea618ab",
        "license",
        "metadata",
    ),
    "README.md": (
        13_965,
        "git:a50b19e76f5274f9ec99f5a5d99873dca5bff25e",
        "readme",
        "metadata",
    ),
    "config.json": (
        726,
        "git:f5c3703b78ae2a478ae15b247e9f855e0ce2107b",
        "config",
        "config",
    ),
    "generation_config.json": (
        239,
        "git:20a8a9156fc8c3f25295ca067f61fdf120d517c5",
        "generation-config",
        "config",
    ),
    "merges.txt": (
        1_671_853,
        "git:31349551d90c7606f325fe0f11bbb8bd5fa0d7c7",
        "merges",
        "tokenizer",
    ),
    "model.safetensors": (
        1_503_300_328,
        "sha256:f47f71177f32bcd101b7573ec9171e6a57f4f4d31148d38e382306f42996874b",
        "weights",
        "weights",
    ),
    "tokenizer.json": (
        11_422_654,
        "git:949e1ec83f61520a25c75426edc4a43acc36f29a",
        "tokenizer",
        "tokenizer",
    ),
    "tokenizer_config.json": (
        9732,
        "git:417d038a63fa3de29cfde265caedae14d1a58d92",
        "tokenizer-config",
        "tokenizer",
    ),
    "vocab.json": (
        2_776_833,
        "git:4783fe10ac3adce15ac8f358ef5462739852c569",
        "vocab",
        "tokenizer",
    ),
}


@dataclass(frozen=True, slots=True)
class _QwenCPUAssets:
    model: ModelDefinition
    source_files: dict[str, Path]
    runtime_archive: Path
    image_evidence: PulledImageEvidence


class _FileResponseStream(httpx.SyncByteStream):
    def __init__(self, path: Path) -> None:
        self._path = path

    def __iter__(self) -> Iterator[bytes]:
        with self._path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                yield chunk

    def close(self) -> None:
        return None


def load_qwen_cpu_assets(asset_root: Path) -> _QwenCPUAssets:
    """Verify persistent Qwen bytes against their exact upstream tree identity."""

    if asset_root.is_symlink() or not asset_root.is_dir():
        raise ValueError("Qwen CPU asset root is not a local directory")
    snapshot = asset_root / "hub-snapshot"
    source_files: dict[str, Path] = {}
    file_documents: list[dict[str, object]] = []
    verified_digests: dict[str, str] = {}
    for path, (
        expected_bytes,
        expected_identity,
        file_id,
        role,
    ) in _QWEN3_PINNED_FILES.items():
        source = snapshot / path
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"pinned Qwen asset is missing or not regular: {path}")
        observed_bytes = source.stat().st_size
        if observed_bytes != expected_bytes:
            raise ValueError(
                f"pinned Qwen asset has {observed_bytes} bytes; "
                f"expected {expected_bytes}: {path}"
            )
        sha256 = hashlib.sha256()
        git_blob_sha1 = hashlib.sha1()
        git_blob_sha1.update(f"blob {observed_bytes}\0".encode("ascii"))
        with source.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                sha256.update(chunk)
                git_blob_sha1.update(chunk)
        observed_sha256 = sha256.hexdigest()
        observed_identity = (
            f"sha256:{observed_sha256}"
            if expected_identity.startswith("sha256:")
            else f"git:{git_blob_sha1.hexdigest()}"
        )
        if observed_identity != expected_identity:
            raise ValueError(
                f"pinned Qwen asset identity differs from the exact revision: {path}"
            )
        source_files[path] = source
        verified_digests[path] = observed_sha256
        file_documents.append(
            {
                "id": file_id,
                "path": path,
                "roles": [role],
                "sha256": observed_sha256,
                "size_bytes": observed_bytes,
            }
        )

    configuration = json.loads((snapshot / "config.json").read_text(encoding="utf-8"))
    if (
        configuration.get("architectures") != ["Qwen3ForCausalLM"]
        or configuration.get("model_type") != "qwen3"
        or configuration.get("max_position_embeddings") != 40_960
        or configuration.get("torch_dtype") != "bfloat16"
    ):
        raise ValueError("pinned Qwen configuration does not match expected identity")
    evidence_digest = verified_digests["README.md"]
    model_document = json.loads(
        resources.files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text(encoding="utf-8")
    )
    model_document.update(
        {
            "identity": {
                "publisher": "qwen",
                "slug": "qwen3-0-6b-bf16",
                "family": {"publisher": "qwen", "slug": "qwen3", "title": "Qwen3"},
                "model": {
                    "architecture": "Qwen3ForCausalLM",
                    "publisher": "qwen",
                    "slug": "qwen3-0-6b",
                    "title": "Qwen3-0.6B",
                },
                "version": _QWEN3_REVISION,
                "variant": "bf16",
            },
            "metadata": {
                "description": (
                    "Qwen3-0.6B causal text-generation model from its pinned "
                    "upstream snapshot."
                ),
                "tags": ["qwen", "text", "transformer"],
            },
            "access": {
                "visibility": "public",
                "gated": False,
                "authentication": "none",
            },
            "lineage": {
                "publisher": "qwen",
                "relation": "official",
                "source_model": {
                    "kind": "model",
                    "publisher": "qwen",
                    "slug": "qwen3-0-6b",
                },
                "derivation": f"Upstream Qwen3-0.6B snapshot {_QWEN3_REVISION}.",
            },
            "source": {"repository": _QWEN3_SOURCE_URL, "revision": _QWEN3_REVISION},
            "format": {
                "container": "safetensors",
                "precision": "bfloat16",
                "quantization": "none",
            },
            "parameters": {},
            "limits": {"context_tokens": 40_960},
            "license": {
                "spdx": "Apache-2.0",
                "url": "https://www.apache.org/licenses/LICENSE-2.0",
                "attribution": ["Qwen"],
                "operator_acceptance_required": False,
            },
            "files": file_documents,
            "capabilities": {
                "facts": [
                    {
                        "capability": "text-generation",
                        "support": "supported",
                        "evidence_status": "declared",
                        "evidence_digest": None,
                    }
                ],
                "provenance": {
                    "source_url": (
                        f"{_QWEN3_SOURCE_URL}/blob/{_QWEN3_REVISION}/README.md"
                    ),
                    "source_revision": _QWEN3_REVISION,
                    "evidence_digest": evidence_digest,
                },
            },
            "provenance": {
                "source_url": f"{_QWEN3_SOURCE_URL}/tree/{_QWEN3_REVISION}",
                "source_revision": _QWEN3_REVISION,
                "evidence_digest": evidence_digest,
                "attribution": ["Qwen"],
            },
        }
    )
    model = ModelDefinition.model_validate(model_document)

    runtime_archive = asset_root / "vllm-qwen3-cpu-runtime-verified-u2-20260924.tar"
    if runtime_archive.is_symlink() or not runtime_archive.is_file():
        raise ValueError("pinned Qwen CPU runtime archive is missing or not regular")
    archive_bytes = runtime_archive.stat().st_size
    if archive_bytes != _QWEN3_ARCHIVE_BYTES:
        raise ValueError(
            f"runtime archive has {archive_bytes} bytes; "
            f"expected {_QWEN3_ARCHIVE_BYTES}"
        )
    with runtime_archive.open("rb") as stream:
        archive_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    if archive_sha256 != _QWEN3_ARCHIVE_SHA256:
        raise ValueError("pinned Qwen CPU runtime archive SHA256 differs")
    inspector = SkopeoOCIImageTransport(executable=_local_skopeo())
    image_evidence = inspector.inspect_archive(
        runtime_archive,
        expected_architecture="linux/arm64",
        expected_runtime_interface="vonk.runtime.v1",
        expected_archive_sha256=archive_sha256,
        expected_archive_bytes=archive_bytes,
    )
    if image_evidence.manifest_digest != _QWEN3_IMAGE_DIGEST:
        raise ValueError("pinned Qwen CPU image manifest differs from Skopeo output")
    return _QwenCPUAssets(model, source_files, runtime_archive, image_evidence)


class _LocalOCIFileTransport:
    """Copy one local archive and attach the exact Skopeo-inspected identity."""

    def __init__(self, archive: Path, inspector: SkopeoOCIImageTransport) -> None:
        self._archive = archive
        self._inspector = inspector

    def pull_and_export(
        self,
        reference: str,
        destination: Path,
        *,
        expected_architecture: str,
        expected_runtime_interface: str,
        progress: Callable[[str, int, int | None], None] | None = None,
    ) -> PulledImageEvidence:
        del progress
        if not self._archive.is_file() or self._archive.is_symlink():
            raise RuntimeImagePreparationError(
                "runtime_image.archive_unavailable",
                "local walkthrough OCI archive is unavailable",
            )
        expected_digest = reference.rpartition("@")[2]
        if not expected_digest.startswith("sha256:"):
            raise RuntimeImagePreparationError(
                "runtime_image.digest_mismatch",
                "local walkthrough image reference is not digest pinned",
            )
        shutil.copyfile(self._archive, destination)
        archive_bytes = destination.stat().st_size
        with destination.open("rb") as stream:
            archive_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
        inspected = self._inspector.inspect_archive(
            destination,
            expected_architecture=expected_architecture,
            expected_runtime_interface=expected_runtime_interface,
            expected_archive_sha256=archive_sha256,
            expected_archive_bytes=archive_bytes,
        )
        if inspected.manifest_digest != expected_digest:
            raise RuntimeImagePreparationError(
                "runtime_image.digest_mismatch",
                "local walkthrough image does not match its recipe digest pin",
            )
        # Skopeo provides every content, config, platform, and archive field.
        # Bind the copied file to the exact recipe reference only after its
        # inspected manifest is proven equal to that immutable digest.
        return replace(
            inspected,
            requested_manifest_digest=expected_digest,
            local_reference=reference,
        )

    def inspect_archive(
        self,
        archive: Path,
        *,
        expected_architecture: str,
        expected_runtime_interface: str,
        expected_archive_sha256: str,
        expected_archive_bytes: int,
    ) -> PulledImageEvidence:
        return self._inspector.inspect_archive(
            archive,
            expected_architecture=expected_architecture,
            expected_runtime_interface=expected_runtime_interface,
            expected_archive_sha256=expected_archive_sha256,
            expected_archive_bytes=expected_archive_bytes,
        )


def _local_skopeo() -> str:
    executable = shutil.which("skopeo")
    if executable is None:
        pytest.skip(
            "Skopeo is required to verify the disposable local OCI walkthrough image"
        )
    return executable


def _local_oci_archive(
    root: Path,
    *,
    architecture: str,
    runtime_interface: str,
    skopeo: str,
) -> tuple[Path, SkopeoOCIImageTransport, PulledImageEvidence]:
    """Build a tiny local OCI layout and inspect its converted archive via Skopeo."""

    os_name, cpu = architecture.split("/", 1)
    interface_label = runtime_interface.removeprefix("vonk.runtime.")
    layout = root / "source-oci-layout"
    blobs = layout / "blobs" / "sha256"
    blobs.mkdir(parents=True)

    config = {
        "architecture": cpu,
        "os": os_name,
        "config": {"Labels": {"ai.vonkforge.runtime-interface": interface_label}},
        "rootfs": {"type": "layers", "diff_ids": []},
        "history": [],
    }
    config_bytes = json.dumps(config, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    config_digest = hashlib.sha256(config_bytes).hexdigest()
    (blobs / config_digest).write_bytes(config_bytes)

    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": f"sha256:{config_digest}",
            "size": len(config_bytes),
        },
        "layers": [],
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    (blobs / manifest_digest).write_bytes(manifest_bytes)
    (layout / "oci-layout").write_text(
        '{"imageLayoutVersion":"1.0.0"}\n', encoding="utf-8"
    )
    index = {
        "schemaVersion": 2,
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": f"sha256:{manifest_digest}",
                "size": len(manifest_bytes),
                "platform": {"architecture": cpu, "os": os_name},
                "annotations": {"org.opencontainers.image.ref.name": "walkthrough"},
            }
        ],
    }
    (layout / "index.json").write_text(
        json.dumps(index, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    archive = root / "source-image.tar"
    converted = subprocess.run(
        [
            skopeo,
            "copy",
            f"oci:{layout}:walkthrough",
            f"docker-archive:{archive}:registry.example/walkthrough/synthetic-runtime:fixture",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if converted.returncode != 0:
        pytest.fail(
            "Skopeo could not convert the local OCI layout: "
            + (converted.stderr.strip() or converted.stdout.strip()),
            pytrace=False,
        )

    inspector = SkopeoOCIImageTransport(executable=skopeo)
    archive_bytes = archive.stat().st_size
    archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    evidence = inspector.inspect_archive(
        archive,
        expected_architecture=architecture,
        expected_runtime_interface=runtime_interface,
        expected_archive_sha256=archive_sha256,
        expected_archive_bytes=archive_bytes,
    )
    return archive, inspector, evidence


class _AuthorizationHeaders(dict[str, str]):
    """Keep pytest's failure-local display from printing the short-lived token."""

    def __repr__(self) -> str:
        return "{'Authorization': 'Bearer <redacted>'}"


def _session_environment(
    *,
    installed_vonkctl: Path,
    workspace: Path,
    url: str,
    certificate: Path,
    headers: dict[str, str],
) -> dict[str, str]:
    """Keep only the wheel, local Controller trust, and private credential."""

    generated = _process_environment(workspace, url, certificate, headers)
    home = workspace / "operator-home"
    home.mkdir(mode=0o700)
    cli_temp = workspace / "operator-tmp"
    cli_temp.mkdir(mode=0o700)
    path = os.pathsep.join(
        (
            str(installed_vonkctl.parent),
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        )
    )
    return {
        "HOME": str(home),
        "PATH": path,
        "LANG": "C.UTF-8",
        "PYTHONPATH": "",
        "PYTHONNOUSERSITE": "1",
        "TMPDIR": str(cli_temp),
        "TEMP": str(cli_temp),
        "HISTFILE": "/dev/null",
        "HISTSIZE": "0",
        "HISTFILESIZE": "0",
        "VONK_CONTROL_URL": generated["VONK_CONTROL_URL"],
        "VONK_CONTROL_TOKEN_FILE": generated["VONK_CONTROL_TOKEN_FILE"],
        "VONK_CLI_UPDATE_NOTICES": "0",
        "VONK_RECIPE_LIBRARY_ROOT": generated["VONK_RECIPE_LIBRARY_ROOT"],
        "SSL_CERT_FILE": generated["SSL_CERT_FILE"],
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }


def _stage_participant_materials(
    workspace: Path,
    *,
    controller_url: str,
    valid_token_file: Path,
    expired_token: str,
) -> Path:
    """Stage the shipped runbook and the two connection cases for U1."""

    materials = workspace / "participant-materials"
    materials.mkdir(mode=0o700)
    repository_root = Path(__file__).resolve().parents[2]
    runbook_source = repository_root / "docs" / "runbooks" / "vonkctl.md"
    runbook_copy = materials / "vonkctl.md"
    shutil.copyfile(runbook_source, runbook_copy)
    runbook_copy.chmod(0o600)

    valid_connection = materials / "valid-connection.env"
    valid_connection.write_text(
        f"export VONK_CONTROL_URL='{controller_url}'\n"
        f"export VONK_CONTROL_TOKEN_FILE='{valid_token_file}'\n",
        encoding="utf-8",
    )
    valid_connection.chmod(0o600)

    invalid_connection = materials / "invalid-connection.env"
    invalid_connection.write_text(
        "export VONK_CONTROL_URL='https://127.0.0.1:1'\n"
        f"export VONK_CONTROL_TOKEN_FILE='{valid_token_file}'\n",
        encoding="utf-8",
    )
    invalid_connection.chmod(0o600)

    expired_token_file = materials / "expired-token"
    expired_token_file.write_text(expired_token, encoding="utf-8")
    expired_token_file.chmod(0o600)
    expired_connection = materials / "expired-token.env"
    expired_connection.write_text(
        f"export VONK_CONTROL_URL='{controller_url}'\n"
        f"export VONK_CONTROL_TOKEN_FILE='{expired_token_file}'\n",
        encoding="utf-8",
    )
    expired_connection.chmod(0o600)
    return materials


def _run_cli(
    executable: Path,
    arguments: tuple[str, ...],
    environment: dict[str, str],
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [str(executable), *arguments],
        env=environment,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    private_token = Path(environment["VONK_CONTROL_TOKEN_FILE"]).read_text(
        encoding="utf-8"
    )
    if private_token in result.stdout or private_token in result.stderr:
        pytest.fail(
            "the installed CLI exposed the private walkthrough credential",
            pytrace=False,
        )
    return result


def _walkthrough_app(
    postgres_engine,
    owner_root: Path,
    *,
    linked_profile: bool = False,
    interactive_clock: bool = False,
    qwen_cpu_assets: _QwenCPUAssets | None = None,
):
    clock, advance_clock = _walkthrough_clock(interactive=interactive_clock)

    def now() -> int:
        return int(datetime.now(UTC).timestamp())

    base_model_document = json.loads(
        resources.files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text(encoding="utf-8")
    )
    base_source = base_model_document["source"]
    assert isinstance(base_source, dict)
    base_source["repository"] = "https://huggingface.co/vonk-forge/synthetic-tiny"
    base_model = ModelDefinition.model_validate(base_model_document)
    base_model_digest = content_sha256(base_model)

    def transform_base_model(document: dict[str, object]) -> None:
        document.update(base_model.model_dump(mode="json"))

    def bind_base_model(document: dict[str, object]) -> None:
        identity = document["identity"]
        metadata = document["metadata"]
        assert isinstance(identity, dict) and isinstance(metadata, dict)
        identity["slug"] = _SYNTHETIC_BASE_RECIPE_SELECTOR
        metadata["title"] = "Walkthrough synthetic source-build base"
        selections = document["models"]
        assert isinstance(selections, list) and selections
        selection = selections[0]
        assert isinstance(selection, dict)
        reference = selection["model"]
        assert isinstance(reference, dict)
        reference["content_sha256"] = base_model_digest

    sessions, lifecycle, _, _, _, node_ids = setup_services(
        owner_root,
        nodes=1,
        engine=postgres_engine,
        model_transform=transform_base_model,
        recipe_transform=bind_base_model,
    )
    node_id = node_ids[0]
    with sessions.begin() as session:
        session.add(User(subject="test", role="administrator"))
        if linked_profile:
            node = session.get(AgentNode, node_id)
            if node is None:
                raise AssertionError("linked walkthrough agent node is missing")
            node.capabilities = sorted(
                set(node.capabilities or ())
                | {
                    "runtime.preflight.v1",
                    "recipe.run.inspect.exact.v1",
                    f"runtime.preflight.fingerprint.{_LINKED_PREFLIGHT_FINGERPRINT}",
                }
            )
            node.observation_receipt_public_key = (
                RECEIPT_SIGNER.public_key().public_bytes_raw().hex()
            )
        session.add(
            AgentNodeProfile(
                node_id=node_id,
                display_name=(
                    "OrbStack ARM64 CPU" if qwen_cpu_assets is not None else "Spark One"
                ),
                hostname=(
                    "orbstack-arm64-cpu" if qwen_cpu_assets is not None else "spark-one"
                ),
            )
        )
    if qwen_cpu_assets is not None:
        # The opt-in CPU fixture describes only its disposable container target.
        # Its resource observation is sized from the bounded local engine run,
        # not a Spark measurement or physical acceptance claim.
        InventoryRepository(sessions, clock=clock).record(
            InventorySnapshotInput(
                node_id=node_id,
                observed_at=NOW + timedelta(seconds=1),
                disk_total_bytes=64 * 1024**3,
                disk_free_bytes=48 * 1024**3,
                host_memory_total_bytes=12 * 1024**3,
                host_memory_free_bytes=10 * 1024**3,
                gpu_memory_total_bytes=0,
                gpu_memory_free_bytes=0,
                gpu_count=0,
                artifact_store_read_only=False,
                capabilities=("runtime.vonk.v1", "recipe.operations.v1"),
                memory_pool="separate",
            )
        )

    authority = DatabaseAuthorityService(sessions, clock=clock)
    authority.ensure_initialized()
    codec = TokenCodec(os.urandom(32))
    cursor_codec = codec.cursor_codec()
    catalog = CatalogEntityService(sessions, clock=clock, cursors=cursor_codec)

    # setup_services seeds one canonical, capacity-fitting source-build recipe
    # and its exact model revision. Publish those existing rows as their
    # current catalog heads. Add separate later-page recipes through the
    # catalog owner: one remains cache-blocked and one is prepared through the
    # real model-cache and runtime-image owners below.
    with sessions.begin() as session:
        revisions = tuple(session.scalars(select(CatalogDocumentRevision)))
        for revision in revisions:
            session.add(
                CatalogDocumentHead(
                    kind=revision.kind,
                    publisher=revision.publisher,
                    slug=revision.slug,
                    active_revision_id=revision.id,
                )
            )
        base_recipe_row = next(
            row
            for row in revisions
            if row.kind == "recipe" and row.slug == _SYNTHETIC_BASE_RECIPE_SELECTOR
        )
        base_recipe_document = copy.deepcopy(base_recipe_row.document)
    model_document = json.loads(
        resources.files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text(encoding="utf-8")
    )
    model_identity = model_document["identity"]
    assert isinstance(model_identity, dict)
    model_identity["publisher"] = "walkthrough"
    model_identity["slug"] = "walkthrough-synthetic-tiny-fp16"
    model_document_identity = model_identity["model"]
    model_family = model_identity["family"]
    assert isinstance(model_document_identity, dict) and isinstance(model_family, dict)
    model_document_identity["publisher"] = "walkthrough"
    model_document_identity["slug"] = "walkthrough-synthetic-tiny"
    model_family["publisher"] = "walkthrough"
    model_family["slug"] = "walkthrough-synthetic"
    model_source = model_document["source"]
    assert isinstance(model_source, dict)
    model_source["repository"] = (
        "https://huggingface.co/walkthrough/walkthrough-synthetic-tiny"
    )
    lineage = model_document["lineage"]
    assert isinstance(lineage, dict)
    lineage["publisher"] = "walkthrough"
    model = ModelDefinition.model_validate(model_document)
    model_draft = catalog.create_draft(model.model_dump(mode="json"), actor="admin")
    catalog.resolve(model_draft.id, actor="admin")

    if qwen_cpu_assets is None:
        ready_model_document = copy.deepcopy(model_document)
        ready_model_identity = ready_model_document["identity"]
        assert isinstance(ready_model_identity, dict)
        ready_model_identity["slug"] = _READY_MODEL_SELECTOR
        ready_model_model = ready_model_identity["model"]
        ready_model_family = ready_model_identity["family"]
        assert isinstance(ready_model_model, dict) and isinstance(
            ready_model_family, dict
        )
        ready_model_model["slug"] = "walkthrough-synthetic-tiny-ready"
        ready_model_family["slug"] = "walkthrough-synthetic-ready"
        ready_model_source = ready_model_document["source"]
        assert isinstance(ready_model_source, dict)
        ready_model_source["repository"] = (
            "https://huggingface.co/walkthrough/walkthrough-synthetic-tiny-ready"
        )
        ready_model_source["revision"] = _READY_MODEL_REVISION
        ready_model_document["files"] = [
            {
                "id": "weights",
                "path": "model.safetensors",
                "roles": ["weights"],
                "sha256": hashlib.sha256(_READY_MODEL_PAYLOAD).hexdigest(),
                "size_bytes": len(_READY_MODEL_PAYLOAD),
            }
        ]
        ready_model = ModelDefinition.model_validate(ready_model_document)
    else:
        ready_model = qwen_cpu_assets.model
    ready_model_digest = content_sha256(ready_model)
    ready_model_draft = catalog.create_draft(
        ready_model.model_dump(mode="json"), actor="admin"
    )
    ready_model_revision = catalog.resolve(ready_model_draft.id, actor="admin")
    assert ready_model_revision.content_digest == ready_model_digest

    image_example = json.loads(
        resources.files("vonk_forge_contracts")
        .joinpath("examples", "recipe-image.json")
        .read_text(encoding="utf-8")
    )

    ready_recipe_document = copy.deepcopy(base_recipe_document)
    ready_identity = ready_recipe_document["identity"]
    ready_metadata = ready_recipe_document["metadata"]
    assert isinstance(ready_identity, dict) and isinstance(ready_metadata, dict)
    ready_identity["slug"] = (
        _QWEN3_READY_RECIPE_SELECTOR
        if qwen_cpu_assets is not None
        else _READY_RECIPE_SELECTOR
    )
    ready_metadata["title"] = (
        "Qwen3 0.6B vLLM CPU ready candidate"
        if qwen_cpu_assets is not None
        else "Walkthrough synthetic vLLM ready candidate"
    )
    if qwen_cpu_assets is not None:
        ready_metadata["description"] = (
            "Opt-in later-page candidate bound to pinned Qwen3-0.6B BF16 "
            "weights and an inspected ARM64 CPU vLLM image."
        )
    ready_recipe_document["execution"] = copy.deepcopy(image_example["execution"])
    ready_execution = ready_recipe_document["execution"]
    assert isinstance(ready_execution, dict)
    ready_image = ready_execution["image"]
    assert isinstance(ready_image, dict)
    ready_image["repository"] = (
        _QWEN3_RUNTIME_REPOSITORY
        if qwen_cpu_assets is not None
        else "registry.example/walkthrough/synthetic-runtime"
    )
    # Compile a canonical temporary candidate to learn the runtime contract;
    # the actual catalog revision is pinned to Skopeo's observed digest below.
    ready_image["digest"] = "0" * 64
    ready_selections = ready_recipe_document["models"]
    assert isinstance(ready_selections, list) and ready_selections
    ready_selection = ready_selections[0]
    assert isinstance(ready_selection, dict)
    ready_model_reference = ready_selection["model"]
    assert isinstance(ready_model_reference, dict)
    ready_model_reference["publisher"] = ready_model.identity.publisher
    ready_model_reference["slug"] = ready_model.identity.slug
    ready_model_reference["content_sha256"] = ready_model_digest
    ready_selection["files"] = [
        {
            "id": model_file.id,
            "file_id": model_file.id,
            "roles": ["entrypoint"],
            "mount": {"target": "/models", "read_only": True},
        }
        for model_file in ready_model.files
    ]
    if qwen_cpu_assets is not None:
        ready_runtime = ready_recipe_document["runtime"]
        ready_interfaces = ready_recipe_document["interfaces"]
        ready_validation_document = ready_recipe_document["validation"]
        ready_topology = ready_recipe_document["topology"]
        assert isinstance(ready_interfaces, list) and ready_interfaces
        assert isinstance(ready_validation_document, dict)
        assert isinstance(ready_topology, dict)
        ready_roles = ready_topology["roles"]
        assert isinstance(ready_roles, list) and ready_roles
        ready_interface = ready_interfaces[0]
        ready_validation = ready_validation_document["serving"]
        ready_settings = ready_recipe_document["settings"]
        ready_role = ready_roles[0]
        assert (
            isinstance(ready_runtime, dict)
            and isinstance(ready_interface, dict)
            and isinstance(ready_validation, dict)
            and isinstance(ready_settings, dict)
            and isinstance(ready_role, dict)
        )
        ready_runtime["entrypoint"] = ["/opt/vonk/bin/vllm", "serve", "/models"]
        ready_runtime["arguments"] = [
            {"name": "dtype", "value": "float32"},
            {"name": "max-model-len", "value": 128},
            {"name": "max-num-seqs", "value": 1},
            {"name": "served-model-name", "value": "qwen3-0.6b"},
        ]
        ready_interface["model_aliases"] = ["qwen3-0.6b"]
        ready_validation["checks"] = [
            {
                "name": "text-generation",
                "kind": "openai.chat",
                "request": {
                    "transport": "http",
                    "method": "POST",
                    "path": "/v1/chat/completions",
                    "body": {
                        "model": "$MODEL",
                        "messages": [{"role": "user", "content": "Reply with READY."}],
                        "max_tokens": 16,
                        "chat_template_kwargs": {"enable_thinking": False},
                    },
                },
                "assertions": ["chat.nonempty", "chat.output-cap"],
            }
        ]
        ready_settings["context_tokens"] = {
            "value": 128,
            "change_effect": "restart",
        }
        ready_role["resources"] = {
            "disk": {
                "image_bytes": 5 * 1024**3,
                "artifact_bytes": ready_model.installed_bytes,
                "staging_bytes": 4 * 1024**3,
                "cache_bytes": 2 * 1024**3,
                "rollback_bytes": 0,
                "safety_margin_bytes": 1 * 1024**3,
            },
            "memory": {
                "kind": "host",
                "startup_peak_bytes": 7 * 1024**3,
                "steady_state_bytes": 4 * 1024**3,
                "runtime_growth_bytes": 1 * 1024**3,
                "system_reserve_bytes": 1 * 1024**3,
            },
        }
    unpinned_ready_recipe = RecipeDefinition.model_validate(ready_recipe_document)
    with sessions() as session:
        resolved_ready_entities = resolve_recipe_entities(
            session, unpinned_ready_recipe.model_dump(mode="json")
        )
    ready_role = unpinned_ready_recipe.topology.roles[0]
    ready_projection = compile_runtime_spec(
        unpinned_ready_recipe,
        resolved_entities=resolved_ready_entities,
        role=ready_role.name,
        rank=0,
    )
    ready_runtime = ready_projection.get("runtime")
    if not isinstance(ready_runtime, dict):
        raise TypeError("canonical ready-recipe runtime is unavailable")
    expectations = runtime_image_expectations(ready_runtime)
    skopeo = _local_skopeo()
    if qwen_cpu_assets is None:
        source_archive, image_inspector, image_evidence = _local_oci_archive(
            owner_root / "image-fixture",
            architecture=expectations["architecture"],
            runtime_interface=expectations["interface"],
            skopeo=skopeo,
        )
    else:
        source_archive = qwen_cpu_assets.runtime_archive
        image_inspector = SkopeoOCIImageTransport(executable=skopeo)
        image_evidence = image_inspector.inspect_archive(
            source_archive,
            expected_architecture=expectations["architecture"],
            expected_runtime_interface=expectations["interface"],
            expected_archive_sha256=_QWEN3_ARCHIVE_SHA256,
            expected_archive_bytes=_QWEN3_ARCHIVE_BYTES,
        )
    expected_image_digest = image_evidence.manifest_digest
    assert expected_image_digest.startswith("sha256:")
    ready_image["digest"] = expected_image_digest.removeprefix("sha256:")
    ready_recipe = RecipeDefinition.model_validate(ready_recipe_document)
    ready_draft = catalog.create_draft(
        ready_recipe.model_dump(mode="json"), actor="admin"
    )
    ready_revision = catalog.resolve(ready_draft.id, actor="admin")

    candidate_recipe_document = (
        copy.deepcopy(ready_recipe_document)
        if qwen_cpu_assets is not None
        else base_recipe_document
    )
    if qwen_cpu_assets is None:
        candidate_recipe_document["execution"] = image_example["execution"]
    identity = candidate_recipe_document["identity"]
    metadata = candidate_recipe_document["metadata"]
    assert isinstance(identity, dict) and isinstance(metadata, dict)
    identity["slug"] = (
        _QWEN3_BLOCKED_RECIPE_SELECTOR
        if qwen_cpu_assets is not None
        else _BLOCKED_RECIPE_SELECTOR
    )
    metadata["title"] = (
        "Qwen3 0.6B vLLM CPU candidate with missing fixture image"
        if qwen_cpu_assets is not None
        else "Walkthrough synthetic vLLM later-page candidate"
    )
    selections = candidate_recipe_document["models"]
    assert isinstance(selections, list) and selections
    selection = selections[0]
    assert isinstance(selection, dict)
    model_reference = selection["model"]
    assert isinstance(model_reference, dict)
    candidate_model = ready_model if qwen_cpu_assets is not None else model
    model_reference["publisher"] = candidate_model.identity.publisher
    model_reference["slug"] = candidate_model.identity.slug
    model_reference["content_sha256"] = content_sha256(candidate_model)
    if qwen_cpu_assets is not None:
        execution = candidate_recipe_document["execution"]
        assert isinstance(execution, dict)
        image = execution["image"]
        assert isinstance(image, dict)
        image["digest"] = hashlib.sha256(
            b"walkthrough Qwen fixture image is intentionally unavailable"
        ).hexdigest()
        selection["files"] = [
            {
                "id": model_file.id,
                "file_id": model_file.id,
                "roles": ["entrypoint"],
                "mount": {"target": "/models", "read_only": True},
            }
            for model_file in candidate_model.files
        ]
    candidate = RecipeDefinition.model_validate(candidate_recipe_document)
    candidate_draft = catalog.create_draft(
        candidate.model_dump(mode="json"), actor="admin"
    )
    candidate_revision = catalog.resolve(candidate_draft.id, actor="admin")

    runtime_storage = FilesystemRuntimeImageStorage(owner_root / "runtime-images")
    model_requests: list[str] = []
    expected_model_paths = (
        {
            f"/Qwen/Qwen3-0.6B/resolve/{_QWEN3_REVISION}/{path}": source
            for path, source in qwen_cpu_assets.source_files.items()
        }
        if qwen_cpu_assets is not None
        else {
            "/walkthrough/walkthrough-synthetic-tiny-ready/resolve/"
            f"{_READY_MODEL_REVISION}/model.safetensors": None
        }
    )

    def serve_only_ready_model_artifact(request: httpx.Request) -> httpx.Response:
        model_requests.append(f"{request.method} {request.url.host}{request.url.path}")
        if request.method != "GET" or request.url.host != "huggingface.co":
            return httpx.Response(403, request=request)
        source = expected_model_paths.get(request.url.path)
        if request.url.path not in expected_model_paths:
            return httpx.Response(403, request=request)
        if source is None:
            return httpx.Response(200, content=_READY_MODEL_PAYLOAD, request=request)
        return httpx.Response(
            200,
            stream=_FileResponseStream(source),
            request=request,
        )

    with httpx.Client(
        transport=httpx.MockTransport(serve_only_ready_model_artifact),
        follow_redirects=False,
    ) as model_source:
        # fixture_sources intentionally stays at ModelCacheService's secure
        # catalog-only default; the MockTransport refuses every other source.
        model_cache = ModelCacheService(
            sessions,
            owner_root / "model-cache",
            reserve_bytes=0,
            clock=clock,
            http_client=model_source,
            runtime_archive_available=runtime_storage.build_archive_available,
        )
        model_preview = model_cache.download_preview(
            model_content_sha256=ready_model_digest
        )
        assert model_preview["blockers"] == []
        assert model_preview["new_bytes"] == ready_model.download_bytes
        model_operation = model_cache.start_download(
            actor="walkthrough-setup",
            request_key="22222222-2222-4222-8222-222222222201",
            plan_digest=str(model_preview["plan_digest"]),
            model_content_sha256=ready_model_digest,
        )
        assert model_operation.state == "queued"
        assert model_cache.run_pending(limit=1) == 1
        model_operation = model_cache.get_operation(model_operation.id)
        assert model_operation.state == "succeeded"
        assert set(model_requests) == {
            f"GET huggingface.co{path}" for path in expected_model_paths
        }
        model_manifest = model_cache.resolve_artifact_set(
            model_content_sha256=ready_model_digest
        )
        assert len(model_manifest.artifacts) == len(ready_model.files)
        for model_artifact in model_manifest.artifacts:
            managed_object = model_cache._object_path(model_artifact.sha256)
            assert managed_object.is_file()
            assert managed_object.stat().st_size == model_artifact.expected_bytes
            receipt_path = model_cache._receipt_path(model_artifact.sha256)
            assert receipt_path.is_file()
            receipt = ModelCacheObjectReceipt.model_validate_json(
                receipt_path.read_bytes()
            )
            assert receipt.sha256 == model_artifact.sha256
            assert receipt.expected_bytes == model_artifact.expected_bytes
            assert receipt.actual_bytes == model_artifact.expected_bytes
            if qwen_cpu_assets is None:
                assert managed_object.read_bytes() == _READY_MODEL_PAYLOAD
                assert hashlib.sha256(managed_object.read_bytes()).hexdigest() == (
                    model_artifact.sha256
                )
        assert (
            model_cache.download_preview(model_content_sha256=ready_model_digest)[
                "new_bytes"
            ]
            == 0
        )

    # Match the production capability graph for placement assessment. This
    # wires real durable AgentJob and verified-object owners, but no scheduler
    # is started and LibraryAssessment only inspects a plan.
    agent_operations = AgentJobService(sessions, clock=clock)
    runtime_image_preparer = make_runtime_image_receipt_preparer(
        sessions,
        runtime_storage,
        SkopeoOCIImageTransport(),
        clock=clock,
    )

    def resolve_runtime_image_receipt(document, image_digest, runtime_spec):
        runtime = runtime_spec.get("runtime")
        if not isinstance(runtime, dict):
            raise TypeError("runtime image projection is unavailable")
        expectations = runtime_image_expectations(runtime)
        receipt = runtime_storage.find_verified(
            image_digest,
            expected_architecture=expectations["architecture"],
            expected_runtime_interface=expectations["interface"],
        )
        if receipt is None:
            raise ValueError("prepared runtime image receipt is unavailable")
        identity = runtime_spec.get("identity")
        execution_key = (
            identity.get("execution_sha256") if isinstance(identity, dict) else None
        )
        recipe_digest = content_sha256(
            RecipeDefinition.model_validate_json(
                canonical_message(document), strict=True
            )
        )
        if not isinstance(execution_key, str):
            raise TypeError("runtime image execution identity is unavailable")
        with sessions() as session:
            revision = session.scalar(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.state == "active",
                    CatalogDocumentRevision.content_digest == recipe_digest,
                )
            )
            if revision is None or revision.content_digest is None:
                raise ValueError("active recipe revision is unavailable")
            resolve_persisted_runtime_image_receipt(
                session,
                recipe_revision_id=revision.id,
                current_content_digest=revision.content_digest,
                effective_execution_key=execution_key,
                receipt=receipt,
            )
        return receipt

    routes: RecipeRouteService | None = None
    route_root: Path | None = None
    if linked_profile:
        route_root = owner_root / "route-bundles"
        routes = RecipeRouteService(
            sessions,
            publisher=AtomicRecipeRoutePublisher(
                AtomicRouteBundlePublisher(route_root, clock=clock), clock=clock
            ),
            management_policy=ManagementAddressPolicy.parse("192.168.1.0/24"),
            clock=clock,
            maximum_age_seconds=120,
        )
        # setup_services supplies the same admissions and PostgreSQL schema as
        # the existing recipe-operation tests, while this connected journey
        # replaces their recording queue with the real claim/result owner.
        lifecycle = RecipeOperationService(
            sessions,
            install_admission=InstallAdmissionService(
                sessions,
                inventory_max_age=300,
                disk_floor_bytes=10,
                compiled_plan_provider=ControllerExecutionPlanService(
                    model_cache,
                    runtime_image_resolver=resolve_runtime_image_receipt,
                ).compile_installation,
                runtime_image_authorizer=authorize_installation_runtime_images,
            ),
            run_admission=RunAdmissionService(
                sessions, inventory_max_age=300, memory_floor_bytes=50
            ),
            agent_jobs=agent_operations,
            clock=clock,
            route_publications=routes,
            builds=lifecycle._builds,
            mappings=lifecycle._mappings,
            distributed_start_timeout_seconds=(
                lifecycle._distributed_start_timeout_seconds
            ),
        )
        agent_operations.set_result_consumer(lifecycle.consume_agent_result)

    distribution = build_distribution_service_from_components(
        model_cache,
        sessions,
        owner_root / "runtime-images",
        clock=clock,
    )
    artifact_phase_executor = CompositeDistributionPhaseExecutor(
        sessions,
        agent_operations,
        distribution,
        model_cache=model_cache,
        runtime_image_preparer=runtime_image_preparer if linked_profile else None,
        clock=clock,
    )
    run_switch = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        model_cache=model_cache,
        clock=clock,
        build_archive_available=runtime_storage.build_archive_available,
        published_image_receipt=runtime_storage.find_published,
        artifact_phase_executor=artifact_phase_executor,
        memory_floor_bytes=50,
    )
    assessment = LibraryAssessment(
        sessions,
        run_switch=run_switch,
        model_cache=model_cache,
        clock=clock,
    )
    fleet_projection = FleetProjection(authority, sessions, clock=clock)
    library_projection = LibraryProjection(
        sessions,
        cursors=cursor_codec,
        clock=clock,
        runtime_archive_available=runtime_storage.build_archive_available,
        assessment=assessment,
    )
    profiles = (
        build_production_fleet_profile_service(
            sessions,
            clock=clock,
            run_switch_operations=run_switch,
            cache_resolver=model_cache.resolve_latest_cached,
        )
        if linked_profile
        else FleetProfileService(
            sessions,
            clock=clock,
            cache_resolver=model_cache.resolve_latest_cached,
            assessment_provider=RunSwitchFleetProfileAdapter(
                sessions, run_switch
            ).assess,
        )
    )

    def resolve_recipe(
        recipe_revision_id: str, *, force: bool = False
    ) -> tuple[RecipeDefinition, dict[str, object]]:
        del force
        with sessions() as session:
            revision = session.get(CatalogDocumentRevision, recipe_revision_id)
            if (
                revision is None
                or revision.kind != "recipe"
                or revision.state != "active"
            ):
                raise ValueError("selected recipe revision is not active")
            recipe = RecipeDefinition.model_validate(revision.document)
            resolved = resolve_recipe_entities(session, revision.document)
        role = recipe.topology.roles[0]
        projection = compile_runtime_spec(
            recipe,
            resolved_entities=resolved,
            role=role.name,
            rank=0,
        )
        runtime = projection.get("runtime")
        if not isinstance(runtime, dict):
            raise TypeError("canonical recipe runtime is unavailable")
        return recipe, runtime

    recipe_preparation = RecipeImageAvailabilityService(
        sessions,
        storage=runtime_storage,
        authority=resolve_recipe,
        transport=_LocalOCIFileTransport(source_archive, image_inspector),
        clock=clock,
        model_cache=model_cache,
    )
    image_operation = recipe_preparation.start(
        ready_revision.id,
        actor="walkthrough-setup",
        request_id="22222222-2222-4222-8222-222222222202",
    )
    assert image_operation.state == "queued"
    assert recipe_preparation.run_pending(limit=1) == 1
    image_operation = recipe_preparation.get(image_operation.id)
    assert image_operation.state == "succeeded"
    assert image_operation.result is not None
    assert image_operation.result["registry_manifest_digest"] == expected_image_digest
    assert image_operation.result["platform_manifest_digest"] == expected_image_digest
    archive_digest = image_operation.result["oci_archive_sha256"]
    archive_bytes = image_operation.result["image_bytes"]
    assert isinstance(archive_digest, str) and type(archive_bytes) is int
    stored_image = runtime_storage.verify_existing(archive_digest, archive_bytes)
    assert stored_image.is_file()
    with stored_image.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == archive_digest
    linked_owners: _LinkedProfileOwners | None = None
    linked_operations = None
    if linked_profile:
        if routes is None or route_root is None:
            raise AssertionError("linked profile route owner was not initialized")
        linked_operations = durable_operation_services(
            sessions,
            route_root,
            clock=clock,
            cursors=cursor_codec,
            operation_providers=(
                profiles.operation_provider(),
                run_switch.activity_provider(),
            ),
            profile_endpoint_intent=profiles.endpoint_intent,
        )
        linked_owners = _LinkedProfileOwners(
            sessions=sessions,
            agent_jobs=agent_operations,
            recipe_operations=lifecycle,
            run_switch_operations=run_switch,
            profiles=profiles,
            routes=routes,
            worker=RecipeOperationWorker(
                sessions,
                routes,
                clock=clock,
                fleet_profiles=profiles,
                run_switches=run_switch,
            ),
            model_cache=model_cache,
            distribution=distribution,
            image_inspector=image_inspector,
            image_evidence=image_evidence,
            target_root=owner_root / "simulated-target" / node_id,
            events=[],
            clock=clock,
            interactive_clock=interactive_clock,
            advance_clock=advance_clock,
        )
    agent_api_services = None
    if linked_profile:
        agent_roots = {
            name: owner_root / "agent" / name
            for name in ("artifacts", "source-bundles", "tuf-metadata", "tuf-targets")
        }
        for root in agent_roots.values():
            root.mkdir(parents=True, exist_ok=True)
        agent_api_services = AgentApiServices(
            enrollment=None,
            operations=agent_operations,
            sessions=sessions,
            clock=clock,
            presence=AgentPresenceService(
                sessions, ManagementAddressPolicy.parse("10.0.0.0/24"), clock=clock
            ),
            artifact_root=agent_roots["artifacts"],
            source_bundles=SourceBundleStore(agent_roots["source-bundles"]),
            workload_tuf_metadata_root=agent_roots["tuf-metadata"],
            workload_tuf_target_root=agent_roots["tuf-targets"],
            host_runtime_authority=HostRuntimeAuthorityService(
                sessions,
                HostHelperGrantIssuer(
                    ed25519.Ed25519PrivateKey.from_private_bytes(b"g" * 32),
                    clock=clock,
                ),
                clock=clock,
            ),
            fabric_policy=ManagementAddressPolicy.parse("192.168.100.0/24"),
        )
    app = create_app(
        jobs=JobService(sessions, clock=clock, cursors=cursor_codec),
        tokens=codec,
        audits=SqlAuditStore(sessions, clock=clock),
        fleet_projection=fleet_projection,
        library_projection=library_projection,
        fleet_profiles=profiles,
        recipe_operations=lifecycle if linked_profile else None,
        run_switch_operations=run_switch if linked_profile else None,
        operations=linked_operations,
        model_cache=model_cache,
        recipe_image_availability=recipe_preparation,
        now=now,
        agent=agent_api_services,
        trusted_agent_proxy_auth=b"p" * 32,
    )
    if linked_owners is not None:
        app.state.linked_profile_owners = linked_owners
    actor = Actor("test", "administrator")
    token = codec.issue(actor, ttl_seconds=4 * 60 * 60, now=now())
    expired_token = codec.issue(
        Actor("expired-walkthrough", "administrator"), ttl_seconds=1, now=0
    )
    app.state.walkthrough_expired_token = expired_token
    return (
        sessions,
        app,
        _AuthorizationHeaders(Authorization=f"Bearer {token}"),
        node_id,
        candidate_revision.id,
        candidate_revision.content_digest,
        ready_revision.id,
        ready_revision.content_digest,
    )


def _effect_counts(sessions) -> tuple[int, int, int]:
    with sessions() as session:
        return (
            session.scalar(select(func.count()).select_from(Job)) or 0,
            session.scalar(select(func.count()).select_from(FleetProfileApplication))
            or 0,
            session.scalar(select(func.count()).select_from(RecipeRun)) or 0,
        )


def _smoke(
    *,
    executable: Path,
    environment: dict[str, str],
    cwd: Path,
    sessions,
    api: TestClient,
    api_headers: dict[str, str],
    peer_calls: list[tuple[str, str, object]],
    node_id: str,
    candidate_revision_id: str,
    candidate_digest: str,
    ready_revision_id: str,
    ready_digest: str,
    participant_materials: Path,
) -> None:
    before_effects = _effect_counts(sessions)
    help_result = _run_cli(executable, ("--help",), environment, cwd)
    assert help_result.returncode == 0, help_result.stderr

    def check_connection_case(case: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "/bin/sh",
                "-c",
                '. "$1"; exec "$2" --check-connection --json',
                "walkthrough-connection-case",
                str(participant_materials / case),
                str(executable),
            ],
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    invalid_origin = check_connection_case("invalid-connection.env")
    assert invalid_origin.returncode == 2
    invalid_error = json.loads(invalid_origin.stdout)
    assert invalid_error["error_type"] == "control_api"
    assert invalid_error["code"] == "controller.transport_connect"
    assert invalid_error["operation"] == "GET /api/fleet"
    assert invalid_error["endpoint"] == "/api/fleet"
    assert invalid_error["source"] == "transport"
    assert invalid_error["transport"] == "connect"
    assert invalid_error["decision"] == "retry"
    assert invalid_error["retryable"] is True

    expired_auth = check_connection_case("expired-token.env")
    assert expired_auth.returncode == 2
    expired_error = json.loads(expired_auth.stdout)
    assert expired_error["error_type"] == "control_api"
    assert expired_error["code"] == "controller.authentication_required"
    assert expired_error["operation"] == "GET /api/fleet"
    assert expired_error["endpoint"] == "/api/fleet"
    assert expired_error["source"] == "remote_rejection"
    assert expired_error["http_status"] == 401

    restored_connection = check_connection_case("valid-connection.env")
    assert restored_connection.returncode == 0, restored_connection.stderr

    fleet = _run_cli(executable, ("fleet", "--json"), environment, cwd)
    assert fleet.returncode == 0, fleet.stderr
    fleet_result = json.loads(fleet.stdout)
    assert fleet_result["nodes"]

    first_page = _run_cli(
        executable,
        (
            "--no-input",
            "--json",
            "recipe",
            "library",
            "--all-models",
            "--fits-fleet",
            "--limit",
            "1",
            "--sort",
            "name",
        ),
        environment,
        cwd,
    )
    assert first_page.returncode == 0, first_page.stdout + first_page.stderr
    first_document = json.loads(first_page.stdout)
    first_rows = first_document.get("recipes")
    cursor = first_document.get("next_cursor")
    assert isinstance(first_rows, list) and len(first_rows) == 1
    assert isinstance(cursor, str) and cursor
    assert first_rows[0]["selector"] == (
        f"vonk-forge/{_SYNTHETIC_BASE_RECIPE_SELECTOR}"
    )

    ready_page = _run_cli(
        executable,
        (
            "--no-input",
            "--json",
            "recipe",
            "library",
            "--all-models",
            "--fits-fleet",
            "--limit",
            "1",
            "--sort",
            "name",
            "--cursor",
            cursor,
        ),
        environment,
        cwd,
    )
    assert ready_page.returncode == 0, ready_page.stdout + ready_page.stderr
    ready_document = json.loads(ready_page.stdout)
    ready_rows = ready_document.get("recipes")
    blocked_cursor = ready_document.get("next_cursor")
    assert isinstance(ready_rows, list) and len(ready_rows) == 1
    assert isinstance(blocked_cursor, str) and blocked_cursor
    ready_candidate = ready_rows[0]
    assert ready_candidate["selector"] == f"vonk-forge/{_READY_RECIPE_SELECTOR}"
    ready_assessment = ready_candidate.get("assessment")
    assert isinstance(ready_assessment, dict)
    assert ready_assessment["fleet_fit"]["state"] == "ready"
    assert ready_assessment["cache"]["state"] == "ready"
    assert ready_assessment["readiness"]["state"] == "ready", ready_assessment[
        "readiness"
    ]
    ready_identity = ready_candidate.get("identity")
    assert isinstance(ready_identity, dict)
    assert ready_identity["recipe_revision_id"] == ready_revision_id
    assert ready_identity["content_sha256"] == ready_digest

    blocked_page = _run_cli(
        executable,
        (
            "--no-input",
            "--json",
            "recipe",
            "library",
            "--all-models",
            "--fits-fleet",
            "--limit",
            "1",
            "--sort",
            "name",
            "--cursor",
            blocked_cursor,
        ),
        environment,
        cwd,
    )
    assert blocked_page.returncode == 0, blocked_page.stdout + blocked_page.stderr
    blocked_document = json.loads(blocked_page.stdout)
    blocked_rows = blocked_document.get("recipes")
    assert isinstance(blocked_rows, list) and len(blocked_rows) == 1
    candidate = blocked_rows[0]
    assert candidate["selector"] == f"vonk-forge/{_BLOCKED_RECIPE_SELECTOR}"
    assessment = candidate.get("assessment")
    assert isinstance(assessment, dict)
    assert assessment["fleet_fit"]["state"] == "ready"
    assert assessment["cache"]["state"] == "blocked"
    assert assessment["readiness"]["state"] == "blocked"
    cache_reasons = assessment["cache"].get("reasons")
    assert isinstance(cache_reasons, list) and cache_reasons
    assert all(
        reason.get("code") == "library.cache_missing" for reason in cache_reasons
    )
    details = [reason.get("detail") for reason in cache_reasons]
    assert all(isinstance(detail, str) for detail in details)
    assert any("recipe-not-cached" in detail for detail in details)
    assert any("model-not-cached" in detail for detail in details)
    assert any("exact model artifact bytes are missing" in detail for detail in details)
    assert any(
        f"vonkctl recipe download vonk-forge/{_BLOCKED_RECIPE_SELECTOR}" in detail
        for detail in details
    )
    assert candidate["identity"]["recipe_revision_id"] == candidate_revision_id
    assert candidate["identity"]["content_sha256"] == candidate_digest

    request_key = "11111111-1111-4111-8111-111111111197"
    prepared = _run_cli(
        executable,
        (
            "--no-input",
            "--json",
            "recipe",
            "download",
            f"vonk-forge/{_BLOCKED_RECIPE_SELECTOR}",
            "--request-key",
            request_key,
            "--detach",
        ),
        environment,
        cwd,
    )
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    preparation_receipt = json.loads(prepared.stdout)
    operation_id = preparation_receipt.get("id")
    assert isinstance(operation_id, str) and operation_id
    observed = api.get(f"/api/recipe/operations/{operation_id}", headers=api_headers)
    assert observed.status_code == 200, observed.text
    operation = observed.json()
    assert operation["id"] == operation_id
    assert operation["request_id"] == request_key
    assert operation["recipe_revision_id"] == candidate_revision_id
    assert operation["state"] == "queued"
    with sessions() as session:
        durable = session.scalar(select(Job).where(Job.request_id == request_key))
        assert durable is not None
        assert durable.id == operation_id
        assert durable.state == "queued"
        payload = durable.payload
        assert isinstance(payload, dict)
        assert payload["recipe_revision_id"] == candidate_revision_id
        assert payload["recipe_content_sha256"] == candidate_digest

    add = _run_cli(
        executable,
        (
            "--profile",
            "1",
            "profile",
            "add",
            f"vonk-forge/{_BLOCKED_RECIPE_SELECTOR}",
            "--spark",
            "Spark One",
            "--as",
            "walkthrough-install-only",
            "--state",
            "installed",
            "--json",
        ),
        environment,
        cwd,
    )
    assert add.returncode == 0, add.stdout + add.stderr

    configured = _run_cli(
        executable,
        (
            "--profile",
            "1",
            "profile",
            "configure",
            "--description",
            "Disposable operator rehearsal",
            "--favorite",
            "false",
            "--label",
            "purpose=walkthrough",
            "--json",
        ),
        environment,
        cwd,
    )
    assert configured.returncode == 0, configured.stdout + configured.stderr
    saved = json.loads(configured.stdout)
    assert type(saved.get("revision")) is int
    assert isinstance(saved.get("definition"), dict)

    exported_path = cwd / "profile-export.json"
    exported = _run_cli(
        executable,
        (
            "--profile",
            "1",
            "profile",
            "export",
            "--output",
            str(exported_path),
            "--json",
        ),
        environment,
        cwd,
    )
    assert exported.returncode == 0, exported.stdout + exported.stderr
    assert stat.S_IMODE(exported_path.stat().st_mode) == 0o600
    exported_definition = json.loads(exported_path.read_text(encoding="utf-8"))
    imported = _run_cli(
        executable,
        (
            "--profile",
            "1",
            "profile",
            "import",
            "--file",
            str(exported_path),
            "--expected-revision",
            str(saved["revision"]),
            "--json",
        ),
        environment,
        cwd,
    )
    assert imported.returncode == 0, imported.stdout + imported.stderr
    definition = api.get("/api/profile/1/definition", headers=api_headers)
    assert definition.status_code == 200, definition.text
    assert definition.json()["definition"] == exported_definition
    assignment = definition.json()["definition"]["assignments"][0]
    assert assignment["desired_state"] == "installed"
    assert assignment["assignment_name"] == "walkthrough-install-only"
    assert assignment["recipe_selector"] == f"vonk-forge/{_BLOCKED_RECIPE_SELECTOR}"
    assert assignment["spark_ids"] == [node_id]

    openapi = api.get("/openapi.json")
    assert openapi.status_code == 200, openapi.text
    assert "/api/profile/{number}/load" in openapi.json()["paths"]
    profile_preview = api.post("/api/profile/1/preview", headers=api_headers)
    assert profile_preview.status_code == 200, profile_preview.text
    preview = FleetProfilePreview.model_validate_json(profile_preview.content)
    assert preview.resolved_assignments[0].alias is None
    assert any(
        reason.code == "profile.switch_authority_unavailable"
        for reason in preview.reasons
    )
    profile_load = api.post(
        "/api/profile/1/load",
        headers=api_headers,
        json=FleetProfileLoadRequest(
            request_key="11111111-1111-4111-8111-111111111194",
            plan_digest=preview.plan_digest,
        ).model_dump(mode="json"),
    )
    assert profile_load.status_code == 409, profile_load.text
    profile_load_detail = profile_load.json().get("detail")
    assert profile_load_detail == (
        "Fleet profile preview is blocked; review the current blockers before loading"
    )

    # Fleet cleanup and upgrade providers are deliberately absent. The profile
    # load route is registered, but refuses this plan because its Run/Switch
    # authority is unavailable in the disposable fixture.
    remove = api.post(f"/api/fleet/{node_id}/remove")
    upgrade = api.post(
        "/api/fleet/upgrade",
        json={
            "all": True,
            "strategy": "one-at-a-time",
            "request_key": "11111111-1111-4111-8111-111111111195",
        },
    )
    assert remove.status_code == 401
    assert upgrade.status_code == 401
    # Requests above are intentionally anonymous. The actual session identity
    # gets the same fail-closed response because no mutation services exist.
    remove = api.post(f"/api/fleet/{node_id}/remove", headers=api_headers)
    upgrade = api.post(
        "/api/fleet/upgrade",
        headers=api_headers,
        json={
            "all": True,
            "strategy": "one-at-a-time",
            "request_key": "11111111-1111-4111-8111-111111111196",
        },
    )
    assert remove.status_code == 503
    assert upgrade.status_code == 503

    assert _effect_counts(sessions) == (
        before_effects[0] + 1,
        before_effects[1],
        before_effects[2],
    )
    assert not any(
        method == "POST" and path == "/api/profile/1/load"
        for method, path, _document in peer_calls
    )
    print(
        "Walkthrough smoke passed: installed wheel, invalid-origin and expired-token "
        "rejections, later-page synthetic cache readiness, and Profile "
        "installed-only edit/export/import. A synthetic recipe-preparation request "
        "was accepted into PostgreSQL as queued; Profile load returned the "
        "registered route's HTTP 409 refusal. No profile application or recipe run "
        "was created.",
        flush=True,
    )


def _run_walkthrough(postgres_engine, mode: str) -> None:
    qwen_cpu_assets = None
    qwen_asset_root = os.environ.get("VONK_QWEN3_CPU_ASSET_ROOT")
    if mode == "interactive" and qwen_asset_root is not None:
        qwen_cpu_assets = load_qwen_cpu_assets(Path(qwen_asset_root))
    temporary_path: Path
    with tempfile.TemporaryDirectory(prefix="vonk-cli-walkthrough-") as temporary:
        workspace = Path(temporary)
        temporary_path = workspace
        workspace.chmod(0o700)
        (
            sessions,
            app,
            headers,
            node_id,
            candidate_revision_id,
            candidate_digest,
            ready_revision_id,
            ready_digest,
        ) = _walkthrough_app(
            postgres_engine,
            workspace / "owner-services",
            qwen_cpu_assets=qwen_cpu_assets,
        )
        executable = _build_installed_vonkctl(workspace / "installed-cli")
        operator_cwd = workspace / "operator-cwd"
        operator_cwd.mkdir(mode=0o700)
        with (
            TestClient(app) as api,
            _https_api_peer(workspace, api, headers) as (url, certificate, peer),
        ):
            environment = _session_environment(
                installed_vonkctl=executable,
                workspace=workspace,
                url=url,
                certificate=certificate,
                headers=headers,
            )
            expired_token = app.state.walkthrough_expired_token
            token_file = Path(environment["VONK_CONTROL_TOKEN_FILE"])
            assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
            if headers["Authorization"] != (
                "Bearer " + token_file.read_text(encoding="utf-8")
            ):
                pytest.fail(
                    "private token file does not match the local API identity",
                    pytrace=False,
                )
            participant_materials = _stage_participant_materials(
                workspace,
                controller_url=url,
                valid_token_file=token_file,
                expired_token=expired_token,
            )
            runbook_copy = participant_materials / "vonkctl.md"
            assert (
                runbook_copy.read_bytes()
                == (
                    Path(__file__).resolve().parents[2]
                    / "docs"
                    / "runbooks"
                    / "vonkctl.md"
                ).read_bytes()
            )
            assert stat.S_IMODE(runbook_copy.stat().st_mode) == 0o600
            assert (
                stat.S_IMODE((participant_materials / "expired-token").stat().st_mode)
                == 0o600
            )
            if mode == "smoke":
                _smoke(
                    executable=executable,
                    environment=environment,
                    cwd=operator_cwd,
                    sessions=sessions,
                    api=api,
                    api_headers=headers,
                    peer_calls=peer.calls,
                    node_id=node_id,
                    candidate_revision_id=candidate_revision_id,
                    candidate_digest=candidate_digest,
                    ready_revision_id=ready_revision_id,
                    ready_digest=ready_digest,
                    participant_materials=participant_materials,
                )
            else:
                print(f"Disposable Controller: {url}", flush=True)
                print(f"Installed CLI: {executable}", flush=True)
                print(f"Participant runbook copy: {runbook_copy}", flush=True)
                print(
                    f"Valid connection case: {participant_materials / 'valid-connection.env'}",
                    flush=True,
                )
                print(
                    f"Seeded invalid connection case: {participant_materials / 'invalid-connection.env'}",
                    flush=True,
                )
                print(
                    f"Seeded expired token case: {participant_materials / 'expired-token.env'}",
                    flush=True,
                )
                print(f"Valid token file: {token_file}", flush=True)
                print(
                    "The bearer credential is in a private 0600 file; its contents "
                    "are not displayed. Use the shipped runbook and outcome cards.",
                    flush=True,
                )
                print(
                    "The API is pointed at local disposable services. Fleet "
                    "removal and upgrade providers are unavailable. Profile load "
                    "is registered and returns HTTP 409 while this disposable "
                    "profile cannot be applied. This is "
                    "not an OS network sandbox. Type `exit` or press Ctrl-D to "
                    "close the session and clean up.",
                    flush=True,
                )
                print(
                    "This launcher never starts an inference container or "
                    "launches a model.",
                    flush=True,
                )
                shell = shutil.which("bash") or "/bin/bash"
                completed = subprocess.run(
                    [shell, "--noprofile", "--norc", "-i"],
                    env=environment,
                    cwd=operator_cwd,
                    check=False,
                )
                print(
                    f"Operator shell exited with status {completed.returncode}.",
                    flush=True,
                )
    assert not temporary_path.exists()
    print(
        "Walkthrough local wheel, API, TLS and credential files were cleaned. "
        "The PostgreSQL fixture drops only its per-run database and stops its "
        "own disposable container during pytest teardown.",
        flush=True,
    )


@pytest.mark.skipif(
    os.environ.get("VONK_WALKTHROUGH_MODE") != "smoke",
    reason="set VONK_WALKTHROUGH_MODE=smoke to run the installed CLI smoke",
)
def test_disposable_cli_operator_walkthrough_smoke(postgres_engine) -> None:
    _run_walkthrough(postgres_engine, "smoke")


@pytest.mark.skipif(
    os.environ.get("VONK_WALKTHROUGH_MODE") != "interactive",
    reason="set VONK_WALKTHROUGH_MODE=interactive to start the human session",
)
def test_disposable_cli_operator_walkthrough_interactive(postgres_engine) -> None:
    _run_walkthrough(postgres_engine, "interactive")
