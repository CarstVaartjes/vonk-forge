"""Documents the NAS and Spark setup programs read and exchange.

``NasInstallTemplate`` is what ``scripts/build-nas-compose-bundle`` writes and
``vonk-nas-setup`` reads.  ``SitePorts`` is the one port list the Spark firewall
and the Controller share.  ``SparkApplyEnvelope`` is the frame the unprivileged
phase of ``vonk-spark-setup`` hands its privileged phase.  Each model here is
the one definition; the Rust types are generated from it.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .wire_model import Digest, WireModel, typed_tag

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
    provisioner_private_jwk: str
    provisioner_public_jwk: str
    ca_config: str
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


# -- Spark setup apply frame -----------------------------------------------------


class SparkHostMapping(WireModel):
    address: str
    hostnames: list[str]


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
