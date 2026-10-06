"""Executable, fail-closed Spark lifecycle acceptance entry point."""

from __future__ import annotations

import argparse
import base64
import binascii
import copy
import gzip
import hashlib
import http.client
import io
import ipaddress
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import NamedTuple, Protocol, Self
from urllib.parse import urlsplit

import yaml

from cluster_profiles.serving_execution import (
    HttpObservation,
    ServingExecutionError,
    evaluate_http_response,
)

sys.path.insert(0, os.fspath(Path(__file__).resolve().parents[2]))

from scripts.development_slice_client import (
    MAXIMUM_RESPONSE_BYTES,
    Client,
    SliceError,
    require_object,
)
from scripts.spark_lifecycle_contract import (
    GATES,
    PHASES,
    ContractError,
    recompute_publication_graphs,
    validate_lifecycle,
)
from tests.acceptance.controller_contract import ContractSkew
from tests.acceptance.runtime import (
    AcceptanceError,
    _compose_rows,
    assert_compose_services_healthy,
    bootstrap_command,
    run_interactive,
)
from tests.acceptance.systemd_sandbox import SandboxError, verify_effective_sandbox
from tests.acceptance.test_fresh_nas_install import (
    DEFAULT_SERVICES,
    command_environment,
    generate_bundle,
    host_command_environment,
    is_channel_image,
    is_immutable_image,
    nas_responses,
    tailscale_service_hostname,
)

PLATFORMS = ("linux-arm64",)

# How long a synthetic-canary operation may take before the lane calls it
# stuck. The product bounds a single distributed step at
# its fixed distributed start timeout, so a canary that never converges could
# otherwise sit inside that budget; the canary moves a tiny synthetic asset set,
# so a well-behaved operation converges in seconds and these diagnostic bounds
# stay far below the product's. Waiting out the product budget only delays the
# diagnosis and hides the cause behind a timeout.
# A cold source-build canary includes an exact arm64 base-image pull and the
# first build on a clean Spark.  Keep the bound finite, but leave enough room
# for that legitimate first attempt to finish before declaring the durable
# operation stuck.  The operation itself remains restart-safe and reports its
# own retry state while we wait.
_CANARY_CONVERGENCE_SECONDS = 300
_CANARY_ROUTE_SECONDS = 60
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
SOURCE_SHA = re.compile(r"[0-9a-f]{40}\Z")
CHANNEL = re.compile(r"(?:dev|stable)\Z")
VERSION = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:[+~][0-9A-Za-z.+~-]+)?\Z"
)
SAFE_HTTPS_URL = re.compile(r"https://[A-Za-z0-9._~:/-]+\Z")
NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")
UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
SERIAL = re.compile(r"[1-9][0-9]{0,127}\Z")
PROJECT = re.compile(r"vonk-spark-[1-9][0-9]*-arm64\Z")
# Exercise the production-supported lower bound.  The agent renews at two thirds
# of a certificate lifetime and its independent rotation lane polls on a bounded
# interval, so 90 seconds leaves real scheduling margin in every ARM64 gate.
CERTIFICATE_LIFETIME_SECONDS = 90
CONTROLLER_ADDRESS = "127.0.0.1"
CANARY_CATALOG_IMPORT = Path(__file__).with_name("spark_canary_catalog_import.py")
SPARK_CONFIG = Path("/etc/vonk-forge-agent/agent.toml")
AGENT_BINARY = Path("/usr/lib/vonk-forge/vonk-agent")
AGENT_DATA = Path("/var/lib/vonk-forge-agent")
COMPOSE_IMAGE_ROLES = {
    "api": "control-api",
    "worker": "control-worker",
    "hermes": "hermes-agent",
    "litellm": "litellm",
}

ED25519_PKCS8_V2_PREFIX = bytes.fromhex("3051020101300506032b657004220420")
ED25519_PKCS8_V2_PUBLIC_PREFIX = bytes.fromhex("812100")
ED25519_PKCS8_V1_PREFIX = bytes.fromhex("302e020100")
TAILSCALE_CONTROLLER_SERVICES = {
    "tailscale-configurator",
    "tailscale-gateway",
}
LOCAL_CONTROLLER_SERVICES = {
    "caddy",
    "control-api",
    "control-worker",
    "grafana",
    "litellm",
    "postgres",
    "prometheus",
    "registry",
    "step-ca",
}
if LOCAL_CONTROLLER_SERVICES | TAILSCALE_CONTROLLER_SERVICES != DEFAULT_SERVICES:
    raise RuntimeError("Spark Controller service allowlist needs review")
LOCAL_CONTROL_SERVICE = "svc:vonk-forge-spark-local"
LOCAL_HERMES_API_SERVICE = "svc:hermes-api-spark-local"
LOCAL_HERMES_DASHBOARD_SERVICE = "svc:hermes-dashboard-spark-local"
LOCAL_DNS_SUFFIX = "spark.acceptance.invalid"
# The installer derives every controller SNI name from the control hostname.
LOCAL_CONTROL_HOSTNAME = (
    f"{LOCAL_CONTROL_SERVICE.removeprefix('svc:')}.{LOCAL_DNS_SUFFIX}"
)
ENROLLMENT_HOST = f"enroll.{LOCAL_CONTROL_HOSTNAME}"
AGENT_HOST = f"agents.{LOCAL_CONTROL_HOSTNAME}"
REGISTRY_HOST = f"registry.{LOCAL_CONTROL_HOSTNAME}"
DISABLED_TAILSCALE_CREDENTIAL = "tailscale-disabled-for-spark-acceptance"
FORBIDDEN_SPARK_TAILNET_INPUTS = (
    "VONK_ACCEPTANCE_TAILNET_DNS_SUFFIX",
    "VONK_ACCEPTANCE_TAILNET_KIND",
    "VONK_ACCEPTANCE_TAILSCALE_CONTROL_SERVICE",
    "VONK_ACCEPTANCE_TAILSCALE_GATEWAY_HOSTNAME",
    "VONK_ACCEPTANCE_TAILSCALE_HERMES_API_SERVICE",
    "VONK_ACCEPTANCE_TAILSCALE_HERMES_DASHBOARD_SERVICE",
    "VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_ID",
    "VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_SECRET",
)
SYNTHETIC_CANARY_STATES = (
    "inventory-ready",
    "recipe-resolved",
    "source-verified",
    "image-built",
    "image-distributed",
    "installed",
    "running",
    "route-published",
    "inference-ok",
    "stopped",
    "route-withdrawn",
    "uninstalled",
)


def _require_loopback_controller_boundary() -> None:
    if os.environ.get("VONK_ACCEPTANCE_SPARK_CONTROLLER_BOUNDARY") != "loopback":
        raise LifecycleError("Spark controller boundary must be loopback")
    present_tailnet_inputs = [
        name for name in FORBIDDEN_SPARK_TAILNET_INPUTS if os.environ.get(name)
    ]
    if present_tailnet_inputs:
        raise LifecycleError("Spark acceptance must not receive tailnet inputs")


def _spark_project_identity(run_id: int, platform_name: str) -> str:
    if run_id <= 0 or platform_name not in PLATFORMS:
        raise LifecycleError("isolated Compose project identity is invalid")
    project = f"vonk-spark-{run_id}-{platform_name.removeprefix('linux-')}"
    if PROJECT.fullmatch(project) is None:
        raise LifecycleError("isolated Compose project identity is invalid")
    return project


def _openssl_compatible_ed25519_private_key(raw: bytes) -> bytes:
    """Convert strict RFC 5958 Ed25519 material to RFC 5208 for OpenSSL 3.0."""
    lines = raw.strip().splitlines()
    if (
        len(lines) < 3
        or lines[0] != b"-----BEGIN PRIVATE KEY-----"
        or lines[-1] != b"-----END PRIVATE KEY-----"
    ):
        raise LifecycleError("retired agent private key is invalid")
    try:
        der = base64.b64decode(b"".join(lines[1:-1]), validate=True)
    except (ValueError, binascii.Error) as error:
        raise LifecycleError("retired agent private key is invalid") from error
    if (
        len(der) != 83
        or not der.startswith(ED25519_PKCS8_V2_PREFIX)
        or der[48:51] != ED25519_PKCS8_V2_PUBLIC_PREFIX
    ):
        raise LifecycleError("retired agent private key is invalid")
    compatible = ED25519_PKCS8_V1_PREFIX + der[5:48]
    encoded = base64.b64encode(compatible)
    body = b"\n".join(
        encoded[index : index + 64] for index in range(0, len(encoded), 64)
    )
    return b"-----BEGIN PRIVATE KEY-----\n" + body + b"\n-----END PRIVATE KEY-----\n"


#: A cache operation that has not ended (an older Controller sent ``partial`` for
#: one that retries).
_LIVE_CACHE_STATES = frozenset({"queued", "running", "backoff", "observing", "partial"})
#: A profile application that has not reached a terminal outcome.
_LIVE_APPLICATION_STATES = frozenset(
    {"queued", "running", "needs-operator", "waiting-for-operator"}
)


class LifecycleError(RuntimeError):
    """A bounded acceptance failure that contains no credential material."""


class CanonicalCanaryFixture(NamedTuple):
    """Exact producer-owned Recipe package selected for the fresh canary."""

    index_path: Path
    index_bytes: bytes
    package_path: PurePosixPath
    package_bytes: bytes
    source_commit: str
    publisher: str
    slug: str
    recipe_content_sha256: str
    model_content_sha256: str
    role: str
    serving_check: dict[str, object]
    recipe: dict[str, object]


def _canary_index_path(library_root: Path) -> Path:
    selected = os.environ.get("VONK_SYNTHETIC_CANARY_INDEX")
    if not selected:
        raise LifecycleError(
            "VONK_SYNTHETIC_CANARY_INDEX must select the exact producer fixture"
        )
    relative = PurePosixPath(selected)
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise LifecycleError("synthetic canary index path is invalid")
    path = (library_root / Path(*relative.parts)).resolve()
    if not path.is_relative_to(library_root) or not path.is_file():
        raise LifecycleError("canonical synthetic canary package index is unavailable")
    return path


def _package_archive_path(
    library_root: Path, index_path: Path, package_path: PurePosixPath
) -> Path:
    candidates = {
        candidate.resolve()
        for candidate in (
            library_root / Path(*package_path.parts),
            index_path.parent / Path(*package_path.parts),
        )
        if candidate.is_file()
    }
    if len(candidates) != 1:
        raise LifecycleError(
            "canonical synthetic canary package archive is unavailable"
        )
    archive = candidates.pop()
    if not archive.is_relative_to(library_root):
        raise LifecycleError("synthetic canary package escapes the recipe library")
    return archive


def _canonical_canary_fixture(library_root: Path) -> CanonicalCanaryFixture:
    root = library_root.expanduser().resolve()
    if not root.is_dir():
        raise LifecycleError("VONK_RECIPE_LIBRARY_ROOT is not an available directory")
    contracts_source = root / "contracts/src"
    if not contracts_source.is_dir():
        raise LifecycleError("canonical Recipe/Model contract source is unavailable")
    sys.path.insert(0, os.fspath(contracts_source))
    try:
        from vonk_forge_contracts import (
            ModelDefinition,
            RecipeDefinition,
            document_sha256,
        )
    except ImportError as error:
        raise LifecycleError(
            "canonical Recipe/Model contract package is unavailable"
        ) from error
    index_path = _canary_index_path(root)
    try:
        index_bytes = index_path.read_bytes()
        index = json.loads(index_bytes)
        entry = index["recipes"][0]
        raw_recipe = entry["document"]
        package = entry["package"]
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        IndexError,
        TypeError,
    ) as error:
        raise LifecycleError(
            "canonical synthetic canary package index is invalid"
        ) from error
    try:
        recipe_contract = RecipeDefinition.model_validate(raw_recipe)
        catalog_models = [
            (ModelDefinition.model_validate(value["document"]), value["document"])
            for value in index["catalog_entities"]
            if isinstance(value, dict)
            and isinstance(value.get("document"), dict)
            and value["document"].get("kind") == "model"
        ]
    except (KeyError, TypeError, ValueError) as error:
        raise LifecycleError(
            "canonical synthetic canary contract is invalid"
        ) from error
    if len(recipe_contract.models) != 1:
        raise LifecycleError("canonical synthetic canary Recipe is not canonical")
    model_reference = recipe_contract.models[0].model
    matching_models = [
        model
        for model, raw_model in catalog_models
        if model.identity.publisher == model_reference.publisher
        and model.identity.slug == model_reference.slug
        and document_sha256(raw_model) == model_reference.content_sha256
    ]
    if len(matching_models) != 1:
        raise LifecycleError("canonical synthetic canary Model closure is invalid")
    package_value = package.get("path") if isinstance(package, dict) else None
    if not isinstance(package_value, str):
        raise LifecycleError("canonical synthetic canary package descriptor is invalid")
    package_path = PurePosixPath(package_value)
    if (
        package_path.is_absolute()
        or any(part in {"", ".", ".."} for part in package_path.parts)
        or re.fullmatch(r"[A-Za-z0-9._/-]{1,512}", package_value) is None
        or index.get("schema_version") != 2
        or index.get("kind") != "recipe-library-index"
        or index.get("repository") != "CarstVaartjes/vonk-forge-recipes"
        or not isinstance(index.get("source_commit"), str)
        or SOURCE_SHA.fullmatch(index["source_commit"]) is None
        or not isinstance(entry.get("content_sha256"), str)
        or entry["content_sha256"] != document_sha256(raw_recipe)
        or package.get("media_type")
        != "application/vnd.vonk-forge.recipe-package.v2+tar+gzip"
        or package.get("recipe_content_sha256") not in {None, entry["content_sha256"]}
    ):
        raise LifecycleError("canonical synthetic canary contract is invalid")
    roles = recipe_contract.topology.roles
    if (
        recipe_contract.topology.mode != "single"
        or recipe_contract.topology.node_count != 1
        or len(roles) != 1
        or roles[0].endpoint_owner is not True
    ):
        raise LifecycleError("canonical synthetic canary topology is invalid")
    http_checks = [
        check
        for check in recipe_contract.validation.serving.checks
        if check.request.transport == "http" and check.kind == "openai.chat"
    ]
    if len(http_checks) != 1:
        raise LifecycleError("canonical synthetic canary serving check is invalid")
    check = http_checks[0].model_dump(mode="json")
    request = check["request"]
    body = request.get("body")
    max_tokens = body.get("max_tokens") if isinstance(body, dict) else None
    if (
        request.get("method") != "POST"
        or request.get("path") != "/v1/chat/completions"
        or type(max_tokens) is not int
        or not 1 <= max_tokens <= 64
    ):
        raise LifecycleError("synthetic canary inference is not bounded")
    archive_path = _package_archive_path(root, index_path, package_path)
    try:
        if not 1 <= archive_path.stat().st_size <= 256 * 1024 * 1024:
            raise LifecycleError("canonical synthetic canary package size is invalid")
        package_bytes = archive_path.read_bytes()
    except OSError as error:
        raise LifecycleError(
            "canonical synthetic canary package is unavailable"
        ) from error
    if (
        package.get("expected_bytes") != len(package_bytes)
        or package.get("sha256") != hashlib.sha256(package_bytes).hexdigest()
    ):
        raise LifecycleError("canonical synthetic canary package digest is invalid")
    try:
        with tarfile.open(fileobj=io.BytesIO(package_bytes), mode="r:gz") as archive:
            manifest_member = archive.getmember("manifest.json")
            recipe_member = archive.getmember("recipe.json")
            if (
                not manifest_member.isfile()
                or not recipe_member.isfile()
                or manifest_member.size > 12 * 1024 * 1024
                or recipe_member.size > 12 * 1024 * 1024
            ):
                raise tarfile.TarError("canonical package entrypoint is not a file")
            manifest_stream = archive.extractfile(manifest_member)
            recipe_stream = archive.extractfile(recipe_member)
            if manifest_stream is None or recipe_stream is None:
                raise tarfile.TarError("canonical package entrypoint is not a file")
            manifest = json.loads(manifest_stream.read())
            packaged_recipe = json.loads(recipe_stream.read())
    except (
        KeyError,
        OSError,
        tarfile.TarError,
        AttributeError,
        json.JSONDecodeError,
    ) as error:
        raise LifecycleError(
            "canonical synthetic canary package closure is invalid"
        ) from error
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 2
        or manifest.get("kind") != "recipe-package"
        or manifest.get("package_type") != "recipe"
        or manifest.get("recipe_content_sha256") != entry["content_sha256"]
        or packaged_recipe != raw_recipe
    ):
        raise LifecycleError(
            "canonical synthetic canary Recipe differs from its package"
        )
    return CanonicalCanaryFixture(
        index_path=index_path,
        index_bytes=index_bytes,
        package_path=package_path,
        package_bytes=package_bytes,
        source_commit=index["source_commit"],
        publisher=recipe_contract.identity.publisher,
        slug=recipe_contract.identity.slug,
        recipe_content_sha256=entry["content_sha256"],
        model_content_sha256=model_reference.content_sha256,
        role=roles[0].name,
        serving_check=check,
        recipe=raw_recipe,
    )


def _rebuilt_recipe_entry(
    fixture: CanonicalCanaryFixture, edit: Callable[[dict[str, object]], None]
) -> tuple[dict[str, object], bytes]:
    """The fixture's index entry and package, rebuilt around an edited Recipe.

    The package is repacked exactly as a producer would publish it: the edited
    recipe.json, the manifest that names its digest and size, and every other
    member (the build context, the Model document) untouched.
    """

    from vonk_forge_contracts import document_sha256

    entry = copy.deepcopy(json.loads(fixture.index_bytes)["recipes"][0])
    recipe = entry["document"]
    edit(recipe)
    digest = document_sha256(recipe)
    recipe_bytes = json.dumps(recipe, sort_keys=True, indent=2).encode() + b"\n"
    with tarfile.open(fileobj=io.BytesIO(fixture.package_bytes), mode="r:gz") as source:
        members = [
            (member, source.extractfile(member).read() if member.isfile() else b"")  # type: ignore[union-attr]
            for member in source.getmembers()
        ]
    archive = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=archive, mode="wb", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as target,
    ):
        for member, payload in members:
            if member.name == "recipe.json":
                payload = recipe_bytes
            elif member.name == "manifest.json":
                manifest = json.loads(payload)
                manifest["recipe_content_sha256"] = digest
                for item in manifest["files"]:
                    if item["path"] == "recipe.json":
                        item["sha256"] = hashlib.sha256(recipe_bytes).hexdigest()
                        item["size"] = len(recipe_bytes)
                payload = json.dumps(
                    manifest, sort_keys=True, separators=(",", ":")
                ).encode()
            member.size = len(payload)
            target.addfile(member, io.BytesIO(payload) if member.isfile() else None)
    package_bytes = archive.getvalue()
    entry["content_sha256"] = digest
    entry["package"] = {
        **entry["package"],
        "expected_bytes": len(package_bytes),
        "recipe_content_sha256": digest,
        "sha256": hashlib.sha256(package_bytes).hexdigest(),
    }
    return entry, package_bytes


def _editorial_successor(fixture: CanonicalCanaryFixture) -> CanonicalCanaryFixture:
    """The same Recipe with a reworded description: a new revision, the same image.

    Only editorial text changes, so the executable inputs, the build context
    and the model stay identical. The package and index are rebuilt around the
    new Recipe document exactly as a producer would publish them.
    """

    def reword(recipe: dict[str, object]) -> None:
        metadata = recipe["metadata"]
        metadata["description"] = f"{metadata['description']} Editorially revised."  # type: ignore[index]

    entry, package_bytes = _rebuilt_recipe_entry(fixture, reword)
    index = json.loads(fixture.index_bytes)
    index["recipes"] = [entry]
    digest = str(entry["content_sha256"])
    index["source_commit"] = hashlib.sha1(digest.encode()).hexdigest()
    return fixture._replace(
        index_bytes=json.dumps(index, sort_keys=True).encode(),
        package_bytes=package_bytes,
        source_commit=index["source_commit"],
        recipe_content_sha256=digest,
        recipe=entry["document"],  # type: ignore[arg-type]
    )


def _sibling_recipes(
    fixture: CanonicalCanaryFixture,
) -> tuple[CanonicalCanaryFixture, CanonicalCanaryFixture]:
    """Two different Recipes that build one and the same image.

    Each sibling is the canary under its own slug and title. The route alias
    is the assignment's name (the slug); the recipe's model alias is the model
    the canary service answers to, so it stays the canary's. The
    build context, the base image and the Model are the canary's, so both
    Recipes name the image the canary builds. Both arrive in one catalog commit,
    as two Recipes of one producer library do.
    """

    def sibling(name: str) -> Callable[[dict[str, object]], None]:
        def rename(recipe: dict[str, object]) -> None:
            slug = f"{recipe['identity']['slug']}-{name}"  # type: ignore[index]
            recipe["identity"]["slug"] = slug  # type: ignore[index]
            recipe["metadata"]["title"] = f"{recipe['metadata']['title']} ({name})"  # type: ignore[index]

        return rename

    built = [_rebuilt_recipe_entry(fixture, sibling(name)) for name in ("one", "two")]
    index = json.loads(fixture.index_bytes)
    index["recipes"] = [entry for entry, _ in built]
    commit = hashlib.sha1(
        "".join(str(entry["content_sha256"]) for entry, _ in built).encode()
    ).hexdigest()
    index["source_commit"] = commit
    index_bytes = json.dumps(index, sort_keys=True).encode()
    siblings = tuple(
        fixture._replace(
            index_bytes=index_bytes,
            package_bytes=package_bytes,
            source_commit=commit,
            slug=str(entry["document"]["identity"]["slug"]),  # type: ignore[index]
            recipe_content_sha256=str(entry["content_sha256"]),
            recipe=entry["document"],  # type: ignore[arg-type]
        )
        for entry, package_bytes in built
    )
    return siblings[0], siblings[1]


class ObservedLifecycle(Protocol):
    def __enter__(self) -> Self: ...

    def observe(self) -> dict[str, object]: ...

    def __exit__(self, *error: object) -> None: ...


def _run_spark_bootstrap(
    url: str,
    *,
    cwd: Path,
    environment: dict[str, str],
    enrollment_url: str | None = None,
    ca_sha256: str | None = None,
    pairing_token: str | None = None,
    interactive: Callable[..., str] = run_interactive,
) -> str:
    if SAFE_HTTPS_URL.fullmatch(url) is None:
        raise LifecycleError("Spark bootstrap URL is invalid")
    pairing = pairing_token is not None
    if pairing != (enrollment_url is not None and ca_sha256 is not None):
        raise LifecycleError("Spark bootstrap pairing inputs are incomplete")
    if pairing and (
        not pairing_token
        or SAFE_HTTPS_URL.fullmatch(str(enrollment_url)) is None
        or SHA256.fullmatch(str(ca_sha256)) is None
    ):
        raise LifecycleError("Spark bootstrap pairing inputs are invalid")
    responses = (
        [
            ("Enrollment URL: ", str(enrollment_url)),
            ("Controller CA SHA-256: ", str(ca_sha256)),
            ("Pairing token: ", str(pairing_token)),
        ]
        if pairing
        else []
    )
    forbidden = [str(pairing_token)] if pairing else []
    return interactive(
        bootstrap_command(url, *(("--enroll",) if pairing else ())),
        cwd=cwd,
        environment=environment,
        responses=responses,
        timeout=300,
        require_all_prompts=True,
        forbidden_values=forbidden,
    )


def _set_bundle_environment(bundle: Path, values: dict[str, str]) -> None:
    environment = bundle / ".env"
    lines = [
        line
        for line in environment.read_text(encoding="utf-8").splitlines()
        if line.partition("=")[0] not in values
    ]
    lines.extend(f"{name}={json.dumps(value)}" for name, value in values.items())
    environment.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _configure_acceptance_renewal(
    bundle: Path,
    *,
    lifetime_seconds: int,
    agent_source_address: str,
    caddyfile: str | None = None,
) -> None:
    try:
        parsed_agent_source = ipaddress.ip_address(agent_source_address)
    except ValueError as error:
        raise LifecycleError("acceptance agent source address is invalid") from error
    if lifetime_seconds != CERTIFICATE_LIFETIME_SECONDS:
        raise LifecycleError("acceptance certificate lifetime is invalid")
    if not isinstance(
        parsed_agent_source, ipaddress.IPv4Address
    ) or parsed_agent_source not in ipaddress.ip_network("172.16.0.0/12"):
        raise LifecycleError("acceptance agent source address is invalid")
    ca_path = bundle / "secrets/step-ca/ca.json"
    ca = _read_document(ca_path, "Step CA configuration")
    try:
        authority = ca["authority"]
        if not isinstance(authority, dict):
            raise TypeError("Step CA authority configuration is invalid")
        provisioners = authority["provisioners"]
        if not isinstance(provisioners, list):
            raise TypeError("Step CA provisioners are invalid")
        provisioner = next(
            value for value in provisioners if value.get("name") == "vonk-forge-agent"
        )
        claims = provisioner["claims"]
    except (KeyError, StopIteration, TypeError) as error:
        raise LifecycleError("Step CA provisioner configuration is invalid") from error
    if (
        not isinstance(claims, dict)
        or claims.get("disableRenewal") is not True
        or claims.get("disableSmallstepExtensions") is not True
    ):
        raise LifecycleError("Step CA provisioner claims are invalid")
    # The Controller derives the agent certificate lifetime from this claim.
    duration = f"{lifetime_seconds}s"
    claims.update(
        defaultTLSCertDuration=duration,
        maxTLSCertDuration=duration,
        minTLSCertDuration=duration,
    )
    ca_path.write_bytes(_canonical(ca))
    os.chmod(ca_path, 0o600)

    compose_path = bundle / "docker-compose.yaml"
    try:
        compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise LifecycleError("Compose controller configuration is invalid") from error
    try:
        caddy_service = compose["services"]["caddy"]
        caddy_ports = caddy_service["ports"]
        caddy_volumes = caddy_service["volumes"]
    except (KeyError, TypeError) as error:
        raise LifecycleError("Compose browser boundary is invalid") from error
    # The release Caddyfile ships in the Controller image. Acceptance runs a
    # copy that names the fixed agent source address instead.
    try:
        caddy = (
            caddyfile
            if caddyfile is not None
            else (REPOSITORY_ROOT / "deploy/compose/Caddyfile").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, UnicodeDecodeError) as error:
        raise LifecycleError("Caddy acceptance boundary is invalid") from error
    source_directive = "header_up X-Vonk-Agent-Source {http.request.remote.host}"
    if not isinstance(caddy_volumes, list) or caddy.count(source_directive) != 1:
        raise LifecycleError("Caddy acceptance boundary is invalid")
    caddy_path = bundle / "acceptance-Caddyfile"
    caddy_path.write_text(
        caddy.replace(
            source_directive,
            f"header_up X-Vonk-Agent-Source {agent_source_address}",
        ),
        encoding="utf-8",
    )
    os.chmod(caddy_path, 0o644)
    caddy_volumes.append("./acceptance-Caddyfile:/etc/caddy/Caddyfile:ro")
    caddy_service["command"] = [
        "caddy",
        "run",
        "--config",
        "/etc/caddy/Caddyfile",
        "--adapter",
        "caddyfile",
    ]
    if not isinstance(caddy_ports, list) or "127.0.0.1::8080" in caddy_ports:
        raise LifecycleError("Compose browser boundary is invalid")
    caddy_ports.append("127.0.0.1::8080")
    caddy_networks = caddy_service.get("networks")
    if (
        not isinstance(caddy_networks, list)
        or "cluster-egress" in caddy_networks
        or "cluster-egress" not in compose.get("networks", {})
    ):
        raise LifecycleError("Compose acceptance network topology is invalid")
    caddy_networks.append("cluster-egress")
    compose_path.write_text(
        yaml.safe_dump(compose, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    os.chmod(compose_path, 0o644)


def _synthetic_device_fixture(platform_name: str) -> tuple[bytes, str]:
    if platform_name not in PLATFORMS:
        raise LifecycleError("synthetic device fixture platform is invalid")
    raw = json.dumps(
        {
            "cdiVersion": "0.5.0",
            "devices": [
                {
                    "containerEdits": {"env": ["VONK_SYNTHETIC_CDI=1"]},
                    "name": "all",
                }
            ],
            "kind": "nvidia.com/gpu",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return raw, hashlib.sha256(raw).hexdigest()


def _agent_package_installed() -> bool:
    query = Path("/usr/bin/dpkg-query")
    if not query.is_file() or not os.access(query, os.X_OK):
        return False
    return (
        subprocess.run(
            [query, "--show", "vonk-forge-agent"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


def _semantic_version(package_version: str) -> str:
    return package_version.split("~", 1)[0].split("+", 1)[0]


def _canonical_recipe_matches(observed: object, expected: object) -> bool:
    from vonk_forge_contracts import RecipeDefinition

    try:
        return RecipeDefinition.model_validate(
            observed
        ) == RecipeDefinition.model_validate(expected)
    except ValueError:
        return False


def _validate_canary_cleanup_preview(
    preview: dict[str, object], *, node_id: str
) -> None:
    """Require the exact admitted delegated-cleanup intent."""

    from vonk_control.fleet_profile_contract import FleetProfilePreview

    try:
        typed = FleetProfilePreview.model_validate_json(_canonical(preview))
    except ValueError as error:
        raise LifecycleError("synthetic canary cleanup preview is invalid") from error
    if (
        typed.allowed is not True
        or typed.summary.blockers != 0
        or typed.scope.node_ids != [node_id]
        or len(typed.steps) != 1
        or typed.steps[0].kind != "switch"
        or typed.steps[0].node_ids != [node_id]
    ):
        raise LifecycleError("synthetic canary cleanup preview is not admitted")


def _installations_removed(
    receipts: Sequence[object], installation_ids: Sequence[str]
) -> bool:
    """Whether every named installation has an uninstall and a verified removal.

    A canary that was replaced, or two Recipes that ran in turn, leave several
    installations; each one needs its own receipts.
    """

    from vonk_control.run_switch_contract import (
        RunSwitchCleanupVerifyResult,
        RunSwitchUninstallResult,
    )

    return all(
        any(
            isinstance(receipt, RunSwitchUninstallResult)
            and receipt.installation_id == installation_id
            for receipt in receipts
        )
        and any(
            isinstance(receipt, RunSwitchCleanupVerifyResult)
            and receipt.installation_id == installation_id
            and receipt.final_verified is True
            and receipt.removed is True
            and receipt.active_runs == 0
            and receipt.installation_state in {None, "uninstalled"}
            for receipt in receipts
        )
        for installation_id in installation_ids
    )


def _validate_canary_cleanup_application(
    application: dict[str, object],
    *,
    installation_ids: Sequence[str],
    run_id: str,
) -> None:
    """Require terminal cleanup receipts from the profile application."""

    from vonk_control.fleet_profile_contract import (
        FleetProfileApplicationView,
        FleetProfileSwitchChildResult,
    )
    from vonk_control.run_switch_contract import RunSwitchStopResult

    try:
        typed = FleetProfileApplicationView.model_validate_json(_canonical(application))
    except ValueError as error:
        raise LifecycleError(
            "synthetic canary cleanup application is invalid"
        ) from error
    step_results = typed.progress.step_results
    if (
        typed.state != "succeeded"
        or len(step_results) != 1
        or not all(step.kind == "switch" for step in step_results.values())
    ):
        raise LifecycleError("synthetic canary cleanup profile receipt is incomplete")
    adapter = typed.progress.switch_adapter
    if adapter is None or adapter.result is None:
        raise LifecycleError("synthetic canary cleanup adapter result is missing")
    children = adapter.result.children
    kinds = [child.kind for child in children]
    # Cleanup stops what runs and removes what is installed. A canary that was
    # replaced by a new revision leaves an older installation too, so it may
    # take more than one cleanup child; every child must be one of the two.
    if not set(kinds) == {"stop", "cleanup"}:
        raise LifecycleError(
            f"synthetic canary cleanup child sequence is incomplete: {kinds}"
        )
    if any(child.state != "succeeded" for child in children):
        raise LifecycleError("synthetic canary cleanup child did not succeed")
    if not all(
        isinstance(child.result, FleetProfileSwitchChildResult) for child in children
    ):
        raise LifecycleError("synthetic canary cleanup child receipt is missing")
    stop_results = [
        receipt
        for child in children
        if child.kind == "stop"
        and isinstance(child.result, FleetProfileSwitchChildResult)
        for receipt in child.result.run_switch.phase_results
    ]
    if not any(
        isinstance(receipt, RunSwitchStopResult) and receipt.run_id == run_id
        for receipt in stop_results
    ):
        raise LifecycleError("synthetic canary stop receipt is incomplete")

    cleanup_results = [
        receipt
        for child in children
        if child.kind == "cleanup"
        and isinstance(child.result, FleetProfileSwitchChildResult)
        for receipt in child.result.run_switch.phase_results
    ]
    if not _installations_removed(cleanup_results, installation_ids):
        raise LifecycleError("synthetic canary removal receipt is incomplete")


class LocalBrowserController:
    def __init__(
        self,
        *,
        hostname: str,
        port: int,
        request_guard: Callable[[str, str, bytes | None], None] | None = None,
    ) -> None:
        if (
            not hostname
            or any(character in hostname for character in "\0\r\n /:")
            or not isinstance(port, int)
            or isinstance(port, bool)
            or not 1 <= port <= 65535
        ):
            raise LifecycleError("local browser port is invalid")
        self.hostname = hostname
        self.port = port
        # Sees every request the administrator session sends before it leaves.
        self.request_guard = request_guard

    def raw_request(
        self,
        method: str,
        path: str,
        body: bytes | None,
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[int, dict[str, list[str]], bytes]:
        if (
            timeout <= 0
            or method not in {"DELETE", "GET", "PATCH", "POST", "PUT"}
            or not path.startswith("/")
            or any(character in path for character in "\0\r\n")
            or (body is not None and not isinstance(body, bytes))
            or (body is not None and len(body) > MAXIMUM_RESPONSE_BYTES)
            or (method == "GET" and body is not None)
            or any(
                not isinstance(name, str)
                or not isinstance(value, str)
                or not name
                or name.lower() in {"connection", "content-length", "host"}
                or any(character in name for character in "\0\r\n:")
                or any(character in value for character in "\0\r\n")
                for name, value in headers.items()
            )
        ):
            raise LifecycleError("local browser request is invalid")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            connection.request(
                method,
                path,
                body=body,
                headers={"Host": self.hostname, **headers},
            )
            response = connection.getresponse()
            response_headers: dict[str, list[str]] = {}
            for name, value in response.getheaders():
                response_headers.setdefault(name.lower(), []).append(value)
            content = response.read(MAXIMUM_RESPONSE_BYTES + 1)
            if len(content) > MAXIMUM_RESPONSE_BYTES:
                raise LifecycleError("local browser response is too large")
            return response.status, response_headers, content
        except (OSError, http.client.HTTPException) as error:
            raise LifecycleError("local browser boundary is unavailable") from error
        finally:
            connection.close()

    def login(self, password: str, *, timeout: float) -> Client:
        body = json.dumps(
            {"password": password, "subject": "admin"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        status_code, headers, response = self.raw_request(
            "POST",
            "/api/auth/login",
            body,
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": f"https://{self.hostname}",
            },
            timeout,
        )
        if status_code != 200:
            raise LifecycleError("administrator login failed")
        try:
            session = json.loads(response)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LifecycleError("administrator login response is invalid") from error
        if session.get("subject") != "admin" or session.get("role") != "administrator":
            raise LifecycleError("administrator login response is invalid")
        cookies: dict[str, str] = {}
        for value in headers.get("set-cookie", []):
            pair = value.partition(";")[0]
            name, marker, cookie_value = pair.partition("=")
            if marker and name in {"vonk_session", "vonk_csrf"} and cookie_value:
                cookies[name] = cookie_value
        if set(cookies) != {"vonk_session", "vonk_csrf"}:
            raise LifecycleError("administrator session cookies are incomplete")
        fixed_headers = {
            "Cookie": "; ".join(
                f"{name}={cookies[name]}" for name in ("vonk_session", "vonk_csrf")
            ),
            "X-CSRF-Token": cookies["vonk_csrf"],
        }

        def transport(
            method: str,
            path: str,
            payload: bytes | None,
            request_headers: dict[str, str],
            request_timeout: float,
        ) -> tuple[int, bytes]:
            if self.request_guard is not None:
                try:
                    self.request_guard(method, path, payload)
                except ContractSkew as error:
                    raise LifecycleError(
                        f"the harness and the Controller disagree: {error}"
                    ) from error
            status, _response_headers, content = self.raw_request(
                method, path, payload, request_headers, request_timeout
            )
            return status, content

        return Client(
            f"https://{self.hostname}",
            None,
            timeout=timeout,
            headers=fixed_headers,
            transport=transport,
        )

    def bearer(self, token: str, *, timeout: float) -> Client:
        def transport(
            method: str,
            path: str,
            payload: bytes | None,
            request_headers: dict[str, str],
            request_timeout: float,
        ) -> tuple[int, bytes]:
            status, _response_headers, content = self.raw_request(
                method, path, payload, request_headers, request_timeout
            )
            return status, content

        return Client(
            f"https://{self.hostname}",
            token,
            timeout=timeout,
            transport=transport,
        )


class LostStartProof(NamedTuple):
    operation_id: str
    payload_digest: str
    run_id: str
    attempt: int
    fence: str
    endpoint: str
    container: str
    managed_containers: tuple[str, ...]
    response_sha256: str


class SparkLifecycle:
    def __init__(self, arguments: argparse.Namespace, graph: dict[str, object]) -> None:
        self.arguments = arguments
        self.graph = graph
        self.project = _spark_project_identity(arguments.run_id, arguments.platform)
        self.workspace = self._required_workspace()
        self.temporary_root: Path | None = None
        self.bundle: Path | None = None
        self.control: Client | None = None
        self.browser: LocalBrowserController | None = None
        self.synthetic_paths: list[Path] = []
        self.synthetic_interfaces: list[str] = []
        self.synthetic_fabric_octet = os.getpid() % 200 + 20
        self.firewall_environment: dict[str, str] = {}
        self.agent_installed = False
        self.lost_start_proof: LostStartProof | None = None
        self.synthetic_fixture_sha256: str | None = None
        # The release whose Controller this lane runs. The upgrade-carry lane
        # starts on the previous release and moves to the candidate.
        self.controller_generation: str = arguments.generation
        self.controller_release: Path = arguments.candidate_release
        _require_loopback_controller_boundary()
        self.tailnet_services = {
            "control": LOCAL_CONTROL_SERVICE,
            "hermes_api": LOCAL_HERMES_API_SERVICE,
            "hermes_dashboard": LOCAL_HERMES_DASHBOARD_SERVICE,
        }
        try:
            self.control_hostname = tailscale_service_hostname(
                self.tailnet_services["control"],
                LOCAL_DNS_SUFFIX,
            )
        except AcceptanceError as error:
            raise LifecycleError(
                "acceptance Tailscale Service name is invalid"
            ) from error
        self.origin = self._required_environment("INSTALLER_PUBLIC_ORIGIN")
        if self.origin != "https://install.vonkforge.ai":
            raise LifecycleError("installer public origin is invalid")
        self.machine = platform.machine()
        expected_machine = {"aarch64", "arm64"}
        if self.machine not in expected_machine or os.geteuid() == 0:
            raise LifecycleError(
                "Spark lifecycle is not running natively as an ordinary user"
            )

    @staticmethod
    def _required_environment(name: str, *, secret: bool = False) -> str:
        value = os.environ.get(name, "")
        if not value or any(character in value for character in "\0\r\n"):
            label = "secret" if secret else "input"
            raise LifecycleError(f"acceptance {label} {name} is missing or invalid")
        return value

    def _required_workspace(self) -> Path:
        workspace = Path(self._required_environment("VONK_ACCEPTANCE_WORKSPACE"))
        try:
            metadata = workspace.lstat()
        except OSError as error:
            raise LifecycleError("acceptance workspace is unavailable") from error
        if (
            workspace.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or not workspace.is_absolute()
        ):
            raise LifecycleError("acceptance workspace is unsafe")
        return workspace

    def __enter__(self) -> Self:
        self._assert_spark_target_is_fresh()
        try:
            self._start_controller()
            return self
        except BaseException:
            self._cleanup()
            raise

    def __exit__(self, *_error: object) -> None:
        self._cleanup()

    def _compose(self, *arguments: str) -> list[str]:
        overlay = os.environ.get("VONK_ACCEPTANCE_COMPOSE_OVERLAY")
        files = ["-f", "docker-compose.yaml", "-f", overlay] if overlay else []
        return [
            "docker",
            "compose",
            "--project-name",
            self.project,
            *files,
            *arguments,
        ]

    def _run_command(
        self,
        command: Sequence[str | Path],
        *,
        cwd: Path,
        timeout: int = 300,
        report_failure_output: bool = False,
        allowed_returncodes: tuple[int, ...] = (0,),
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                env=host_command_environment(),
                stdin=subprocess.DEVNULL if input_text is None else None,
                input=input_text,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise LifecycleError(
                f"acceptance command timed out after {timeout}s: "
                f"{Path(command[0]).name} {command[1] if len(command) > 1 else ''}".rstrip()
            ) from error
        except (OSError, subprocess.SubprocessError) as error:
            raise LifecycleError("acceptance command could not execute") from error
        if result.returncode not in allowed_returncodes:
            detail = (
                "; " + self._redact_diagnostics(result.stderr or result.stdout)
                if report_failure_output
                else ""
            )
            raise LifecycleError(
                f"acceptance command failed: {Path(command[0]).name} {command[1] if len(command) > 1 else ''}".rstrip()
                + detail
            )
        return result

    @staticmethod
    def _redact_diagnostics(raw: str, *, limit: int = 8_000) -> str:
        redacted = raw
        for name in (
            "VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_ID",
            "VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_SECRET",
            "VONK_ACCEPTANCE_LITELLM_UPSTREAM_KEY",
        ):
            value = os.environ.get(name)
            if value:
                redacted = redacted.replace(value, "<redacted>")
        redacted = re.sub(r"\x1b\[[0-9;]*m", "", redacted)
        # step-ca includes its short-lived enrollment JWT in request logs.
        redacted = re.sub(
            r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
            "<redacted-jwt>",
            redacted,
        )
        return redacted[-limit:]

    def _diagnostic_command(
        self, command: list[str]
    ) -> subprocess.CompletedProcess[str] | None:
        assert self.bundle is not None
        try:
            return subprocess.run(
                command,
                cwd=self.bundle,
                env=host_command_environment(),
                stdin=subprocess.DEVNULL,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    def _controller_startup_diagnostics(self) -> str:
        status = self._diagnostic_command(
            self._compose("ps", "--all", "--format", "json")
        )
        if status is None:
            return "controller diagnostics unavailable"
        if status.returncode != 0:
            output = self._redact_diagnostics(status.stderr or status.stdout)
            return f"controller status unavailable: {output or 'no output'}"
        try:
            rows = _compose_rows(status.stdout)
        except AcceptanceError:
            output = self._redact_diagnostics(status.stdout)
            return f"controller status invalid: {output or 'no output'}"
        states: list[str] = []
        broken: list[str] = []
        for row in sorted(rows, key=lambda item: str(item.get("Service", ""))):
            service = row.get("Service")
            if not isinstance(service, str) or not service:
                continue
            state = str(row.get("State", "unknown"))
            health = str(row.get("Health", "none")) or "none"
            exit_code = str(row.get("ExitCode", "unknown"))
            states.append(f"{service}={state}/{health}/exit-{exit_code}")
            if state != "running" or health != "healthy":
                broken.append(service)
        details = f"states: {', '.join(states) or 'none'}"
        if not broken:
            return details
        logs = self._diagnostic_command(
            self._compose("logs", "--no-color", "--tail", "80", *broken)
        )
        if logs is None:
            return f"{details}; logs unavailable"
        output = self._redact_diagnostics(logs.stdout or logs.stderr)
        return f"{details}; failing service logs:\n{output or 'no output'}"

    # The whole diagnostics block is appended to one LifecycleError, so it is
    # spent from a single budget in priority order rather than letting each
    # section grow unbounded.
    _DIAGNOSTIC_BUDGET = 8_400

    def _bounded_diagnostics(self, sections: Sequence[tuple[str, str]]) -> str:
        """Keep each prioritized diagnostic visible within one total budget."""

        rendered: list[str] = []
        remaining = self._DIAGNOSTIC_BUDGET
        for index, (title, body) in enumerate(sections):
            unrendered = sections[index:]
            heading_cost = sum(len(item_title) + 2 for item_title, _ in unrendered)
            heading_cost += max(0, len(unrendered) - 1)
            allowance = (remaining - heading_cost) // len(unrendered)
            if allowance <= 0:
                break
            entry = f"{title}:\n" + self._redact_diagnostics(body, limit=allowance)
            rendered.append(entry)
            remaining -= len(entry) + (1 if index < len(sections) - 1 else 0)
        return "\n".join(rendered)

    def _installation_failure(self, stage: str, error: Exception) -> LifecycleError:
        sections: list[tuple[str, str]] = [("installer error", str(error))]
        if getattr(self, "bundle", None) is not None:
            # Service states come first: a restarting or unhealthy worker is the
            # difference between "the work is stuck" and "nothing is draining
            # the queue", and an interleaved log tail cannot show it.
            sections.append(
                ("controller services", self._controller_startup_diagnostics())
            )
            for service in ("control-worker", "control-api"):
                logs = self._diagnostic_command(
                    self._compose("logs", "--no-color", "--tail", "80", service)
                )
                if logs is not None:
                    sections.append(
                        (f"{service} diagnostics", logs.stdout or logs.stderr)
                    )
            for unit in (
                "vonk-forge-agent.service",
                "vonk-forge-package-helper.service",
            ):
                journal = self._diagnostic_command(
                    ["sudo", "journalctl", "--no-pager", "--lines=40", f"--unit={unit}"]
                )
                if journal is not None:
                    sections.append(
                        (f"{unit} diagnostics", journal.stdout or journal.stderr)
                    )
        diagnostics = self._bounded_diagnostics(sections)
        return LifecycleError(
            f"{stage} failed; {diagnostics or 'installer diagnostics unavailable'}"
        )

    def _cleanup(self) -> None:
        failures: list[BaseException] = []
        if _agent_package_installed():
            try:
                self._run_command(
                    ["sudo", "/usr/bin/dpkg", "--purge", "vonk-forge-agent"],
                    cwd=Path("/"),
                    timeout=120,
                )
            except BaseException as error:  # noqa: BLE001 - continue cleanup.
                failures.append(error)
        for path in reversed(getattr(self, "synthetic_paths", [])):
            try:
                self._run_command(
                    ["sudo", "/usr/bin/rm", "-f", "--", os.fspath(path)],
                    cwd=Path("/"),
                    timeout=30,
                )
            except BaseException as error:  # noqa: BLE001 - continue cleanup.
                failures.append(error)
        try:
            self._run_command(
                [
                    "sudo",
                    "/usr/bin/rm",
                    "-rf",
                    "--",
                    "/etc/vonk-forge-agent",
                    "/var/lib/vonk-forge-agent",
                ],
                cwd=Path("/"),
                timeout=60,
            )
        except BaseException as error:  # noqa: BLE001 - continue cleanup.
            failures.append(error)
        for interface in reversed(getattr(self, "synthetic_interfaces", [])):
            interface_path = Path("/sys/class/net") / interface
            if not interface_path.exists():
                continue
            try:
                self._run_command(
                    ["sudo", "/usr/sbin/ip", "link", "delete", interface],
                    cwd=Path("/"),
                    timeout=30,
                )
            except BaseException as error:  # noqa: BLE001 - continue cleanup.
                if interface_path.exists():
                    failures.append(error)
        bundle = getattr(self, "bundle", None)
        if bundle is not None:
            try:
                self._run_command(
                    self._compose(
                        "down",
                        "--volumes",
                        "--remove-orphans",
                        "--timeout",
                        "30",
                    ),
                    cwd=bundle,
                    timeout=120,
                )
            except BaseException as error:  # noqa: BLE001 - continue cleanup.
                failures.append(error)
        root = getattr(self, "temporary_root", None)
        if root is not None:
            shutil.rmtree(root, ignore_errors=False)
            self.temporary_root = None
        if failures:
            raise LifecycleError(
                "isolated Spark lifecycle cleanup failed"
            ) from failures[0]

    @staticmethod
    def _assert_spark_target_is_fresh() -> None:
        if _agent_package_installed() or any(
            path.exists() or path.is_symlink()
            for path in (
                Path("/etc/vonk-forge-agent"),
                Path("/var/lib/vonk-forge-agent"),
                AGENT_BINARY,
            )
        ):
            raise LifecycleError("Spark lifecycle target is not fresh")

    def _controller_site_values(self) -> dict[str, str]:
        """Synthetic Spark networks, set in .env where an operator would."""
        return {
            "VONK_MANAGEMENT_CIDRS": "172.16.0.0/12",
            "VONK_DIRECT_FABRIC_CIDRS": f"198.19.{self.synthetic_fabric_octet}.0/24",
        }

    def _start_controller(self) -> None:
        self.temporary_root = Path(
            tempfile.mkdtemp(prefix="vonk-spark-lifecycle-", dir=self.workspace)
        )
        child_environment = command_environment(self.temporary_root / "workstation")
        responses = nas_responses(
            nas_ip="127.0.0.1",
            tailnet_suffix=LOCAL_DNS_SUFFIX,
            oauth_client_id=DISABLED_TAILSCALE_CREDENTIAL,
            oauth_client_secret=DISABLED_TAILSCALE_CREDENTIAL,
            upstream_key=self._required_environment(
                "VONK_ACCEPTANCE_LITELLM_UPSTREAM_KEY", secret=True
            ),
            hermes=False,
            control_service=self.tailnet_services["control"],
            hermes_dashboard_service=self.tailnet_services["hermes_dashboard"],
        )
        release_url = (
            f"{self.origin}/artifacts/{self.arguments.channel}/releases/"
            f"{getattr(self, 'controller_generation', self.arguments.generation)}"
        )
        # Kept so an upgrade can rerun the installer with the same answers.
        self._controller_inputs = (child_environment, responses)
        self.bundle = generate_bundle(
            self.temporary_root / "controller",
            candidate_url=f"{release_url}/bootstraps/nas",
            child_environment=child_environment,
            responses=responses,
        )
        self._reapply_controller_site()
        library_root = self._required_environment("VONK_RECIPE_LIBRARY_ROOT")
        self.synthetic_canary_fixture = _canonical_canary_fixture(Path(library_root))
        self._assert_project_is_empty()
        self._assert_compose_image_graph()
        try:
            self._run_command(
                self._local_controller_up_command(),
                cwd=self.bundle,
                timeout=420,
                report_failure_output=True,
            )
        except LifecycleError as error:
            diagnostics = self._controller_startup_diagnostics()
            raise LifecycleError(
                "candidate controller startup failed; "
                f"{self._redact_diagnostics(str(error))}; {diagnostics}"
            ) from error
        status = self._run_command(
            self._compose("ps", "--all", "--format", "json"), cwd=self.bundle
        )
        try:
            assert_compose_services_healthy(status.stdout, LOCAL_CONTROLLER_SERVICES)
        except AcceptanceError as error:
            raise LifecycleError(
                "candidate controller services are not healthy"
            ) from error
        self._assert_running_publication_images()
        # Both package lanes need deterministic NVIDIA discovery so the agent
        # can start on a GPU-less CI runner. Only ARM64 consumes it in a recipe.
        self.synthetic_fixture_sha256 = self._materialize_synthetic_device()
        self._prepare_synthetic_firewall_environment()
        boundary = LocalBrowserController(
            hostname=self.control_hostname,
            port=self._local_browser_port(),
            request_guard=self._controller_request_guard(),
        )
        self.browser = boundary
        password = self._read_secret("admin-password")
        self.control = boundary.login(password, timeout=30)
        del password

    def _controller_request_guard(
        self,
    ) -> Callable[[str, str, bytes | None], None] | None:
        """Check what the harness sends against the Controller it talks to.

        A lane that drives a Controller other than this source's own overrides
        it; the fresh-install lanes run the Controller they were built from.
        """
        return None

    def _reapply_controller_site(self) -> None:
        """Set this lane's site values in the bundle the installer wrote."""
        assert self.bundle is not None
        _set_bundle_environment(self.bundle, self._controller_site_values())
        _configure_acceptance_renewal(
            self.bundle,
            lifetime_seconds=CERTIFICATE_LIFETIME_SECONDS,
            agent_source_address=f"172.31.{self.synthetic_fabric_octet}.1",
            caddyfile=self._acceptance_caddyfile(),
        )
        self._configure_receipt_fault_relay()

    def _configure_receipt_fault_relay(self) -> None:
        assert self.bundle is not None
        root = self.bundle.parent / "acceptance-receipts"
        root.mkdir(mode=0o770, exist_ok=True)
        self._run_command(
            ["sudo", "/usr/bin/chown", f"{os.getuid()}:10001", os.fspath(root)],
            cwd=self.bundle,
            timeout=30,
        )
        os.chmod(root, 0o770)
        relay = self.bundle.parent / "acceptance-receipt-fault.py"
        shutil.copyfile(Path(__file__).with_name("receipt_fault.py"), relay)
        os.chmod(relay, 0o644)
        path = self.bundle / "docker-compose.yaml"
        compose = yaml.safe_load(path.read_text(encoding="utf-8"))
        service = compose["services"]["control-api"]
        for mount in (
            f"{relay}:/acceptance/receipt_fault.py:ro",
            f"{root}:/acceptance-state",
        ):
            if mount not in service["volumes"]:
                service["volumes"].append(mount)
        service["command"] = ["python", "/acceptance/receipt_fault.py"]
        path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")

    def _arm_start_receipt_loss(self) -> None:
        assert self.bundle is not None
        if self.lost_start_proof is not None:
            return
        root = self.bundle.parent / "acceptance-receipts"
        for name in ("blocked.json", "replayed.json", "recovered.json"):
            if (root / name).exists():
                raise LifecycleError("lost Start receipt acceptance state is not fresh")
        (root / "armed.json").write_text(
            json.dumps({"id": str(uuid.uuid4())}),
            encoding="utf-8",
        )
        os.chmod(root / "armed.json", 0o644)

    def _container_for_replay(self, run_id: str) -> str:
        assert self.temporary_root is not None
        result = self._run_command(
            [
                "docker",
                "inspect",
                "--format",
                "{{.Id}} {{.State.Running}}",
                f"vonk-{run_id}",
            ],
            cwd=self.temporary_root,
            timeout=30,
        ).stdout.strip()
        if re.fullmatch(r"[0-9a-f]{64} true", result) is None:
            raise LifecycleError("lost Start receipt canary is not serving")
        return result

    def _managed_for_replay(self) -> tuple[str, ...]:
        assert self.temporary_root is not None
        lines = self._run_command(
            ["docker", "ps", "--no-trunc", "--format", "{{.ID}} {{.Names}}"],
            cwd=self.temporary_root,
            timeout=30,
        ).stdout.splitlines()
        return tuple(
            sorted(
                line
                for line in lines
                if re.fullmatch(
                    r"[0-9a-f]{64} vonk-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",
                    line,
                )
            )
        )

    def _direct_canary_inference(self, endpoint: str) -> str:
        """Probe from the authorized NAS namespace before route publication."""
        assert self.bundle is not None
        bundle = self.bundle
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname != f"172.31.{self.synthetic_fabric_octet}.1"
            or parsed.port is None
            or not 1 <= parsed.port <= 65535
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise LifecycleError(
                "lost Start receipt endpoint is outside the disposable Spark"
            )

        def transport(
            method: str,
            path: str,
            body: bytes | None,
            _headers: Mapping[str, str],
            _timeout: float,
        ) -> tuple[int, bytes]:
            probe = self._run_command(
                self._compose(
                    "exec",
                    "-T",
                    "litellm",
                    "python",
                    "-c",
                    "import base64,json,sys,urllib.request; "
                    "req=urllib.request.Request(sys.argv[1]+sys.argv[3], data=sys.stdin.buffer.read(), method=sys.argv[2], headers={'Content-Type':'application/json'}); "
                    "response=urllib.request.urlopen(req,timeout=30); "
                    "print(json.dumps({'status':response.status,'body':base64.b64encode(response.read(1048577)).decode()}))",
                    endpoint.rstrip("/"),
                    method,
                    path,
                ),
                cwd=bundle,
                timeout=40,
                input_text="" if body is None else body.decode("utf-8"),
            )
            observed = require_object(json.loads(probe.stdout), "direct canary probe")
            status, encoded = observed.get("status"), observed.get("body")
            if type(status) is not int or not isinstance(encoded, str):
                raise LifecycleError("direct canary probe is invalid")
            return status, base64.b64decode(encoded, validate=True)

        inference = Client(
            endpoint.rstrip("/"),
            None,
            timeout=30,
            headers={"X-Vonk-Acceptance-Probe": "lost-start-receipt"},
            transport=transport,
        )
        fixture = self.synthetic_canary_fixture
        return self._run_canonical_inference(
            inference, fixture.serving_check, fixture.slug
        )

    def _recover_lost_start_receipt(self, node_id: str) -> None:
        if getattr(self, "lost_start_proof", None) is not None:
            return
        bundle = getattr(self, "bundle", None)
        if bundle is None:
            return
        assert self.temporary_root is not None
        path = bundle.parent / "acceptance-receipts/blocked.json"
        if not path.is_file():
            return
        if path.stat().st_size > 64 * 1024:
            raise LifecycleError("lost Start receipt exceeds its evidence bound")
        receipt = require_object(
            json.loads(path.read_text(encoding="utf-8")), "lost Start receipt"
        )
        fence, endpoint = receipt.get("fence"), receipt.get("endpoint")
        if (
            not isinstance(fence, str)
            or UUID.fullmatch(fence) is None
            or not isinstance(endpoint, str)
        ):
            raise LifecycleError("lost Start receipt identity is invalid")
        rows = self._psql(
            "SELECT o.id,o.payload_digest,o.payload->>'run_id',a.attempt,o.kind,o.node_id,a.state,COALESCE(o.payload->>'phase','single') "
            "FROM agent_operations o JOIN agent_operation_attempts a ON a.operation_id=o.id "
            f"WHERE a.fence='{fence}'"
        )
        if len(rows) != 1 or len(rows[0]) != 8:
            raise LifecycleError("lost Start receipt has no accepted Controller claim")
        operation_id, payload_digest, run_id, attempt, kind, owner, state, phase = rows[
            0
        ]
        if (
            UUID.fullmatch(operation_id) is None
            or SHA256.fullmatch(payload_digest) is None
            or UUID.fullmatch(run_id) is None
            or not attempt.isdigit()
            or kind != "recipe.start"
            or phase != "single"
            or owner != node_id
            or state != "running"
        ):
            raise LifecycleError(
                "lost Start receipt is not the live exact canary Start"
            )
        before_container = self._container_for_replay(run_id)
        before_managed = self._managed_for_replay()
        before_response = self._direct_canary_inference(endpoint)
        self._run_command(
            ["sudo", "/usr/bin/systemctl", "stop", "vonk-forge-agent.service"],
            cwd=self.temporary_root,
            timeout=30,
        )
        self._run_command(
            [
                "sudo",
                "/usr/bin/python3",
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b'acceptance-lost-start-journal')",
                os.fspath(AGENT_DATA / "state.sqlite"),
            ],
            cwd=self.temporary_root,
            timeout=30,
        )
        # Retire every pre-restart lease using Controller time. A buffered
        # receipt from the stopped process must be stale even if the network
        # delivers it after the relay is released. The Controller still owns
        # every expiry/retry transition; this observer never writes SQL.
        expiry_deadline = time.monotonic() + 120
        while True:
            leases = self._psql(
                "SELECT count(*) FROM agent_operation_attempts "
                f"WHERE operation_id='{operation_id}' AND lease_deadline>clock_timestamp()"
            )
            if leases == [["0"]]:
                break
            if time.monotonic() >= expiry_deadline:
                raise LifecycleError("pre-restart Start leases did not expire")
            time.sleep(1)
        self._run_command(
            ["sudo", "/usr/bin/systemctl", "start", "vonk-forge-agent.service"],
            cwd=self.temporary_root,
            timeout=30,
        )
        recovered = bundle.parent / "acceptance-receipts/recovered.json"
        recovered.write_text(json.dumps({"fence": fence}), encoding="utf-8")
        os.chmod(recovered, 0o644)
        self.lost_start_proof = LostStartProof(
            operation_id,
            payload_digest,
            run_id,
            int(attempt),
            fence,
            endpoint,
            before_container,
            before_managed,
            before_response,
        )
        if self._direct_canary_inference(endpoint) != before_response:
            raise LifecycleError(
                "canary inference changed after the unacknowledged Start journal was lost"
            )

    def _verify_lost_start_replay(self, run_id: str) -> None:
        assert self.bundle is not None
        proof = self.lost_start_proof
        if proof is None or proof.run_id != run_id:
            raise LifecycleError("canary never exercised an unacknowledged exact Start")
        path = self.bundle.parent / "acceptance-receipts/replayed.json"
        if not path.is_file():
            raise LifecycleError(
                "Controller did not accept a fresh Start receipt after journal loss"
            )
        receipt = require_object(
            json.loads(path.read_text(encoding="utf-8")), "replayed Start receipt"
        )
        fence = receipt.get("fence")
        if (
            not isinstance(fence, str)
            or UUID.fullmatch(fence) is None
            or fence == proof.fence
            or receipt.get("endpoint") != proof.endpoint
        ):
            raise LifecycleError(
                "Start replay did not report the same serving effect under a fresh fence"
            )
        rows = self._psql(
            "SELECT o.id,o.payload_digest,o.payload->>'run_id',a.attempt,a.state "
            "FROM agent_operations o JOIN agent_operation_attempts a ON a.operation_id=o.id "
            f"WHERE a.fence='{fence}'"
        )
        if (
            len(rows) != 1
            or len(rows[0]) != 5
            or rows[0][:3] != [proof.operation_id, proof.payload_digest, proof.run_id]
            or not rows[0][3].isdigit()
            or int(rows[0][3]) <= proof.attempt
            or rows[0][4] != "succeeded"
        ):
            raise LifecycleError(
                "Start replay changed the accepted operation or exact payload"
            )
        if (
            self._container_for_replay(run_id) != proof.container
            or self._managed_for_replay() != proof.managed_containers
            or self._direct_canary_inference(proof.endpoint) != proof.response_sha256
        ):
            raise LifecycleError(
                "Start replay replaced, duplicated or changed the serving effect"
            )
        print(
            json.dumps(
                {
                    "event": "exact-start-replayed-after-journal-loss",
                    "operation_id": proof.operation_id,
                    "run_id": proof.run_id,
                    "payload_digest": proof.payload_digest,
                    "original_attempt": proof.attempt,
                    "replay_attempt": int(rows[0][3]),
                    "container": proof.container.split()[0],
                }
            ),
            flush=True,
        )

    def _acceptance_caddyfile(self) -> str | None:
        """The Caddyfile of the Controller release this lane runs."""
        return None

    def _local_controller_up_command(self) -> list[str]:
        return self._compose(
            "up",
            "-d",
            "--wait",
            "--wait-timeout",
            "360",
            "--remove-orphans",
            *sorted(LOCAL_CONTROLLER_SERVICES),
        )

    def _local_browser_port(self) -> int:
        assert self.bundle is not None
        result = self._run_command(
            self._compose("port", "caddy", "8080"), cwd=self.bundle
        )
        matched = re.fullmatch(r"127\.0\.0\.1:([1-9][0-9]{0,4})\n?", result.stdout)
        if matched is None:
            raise LifecycleError("local browser publication is invalid")
        port = int(matched.group(1))
        if port > 65535:
            raise LifecycleError("local browser publication is invalid")
        return port

    def _assert_project_is_empty(self) -> None:
        assert self.bundle is not None
        containers = self._run_command(
            [
                "docker",
                "ps",
                "--all",
                "--quiet",
                "--filter",
                f"label=com.docker.compose.project={self.project}",
            ],
            cwd=self.bundle,
        ).stdout.strip()
        volumes = self._run_command(
            [
                "docker",
                "volume",
                "ls",
                "--quiet",
                "--filter",
                f"label=com.docker.compose.project={self.project}",
            ],
            cwd=self.bundle,
        ).stdout.strip()
        if containers or volumes:
            raise LifecycleError("isolated Compose project is not empty")

    def _assert_compose_image_graph(self) -> None:
        assert self.bundle is not None
        candidate = _read_canonical_document(
            getattr(self, "controller_release", self.arguments.candidate_release),
            "candidate release object",
        )
        images = _object(candidate.get("images"), "candidate image graph")
        if candidate.get("generation") != getattr(
            self, "controller_generation", self.arguments.generation
        ):
            raise LifecycleError("candidate controller generation is invalid")
        configured = self._run_command(
            self._compose("--profile", "hermes", "config", "--format", "json"),
            cwd=self.bundle,
        )
        try:
            document = json.loads(configured.stdout)
            services = document["services"]
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise LifecycleError("Compose image graph is invalid") from error
        if not isinstance(services, dict):
            raise LifecycleError("Compose image graph is invalid")
        base = self._run_command(
            [
                "docker",
                "compose",
                "--project-name",
                self.project,
                "--profile",
                "hermes",
                "config",
                "--format",
                "json",
            ],
            cwd=self.bundle,
        )
        try:
            base_services = json.loads(base.stdout)["services"]
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise LifecycleError("base Compose image graph is invalid") from error
        if not isinstance(base_services, dict):
            raise LifecycleError("base Compose image graph is invalid")
        for service in base_services.values():
            image = service.get("image") if isinstance(service, dict) else None
            if not isinstance(image, str) or not is_channel_image(
                image, self.arguments.channel
            ):
                raise LifecycleError("base Compose image does not follow its channel")
        for role, service in COMPOSE_IMAGE_ROLES.items():
            configured_service = services.get(service)
            expected_image = str(images.get(role)).split("@", 1)[0].rsplit(":", 1)[
                0
            ] + (":dev" if self.arguments.channel == "dev" else ":latest")
            if os.environ.get("VONK_ACCEPTANCE_COMPOSE_OVERLAY"):
                expected_image = str(images.get(role))
            if (
                not isinstance(configured_service, dict)
                or configured_service.get("image") != expected_image
            ):
                raise LifecycleError("Compose image graph differs from publication")
        provisioner = services.get("hermes-litellm-key-provisioner")
        provisioner_expected = str(images["litellm"])
        if not os.environ.get("VONK_ACCEPTANCE_COMPOSE_OVERLAY"):
            provisioner_expected = provisioner_expected.split("@", 1)[0].rsplit(":", 1)[
                0
            ] + (":dev" if self.arguments.channel == "dev" else ":latest")
        if (
            not isinstance(provisioner, dict)
            or provisioner.get("image") != provisioner_expected
        ):
            raise LifecycleError("Compose image graph differs from publication")
        for service in services.values():
            image = service.get("image") if isinstance(service, dict) else None
            if (
                not isinstance(image, str)
                or (
                    image.startswith("ghcr.io/carstvaartjes/vonk-forge-")
                    and os.environ.get("VONK_ACCEPTANCE_COMPOSE_OVERLAY")
                    and not is_immutable_image(image)
                )
                or (
                    (
                        not os.environ.get("VONK_ACCEPTANCE_COMPOSE_OVERLAY")
                        or not image.startswith("ghcr.io/carstvaartjes/vonk-forge-")
                    )
                    and not is_channel_image(image, self.arguments.channel)
                )
            ):
                raise LifecycleError("Compose image does not follow its channel")

    def _assert_running_publication_images(self) -> None:
        """A moving alias must still resolve to the candidate being qualified."""
        assert self.bundle is not None
        candidate = _read_canonical_document(
            getattr(self, "controller_release", self.arguments.candidate_release),
            "candidate release object",
        )
        images = _object(candidate.get("images"), "candidate image graph")
        for role, service in COMPOSE_IMAGE_ROLES.items():
            if service not in LOCAL_CONTROLLER_SERVICES:
                continue
            container = self._run_command(
                self._compose("ps", "-q", service), cwd=self.bundle
            ).stdout.strip()
            observed = self._run_command(
                ["docker", "inspect", "--format", "{{.Image}}", container],
                cwd=self.bundle,
            ).stdout.strip()
            expected = self._run_command(
                [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    "{{.Id}}",
                    str(images[role]),
                ],
                cwd=self.bundle,
            ).stdout.strip()
            if not observed or observed != expected:
                raise LifecycleError("running channel image differs from publication")

    def _read_secret(self, relative: str) -> str:
        assert self.bundle is not None
        path = self.bundle / "secrets" / relative
        try:
            metadata = path.lstat()
            value = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as error:
            raise LifecycleError("controller secret is unavailable") from error
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or not value
        ):
            raise LifecycleError("controller secret is unsafe")
        return value

    def _materialize_synthetic_device(self) -> str:
        assert self.temporary_root is not None
        raw, digest = _synthetic_device_fixture(self.arguments.platform)
        cdi_target = Path("/etc/cdi/vonk-spark-acceptance.json")
        smi_target = Path("/usr/bin/nvidia-smi")
        ctk_target = Path("/usr/bin/nvidia-ctk")
        if any(
            path.exists() or path.is_symlink()
            for path in (cdi_target, smi_target, ctk_target)
        ):
            raise LifecycleError("synthetic device fixture target already exists")
        cdi = self.temporary_root / "synthetic-cdi.json"
        smi = self.temporary_root / "synthetic-nvidia-smi"
        ctk = self.temporary_root / "synthetic-nvidia-ctk"
        cdi.write_bytes(raw)
        smi.write_text(
            "#!/bin/sh\nprintf '%s\\n' 'NVIDIA GB10, [N/A], [N/A], synthetic-ci'\n",
            encoding="ascii",
        )
        ctk.write_text(
            "#!/bin/sh\n"
            'test "$#" = 2 && test "$1" = cdi && test "$2" = list\n'
            "printf '%s\\n' 'nvidia.com/gpu=all'\n",
            encoding="ascii",
        )
        os.chmod(cdi, 0o600)
        os.chmod(smi, 0o700)
        os.chmod(ctk, 0o700)
        docker = Path("/usr/bin/docker")
        if not docker.is_file() or not os.access(docker, os.X_OK):
            raise LifecycleError("native synthetic CDI prerequisite is unavailable")
        self._run_command(
            ["sudo", "/usr/bin/install", "-D", "-m", "0644", cdi, cdi_target],
            cwd=self.temporary_root,
        )
        self.synthetic_paths.append(cdi_target)
        self._run_command(
            ["sudo", "/usr/bin/install", "-m", "0755", smi, smi_target],
            cwd=self.temporary_root,
        )
        self.synthetic_paths.append(smi_target)
        self._run_command(
            ["sudo", "/usr/bin/install", "-m", "0755", ctk, ctk_target],
            cwd=self.temporary_root,
        )
        self.synthetic_paths.append(ctk_target)
        listed = self._run_command(
            ["/usr/bin/nvidia-ctk", "cdi", "list"], cwd=self.temporary_root
        ).stdout.splitlines()
        if "nvidia.com/gpu=all" not in {line.strip() for line in listed}:
            raise LifecycleError("synthetic CDI device was not discovered")
        self._verify_synthetic_docker_device()
        return digest

    def _verify_synthetic_docker_device(self) -> None:
        assert self.bundle is not None and self.temporary_root is not None
        caddy_container = self._run_command(
            self._compose("ps", "--quiet", "caddy"), cwd=self.bundle
        ).stdout.strip()
        if re.fullmatch(r"[0-9a-f]{64}", caddy_container) is None:
            raise LifecycleError("synthetic CDI probe image is unavailable")
        image = self._run_command(
            ["docker", "inspect", "--format", "{{.Config.Image}}", caddy_container],
            cwd=self.bundle,
        ).stdout.strip()
        if not image or "\x00" in image or "\n" in image or "\r" in image:
            raise LifecycleError("synthetic CDI probe image is unavailable")
        name = f"vonk-cdi-probe-{self.project}"
        try:
            self._run_command(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--name",
                    name,
                    "--network",
                    "none",
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--device",
                    "nvidia.com/gpu=all",
                    "--entrypoint",
                    "/bin/sh",
                    image,
                    "-eu",
                    "-c",
                    'test "${VONK_SYNTHETIC_CDI:-}" = 1',
                ],
                cwd=self.bundle,
            )
        except LifecycleError as error:
            raise LifecycleError("native Docker CDI support is unavailable") from error

    def _prepare_synthetic_firewall_environment(self) -> None:
        assert self.bundle is not None and self.temporary_root is not None
        suffix = self.synthetic_fabric_octet
        management_interface = f"vmgt{os.getpid() % 100000}"
        management_peer = f"vnas{os.getpid() % 100000}"
        fabric_interface = f"vfab{os.getpid() % 100000}"
        node_management_ip = f"172.31.{suffix}.1"
        nas_management_ip = f"172.31.{suffix}.2"
        node_fabric_ip = f"198.19.{suffix}.1"
        peer_fabric_ip = f"198.19.{suffix}.2"
        if any(
            len(interface) > 15
            for interface in (
                management_interface,
                management_peer,
                fabric_interface,
            )
        ):
            raise LifecycleError("synthetic firewall interface identity is invalid")
        litellm_container = self._run_command(
            self._compose("ps", "--quiet", "litellm"), cwd=self.bundle
        ).stdout.strip()
        if re.fullmatch(r"[0-9a-f]{64}", litellm_container) is None:
            raise LifecycleError("synthetic firewall source container is invalid")
        litellm_pid = self._run_command(
            [
                "docker",
                "inspect",
                "--format",
                "{{.State.Pid}}",
                litellm_container,
            ],
            cwd=self.bundle,
        ).stdout.strip()
        if re.fullmatch(r"[1-9][0-9]{1,9}", litellm_pid) is None:
            raise LifecycleError("synthetic firewall source namespace is invalid")
        if any(
            re.fullmatch(r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}", value) is None
            for value in (node_management_ip, nas_management_ip)
        ):
            raise LifecycleError("synthetic firewall management topology is invalid")

        self._run_command(
            [
                "sudo",
                "/usr/sbin/ip",
                "link",
                "add",
                management_interface,
                "type",
                "veth",
                "peer",
                "name",
                management_peer,
            ],
            cwd=self.temporary_root,
        )
        self.synthetic_interfaces.append(management_interface)
        self._run_command(
            [
                "sudo",
                "/usr/sbin/ip",
                "link",
                "set",
                management_peer,
                "netns",
                litellm_pid,
            ],
            cwd=self.temporary_root,
        )
        self._run_command(
            [
                "sudo",
                "/usr/sbin/ip",
                "address",
                "add",
                f"{node_management_ip}/30",
                "dev",
                management_interface,
            ],
            cwd=self.temporary_root,
        )
        self._run_command(
            ["sudo", "/usr/sbin/ip", "link", "set", management_interface, "up"],
            cwd=self.temporary_root,
        )
        for command in (
            [
                "/usr/bin/nsenter",
                "--target",
                litellm_pid,
                "--net",
                "/usr/sbin/ip",
                "address",
                "add",
                f"{nas_management_ip}/30",
                "dev",
                management_peer,
            ],
            [
                "/usr/bin/nsenter",
                "--target",
                litellm_pid,
                "--net",
                "/usr/sbin/ip",
                "link",
                "set",
                management_peer,
                "up",
            ],
        ):
            self._run_command(["sudo", *command], cwd=self.temporary_root)
        self._run_command(
            [
                "sudo",
                "/usr/sbin/ip",
                "link",
                "add",
                fabric_interface,
                "type",
                "dummy",
            ],
            cwd=self.temporary_root,
        )
        self.synthetic_interfaces.append(fabric_interface)
        self._run_command(
            [
                "sudo",
                "/usr/sbin/ip",
                "address",
                "add",
                f"{node_fabric_ip}/24",
                "dev",
                fabric_interface,
            ],
            cwd=self.temporary_root,
        )
        self._run_command(
            ["sudo", "/usr/sbin/ip", "link", "set", fabric_interface, "up"],
            cwd=self.temporary_root,
        )
        self.firewall_environment = {
            "VONK_NAS_MANAGEMENT_IP": nas_management_ip,
            "VONK_NODE_MANAGEMENT_IP": node_management_ip,
            "VONK_NODE_FABRIC_IP": node_fabric_ip,
            "VONK_PEER_FABRIC_IP": peer_fabric_ip,
        }

    def _installer_environment(self, *, baseline: bool) -> dict[str, str]:
        assert self.temporary_root is not None
        release_root = (
            f"{self.origin}/artifacts/{self.arguments.channel}/releases/"
            f"{self.arguments.generation}"
        )
        local_release = (
            self.arguments.baseline_release
            if baseline
            else self.arguments.candidate_release
        )
        signature = local_release.parent / "release.sig"
        for path in (local_release, signature):
            try:
                metadata = path.lstat()
            except OSError as error:
                raise LifecycleError("signed release input is unavailable") from error
            if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                raise LifecycleError("signed release input is unsafe")
        base = f"{release_root}/acceptance-baseline" if baseline else release_root
        environment = {
            "HOME": os.environ.get("HOME", "/tmp"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "TMPDIR": os.fspath(self.temporary_root),
            "VONK_CONTROLLER_ADDRESS": CONTROLLER_ADDRESS,
            "VONK_INSTALL_BASE_URL": base,
            "VONK_INSTALL_RELEASE_MANIFEST": os.fspath(local_release),
            "VONK_INSTALL_RELEASE_SIGNATURE": os.fspath(signature),
        }
        environment.update(getattr(self, "firewall_environment", {}))
        return environment

    def _bootstrap_url(self, *, baseline: bool) -> str:
        release = (
            f"{self.origin}/artifacts/{self.arguments.channel}/releases/"
            f"{self.arguments.generation}"
        )
        if baseline:
            release += "/acceptance-baseline"
        return f"{release}/bootstraps/spark"

    def observe(self) -> dict[str, object]:
        if self.control is None or self.bundle is None or self.temporary_root is None:
            raise LifecycleError("candidate controller is not ready")
        grant_id, enrollment_url, ca_sha256, pairing_token = self._create_grant()
        try:
            _run_spark_bootstrap(
                self._bootstrap_url(baseline=False),
                cwd=self.temporary_root,
                environment=self._installer_environment(baseline=False),
                enrollment_url=enrollment_url,
                ca_sha256=ca_sha256,
                pairing_token=pairing_token,
            )
        except AcceptanceError as error:
            raise self._installation_failure(
                "candidate Spark installation", error
            ) from error
        finally:
            del pairing_token
        self.agent_installed = True
        try:
            verify_effective_sandbox()
        except (SandboxError, OSError, subprocess.SubprocessError) as error:
            raise self._installation_failure(
                "Spark service sandbox verification", error
            ) from error
        self._prepare_podman_apparmor_profile()
        candidate = self._wait_for_agent_identity(
            package_version=str(self.graph["candidate_version"]), timeout=180
        )
        use_count = self._pairing_grant_use_count(grant_id)
        node_id = str(candidate["node_id"])
        canary = self._run_synthetic_canary(node_id)
        synthetic_device = {
            "architecture": self.arguments.platform,
            "cdi_name": "nvidia.com/gpu=all",
            "fixture_sha256": self.synthetic_fixture_sha256,
            "physical_gpu": False,
            "provenance": "ci-only-synthetic-cdi",
            "synthetic": True,
        }
        renewal = self._observe_renewal(node_id, str(candidate["serial"]))
        return {
            "canary": canary,
            "controller_generation": self.arguments.generation,
            "direct_agent_health": self._direct_agent_health(),
            "installation": {
                "architecture": "arm64",
                "identity": self._installation_identity(candidate),
            },
            "node_id_after_renewal": renewal["node_id"],
            "node_id_before_renewal": node_id,
            "pairing_grant_use_count": use_count,
            "publication_graph": self.graph,
            "renewal": renewal["proof"],
            "synthetic_device": synthetic_device,
        }

    def _prepare_podman_apparmor_profile(self) -> None:
        enabled = Path("/sys/module/apparmor/parameters/enabled")
        if not enabled.exists() or enabled.read_text(encoding="ascii").strip() != "Y":
            return
        profile = Path("/etc/apparmor.d/podman")
        try:
            metadata = profile.lstat()
        except OSError as error:
            raise LifecycleError("Podman AppArmor profile is unavailable") from error
        if (
            profile.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o644
            or metadata.st_nlink != 1
        ):
            raise LifecycleError("Podman AppArmor profile is invalid")
        self._run_command(
            ["sudo", "/usr/sbin/apparmor_parser", "--replace", os.fspath(profile)],
            cwd=Path("/"),
            timeout=30,
        )
        self._run_command(
            [
                "sudo",
                "/usr/bin/grep",
                "-Fx",
                "podman (unconfined)",
                "/sys/kernel/security/apparmor/profiles",
            ],
            cwd=Path("/"),
            timeout=30,
        )

    def _create_grant(self) -> tuple[str, str, str, str]:
        from cluster_profiles.generated_control.models.enrollment_grant_response import (
            EnrollmentGrantResponse,
        )
        from cluster_profiles.generated_control.models.fleet_enroll_request import (
            FleetEnrollRequest,
        )

        assert self.control is not None
        try:
            _, response = self.control.request(
                "POST",
                "/api/fleet/enroll",
                FleetEnrollRequest(
                    name="Acceptance Spark",
                    request_key=str(uuid.uuid4()),
                ).to_dict(),
            )
            envelope = require_object(response, "Fleet enrollment")
            grant = require_object(envelope.get("grant"), "enrollment grant")
            EnrollmentGrantResponse.from_dict(grant)
        except (SliceError, KeyError, TypeError, ValueError) as error:
            raise LifecycleError(
                "single-use enrollment grant creation failed"
            ) from error
        grant_id = grant.get("id")
        enrollment = grant.get("enrollment_endpoint")
        controller = grant.get("controller_endpoint")
        ca_sha256 = grant.get("ca_fingerprint")
        token = grant.pop("token", None)
        if (
            not isinstance(grant_id, str)
            or re.fullmatch(r"[0-9a-f-]{36}", grant_id) is None
            or not isinstance(enrollment, str)
            or enrollment != f"https://{ENROLLMENT_HOST}:8443"
            or controller != f"https://{AGENT_HOST}:8443"
            or grant.get("controller_address") != CONTROLLER_ADDRESS
            or grant.get("service_hostnames")
            != [
                self.control_hostname,
                ENROLLMENT_HOST,
                AGENT_HOST,
                REGISTRY_HOST,
            ]
            or not isinstance(ca_sha256, str)
            or SHA256.fullmatch(ca_sha256) is None
            or not isinstance(token, str)
            or not 43 <= len(token) <= 64
            or grant.get("installer_url")
            != (
                "https://install.vonkforge.ai/dev/spark"
                if self.arguments.channel == "dev"
                else "https://install.vonkforge.ai/spark"
            )
            or grant.get("purpose") != "new-node"
        ):
            raise LifecycleError("single-use enrollment grant is invalid")
        return grant_id, enrollment, ca_sha256, token

    def _psql(self, query: str) -> list[list[str]]:
        assert self.bundle is not None
        if any(character in query for character in "\0\r\n"):
            raise LifecycleError("controller observation query is invalid")
        result = self._run_command(
            self._compose(
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "control",
                "-d",
                "control",
                "-A",
                "-t",
                "-F",
                "\t",
                "-c",
                query,
            ),
            cwd=self.bundle,
            timeout=30,
        )
        return [line.split("\t") for line in result.stdout.splitlines() if line]

    def _pairing_grant_use_count(self, grant_id: str) -> int:
        if re.fullmatch(r"[0-9a-f-]{36}", grant_id) is None:
            raise LifecycleError("enrollment grant identity is invalid")
        rows = self._psql(
            "SELECT (consumed_at IS NOT NULL)::int, "
            "(SELECT count(*) FROM agent_enrollments e WHERE e.grant_id=g.id) "
            f"FROM agent_enrollment_grants g WHERE id='{grant_id}'"
        )
        if rows != [["1", "1"]]:
            raise LifecycleError("pairing grant was not used exactly once")
        return 1

    def _direct_agent_health(self) -> dict[str, str | bool]:
        self._self_test()
        return {
            "healthy": True,
            "implementation": "rust",
            "transport": "direct",
        }

    def _import_canary_catalog(
        self,
        fixture: CanonicalCanaryFixture,
        request_key: str,
        *,
        companions: Sequence[CanonicalCanaryFixture] = (),
    ) -> dict[str, object]:
        """Apply the producer fixture through the Controller's catalog sync.

        Production Controllers only read signed recipe releases, so the
        fixture's exact index and package bytes are handed to the running
        control-api container, which imports them with its own sync service.
        ``companions`` are further Recipes of the same index, whose packages
        travel with it.
        """
        assert self.bundle is not None
        result = self._run_command(
            self._compose(
                "exec",
                "-T",
                "--user",
                "10001:10001",
                "control-api",
                "python",
                "-c",
                CANARY_CATALOG_IMPORT.read_text(encoding="utf-8"),
            ),
            cwd=self.bundle,
            timeout=660,
            report_failure_output=True,
            input_text=json.dumps(
                {
                    "request_key": request_key,
                    "index": fixture.index_bytes.decode("utf-8"),
                    "packages": [
                        base64.b64encode(value.package_bytes).decode("ascii")
                        for value in (fixture, *companions)
                    ],
                }
            ),
        )
        lines = result.stdout.strip().splitlines()
        try:
            sync = json.loads(lines[-1])
        except (IndexError, json.JSONDecodeError) as error:
            raise LifecycleError(
                "synthetic canary catalog import returned no sync view"
            ) from error
        return require_object(sync, "synthetic canary catalog sync")

    def _run_synthetic_canary(
        self,
        node_id: str,
        *,
        carry: Callable[[], tuple[str, str] | None] | None = None,
    ) -> dict[str, object]:
        """Run the canary from catalog sync to uninstall.

        ``carry`` runs while the canary serves, between its first inference
        and its cleanup: the upgrade-carry lane upgrades the Controller and
        the agent there and proves the workload kept serving.
        """
        assert (
            self.control is not None
            and self.browser is not None
            and isinstance(self.synthetic_canary_fixture, CanonicalCanaryFixture)
        )
        if NODE_ID.fullmatch(node_id) is None:
            raise LifecycleError("synthetic canary node identity is invalid")
        fixture = self.synthetic_canary_fixture
        completed = ["inventory-ready"]
        response_digest: str | None = None
        try:
            sync = self._import_canary_catalog(
                fixture, self._canary_request_key(fixture, node_id, "catalog-sync")
            )
            # The sync counts every catalog document it applies: the one canary
            # Recipe plus the model documents its catalog index carries.
            total = sync.get("total_count")
            if (
                sync.get("state") != "current"
                or sync.get("commit") != fixture.source_commit
                or not isinstance(total, int)
                or isinstance(total, bool)
                or total < 1
                or sync.get("processed_count") != total
                or (sync.get("imported_count"), sync.get("unchanged_count"))
                not in {(1, 0), (0, 1)}
                or sync.get("problems") != []
            ):
                summary = {
                    key: sync.get(key)
                    for key in (
                        "state",
                        "total_count",
                        "processed_count",
                        "imported_count",
                        "updated_count",
                        "unchanged_count",
                        "problems",
                    )
                }
                raise LifecycleError(
                    "synthetic canary catalog sync is incomplete: "
                    + json.dumps(summary, sort_keys=True)[:1024]
                )
            _, listed_payload = self.control.request("GET", "/api/recipe/library")
            listed = require_object(listed_payload, "synthetic canary Library")
            recipes = listed.get("recipes")
            if not isinstance(recipes, list):
                raise LifecycleError("synthetic canary Library response is invalid")
            matches = [
                value
                for value in recipes
                if isinstance(value, dict)
                and isinstance(value.get("identity"), dict)
                and value["identity"].get("publisher") == fixture.publisher
                and value["identity"].get("slug") == fixture.slug
                and value["identity"].get("content_sha256")
                == fixture.recipe_content_sha256
            ]
            if len(matches) != 1:
                raise LifecycleError("exact synthetic canary Recipe is unavailable")
            summary = matches[0]
            identity = summary["identity"]
            recipe_id = identity.get("recipe_id")
            revision_id = identity.get("recipe_revision_id")
            recipe_selector = summary.get("selector")
            if (
                not isinstance(recipe_id, str)
                or UUID.fullmatch(recipe_id) is None
                or not isinstance(revision_id, str)
                or UUID.fullmatch(revision_id) is None
                or not isinstance(recipe_selector, str)
                or recipe_selector != f"{fixture.publisher}/{fixture.slug}"
            ):
                raise LifecycleError("synthetic canary Recipe identity is invalid")
            _, detail_payload = self.control.request(
                "GET", f"/api/recipe/{recipe_selector}"
            )
            detail = require_object(detail_payload, "synthetic canary Recipe detail")
            detail_identity = detail.get("identity")
            model_selectors = detail.get("model_selectors")
            if (
                detail_identity != identity
                or not _canonical_recipe_matches(detail.get("document"), fixture.recipe)
                or not isinstance(model_selectors, list)
                or len(model_selectors) != 1
                or not isinstance(model_selectors[0], str)
            ):
                raise LifecycleError("synthetic canary canonical closure differs")
            completed.append("recipe-resolved")
            fleet_before = self._fleet_snapshot()
            fleet_nodes = fleet_before.get("nodes")
            fleet_node_ids = (
                {
                    node.get("id")
                    for node in fleet_nodes
                    if isinstance(node, dict) and isinstance(node.get("id"), str)
                }
                if isinstance(fleet_nodes, list)
                else set()
            )
            if fleet_node_ids != {node_id}:
                raise LifecycleError(
                    "synthetic canary requires a disposable fleet with exactly one enrolled Spark"
                )
            download_payload = self._request_recipe_download(
                recipe_selector,
                request_key=self._canary_request_key(
                    fixture, node_id, "recipe-download"
                ),
            )
            download = self._await_recipe_download(
                require_object(download_payload, "synthetic canary recipe download"),
                fixture=fixture,
                recipe_revision_id=revision_id,
            )
            download_result = require_object(
                download.get("result"), "recipe download result"
            )
            if (
                download_result.get("recipe_content_sha256")
                != fixture.recipe_content_sha256
                or fixture.model_content_sha256
                not in download_result.get("model_content_digests", [])
                or re.fullmatch(
                    r"sha256:[0-9a-f]{64}", str(download_result.get("image_digest"))
                )
                is None
                or SHA256.fullmatch(str(download_result.get("oci_archive_sha256")))
                is None
                or type(download_result.get("image_bytes")) is not int
                or download_result["image_bytes"] <= 0
            ):
                raise LifecycleError(
                    "synthetic canary recipe download receipts are incomplete"
                )
            completed.append("source-verified")

            profile_payload = {
                "name": "Acceptance synthetic canary",
                "description": "Disposable whole-fleet lifecycle canary",
                "installation_policy": "keep-cached",
                "labels": {"purpose": "acceptance"},
                "favorite": False,
                "assignments": [
                    {
                        "recipe_selector": recipe_selector,
                        "spark_ids": [node_id],
                        "assignment_name": fixture.slug,
                        "desired_state": "running",
                    }
                ],
            }
            _, saved_payload = self.control.request(
                "PUT", "/api/profile/1", profile_payload
            )
            saved = require_object(saved_payload, "synthetic canary profile save")
            profile_revision = saved.get("revision")
            if type(profile_revision) is not int or profile_revision < 1:
                raise LifecycleError("synthetic canary profile revision is invalid")
            _, preview_payload = self.control.request("POST", "/api/profile/1/preview")
            preview = require_object(
                preview_payload, "synthetic canary profile preview"
            )
            if (
                preview.get("allowed") is not True
                or not isinstance(preview.get("plan_digest"), str)
                or SHA256.fullmatch(preview["plan_digest"]) is None
                or not isinstance(preview.get("scope"), dict)
                or set(preview["scope"].get("node_ids", [])) != fleet_node_ids
                or set(preview["scope"].get("idle_node_ids", [])) != set()
                or not isinstance(preview.get("preparations"), list)
                or len(preview["preparations"]) != 1
            ):
                raise LifecycleError(
                    "synthetic canary profile preview is not admitted: "
                    + self._preview_diagnostic(preview)
                )
            preparation = require_object(
                preview["preparations"][0], "synthetic canary preparation"
            ).get("preparation")
            preparation = require_object(preparation, "synthetic canary preparation")
            model_preparation = require_object(
                preparation.get("model"), "model preparation"
            )
            runtime_preparation = require_object(
                preparation.get("runtime_image"), "runtime image preparation"
            )
            if (
                model_preparation.get("model_content_sha256")
                != fixture.model_content_sha256
                or SHA256.fullmatch(str(model_preparation.get("artifact_set_sha256")))
                is None
                or type(model_preparation.get("artifact_set_bytes")) is not int
                or model_preparation["artifact_set_bytes"] <= 0
                or re.fullmatch(
                    r"sha256:[0-9a-f]{64}", str(runtime_preparation.get("image_digest"))
                )
                is None
                or SHA256.fullmatch(str(runtime_preparation.get("oci_layout_sha256")))
                is None
                or type(runtime_preparation.get("image_bytes")) is not int
                or runtime_preparation["image_bytes"] <= 0
            ):
                raise LifecycleError(
                    "synthetic canary preparation receipts are incomplete"
                )

            # The carry lane is still running the published baseline here;
            # journal repair is a candidate behavior exercised by the fresh lane.
            if carry is None:
                self._arm_start_receipt_loss()
            application_payload = self._load_canary_profile(
                preview,
                request_key=self._canary_request_key(fixture, node_id, "profile-load"),
            )
            application = self._await_profile_application(
                require_object(
                    application_payload, "synthetic canary profile application"
                ),
                label="synthetic canary profile load",
                node_id=node_id,
            )
            profile_progress = require_object(
                application.get("progress"), "profile progress"
            )
            step_results = profile_progress.get("step_results")
            if not isinstance(step_results, dict) or not step_results:
                raise LifecycleError(
                    "synthetic canary profile child receipts are missing"
                )
            run_result = self._profile_run_switch_result(step_results)
            phase_results = run_result.get("phase_results")
            if not isinstance(phase_results, list):
                raise LifecycleError("synthetic canary profile run receipt is invalid")
            installation_id = self._canary_phase_identity(
                phase_results, "installation_id"
            )
            run_id = self._canary_phase_identity(phase_results, "run_id")
            image = next(
                (
                    value
                    for value in phase_results
                    if isinstance(value, dict)
                    and re.fullmatch(
                        r"sha256:[0-9a-f]{64}", str(value.get("image_digest"))
                    )
                    and SHA256.fullmatch(str(value.get("oci_layout_sha256")))
                    and type(value.get("image_bytes")) is int
                    and value["image_bytes"] > 0
                ),
                None,
            )
            final_verify = next(
                (
                    value
                    for value in phase_results
                    if isinstance(value, dict) and value.get("phase") == "final_verify"
                ),
                None,
            )
            install_phase = next(
                (
                    value
                    for value in phase_results
                    if isinstance(value, dict)
                    and value.get("phase") == "prepare"
                    and value.get("subphase") == "runtime-install"
                ),
                None,
            )
            if (
                image is None
                or not isinstance(install_phase, dict)
                or install_phase.get("installation_id") != installation_id
                or not isinstance(final_verify, dict)
                or final_verify.get("run_id") != run_id
                or final_verify.get("healthy") is not True
                or final_verify.get("route_state") != "published"
            ):
                raise LifecycleError(
                    "synthetic canary profile execution receipts are incomplete"
                )
            completed.extend(
                ("image-built", "image-distributed", "installed", "running")
            )
            self._await_canary_endpoint(fixture.slug, published=True)
            completed.append("route-published")
            inference_key = self._read_secret("litellm-master-key")
            inference = self.browser.bearer(inference_key, timeout=30)
            del inference_key
            response_digest = self._run_canonical_inference(
                inference, fixture.serving_check, fixture.slug
            )
            completed.append("inference-ok")
            if carry is None:
                self._verify_lost_start_replay(run_id)
            if carry is not None:
                # A carry that replaced the workload reports the installation
                # and run that now serve, which the cleanup must remove.
                carried = carry()
                if carried is not None:
                    installation_id, run_id = carried
            self._exercise_corrupt_agent_journal(
                node_id=node_id,
                run_id=run_id,
                fixture=fixture,
                inference=inference,
                response_digest=response_digest,
            )
            cleanup_payload = {
                "name": "Acceptance synthetic canary",
                "description": "Disposable whole-fleet lifecycle canary cleanup",
                "installation_policy": "exact",
                "labels": {"purpose": "acceptance"},
                "favorite": False,
                "expected_revision": profile_revision,
                "assignments": [],
            }
            _, cleanup_saved_payload = self.control.request(
                "PUT", "/api/profile/1", cleanup_payload
            )
            cleanup_saved = require_object(
                cleanup_saved_payload, "synthetic canary cleanup save"
            )
            profile_revision = cleanup_saved.get("revision")
            _, cleanup_preview_payload = self.control.request(
                "POST", "/api/profile/1/preview"
            )
            cleanup_preview = require_object(
                cleanup_preview_payload, "synthetic canary cleanup preview"
            )
            try:
                _validate_canary_cleanup_preview(cleanup_preview, node_id=node_id)
            except LifecycleError as error:
                raise LifecycleError(
                    "synthetic canary cleanup preview is not admitted: "
                    + self._preview_diagnostic(cleanup_preview)
                ) from error
            cleanup_application_payload = self._load_canary_profile(
                cleanup_preview,
                request_key=self._canary_request_key(
                    fixture, node_id, "profile-cleanup"
                ),
            )
            cleanup_application = self._await_profile_application(
                require_object(
                    cleanup_application_payload, "synthetic canary cleanup application"
                ),
                label="synthetic canary profile cleanup",
                node_id=node_id,
            )
            _validate_canary_cleanup_application(
                cleanup_application,
                installation_ids=[installation_id],
                run_id=run_id,
            )
            completed.append("stopped")
            self._await_canary_endpoint(fixture.slug, published=False)
            completed.append("route-withdrawn")
            fleet = self._fleet_snapshot()
            fleet_nodes = fleet.get("nodes", [])
            if not isinstance(fleet_nodes, list):
                fleet_nodes = []
            if any(
                isinstance(node, dict)
                and any(
                    isinstance(run, dict) and run.get("alias") == fixture.slug
                    for run in node.get("loaded", [])
                    if isinstance(node.get("loaded"), list)
                )
                for node in fleet_nodes
            ):
                raise LifecycleError("synthetic canary route cleanup left a loaded run")
            if any(
                isinstance(node, dict)
                and any(
                    isinstance(installed, dict)
                    and installed.get("installation_id") == installation_id
                    for installed in node.get("installed", [])
                    if isinstance(node.get("installed"), list)
                )
                for node in fleet_nodes
            ):
                raise LifecycleError(
                    "synthetic canary cleanup left the installation present"
                )
            completed.append("uninstalled")
        except (SliceError, ServingExecutionError, LifecycleError) as error:
            # Keep the API response concise for the lifecycle client, but make
            # the bounded Controller logs available before cleanup.  This is
            # the only useful evidence for an unexpected 5xx from a fresh
            # candidate and uses the existing secret redaction path.
            raise self._installation_failure("synthetic canary", error) from error
        if (
            completed != list(SYNTHETIC_CANARY_STATES)
            or not isinstance(response_digest, str)
            or SHA256.fullmatch(response_digest) is None
        ):
            raise LifecycleError("synthetic canary evidence is incomplete")
        return {
            "completed_states": completed,
            "deterministic_response_sha256": response_digest,
        }

    def _exercise_corrupt_agent_journal(
        self,
        *,
        node_id: str,
        run_id: str,
        fixture: CanonicalCanaryFixture,
        inference: Client,
        response_digest: str,
    ) -> None:
        """Fault the disposable runner while its exact canary keeps serving.

        __enter__ has refused every pre-existing Spark installation. This is
        only the isolated ARM64 systemd acceptance host, never a fleet action.
        The running container and retained exact runtime files remain intact;
        recovery must reconnect, adopt them and complete subsequent cleanup.
        """
        assert self.agent_installed and self.temporary_root is not None
        temporary_root = self.temporary_root
        if UUID.fullmatch(run_id) is None or NODE_ID.fullmatch(node_id) is None:
            raise LifecycleError("journal recovery canary identity is invalid")
        name = f"vonk-{run_id}"

        def container_identity() -> str:
            observed = self._run_command(
                ["docker", "inspect", "--format", "{{.Id}} {{.State.Running}}", name],
                cwd=temporary_root,
                timeout=30,
            ).stdout.strip()
            if re.fullmatch(r"[0-9a-f]{64} true", observed) is None:
                raise LifecycleError("journal recovery canary container is not running")
            return observed

        def running_managed_containers() -> list[str]:
            lines = self._run_command(
                ["docker", "ps", "--no-trunc", "--format", "{{.ID}} {{.Names}}"],
                cwd=temporary_root,
                timeout=30,
            ).stdout.splitlines()
            return sorted(
                line
                for line in lines
                if re.fullmatch(
                    r"[0-9a-f]{64} vonk-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",
                    line,
                )
            )

        before_container = container_identity()
        before_containers = running_managed_containers()
        before_fleet = self._fleet_snapshot()
        before_nodes = before_fleet.get("nodes")
        if not isinstance(before_nodes, list):
            raise LifecycleError("journal recovery Fleet nodes are unavailable")
        before_node = next(
            (
                node
                for node in before_nodes
                if isinstance(node, dict) and node.get("id") == node_id
            ),
            None,
        )
        if not isinstance(before_node, dict):
            raise LifecycleError("journal recovery canary Spark is unavailable")
        before_seen = require_object(before_node["connection"], "Spark connection").get(
            "last_seen_at"
        )
        self._run_command(
            ["sudo", "/usr/bin/systemctl", "stop", "vonk-forge-agent.service"],
            cwd=temporary_root,
            timeout=30,
        )
        # The service is stopped: no live SQLite writer is being overwritten.
        # Only the derived journal is faulted; credentials and runtime evidence
        # are preserved so the recovered agent must adopt the exact effect.
        self._run_command(
            [
                "sudo",
                "/usr/bin/python3",
                "-c",
                (
                    "from pathlib import Path; import sys; "
                    "Path(sys.argv[1]).write_bytes(b'acceptance-corrupt-agent-journal')"
                ),
                os.fspath(AGENT_DATA / "state.sqlite"),
            ],
            cwd=temporary_root,
            timeout=30,
        )
        self._run_command(
            ["sudo", "/usr/bin/systemctl", "start", "vonk-forge-agent.service"],
            cwd=temporary_root,
            timeout=30,
        )
        deadline = time.monotonic() + _CANARY_ROUTE_SECONDS
        while True:
            snapshot = self._fleet_snapshot()
            nodes = snapshot.get("nodes")
            node = (
                next(
                    (
                        node
                        for node in nodes
                        if isinstance(node, dict) and node.get("id") == node_id
                    ),
                    None,
                )
                if isinstance(nodes, list)
                else None
            )
            connection = None if not isinstance(node, dict) else node.get("connection")
            if (
                isinstance(connection, dict)
                and connection.get("online_state") == "online"
                and connection.get("last_seen_at") is not None
                and connection["last_seen_at"] != before_seen
            ):
                break
            if time.monotonic() >= deadline:
                raise LifecycleError(
                    "agent did not reconnect after corrupt journal recovery"
                )
            time.sleep(1)
        if (
            container_identity() != before_container
            or running_managed_containers() != before_containers
        ):
            raise LifecycleError(
                "journal recovery replaced or duplicated a running container"
            )
        self._await_canary_endpoint(fixture.slug, published=True)
        if (
            self._run_canonical_inference(
                inference, fixture.serving_check, fixture.slug
            )
            != response_digest
        ):
            raise LifecycleError("canary response changed after journal recovery")
        evidence = self._run_command(
            [
                "sudo",
                "/usr/bin/python3",
                "-c",
                (
                    "from pathlib import Path; import sys; "
                    "print(any(p.name.startswith('state.sqlite.corrupt-') "
                    "for p in Path(sys.argv[1]).iterdir()))"
                ),
                os.fspath(AGENT_DATA),
            ],
            cwd=temporary_root,
            timeout=30,
        ).stdout.strip()
        if evidence != "True":
            raise LifecycleError("agent did not retain corrupt journal evidence")

    def _load_canary_profile(
        self, preview: dict[str, object], *, request_key: str
    ) -> object:
        from vonk_control.fleet_profile_contract import FleetProfileLoadRequest

        assert self.control is not None
        request = FleetProfileLoadRequest.model_validate_json(
            _canonical({"request_key": request_key})
        )
        _, payload = self.control.request(
            "POST",
            "/api/profile/1/load",
            # The lane loads on the previous release's Controller too, which
            # refuses a field its contract does not know: send only what is set.
            request.model_dump(mode="json", exclude_none=True),
            allowed=(202,),
        )
        return payload

    @staticmethod
    def _canary_request_key(
        fixture: CanonicalCanaryFixture, node_id: str, stage: str
    ) -> str:
        return str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "vonk:spark-lifecycle:canonical-canary:"
                f"{fixture.source_commit}:{fixture.recipe_content_sha256}:"
                f"{node_id}:{stage}",
            )
        )

    def _preflight_failure_evidence(self, operation_id: str) -> list[object]:
        # Project only diagnostic fields from the current checkpoint. Never
        # serialize complete operation results, signed grants, or credentials.
        query = (
            "SELECT json_build_object('node_id',r.key,"
            "'current_fingerprint',n.preflight_fingerprint,"
            "'receipt_fingerprint',r.value->>'fingerprint',"
            "'payload_sha256',a.payload_digest,"
            "'observed_at',r.value->'observed_at',"
            "'controller_now',floor(extract(epoch FROM clock_timestamp())),"
            "'failed_findings',(SELECT jsonb_agg(jsonb_build_object("
            "'capability',f->>'capability','code',f->>'code')) FROM "
            "jsonb_array_elements(r.value->'findings') f WHERE f->>'status'!='passed')) "
            "FROM jobs j CROSS JOIN LATERAL "
            "jsonb_each(j.result::jsonb->'preflight'->'receipts') r "
            "LEFT JOIN agent_nodes n ON n.node_id=r.key "
            "LEFT JOIN LATERAL (SELECT o.payload_digest FROM agent_operations o "
            "JOIN agent_operation_attempts t ON t.operation_id=o.id "
            "AND t.attempt=o.current_attempt WHERE o.node_id=r.key "
            "AND o.kind='runtime.preflight.v1' "
            "AND t.result::jsonb->>'observed_at'=r.value->>'observed_at' "
            "ORDER BY o.updated_at DESC LIMIT 1) a ON true "
            f"WHERE j.id='{operation_id}' ORDER BY r.key LIMIT 2"
        )
        pending_query = (
            "SELECT json_build_object('node_id',j.result::jsonb->'preflight'->>'pending_node_id',"
            "'pending_job_id',child.id,'child_state',child.state,"
            "'attempt_state',t.state,'lease_deadline',t.lease_deadline,"
            "'progress_phase',t.progress::jsonb->>'phase') FROM jobs j "
            "JOIN jobs child ON child.id=j.result::jsonb->'preflight'->>'pending_job_id' "
            "LEFT JOIN agent_operations o ON o.parent_job_id=child.id "
            "LEFT JOIN agent_operation_attempts t ON t.operation_id=o.id "
            "AND t.attempt=o.current_attempt "
            f"WHERE j.id='{operation_id}' AND child.state!='succeeded' LIMIT 1"
        )
        try:
            rows = self._psql(query) + self._psql(pending_query)
            return [json.loads(row[0]) for row in rows if len(row) == 1]
        except (AcceptanceError, OSError, ValueError, subprocess.SubprocessError):
            return [{"diagnostic": "preflight evidence unavailable"}]

    def _request_recipe_download(
        self, selector: str, *, request_key: str
    ) -> dict[str, object]:
        from cluster_profiles.generated_control.models.recipe_download_request import (
            RecipeDownloadRequest,
        )

        assert self.control is not None
        _, payload = self.control.request(
            "POST",
            f"/api/recipe/{selector}/download",
            RecipeDownloadRequest(request_key=request_key).to_dict(),
            allowed=(200, 201, 202),
        )
        return require_object(payload, "synthetic canary recipe download")

    def _await_recipe_download(
        self,
        operation: dict[str, object],
        *,
        fixture: CanonicalCanaryFixture,
        recipe_revision_id: str,
    ) -> dict[str, object]:
        """Follow the current Recipe download contract to terminal evidence."""
        assert self.control is not None
        control_src = REPOSITORY_ROOT / "control/src"
        if os.fspath(control_src) not in sys.path:
            sys.path.insert(0, os.fspath(control_src))
        from vonk_control.recipe_image_availability_api import (
            RecipeImageAvailabilityResponse,
        )

        try:
            typed = RecipeImageAvailabilityResponse.model_validate_json(
                _canonical(operation)
            )
        except (TypeError, ValueError) as error:
            raise LifecycleError(
                "synthetic canary recipe download response is invalid"
            ) from error
        if (
            typed.recipe_revision_id != recipe_revision_id
            or typed.recipe_content_sha256 != fixture.recipe_content_sha256
        ):
            raise LifecycleError("synthetic canary recipe download identity differs")
        deadline = time.monotonic() + _CANARY_CONVERGENCE_SECONDS
        while typed.state in _LIVE_CACHE_STATES:
            if time.monotonic() >= deadline:
                evidence = {
                    "state": typed.state,
                    "attempt": typed.attempt,
                    "progress": typed.progress.model_dump(
                        mode="json", exclude_none=True
                    ),
                    "failure": (
                        None
                        if typed.failure is None
                        else typed.failure.model_dump(mode="json", exclude_none=True)
                    ),
                    "children": [
                        child.model_dump(mode="json", exclude_none=True)
                        for child in typed.children
                    ],
                    "actions": list(typed.actions),
                    "updated_at": typed.updated_at,
                }
                raise LifecycleError(
                    "synthetic canary recipe download did not converge: "
                    + self._redact_diagnostics(
                        json.dumps(evidence, sort_keys=True, separators=(",", ":")),
                        limit=4_000,
                    )
                )
            time.sleep(1)
            _, payload = self.control.request(
                "GET", f"/api/recipe/operations/{typed.id}"
            )
            try:
                typed = RecipeImageAvailabilityResponse.model_validate_json(
                    _canonical(
                        require_object(payload, "synthetic canary recipe download")
                    )
                )
            except (TypeError, ValueError) as error:
                raise LifecycleError(
                    "synthetic canary recipe download response is invalid"
                ) from error
        if typed.state != "succeeded" or typed.result is None:
            failure = typed.failure.model_dump(mode="json") if typed.failure else None
            raise LifecycleError(
                "synthetic canary recipe download failed: "
                + self._redact_diagnostics(json.dumps(failure))
            )
        return typed.model_dump(mode="json")

    def _await_profile_application(
        self, operation: dict[str, object], *, label: str, node_id: str
    ) -> dict[str, object]:
        """Consume the submitted profile application until it is terminal."""
        assert self.control is not None
        control_src = REPOSITORY_ROOT / "control/src"
        if os.fspath(control_src) not in sys.path:
            sys.path.insert(0, os.fspath(control_src))
        from vonk_control.fleet_profile_contract import FleetProfileApplicationView

        try:
            typed = FleetProfileApplicationView.model_validate_json(
                _canonical(operation)
            )
        except (TypeError, ValueError) as error:
            raise LifecycleError(
                f"{label} response is invalid: {str(error)[:400]}"
            ) from error
        application_id = typed.id
        deadline = time.monotonic() + _CANARY_CONVERGENCE_SECONDS
        # Admission may be durably parked while an active workload owner
        # finishes.  This is a recoverable state: the Controller owns the
        # retry schedule and the same application identity must be observed
        # until it reaches a terminal outcome.
        while typed.state in _LIVE_APPLICATION_STATES:
            self._recover_lost_start_receipt(node_id)
            if time.monotonic() >= deadline:
                # Say where it stalled: a queued application with no step
                # means nothing claimed it, while a running one names the step
                # and child phase it never left.
                progress = typed.progress
                child = progress.child_progress
                child_detail = "none"
                child_bytes = "none"
                if child is not None:
                    inner = child.operation
                    child_detail = (
                        f"{child.phase}/{inner.phase}"
                        if inner is not None
                        else child.phase
                    )
                    child_bytes = f"{child.bytes}/{child.total_bytes}"
                # The Controller's own view separates a job that was never
                # handed to the Spark from a transfer that is running there: a
                # queued job is a dispatch stall, a running one is not.
                # Ask the Controller about the stalled child. Each call is
                # reported separately with its own failure text: a probe that
                # discards the status tells us nothing, and one failing call
                # must not hide the answer from the other.
                job_detail = "unavailable"
                if self.control is not None:
                    parts: list[str] = []
                    try:
                        _, node_operations = self.control.request(
                            "GET",
                            "/api/operations",
                            query={"node_id": node_id, "limit": 5},
                        )
                        operations = require_object(
                            node_operations, "stalled operation list"
                        ).get("operations")
                        if isinstance(operations, list):
                            for item in operations:
                                operation = require_object(item, "stalled operation")
                                parts.append(
                                    f"op {operation.get('kind')}"
                                    f"={operation.get('state')}"
                                    f"/{operation.get('attempt')}"
                                    f" progress={json.dumps(operation.get('progress'), sort_keys=True)[:160]}"
                                )
                    except (
                        KeyError,
                        OSError,
                        SliceError,
                        TypeError,
                        ValueError,
                    ) as error:
                        parts.append(
                            "operations unavailable: "
                            + self._redact_diagnostics(str(error), limit=200)
                        )
                    job_detail = " | ".join(parts) or "none"
                raise LifecycleError(
                    f"{label} did not converge: state={typed.state} "
                    f"step={typed.current_step}/{typed.total_steps} "
                    f"completed={progress.completed_steps}/{progress.total_steps} "
                    f"label={progress.current_label or 'none'} "
                    f"operation={typed.current_operation_id or 'none'} "
                    f"child={child_detail} child_bytes={child_bytes} "
                    f"agent_job={job_detail} "
                    f"reason={typed.status_reason or 'none'}"
                )
            time.sleep(1)
            _, payload = self.control.request(
                "GET", f"/api/profile/applications/{application_id}"
            )
            try:
                observed = FleetProfileApplicationView.model_validate_json(
                    _canonical(require_object(payload, label))
                )
            except (TypeError, ValueError) as error:
                raise LifecycleError(f"{label} response is invalid") from error
            if observed.id != application_id:
                raise LifecycleError(
                    f"{label} response identifies a different application"
                )
            typed = observed
        if typed.state != "succeeded":
            preflight = (
                self._preflight_failure_evidence(typed.id)
                if isinstance(typed.status_reason, str)
                and typed.status_reason.startswith("runtime_preflight.")
                else None
            )
            details = self._redact_diagnostics(
                json.dumps(
                    {
                        "id": typed.id,
                        "state": typed.state,
                        "status_reason": typed.status_reason,
                        "current_step": typed.current_step,
                        "total_steps": typed.total_steps,
                        "current_label": typed.progress.current_label,
                        "preflight": preflight,
                    }
                )
            )
            raise LifecycleError(f"{label} failed: {details}")
        return typed.model_dump(mode="json")

    @staticmethod
    def _preview_diagnostic(preview: dict[str, object]) -> str:
        """Describe a rejected profile preview instead of collapsing its cause.

        The preview contract carries the decisive evidence separately: entry
        admission, per-assignment preparation receipts, the plan summary and
        the structured reasons.  Reporting them together keeps an acceptance
        failure diagnosable without a second run.
        """

        reasons = preview.get("reasons")
        rendered_reasons: object = reasons
        if isinstance(reasons, list):
            rendered_reasons = [
                {
                    "code": reason.get("code"),
                    "severity": reason.get("severity"),
                    "detail": reason.get("detail"),
                }
                for reason in reasons
                if isinstance(reason, dict)
            ]
        preparations = preview.get("preparations")
        rendered_preparations: object = preparations
        if isinstance(preparations, list):
            rendered_preparations = [
                item.get("assignment_id")
                for item in preparations
                if isinstance(item, dict)
            ]
        return json.dumps(
            {
                "allowed": preview.get("allowed"),
                "preparations": rendered_preparations,
                "reasons": rendered_reasons,
                "scope": preview.get("scope"),
                "summary": preview.get("summary"),
            },
            default=str,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _profile_run_switch_result(
        step_results: dict[str, object],
    ) -> dict[str, object]:
        for value in step_results.values():
            if not isinstance(value, dict):
                continue
            result = value.get("result")
            if isinstance(result, dict) and isinstance(result.get("run_switch"), dict):
                return result["run_switch"]
        raise LifecycleError("synthetic canary profile run child receipt is missing")

    def _fleet_snapshot(self) -> dict[str, object]:
        assert self.control is not None
        control_src = REPOSITORY_ROOT / "control/src"
        if os.fspath(control_src) not in sys.path:
            sys.path.insert(0, os.fspath(control_src))
        from vonk_control.fleet_projection import FleetSnapshot

        _, payload = self.control.request("GET", "/api/fleet")
        try:
            return FleetSnapshot.model_validate_json(
                json.dumps(
                    require_object(payload, "Fleet snapshot"), separators=(",", ":")
                )
            ).model_dump(mode="json")
        except (TypeError, ValueError) as error:
            raise LifecycleError("Fleet snapshot response is invalid") from error

    @staticmethod
    def _canary_phase_identity(results: list[object], field: str) -> str:
        identities = {
            value[field]
            for value in results
            if isinstance(value, dict)
            and isinstance(value.get(field), str)
            and UUID.fullmatch(value[field]) is not None
        }
        if len(identities) != 1:
            raise LifecycleError(f"synthetic canary {field} evidence is invalid")
        return identities.pop()

    @staticmethod
    def _serving_identity(results: list[object]) -> tuple[str, str]:
        """The installation and run that now serve, from a load that replaced one.

        A load over a running workload also stops the old run, so its receipts
        name two runs; the serving one is the run its final verification
        checked, installed by the runtime-install phase.
        """

        def named(field: str, **where: str) -> str:
            values = {
                value[field]
                for value in results
                if isinstance(value, dict)
                and all(value.get(key) == wanted for key, wanted in where.items())
                and isinstance(value.get(field), str)
                and UUID.fullmatch(value[field]) is not None
            }
            if len(values) != 1:
                raise LifecycleError(f"synthetic canary {field} evidence is invalid")
            return values.pop()

        return (
            named("installation_id", phase="prepare", subphase="runtime-install"),
            named("run_id", phase="final_verify"),
        )

    def _await_canary_endpoint(self, alias: str, *, published: bool) -> None:
        deadline = time.monotonic() + _CANARY_ROUTE_SECONDS
        while True:
            try:
                fleet = self._fleet_snapshot()
            except (SliceError, LifecycleError):
                fleet = None
            runs: list[dict[str, object]] = []
            if isinstance(fleet, dict):
                fleet_nodes = fleet.get("nodes")
                if isinstance(fleet_nodes, list):
                    for node in fleet_nodes:
                        if not isinstance(node, dict) or not isinstance(
                            node.get("loaded"), list
                        ):
                            continue
                        runs.extend(
                            run
                            for run in node["loaded"]
                            if isinstance(run, dict) and run.get("alias") == alias
                        )
            if published and any(
                run.get("route_state") == "published" and run.get("healthy") is True
                for run in runs
            ):
                return
            if not published and not any(
                run.get("route_state") in {"published", "pending"} for run in runs
            ):
                return
            if time.monotonic() >= deadline:
                raise LifecycleError("synthetic canary route state did not converge")
            time.sleep(1)

    @staticmethod
    def _serving_request(
        check: dict[str, object], alias: str
    ) -> tuple[str, dict[str, object]]:
        """The canary's serving request, addressed to ``alias``."""
        request = require_object(check.get("request"), "synthetic serving request")

        def substitute(value: object) -> object:
            if isinstance(value, str) and value in {"$ALIAS", "$MODEL"}:
                return alias
            if isinstance(value, dict):
                return {str(key): substitute(item) for key, item in value.items()}
            if isinstance(value, list):
                return [substitute(item) for item in value]
            return value

        body = substitute(request.get("body"))
        if not isinstance(body, dict):
            raise LifecycleError("synthetic serving request body is invalid")
        return str(request["path"]), body

    @staticmethod
    def _run_canonical_inference(
        inference: Client, check: dict[str, object], alias: str
    ) -> str:
        path, body = SparkLifecycle._serving_request(check, alias)
        responses: list[dict[str, object]] = []
        for _attempt in range(2):
            status, payload = inference.request("POST", path, body)
            response = require_object(payload, "synthetic serving response")
            evaluate_http_response(
                HttpObservation(status=status, headers={}, body=_canonical(response)),
                check,
            )
            responses.append(response)
        first_response, second_response = responses
        if first_response != second_response:
            raise LifecycleError("synthetic canary response is not deterministic")
        return hashlib.sha256(_canonical(first_response)).hexdigest()

    @staticmethod
    def _serial_proof(serial: str) -> str:
        if SERIAL.fullmatch(serial) is None:
            raise LifecycleError("certificate serial is invalid")
        encoded = format(int(serial), "x")
        if not 16 <= len(encoded) <= 64:
            raise LifecycleError("certificate serial proof is invalid")
        return encoded

    def _old_certificate_rejected(self, serial_before: str, serial_after: str) -> bool:
        if (
            SERIAL.fullmatch(serial_before) is None
            or SERIAL.fullmatch(serial_after) is None
        ):
            raise LifecycleError("certificate probe identity is invalid")
        rows = self._psql(
            "SELECT generation FROM agent_certificates "
            f"WHERE serial='{serial_after}' AND state='active'"
        )
        if len(rows) != 1 or len(rows[0]) != 1 or not rows[0][0].isdigit():
            raise LifecycleError("active certificate probe generation is invalid")
        generation = int(rows[0][0])
        if not 1 < generation <= 2**64 - 1:
            raise LifecycleError("active certificate probe generation is invalid")
        root = AGENT_DATA / "credentials"
        current = self._certificate_probe(
            serial_after, root / f"generation-{generation:020}", retired=False
        )
        # This authenticated endpoint returns 404 only after accepting the current
        # identity. A missing all-zero source bundle is the deliberate control.
        if current.returncode != 0 or current.stdout != "404":
            raise LifecycleError(
                "current certificate did not pass the authenticated probe"
            )
        retired = self._certificate_probe(serial_before, root, retired=True)
        if retired.returncode == 0:
            return retired.stdout == "401"
        # A complete cold build may outlast the original short-lived certificate.
        # Accept only the peer's explicit expiry alert, never generic curl 56,
        # local CA/hostname/key failures, timeouts or other connection errors.
        return (
            retired.returncode == 56
            and retired.stdout == "000"
            and re.search(
                r"SSL_read:.*SSL routines::sslv3 alert certificate expired(?:,|\s)",
                retired.stderr,
            )
            is not None
        )

    def _certificate_probe(
        self, serial: str, credential_root: Path, *, retired: bool
    ) -> subprocess.CompletedProcess[str]:
        assert self.temporary_root is not None
        if SERIAL.fullmatch(serial) is None:
            raise LifecycleError("retired agent certificate serial is invalid")
        probe = self.temporary_root / (
            "retired-agent-probe" if retired else "active-agent-probe"
        )
        probe.mkdir(mode=0o700)
        leaf = probe / "certificate.pem"
        chain = probe / "chain.pem"
        key_v2 = probe / "private-key-v2.pem"
        identity = probe / "identity.json"
        bundle = probe / "bundle.pem"
        key_v1 = probe / "private-key.pem"
        try:
            for source, target in (
                (credential_root / "certificate.pem", leaf),
                (credential_root / "chain.pem", chain),
                (credential_root / "private-key.pem", key_v2),
                (credential_root / "identity.json", identity),
            ):
                self._run_command(
                    [
                        "sudo",
                        "/usr/bin/install",
                        "-o",
                        str(os.getuid()),
                        "-m",
                        "0600",
                        os.fspath(source),
                        os.fspath(target),
                    ],
                    cwd=self.temporary_root,
                    timeout=30,
                )
            try:
                metadata = json.loads(identity.read_bytes())
            except (OSError, json.JSONDecodeError) as error:
                raise LifecycleError("retired agent identity is invalid") from error
            if not isinstance(metadata, dict) or metadata.get("serial") != serial:
                raise LifecycleError("retired agent identity serial changed")
            bundle.write_bytes(leaf.read_bytes() + chain.read_bytes())
            key_v1.write_bytes(
                _openssl_compatible_ed25519_private_key(key_v2.read_bytes())
            )
            os.chmod(bundle, 0o600)
            os.chmod(key_v1, 0o600)
            result = self._run_command(
                [
                    "/usr/bin/curl",
                    "--config",
                    "/dev/null",
                    "--silent",
                    "--show-error",
                    "--output",
                    "/dev/null",
                    "--write-out",
                    "%{http_code}",
                    "--max-time",
                    "30",
                    "--cacert",
                    "/etc/vonk-forge-agent/controller-ca.pem",
                    "--cert",
                    os.fspath(bundle),
                    "--key",
                    os.fspath(key_v1),
                    f"https://{AGENT_HOST}:8443/agent/source-bundles/{'0' * 64}",
                ],
                cwd=Path("/"),
                timeout=40,
                report_failure_output=True,
                allowed_returncodes=(0, 56) if retired else (0,),
            )
            return result
        finally:
            shutil.rmtree(probe)

    def _observe_renewal(self, node_id: str, serial_before: str) -> dict[str, object]:
        if (
            NODE_ID.fullmatch(node_id) is None
            or SERIAL.fullmatch(serial_before) is None
        ):
            raise LifecycleError("renewal identity is invalid")
        deadline = time.monotonic() + CERTIFICATE_LIFETIME_SECONDS + 60
        while time.monotonic() < deadline:
            rows = self._psql(
                "SELECT n.contact_certificate_serial,c.state,"
                "(c.revoked_at IS NOT NULL)::int "
                "FROM agent_nodes n JOIN agent_certificates c "
                f"ON c.serial='{serial_before}' WHERE n.node_id='{node_id}'"
            )
            if (
                len(rows) == 1
                and len(rows[0]) == 3
                and rows[0][0] != serial_before
                and SERIAL.fullmatch(rows[0][0]) is not None
                and rows[0][1:] == ["revoked", "1"]
            ):
                serial_after = rows[0][0]
                identity = self._wait_for_agent_identity(
                    package_version=str(self.graph["candidate_version"]), timeout=30
                )
                if (
                    identity.get("node_id") != node_id
                    or identity.get("serial") != serial_after
                ):
                    raise LifecycleError("renewed agent identity is inconsistent")
                if not self._old_certificate_rejected(serial_before, serial_after):
                    raise LifecycleError("retired agent certificate was not rejected")
                before_proof = self._serial_proof(serial_before)
                return {
                    "node_id": node_id,
                    "proof": {
                        "certificate_serial_after": self._serial_proof(serial_after),
                        "certificate_serial_before": before_proof,
                        "old_certificate_rejection": {
                            "durably_recorded": True,
                            "rejected": True,
                            "serial": before_proof,
                        },
                    },
                }
            time.sleep(2)
        raise LifecycleError("agent certificate renewal did not converge")

    def _hash_path(self, path: Path) -> str:
        allowed = {
            SPARK_CONFIG,
            AGENT_BINARY,
            AGENT_DATA / "machine-evidence",
        }
        if path not in allowed:
            raise LifecycleError("installation identity path is invalid")
        result = self._run_command(
            ["sudo", "/usr/bin/sha256sum", "--", os.fspath(path)],
            cwd=Path("/"),
            timeout=30,
        )
        digest, separator, observed_path = result.stdout.rstrip("\n").partition("  ")
        if (
            separator != "  "
            or observed_path != os.fspath(path)
            or SHA256.fullmatch(digest) is None
        ):
            raise LifecycleError("installation identity digest is invalid")
        return digest

    def _self_test(self) -> dict[str, str | bool]:
        result = self._run_command(
            [
                "sudo",
                os.fspath(AGENT_BINARY),
                "--config",
                os.fspath(SPARK_CONFIG),
                "self-test",
            ],
            cwd=Path("/"),
            timeout=30,
        )
        try:
            document = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise LifecycleError("direct Rust agent self-test is invalid") from error
        identity_fields = {
            "architecture",
            "binary_digest",
            "build_digest",
            "semantic_version",
        }
        if (
            not isinstance(document, dict)
            or not identity_fields <= set(document)
            or not isinstance(document.get("build_digest"), str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", document["build_digest"]) is None
            or not isinstance(document.get("binary_digest"), str)
            or SHA256.fullmatch(document["binary_digest"]) is None
        ):
            raise LifecycleError("direct Rust agent self-test is invalid")
        binary = self._hash_path(AGENT_BINARY)
        if binary != document["binary_digest"]:
            raise LifecycleError("direct Rust agent binary identity changed")
        return document

    def _installed_package_version(self) -> str:
        result = self._run_command(
            [
                "/usr/bin/dpkg-query",
                "-W",
                "-f=${Version}",
                "vonk-forge-agent",
            ],
            cwd=Path("/"),
            timeout=30,
        )
        return result.stdout.strip()

    def _wait_for_agent_identity(
        self, *, package_version: str, timeout: int
    ) -> dict[str, object]:
        assert self.control is not None
        expected_semantic = _semantic_version(package_version)
        expected_architecture = self.arguments.platform
        deadline = time.monotonic() + timeout
        last_error: BaseException | None = None
        while time.monotonic() < deadline:
            try:
                self_test = self._self_test()
                installed_package_version = self._installed_package_version()
                package_mismatches = []
                if installed_package_version != package_version:
                    package_mismatches.append("package_version")
                if self_test.get("semantic_version") != expected_semantic:
                    package_mismatches.append("semantic_version")
                if self_test.get("architecture") != expected_architecture:
                    package_mismatches.append("architecture")
                if package_mismatches:
                    raise LifecycleError(
                        "installed package identity is unexpected: "
                        + ",".join(package_mismatches)
                    )
                _, response = self.control.request("GET", "/api/fleet")
                nodes = require_object(response, "Fleet snapshot").get("nodes")
                if not isinstance(nodes, list):
                    nodes = []
                matching = [
                    node
                    for node in nodes
                    if isinstance(node, dict)
                    and isinstance(node.get("connection"), dict)
                    and node["connection"].get("agent_state") == "active"
                    and node["connection"].get("online_state") == "online"
                ]
                if len(matching) != 1:
                    raise LifecycleError(
                        "controller has not observed the direct agent: "
                        f"active_online_fleet_nodes={len(matching)}"
                    )
                agent = matching[0]
                node_id = agent.get("id")
                agent_mismatches = []
                if not isinstance(node_id, str) or NODE_ID.fullmatch(node_id) is None:
                    agent_mismatches.append("node_id")
                if (
                    not isinstance(agent.get("display_name"), str)
                    or not agent["display_name"]
                ):
                    agent_mismatches.append("display_name")
                inventory = agent.get("inventory")
                if not isinstance(inventory, dict) or inventory.get(
                    "freshness"
                ) not in {"fresh", "stale"}:
                    agent_mismatches.append("inventory")
                elif "recipe.build.v1" not in inventory.get("capabilities", []):
                    agent_mismatches.append("recipe_builder_capability")
                if agent_mismatches:
                    raise LifecycleError(
                        "controller direct-agent identity is invalid: "
                        + ",".join(agent_mismatches)
                    )
                rows = self._psql(
                    "SELECT architecture,semantic_version,build_digest,binary_digest,"
                    "contact_certificate_serial "
                    f"FROM agent_nodes WHERE node_id='{node_id}'"
                )
                expected_row = [
                    expected_architecture,
                    expected_semantic,
                    str(self_test["build_digest"]),
                    str(self_test["binary_digest"]),
                ]
                row_mismatches = []
                if len(rows) != 1:
                    row_mismatches.append("row_count")
                elif rows[0][:4] != expected_row:
                    row_mismatches.extend(
                        field
                        for index, field in enumerate(
                            (
                                "architecture",
                                "semantic_version",
                                "build_digest",
                                "binary_digest",
                            )
                        )
                        if len(rows[0]) <= index
                        or rows[0][index] != expected_row[index]
                    )
                if len(rows) == 1 and len(rows[0]) != 5:
                    row_mismatches.append("column_count")
                elif len(rows) == 1 and SERIAL.fullmatch(rows[0][4]) is None:
                    row_mismatches.append("contact_certificate_serial")
                if row_mismatches:
                    raise LifecycleError(
                        "controller runtime identity is incomplete: "
                        + ",".join(dict.fromkeys(row_mismatches))
                    )
                return {
                    "binary_sha256": self_test["binary_digest"],
                    "build_sha256": str(self_test["build_digest"]).removeprefix(
                        "sha256:"
                    ),
                    "node_id": node_id,
                    "package_sha256": (
                        self.graph.get("baseline_package_sha256")
                        if package_version == self.graph.get("baseline_version")
                        else self.graph.get("candidate_package_sha256")
                    ),
                    "serial": rows[0][4],
                    "version": package_version,
                }
            except (LifecycleError, SliceError, TypeError) as error:
                last_error = error
                time.sleep(2)
        reason = str(last_error) if last_error is not None else "no observation"
        raise LifecycleError(
            f"direct Rust agent identity did not converge: {reason}"
        ) from last_error

    @staticmethod
    def _installation_identity(identity: dict[str, object]) -> dict[str, object]:
        return {
            field: identity[field]
            for field in ("binary_sha256", "build_sha256", "package_sha256", "version")
        }


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise LifecycleError(f"{label} is invalid")
    return value


def _read_document(path: Path, label: str) -> dict[str, object]:
    try:
        metadata = path.lstat()
        raw = path.read_bytes()
        document = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LifecycleError(f"{label} is unavailable or invalid") from error
    if path.is_symlink() or not path.is_file() or metadata.st_nlink != 1:
        raise LifecycleError(f"{label} is unsafe")
    return _object(document, label)


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _read_canonical_document(path: Path, label: str) -> dict[str, object]:
    document = _read_document(path, label)
    if path.read_bytes() != _canonical(document):
        raise LifecycleError(f"{label} is not canonical JSON")
    return document


def _atomic_write(path: Path, value: object) -> None:
    parent = path.parent
    try:
        metadata = parent.lstat()
    except OSError as error:
        raise LifecycleError("report directory is unavailable") from error
    if parent.is_symlink() or not parent.is_dir() or metadata.st_nlink < 1:
        raise LifecycleError("report directory is unsafe")
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(_canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def check_publication_graph(arguments: argparse.Namespace) -> dict[str, object]:
    if (
        CHANNEL.fullmatch(arguments.channel) is None
        or VERSION.fullmatch(arguments.version) is None
        or SOURCE_SHA.fullmatch(arguments.source_sha) is None
        or SHA256.fullmatch(arguments.generation) is None
        or arguments.platform not in PLATFORMS
    ):
        raise LifecycleError("publication graph inputs are invalid")
    try:
        graphs = recompute_publication_graphs(
            candidate_release=arguments.candidate_release,
            baseline_release=arguments.baseline_release,
            object_root=arguments.object_root,
            channel=arguments.channel,
            version=arguments.version,
            source_sha=arguments.source_sha,
            generation=arguments.generation,
        )
    except ContractError as error:
        raise LifecycleError(str(error)) from error
    return graphs[arguments.platform]


def emit_report(arguments: argparse.Namespace) -> None:
    if (
        CHANNEL.fullmatch(arguments.channel) is None
        or VERSION.fullmatch(arguments.version) is None
        or SOURCE_SHA.fullmatch(arguments.source_sha) is None
        or SHA256.fullmatch(arguments.generation) is None
        or arguments.run_id <= 0
        or arguments.platform not in PLATFORMS
    ):
        raise LifecycleError("report identity is invalid")
    evidence = _read_canonical_document(arguments.evidence, "lifecycle evidence")
    if not {
        "channel",
        "completed_phases",
        "generation",
        "platform",
        "proof",
        "run_id",
        "schema_version",
        "source_sha",
        "version",
    } <= set(evidence) or any(
        evidence.get(name) != expected
        for name, expected in {
            "schema_version": 1,
            "channel": arguments.channel,
            "version": arguments.version,
            "source_sha": arguments.source_sha,
            "generation": arguments.generation,
            "run_id": arguments.run_id,
            "platform": arguments.platform,
            "completed_phases": PHASES[arguments.platform],
        }.items()
    ):
        raise LifecycleError(
            "lifecycle evidence is incomplete or belongs to another run"
        )
    lifecycle = {
        "completed_phases": evidence["completed_phases"],
        "proof": evidence["proof"],
    }
    try:
        validate_lifecycle(
            lifecycle,
            platform=arguments.platform,
            channel=arguments.channel,
            version=arguments.version,
            source_sha=arguments.source_sha,
            generation=arguments.generation,
        )
    except ContractError as error:
        raise LifecycleError(str(error)) from error
    _atomic_write(
        arguments.output,
        _report_document(arguments, lifecycle),
    )


def _report_document(
    arguments: argparse.Namespace, lifecycle: dict[str, object]
) -> dict[str, object]:
    return {
        "channel": arguments.channel,
        "gates": GATES[arguments.platform],
        "generation": arguments.generation,
        "lifecycle": lifecycle,
        "platform": arguments.platform,
        "run_id": arguments.run_id,
        "schema_version": 2,
        "source_sha": arguments.source_sha,
        "status": "passed",
        "version": arguments.version,
    }


def _valid_run_identity(arguments: argparse.Namespace) -> bool:
    return (
        CHANNEL.fullmatch(arguments.channel) is not None
        and VERSION.fullmatch(arguments.version) is not None
        and SOURCE_SHA.fullmatch(arguments.source_sha) is not None
        and SHA256.fullmatch(arguments.generation) is not None
        and arguments.run_id > 0
        and arguments.platform in PLATFORMS
    )


def run_lifecycle(
    arguments: argparse.Namespace,
    *,
    lifecycle_factory: Callable[
        [argparse.Namespace, dict[str, object]], ObservedLifecycle
    ]
    | None = None,
) -> None:
    """Observe the real lifecycle and own validation, cleanup, and report output."""
    if not _valid_run_identity(arguments):
        raise LifecycleError("lifecycle run identity is invalid")
    graph = check_publication_graph(arguments)
    factory = lifecycle_factory
    if factory is None:
        factory = SparkLifecycle
    with factory(arguments, graph) as lifecycle_run:
        proof = lifecycle_run.observe()
    lifecycle = {
        "completed_phases": PHASES[arguments.platform],
        "proof": proof,
    }
    try:
        validate_lifecycle(
            lifecycle,
            platform=arguments.platform,
            channel=arguments.channel,
            version=arguments.version,
            source_sha=arguments.source_sha,
            generation=arguments.generation,
            expected_publication_graph=graph,
        )
    except ContractError as error:
        raise LifecycleError(str(error)) from error
    _atomic_write(arguments.output, _report_document(arguments, lifecycle))


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    graph = commands.add_parser("check-publication-graph")
    graph.add_argument("--candidate-release", type=Path, required=True)
    graph.add_argument("--baseline-release", type=Path, required=True)
    graph.add_argument("--object-root", type=Path, required=True)
    graph.add_argument("--channel", required=True)
    graph.add_argument("--version", required=True)
    graph.add_argument("--source-sha", required=True)
    graph.add_argument("--generation", required=True)
    graph.add_argument("--platform", required=True)
    report = commands.add_parser("emit-report")
    report.add_argument("--evidence", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    report.add_argument("--channel", required=True)
    report.add_argument("--version", required=True)
    report.add_argument("--source-sha", required=True)
    report.add_argument("--generation", required=True)
    report.add_argument("--run-id", type=int, required=True)
    report.add_argument("--platform", required=True)
    run = commands.add_parser("run")
    run.add_argument("--candidate-release", type=Path, required=True)
    run.add_argument("--baseline-release", type=Path, required=True)
    run.add_argument("--object-root", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--channel", required=True)
    run.add_argument("--version", required=True)
    run.add_argument("--source-sha", required=True)
    run.add_argument("--generation", required=True)
    run.add_argument("--run-id", type=int, required=True)
    run.add_argument("--platform", required=True)
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    try:
        if arguments.command == "check-publication-graph":
            result = check_publication_graph(arguments)
        elif arguments.command == "emit-report":
            emit_report(arguments)
            return 0
        elif arguments.command == "run":
            run_lifecycle(arguments)
            return 0
        else:
            raise LifecycleError("lifecycle command is invalid")
    except LifecycleError as error:
        print(f"Spark lifecycle failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
