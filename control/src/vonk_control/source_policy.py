"""Fail-closed static policy for recipe Dockerfiles and Compose documents."""

from __future__ import annotations

import json
import re
import shlex
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

import yaml

from .source_bundles import GeneratedSourceBundle

_PINNED_IMAGE = re.compile(r"^[^\s$]+@sha256:([0-9a-f]{64})$")
_NON_ROOT_USER = re.compile(r"^[1-9][0-9]*(?::[1-9][0-9]*)?$")
_COMPOSE_NAMES = frozenset(
    {"compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml"}
)
_HTTPS_URL = re.compile(r"https://[^\s\"'<>]+")


@dataclass(frozen=True, slots=True)
class SourcePolicyFinding:
    code: str
    path: str
    line: int | None
    detail: str


@dataclass(frozen=True, slots=True)
class SourcePolicyReport:
    passed: bool
    source_bundle_sha256: str
    dockerfile: str
    findings: tuple[SourcePolicyFinding, ...]


class SourcePolicyError(ValueError):
    def __init__(self, report: SourcePolicyReport) -> None:
        self.report = report
        super().__init__(
            report.findings[0].detail if report.findings else "source policy failed"
        )


def inspect_build_source_policy(
    recipe: Mapping[str, object], bundle: GeneratedSourceBundle
) -> SourcePolicyReport:
    findings: list[SourcePolicyFinding] = []
    build = recipe.get("build")
    context = build.get("context") if isinstance(build, Mapping) else None
    dockerfile = build.get("dockerfile") if isinstance(build, Mapping) else None
    dockerfile_path = dockerfile if isinstance(dockerfile, str) else "Dockerfile"
    network = build.get("network") if isinstance(build, Mapping) else None
    network_mode = network.get("mode") if isinstance(network, Mapping) else None
    allowed_hosts = (
        frozenset(
            host.lower()
            for host in network.get("hosts", ())
            if isinstance(host, str)
        )
        if isinstance(network, Mapping)
        else frozenset()
    )
    expected = context.get("sha256") if isinstance(context, Mapping) else None
    if expected != bundle.sha256:
        findings.append(
            SourcePolicyFinding(
                "source.digest_mismatch",
                dockerfile_path,
                None,
                "recipe source digest does not match the canonical bundle",
            )
        )
    payload = bundle.files.get(dockerfile_path)
    if payload is None:
        findings.append(
            SourcePolicyFinding(
                "dockerfile.missing",
                dockerfile_path,
                None,
                "recipe Dockerfile is missing from the source bundle",
            )
        )
    else:
        findings.extend(
            _inspect_dockerfile(
                dockerfile_path,
                payload,
                network_mode=network_mode,
                allowed_hosts=allowed_hosts,
            )
        )
    for path, content in bundle.files.items():
        if PurePosixPath(path).name.lower() in _COMPOSE_NAMES:
            findings.extend(_inspect_compose(path, content))
    ordered = tuple(
        sorted(findings, key=lambda item: (item.path, item.line or 0, item.code))
    )
    return SourcePolicyReport(not ordered, bundle.sha256, dockerfile_path, ordered)


def enforce_build_source_policy(
    recipe: Mapping[str, object], bundle: GeneratedSourceBundle
) -> SourcePolicyReport:
    report = inspect_build_source_policy(recipe, bundle)
    if not report.passed:
        raise SourcePolicyError(report)
    return report


def dockerfile_base_images(payload: bytes) -> tuple[dict[str, str], ...]:
    """Return the ordered, unique immutable FROM authorities in a checked file."""

    text = payload.decode("utf-8")
    images: list[dict[str, str]] = []
    seen: set[str] = set()
    for _line, instruction, argument in _dockerfile_instructions(text):
        if instruction.upper() != "FROM":
            continue
        tokens = [
            token for token in argument.split() if not token.startswith("--platform=")
        ]
        reference = tokens[0] if tokens else ""
        if reference == "scratch" or reference in seen:
            continue
        matched = _PINNED_IMAGE.fullmatch(reference)
        if matched is None:
            raise ValueError("Dockerfile base image authority is invalid")
        images.append(
            {
                "manifest_digest": f"sha256:{matched.group(1)}",
                "reference": reference,
            }
        )
        seen.add(reference)
    return tuple(images)


def _inspect_dockerfile(
    path: str,
    payload: bytes,
    *,
    network_mode: object,
    allowed_hosts: frozenset[str],
) -> list[SourcePolicyFinding]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return [
            _finding("dockerfile.invalid_utf8", path, None, "Dockerfile must be UTF-8")
        ]
    findings: list[SourcePolicyFinding] = []
    aliases: set[str] = set()
    final_user: tuple[str, int] | None = None
    saw_from = False
    for line_number, instruction, argument in _dockerfile_instructions(text):
        upper = instruction.upper()
        if "<<" in argument:
            findings.append(
                _finding(
                    "dockerfile.heredoc_forbidden",
                    path,
                    line_number,
                    "Dockerfile heredocs are not accepted",
                )
            )
        if upper == "FROM":
            saw_from = True
            final_user = None
            tokens = argument.split()
            tokens = [token for token in tokens if not token.startswith("--platform=")]
            base = tokens[0] if tokens else ""
            pinned = _PINNED_IMAGE.fullmatch(base)
            if pinned is not None and pinned.group(1) == "0" * 64:
                findings.append(
                    _finding(
                        "dockerfile.base_placeholder",
                        path,
                        line_number,
                        "replace the all-zero base-image placeholder with a verified linux/arm64 digest",
                    )
                )
            elif base != "scratch" and pinned is None:
                findings.append(
                    _finding(
                        "dockerfile.base_unpinned",
                        path,
                        line_number,
                        "every base image must be pinned by sha256 digest",
                    )
                )
            if len(tokens) >= 3 and tokens[-2].lower() == "as":
                aliases.add(tokens[-1])
        elif upper == "USER":
            final_user = (argument.strip(), line_number)
        elif upper == "ADD":
            findings.append(
                _finding(
                    "dockerfile.add_forbidden",
                    path,
                    line_number,
                    "ADD is forbidden; use bounded local COPY",
                )
            )
        elif upper == "ONBUILD":
            findings.append(
                _finding(
                    "dockerfile.onbuild_forbidden",
                    path,
                    line_number,
                    "ONBUILD may hide deferred build instructions",
                )
            )
        elif upper == "RUN":
            lowered = argument.lower()
            for url in _HTTPS_URL.findall(argument):
                host = urllib.parse.urlsplit(url).hostname
                if (
                    host is None
                    or network_mode != "public"
                    or host.lower() not in allowed_hosts
                ):
                    findings.append(
                        _finding(
                            "dockerfile.network_host",
                            path,
                            line_number,
                            "Dockerfile URL host is outside the declared build allowlist",
                        )
                    )
            if re.search(r"--mount\s*=\s*type\s*=\s*(?:secret|ssh)", lowered):
                findings.append(
                    _finding(
                        "dockerfile.secret_mount",
                        path,
                        line_number,
                        "secret and SSH build mounts are forbidden",
                    )
                )
            if "--network=host" in lowered or "--security=insecure" in lowered:
                findings.append(
                    _finding(
                        "dockerfile.build_privilege",
                        path,
                        line_number,
                        "host networking and insecure build execution are forbidden",
                    )
                )
        elif upper == "COPY":
            findings.extend(_inspect_copy(path, line_number, argument, aliases))
    if not saw_from:
        findings.append(
            _finding(
                "dockerfile.from_missing",
                path,
                None,
                "Dockerfile must declare a base stage",
            )
        )
    if final_user is None or _NON_ROOT_USER.fullmatch(final_user[0]) is None:
        findings.append(
            _finding(
                "dockerfile.root_user",
                path,
                final_user[1] if final_user else None,
                "the final image stage must select an explicit numeric non-root user",
            )
        )
    return findings


def _dockerfile_instructions(text: str):
    logical = ""
    start = 0
    for number, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not logical and (not stripped or stripped.startswith("#")):
            continue
        if not logical:
            start = number
        logical += stripped[:-1].rstrip() + " " if stripped.endswith("\\") else stripped
        if stripped.endswith("\\"):
            continue
        instruction, separator, argument = logical.partition(" ")
        if instruction:
            yield start, instruction, argument.strip() if separator else ""
        logical = ""
    if logical:
        instruction, _, argument = logical.partition(" ")
        yield start, instruction, argument.strip()


def _inspect_copy(
    path: str, line: int, argument: str, aliases: set[str]
) -> list[SourcePolicyFinding]:
    findings: list[SourcePolicyFinding] = []
    try:
        tokens = (
            json.loads(argument)
            if argument.lstrip().startswith("[")
            else shlex.split(argument)
        )
    except (json.JSONDecodeError, ValueError):
        return [
            _finding(
                "dockerfile.copy_invalid",
                path,
                line,
                "COPY syntax cannot be reviewed safely",
            )
        ]
    if not isinstance(tokens, list) or not all(
        isinstance(token, str) for token in tokens
    ):
        return [
            _finding(
                "dockerfile.copy_invalid",
                path,
                line,
                "COPY syntax cannot be reviewed safely",
            )
        ]
    sources: list[str] = []
    copies_from_stage = False
    for token in tokens:
        if token.startswith("--from="):
            copies_from_stage = True
            source = token.removeprefix("--from=")
            pinned = _PINNED_IMAGE.fullmatch(source)
            if pinned is not None and pinned.group(1) == "0" * 64:
                findings.append(
                    _finding(
                        "dockerfile.copy_base_placeholder",
                        path,
                        line,
                        "replace the all-zero external COPY placeholder with a verified digest",
                    )
                )
            elif source not in aliases and pinned is None:
                findings.append(
                    _finding(
                        "dockerfile.copy_base_unpinned",
                        path,
                        line,
                        "external COPY stage must be digest-pinned",
                    )
                )
        elif not token.startswith("--"):
            sources.append(token)
    for source in sources[:-1]:
        source_path = PurePosixPath(source)
        if (
            (source.startswith("/") and not copies_from_stage)
            or source.startswith("~")
            or ".." in source_path.parts
            or "$" in source
        ):
            findings.append(
                _finding(
                    "dockerfile.copy_path",
                    path,
                    line,
                    "COPY source must stay within the canonical context",
                )
            )
    return findings


def _inspect_compose(path: str, payload: bytes) -> list[SourcePolicyFinding]:
    if len(payload) > 256 * 1024:
        return [
            _finding(
                "compose.too_large", path, None, "Compose policy input exceeds 256 KiB"
            )
        ]
    try:
        document = yaml.safe_load(payload.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        return [
            _finding(
                "compose.invalid",
                path,
                None,
                "Compose document is not valid UTF-8 YAML",
            )
        ]
    if not isinstance(document, Mapping) or not isinstance(
        document.get("services"), Mapping
    ):
        return [
            _finding(
                "compose.invalid",
                path,
                None,
                "Compose document must contain a services mapping",
            )
        ]
    findings: list[SourcePolicyFinding] = []
    for service_name, raw_service in document["services"].items():
        service_path = f"{path}:services.{service_name}"
        if not isinstance(raw_service, Mapping):
            findings.append(
                _finding(
                    "compose.service_invalid",
                    service_path,
                    None,
                    "Compose service must be a mapping",
                )
            )
            continue
        if raw_service.get("privileged") is True:
            findings.append(
                _finding(
                    "compose.privileged",
                    service_path,
                    None,
                    "privileged Compose services are forbidden",
                )
            )
        for key in ("network_mode", "pid", "ipc", "uts", "userns_mode"):
            if raw_service.get(key) == "host":
                findings.append(
                    _finding(
                        "compose.host_namespace",
                        service_path,
                        None,
                        f"host {key} is forbidden",
                    )
                )
        if raw_service.get("cap_add"):
            findings.append(
                _finding(
                    "compose.capabilities",
                    service_path,
                    None,
                    "added Linux capabilities are forbidden",
                )
            )
        if raw_service.get("devices"):
            findings.append(
                _finding(
                    "compose.devices",
                    service_path,
                    None,
                    "Compose device passthrough is not a build input",
                )
            )
        options = raw_service.get("security_opt", [])
        if isinstance(options, str):
            options = [options]
        if isinstance(options, list) and any(
            "unconfined" in str(item).lower() for item in options
        ):
            findings.append(
                _finding(
                    "compose.unconfined",
                    service_path,
                    None,
                    "unconfined security profiles are forbidden",
                )
            )
        findings.extend(
            _inspect_compose_volumes(service_path, raw_service.get("volumes", []))
        )
    return findings


def _inspect_compose_volumes(path: str, volumes: object) -> list[SourcePolicyFinding]:
    if not isinstance(volumes, list):
        return [
            _finding(
                "compose.volumes_invalid", path, None, "Compose volumes must be a list"
            )
        ]
    findings: list[SourcePolicyFinding] = []
    for volume in volumes:
        if isinstance(volume, str):
            source = volume.split(":", 1)[0]
            host_bind = source.startswith(("/", ".", "~"))
            socket = "docker.sock" in volume or "podman.sock" in volume
        elif isinstance(volume, Mapping):
            source = str(volume.get("source", ""))
            host_bind = volume.get("type") == "bind" or source.startswith(
                ("/", ".", "~")
            )
            socket = "docker.sock" in source or "podman.sock" in source
        else:
            findings.append(
                _finding(
                    "compose.volumes_invalid",
                    path,
                    None,
                    "Compose volume syntax is invalid",
                )
            )
            continue
        if host_bind or socket:
            findings.append(
                _finding(
                    "compose.host_bind",
                    path,
                    None,
                    "host bind mounts and container-engine sockets are forbidden",
                )
            )
    return findings


def _finding(
    code: str, path: str, line: int | None, detail: str
) -> SourcePolicyFinding:
    return SourcePolicyFinding(code, path, line, detail)
