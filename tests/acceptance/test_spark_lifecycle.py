"""Executable, fail-closed Spark lifecycle acceptance entry point."""

from __future__ import annotations

import argparse
import base64
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
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import NamedTuple, Protocol, Self

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
from tests.acceptance.runtime import (
    AcceptanceError,
    _compose_rows,
    assert_compose_services_healthy,
    bootstrap_command,
    run_interactive,
)
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
# of a certificate lifetime and checks renewal on its 60-second inventory tick,
# so 90 seconds preserves a real scheduled rotation while avoiding three idle
# inventory intervals in every ARM64 publication gate.
CERTIFICATE_LIFETIME_SECONDS = 90
ENROLLMENT_HOST = "enroll.spark.localhost"
AGENT_HOST = "agents.spark.localhost"
REGISTRY_HOST = "registry.spark.localhost"
CONTROLLER_ADDRESS = "127.0.0.1"
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
    except (ValueError, base64.binascii.Error) as error:
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
    model_version_sha256: str
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
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
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
        raise LifecycleError("canonical synthetic canary package archive is unavailable")
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
            content_sha256,
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
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as error:
        raise LifecycleError("canonical synthetic canary package index is invalid") from error
    try:
        recipe_contract = RecipeDefinition.model_validate(raw_recipe)
        recipe = recipe_contract.model_dump(mode="json")
        catalog_models = [
            ModelDefinition.model_validate(value["document"])
            for value in index["catalog_entities"]
            if isinstance(value, dict)
            and isinstance(value.get("document"), dict)
            and value["document"].get("kind") == "model"
        ]
    except (KeyError, TypeError, ValueError) as error:
        raise LifecycleError("canonical synthetic canary contract is invalid") from error
    if recipe != raw_recipe or len(recipe_contract.models) != 1:
        raise LifecycleError("canonical synthetic canary Recipe is not canonical")
    model_reference = recipe_contract.models[0].model
    matching_models = [
        model
        for model in catalog_models
        if model.identity.publisher == model_reference.publisher
        and model.identity.slug == model_reference.slug
        and content_sha256(model) == model_reference.content_sha256
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
        or entry["content_sha256"] != content_sha256(recipe_contract)
        or package.get("media_type")
        != "application/vnd.vonk-forge.recipe-package.v2+tar+gzip"
        or package.get("recipe_content_sha256")
        not in {None, entry["content_sha256"]}
    ):
        raise LifecycleError("canonical synthetic canary contract is invalid")
    roles = recipe_contract.topology.roles
    if (
        recipe_contract.execution.mode != "build"
        or recipe_contract.topology.mode != "single"
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
        raise LifecycleError("canonical synthetic canary package is unavailable") from error
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
            manifest = json.loads(archive.extractfile(manifest_member).read())
            packaged_recipe = json.loads(archive.extractfile(recipe_member).read())
    except (KeyError, OSError, tarfile.TarError, AttributeError, json.JSONDecodeError) as error:
        raise LifecycleError("canonical synthetic canary package closure is invalid") from error
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 2
        or manifest.get("kind") != "recipe-package"
        or manifest.get("package_type") != "recipe"
        or manifest.get("recipe_content_sha256") != entry["content_sha256"]
        or packaged_recipe != recipe
    ):
        raise LifecycleError("canonical synthetic canary Recipe differs from its package")
    return CanonicalCanaryFixture(
        index_path=index_path,
        index_bytes=index_bytes,
        package_path=package_path,
        package_bytes=package_bytes,
        source_commit=index["source_commit"],
        publisher=recipe_contract.identity.publisher,
        slug=recipe_contract.identity.slug,
        recipe_content_sha256=entry["content_sha256"],
        model_version_sha256=model_reference.content_sha256,
        role=roles[0].name,
        serving_check=check,
        recipe=recipe,
    )


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


def _configure_acceptance_renewal(
    bundle: Path, *, lifetime_seconds: int, agent_source_address: str
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
        provisioners = ca["authority"]["provisioners"]
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
        control = compose["services"]["control-api"]
        environment = control["environment"]
    except (OSError, TypeError, KeyError, yaml.YAMLError) as error:
        raise LifecycleError("Compose controller configuration is invalid") from error
    if not isinstance(environment, dict) or (
        "VONK_AGENT_CA_CERTIFICATE_LIFETIME_SECONDS" in environment
    ):
        raise LifecycleError("Compose controller environment is invalid")
    environment["VONK_AGENT_CA_CERTIFICATE_LIFETIME_SECONDS"] = str(lifetime_seconds)
    try:
        caddy_service = compose["services"]["caddy"]
        caddy_ports = caddy_service["ports"]
        caddy_mounts = caddy_service["configs"]
        caddy_source = next(
            value["source"]
            for value in caddy_mounts
            if value.get("target") == "/etc/caddy/Caddyfile"
        )
        caddy_definition = compose["configs"][caddy_source]
    except (KeyError, TypeError) as error:
        raise LifecycleError("Compose browser boundary is invalid") from error
    except StopIteration as error:
        raise LifecycleError("Caddy acceptance boundary is invalid") from error
    if re.fullmatch(
        r"vonk_runtime_[0-9a-f]{16}", caddy_source
    ) is None or caddy_definition != {
        "file": f"./secrets/runtime-configs/{caddy_source}"
    }:
        raise LifecycleError("Caddy acceptance boundary is invalid")
    caddy_path = bundle / "secrets/runtime-configs" / caddy_source
    try:
        caddy_metadata = caddy_path.lstat()
        caddy = caddy_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise LifecycleError("Caddy acceptance boundary is invalid") from error
    source_directive = "header_up X-Vonk-Agent-Source {http.request.remote.host}"
    if (
        caddy_path.is_symlink()
        or not stat.S_ISREG(caddy_metadata.st_mode)
        or caddy.count(source_directive) != 1
    ):
        raise LifecycleError("Caddy acceptance boundary is invalid")
    caddy_path.write_text(
        caddy.replace(
            source_directive,
            f"header_up X-Vonk-Agent-Source {agent_source_address}",
        ),
        encoding="utf-8",
    )
    os.chmod(caddy_path, 0o644)
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


def _configure_canonical_canary_library(
    bundle: Path, fixture: CanonicalCanaryFixture
) -> None:
    """Serve one exact producer package to the isolated Controller sync client."""

    compose_path = bundle / "docker-compose.yaml"
    try:
        compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
        control_environment = compose["services"]["control-api"]["environment"]
        caddy_service = compose["services"]["caddy"]
        caddy_configs = caddy_service["configs"]
        caddy_source = next(
            value["source"]
            for value in caddy_configs
            if value.get("target") == "/etc/caddy/Caddyfile"
        )
        caddy_path = bundle / "secrets/runtime-configs" / caddy_source
        caddy = caddy_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, TypeError, KeyError, StopIteration, yaml.YAMLError) as error:
        raise LifecycleError("canonical canary Controller package boundary is invalid") from error
    if (
        not isinstance(control_environment, dict)
        or not isinstance(caddy_service, dict)
        or "http://:8085" in caddy
    ):
        raise LifecycleError("canonical canary Controller package boundary is invalid")
    serving_root = bundle / "secrets/synthetic-recipe-library"
    index_target = serving_root / "v1/recipe-library/index.json"
    package_target = serving_root / Path(*fixture.package_path.parts)
    try:
        index_target.parent.mkdir(mode=0o755, parents=True)
        package_target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        index_target.write_bytes(fixture.index_bytes)
        package_target.write_bytes(fixture.package_bytes)
    except OSError as error:
        raise LifecycleError("canonical canary package staging failed") from error
    # The producer's package path is repository-relative and may contain
    # several directories (the canonical canary lives under
    # ``tests/fixtures/...``).  ``Path.mkdir(parents=True)`` applies its mode
    # only to the leaf, so make every bind-mounted ancestor traversable by
    # Caddy explicitly even under a private umask.
    package_directories = [
        path
        for path in package_target.parents
        if path.is_relative_to(serving_root)
    ]
    for directory in (
        serving_root,
        serving_root / "v1",
        serving_root / "v1/recipe-library",
        *reversed(package_directories),
    ):
        os.chmod(directory, 0o755)
    os.chmod(index_target, 0o644)
    os.chmod(package_target, 0o644)
    if (
        index_target.read_bytes() != fixture.index_bytes
        or package_target.read_bytes() != fixture.package_bytes
    ):
        raise LifecycleError("canonical canary staged package bytes differ")
    control_environment["VONK_RECIPE_LIBRARY_PACKAGE_URL"] = "http://caddy:8085"
    volumes = caddy_service.setdefault("volumes", [])
    if not isinstance(volumes, list):
        raise LifecycleError("canonical canary Caddy volumes are invalid")
    volumes.append(
        "./secrets/synthetic-recipe-library:/srv/vonk-recipe-library:ro"
    )
    caddy_path.write_text(
        caddy.rstrip()
        + "\n\nhttp://:8085 {\n"
        + "\troot * /srv/vonk-recipe-library\n"
        + f"\t@canary_package path /{fixture.package_path.as_posix()}\n"
        + "\theader @canary_package Content-Type application/octet-stream\n"
        + "\tfile_server\n"
        + "}\n",
        encoding="utf-8",
    )
    os.chmod(caddy_path, 0o644)
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


class LocalBrowserController:
    def __init__(
        self,
        *,
        hostname: str,
        port: int,
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
            "/api/v1/auth/login",
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
        self.synthetic_fixture_sha256: str | None = None
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
        command: list[str],
        *,
        cwd: Path,
        timeout: int = 300,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                env=host_command_environment(),
                stdin=subprocess.DEVNULL,
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
        if result.returncode != 0:
            raise LifecycleError(
                f"acceptance command failed: {Path(command[0]).name} {command[1] if len(command) > 1 else ''}".rstrip()
            )
        return result

    @staticmethod
    def _redact_diagnostics(raw: str) -> str:
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
        return redacted[-8_000:]

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

    def _installation_failure(
        self, stage: str, error: AcceptanceError
    ) -> LifecycleError:
        raw = ""
        if getattr(self, "bundle", None) is not None:
            logs = self._diagnostic_command(
                self._compose(
                    "logs",
                    "--no-color",
                    "--tail",
                    "120",
                    "control-api",
                    "step-ca",
                    "caddy",
                )
            )
            if logs is not None:
                raw = "controller diagnostics:\n" + (logs.stdout or logs.stderr)
        raw += "\ninstaller error:\n" + str(error)
        diagnostics = self._redact_diagnostics(raw)
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

    def _controller_response_replacements(self) -> dict[str, str]:
        return {
            "Trusted Spark management CIDRs: ": "172.16.0.0/12",
            "Direct GPU fabric CIDRs [192.168.100.0/24,192.168.101.0/24]: ": (
                f"198.19.{self.synthetic_fabric_octet}.0/24"
            ),
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
            enrollment_hostname=ENROLLMENT_HOST,
            agent_hostname=AGENT_HOST,
            registry_hostname=REGISTRY_HOST,
        )
        replacements = self._controller_response_replacements()
        responses = [
            (prompt, replacements.get(prompt, answer)) for prompt, answer in responses
        ]
        release_url = (
            f"{self.origin}/artifacts/{self.arguments.channel}/releases/"
            f"{self.arguments.generation}"
        )
        self.bundle = generate_bundle(
            self.temporary_root / "controller",
            candidate_url=f"{release_url}/bootstraps/nas",
            child_environment=child_environment,
            responses=responses,
        )
        _configure_acceptance_renewal(
            self.bundle,
            lifetime_seconds=CERTIFICATE_LIFETIME_SECONDS,
            agent_source_address=f"172.31.{self.synthetic_fabric_octet}.1",
        )
        library_root = self._required_environment("VONK_RECIPE_LIBRARY_ROOT")
        self.synthetic_canary_fixture = _canonical_canary_fixture(
            Path(library_root)
        )
        _configure_canonical_canary_library(
            self.bundle, self.synthetic_canary_fixture
        )
        self._assert_project_is_empty()
        self._assert_compose_image_graph()
        try:
            self._run_command(
                self._local_controller_up_command(),
                cwd=self.bundle,
                timeout=420,
            )
        except LifecycleError as error:
            diagnostics = self._controller_startup_diagnostics()
            raise LifecycleError(
                f"candidate controller startup failed; {diagnostics}"
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
        )
        self.browser = boundary
        password = self._read_secret("admin-password")
        self.control = boundary.login(password, timeout=30)
        del password

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
            self.arguments.candidate_release, "candidate release object"
        )
        images = _object(candidate.get("images"), "candidate image graph")
        if candidate.get("generation") != self.arguments.generation:
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
            self.arguments.candidate_release, "candidate release object"
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
            "VONK_ENDPOINT_HOST_PORTS": "8000,8101",
            "VONK_HOST_ENDPOINT_PORTS": "8888",
            "VONK_RENDEZVOUS_PORT": "29500",
            "VONK_FABRIC_BANDWIDTH_MBPS": "200000",
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
        assert self.control is not None
        try:
            _, response = self.control.request(
                "POST", "/api/v1/agents/enrollments/grants", {"ttl_seconds": 600}
            )
            grant = require_object(response, "enrollment grant")
        except SliceError as error:
            raise LifecycleError(
                "single-use enrollment grant creation failed"
            ) from error
        expected = {
            "ca_fingerprint",
            "controller_address",
            "controller_endpoint",
            "enrollment_endpoint",
            "expires_at",
            "id",
            "installer_url",
            "purpose",
            "service_hostnames",
            "token",
        }
        grant_id = grant.get("id")
        enrollment = grant.get("enrollment_endpoint")
        controller = grant.get("controller_endpoint")
        ca_sha256 = grant.get("ca_fingerprint")
        token = grant.pop("token", None)
        if (
            set(grant) | {"token"} != expected
            or not isinstance(grant_id, str)
            or re.fullmatch(r"[0-9a-f-]{36}", grant_id) is None
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
        self_test = self._self_test()
        if self_test.get("self_test_passed") is not True:
            raise LifecycleError("direct Rust agent is not healthy")
        return {
            "healthy": True,
            "implementation": "rust",
            "transport": "direct",
        }

    def _run_synthetic_canary(self, node_id: str) -> dict[str, object]:
        assert (
            self.control is not None
            and self.browser is not None
            and isinstance(self.synthetic_canary_fixture, CanonicalCanaryFixture)
        )
        if NODE_ID.fullmatch(node_id) is None:
            raise LifecycleError("synthetic canary node identity is invalid")
        fixture = self.synthetic_canary_fixture
        completed = ["inventory-ready"]
        try:
            _, sync_payload = self.control.request(
                "POST",
                "/api/v1/catalog/managed-recipes/sync",
                {
                    "request_key": self._canary_request_key(
                        fixture, node_id, "catalog-sync"
                    ),
                    "expected_commit": fixture.source_commit,
                },
            )
            sync = require_object(sync_payload, "synthetic canary catalog sync")
            if (
                sync.get("state") != "current"
                or sync.get("commit") != fixture.source_commit
                or sync.get("total_count") != 1
                or sync.get("processed_count") != 1
                or (sync.get("imported_count"), sync.get("unchanged_count"))
                not in {(1, 0), (0, 1)}
                or sync.get("problems") != []
            ):
                raise LifecycleError("synthetic canary catalog sync is incomplete")
            _, listed_payload = self.control.request(
                "GET", "/api/v1/library/recipes"
            )
            listed = require_object(listed_payload, "synthetic canary Library")
            recipes = listed.get("recipes")
            if not isinstance(recipes, list):
                raise LifecycleError("synthetic canary Library response is invalid")
            matches = [
                value
                for value in recipes
                if isinstance(value, dict)
                and value.get("publisher") == fixture.publisher
                and value.get("slug") == fixture.slug
                and value.get("content_sha256") == fixture.recipe_content_sha256
            ]
            if len(matches) != 1:
                raise LifecycleError("exact synthetic canary Recipe is unavailable")
            summary = matches[0]
            recipe_id = summary.get("recipe_id")
            revision_id = summary.get("recipe_revision_id")
            if (
                not isinstance(recipe_id, str)
                or UUID.fullmatch(recipe_id) is None
                or not isinstance(revision_id, str)
                or UUID.fullmatch(revision_id) is None
            ):
                raise LifecycleError("synthetic canary Recipe identity is invalid")
            _, detail_payload = self.control.request(
                "GET", f"/api/v1/library/recipes/{recipe_id}"
            )
            detail = require_object(detail_payload, "synthetic canary Recipe detail")
            model_documents = detail.get("model_documents")
            model_item = (
                model_documents[0]
                if isinstance(model_documents, list)
                and len(model_documents) == 1
                and isinstance(model_documents[0], dict)
                else None
            )
            selection = model_item.get("selection") if model_item is not None else None
            selected_model = (
                selection.get("model") if isinstance(selection, dict) else None
            )
            if (
                detail.get("definition") != fixture.recipe
                or not isinstance(selected_model, dict)
                or selected_model.get("content_sha256")
                != fixture.model_version_sha256
            ):
                raise LifecycleError("synthetic canary canonical closure differs")
            completed.append("recipe-resolved")
            run_request = {
                "schema_version": 2,
                "model_version_sha256": fixture.model_version_sha256,
                "recipe_revision_id": revision_id,
                "spark_group": {
                    "nodes": [
                        {
                            "node_id": node_id,
                            "rank": 0,
                            "role": fixture.role,
                            "endpoint_owner": True,
                        }
                    ]
                },
                "alias": fixture.slug,
                "action": "run",
                "retention": "retain-cached",
                "invocation": {
                    "origin": "spark-lifecycle-acceptance",
                    "reason": "fresh canonical synthetic serving canary",
                },
            }
            _, preview_payload = self.control.request(
                "POST", "/api/v1/recipes/run-switch-plans/preview", run_request
            )
            preview = require_object(preview_payload, "synthetic canary run preview")
            plan_digest = preview.get("plan_digest")
            phases = preview.get("phases")
            planned = [
                (phase.get("kind"), phase.get("subphase"))
                for phase in phases
                if isinstance(phase, dict)
            ] if isinstance(phases, list) else []
            required_phases = {
                ("prepare", "container-build"),
                ("transfer", "model-download"),
                ("prepare", "runtime-image"),
                ("prepare", "runtime-plan"),
                ("transfer", "target-copy"),
                ("verify", "target-copy"),
                ("prepare", "runtime-install"),
                ("start", None),
                ("final_verify", None),
            }
            build = preview.get("build")
            source = build.get("source") if isinstance(build, dict) else None
            if (
                preview.get("allowed") is not True
                or not isinstance(plan_digest, str)
                or SHA256.fullmatch(plan_digest) is None
                or not required_phases.issubset(planned)
                or not isinstance(source, dict)
                or source.get("state") != "available"
            ):
                details = {
                    "allowed": preview.get("allowed"),
                    "blockers": preview.get("blockers"),
                    "phases": planned,
                    "source_state": source.get("state") if isinstance(source, dict) else None,
                }
                raise LifecycleError(
                    "canonical synthetic canary run is not admitted: "
                    + self._redact_diagnostics(json.dumps(details))
                )
            completed.append("source-verified")
            _, operation_payload = self.control.request(
                "POST",
                "/api/v1/recipes/run-switches",
                {
                    **run_request,
                    "plan_digest": plan_digest,
                    "request_key": self._canary_request_key(
                        fixture, node_id, "run-switch"
                    ),
                },
            )
            operation = self._await_canary_run_switch(
                require_object(operation_payload, "synthetic canary run operation"),
                expected_phases=[kind for kind, _subphase in planned],
                label="synthetic canary run",
            )
            result = require_object(operation.get("result"), "synthetic canary run result")
            phase_results = result.get("phase_results")
            if not isinstance(phase_results, list):
                raise LifecycleError("synthetic canary run evidence is invalid")
            image = next(
                (
                    value
                    for value in phase_results
                    if isinstance(value, dict)
                    and isinstance(value.get("image_digest"), str)
                    and re.fullmatch(r"sha256:[0-9a-f]{64}", value["image_digest"])
                    is not None
                    and isinstance(value.get("oci_layout_sha256"), str)
                    and SHA256.fullmatch(value["oci_layout_sha256"]) is not None
                    and type(value.get("image_bytes")) is int
                    and value["image_bytes"] > 0
                ),
                None,
            )
            installation_id = self._canary_phase_identity(
                phase_results, "installation_id"
            )
            run_id = self._canary_phase_identity(phase_results, "run_id")
            verified_image = next(
                (
                    value
                    for value in phase_results
                    if isinstance(value, dict)
                    and image is not None
                    and value.get("verified_image_digest")
                    == image.get("image_digest")
                    and value.get("verified_oci_layout_sha256")
                    == image.get("oci_layout_sha256")
                ),
                None,
            )
            if image is None or verified_image is None:
                raise LifecycleError("synthetic canary build evidence is incomplete")
            completed.extend(("image-built", "image-distributed", "installed", "running"))
            self._await_canary_endpoint(fixture.slug, published=True)
            completed.append("route-published")
            inference_key = self._read_secret("litellm-master-key")
            inference = self.browser.bearer(inference_key, timeout=30)
            del inference_key
            response_digest = self._run_canonical_inference(
                inference, fixture.serving_check, fixture.slug
            )
            completed.append("inference-ok")
            stop_request = {
                "schema_version": 2,
                "run_id": run_id,
                "invocation": {
                    "origin": "spark-lifecycle-acceptance",
                    "reason": "complete fresh canonical synthetic serving canary",
                },
            }
            _, stop_preview_payload = self.control.request(
                "POST", "/api/v1/recipes/run-switch-stops/preview", stop_request
            )
            stop_preview = require_object(
                stop_preview_payload, "synthetic canary stop preview"
            )
            stop_digest = stop_preview.get("plan_digest")
            stop_phases = stop_preview.get("phases")
            stop_kinds = [
                phase.get("kind")
                for phase in stop_phases
                if isinstance(phase, dict)
            ] if isinstance(stop_phases, list) else []
            if (
                stop_preview.get("allowed") is not True
                or not isinstance(stop_digest, str)
                or SHA256.fullmatch(stop_digest) is None
                or stop_kinds != ["stop", "final_verify"]
            ):
                raise LifecycleError("synthetic canary stop is not admitted")
            _, stop_payload = self.control.request(
                "POST",
                "/api/v1/recipes/run-switch-stops",
                {
                    **stop_request,
                    "plan_digest": stop_digest,
                    "request_key": self._canary_request_key(
                        fixture, node_id, f"stop:{run_id}"
                    ),
                },
            )
            self._await_canary_run_switch(
                require_object(stop_payload, "synthetic canary stop operation"),
                expected_phases=stop_kinds,
                label="synthetic canary stop",
            )
            completed.append("stopped")
            self._await_canary_endpoint(fixture.slug, published=False)
            completed.append("route-withdrawn")
            _, uninstall_preview_payload = self.control.request(
                "POST",
                "/api/v1/recipes/uninstall-plans/preview",
                {"installation_id": installation_id},
            )
            uninstall_preview = require_object(
                uninstall_preview_payload, "synthetic canary uninstall preview"
            )
            uninstall_digest = uninstall_preview.get("plan_digest")
            if (
                uninstall_preview.get("allowed") is not True
                or uninstall_preview.get("installation_id") != installation_id
                or uninstall_preview.get("recipe_content_sha256")
                != fixture.recipe_content_sha256
                or uninstall_preview.get("active_run_count") != 0
                or not isinstance(uninstall_digest, str)
                or SHA256.fullmatch(uninstall_digest) is None
            ):
                raise LifecycleError("synthetic canary uninstall is not admitted")
            _, uninstall_payload = self.control.request(
                "POST",
                f"/api/v1/recipes/installations/{installation_id}/uninstall",
                {
                    "plan_digest": uninstall_digest,
                    "request_key": self._canary_request_key(
                        fixture, node_id, f"uninstall:{installation_id}"
                    ),
                },
            )
            self._await_canary_recipe_operation(
                require_object(
                    uninstall_payload, "synthetic canary uninstall operation"
                ),
                node_id=node_id,
                owner_id=installation_id,
                plan_digest=uninstall_digest,
            )
            completed.append("uninstalled")
        except (SliceError, ServingExecutionError) as error:
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

    def _await_canary_run_switch(
        self,
        operation: dict[str, object],
        *,
        expected_phases: list[object],
        label: str,
    ) -> dict[str, object]:
        assert self.control is not None
        operation_id = operation.get("operation_id")
        if not isinstance(operation_id, str) or UUID.fullmatch(operation_id) is None:
            raise LifecycleError(f"{label} identity is invalid")
        deadline = time.monotonic() + 300
        while operation.get("state") in {"queued", "running"}:
            if time.monotonic() >= deadline:
                raise LifecycleError(f"{label} did not converge")
            time.sleep(1)
            _, payload = self.control.request(
                "GET", f"/api/v1/recipes/run-switches/{operation_id}"
            )
            operation = require_object(payload, label)
        if (
            operation.get("state") != "succeeded"
            or operation.get("completed_phases") != expected_phases
        ):
            reason = operation.get("status_reason")
            raise LifecycleError(
                f"{label} failed: {reason if isinstance(reason, str) else 'incomplete evidence'}"
            )
        return operation

    def _await_canary_recipe_operation(
        self,
        operation: dict[str, object],
        *,
        node_id: str,
        owner_id: str,
        plan_digest: str,
    ) -> dict[str, object]:
        assert self.control is not None
        operation_id = operation.get("id")
        if not isinstance(operation_id, str) or UUID.fullmatch(operation_id) is None:
            raise LifecycleError("synthetic canary uninstall identity is invalid")
        deadline = time.monotonic() + 300
        while operation.get("state") in {"queued", "running"}:
            if time.monotonic() >= deadline:
                raise LifecycleError("synthetic canary uninstall did not converge")
            time.sleep(1)
            _, payload = self.control.request(
                "GET", f"/api/v1/recipes/operations/{operation_id}"
            )
            operation = require_object(payload, "synthetic canary uninstall")
        if (
            operation.get("state") != "succeeded"
            or operation.get("owner_id") != owner_id
            or operation.get("plan_digest") != plan_digest
            or operation.get("nodes") != [node_id]
        ):
            raise LifecycleError("synthetic canary uninstall evidence is incomplete")
        return operation

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

    def _await_canary_endpoint(self, alias: str, *, published: bool) -> None:
        assert self.control is not None
        deadline = time.monotonic() + 300
        while True:
            allowed = (200,) if published else (404, 503)
            try:
                status, payload = self.control.request(
                    "GET", f"/api/v1/endpoints/{alias}", allowed=allowed
                )
            except SliceError:
                status, payload = 0, None
            if published and status == 200:
                endpoint = require_object(payload, "synthetic canary endpoint")
                if endpoint.get("alias") == alias:
                    return
            if not published and status in {404, 503}:
                return
            if time.monotonic() >= deadline:
                raise LifecycleError("synthetic canary route state did not converge")
            time.sleep(1)

    @staticmethod
    def _run_canonical_inference(
        inference: Client, check: dict[str, object], alias: str
    ) -> str:
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
        responses: list[dict[str, object]] = []
        for _attempt in range(2):
            status, payload = inference.request("POST", str(request["path"]), body)
            response = require_object(payload, "synthetic serving response")
            evaluate_http_response(
                HttpObservation(status=status, headers={}, body=_canonical(response)),
                check,
            )
            responses.append(response)
        if responses[0] != responses[1]:
            raise LifecycleError("synthetic canary response is not deterministic")
        return hashlib.sha256(_canonical(responses[0])).hexdigest()

    @staticmethod
    def _serial_proof(serial: str) -> str:
        if SERIAL.fullmatch(serial) is None:
            raise LifecycleError("certificate serial is invalid")
        encoded = format(int(serial), "x")
        if not 16 <= len(encoded) <= 64:
            raise LifecycleError("certificate serial proof is invalid")
        return encoded

    def _old_certificate_rejected(self, serial_before: str) -> bool:
        assert self.temporary_root is not None
        if SERIAL.fullmatch(serial_before) is None:
            raise LifecycleError("retired agent certificate serial is invalid")
        credential_root = AGENT_DATA / "credentials"
        probe = self.temporary_root / "retired-agent-probe"
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
            if (
                not isinstance(metadata, dict)
                or metadata.get("serial") != serial_before
            ):
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
                    f"https://{AGENT_HOST}:8443/agent/v1/source-bundles/{'0' * 64}",
                ],
                cwd=Path("/"),
                timeout=40,
            )
            return result.stdout == "401"
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
                if not self._old_certificate_rejected(serial_before):
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
            "self_test_passed",
            "semantic_version",
        }
        receipt_key = document.get("observation_receipt_public_key")
        if (
            not isinstance(document, dict)
            or not identity_fields <= set(document)
            or document.get("self_test_passed") is not True
            or not isinstance(document.get("build_digest"), str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", document["build_digest"]) is None
            or not isinstance(document.get("binary_digest"), str)
            or SHA256.fullmatch(document["binary_digest"]) is None
            or (
                receipt_key is not None
                and (
                    not isinstance(receipt_key, str)
                    or SHA256.fullmatch(receipt_key) is None
                )
            )
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
                _, response = self.control.request("GET", "/api/v1/agents")
                agents = require_object(response, "agents").get("agents")
                matching = [
                    agent
                    for agent in agents
                    if isinstance(agent, dict)
                    and agent.get("semantic_version") == expected_semantic
                ]
                if len(matching) != 1:
                    raise LifecycleError(
                        "controller has not observed the direct agent: "
                        f"semantic_version_matches={len(matching)}"
                    )
                agent = matching[0]
                node_id = agent.get("node_id")
                agent_mismatches = []
                if not isinstance(node_id, str) or NODE_ID.fullmatch(node_id) is None:
                    agent_mismatches.append("node_id")
                if agent.get("state") != "active":
                    agent_mismatches.append("state")
                if agent.get("stale") is not False:
                    agent_mismatches.append("stale")
                if agent.get("build_digest") != self_test["build_digest"]:
                    agent_mismatches.append("build_digest")
                if agent.get("binary_digest") != self_test["binary_digest"]:
                    agent_mismatches.append("binary_digest")
                if "agent.runtime.rust.v1" not in agent.get("capabilities", []):
                    agent_mismatches.append("capabilities")
                if agent_mismatches:
                    raise LifecycleError(
                        "controller direct-agent identity is invalid: "
                        + ",".join(agent_mismatches)
                    )
                rows = self._psql(
                    "SELECT architecture,semantic_version,build_digest,binary_digest,"
                    "self_test_passed::int,contact_certificate_serial "
                    f"FROM agent_nodes WHERE node_id='{node_id}'"
                )
                expected_row = [
                    expected_architecture,
                    expected_semantic,
                    str(self_test["build_digest"]),
                    str(self_test["binary_digest"]),
                    "1",
                ]
                row_mismatches = []
                if len(rows) != 1:
                    row_mismatches.append("row_count")
                elif rows[0][:5] != expected_row:
                    row_mismatches.extend(
                        field
                        for index, field in enumerate(
                            (
                                "architecture",
                                "semantic_version",
                                "build_digest",
                                "binary_digest",
                                "self_test_passed",
                            )
                        )
                        if len(rows[0]) <= index
                        or rows[0][index] != expected_row[index]
                    )
                if len(rows) == 1 and len(rows[0]) != 6:
                    row_mismatches.append("column_count")
                elif len(rows) == 1 and SERIAL.fullmatch(rows[0][5]) is None:
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
                        self.graph["baseline_package_sha256"]
                        if package_version == self.graph["baseline_version"]
                        else self.graph["candidate_package_sha256"]
                    ),
                    "serial": rows[0][5],
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
    if set(evidence) != {
        "channel",
        "completed_phases",
        "generation",
        "platform",
        "proof",
        "run_id",
        "schema_version",
        "source_sha",
        "version",
    } or any(
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
