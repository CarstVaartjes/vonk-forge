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
from collections.abc import Callable
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
from vonk_control.fleet_profiles import (
    FleetProfileService,
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
from vonk_control.jobs import JobService
from vonk_control.library_assessment import LibraryAssessment
from vonk_control.library_projection import LibraryProjection
from vonk_control.model_cache import ModelCacheService
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
_READY_RECIPE_SELECTOR = "qwen3-vllm-a-ready"
_BLOCKED_RECIPE_SELECTOR = "qwen3-vllm-z-candidate"
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
        archive_sha256 = hashlib.sha256(destination.read_bytes()).hexdigest()
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
                display_name="Spark One",
                hostname="spark-one",
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
            if row.kind == "recipe" and row.slug == "qwen3-vllm"
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

    ready_model_document = copy.deepcopy(model_document)
    ready_model_identity = ready_model_document["identity"]
    assert isinstance(ready_model_identity, dict)
    ready_model_identity["slug"] = _READY_MODEL_SELECTOR
    ready_model_model = ready_model_identity["model"]
    ready_model_family = ready_model_identity["family"]
    assert isinstance(ready_model_model, dict) and isinstance(ready_model_family, dict)
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
    ready_identity["slug"] = _READY_RECIPE_SELECTOR
    ready_metadata["title"] = "Qwen 3 VLLM ready candidate"
    ready_recipe_document["execution"] = copy.deepcopy(image_example["execution"])
    ready_execution = ready_recipe_document["execution"]
    assert isinstance(ready_execution, dict)
    ready_image = ready_execution["image"]
    assert isinstance(ready_image, dict)
    ready_image["repository"] = "registry.example/walkthrough/synthetic-runtime"
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
            "id": "weights",
            "file_id": "weights",
            "roles": ["entrypoint"],
            "mount": {"target": "/models/model.safetensors", "read_only": True},
        }
    ]
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
    source_archive, image_inspector, image_evidence = _local_oci_archive(
        owner_root / "image-fixture",
        architecture=expectations["architecture"],
        runtime_interface=expectations["interface"],
        skopeo=skopeo,
    )
    expected_image_digest = image_evidence.manifest_digest
    assert expected_image_digest.startswith("sha256:")
    ready_image["digest"] = expected_image_digest.removeprefix("sha256:")
    ready_recipe = RecipeDefinition.model_validate(ready_recipe_document)
    ready_draft = catalog.create_draft(
        ready_recipe.model_dump(mode="json"), actor="admin"
    )
    ready_revision = catalog.resolve(ready_draft.id, actor="admin")

    identity = base_recipe_document["identity"]
    metadata = base_recipe_document["metadata"]
    assert isinstance(identity, dict) and isinstance(metadata, dict)
    identity["slug"] = _BLOCKED_RECIPE_SELECTOR
    metadata["title"] = "Qwen 3 VLLM later-page candidate"
    base_recipe_document["execution"] = image_example["execution"]
    selections = base_recipe_document["models"]
    assert isinstance(selections, list) and selections
    selection = selections[0]
    assert isinstance(selection, dict)
    model_reference = selection["model"]
    assert isinstance(model_reference, dict)
    model_reference["publisher"] = model.identity.publisher
    model_reference["slug"] = model.identity.slug
    model_reference["content_sha256"] = content_sha256(model)
    candidate = RecipeDefinition.model_validate(base_recipe_document)
    candidate_draft = catalog.create_draft(
        candidate.model_dump(mode="json"), actor="admin"
    )
    candidate_revision = catalog.resolve(candidate_draft.id, actor="admin")

    runtime_storage = FilesystemRuntimeImageStorage(owner_root / "runtime-images")
    expected_model_path = (
        "/walkthrough/walkthrough-synthetic-tiny-ready/resolve/"
        f"{_READY_MODEL_REVISION}/model.safetensors"
    )
    model_requests: list[str] = []

    def serve_only_ready_model_artifact(request: httpx.Request) -> httpx.Response:
        model_requests.append(f"{request.method} {request.url.host}{request.url.path}")
        if (
            request.method != "GET"
            or request.url.host != "huggingface.co"
            or request.url.path != expected_model_path
        ):
            return httpx.Response(403, request=request)
        return httpx.Response(200, content=_READY_MODEL_PAYLOAD, request=request)

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
        assert model_preview["new_bytes"] == len(_READY_MODEL_PAYLOAD)
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
        assert model_requests == [f"GET huggingface.co{expected_model_path}"]
        model_manifest = model_cache.resolve_artifact_set(
            model_content_sha256=ready_model_digest
        )
        assert len(model_manifest.artifacts) == 1
        model_artifact = model_manifest.artifacts[0]
        managed_object = model_cache._object_path(model_artifact.sha256)
        assert managed_object.read_bytes() == _READY_MODEL_PAYLOAD
        assert hashlib.sha256(managed_object.read_bytes()).hexdigest() == (
            model_artifact.sha256
        )
        assert model_cache._receipt_path(model_artifact.sha256).is_file()
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
        else FleetProfileService(sessions, clock=clock)
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
    assert hashlib.sha256(stored_image.read_bytes()).hexdigest() == archive_digest
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
) -> None:
    before_effects = _effect_counts(sessions)
    help_result = _run_cli(executable, ("--help",), environment, cwd)
    assert help_result.returncode == 0, help_result.stderr

    invalid_origin = _run_cli(
        executable,
        ("--controller", "https://127.0.0.1:1", "fleet", "--json"),
        environment,
        cwd,
    )
    assert invalid_origin.returncode != 0

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
    assert first_rows[0]["selector"] == "vonk-forge/qwen3-vllm"

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
    assert assignment["recipe_selector"] == f"vonk-forge/{_BLOCKED_RECIPE_SELECTOR}"
    assert assignment["spark_ids"] == [node_id]

    # The app deliberately has no fleet mutation providers. Verify the exact
    # registered routes refuse cleanup and upgrade even for this admin token.
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
        "Walkthrough smoke passed: installed wheel, local Fleet read, a later-page "
        "cache-ready candidate, a separate cache-blocked candidate with queued "
        "preparation, and Profile installed-only edit/export/import. No profile "
        "application or recipe run was created.",
        flush=True,
    )


def _run_walkthrough(postgres_engine, mode: str) -> None:
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
        ) = _walkthrough_app(postgres_engine, workspace / "owner-services")
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
            token_file = Path(environment["VONK_CONTROL_TOKEN_FILE"])
            assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
            if headers["Authorization"] != (
                "Bearer " + token_file.read_text(encoding="utf-8")
            ):
                pytest.fail(
                    "private token file does not match the local API identity",
                    pytrace=False,
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
                )
            else:
                print(f"Disposable Controller: {url}", flush=True)
                print(f"Installed CLI: {executable}", flush=True)
                print(
                    "The bearer credential is in a private 0600 file; its contents "
                    "are not displayed. Use the shipped runbook and outcome cards.",
                    flush=True,
                )
                print(
                    "The API is pointed at local disposable services, including "
                    "the prepared U2 candidate, and has no "
                    "Fleet removal, upgrade, or profile-load provider. This is "
                    "not an OS network sandbox. Type `exit` or press Ctrl-D to "
                    "close the session and clean up.",
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
