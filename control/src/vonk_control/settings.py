"""Strict application configuration loaded from paths and secret files."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from .dev_cohort import DevelopmentCohortError, require_selected_cohort
from .presence import ManagementAddressPolicy, PresenceError


class SettingsError(ValueError):
    pass


class StartupMode(StrEnum):
    """Explicit control-process mode during host generation selection."""

    PRESELECTION = "preselection"
    SELECTED = "selected"


_AGENT_PROXY_AUTH_PATTERN = re.compile(rb"[A-Za-z0-9_-]{32,}\Z")
_GENERATION_IDENTITY_ENVIRONMENT = (
    "VONK_CONTROL_GENERATION_ID",
    "VONK_DATABASE_REVISION",
    "VONK_PLATFORM_VERSION",
    "VONK_PLATFORM_RELEASE_DIGEST",
    "VONK_PLATFORM_BUILD_DIGEST",
    "VONK_CONTROL_PROCESS_IMAGE",
    "VONK_CONTROL_START_NONCE",
)


def _secret(name: str, *, production: bool) -> str:
    raw_name = name.removesuffix("_FILE")
    raw = os.environ.get(raw_name)
    source = os.environ.get(name)
    if production and raw:
        raise SettingsError(f"{raw_name} must be supplied through a secret file")
    if source:
        path = Path(source)
        if path.is_symlink() or not path.is_file():
            raise SettingsError(f"{name} must name a regular non-symlink file")
        value = path.read_text().strip()
    else:
        value = raw or ""
    if not value:
        raise SettingsError(f"{name} is required")
    return value


def _secret_path(name: str) -> Path:
    source = os.environ.get(name)
    if not source:
        raise SettingsError(f"{name} is required")
    path = Path(source)
    if path.is_symlink() or not path.is_file():
        raise SettingsError(f"{name} must name a regular non-symlink file")
    return path


def _secret_or_file(name: str, file_name: str) -> str:
    raw = os.environ.get(name)
    source = os.environ.get(file_name)
    if raw is not None and source is not None:
        raise SettingsError(f"{name} and {file_name} cannot be combined")
    if source:
        path = Path(source)
        if path.is_symlink() or not path.is_file():
            raise SettingsError(f"{file_name} must name a regular non-symlink file")
        value = path.read_text().strip()
        if not value:
            raise SettingsError(f"{file_name} must not be empty")
        return value
    return (raw or "").strip()


def _agent_proxy_auth_secret(name: str, *, production: bool) -> bytes:
    raw_name = name.removesuffix("_FILE")
    raw = os.environ.get(raw_name)
    source = os.environ.get(name)
    if production and raw:
        raise SettingsError(f"{raw_name} must be supplied through a secret file")
    if source:
        path = Path(source)
        if path.is_symlink() or not path.is_file():
            raise SettingsError(f"{name} must name a regular non-symlink file")
        value = path.read_bytes()
    else:
        value = (raw or "").encode("ascii", errors="strict")
    normalized = value.rstrip(b"\r\n")
    if _AGENT_PROXY_AUTH_PATTERN.fullmatch(normalized) is None:
        raise SettingsError(f"{name} must contain one base64url-like token of at least 32 characters")
    return normalized


def _absolute_root(name: str, default: str) -> Path:
    value = os.environ.get(name, default)
    path = Path(value)
    if not path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise SettingsError(f"{name} must be an absolute normalized path")
    return path


@dataclass(frozen=True)
class GenerationStartupSettings:
    """Minimal immutable identity shared by one generation's API and worker."""

    database_url: str
    startup_mode: StartupMode
    identity_root: Path
    operation_id: str | None
    generation_id: str
    release_digest: str
    build_digest: str
    platform_version: str
    process_image: str
    database_revision: str
    start_nonce: str

    @classmethod
    def from_env_and_secrets(cls) -> GenerationStartupSettings:
        deployment_mode = os.environ.get("VONK_DEPLOYMENT_MODE", "development")
        if deployment_mode not in {"development", "test", "production"}:
            raise SettingsError("VONK_DEPLOYMENT_MODE is invalid")
        raw_mode = os.environ.get("VONK_CONTROL_STARTUP_MODE", "selected")
        try:
            startup_mode = StartupMode(raw_mode)
        except ValueError as error:
            raise SettingsError("VONK_CONTROL_STARTUP_MODE is invalid") from error
        database_url = _secret(
            "VONK_DATABASE_URL_FILE",
            production=deployment_mode == "production",
        )
        if urlsplit(database_url).scheme not in {
            "postgresql",
            "postgresql+psycopg",
        }:
            raise SettingsError("database URL must use PostgreSQL")
        operation_id = os.environ.get("VONK_CONTROL_OPERATION_ID")
        if startup_mode is StartupMode.PRESELECTION:
            if operation_id is None or re.fullmatch(
                r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?",
                operation_id,
            ) is None:
                raise SettingsError(
                    "VONK_CONTROL_OPERATION_ID is required for preselection"
                )
        elif operation_id is not None:
            raise SettingsError(
                "VONK_CONTROL_OPERATION_ID is forbidden in selected mode"
            )
        selected_name = "VONK_DEV_SELECTED_COHORT_FILE"
        if selected_name in os.environ:
            if deployment_mode != "development":
                raise SettingsError(
                    "VONK_DEV_SELECTED_COHORT_FILE is development-only"
                )
            if startup_mode is not StartupMode.SELECTED:
                raise SettingsError(
                    "development selected cohort requires selected startup mode"
                )
            if any(name in os.environ for name in _GENERATION_IDENTITY_ENVIRONMENT):
                raise SettingsError(
                    "explicit generation identity cannot be combined with "
                    "VONK_DEV_SELECTED_COHORT_FILE"
                )
            role = os.environ.get("VONK_CONTROL_PROCESS_ROLE", "")
            if role not in {"api", "worker"}:
                raise SettingsError(
                    "VONK_CONTROL_PROCESS_ROLE must be api or worker"
                )
            selected_path = os.environ.get(selected_name, "")
            if not selected_path:
                raise SettingsError(f"{selected_name} is required")
            try:
                selected = require_selected_cohort(Path(selected_path), role)
            except DevelopmentCohortError as error:
                raise SettingsError(
                    "development selected cohort is invalid"
                ) from error
            generation_id = selected.generation_id
            database_revision = selected.database_revision
            version = selected.platform_version
            release = selected.release_digest
            build = selected.build_digest
            image = selected.api_image if role == "api" else selected.worker_image
            nonce = selected.start_nonce
        else:
            generation_id = os.environ.get("VONK_CONTROL_GENERATION_ID", "")
            database_revision = os.environ.get("VONK_DATABASE_REVISION", "")
            version = os.environ.get("VONK_PLATFORM_VERSION", "")
            release = os.environ.get("VONK_PLATFORM_RELEASE_DIGEST", "")
            build = os.environ.get("VONK_PLATFORM_BUILD_DIGEST", "")
            image = os.environ.get("VONK_CONTROL_PROCESS_IMAGE", "")
            nonce = os.environ.get("VONK_CONTROL_START_NONCE", "")
        if re.fullmatch(
            r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?", generation_id
        ) is None:
            raise SettingsError("VONK_CONTROL_GENERATION_ID is invalid")
        if re.fullmatch(
            r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?", database_revision
        ) is None:
            raise SettingsError("VONK_DATABASE_REVISION is invalid")
        if re.fullmatch(
            r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version
        ) is None:
            raise SettingsError("VONK_PLATFORM_VERSION is invalid")
        for name, value in (
            ("VONK_PLATFORM_RELEASE_DIGEST", release),
            ("VONK_PLATFORM_BUILD_DIGEST", build),
        ):
            if re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
                raise SettingsError(f"{name} is invalid")
        if re.fullmatch(r"[^\s]{1,1900}@sha256:[0-9a-f]{64}", image) is None:
            raise SettingsError("VONK_CONTROL_PROCESS_IMAGE is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", nonce) is None:
            raise SettingsError("VONK_CONTROL_START_NONCE is invalid")
        return cls(
            database_url=database_url,
            startup_mode=startup_mode,
            identity_root=_absolute_root(
                "VONK_CONTROL_IDENTITY_ROOT", "/control-identity"
            ),
            operation_id=operation_id,
            generation_id=generation_id,
            release_digest=release,
            build_digest=build,
            platform_version=version,
            process_image=image,
            database_revision=database_revision,
            start_nonce=nonce,
        )


@dataclass(frozen=True)
class Settings:
    database_url: str
    repository_path: Path
    state_path: Path
    deployment_mode: str
    legacy_direct_transport: str
    token_signing_key: bytes
    metrics_token: str
    git_signing_key_path: Path | None
    admin_grant_private_key_path: Path | None
    deployment_branch: str
    required_checks: tuple[str, ...]
    agent_ca_provider: str
    agent_runtime: str
    agent_client_ca: bytes
    agent_intermediate_certificate: bytes
    agent_intermediate_certificate_path: Path | None
    agent_intermediate_key_path: Path | None
    agent_ca_credential_path: Path | None
    agent_ca_provisioner_public_jwk_path: Path | None
    agent_ca_url: str
    agent_ca_root_path: Path | None
    agent_ca_provisioner_name: str
    agent_ca_provisioner_kid: str
    agent_ca_timeout_seconds: float
    agent_ca_max_response_bytes: int
    agent_artifact_root: Path
    agent_tuf_metadata_root: Path
    agent_tuf_target_root: Path
    workload_tuf_metadata_root: Path
    workload_tuf_target_root: Path
    agent_proxy_auth: bytes
    worker_api_token: bytes
    management_cidrs: str
    direct_fabric_cidrs: str
    package_helper_grant_private_key_path: Path | None = None
    package_helper_receipt_private_key_path: Path | None = None
    workload_signer_socket_path: Path = Path("/run/vonk-workload-signer/signer.sock")
    global_catalog_url: str = "https://vonkforge.ai"

    @property
    def database_host(self) -> str | None:
        return urlsplit(self.database_url).hostname

    @classmethod
    def from_env_and_secrets(cls) -> Settings:
        mode = os.environ.get("VONK_DEPLOYMENT_MODE", "development")
        if mode not in {"development", "test", "production"}:
            raise SettingsError("VONK_DEPLOYMENT_MODE is invalid")
        legacy_direct_transport = os.environ.get(
            "VONK_LEGACY_DIRECT_TRANSPORT",
            "",
        )
        if legacy_direct_transport not in {"", "explicit-test-only"}:
            raise SettingsError("legacy direct transport selector is invalid")
        if mode == "production" and legacy_direct_transport:
            raise SettingsError("legacy direct transport is forbidden in production")
        agent_ca_provider = os.environ.get("VONK_AGENT_CA_PROVIDER", "")
        agent_runtime = os.environ.get(
            "VONK_AGENT_RUNTIME",
            "disabled" if mode == "development" else "enabled",
        )
        if agent_runtime not in {"enabled", "disabled"}:
            raise SettingsError("VONK_AGENT_RUNTIME is invalid")
        agent_enabled = agent_runtime == "enabled" and mode in {"development", "production"}
        builtin_bootstrap = os.environ.get("VONK_AGENT_BUILTIN_CA_BOOTSTRAP", "")
        if builtin_bootstrap not in {"", "1"}:
            raise SettingsError("VONK_AGENT_BUILTIN_CA_BOOTSTRAP is invalid")
        if mode == "production" and not agent_ca_provider:
            raise SettingsError("VONK_AGENT_CA_PROVIDER is required in production")
        if agent_ca_provider and agent_ca_provider not in {"step-ca", "builtin"}:
            raise SettingsError("VONK_AGENT_CA_PROVIDER is invalid")
        if mode == "development" and agent_ca_provider == "step-ca":
            raise SettingsError("development agent runtime cannot use step-ca")
        if (
            mode == "development"
            and agent_runtime == "enabled"
            and agent_ca_provider != "builtin"
        ):
            raise SettingsError(
                "development agent runtime requires the builtin provider"
            )
        step_ca_settings_present = any(
            os.environ.get(name)
            for name in (
                "VONK_AGENT_CA_CREDENTIAL", "VONK_AGENT_CA_CREDENTIAL_FILE",
                "VONK_AGENT_CA_PROVISIONER_PUBLIC_JWK_FILE", "VONK_AGENT_CA_ROOT_FILE",
            )
        )
        builtin_settings_present = bool(
            builtin_bootstrap or os.environ.get("VONK_AGENT_INTERMEDIATE_KEY_FILE")
        )
        if (
            agent_ca_provider == "builtin" and step_ca_settings_present
        ) or (
            agent_ca_provider == "step-ca" and builtin_settings_present
        ):
            raise SettingsError("agent CA provider settings cannot be combined")
        if agent_ca_provider == "builtin" and builtin_bootstrap != "1":
            raise SettingsError("built-in CA requires explicit bootstrap selection")
        if agent_ca_provider != "builtin" and builtin_bootstrap:
            raise SettingsError("built-in CA bootstrap requires the builtin provider")
        database_url = _secret("VONK_DATABASE_URL_FILE", production=mode == "production")
        if urlsplit(database_url).scheme not in {"postgresql", "postgresql+psycopg"}:
            raise SettingsError("database URL must use PostgreSQL")
        management_cidrs = _secret_or_file(
            "VONK_MANAGEMENT_CIDRS",
            "VONK_MANAGEMENT_CIDRS_FILE",
        )
        direct_fabric_cidrs = os.environ.get(
            "VONK_DIRECT_FABRIC_CIDRS", ""
        ).strip()
        if mode == "production" and not management_cidrs:
            raise SettingsError("VONK_MANAGEMENT_CIDRS is required in production")
        if not management_cidrs and direct_fabric_cidrs:
            raise SettingsError(
                "VONK_MANAGEMENT_CIDRS is required when direct fabric CIDRs are set"
            )
        if management_cidrs:
            try:
                ManagementAddressPolicy.parse(
                    management_cidrs,
                    forbidden_cidrs=direct_fabric_cidrs,
                )
            except PresenceError as error:
                raise SettingsError(str(error)) from error
        signing_file = os.environ.get("VONK_TOKEN_SIGNING_KEY_FILE")
        if signing_file:
            signing_path = Path(signing_file)
            if signing_path.is_symlink() or not signing_path.is_file():
                raise SettingsError("token signing key must be a regular non-symlink file")
            signing_key = signing_path.read_bytes().strip()
        elif mode == "production":
            raise SettingsError("VONK_TOKEN_SIGNING_KEY_FILE is required in production")
        else:
            signing_key = b"development-only-signing-key-32b"
        if len(signing_key) < 32:
            raise SettingsError("token signing key must contain at least 32 bytes")
        metrics_file = os.environ.get("VONK_METRICS_TOKEN_FILE")
        if metrics_file:
            metrics_path = Path(metrics_file)
            if metrics_path.is_symlink() or not metrics_path.is_file():
                raise SettingsError("metrics token must be a regular non-symlink file")
            metrics_token = metrics_path.read_text().strip()
        elif mode == "production":
            raise SettingsError("VONK_METRICS_TOKEN_FILE is required in production")
        else:
            metrics_token = "development-metrics-token"
        if len(metrics_token) < 16 or any(character.isspace() for character in metrics_token):
            raise SettingsError("metrics token is invalid")
        git_signing_raw = os.environ.get("VONK_GIT_SIGNING_KEY_FILE")
        git_signing_key_path = Path(git_signing_raw) if git_signing_raw else None
        if git_signing_key_path is not None and (
            git_signing_key_path.is_symlink() or not git_signing_key_path.is_file()
        ):
            raise SettingsError("Git signing key must be a regular non-symlink file")
        if mode == "production" and git_signing_key_path is None:
            raise SettingsError("VONK_GIT_SIGNING_KEY_FILE is required in production")
        deployment_branch = os.environ.get("VONK_DEPLOYMENT_BRANCH", "deploy")
        if not deployment_branch or any(value in deployment_branch for value in ("..", "//", "\n", "\x00")):
            raise SettingsError("deployment branch is invalid")
        required_checks = tuple(
            value.strip() for value in os.environ.get("VONK_REQUIRED_CHECKS", "").split(",")
            if value.strip()
        )
        if len(required_checks) != len(set(required_checks)):
            raise SettingsError("required checks must be unique")
        agent_client_ca = _secret("VONK_AGENT_CLIENT_CA_FILE", production=True).encode() if agent_enabled else b""
        agent_intermediate_certificate_path = (
            _secret_path("VONK_AGENT_INTERMEDIATE_CERTIFICATE_FILE") if agent_enabled else None
        )
        agent_intermediate_certificate = (
            agent_intermediate_certificate_path.read_bytes() if agent_intermediate_certificate_path else b""
        )
        agent_intermediate_key_path = (
            _secret_path("VONK_AGENT_INTERMEDIATE_KEY_FILE")
            if agent_enabled and agent_ca_provider == "builtin" else None
        )
        step_ca_enabled = agent_enabled and agent_ca_provider == "step-ca"
        agent_ca_credential_path = _secret_path("VONK_AGENT_CA_CREDENTIAL_FILE") if step_ca_enabled else None
        agent_ca_provisioner_public_jwk_path = (
            _secret_path("VONK_AGENT_CA_PROVISIONER_PUBLIC_JWK_FILE") if step_ca_enabled else None
        )
        agent_ca_root_path = _secret_path("VONK_AGENT_CA_ROOT_FILE") if step_ca_enabled else None
        agent_ca_url = os.environ.get("VONK_AGENT_CA_URL", "") if step_ca_enabled else ""
        parsed_ca_url = urlsplit(agent_ca_url)
        if step_ca_enabled and (
            parsed_ca_url.scheme != "https" or not parsed_ca_url.hostname
            or parsed_ca_url.path not in {"", "/"} or parsed_ca_url.query or parsed_ca_url.fragment
            or parsed_ca_url.username is not None or parsed_ca_url.password is not None
        ):
            raise SettingsError("VONK_AGENT_CA_URL must be a fixed HTTPS origin")
        agent_ca_provisioner_name = os.environ.get("VONK_AGENT_CA_PROVISIONER_NAME", "") if step_ca_enabled else ""
        agent_ca_provisioner_kid = os.environ.get("VONK_AGENT_CA_PROVISIONER_KID", "") if step_ca_enabled else ""
        if step_ca_enabled and (not agent_ca_provisioner_name or not agent_ca_provisioner_kid):
            raise SettingsError("Smallstep provisioner name and key ID are required")
        try:
            agent_ca_timeout_seconds = float(os.environ.get("VONK_AGENT_CA_TIMEOUT_SECONDS", "3"))
            agent_ca_max_response_bytes = int(os.environ.get("VONK_AGENT_CA_MAX_RESPONSE_BYTES", str(64 * 1024)))
        except ValueError as error:
            raise SettingsError("Smallstep timeout and response limit must be numeric") from error
        if not 0 < agent_ca_timeout_seconds <= 30:
            raise SettingsError("Smallstep timeout must be between zero and 30 seconds")
        if not 1024 <= agent_ca_max_response_bytes <= 1024 * 1024:
            raise SettingsError("Smallstep response limit must be between 1024 bytes and one MiB")
        agent_proxy_auth = (
            _agent_proxy_auth_secret("VONK_AGENT_PROXY_AUTH_FILE", production=True)
            if agent_enabled else b""
        )
        worker_api_token = (
            _agent_proxy_auth_secret("VONK_WORKER_API_TOKEN_FILE", production=True)
            if agent_enabled else b""
        )
        agent_artifact_root = _absolute_root(
            "VONK_AGENT_ARTIFACT_ROOT", "/state/agent-artifacts"
        )
        agent_tuf_metadata_root = _absolute_root(
            "VONK_AGENT_TUF_METADATA_ROOT", "/state/agent-tuf/metadata"
        )
        agent_tuf_target_root = _absolute_root(
            "VONK_AGENT_TUF_TARGET_ROOT", "/state/agent-tuf/targets"
        )
        workload_tuf_metadata_root = _absolute_root(
            "VONK_WORKLOAD_TUF_METADATA_ROOT", "/state/workload-tuf/metadata"
        )
        workload_tuf_target_root = _absolute_root(
            "VONK_WORKLOAD_TUF_TARGET_ROOT", "/state/workload-tuf/targets"
        )
        agent_roots = (
            agent_artifact_root,
            agent_tuf_metadata_root,
            agent_tuf_target_root,
            workload_tuf_metadata_root,
            workload_tuf_target_root,
        )
        if any(
            left == right
            or left.is_relative_to(right)
            or right.is_relative_to(left)
            for index, left in enumerate(agent_roots)
            for right in agent_roots[index + 1 :]
        ):
            raise SettingsError(
                "agent artifact and TUF roots must be distinct and nonoverlapping"
            )
        admin_grant_private_key_path = (
            _secret_path("VONK_ADMIN_GRANT_PRIVATE_KEY_FILE")
            if mode == "production"
            or os.environ.get("VONK_ADMIN_GRANT_PRIVATE_KEY_FILE")
            else None
        )
        package_helper_grant_private_key_path = (
            _secret_path("VONK_PACKAGE_HELPER_GRANT_PRIVATE_KEY_FILE")
            if os.environ.get("VONK_PACKAGE_HELPER_GRANT_PRIVATE_KEY_FILE")
            else None
        )
        package_helper_receipt_private_key_path = (
            _secret_path("VONK_PACKAGE_HELPER_RECEIPT_PRIVATE_KEY_FILE")
            if os.environ.get("VONK_PACKAGE_HELPER_RECEIPT_PRIVATE_KEY_FILE")
            else None
        )
        global_catalog_url = os.environ.get(
            "VONK_GLOBAL_CATALOG_URL", "https://vonkforge.ai"
        ).rstrip("/")
        parsed_catalog = urlsplit(global_catalog_url)
        catalog_loopback = parsed_catalog.hostname in {
            "localhost", "127.0.0.1", "::1"
        }
        if (
            (parsed_catalog.scheme != "https" and not (
                parsed_catalog.scheme == "http" and catalog_loopback
            ))
            or not parsed_catalog.hostname
            or parsed_catalog.path not in {"", "/"}
            or parsed_catalog.query
            or parsed_catalog.fragment
            or parsed_catalog.username is not None
            or parsed_catalog.password is not None
        ):
            raise SettingsError("global catalog URL must be a fixed HTTPS origin")
        return cls(
            database_url=database_url,
            repository_path=Path(os.environ.get("VONK_REPOSITORY_PATH", "/srv/vonk-forge/repository")),
            state_path=Path(os.environ.get("VONK_STATE_PATH", "/srv/vonk-forge/state")),
            deployment_mode=mode,
            legacy_direct_transport=legacy_direct_transport,
            token_signing_key=signing_key,
            metrics_token=metrics_token,
            git_signing_key_path=git_signing_key_path,
            admin_grant_private_key_path=admin_grant_private_key_path,
            deployment_branch=deployment_branch,
            required_checks=required_checks,
            agent_ca_provider=agent_ca_provider,
            agent_runtime=agent_runtime,
            agent_client_ca=agent_client_ca,
            agent_intermediate_certificate=agent_intermediate_certificate,
            agent_intermediate_certificate_path=agent_intermediate_certificate_path,
            agent_intermediate_key_path=agent_intermediate_key_path,
            agent_ca_credential_path=agent_ca_credential_path,
            agent_ca_provisioner_public_jwk_path=agent_ca_provisioner_public_jwk_path,
            agent_ca_url=agent_ca_url,
            agent_ca_root_path=agent_ca_root_path,
            agent_ca_provisioner_name=agent_ca_provisioner_name,
            agent_ca_provisioner_kid=agent_ca_provisioner_kid,
            agent_ca_timeout_seconds=agent_ca_timeout_seconds,
            agent_ca_max_response_bytes=agent_ca_max_response_bytes,
            agent_artifact_root=agent_artifact_root,
            agent_tuf_metadata_root=agent_tuf_metadata_root,
            agent_tuf_target_root=agent_tuf_target_root,
            workload_tuf_metadata_root=workload_tuf_metadata_root,
            workload_tuf_target_root=workload_tuf_target_root,
            agent_proxy_auth=agent_proxy_auth,
            worker_api_token=worker_api_token,
            management_cidrs=management_cidrs,
            direct_fabric_cidrs=direct_fabric_cidrs,
            package_helper_grant_private_key_path=package_helper_grant_private_key_path,
            package_helper_receipt_private_key_path=package_helper_receipt_private_key_path,
            workload_signer_socket_path=_absolute_root(
                "VONK_WORKLOAD_SIGNER_SOCKET",
                "/run/vonk-workload-signer/signer.sock",
            ),
            global_catalog_url=global_catalog_url,
        )


@dataclass(frozen=True)
class WorkerSettings:
    """Minimal production-worker settings without repository or API authority."""

    database_url: str
    deployment_mode: str
    internal_api_url: str
    internal_api_token: bytes
    internal_api_timeout_seconds: float
    management_cidrs: str
    direct_fabric_cidrs: str
    update_signer_socket_path: Path

    @classmethod
    def from_env_and_secrets(cls) -> WorkerSettings:
        mode = os.environ.get("VONK_DEPLOYMENT_MODE", "development")
        if mode not in {"development", "test", "production"}:
            raise SettingsError("VONK_DEPLOYMENT_MODE is invalid")
        legacy = os.environ.get("VONK_LEGACY_DIRECT_TRANSPORT", "")
        if legacy not in {"", "explicit-test-only"}:
            raise SettingsError("legacy direct transport selector is invalid")
        if mode == "production" and legacy:
            raise SettingsError("legacy direct transport is forbidden in production")
        database_url = _secret(
            "VONK_DATABASE_URL_FILE",
            production=mode == "production",
        )
        if urlsplit(database_url).scheme not in {
            "postgresql",
            "postgresql+psycopg",
        }:
            raise SettingsError("database URL must use PostgreSQL")
        token = _agent_proxy_auth_secret(
            "VONK_WORKER_API_TOKEN_FILE",
            production=mode == "production",
        )
        origin = os.environ.get(
            "VONK_INTERNAL_API_URL",
            "http://control-api:8000",
        )
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise SettingsError("VONK_INTERNAL_API_URL must be a fixed HTTP origin")
        origin = origin.rstrip("/")
        try:
            timeout = float(os.environ.get("VONK_INTERNAL_API_TIMEOUT_SECONDS", "3"))
        except ValueError as error:
            raise SettingsError("internal API timeout must be numeric") from error
        if not 0 < timeout <= 30:
            raise SettingsError("internal API timeout must be between zero and 30 seconds")
        management_cidrs = _secret_or_file(
            "VONK_MANAGEMENT_CIDRS",
            "VONK_MANAGEMENT_CIDRS_FILE",
        )
        direct_fabric_cidrs = os.environ.get(
            "VONK_DIRECT_FABRIC_CIDRS",
            "",
        ).strip()
        if mode == "production" and not management_cidrs:
            raise SettingsError("VONK_MANAGEMENT_CIDRS is required in production")
        if not management_cidrs and direct_fabric_cidrs:
            raise SettingsError(
                "VONK_MANAGEMENT_CIDRS is required when direct fabric CIDRs are set"
            )
        if management_cidrs:
            try:
                ManagementAddressPolicy.parse(
                    management_cidrs,
                    forbidden_cidrs=direct_fabric_cidrs,
                )
            except PresenceError as error:
                raise SettingsError(str(error)) from error
        return cls(
            database_url=database_url,
            deployment_mode=mode,
            internal_api_url=origin,
            internal_api_token=token,
            internal_api_timeout_seconds=timeout,
            management_cidrs=management_cidrs,
            direct_fabric_cidrs=direct_fabric_cidrs,
            update_signer_socket_path=_absolute_root(
                "VONK_UPDATE_SIGNER_SOCKET", "/run/vonk-signer/signer.sock"
            ),
        )


@dataclass(frozen=True)
class SignerSettings:
    """Filesystem-only settings for the networkless update signer."""

    socket_path: Path
    update_authority_key_path: Path
    admin_grant_public_key_path: Path
    tuf_bootstrap_root_path: Path
    tuf_metadata_root: Path
    tuf_target_root: Path
    tuf_verified_metadata_root: Path
    tuf_verified_target_root: Path
    control_identity_root: Path
    platform_version: str
    platform_release_digest: str
    platform_build_digest: str
    process_image: str

    @classmethod
    def from_env_and_secrets(cls) -> SignerSettings:
        version = os.environ.get("VONK_PLATFORM_VERSION", "")
        release = os.environ.get("VONK_PLATFORM_RELEASE_DIGEST", "")
        build = os.environ.get("VONK_PLATFORM_BUILD_DIGEST", "")
        image = os.environ.get("VONK_CONTROL_PROCESS_IMAGE", "")
        if re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version) is None:
            raise SettingsError("VONK_PLATFORM_VERSION is invalid")
        for name, value in (
            ("VONK_PLATFORM_RELEASE_DIGEST", release),
            ("VONK_PLATFORM_BUILD_DIGEST", build),
        ):
            if re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
                raise SettingsError(f"{name} is invalid")
        if re.fullmatch(r"[^\s]{1,1900}@sha256:[0-9a-f]{64}", image) is None:
            raise SettingsError("VONK_CONTROL_PROCESS_IMAGE is invalid")
        return cls(
            socket_path=_absolute_root(
                "VONK_UPDATE_SIGNER_SOCKET", "/run/vonk-signer/signer.sock"
            ),
            update_authority_key_path=_secret_path(
                "VONK_AGENT_UPDATE_AUTHORITY_KEY_FILE"
            ),
            admin_grant_public_key_path=_secret_path(
                "VONK_ADMIN_GRANT_PUBLIC_KEY_FILE"
            ),
            tuf_bootstrap_root_path=_secret_path(
                "VONK_AGENT_TUF_BOOTSTRAP_ROOT_FILE"
            ),
            tuf_metadata_root=_absolute_root(
                "VONK_AGENT_TUF_METADATA_ROOT", "/state/agent-tuf/metadata"
            ),
            tuf_target_root=_absolute_root(
                "VONK_AGENT_TUF_TARGET_ROOT", "/state/agent-tuf/targets"
            ),
            tuf_verified_metadata_root=_absolute_root(
                "VONK_AGENT_TUF_VERIFIED_METADATA_ROOT",
                "/state/agent-tuf-verifier/metadata",
            ),
            tuf_verified_target_root=_absolute_root(
                "VONK_AGENT_TUF_VERIFIED_TARGET_ROOT",
                "/state/agent-tuf-verifier/targets",
            ),
            control_identity_root=_absolute_root(
                "VONK_CONTROL_IDENTITY_ROOT", "/control-identity"
            ),
            platform_version=version,
            platform_release_digest=release,
            platform_build_digest=build,
            process_image=image,
        )
