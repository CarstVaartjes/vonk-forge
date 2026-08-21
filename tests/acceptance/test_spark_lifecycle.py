#!/usr/bin/env python3
"""Executable, fail-closed Spark lifecycle acceptance entry point."""

from __future__ import annotations

import argparse
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, Self

import yaml

sys.path.insert(0, os.fspath(Path(__file__).resolve().parents[2]))

from scripts.development_slice_client import Client, SliceError, require_object
from scripts.spark_lifecycle_contract import (
    GATES,
    PHASES,
    ContractError,
    recompute_publication_graphs,
    validate_lifecycle,
)
from tests.acceptance.runtime import (
    AcceptanceError,
    assert_compose_services_healthy,
    bootstrap_command,
    https_over_command,
    run_interactive,
)
from tests.acceptance.test_fresh_nas_install import (
    DEFAULT_SERVICES,
    command_environment,
    generate_bundle,
    host_command_environment,
    nas_responses,
)

PLATFORMS = ("linux-amd64", "linux-arm64")
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
SOURCE_SHA = re.compile(r"[0-9a-f]{40}\Z")
CHANNEL = re.compile(r"(?:dev|stable)\Z")
VERSION = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:[+~][0-9A-Za-z.+~-]+)?\Z"
)
SAFE_HTTPS_URL = re.compile(r"https://[A-Za-z0-9._~:/-]+\Z")
NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")
SERIAL = re.compile(r"[1-9][0-9]{0,127}\Z")
PROJECT = re.compile(r"vonk-spark-[1-9][0-9]*-(?:amd64|arm64)\Z")
CERTIFICATE_LIFETIME_SECONDS = 300
CONTROL_HOST_PREFIX = "vonk-forge"
ENROLLMENT_HOST = "enroll.spark.localhost"
AGENT_HOST = "agents.spark.localhost"
REGISTRY_HOST = "registry.spark.localhost"
SPARK_CONFIG = Path("/etc/vonk-forge-agent/agent.toml")
AGENT_BINARY = Path("/usr/lib/vonk-forge/vonk-agent")
AGENT_DATA = Path("/var/lib/vonk-forge-agent")
COMPOSE_IMAGE_ROLES = {
    "api": "control-api",
    "worker": "control-worker",
    "hermes": "hermes-agent",
}


class LifecycleError(RuntimeError):
    """A bounded acceptance failure that contains no credential material."""


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
        bootstrap_command(url),
        cwd=cwd,
        environment=environment,
        responses=responses,
        timeout=300,
        require_all_prompts=True,
        forbidden_values=forbidden,
    )


def _configure_acceptance_renewal(bundle: Path, *, lifetime_seconds: int) -> None:
    if lifetime_seconds != CERTIFICATE_LIFETIME_SECONDS:
        raise LifecycleError("acceptance certificate lifetime is invalid")
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
    if not isinstance(claims, dict) or claims.get("disableRenewal") is not True:
        raise LifecycleError("Step CA provisioner claims are invalid")
    duration = f"{lifetime_seconds}s"
    provisioner["claims"] = {
        "defaultTLSCertDuration": duration,
        "disableRenewal": True,
        "maxTLSCertDuration": duration,
        "minTLSCertDuration": duration,
    }
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
    compose_path.write_text(
        yaml.safe_dump(compose, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    os.chmod(compose_path, 0o644)


def _synthetic_device_fixture(platform_name: str) -> tuple[bytes, str]:
    if platform_name != "linux-arm64":
        raise LifecycleError("synthetic device fixture is ARM64-only")
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


def _semantic_version(package_version: str) -> str:
    return package_version.split("~", 1)[0].split("+", 1)[0]


def _http_response(raw: bytes) -> tuple[int, dict[str, list[str]], bytes]:
    head, separator, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    if not separator or not lines:
        raise LifecycleError("HTTPS response is invalid")
    matched = re.fullmatch(rb"HTTP/1\.[01] ([0-9]{3})(?: .*)?", lines[0])
    if matched is None:
        raise LifecycleError("HTTPS response status is invalid")
    headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        name, marker, value = line.partition(b":")
        if not marker:
            raise LifecycleError("HTTPS response headers are invalid")
        try:
            decoded_name = name.decode("ascii").strip().lower()
            decoded_value = value.decode("latin-1").strip()
        except UnicodeDecodeError as error:
            raise LifecycleError("HTTPS response headers are invalid") from error
        if not decoded_name:
            raise LifecycleError("HTTPS response headers are invalid")
        headers.setdefault(decoded_name, []).append(decoded_value)
    transfer = headers.get("transfer-encoding", [])
    if transfer:
        if transfer != ["chunked"]:
            raise LifecycleError("HTTPS response transfer encoding is invalid")
        body = _decode_chunked(body)
    elif "content-length" in headers:
        try:
            length = int(headers["content-length"][0])
        except (ValueError, IndexError) as error:
            raise LifecycleError("HTTPS response length is invalid") from error
        if length != len(body):
            raise LifecycleError("HTTPS response body is truncated")
    return int(matched.group(1)), headers, body


def _decode_chunked(raw: bytes) -> bytes:
    decoded = bytearray()
    remaining = raw
    while True:
        size_line, marker, remaining = remaining.partition(b"\r\n")
        if not marker:
            raise LifecycleError("chunked HTTPS response is invalid")
        try:
            size = int(size_line.partition(b";")[0], 16)
        except ValueError as error:
            raise LifecycleError("chunked HTTPS response is invalid") from error
        if size == 0:
            if remaining not in {b"\r\n", b""} and not remaining.endswith(b"\r\n\r\n"):
                raise LifecycleError("chunked HTTPS response trailer is invalid")
            return bytes(decoded)
        if len(remaining) < size + 2 or remaining[size : size + 2] != b"\r\n":
            raise LifecycleError("chunked HTTPS response is truncated")
        decoded.extend(remaining[:size])
        remaining = remaining[size + 2 :]


class PublicController:
    def __init__(
        self,
        *,
        bundle: Path,
        project: str,
        hostname: str,
    ) -> None:
        self.bundle = bundle
        self.project = project
        self.hostname = hostname

    def _command(self) -> list[str]:
        return [
            "docker",
            "compose",
            "--project-name",
            self.project,
            "exec",
            "-T",
            "tailscale-gateway",
            "tailscale",
            "--socket=/var/run/tailscale/tailscaled.sock",
            "nc",
            self.hostname,
            "443",
        ]

    def raw_request(
        self,
        method: str,
        path: str,
        body: bytes | None,
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[int, dict[str, list[str]], bytes]:
        try:
            raw = https_over_command(
                self._command(),
                server_hostname=self.hostname,
                path=path,
                cwd=self.bundle,
                environment=host_command_environment(),
                timeout=timeout,
                headers=headers,
                accepted_statuses=set(range(100, 600)),
                method=method,
                body=body,
            )
        except AcceptanceError as error:
            raise LifecycleError("public controller boundary is unavailable") from error
        return _http_response(raw)

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


class SparkLifecycle:
    def __init__(self, arguments: argparse.Namespace, graph: dict[str, object]) -> None:
        self.arguments = arguments
        self.graph = graph
        architecture = arguments.platform.removeprefix("linux-")
        self.project = f"vonk-spark-{arguments.run_id}-{architecture}"
        if PROJECT.fullmatch(self.project) is None:
            raise LifecycleError("isolated Compose project identity is invalid")
        self.workspace = self._required_workspace()
        self.temporary_root: Path | None = None
        self.bundle: Path | None = None
        self.control: Client | None = None
        self.synthetic_paths: list[Path] = []
        self.agent_installed = False
        self.synthetic_fixture_sha256: str | None = None
        self.control_hostname = f"{CONTROL_HOST_PREFIX}.{self._required_environment('VONK_ACCEPTANCE_TAILNET_DNS_SUFFIX')}"
        self.origin = self._required_environment("INSTALLER_PUBLIC_ORIGIN")
        if self.origin != "https://install.vonkforge.ai":
            raise LifecycleError("installer public origin is invalid")
        self.machine = platform.machine()
        expected_machine = {
            "linux-amd64": {"x86_64", "amd64"},
            "linux-arm64": {"aarch64", "arm64"},
        }[arguments.platform]
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
        try:
            self._start_controller()
            return self
        except BaseException:
            self._cleanup()
            raise

    def __exit__(self, *_error: object) -> None:
        self._cleanup()

    def _compose(self, *arguments: str) -> list[str]:
        return [
            "docker",
            "compose",
            "--project-name",
            self.project,
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
        except (OSError, subprocess.SubprocessError) as error:
            raise LifecycleError("acceptance command could not execute") from error
        if result.returncode != 0:
            raise LifecycleError(
                f"acceptance command failed: {Path(command[0]).name} {command[1] if len(command) > 1 else ''}".rstrip()
            )
        return result

    def _cleanup(self) -> None:
        failures: list[BaseException] = []
        if getattr(self, "agent_installed", False):
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
                    ["sudo", "/usr/bin/rm", "--", os.fspath(path)],
                    cwd=Path("/"),
                    timeout=30,
                )
            except BaseException as error:  # noqa: BLE001 - continue cleanup.
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

    def _start_controller(self) -> None:
        self.temporary_root = Path(
            tempfile.mkdtemp(prefix="vonk-spark-lifecycle-", dir=self.workspace)
        )
        child_environment = command_environment(self.temporary_root / "workstation")
        responses = nas_responses(
            nas_ip="127.0.0.1",
            tailnet_suffix=self.control_hostname.removeprefix(
                f"{CONTROL_HOST_PREFIX}."
            ),
            oauth_client_id=self._required_environment(
                "VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_ID", secret=True
            ),
            oauth_client_secret=self._required_environment(
                "VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_SECRET", secret=True
            ),
            upstream_key=self._required_environment(
                "VONK_ACCEPTANCE_LITELLM_UPSTREAM_KEY", secret=True
            ),
            hermes=False,
        )
        replacements = {
            "Agent enrollment hostname: ": ENROLLMENT_HOST,
            "Agent controller hostname: ": AGENT_HOST,
            "Registry hostname: ": REGISTRY_HOST,
        }
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
            self.bundle, lifetime_seconds=CERTIFICATE_LIFETIME_SECONDS
        )
        self._assert_project_is_empty()
        self._assert_compose_image_graph()
        self._run_command(
            self._compose(
                "up",
                "-d",
                "--wait",
                "--wait-timeout",
                "360",
                "--remove-orphans",
            ),
            cwd=self.bundle,
            timeout=420,
        )
        status = self._run_command(
            self._compose("ps", "--all", "--format", "json"), cwd=self.bundle
        )
        try:
            assert_compose_services_healthy(status.stdout, DEFAULT_SERVICES)
        except AcceptanceError as error:
            raise LifecycleError(
                "candidate controller services are not healthy"
            ) from error
        if self.arguments.platform == "linux-arm64":
            self.synthetic_fixture_sha256 = self._materialize_synthetic_device()
        boundary = PublicController(
            bundle=self.bundle,
            project=self.project,
            hostname=self.control_hostname,
        )
        password = self._read_secret("admin-password")
        self.control = boundary.login(password, timeout=30)
        del password

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
        for role, service in COMPOSE_IMAGE_ROLES.items():
            configured_service = services.get(service)
            if not isinstance(configured_service, dict) or configured_service.get(
                "image"
            ) != images.get(role):
                raise LifecycleError("Compose image graph differs from publication")
        for service in services.values():
            image = service.get("image") if isinstance(service, dict) else None
            if (
                not isinstance(image, str)
                or "@sha256:" not in image
                or any(
                    value in image for value in (":latest", ":dev", ":main", ":edge")
                )
            ):
                raise LifecycleError("Compose contains a mutable image")

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
        return digest

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
        return {
            "HOME": os.environ.get("HOME", "/tmp"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "TMPDIR": os.fspath(self.temporary_root),
            "VONK_INSTALL_BASE_URL": base,
            "VONK_INSTALL_RELEASE_MANIFEST": os.fspath(local_release),
            "VONK_INSTALL_RELEASE_SIGNATURE": os.fspath(signature),
        }

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
                self._bootstrap_url(baseline=True),
                cwd=self.temporary_root,
                environment=self._installer_environment(baseline=True),
                enrollment_url=enrollment_url,
                ca_sha256=ca_sha256,
                pairing_token=pairing_token,
            )
        except AcceptanceError as error:
            raise LifecycleError("baseline Spark installation failed") from error
        finally:
            del pairing_token
        self.agent_installed = True
        baseline = self._wait_for_agent_identity(
            package_version=str(self.graph["baseline_version"]), timeout=180
        )
        use_count = self._pairing_grant_use_count(grant_id)
        direct_health = self._direct_agent_health()
        node_id = str(baseline["node_id"])
        if self.arguments.platform == "linux-amd64":
            return {
                "controller_generation": self.arguments.generation,
                "direct_agent_health": direct_health,
                "installation": {
                    "architecture": "amd64",
                    "package_sha256": self.graph["baseline_package_sha256"],
                    "version": self.graph["baseline_version"],
                },
                "node_id": node_id,
                "pairing_grant_use_count": use_count,
                "publication_graph": self.graph,
            }

        canary = self._run_synthetic_canary(node_id)
        renewal = self._observe_renewal(node_id, str(baseline["serial"]))
        config_before = self._hash_path(SPARK_CONFIG)
        private_before = self._hash_path(AGENT_DATA / "machine-evidence")
        try:
            _run_spark_bootstrap(
                self._bootstrap_url(baseline=False),
                cwd=self.temporary_root,
                environment=self._installer_environment(baseline=False),
            )
        except AcceptanceError as error:
            raise LifecycleError("candidate Spark upgrade failed") from error
        candidate = self._wait_for_agent_identity(
            package_version=str(self.graph["candidate_version"]), timeout=180
        )
        config_after = self._hash_path(SPARK_CONFIG)
        private_after = self._hash_path(AGENT_DATA / "machine-evidence")
        if candidate["node_id"] != node_id:
            raise LifecycleError("candidate upgrade changed node identity")
        return {
            "canary": canary,
            "config_sha256_after_upgrade": config_after,
            "config_sha256_before_upgrade": config_before,
            "controller_generation": self.arguments.generation,
            "direct_agent_health": self._direct_agent_health(),
            "installation": {
                "architecture": "arm64",
                "baseline": self._installation_identity(baseline),
                "candidate": self._installation_identity(candidate),
            },
            "node_id_after_renewal": renewal["node_id"],
            "node_id_after_upgrade": candidate["node_id"],
            "node_id_before_renewal": node_id,
            "pairing_grant_use_count": use_count,
            "private_identity_sha256_after_upgrade": private_after,
            "private_identity_sha256_before_upgrade": private_before,
            "publication_graph": self.graph,
            "renewal": renewal["proof"],
            "synthetic_device": {
                "architecture": "linux-arm64",
                "cdi_name": "nvidia.com/gpu=all",
                "fixture_sha256": self.synthetic_fixture_sha256,
                "physical_gpu": False,
                "provenance": "ci-only-synthetic-cdi",
                "synthetic": True,
            },
        }

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
            "controller_endpoint",
            "enrollment_endpoint",
            "expires_at",
            "id",
            "purpose",
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
            or not isinstance(ca_sha256, str)
            or SHA256.fullmatch(ca_sha256) is None
            or not isinstance(token, str)
            or not 43 <= len(token) <= 64
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
        assert self.control is not None and self.temporary_root is not None
        if NODE_ID.fullmatch(node_id) is None:
            raise LifecycleError("synthetic canary node identity is invalid")
        runner_path = REPOSITORY_ROOT / "scripts/run-development-slices"
        scripts_path = os.fspath(runner_path.parent)
        loader = importlib.machinery.SourceFileLoader(
            "spark_lifecycle_development_runner", os.fspath(runner_path)
        )
        specification = importlib.util.spec_from_loader(loader.name, loader)
        if specification is None or specification.loader is None:
            raise LifecycleError("synthetic canary runner is unavailable")
        module = importlib.util.module_from_spec(specification)
        sys.path.insert(0, scripts_path)
        try:
            specification.loader.exec_module(module)
        except (ImportError, OSError) as error:
            raise LifecycleError("synthetic canary runner is unavailable") from error
        finally:
            sys.path.remove(scripts_path)
        evidence = self.temporary_root / "synthetic-canary.json"
        arguments = argparse.Namespace(
            phase="synthetic",
            api_base=f"https://{self.control_hostname}",
            inference_base=f"https://{self.control_hostname}",
            admin_token_file=None,
            inference_token_file=None,
            timeout_seconds=300.0,
            recipe=None,
            catalog=None,
            library_root=REPOSITORY_ROOT,
            builder_node=node_id,
            target_node=[node_id],
            failure_node=None,
            source_context=None,
            qualification_file=None,
            evidence_file=evidence,
            poll_seconds=1.0,
            stop_after=None,
        )
        try:
            runner = module.Runner(
                arguments,
                control_client=self.control,
                inference_client=self.control,
            )
            runner.run()
        except module.SliceError as error:
            raise LifecycleError("synthetic canary failed") from error
        completed = runner.evidence.get("completed_states")
        response_digest = runner.outputs.get("inference_response_sha256")
        if (
            completed != list(module.SYNTHETIC_STATES)
            or not isinstance(response_digest, str)
            or SHA256.fullmatch(response_digest) is None
        ):
            raise LifecycleError("synthetic canary evidence is incomplete")
        return {
            "completed_states": completed,
            "deterministic_response_sha256": response_digest,
        }

    @staticmethod
    def _serial_proof(serial: str) -> str:
        if SERIAL.fullmatch(serial) is None:
            raise LifecycleError("certificate serial is invalid")
        encoded = format(int(serial), "x")
        if not 16 <= len(encoded) <= 64:
            raise LifecycleError("certificate serial proof is invalid")
        return encoded

    def _old_certificate_rejected(self) -> bool:
        assert self.temporary_root is not None
        credential_root = AGENT_DATA / "credentials"
        leaf = self.temporary_root / "retired-agent-certificate.pem"
        chain = self.temporary_root / "retired-agent-chain.pem"
        combined = self.temporary_root / "retired-agent-bundle.pem"
        for source, target in (
            (credential_root / "certificate.pem", leaf),
            (credential_root / "chain.pem", chain),
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
        combined.write_bytes(leaf.read_bytes() + chain.read_bytes())
        os.chmod(combined, 0o600)
        leaf.unlink()
        chain.unlink()
        result = self._run_command(
            [
                "sudo",
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
                os.fspath(combined),
                "--key",
                os.fspath(credential_root / "private-key.pem"),
                f"https://{AGENT_HOST}:8443/agent/v1/source-bundles/{'0' * 64}",
            ],
            cwd=Path("/"),
            timeout=40,
        )
        return result.stdout == "401"

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
                    package_version=str(self.graph["baseline_version"]), timeout=30
                )
                if (
                    identity.get("node_id") != node_id
                    or identity.get("serial") != serial_after
                ):
                    raise LifecycleError("renewed agent identity is inconsistent")
                if not self._old_certificate_rejected():
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
        if (
            not isinstance(document, dict)
            or set(document)
            != {
                "architecture",
                "binary_digest",
                "build_digest",
                "self_test_passed",
                "semantic_version",
            }
            or document.get("self_test_passed") is not True
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
                if (
                    self._installed_package_version() != package_version
                    or self_test.get("semantic_version") != expected_semantic
                    or self_test.get("architecture") != expected_architecture
                ):
                    raise LifecycleError("installed package identity is unexpected")
                _, response = self.control.request("GET", "/api/v1/agents")
                agents = require_object(response, "agents").get("agents")
                matching = [
                    agent
                    for agent in agents
                    if isinstance(agent, dict)
                    and agent.get("semantic_version") == expected_semantic
                ]
                if len(matching) != 1:
                    raise LifecycleError("controller has not observed the direct agent")
                agent = matching[0]
                node_id = agent.get("node_id")
                if (
                    not isinstance(node_id, str)
                    or NODE_ID.fullmatch(node_id) is None
                    or agent.get("state") != "active"
                    or agent.get("stale") is not False
                    or agent.get("build_digest") != self_test["build_digest"]
                    or agent.get("binary_digest") != self_test["binary_digest"]
                    or "agent.runtime.rust.v1" not in agent.get("capabilities", [])
                ):
                    raise LifecycleError("controller direct-agent identity is invalid")
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
                if (
                    len(rows) != 1
                    or rows[0][:5] != expected_row
                    or len(rows[0]) != 6
                    or SERIAL.fullmatch(rows[0][5]) is None
                ):
                    raise LifecycleError("controller runtime identity is incomplete")
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
        raise LifecycleError(
            "direct Rust agent identity did not converge"
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
