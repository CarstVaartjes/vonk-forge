#!/usr/bin/env python3
from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from tests.acceptance.runtime import (
    AcceptanceError,
    _compose_rows,
    assert_bundle_contract,
    assert_compose_compatibility,
    assert_compose_services_healthy,
    bootstrap_command,
    https_over_command,
    run_interactive,
)

DEFAULT_SERVICES = {
    "caddy",
    "control-api",
    "control-worker",
    "grafana",
    "litellm",
    "postgres",
    "prometheus",
    "registry",
    "step-ca",
    "tailscale-configurator",
    "tailscale-gateway",
}
HERMES_SERVICES = DEFAULT_SERVICES | {"hermes-agent"}
SAFE_URL = re.compile(r"https://[A-Za-z0-9._~:/-]+\Z")
SAFE_DNS_SUFFIX = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\Z"
)
TAILSCALE_SERVICE = re.compile(
    r"svc:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)
TAILSCALE_HOSTNAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
PINNED_IMAGE = re.compile(
    r"[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}\Z"
)
MUTABLE_IMAGE_TAG = re.compile(r":(?:latest|dev|main|edge)@sha256:")


def required_environment(name: str, *, secret: bool = False) -> str:
    value = os.environ.get(name, "")
    if not value or "\0" in value or "\n" in value or "\r" in value:
        label = "acceptance secret" if secret else "acceptance input"
        raise AcceptanceError(f"{label} {name} is missing or invalid")
    return value


def require_tailnet_client() -> bool:
    value = os.environ.get("VONK_ACCEPTANCE_REQUIRE_TAILNET_CLIENT", "true")
    if value not in {"true", "false"}:
        raise AcceptanceError(
            "VONK_ACCEPTANCE_REQUIRE_TAILNET_CLIENT must be true or false"
        )
    return value == "true"


def host_ipv4() -> str:
    configured = os.environ.get("VONK_ACCEPTANCE_NAS_IP")
    if configured:
        address = ipaddress.ip_address(configured)
        if address.version != 4 or address.is_unspecified or address.is_multicast:
            raise AcceptanceError("VONK_ACCEPTANCE_NAS_IP is invalid")
        return str(address)
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("1.1.1.1", 80))
        address = probe.getsockname()[0]
    finally:
        probe.close()
    parsed = ipaddress.ip_address(address)
    if parsed.version != 4 or parsed.is_unspecified or parsed.is_loopback:
        raise AcceptanceError("the runner has no bindable IPv4 address")
    return str(parsed)


def nas_bind_ipv4(default: str) -> str:
    configured = os.environ.get("VONK_ACCEPTANCE_NAS_BIND_IP")
    if configured is None:
        configured = (
            "0.0.0.0"
            if os.environ.get("DOCKER_HOST", "").startswith("tcp://")
            else default
        )
    try:
        address = ipaddress.ip_address(configured)
    except ValueError as error:
        raise AcceptanceError("VONK_ACCEPTANCE_NAS_BIND_IP is invalid") from error
    if address.version != 4 or address.is_multicast:
        raise AcceptanceError("VONK_ACCEPTANCE_NAS_BIND_IP is invalid")
    return str(address)


def command_environment(root: Path) -> dict[str, str]:
    root.mkdir(mode=0o700)
    commands = root / "commands"
    commands.mkdir(mode=0o700)
    for name in ("awk", "chmod", "curl", "mktemp", "rm", "sha256sum", "sh", "uname"):
        source = shutil.which(name)
        if source is None:
            raise AcceptanceError(f"workstation command {name} is unavailable")
        (commands / name).symlink_to(source)
    home = root / "home"
    temporary = root / "tmp"
    home.mkdir(mode=0o700)
    temporary.mkdir(mode=0o700)
    return {
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": str(commands),
        "TMPDIR": str(temporary),
    }


def nas_responses(
    *,
    nas_ip: str,
    tailnet_suffix: str,
    oauth_client_id: str,
    oauth_client_secret: str,
    upstream_key: str,
    hermes: bool,
    control_service: str = "svc:vonk-forge",
    hermes_dashboard_service: str = "svc:hermes-dashboard",
) -> list[tuple[str, str]]:
    control_hostname = tailscale_service_hostname(control_service, tailnet_suffix)
    hermes_dashboard_hostname = tailscale_service_hostname(
        hermes_dashboard_service, tailnet_suffix
    )
    hostnames = {
        "Control hostname": control_hostname,
        "Agent enrollment hostname": f"enroll.acceptance.{tailnet_suffix}",
        "Agent controller hostname": f"agents.acceptance.{tailnet_suffix}",
        "Registry hostname": f"registry.acceptance.{tailnet_suffix}",
    }
    responses = [
        ("Reserved NAS LAN IP: ", nas_ip),
        ("Trusted Spark management CIDRs: ", "0.0.0.0/0"),
        ("Direct GPU fabric CIDRs []: ", ""),
        *((f"{label}: ", value) for label, value in hostnames.items()),
        ("Vonk Forge administrator password (leave blank to generate): ", ""),
        ("Tailscale OAuth client ID: ", oauth_client_id),
        ("Tailscale OAuth client secret: ", oauth_client_secret),
        ("LiteLLM upstream provider API key: ", upstream_key),
    ]
    for label in (
        "PostgreSQL control password",
        "LiteLLM database password",
        "Controller token-signing key",
        "Prometheus metrics token",
        "LiteLLM administrator key",
        "Grafana administrator password",
        "Internal agent proxy token",
        "Internal worker API token",
        "Hermes API key",
    ):
        responses.append((f"{label} (leave blank to generate): ", ""))
    for label in (
        "Workload package grant Ed25519 key",
        "Workload receipt Ed25519 key",
        "Host runtime grant Ed25519 key",
    ):
        responses.append(
            (f"{label} (existing PEM path; leave blank to generate): ", "")
        )
    responses.extend(
        (
            (
                "Step CA/controller PKI (existing bundle secrets directory; leave blank to generate): ",
                "",
            ),
            ("Enable the optional Hermes agent? [y/N]: ", "y" if hermes else "n"),
        )
    )
    if hermes:
        responses.append(
            (
                "Hermes dashboard HTTPS origin: ",
                f"https://{hermes_dashboard_hostname}",
            )
        )
    return responses


def tailscale_service_hostname(service: str, tailnet_suffix: str) -> str:
    if TAILSCALE_SERVICE.fullmatch(service) is None:
        raise AcceptanceError("acceptance Tailscale Service name is invalid")
    if SAFE_DNS_SUFFIX.fullmatch(tailnet_suffix) is None:
        raise AcceptanceError("acceptance tailnet DNS suffix is invalid")
    return f"{service.removeprefix('svc:')}.{tailnet_suffix}"


def configure_tailnet_service_names(
    bundle: Path,
    *,
    control: str,
    gateway_hostname: str,
    hermes_api: str,
    hermes_dashboard: str,
) -> None:
    services = {
        "VONK_TAILSCALE_CONTROL_SERVICE": control,
        "VONK_TAILSCALE_HERMES_API_SERVICE": hermes_api,
        "VONK_TAILSCALE_HERMES_DASHBOARD_SERVICE": hermes_dashboard,
    }
    if len(set(services.values())) != len(services) or any(
        TAILSCALE_SERVICE.fullmatch(value) is None for value in services.values()
    ):
        raise AcceptanceError("acceptance Tailscale Service names are invalid")
    if TAILSCALE_HOSTNAME.fullmatch(gateway_hostname) is None:
        raise AcceptanceError("acceptance Tailscale gateway hostname is invalid")
    settings = services | {
        "VONK_TAILSCALE_GATEWAY_HOSTNAME": gateway_hostname,
    }
    environment = bundle / ".env"
    try:
        metadata = environment.lstat()
        source = environment.read_text(encoding="utf-8")
    except OSError as error:
        raise AcceptanceError("bundle environment is unavailable") from error
    if environment.is_symlink() or not environment.is_file() or metadata.st_nlink != 1:
        raise AcceptanceError("bundle environment is unsafe")
    if any(f"{name}=" in source for name in settings):
        raise AcceptanceError("bundle already selects Tailscale acceptance settings")
    suffix = "" if source.endswith("\n") else "\n"
    environment.write_text(
        source
        + suffix
        + "".join(f"{name}={value}\n" for name, value in settings.items()),
        encoding="utf-8",
    )
    os.chmod(environment, metadata.st_mode & 0o777)


def generate_bundle(
    root: Path,
    *,
    candidate_url: str,
    child_environment: dict[str, str],
    responses: list[tuple[str, str]],
    require_all_prompts: bool = True,
) -> Path:
    try:
        root.mkdir(mode=0o700)
    except FileExistsError as error:
        if root.is_symlink() or not root.is_dir():
            raise AcceptanceError("NAS acceptance target is unsafe") from error
    run_interactive(
        bootstrap_command(candidate_url),
        cwd=root,
        environment=child_environment,
        responses=responses,
        timeout=180,
        require_all_prompts=require_all_prompts,
    )
    bundle = root / "vonk-forge"
    assert_bundle_contract(bundle)
    return bundle


def is_immutable_image(image: str) -> bool:
    return (
        PINNED_IMAGE.fullmatch(image) is not None
        and MUTABLE_IMAGE_TAG.search(image) is None
    )


def run(
    command: list[str],
    *,
    cwd: Path,
    timeout: int = 300,
    allow_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=host_command_environment(),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise AcceptanceError(
            f"command failed ({' '.join(command)}):\n{result.stdout[-4000:]}\n{result.stderr[-4000:]}"
        )
    if "WARN[" in result.stderr or "level=warning" in result.stderr.lower():
        raise AcceptanceError(f"Compose emitted a warning:\n{result.stderr[-4000:]}")
    if not allow_output and (result.stdout or result.stderr):
        raise AcceptanceError(f"command emitted unexpected output: {' '.join(command)}")
    return result


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
    bundle: Path, command: list[str]
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            command,
            cwd=bundle,
            env=host_command_environment(),
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def compose_startup_diagnostics(bundle: Path) -> str:
    status = _diagnostic_command(
        bundle, [*reference_compose(), "ps", "--all", "--format", "json"]
    )
    if status is None:
        return "NAS Compose diagnostics unavailable"
    if status.returncode != 0:
        output = _redact_diagnostics(status.stderr or status.stdout)
        return f"NAS Compose status unavailable: {output or 'no output'}"
    try:
        rows = _compose_rows(status.stdout)
    except AcceptanceError:
        output = _redact_diagnostics(status.stdout)
        return f"NAS Compose status invalid: {output or 'no output'}"
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
    logs = _diagnostic_command(
        bundle,
        [*reference_compose(), "logs", "--no-color", "--tail", "80", *broken],
    )
    if logs is None:
        return f"{details}; logs unavailable"
    output = _redact_diagnostics(logs.stdout or logs.stderr)
    return f"{details}; failing service logs:\n{output or 'no output'}"


def compose_compatibility_fixtures() -> list[tuple[str, Path]]:
    fixtures = []
    for name, variable in (
        ("ugreen-docker-29.4.3-compose-5.1.3", "VONK_ACCEPTANCE_COMPOSE_UGREEN"),
        ("lower-compose-2.24.6", "VONK_ACCEPTANCE_COMPOSE_LOWER"),
    ):
        path = Path(required_environment(variable))
        if not path.is_absolute():
            raise AcceptanceError(f"Compose fixture {name!r} must use an absolute path")
        fixtures.append((name, path))
    return fixtures


def host_command_environment() -> dict[str, str]:
    environment = {
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    for name in (
        "DOCKER_CERT_PATH",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "DOCKER_TLS_VERIFY",
    ):
        if value := os.environ.get(name):
            environment[name] = value
    return environment


def reference_compose() -> list[str]:
    executable = Path(required_environment("VONK_ACCEPTANCE_REFERENCE_COMPOSE"))
    if (
        not executable.is_absolute()
        or not executable.is_file()
        or not os.access(executable, os.X_OK)
    ):
        raise AcceptanceError("reference Compose fixture is unavailable")
    return [str(executable)]


def parsed_environment(bundle: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in (bundle / ".env").read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or name in result:
            raise AcceptanceError("generated .env is invalid")
        result[name] = value
    return result


def assert_repeatable(first: Path, second: Path) -> None:
    if (first / "docker-compose.yaml").read_bytes() != (
        second / "docker-compose.yaml"
    ).read_bytes():
        raise AcceptanceError("two clean NAS runs produced different Compose models")
    first_env = parsed_environment(first)
    second_env = parsed_environment(second)
    if set(first_env) != set(second_env):
        raise AcceptanceError("two clean NAS runs produced different .env keys")
    for name in set(first_env) - {"AGENT_CA_PROVISIONER_KID"}:
        if first_env[name] != second_env[name]:
            raise AcceptanceError(
                f"non-secret generated input {name} is not repeatable"
            )
    first_secrets = {
        path.relative_to(first / "secrets")
        for path in (first / "secrets").rglob("*")
        if path.is_file()
    }
    second_secrets = {
        path.relative_to(second / "secrets")
        for path in (second / "secrets").rglob("*")
        if path.is_file()
    }
    if first_secrets != second_secrets:
        raise AcceptanceError("two clean NAS runs produced different secret contracts")


def secret_snapshot(bundle: Path) -> dict[Path, bytes]:
    secrets = bundle / "secrets"
    return {
        path.relative_to(secrets): path.read_bytes()
        for path in secrets.rglob("*")
        if path.is_file() and path.relative_to(secrets).parts[0] != "runtime-configs"
    }


def assert_site_secrets_preserved(bundle: Path, before: dict[Path, bytes]) -> None:
    if secret_snapshot(bundle) != before:
        raise AcceptanceError("NAS upgrade replaced site-local secret material")


def compose_services(bundle: Path) -> set[str]:
    output = run([*reference_compose(), "config", "--services"], cwd=bundle)
    return {line for line in output.stdout.splitlines() if line}


def verify_controller_tls(bundle: Path, nas_ip: str, enrollment_hostname: str) -> None:
    root = bundle / "secrets/step-ca/root-certificate"
    response = run(
        [
            "curl",
            "-fsS",
            "--noproxy",
            "*",
            "--resolve",
            f"{enrollment_hostname}:8443:{nas_ip}",
            "--cacert",
            str(root),
            f"https://{enrollment_hostname}:8443/agent/v1/bootstrap",
        ],
        cwd=bundle,
    )
    try:
        document = json.loads(response.stdout)
    except json.JSONDecodeError as error:
        raise AcceptanceError("enrollment bootstrap is not JSON") from error
    if not isinstance(document, dict) or not document:
        raise AcceptanceError("enrollment bootstrap is empty")


def verify_postgres_databases(bundle: Path) -> None:
    result = run(
        [
            *reference_compose(),
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "control",
            "-d",
            "control",
            "-Atc",
            "SELECT datname FROM pg_database WHERE datname IN ('control','litellm') ORDER BY datname",
        ],
        cwd=bundle,
    )
    if result.stdout.split() != ["control", "litellm"]:
        raise AcceptanceError("control and LiteLLM do not have distinct databases")
    tables = run(
        [
            *reference_compose(),
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "litellm",
            "-d",
            "litellm",
            "-Atc",
            "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname = 'public'",
        ],
        cwd=bundle,
    )
    try:
        initialized_tables = int(tables.stdout.strip())
    except ValueError as error:
        raise AcceptanceError("LiteLLM database schema count is invalid") from error
    if initialized_tables < 1:
        raise AcceptanceError("LiteLLM database schema is not initialized")


def _bundle_secret(bundle: Path, relative: str) -> str:
    path = bundle / "secrets" / relative
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise AcceptanceError(f"acceptance secret {relative} is unavailable") from error
    if not value:
        raise AcceptanceError(f"acceptance secret {relative} is empty")
    return value


def _http_json(response: bytes, *, label: str) -> object:
    _, marker, body = response.partition(b"\r\n\r\n")
    if not marker:
        raise AcceptanceError(f"{label} response has no HTTP body")
    try:
        return json.loads(body)
    except json.JSONDecodeError as error:
        raise AcceptanceError(f"{label} response is not JSON") from error


def _tcp_tunnel(host: str, port: int) -> list[str]:
    return [
        sys.executable,
        "-c",
        (
            "import os,select,socket,sys\n"
            "peer=socket.create_connection((sys.argv[1],int(sys.argv[2])))\n"
            "while True:\n"
            "    readable,_,_=select.select([0,peer],[],[])\n"
            "    if 0 in readable:\n"
            "        data=os.read(0,65536)\n"
            "        if not data: break\n"
            "        peer.sendall(data)\n"
            "    if peer in readable:\n"
            "        data=peer.recv(65536)\n"
            "        if not data: break\n"
            "        os.write(1,data)\n"
        ),
        host,
        str(port),
    ]


def _tailnet_tunnel(host: str, port: int) -> list[str]:
    """Use tailscaled's own dialer instead of the runner's kernel route."""
    return ["tailscale", "nc", host, str(port)]


def _tailnet_request(
    bundle: Path,
    *,
    hostname: str,
    connect_host: str,
    path: str,
    headers: dict[str, str],
    accepted_statuses: set[int],
) -> bytes:
    return https_over_command(
        _tailnet_tunnel(connect_host, 443),
        server_hostname=hostname,
        path=path,
        cwd=bundle,
        environment=host_command_environment(),
        headers=headers,
        accepted_statuses=accepted_statuses,
        timeout=30,
    )


def issue_registry_client_certificate(
    bundle: Path, directory: Path
) -> tuple[Path, Path]:
    secret_root = bundle / "secrets"
    try:
        password = (secret_root / "step-ca-password").read_bytes().strip()
        intermediate_key = serialization.load_pem_private_key(
            (secret_root / "step-ca/intermediate-key").read_bytes(), password=password
        )
        intermediate = x509.load_pem_x509_certificate(
            (secret_root / "step-ca/intermediate-certificate").read_bytes()
        )
    except (OSError, TypeError, ValueError) as error:
        raise AcceptanceError(
            "registry client certificate authority is unavailable"
        ) from error
    if not isinstance(intermediate_key, ed25519.Ed25519PrivateKey):
        raise AcceptanceError("registry client certificate authority is invalid")
    node_id = "spk_" + "a" * 32
    client_key = ed25519.Ed25519PrivateKey.generate()
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node_id)]))
        .issuer_name(intermediate.subject)
        .public_key(client_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(minutes=10))
        .add_extension(
            x509.KeyUsage(True, False, False, False, False, False, False, False, False),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False
        )
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.UniformResourceIdentifier(
                        f"spiffe://vonk-forge.local/node/{node_id}"
                    )
                ]
            ),
            critical=False,
        )
        .sign(intermediate_key, algorithm=None)
    )
    certificate_path = directory / "registry-client-certificate.pem"
    key_path = directory / "registry-client-key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        client_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    certificate_path.chmod(0o600)
    key_path.chmod(0o600)
    return certificate_path, key_path


def verify_routed_service_behavior(
    bundle: Path,
    *,
    nas_ip: str,
    control_hostname: str,
    registry_hostname: str,
) -> None:
    litellm_key = _bundle_secret(bundle, "litellm-master-key")
    grafana_password = _bundle_secret(bundle, "grafana-admin-password")
    grafana_authorization = "Basic " + base64.b64encode(
        f"admin:{grafana_password}".encode()
    ).decode("ascii")

    for headers in ({}, {"Authorization": "Bearer acceptance-wrong-key"}):
        _tailnet_request(
            bundle,
            hostname=control_hostname,
            connect_host=control_hostname,
            path="/v1/models",
            headers=headers,
            accepted_statuses={401, 403},
        )
    models = _http_json(
        _tailnet_request(
            bundle,
            hostname=control_hostname,
            connect_host=control_hostname,
            path="/v1/models",
            headers={"Authorization": f"Bearer {litellm_key}"},
            accepted_statuses={200},
        ),
        label="LiteLLM models",
    )
    if not isinstance(models, dict) or not isinstance(models.get("data"), list):
        raise AcceptanceError("LiteLLM models response is invalid")

    for authorization in ("", "Basic YWRtaW46d3Jvbmc="):
        _tailnet_request(
            bundle,
            hostname=control_hostname,
            connect_host=control_hostname,
            path="/grafana/api/user",
            headers={} if not authorization else {"Authorization": authorization},
            accepted_statuses={401, 403},
        )
    user = _http_json(
        _tailnet_request(
            bundle,
            hostname=control_hostname,
            connect_host=control_hostname,
            path="/grafana/api/user",
            headers={"Authorization": grafana_authorization},
            accepted_statuses={200},
        ),
        label="Grafana user",
    )
    if not isinstance(user, dict) or user.get("login") != "admin":
        raise AcceptanceError("Grafana administrator authentication failed")
    datasource = _http_json(
        _tailnet_request(
            bundle,
            hostname=control_hostname,
            connect_host=control_hostname,
            path="/grafana/api/datasources/uid/vonk-prometheus",
            headers={"Authorization": grafana_authorization},
            accepted_statuses={200},
        ),
        label="Grafana datasource",
    )
    if not isinstance(datasource, dict) or datasource.get("type") != "prometheus":
        raise AcceptanceError("Grafana Prometheus datasource is unavailable")
    dashboards = _http_json(
        _tailnet_request(
            bundle,
            hostname=control_hostname,
            connect_host=control_hostname,
            path="/grafana/api/search?query=Vonk%20Forge",
            headers={"Authorization": grafana_authorization},
            accepted_statuses={200},
        ),
        label="Grafana dashboards",
    )
    if not isinstance(dashboards, list) or not {"vonk-fleet", "vonk-jobs"} <= {
        item.get("uid") for item in dashboards if isinstance(item, dict)
    }:
        raise AcceptanceError("Grafana provisioned dashboards are unavailable")
    query = _http_json(
        _tailnet_request(
            bundle,
            hostname=control_hostname,
            connect_host=control_hostname,
            path=(
                "/grafana/api/datasources/uid/vonk-prometheus/resources/api/v1/query?"
                "query=up%7Bjob%3D%22vonk-control%22%7D"
            ),
            headers={"Authorization": grafana_authorization},
            accepted_statuses={200},
        ),
        label="Prometheus query",
    )
    result = (
        query.get("data", {}).get("result")
        if isinstance(query, dict) and isinstance(query.get("data"), dict)
        else None
    )
    if (
        not isinstance(query, dict)
        or query.get("status") != "success"
        or not isinstance(result, list)
        or not any(
            isinstance(item, dict)
            and isinstance(item.get("metric"), dict)
            and item["metric"].get("job") == "vonk-control"
            for item in result
        )
    ):
        raise AcceptanceError("Prometheus did not ingest the control scrape")

    root = bundle / "secrets/step-ca/root-certificate"
    try:
        https_over_command(
            _tcp_tunnel(nas_ip, 8443),
            server_hostname=registry_hostname,
            path="/v2/",
            cwd=bundle,
            environment=host_command_environment(),
            ca_file=root,
            timeout=30,
        )
    except AcceptanceError:
        pass
    else:
        raise AcceptanceError(
            "registry accepted a request without client authentication"
        )
    with tempfile.TemporaryDirectory(
        prefix="vonk-registry-client-", dir=bundle.parent
    ) as directory:
        certificate, key = issue_registry_client_certificate(bundle, Path(directory))
        registry = _http_json(
            https_over_command(
                _tcp_tunnel(nas_ip, 8443),
                server_hostname=registry_hostname,
                path="/v2/",
                cwd=bundle,
                environment=host_command_environment(),
                ca_file=root,
                client_certificate=certificate,
                client_key=key,
                accepted_statuses={200},
                timeout=30,
            ),
            label="registry",
        )
    if registry != {}:
        raise AcceptanceError("registry route returned an unexpected API response")


def _json_object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in document:
            raise ValueError("JSON object has duplicate or invalid keys")
        document[key] = value
    return document


def _same_json_shape(actual: object, expected: object) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return (
            isinstance(actual, dict)
            and actual.keys() == expected.keys()
            and all(
                _same_json_shape(actual[key], value) for key, value in expected.items()
            )
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(
                _same_json_shape(left, right) for left, right in zip(actual, expected)
            )
        )
    return actual == expected


def _tailnet_hostname_address(hostname: str) -> str | None:
    try:
        results = socket.getaddrinfo(
            hostname,
            443,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror:
        return None
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for _family, _type, _protocol, _canonical, sockaddr in results:
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except (IndexError, ValueError):
            continue
        if not address.is_unspecified and not address.is_multicast:
            addresses.append(address)
    if not addresses:
        return None
    return str(next((address for address in addresses if address.version == 4), addresses[0]))


def wait_for_tailnet_services(
    bundle: Path,
    expected: dict[str, str],
    *,
    timeout: float = 120,
) -> dict[str, str]:
    if (
        not expected
        or not bundle.is_dir()
        or len(set(expected.values())) != len(expected)
        or any(TAILSCALE_SERVICE.fullmatch(service) is None for service in expected)
        or any(SAFE_DNS_SUFFIX.fullmatch(hostname) is None for hostname in expected.values())
    ):
        raise AcceptanceError("expected Tailscale Services are invalid")
    deadline = time.monotonic() + timeout
    while True:
        addresses = {
            service: address
            for service, hostname in expected.items()
            if (address := _tailnet_hostname_address(hostname)) is not None
        }
        missing = expected.keys() - addresses.keys()
        if not missing:
            return addresses
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            names = ", ".join(sorted(missing))
            raise AcceptanceError(
                f"acceptance client cannot resolve Tailscale Services: {names}"
            )
        time.sleep(min(2, remaining))


def wait_for_tailnet_https(
    bundle: Path,
    *,
    service: str,
    hostname: str,
    address: str,
    path: str,
    timeout: float = 120,
) -> None:
    deadline = time.monotonic() + timeout
    last_error: AcceptanceError | None = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            detail = "unknown failure" if last_error is None else str(last_error)
            raise AcceptanceError(
                f"Tailscale Service {service} at {address} did not become "
                f"HTTPS-ready: {detail}"
            ) from last_error
        try:
            https_over_command(
                _tailnet_tunnel(hostname, 443),
                cwd=bundle,
                server_hostname=hostname,
                path=path,
                environment=host_command_environment(),
                timeout=min(10, remaining),
            )
            return
        except AcceptanceError as error:
            last_error = error
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(2, remaining))


def _expected_tailnet_serve_status(
    *,
    hermes: bool,
    tailnet_suffix: str,
    control_service: str = "svc:vonk-forge",
    hermes_api_service: str = "svc:hermes-api",
    hermes_dashboard_service: str = "svc:hermes-dashboard",
) -> dict[str, object]:
    control_hostname = tailscale_service_hostname(control_service, tailnet_suffix)
    services: dict[str, object] = {
        control_service: {
            "TCP": {"443": {"HTTPS": True}},
            "Web": {
                f"{control_hostname}:443": {
                    "Handlers": {"/": {"Proxy": "http://caddy:8080"}}
                }
            },
        }
    }
    if hermes:
        services.update(
            {
                hermes_api_service: {
                    "TCP": {"443": {"HTTPS": True}},
                    "Web": {
                        f"{tailscale_service_hostname(hermes_api_service, tailnet_suffix)}:443": {
                            "Handlers": {"/": {"Proxy": "http://hermes-agent:8642"}}
                        }
                    },
                },
                hermes_dashboard_service: {
                    "TCP": {"443": {"HTTPS": True}},
                    "Web": {
                        f"{tailscale_service_hostname(hermes_dashboard_service, tailnet_suffix)}:443": {
                            "Handlers": {"/": {"Proxy": "http://hermes-agent:9119"}}
                        }
                    },
                },
            }
        )
    return {"Services": services}


def _expected_tailnet_serve_configuration(
    *,
    hermes: bool,
    control_service: str = "svc:vonk-forge",
    hermes_api_service: str = "svc:hermes-api",
    hermes_dashboard_service: str = "svc:hermes-dashboard",
) -> dict[str, object]:
    services: dict[str, object] = {
        control_service: {"endpoints": {"tcp:443": "http://caddy:8080"}}
    }
    if hermes:
        services.update(
            {
                hermes_api_service: {
                    "endpoints": {"tcp:443": "http://hermes-agent:8642"}
                },
                hermes_dashboard_service: {
                    "endpoints": {"tcp:443": "http://hermes-agent:9119"}
                },
            }
        )
    return {"version": "0.0.1", "services": services}


def _parse_tailnet_serve_json(raw: str, *, label: str) -> object:
    try:
        return json.loads(
            raw,
            object_pairs_hook=_json_object_without_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise AcceptanceError(f"Tailscale Serve {label} is invalid JSON") from error


def assert_tailnet_serve_status(
    raw: str,
    *,
    hermes: bool,
    tailnet_suffix: str,
    control_service: str = "svc:vonk-forge",
    hermes_api_service: str = "svc:hermes-api",
    hermes_dashboard_service: str = "svc:hermes-dashboard",
) -> None:
    if SAFE_DNS_SUFFIX.fullmatch(tailnet_suffix) is None:
        raise AcceptanceError("Tailscale Serve status suffix is invalid")
    document = _parse_tailnet_serve_json(raw, label="status")
    expected = _expected_tailnet_serve_status(
        hermes=hermes,
        tailnet_suffix=tailnet_suffix,
        control_service=control_service,
        hermes_api_service=hermes_api_service,
        hermes_dashboard_service=hermes_dashboard_service,
    )
    if not _same_json_shape(document, expected):
        raise AcceptanceError("Tailscale Serve status does not match selected topology")


def assert_tailnet_serve_configuration(
    status: str,
    configuration: str,
    *,
    hermes: bool,
    tailnet_suffix: str,
    control_service: str = "svc:vonk-forge",
    hermes_api_service: str = "svc:hermes-api",
    hermes_dashboard_service: str = "svc:hermes-dashboard",
) -> None:
    assert_tailnet_serve_status(
        status,
        hermes=hermes,
        tailnet_suffix=tailnet_suffix,
        control_service=control_service,
        hermes_api_service=hermes_api_service,
        hermes_dashboard_service=hermes_dashboard_service,
    )
    document = _parse_tailnet_serve_json(configuration, label="configuration")
    expected = _expected_tailnet_serve_configuration(
        hermes=hermes,
        control_service=control_service,
        hermes_api_service=hermes_api_service,
        hermes_dashboard_service=hermes_dashboard_service,
    )
    if not _same_json_shape(document, expected):
        raise AcceptanceError(
            "Tailscale Serve configuration does not match selected topology"
        )


def verify_tailscale_services(
    bundle: Path,
    *,
    hermes: bool,
    tailnet_suffix: str,
    control_service: str = "svc:vonk-forge",
    hermes_api_service: str = "svc:hermes-api",
    hermes_dashboard_service: str = "svc:hermes-dashboard",
    service_addresses: dict[str, str] | None,
    compose_command: list[str] | None = None,
) -> None:
    compose = compose_command if compose_command is not None else reference_compose()
    status = run(
        [
            *compose,
            "exec",
            "-T",
            "tailscale-gateway",
            "tailscale",
            "--socket=/var/run/tailscale/tailscaled.sock",
            "status",
            "--json",
        ],
        cwd=bundle,
    )
    document = json.loads(status.stdout)
    if document.get("BackendState") != "Running":
        raise AcceptanceError("Tailscale gateway is not running")
    serve = run(
        [
            *compose,
            "exec",
            "-T",
            "tailscale-gateway",
            "tailscale",
            "--socket=/var/run/tailscale/tailscaled.sock",
            "serve",
            "status",
            "--json",
        ],
        cwd=bundle,
    ).stdout
    configuration = run(
        [
            *compose,
            "exec",
            "-T",
            "tailscale-gateway",
            "tailscale",
            "--socket=/var/run/tailscale/tailscaled.sock",
            "serve",
            "get-config",
            "--all",
        ],
        cwd=bundle,
    ).stdout
    assert_tailnet_serve_configuration(
        serve,
        configuration,
        hermes=hermes,
        tailnet_suffix=tailnet_suffix,
        control_service=control_service,
        hermes_api_service=hermes_api_service,
        hermes_dashboard_service=hermes_dashboard_service,
    )

    if service_addresses is None:
        return

    routes = [
        (
            control_service,
            tailscale_service_hostname(control_service, tailnet_suffix),
            "/healthz",
        )
    ]
    if hermes:
        routes.append(
            (
                hermes_dashboard_service,
                tailscale_service_hostname(
                    hermes_dashboard_service, tailnet_suffix
                ),
                "/",
            )
        )
    for service, hostname, path in routes:
        address = service_addresses.get(service)
        if address is None:
            raise AcceptanceError("Tailscale Service address is unavailable")
        https_over_command(
            _tailnet_tunnel(hostname, 443),
            cwd=bundle,
            server_hostname=hostname,
            path=path,
            environment=host_command_environment(),
            timeout=30,
        )


def exercise_compose(
    bundle: Path,
    *,
    nas_ip: str,
    control_hostname: str,
    enrollment_hostname: str,
    registry_hostname: str,
    tailnet_suffix: str,
    hermes: bool,
    control_service: str = "svc:vonk-forge",
    hermes_api_service: str = "svc:hermes-api",
    hermes_dashboard_service: str = "svc:hermes-dashboard",
    require_external_tailnet_client: bool = True,
) -> None:
    expected = HERMES_SERVICES if hermes else DEFAULT_SERVICES
    configured = run([*reference_compose(), "config", "--quiet"], cwd=bundle)
    if configured.stdout or configured.stderr:
        raise AcceptanceError("Compose validation emitted output")
    if compose_services(bundle) != expected:
        raise AcceptanceError("rendered Compose service topology is not canonical")
    images = run([*reference_compose(), "config", "--images"], cwd=bundle).stdout
    for image in images.splitlines():
        if not is_immutable_image(image):
            raise AcceptanceError(f"Compose image is not immutable: {image}")

    try:
        try:
            run(
                [
                    *reference_compose(),
                    "up",
                    "-d",
                    "--wait",
                    "--wait-timeout",
                    "360",
                    "--remove-orphans",
                ],
                cwd=bundle,
                timeout=420,
            )
        except (AcceptanceError, subprocess.TimeoutExpired) as error:
            diagnostics = compose_startup_diagnostics(bundle)
            cause = _redact_diagnostics(str(error)) or type(error).__name__
            raise AcceptanceError(
                f"NAS Compose startup failed; cause: {cause}; {diagnostics}"
            ) from error
        status = run(
            [*reference_compose(), "ps", "--all", "--format", "json"],
            cwd=bundle,
        )
        assert_compose_services_healthy(status.stdout, expected)
        verify_controller_tls(bundle, nas_ip, enrollment_hostname)
        verify_postgres_databases(bundle)
        if not require_external_tailnet_client:
            verify_tailscale_services(
                bundle,
                hermes=hermes,
                tailnet_suffix=tailnet_suffix,
                control_service=control_service,
                hermes_api_service=hermes_api_service,
                hermes_dashboard_service=hermes_dashboard_service,
                service_addresses=None,
            )
            return
        expected_tailnet_services = {
            control_service: control_hostname,
        }
        if hermes:
            expected_tailnet_services.update(
                {
                    hermes_api_service: tailscale_service_hostname(
                        hermes_api_service, tailnet_suffix
                    ),
                    hermes_dashboard_service: tailscale_service_hostname(
                        hermes_dashboard_service, tailnet_suffix
                    ),
                }
            )
        service_addresses = wait_for_tailnet_services(
            bundle, expected_tailnet_services
        )
        wait_for_tailnet_https(
            bundle,
            service=control_service,
            hostname=control_hostname,
            address=service_addresses[control_service],
            path="/healthz",
        )
        verify_routed_service_behavior(
            bundle,
            nas_ip=nas_ip,
            control_hostname=control_hostname,
            registry_hostname=registry_hostname,
        )
        verify_tailscale_services(
            bundle,
            hermes=hermes,
            tailnet_suffix=tailnet_suffix,
            control_service=control_service,
            hermes_api_service=hermes_api_service,
            hermes_dashboard_service=hermes_dashboard_service,
            service_addresses=service_addresses,
        )
    finally:
        run(
            [
                *reference_compose(),
                "down",
                "--volumes",
                "--remove-orphans",
                "--timeout",
                "30",
            ],
            cwd=bundle,
            timeout=120,
        )


def reference_rollout_bundles(default: Path, hermes: Path) -> tuple[Path, ...]:
    """The Hermes graph is a superset, so one rollout covers all services."""
    del default
    return (hermes,)


def main() -> None:
    if os.name != "posix" or os.geteuid() == 0:
        raise AcceptanceError("NAS acceptance must run as an ordinary non-root user")
    candidate_url = required_environment("VONK_ACCEPTANCE_CANDIDATE_URL")
    if SAFE_URL.fullmatch(candidate_url) is None:
        raise AcceptanceError("candidate NAS URL is invalid")
    tailnet_suffix = required_environment("VONK_ACCEPTANCE_TAILNET_DNS_SUFFIX")
    if SAFE_DNS_SUFFIX.fullmatch(tailnet_suffix) is None:
        raise AcceptanceError("acceptance tailnet DNS suffix is invalid")
    services = {
        "control": required_environment(
            "VONK_ACCEPTANCE_TAILSCALE_CONTROL_SERVICE"
        ),
        "hermes_api": required_environment(
            "VONK_ACCEPTANCE_TAILSCALE_HERMES_API_SERVICE"
        ),
        "hermes_dashboard": required_environment(
            "VONK_ACCEPTANCE_TAILSCALE_HERMES_DASHBOARD_SERVICE"
        ),
    }
    if len(set(services.values())) != len(services) or any(
        TAILSCALE_SERVICE.fullmatch(value) is None for value in services.values()
    ):
        raise AcceptanceError("acceptance Tailscale Service names are invalid")
    control_hostname = tailscale_service_hostname(services["control"], tailnet_suffix)
    gateway_hostname = required_environment(
        "VONK_ACCEPTANCE_TAILSCALE_GATEWAY_HOSTNAME"
    )
    if TAILSCALE_HOSTNAME.fullmatch(gateway_hostname) is None:
        raise AcceptanceError("acceptance Tailscale gateway hostname is invalid")
    oauth_client_id = required_environment(
        "VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_ID", secret=True
    )
    oauth_client_secret = required_environment(
        "VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_SECRET", secret=True
    )
    upstream_key = required_environment(
        "VONK_ACCEPTANCE_LITELLM_UPSTREAM_KEY", secret=True
    )
    workspace = Path(required_environment("VONK_ACCEPTANCE_WORKSPACE"))
    if not workspace.is_absolute() or workspace.is_symlink() or not workspace.is_dir():
        raise AcceptanceError("acceptance workspace is unavailable")
    nas_ip = host_ipv4()
    nas_bind_ip = nas_bind_ipv4(nas_ip)
    enrollment_hostname = f"enroll.acceptance.{tailnet_suffix}"
    fixtures = compose_compatibility_fixtures()
    require_external_tailnet_client = require_tailnet_client()

    with tempfile.TemporaryDirectory(
        prefix="vonk-nas-acceptance-", dir=workspace
    ) as directory:
        root = Path(directory)
        child_environment = command_environment(root / "workstation")
        common = {
            "nas_ip": nas_bind_ip,
            "tailnet_suffix": tailnet_suffix,
            "oauth_client_id": oauth_client_id,
            "oauth_client_secret": oauth_client_secret,
            "upstream_key": upstream_key,
            "control_service": services["control"],
            "hermes_dashboard_service": services["hermes_dashboard"],
        }
        first = generate_bundle(
            root / "default-first",
            candidate_url=candidate_url,
            child_environment=child_environment,
            responses=nas_responses(**common, hermes=False),
        )
        first_secrets = secret_snapshot(first)
        generate_bundle(
            root / "default-first",
            candidate_url=candidate_url,
            child_environment=child_environment,
            responses=nas_responses(**common, hermes=False),
            require_all_prompts=False,
        )
        assert_site_secrets_preserved(first, first_secrets)
        second = generate_bundle(
            root / "default-second",
            candidate_url=candidate_url,
            child_environment=child_environment,
            responses=nas_responses(**common, hermes=False),
        )
        hermes = generate_bundle(
            root / "hermes",
            candidate_url=candidate_url,
            child_environment=child_environment,
            responses=nas_responses(**common, hermes=True),
        )
        assert_repeatable(first, second)
        for bundle in (first, hermes):
            configure_tailnet_service_names(
                bundle,
                gateway_hostname=gateway_hostname,
                **services,
            )
        for bundle in (first, hermes):
            assert_compose_compatibility(
                bundle,
                fixtures=fixtures,
                environment=host_command_environment(),
            )
        for bundle in reference_rollout_bundles(first, hermes):
            exercise_compose(
                bundle,
                nas_ip=nas_ip,
                control_hostname=control_hostname,
                enrollment_hostname=enrollment_hostname,
                registry_hostname=f"registry.acceptance.{tailnet_suffix}",
                tailnet_suffix=tailnet_suffix,
                hermes=True,
                control_service=services["control"],
                hermes_api_service=services["hermes_api"],
                hermes_dashboard_service=services["hermes_dashboard"],
                require_external_tailnet_client=require_external_tailnet_client,
            )


if __name__ == "__main__":
    try:
        main()
    except (AcceptanceError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(f"fresh NAS acceptance: {error}") from error
