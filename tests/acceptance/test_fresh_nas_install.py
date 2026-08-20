#!/usr/bin/env python3
from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

from tests.acceptance.runtime import (
    AcceptanceError,
    assert_bundle_contract,
    assert_compose_compatibility,
    assert_compose_services_healthy,
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


def required_environment(name: str, *, secret: bool = False) -> str:
    value = os.environ.get(name, "")
    if not value or "\0" in value or "\n" in value or "\r" in value:
        label = "acceptance secret" if secret else "acceptance input"
        raise AcceptanceError(f"{label} {name} is missing or invalid")
    return value


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
) -> list[tuple[str, str]]:
    hostnames = {
        "Control hostname": f"vonk-forge.{tailnet_suffix}",
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
                f"https://hermes-dashboard.{tailnet_suffix}",
            )
        )
    return responses


def generate_bundle(
    root: Path,
    *,
    candidate_url: str,
    child_environment: dict[str, str],
    responses: list[tuple[str, str]],
    require_all_prompts: bool = True,
) -> Path:
    root.mkdir(mode=0o700)
    run_interactive(
        ["/bin/sh", "-c", f"curl -fsSL '{candidate_url}' | sh"],
        cwd=root,
        environment=child_environment,
        responses=responses,
        timeout=180,
        require_all_prompts=require_all_prompts,
    )
    bundle = root / "vonk-forge"
    assert_bundle_contract(bundle)
    return bundle


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
    return {
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }


def reference_compose() -> list[str]:
    executable = Path(required_environment("VONK_ACCEPTANCE_REFERENCE_COMPOSE"))
    if not executable.is_absolute() or not executable.is_file() or not os.access(
        executable, os.X_OK
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
        if path.is_file()
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


def verify_tailscale_services(
    bundle: Path, *, hermes: bool, tailnet_suffix: str
) -> None:
    status = run(
        [
            *reference_compose(),
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
            *reference_compose(),
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
    expected = ["svc:vonk-forge"]
    if hermes:
        expected.extend(("svc:hermes-api", "svc:hermes-dashboard"))
    if any(service not in serve for service in expected):
        raise AcceptanceError("Tailscale HTTPS service publication is incomplete")
    if not hermes and "svc:hermes-" in serve:
        raise AcceptanceError("Hermes was advertised while disabled")

    urls = [f"https://vonk-forge.{tailnet_suffix}/healthz"]
    if hermes:
        urls.append(f"https://hermes-dashboard.{tailnet_suffix}/")
    for url in urls:
        hostname, _, path = url.removeprefix("https://").partition("/")
        https_over_command(
            [
                *reference_compose(),
                "exec",
                "-T",
                "tailscale-gateway",
                "tailscale",
                "--socket=/var/run/tailscale/tailscaled.sock",
                "nc",
                hostname,
                "443",
            ],
            cwd=bundle,
            server_hostname=hostname,
            path=f"/{path}",
            environment=host_command_environment(),
            timeout=30,
        )


def exercise_compose(
    bundle: Path,
    *,
    nas_ip: str,
    enrollment_hostname: str,
    tailnet_suffix: str,
    hermes: bool,
) -> None:
    expected = HERMES_SERVICES if hermes else DEFAULT_SERVICES
    configured = run([*reference_compose(), "config", "--quiet"], cwd=bundle)
    if configured.stdout or configured.stderr:
        raise AcceptanceError("Compose validation emitted output")
    if compose_services(bundle) != expected:
        raise AcceptanceError("rendered Compose service topology is not canonical")
    images = run([*reference_compose(), "config", "--images"], cwd=bundle).stdout
    for image in images.splitlines():
        if "@sha256:" not in image or any(
            mutable in image for mutable in (":latest", ":dev", ":main", ":edge")
        ):
            raise AcceptanceError(f"Compose image is not immutable: {image}")

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
        status = run(
            [*reference_compose(), "ps", "--all", "--format", "json"],
            cwd=bundle,
        )
        assert_compose_services_healthy(status.stdout, expected)
        verify_controller_tls(bundle, nas_ip, enrollment_hostname)
        verify_postgres_databases(bundle)
        verify_tailscale_services(bundle, hermes=hermes, tailnet_suffix=tailnet_suffix)
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
    oauth_client_id = required_environment(
        "VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_ID", secret=True
    )
    oauth_client_secret = required_environment(
        "VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_SECRET", secret=True
    )
    upstream_key = required_environment(
        "VONK_ACCEPTANCE_LITELLM_UPSTREAM_KEY", secret=True
    )
    nas_ip = host_ipv4()
    enrollment_hostname = f"enroll.acceptance.{tailnet_suffix}"
    fixtures = compose_compatibility_fixtures()

    with tempfile.TemporaryDirectory(prefix="vonk-nas-acceptance-") as directory:
        root = Path(directory)
        child_environment = command_environment(root / "workstation")
        common = {
            "nas_ip": nas_ip,
            "tailnet_suffix": tailnet_suffix,
            "oauth_client_id": oauth_client_id,
            "oauth_client_secret": oauth_client_secret,
            "upstream_key": upstream_key,
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
            assert_compose_compatibility(
                bundle,
                fixtures=fixtures,
                environment=host_command_environment(),
            )
        for bundle in reference_rollout_bundles(first, hermes):
            exercise_compose(
                bundle,
                nas_ip=nas_ip,
                enrollment_hostname=enrollment_hostname,
                tailnet_suffix=tailnet_suffix,
                hermes=True,
            )


if __name__ == "__main__":
    try:
        main()
    except (AcceptanceError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(f"fresh NAS acceptance: {error}") from error
