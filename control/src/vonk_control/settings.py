"""Strict application configuration loaded from paths and secret files."""

from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .legal_admission import operator_jurisdiction
from .presence import ManagementAddressPolicy, PresenceError


class SettingsError(ValueError):
    pass


_AGENT_PROXY_AUTH_PATTERN = re.compile(rb"[A-Za-z0-9_-]{32,}\Z")
_EPHEMERAL_DEVELOPMENT_TOKEN_SIGNING_KEY = secrets.token_bytes(32)


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
        raise SettingsError(
            f"{name} must contain one base64url-like token of at least 32 characters"
        )
    return normalized


def _absolute_root(name: str, default: str) -> Path:
    value = os.environ.get(name, default)
    path = Path(value)
    if not path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise SettingsError(f"{name} must be an absolute normalized path")
    return path


def _fixed_https_origin(name: str, value: str) -> str:
    if value != value.strip():
        raise SettingsError(f"{name} must be a fixed HTTPS origin")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise SettingsError(f"{name} must be a fixed HTTPS origin") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        and not 1 <= port <= 65535
    ):
        raise SettingsError(f"{name} must be a fixed HTTPS origin")
    return value.rstrip("/")


@dataclass(frozen=True)
class Settings:
    database_url: str
    state_path: Path
    deployment_mode: str
    token_signing_key: bytes
    metrics_token: str
    agent_runtime: str
    agent_controller_origin: str
    agent_enrollment_origin: str
    controller_ca_path: Path | None
    agent_client_ca: bytes
    agent_intermediate_certificate: bytes
    agent_intermediate_certificate_path: Path | None
    agent_ca_credential_path: Path | None
    agent_ca_provisioner_public_jwk_path: Path | None
    agent_ca_url: str
    agent_ca_root_path: Path | None
    agent_ca_provisioner_name: str
    agent_ca_provisioner_kid: str
    agent_ca_timeout_seconds: float
    agent_ca_max_response_bytes: int
    agent_ca_certificate_lifetime_seconds: int
    agent_artifact_root: Path
    workload_tuf_metadata_root: Path
    workload_tuf_target_root: Path
    agent_proxy_auth: bytes
    worker_api_token: bytes
    management_cidrs: str
    direct_fabric_cidrs: str
    operator_jurisdiction: str | None
    package_helper_grant_private_key_path: Path | None = None
    package_helper_receipt_private_key_path: Path | None = None
    host_runtime_grant_private_key_path: Path | None = None
    global_catalog_url: str = "https://vonkforge.ai"
    recipe_library_api_url: str = "https://api.github.com"
    recipe_library_sync_interval_seconds: int = 60
    agent_release_api_url: str = "https://install.vonkforge.ai"
    agent_controller_address: str | None = None
    agent_service_hostnames: tuple[str, ...] = ()
    install_channel: str = "stable"
    artifact_job_storage_max_bytes: int = 16 * 1024**3
    artifact_job_retention_seconds: int = 7 * 24 * 60 * 60

    @property
    def database_host(self) -> str | None:
        return urlsplit(self.database_url).hostname

    @classmethod
    def from_env_and_secrets(cls) -> Settings:
        mode = os.environ.get("VONK_DEPLOYMENT_MODE", "development")
        if mode not in {"development", "test", "production"}:
            raise SettingsError("VONK_DEPLOYMENT_MODE is invalid")
        agent_runtime = os.environ.get(
            "VONK_AGENT_RUNTIME",
            "disabled" if mode == "development" else "enabled",
        )
        if agent_runtime not in {"enabled", "disabled"}:
            raise SettingsError("VONK_AGENT_RUNTIME is invalid")
        agent_enabled = agent_runtime == "enabled" and mode in {
            "development",
            "production",
        }
        database_url = _secret(
            "VONK_DATABASE_URL_FILE", production=mode == "production"
        )
        if urlsplit(database_url).scheme not in {"postgresql", "postgresql+psycopg"}:
            raise SettingsError("database URL must use PostgreSQL")
        management_cidrs = _secret_or_file(
            "VONK_MANAGEMENT_CIDRS",
            "VONK_MANAGEMENT_CIDRS_FILE",
        )
        direct_fabric_cidrs = os.environ.get("VONK_DIRECT_FABRIC_CIDRS", "").strip()
        if (mode == "production" or agent_enabled) and not management_cidrs:
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
                raise SettingsError(
                    "token signing key must be a regular non-symlink file"
                )
            signing_key = signing_path.read_bytes().strip()
        elif mode == "production" or (mode == "development" and agent_enabled):
            raise SettingsError(
                "VONK_TOKEN_SIGNING_KEY_FILE is required when the agent runtime is enabled"
            )
        else:
            signing_key = _EPHEMERAL_DEVELOPMENT_TOKEN_SIGNING_KEY
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
        if len(metrics_token) < 16 or any(
            character.isspace() for character in metrics_token
        ):
            raise SettingsError("metrics token is invalid")
        agent_controller_origin = (
            _fixed_https_origin(
                "VONK_AGENT_CONTROLLER_ORIGIN",
                os.environ.get("VONK_AGENT_CONTROLLER_ORIGIN", ""),
            )
            if agent_enabled
            else ""
        )
        agent_enrollment_origin = (
            _fixed_https_origin(
                "VONK_AGENT_ENROLLMENT_ORIGIN",
                os.environ.get("VONK_AGENT_ENROLLMENT_ORIGIN", ""),
            )
            if agent_enabled
            else ""
        )
        agent_controller_address = (
            os.environ.get("VONK_AGENT_CONTROLLER_ADDRESS", "").strip() or None
            if agent_enabled
            else None
        )
        agent_service_hostnames = (
            tuple(
                value.strip()
                for value in os.environ.get("VONK_AGENT_SERVICE_HOSTNAMES", "").split(
                    ","
                )
                if value.strip()
            )
            if agent_enabled
            else ()
        )
        install_channel = os.environ.get("VONK_INSTALL_CHANNEL", "stable")
        if install_channel not in {"dev", "stable"}:
            raise SettingsError("VONK_INSTALL_CHANNEL is invalid")
        try:
            artifact_job_storage_max_bytes = int(
                os.environ.get("VONK_ARTIFACT_JOB_STORAGE_MAX_BYTES", str(16 * 1024**3))
            )
            artifact_job_retention_seconds = int(
                os.environ.get(
                    "VONK_ARTIFACT_JOB_RETENTION_SECONDS", str(7 * 24 * 60 * 60)
                )
            )
            recipe_library_sync_interval_seconds = int(
                os.environ.get("VONK_RECIPE_LIBRARY_SYNC_INTERVAL_SECONDS", "60")
            )
        except ValueError as error:
            raise SettingsError(
                "artifact job storage settings must be integers"
            ) from error
        if not 1024**3 <= artifact_job_storage_max_bytes <= 1024**4:
            raise SettingsError(
                "artifact job storage maximum must be between 1 GiB and 1 TiB"
            )
        if not 3600 <= artifact_job_retention_seconds <= 365 * 24 * 60 * 60:
            raise SettingsError(
                "artifact job retention must be between one hour and one year"
            )
        if not 60 <= recipe_library_sync_interval_seconds <= 24 * 60 * 60:
            raise SettingsError(
                "recipe library sync interval must be between one minute and one day"
            )
        try:
            configured_jurisdiction = operator_jurisdiction(
                os.environ.get("VONK_OPERATOR_JURISDICTION")
            )
        except ValueError as error:
            raise SettingsError(
                f"VONK_OPERATOR_JURISDICTION is invalid: {error}"
            ) from error
        controller_ca_path = (
            _secret_path("VONK_CONTROLLER_CA_FILE") if agent_enabled else None
        )
        agent_client_ca = (
            _secret("VONK_AGENT_CLIENT_CA_FILE", production=True).encode()
            if agent_enabled
            else b""
        )
        agent_intermediate_certificate_path = (
            _secret_path("VONK_AGENT_INTERMEDIATE_CERTIFICATE_FILE")
            if agent_enabled
            else None
        )
        agent_intermediate_certificate = (
            agent_intermediate_certificate_path.read_bytes()
            if agent_intermediate_certificate_path
            else b""
        )
        step_ca_enabled = agent_enabled
        agent_ca_credential_path = (
            _secret_path("VONK_AGENT_CA_CREDENTIAL_FILE") if step_ca_enabled else None
        )
        agent_ca_provisioner_public_jwk_path = (
            _secret_path("VONK_AGENT_CA_PROVISIONER_PUBLIC_JWK_FILE")
            if step_ca_enabled
            else None
        )
        agent_ca_root_path = (
            _secret_path("VONK_AGENT_CA_ROOT_FILE") if step_ca_enabled else None
        )
        agent_ca_url = (
            os.environ.get("VONK_AGENT_CA_URL", "") if step_ca_enabled else ""
        )
        parsed_ca_url = urlsplit(agent_ca_url)
        if step_ca_enabled and (
            parsed_ca_url.scheme != "https"
            or not parsed_ca_url.hostname
            or parsed_ca_url.path not in {"", "/"}
            or parsed_ca_url.query
            or parsed_ca_url.fragment
            or parsed_ca_url.username is not None
            or parsed_ca_url.password is not None
        ):
            raise SettingsError("VONK_AGENT_CA_URL must be a fixed HTTPS origin")
        agent_ca_provisioner_name = (
            os.environ.get("VONK_AGENT_CA_PROVISIONER_NAME", "")
            if step_ca_enabled
            else ""
        )
        agent_ca_provisioner_kid = (
            os.environ.get("VONK_AGENT_CA_PROVISIONER_KID", "")
            if step_ca_enabled
            else ""
        )
        if step_ca_enabled and (
            not agent_ca_provisioner_name or not agent_ca_provisioner_kid
        ):
            raise SettingsError("Smallstep provisioner name and key ID are required")
        try:
            agent_ca_timeout_seconds = float(
                os.environ.get("VONK_AGENT_CA_TIMEOUT_SECONDS", "3")
            )
            agent_ca_max_response_bytes = int(
                os.environ.get("VONK_AGENT_CA_MAX_RESPONSE_BYTES", str(64 * 1024))
            )
        except ValueError as error:
            raise SettingsError(
                "Smallstep timeout and response limit must be numeric"
            ) from error
        if not 0 < agent_ca_timeout_seconds <= 30:
            raise SettingsError("Smallstep timeout must be between zero and 30 seconds")
        if not 1024 <= agent_ca_max_response_bytes <= 1024 * 1024:
            raise SettingsError(
                "Smallstep response limit must be between 1024 bytes and one MiB"
            )
        if step_ca_enabled:
            try:
                agent_ca_certificate_lifetime_seconds = int(
                    os.environ.get(
                        "VONK_AGENT_CA_CERTIFICATE_LIFETIME_SECONDS", "86400"
                    )
                )
            except ValueError as error:
                raise SettingsError(
                    "Smallstep certificate lifetime must be an integer between 90 and 86400 seconds"
                ) from error
            if not 90 <= agent_ca_certificate_lifetime_seconds <= 86400:
                raise SettingsError(
                    "Smallstep certificate lifetime must be between 90 and 86400 seconds"
                )
        else:
            agent_ca_certificate_lifetime_seconds = 86400
        agent_proxy_auth = (
            _agent_proxy_auth_secret("VONK_AGENT_PROXY_AUTH_FILE", production=True)
            if agent_enabled
            else b""
        )
        worker_api_token = (
            _agent_proxy_auth_secret("VONK_WORKER_API_TOKEN_FILE", production=True)
            if agent_enabled
            else b""
        )
        agent_artifact_root = _absolute_root(
            "VONK_AGENT_ARTIFACT_ROOT", "/state/agent-artifacts"
        )
        workload_tuf_metadata_root = _absolute_root(
            "VONK_WORKLOAD_TUF_METADATA_ROOT", "/state/workload-tuf/metadata"
        )
        workload_tuf_target_root = _absolute_root(
            "VONK_WORKLOAD_TUF_TARGET_ROOT", "/state/workload-tuf/targets"
        )
        agent_roots = (
            agent_artifact_root,
            workload_tuf_metadata_root,
            workload_tuf_target_root,
        )
        if any(
            left == right or left.is_relative_to(right) or right.is_relative_to(left)
            for index, left in enumerate(agent_roots)
            for right in agent_roots[index + 1 :]
        ):
            raise SettingsError(
                "agent artifact and workload TUF roots must be distinct and nonoverlapping"
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
        host_runtime_grant_private_key_path = (
            _secret_path("VONK_HOST_RUNTIME_GRANT_PRIVATE_KEY_FILE")
            if os.environ.get("VONK_HOST_RUNTIME_GRANT_PRIVATE_KEY_FILE")
            else None
        )
        global_catalog_url = os.environ.get(
            "VONK_GLOBAL_CATALOG_URL", "https://vonkforge.ai"
        ).rstrip("/")
        parsed_catalog = urlsplit(global_catalog_url)
        catalog_loopback = parsed_catalog.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            (
                parsed_catalog.scheme != "https"
                and not (parsed_catalog.scheme == "http" and catalog_loopback)
            )
            or not parsed_catalog.hostname
            or parsed_catalog.path not in {"", "/"}
            or parsed_catalog.query
            or parsed_catalog.fragment
            or parsed_catalog.username is not None
            or parsed_catalog.password is not None
        ):
            raise SettingsError("global catalog URL must be a fixed HTTPS origin")
        recipe_library_api_url = os.environ.get(
            "VONK_RECIPE_LIBRARY_API_URL", "https://api.github.com"
        ).rstrip("/")
        if recipe_library_api_url not in {
            "https://api.github.com",
            "http://caddy:8083",
        }:
            raise SettingsError(
                "recipe library API URL must be GitHub or the fixed internal relay"
            )
        agent_release_api_url = os.environ.get(
            "VONK_AGENT_RELEASE_API_URL", "https://install.vonkforge.ai"
        ).rstrip("/")
        if agent_release_api_url not in {
            "https://install.vonkforge.ai",
            "http://caddy:8084",
        }:
            raise SettingsError(
                "agent release API URL must be the public origin or fixed internal relay"
            )
        return cls(
            database_url=database_url,
            state_path=Path(os.environ.get("VONK_STATE_PATH", "/srv/vonk-forge/state")),
            deployment_mode=mode,
            token_signing_key=signing_key,
            metrics_token=metrics_token,
            agent_runtime=agent_runtime,
            agent_controller_origin=agent_controller_origin,
            agent_enrollment_origin=agent_enrollment_origin,
            controller_ca_path=controller_ca_path,
            agent_client_ca=agent_client_ca,
            agent_intermediate_certificate=agent_intermediate_certificate,
            agent_intermediate_certificate_path=agent_intermediate_certificate_path,
            agent_ca_credential_path=agent_ca_credential_path,
            agent_ca_provisioner_public_jwk_path=agent_ca_provisioner_public_jwk_path,
            agent_ca_url=agent_ca_url,
            agent_ca_root_path=agent_ca_root_path,
            agent_ca_provisioner_name=agent_ca_provisioner_name,
            agent_ca_provisioner_kid=agent_ca_provisioner_kid,
            agent_ca_timeout_seconds=agent_ca_timeout_seconds,
            agent_ca_max_response_bytes=agent_ca_max_response_bytes,
            agent_ca_certificate_lifetime_seconds=agent_ca_certificate_lifetime_seconds,
            agent_artifact_root=agent_artifact_root,
            workload_tuf_metadata_root=workload_tuf_metadata_root,
            workload_tuf_target_root=workload_tuf_target_root,
            agent_proxy_auth=agent_proxy_auth,
            worker_api_token=worker_api_token,
            management_cidrs=management_cidrs,
            direct_fabric_cidrs=direct_fabric_cidrs,
            operator_jurisdiction=configured_jurisdiction,
            package_helper_grant_private_key_path=package_helper_grant_private_key_path,
            package_helper_receipt_private_key_path=package_helper_receipt_private_key_path,
            host_runtime_grant_private_key_path=host_runtime_grant_private_key_path,
            global_catalog_url=global_catalog_url,
            recipe_library_api_url=recipe_library_api_url,
            recipe_library_sync_interval_seconds=recipe_library_sync_interval_seconds,
            agent_release_api_url=agent_release_api_url,
            agent_controller_address=agent_controller_address,
            agent_service_hostnames=agent_service_hostnames,
            install_channel=install_channel,
            artifact_job_storage_max_bytes=artifact_job_storage_max_bytes,
            artifact_job_retention_seconds=artifact_job_retention_seconds,
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
    operator_jurisdiction: str | None
    state_path: Path
    artifact_job_storage_max_bytes: int
    artifact_job_retention_seconds: int
    artifact_job_reconcile_interval_seconds: int
    artifact_job_reconcile_batch_limit: int

    @classmethod
    def from_env_and_secrets(cls) -> WorkerSettings:
        mode = os.environ.get("VONK_DEPLOYMENT_MODE", "development")
        if mode not in {"development", "test", "production"}:
            raise SettingsError("VONK_DEPLOYMENT_MODE is invalid")
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
            raise SettingsError(
                "internal API timeout must be between zero and 30 seconds"
            )
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
        try:
            configured_jurisdiction = operator_jurisdiction(
                os.environ.get("VONK_OPERATOR_JURISDICTION")
            )
        except ValueError as error:
            raise SettingsError(
                f"VONK_OPERATOR_JURISDICTION is invalid: {error}"
            ) from error
        try:
            artifact_job_storage_max_bytes = int(
                os.environ.get("VONK_ARTIFACT_JOB_STORAGE_MAX_BYTES", str(16 * 1024**3))
            )
            artifact_job_retention_seconds = int(
                os.environ.get(
                    "VONK_ARTIFACT_JOB_RETENTION_SECONDS", str(7 * 24 * 60 * 60)
                )
            )
            artifact_job_reconcile_interval_seconds = int(
                os.environ.get("VONK_ARTIFACT_JOB_RECONCILE_INTERVAL_SECONDS", "3600")
            )
            artifact_job_reconcile_batch_limit = int(
                os.environ.get("VONK_ARTIFACT_JOB_RECONCILE_BATCH_LIMIT", "1000")
            )
        except ValueError as error:
            raise SettingsError(
                "artifact job worker settings must be integers"
            ) from error
        if not 1024**3 <= artifact_job_storage_max_bytes <= 1024**4:
            raise SettingsError(
                "artifact job storage maximum must be between 1 GiB and 1 TiB"
            )
        if not 3600 <= artifact_job_retention_seconds <= 365 * 24 * 60 * 60:
            raise SettingsError(
                "artifact job retention must be between one hour and one year"
            )
        if not 60 <= artifact_job_reconcile_interval_seconds <= 7 * 24 * 60 * 60:
            raise SettingsError(
                "artifact job reconciliation interval must be between one minute and one week"
            )
        if not 1 <= artifact_job_reconcile_batch_limit <= 10000:
            raise SettingsError(
                "artifact job reconciliation batch limit must be between 1 and 10000"
            )
        return cls(
            database_url=database_url,
            deployment_mode=mode,
            internal_api_url=origin,
            internal_api_token=token,
            internal_api_timeout_seconds=timeout,
            management_cidrs=management_cidrs,
            direct_fabric_cidrs=direct_fabric_cidrs,
            operator_jurisdiction=configured_jurisdiction,
            state_path=_absolute_root("VONK_STATE_PATH", "/srv/vonk-forge/state"),
            artifact_job_storage_max_bytes=artifact_job_storage_max_bytes,
            artifact_job_retention_seconds=artifact_job_retention_seconds,
            artifact_job_reconcile_interval_seconds=(
                artifact_job_reconcile_interval_seconds
            ),
            artifact_job_reconcile_batch_limit=artifact_job_reconcile_batch_limit,
        )
