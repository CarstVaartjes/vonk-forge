"""Documents the NAS and Spark setup programs read and exchange.

``NasInstallTemplate`` is what ``scripts/build-nas-compose-bundle`` writes and
``vonk-nas-setup`` reads.  ``SitePorts`` is the one port list the Spark firewall
and the Controller share.  ``SparkApplyEnvelope`` is the frame the unprivileged
phase of ``vonk-spark-setup`` hands its privileged phase.  Each model here is
the one definition; the Rust types are generated from it.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .wire_model import Digest, WireEnum, WireModel, typed_tag

U16 = Annotated[int, Field(ge=0, le=65535)]
U32 = Annotated[int, Field(ge=0, le=2**32 - 1)]
EnvName = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]*$")]
#: Bytes carried through JSON: lower-case hex, two digits per byte.
HexBytes = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{2})*$")]


SitePort = Annotated[int, Field(ge=1024, le=65535)]


class SitePorts(WireModel):
    """The ports the Spark firewall authorises, read by Controller and setup."""

    endpoint_host_ports: list[SitePort] = Field(min_length=1)
    host_endpoint_ports: list[SitePort] = Field(min_length=1)
    rendezvous_port: SitePort

    @model_validator(mode="after")
    def ports_are_unique(self) -> SitePorts:
        for ports in (self.endpoint_host_ports, self.host_endpoint_ports):
            if len(set(ports)) != len(ports):
                raise ValueError("site ports must not repeat")
        return self


# -- NAS install template ------------------------------------------------------


class NasInternalValue(WireModel):
    """An environment value the installer writes without asking."""

    env: str
    value: str


class NasRequiredValuePrompt(WireModel):
    env: str
    prompt: str
    default: str | None = None
    validation: Literal[
        "non_empty", "ipv4", "cidr_list", "optional_cidr_list", "hostname"
    ] = "non_empty"


class NasSecretPrompt(WireModel):
    file: str
    prompt: str
    optional: bool = False
    #: Lab mode leaves this secret empty instead of asking for it.
    secure_remote_only: bool = False


class NasGroupReadableSecrets(WireModel):
    """Secrets read by capability-free containers through one supplementary group."""

    gid: U32
    files: list[str]


class NasInstallModes(WireModel):
    prompt: str
    default: str = "secure-remote"
    lab_value: str
    secure_remote_value: str
    #: Values that replace the secure-remote-only prompts in lab mode.
    lab_values: list[NasInternalValue]


class NasRandomTextRequest(WireModel):
    file: str
    bytes: U32
    prefix: str | None = None


class NasEd25519KeyRequest(WireModel):
    file: str


class NasPostgresUrlRequest(WireModel):
    file: str
    password_file: str
    scheme: str
    username: str
    host: str
    port: Annotated[int, Field(ge=0, le=65535)]
    database: str


class NasGeneratedSecrets(WireModel):
    random_text: list[NasRandomTextRequest] = Field(default_factory=list)
    ed25519_pkcs8_pem: list[NasEd25519KeyRequest] = Field(default_factory=list)
    postgres_urls: list[NasPostgresUrlRequest] = Field(default_factory=list)


class NasStepCaControllerFiles(WireModel):
    root_certificate: str
    intermediate_certificate: str
    intermediate_private_key: str
    controller_server_certificate: str
    controller_server_private_key: str
    provisioner_public_jwk: str
    password: str


class NasStepCaControllerRequest(WireModel):
    #: The control hostname; the enrollment, agent and registry names are fixed
    #: prefixes of it, so one value names the whole controller.
    hostname_env: str
    provisioner_name: str
    password_bytes: U32
    files: NasStepCaControllerFiles


class NasHermesPrompt(WireModel):
    env: str
    prompt: str
    enabled_value: str
    disabled_value: str


class NasInstallTemplate(WireModel):
    """The canonical NAS Compose template with its install questions."""

    schema_version: Literal[2]
    docker_compose_yaml: str
    preflight: list[str] = Field(default_factory=list)
    internal_values: list[NasInternalValue] = Field(default_factory=list)
    required_values: list[NasRequiredValuePrompt] = Field(default_factory=list)
    #: Keys the installer never writes but an upgrade keeps when the operator set
    #: them; any other key not named by this payload is dropped.
    optional_values: list[str] = Field(default_factory=list)
    secrets: list[NasSecretPrompt] = Field(default_factory=list)
    generated_secrets: NasGeneratedSecrets | None = None
    group_readable_secrets: NasGroupReadableSecrets | None = None
    install_modes: NasInstallModes | None = None
    step_ca_controller: NasStepCaControllerRequest | None = None
    hermes: NasHermesPrompt | None = None

    @model_validator(mode="after")
    def validate_setup_semantics(self) -> NasInstallTemplate:
        """Reject unsafe effects before a setup consumer touches the filesystem."""
        if not self.docker_compose_yaml or "\0" in self.docker_compose_yaml:
            raise ValueError("docker compose payload is empty or malformed")
        if any(
            not item.strip() or len(item.encode()) > 512 or not single_line(item)
            for item in self.preflight
        ):
            raise ValueError("preflight checklist is malformed")
        environment: set[str] = set()
        secret_names: set[str] = set()

        def env(name: str) -> None:
            validate_environment_name(name)
            if name in environment:
                raise ValueError(f"duplicate environment key {name}")
            environment.add(name)

        def secret(name: str) -> None:
            validate_secret_name(name)
            if any(
                name == old or name.startswith(old + "/") or old.startswith(name + "/")
                for old in secret_names
            ):
                raise ValueError(f"conflicting secret path {name}")
            secret_names.add(name)

        for item in self.internal_values:
            env(item.env)
            if "\0" in item.value:
                raise ValueError(f"internal value for {item.env} contains NUL")
        for item in self.required_values:
            env(item.env)
            if not item.prompt.strip() or (
                item.default is not None
                and not valid_required_value(item.default, item.validation)
            ):
                raise ValueError(f"invalid prompt or default for {item.env}")
        for name in self.optional_values:
            validate_environment_name(name)
        for item in self.secrets:
            secret(item.file)
            if not item.prompt.strip():
                raise ValueError(f"prompt for {item.file} is empty")
        if self.hermes:
            env(self.hermes.env)
            if (
                not self.hermes.prompt.strip()
                or self.hermes.enabled_value == self.hermes.disabled_value
                or not single_line(self.hermes.enabled_value)
                or not single_line(self.hermes.disabled_value)
            ):
                raise ValueError("Hermes choices are invalid")
        if self.install_modes:
            modes = self.install_modes
            if (
                not modes.prompt.strip()
                or not modes.lab_value.strip()
                or not modes.secure_remote_value.strip()
                or modes.lab_value == modes.secure_remote_value
                or modes.default not in (modes.lab_value, modes.secure_remote_value)
            ):
                raise ValueError("install mode choices are invalid")
            lab_names: set[str] = set()
            for item in modes.lab_values:
                validate_environment_name(item.env)
                if item.env in lab_names or "\0" in item.value:
                    raise ValueError(f"invalid lab environment value {item.env}")
                lab_names.add(item.env)
        if self.generated_secrets:
            generated = self.generated_secrets
            for item in generated.random_text:
                secret(item.file)
                if not 16 <= item.bytes <= 128 or (
                    item.prefix is not None
                    and re.fullmatch(r"[A-Za-z0-9_.~-]{1,32}", item.prefix) is None
                ):
                    raise ValueError(f"invalid entropy request for {item.file}")
            for item in generated.ed25519_pkcs8_pem:
                secret(item.file)
            for item in generated.postgres_urls:
                secret(item.file)
                validate_secret_name(item.password_file)
                if item.password_file not in secret_names:
                    raise ValueError(f"undeclared password file {item.password_file}")
                if (
                    re.fullmatch(r"[a-z][a-z0-9+.-]*", item.scheme) is None
                    or not item.port
                    or any(
                        not value or not single_line(value)
                        for value in (item.username, item.host, item.database)
                    )
                ):
                    raise ValueError(f"invalid PostgreSQL URL request for {item.file}")
        if self.step_ca_controller:
            request = self.step_ca_controller
            if (
                not request.provisioner_name.strip()
                or not single_line(request.provisioner_name)
                or not 16 <= request.password_bytes <= 128
                or request.hostname_env not in environment
            ):
                raise ValueError("Step CA/controller request is incomplete")
            for name in request.files.model_dump().values():
                secret(name)
        if self.group_readable_secrets:
            group = self.group_readable_secrets
            if (
                not group.gid
                or not group.files
                or len(set(group.files)) != len(group.files)
            ):
                raise ValueError("secret group needs a non-root gid and unique files")
            for name in group.files:
                validate_secret_name(name)
                if name not in secret_names:
                    raise ValueError(f"group-readable secret {name} is undeclared")
        return self


def single_line(value: str) -> bool:
    return not any(character in value for character in "\0\r\n")


def validate_environment_name(name: str) -> None:
    if re.fullmatch(r"[A-Z_][A-Z0-9_]*", name) is None:
        raise ValueError(f"invalid environment key {name}")


def validate_secret_name(name: str) -> None:
    if not name or any(
        part in ("", ".", "..") or re.fullmatch(r"[a-z0-9_.-]+", part) is None
        for part in name.split("/")
    ):
        raise ValueError(f"invalid secret filename {name}")


def valid_required_value(value: str, validation: str) -> bool:
    if not single_line(value):
        return False
    if validation == "non_empty":
        return bool(value)
    if validation == "hostname":
        return valid_hostname(value)
    try:
        if validation == "ipv4":
            return isinstance(ipaddress.ip_address(value), ipaddress.IPv4Address)
        if validation in ("cidr_list", "optional_cidr_list"):
            if not value:
                return validation == "optional_cidr_list"
            for item in value.split(","):
                if "/" not in item:
                    return False
                ipaddress.ip_network(item.strip(), strict=False)
            return True
    except ValueError:
        return False
    return False


def valid_hostname(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 253
        and all(
            len(label) <= 63
            and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
            for label in value.split(".")
        )
    )


# -- Spark setup apply frame -----------------------------------------------------


class SparkHostMapping(WireModel):
    address: str
    hostnames: list[str]


class SparkInstallationState(WireEnum):
    FRESH = "fresh"
    CONFIGURED_UNPAIRED = "unpaired-v1"
    RECOVERING = "recovering-v1"
    EXISTING = "paired-v1"


class SparkInstallationObservation(WireModel):
    state: SparkInstallationState


class SparkFirewallConfig(WireModel):
    nas_management_ip: Annotated[str, Field(json_schema_extra={"format": "ip"})]
    node_management_ip: Annotated[str, Field(json_schema_extra={"format": "ip"})]
    node_fabric_ip: Annotated[str, Field(json_schema_extra={"format": "ip"})]
    peer_fabric_ip: Annotated[str, Field(json_schema_extra={"format": "ip"})]
    endpoint_host_ports: list[U16]
    host_endpoint_ports: list[U16]
    rendezvous_port: U16
    fabric_bandwidth_mbps: Annotated[int, Field(ge=0, le=2**64 - 1)]


class SparkApplyFresh(WireModel):
    operation: Literal["fresh"] = typed_tag()
    enrollment_url: str
    controller_url: str
    ca_sha256: Digest
    ca_pem: HexBytes
    node_id: str
    pairing_token: str
    host_mapping: SparkHostMapping | None = None
    firewall: SparkFirewallConfig
    helper_authority: HexBytes


class SparkApplyPair(WireModel):
    operation: Literal["pair"] = typed_tag()
    enrollment_url: str
    ca_sha256: Digest
    pairing_token: str


class SparkApplyReenroll(WireModel):
    operation: Literal["reenroll"] = typed_tag()
    enrollment_url: str
    ca_sha256: Digest
    pairing_token: str


class SparkApplyRecover(WireModel):
    operation: Literal["recover"] = typed_tag()


class SparkApplyUpgrade(WireModel):
    operation: Literal["upgrade"] = typed_tag()


type SparkApplyOperation = Annotated[
    SparkApplyFresh
    | SparkApplyPair
    | SparkApplyReenroll
    | SparkApplyRecover
    | SparkApplyUpgrade,
    Field(discriminator="operation"),
]


class SparkApplyEnvelope(WireModel):
    """What the privileged phase of the Spark setup applies, in one frame."""

    schema_version: Literal[1]
    caller_uid: U32
    release_manifest: HexBytes
    release_signature: HexBytes
    plan: SparkApplyOperation
    repair_firewall: SparkFirewallConfig | None = None
    repair_ca_pem: HexBytes | None = None
    repair_helper_authority: HexBytes | None = None
