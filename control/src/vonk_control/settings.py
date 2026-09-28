"""Controller configuration: a few operator choices, everything else constant.

The API and the worker share one environment block and one ``Settings``
loader. Only values an operator genuinely chooses come from the environment;
file locations, relay origins and tuning budgets are fixed constants. Values
that can be repaired are repaired with a logged warning instead of refusing to
start. Security material still fails closed, but API-only secrets are read
lazily so the worker never touches them.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import secrets
import stat
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from urllib.parse import urlsplit

_LOGGER = logging.getLogger(__name__)


class SettingsError(ValueError):
    pass


# Fixed file locations inside the Controller containers.
SECRETS_ROOT = Path("/run/vonk-normalized-secrets")
STATE_ROOT = Path("/state")
AGENT_ARTIFACT_ROOT = Path("/state/agent-artifacts")
MODEL_CACHE_ROOT = Path("/state/model-cache")

# Fixed in-project relays and the private certificate authority.
RECIPE_LIBRARY_API_URL = "http://caddy:8083"
RECIPE_LIBRARY_ASSET_URL = "http://caddy:8085"
AGENT_RELEASE_API_URL = "http://caddy:8084"
AGENT_CA_URL = "https://step-ca:9000"
AGENT_CA_PROVISIONER_NAME = "vonk-forge-agent"
DEFAULT_AGENT_CERTIFICATE_LIFETIME_SECONDS = 30 * 24 * 60 * 60

# Tuning budgets.
ARTIFACT_JOB_STORAGE_MAX_BYTES = 16 * 1024**3
ARTIFACT_JOB_RETENTION_SECONDS = 7 * 24 * 60 * 60
ARTIFACT_JOB_RECONCILE_INTERVAL_SECONDS = 3600
ARTIFACT_JOB_RECONCILE_BATCH_LIMIT = 1000
MODEL_CACHE_RESERVE_BYTES = 10 * 1024**3
MODEL_CACHE_PARALLEL_DOWNLOADS = 8
RECIPE_IMAGE_PARALLEL_PREPARATIONS = 4
RECIPE_BUILD_PARALLEL_PREPARATIONS = 2
RECIPE_LIBRARY_SYNC_INTERVAL_SECONDS = 900
DISTRIBUTED_START_TIMEOUT_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class DatabaseWaitBudgets:
    """Every finite upper bound on a PostgreSQL wait, in one place.

    The engine applies the connection and pool budgets per connection. The
    admission budget narrows ``lock_timeout`` inside admission transactions so
    implicit foreign-key and unique-index waits are rescheduled promptly.
    Invariants: admission lock <= lock <= statement <= transaction, and
    idle-in-transaction <= transaction.
    """

    lock_timeout_ms: int = 30_000
    admission_lock_timeout_ms: int = 750
    statement_timeout_ms: int = 120_000
    transaction_timeout_ms: int = 300_000
    idle_in_transaction_timeout_ms: int = 60_000
    pool_size: int = 5
    max_overflow: int = 10
    pool_timeout_seconds: float = 30.0


DATABASE_WAIT_BUDGETS = DatabaseWaitBudgets()

_AGENT_PROXY_AUTH_PATTERN = re.compile(rb"[A-Za-z0-9_-]{32,}\Z")
_RELEASE_TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+\Z")
_HOSTNAME = re.compile(
    r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\Z"
)
_GO_DURATION_PART = re.compile(r"([0-9]+)(h|m|s)")
_DURATION_UNITS = {"h": 3600, "m": 60, "s": 1}
_EPHEMERAL_DEVELOPMENT_TOKEN_SIGNING_KEY = secrets.token_bytes(32)

type _Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def _read_secret(path: Path) -> bytes:
    """Read one regular, non-symlink secret file or fail closed."""
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise SettingsError(f"secret {path.name} is missing") from error
    except OSError as error:
        raise SettingsError(f"secret {path.name} is unreadable") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise SettingsError(f"secret {path.name} must be a regular non-symlink file")
    try:
        return path.read_bytes()
    except OSError as error:
        raise SettingsError(f"secret {path.name} is unreadable") from error


def _regular_file(path: Path) -> Path | None:
    """Return a present, non-empty, regular file path without reading it."""
    try:
        metadata = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size == 0:
        return None
    return path


def _parse_networks(raw: str, label: str) -> list[_Network]:
    """Parse CIDRs leniently: strip host bits, drop junk and duplicates."""
    networks: list[_Network] = []
    for item in raw.replace(",", " ").split():
        try:
            network = ipaddress.ip_network(item, strict=False)
        except ValueError:
            _LOGGER.warning("ignoring unparseable %s entry %r", label, item)
            continue
        if str(network) != item:
            _LOGGER.warning("normalized %s entry %s to %s", label, item, network)
        if network not in networks:
            networks.append(network)
    return networks


def _overlaps(left: _Network, right: _Network) -> bool:
    if isinstance(left, ipaddress.IPv4Network) and isinstance(
        right, ipaddress.IPv4Network
    ):
        return left.overlaps(right)
    if isinstance(left, ipaddress.IPv6Network) and isinstance(
        right, ipaddress.IPv6Network
    ):
        return left.overlaps(right)
    return False


def _network_for_address(address: str | None) -> _Network | None:
    if address is None:
        return None
    parsed = ipaddress.ip_address(address)
    prefix = 24 if parsed.version == 4 else 64
    return ipaddress.ip_network(f"{parsed}/{prefix}", strict=False)


def _normalize_networks(
    management_raw: str, fabric_raw: str, nas_lan_ip: str | None
) -> tuple[str, str]:
    management = _parse_networks(management_raw, "management CIDR")
    if not management:
        default = _network_for_address(nas_lan_ip)
        if default is not None:
            _LOGGER.warning(
                "no management CIDRs configured; using %s around the NAS LAN IP",
                default,
            )
            management = [default]
    fabric: list[_Network] = []
    for network in _parse_networks(fabric_raw, "direct fabric CIDR"):
        # Management and fabric networks must be disjoint. An overlapping
        # fabric entry is the ambiguous one: the operator explicitly trusts the
        # management range, so the fabric entry is ignored rather than refusing
        # to start.
        if any(_overlaps(network, allowed) for allowed in management):
            _LOGGER.warning(
                "ignoring direct fabric CIDR %s: it overlaps a management CIDR",
                network,
            )
            continue
        fabric.append(network)
    return (
        ",".join(str(network) for network in management),
        ",".join(str(network) for network in fabric),
    )


def _nas_lan_ip(raw: str) -> str | None:
    value = raw.strip()
    if not value:
        return None
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        _LOGGER.warning("ignoring invalid VONK_NAS_LAN_IP %r", value)
        return None
    if address.is_unspecified or address.is_multicast:
        _LOGGER.warning("ignoring unusable VONK_NAS_LAN_IP %s", address)
        return None
    return str(address)


def _control_hostname(raw: str) -> str:
    hostname = raw.strip().lower().rstrip(".")
    if hostname and _HOSTNAME.fullmatch(hostname) is None:
        raise SettingsError("VONK_CONTROL_HOSTNAME must be a DNS hostname")
    return hostname


def parse_go_duration_seconds(value: object) -> int | None:
    """Parse the step-ca duration subset used for certificate claims."""
    if not isinstance(value, str) or not value:
        return None
    total = 0
    position = 0
    for match in _GO_DURATION_PART.finditer(value):
        if match.start() != position:
            return None
        total += int(match.group(1)) * _DURATION_UNITS[match.group(2)]
        position = match.end()
    return total if position == len(value) else None


def certificate_lifetime_from_step_ca_config(path: Path) -> int:
    """Read the agent provisioner's default TLS duration; fall back to 30 days."""
    try:
        config = json.loads(path.read_bytes())
        provisioners = config["authority"]["provisioners"]
        provisioner = next(
            item
            for item in provisioners
            if isinstance(item, dict) and item.get("name") == AGENT_CA_PROVISIONER_NAME
        )
        seconds = parse_go_duration_seconds(
            provisioner["claims"]["defaultTLSCertDuration"]
        )
    except (OSError, ValueError, KeyError, TypeError, StopIteration):
        seconds = None
    if (
        seconds is None
        or not 90 <= seconds <= DEFAULT_AGENT_CERTIFICATE_LIFETIME_SECONDS
    ):
        _LOGGER.warning(
            "agent certificate lifetime unavailable from step-ca config; using %s s",
            DEFAULT_AGENT_CERTIFICATE_LIFETIME_SECONDS,
        )
        return DEFAULT_AGENT_CERTIFICATE_LIFETIME_SECONDS
    return seconds


@dataclass(frozen=True)
class Settings:
    """The Controller configuration shared by the API and the worker."""

    database_url: str
    deployment_mode: str = "production"
    control_hostname: str = ""
    nas_lan_ip: str | None = None
    management_cidrs: str = ""
    direct_fabric_cidrs: str = ""
    install_channel: str = "stable"
    recipe_library_release: str = "latest"
    secrets_root: Path = SECRETS_ROOT
    state_path: Path = STATE_ROOT
    agent_artifact_root: Path = AGENT_ARTIFACT_ROOT
    model_cache_root: Path = MODEL_CACHE_ROOT

    @classmethod
    def from_env_and_secrets(cls) -> Settings:
        mode = os.environ.get("VONK_DEPLOYMENT_MODE", "production").strip()
        if mode not in {"development", "test", "production"}:
            _LOGGER.warning("unknown VONK_DEPLOYMENT_MODE %r; using production", mode)
            mode = "production"
        database_url = _read_secret(SECRETS_ROOT / "database-url").decode().strip()
        if urlsplit(database_url).scheme not in {"postgresql", "postgresql+psycopg"}:
            raise SettingsError("database URL must use PostgreSQL")
        nas_lan_ip = _nas_lan_ip(os.environ.get("VONK_NAS_LAN_IP", ""))
        management_cidrs, direct_fabric_cidrs = _normalize_networks(
            os.environ.get("VONK_MANAGEMENT_CIDRS", ""),
            os.environ.get("VONK_DIRECT_FABRIC_CIDRS", ""),
            nas_lan_ip,
        )
        control_hostname = _control_hostname(
            os.environ.get("VONK_CONTROL_HOSTNAME", "")
        )
        if mode == "production":
            if not control_hostname:
                raise SettingsError("VONK_CONTROL_HOSTNAME is required in production")
            if not management_cidrs:
                raise SettingsError(
                    "VONK_MANAGEMENT_CIDRS or VONK_NAS_LAN_IP is required in production"
                )
        install_channel = os.environ.get("VONK_INSTALL_CHANNEL", "").strip()
        if install_channel not in {"dev", "stable"}:
            if install_channel:
                _LOGGER.warning(
                    "unknown VONK_INSTALL_CHANNEL %r; using stable", install_channel
                )
            install_channel = "stable"
        release = os.environ.get("VONK_RECIPE_LIBRARY_RELEASE", "").strip()
        if release != "latest" and _RELEASE_TAG.fullmatch(release) is None:
            if release:
                _LOGGER.warning(
                    "invalid VONK_RECIPE_LIBRARY_RELEASE %r; using latest", release
                )
            release = "latest"
        return cls(
            database_url=database_url,
            deployment_mode=mode,
            control_hostname=control_hostname,
            nas_lan_ip=nas_lan_ip,
            management_cidrs=management_cidrs,
            direct_fabric_cidrs=direct_fabric_cidrs,
            install_channel=install_channel,
            recipe_library_release=release,
            secrets_root=SECRETS_ROOT,
        )

    @property
    def database_host(self) -> str | None:
        return urlsplit(self.database_url).hostname

    @property
    def agent_runtime_enabled(self) -> bool:
        return self.deployment_mode == "production"

    # Hostnames and origins derived from the one control hostname.

    @property
    def agent_service_hostnames(self) -> tuple[str, ...]:
        host = self.control_hostname
        return (host, f"enroll.{host}", f"agents.{host}", f"registry.{host}")

    @property
    def agent_controller_origin(self) -> str:
        return f"https://agents.{self.control_hostname}:8443"

    @property
    def agent_enrollment_origin(self) -> str:
        return f"https://enroll.{self.control_hostname}:8443"

    # Secret file locations (read by their consumers).

    @property
    def controller_ca_path(self) -> Path:
        return self.secrets_root / "controller-ca"

    @property
    def agent_intermediate_certificate_path(self) -> Path:
        return self.secrets_root / "agent-intermediate-certificate"

    @property
    def agent_ca_credential_path(self) -> Path:
        return self.secrets_root / "agent-ca-credential"

    @property
    def agent_ca_provisioner_public_jwk_path(self) -> Path:
        return self.secrets_root / "agent-ca-provisioner-public-jwk"

    @property
    def agent_ca_root_path(self) -> Path:
        return self.secrets_root / "step-ca-root-certificate"

    @property
    def host_runtime_grant_private_key_path(self) -> Path | None:
        return _regular_file(self.secrets_root / "host-runtime-grant-private-key")

    @property
    def huggingface_token_path(self) -> Path | None:
        # Optional: absent or empty keeps public model downloads anonymous.
        return _regular_file(self.secrets_root / "hf-token")

    # API-only secret material, read and validated on first use.

    @cached_property
    def token_signing_key(self) -> bytes:
        path = self.secrets_root / "token-signing-key"
        if self.deployment_mode != "production" and not path.exists():
            return _EPHEMERAL_DEVELOPMENT_TOKEN_SIGNING_KEY
        key = _read_secret(path).strip()
        if len(key) < 32:
            raise SettingsError("token signing key must contain at least 32 bytes")
        return key

    @cached_property
    def metrics_token(self) -> str:
        path = self.secrets_root / "metrics-token"
        if self.deployment_mode != "production" and not path.exists():
            return "development-metrics-token"
        token = _read_secret(path).decode(errors="replace").strip()
        if len(token) < 16 or any(character.isspace() for character in token):
            raise SettingsError("metrics token is invalid")
        return token

    @cached_property
    def agent_proxy_auth(self) -> bytes:
        if not self.agent_runtime_enabled:
            return b""
        value = _read_secret(self.secrets_root / "agent-proxy-auth").rstrip(b"\r\n")
        if _AGENT_PROXY_AUTH_PATTERN.fullmatch(value) is None:
            raise SettingsError(
                "agent proxy auth must contain one base64url-like token of at "
                "least 32 characters"
            )
        return value

    @cached_property
    def agent_ca_provisioner_kid(self) -> str:
        raw = _read_secret(self.agent_ca_provisioner_public_jwk_path)
        try:
            kid = json.loads(raw)["kid"]
        except (ValueError, KeyError, TypeError) as error:
            raise SettingsError("agent CA provisioner public JWK has no kid") from error
        if not isinstance(kid, str) or not kid:
            raise SettingsError("agent CA provisioner public JWK has no kid")
        return kid

    @cached_property
    def agent_ca_certificate_lifetime_seconds(self) -> int:
        return certificate_lifetime_from_step_ca_config(
            self.secrets_root / "step-ca" / "ca.json"
        )
