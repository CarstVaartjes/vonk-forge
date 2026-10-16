"""Authenticated schema-1 inventory evidence reported by an agent."""

from __future__ import annotations

import ipaddress
from datetime import datetime
from typing import Annotated, ClassVar, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from .optional_evidence import (
    EvidenceGroup,
    OptionalEvidenceModel,
    fail_open_on_optional_evidence,
)
from .reason_codes import AgentEvidenceCode
from .wire_model import WireModel, strict_json_datetime

Capability = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")]
MemoryPool = Literal["shared", "separate"]
# "fabric" is an RDMA/ConnectX port (the QSFP cable between Sparks); it never
# carries the NAS route, unlike the general-purpose "wired" RJ45 port.
# "tunnel" is a virtual overlay (Tailscale, WireGuard): a NAS route through it is
# reported as such and is never a Wi-Fi warning.
NetworkInterfaceKind = Literal["wired", "wifi", "fabric", "tunnel", "other"]
InterfaceName = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,14}$")]
RuntimeVersion = Annotated[
    str, Field(min_length=1, max_length=256, pattern=r"^[\x00-\x7f]+$")
]
#: An installation directory's name on a Spark: the Controller's installation id.
InstallationId = Annotated[
    str,
    Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"),
]
#: The most installation directories one inventory names (the agent reports
#: the first ones in name order; the rest after those are gone).
MAX_REPORTED_INSTALLATIONS = 4096


class NetworkInterface(WireModel):
    """One NIC as the agent reads it from sysfs."""

    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, allow_inf_nan=False
    )

    name: InterfaceName
    kind: NetworkInterfaceKind
    # Negotiated speed; None while there is no link or the driver reports none.
    link_speed_mbps: int | None = Field(default=None, ge=1, le=1_000_000)
    carrier: bool


class HostSettings(WireModel):
    """The Spark host settings the platform keeps, as the agent observes them.

    A Spark runs headless: a desktop costs about 1 GB of the memory recipes
    count on. Its GPU clock is capped so sustained load cannot power it off.
    The privileged helper corrects drift; this is what is left.
    """

    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, allow_inf_nan=False
    )

    # The display manager (a desktop session) is running.
    display_manager_active: bool
    # The default boot target is graphical.target, which starts the desktop.
    graphical_default_target: bool
    # The GPU clock ceiling unit (vonk-forge-gpu-clock-cap.service) is active
    # this boot. None: the agent predates it or could not observe it (unknown).
    gpu_clock_cap_active: bool | None = None


class InventoryRequest(OptionalEvidenceModel, WireModel):
    """Canonical Controller body and Rust agent inventory wire shape.

    The mandatory core is the capacity, pool, capability and version evidence.
    The network interfaces, the NAS route, the fabric pair, the host settings
    and the installation directories are optional evidence: when one is
    inconsistent it is dropped (and named in ``evidence_warnings``) instead of
    refusing the core.
    """

    EVIDENCE_GROUPS: ClassVar[tuple[EvidenceGroup, ...]] = (
        EvidenceGroup(
            AgentEvidenceCode.INVENTORY_NAS_ROUTE_DROPPED, (("nas_route_interface",),)
        ),
        EvidenceGroup(
            AgentEvidenceCode.INVENTORY_NETWORK_DROPPED, (("network_interfaces",),)
        ),
        EvidenceGroup(
            AgentEvidenceCode.INVENTORY_HOST_SETTINGS_DROPPED, (("host_settings",),)
        ),
        EvidenceGroup(
            AgentEvidenceCode.INVENTORY_INSTALLATIONS_DROPPED, (("installations",),)
        ),
        EvidenceGroup(
            AgentEvidenceCode.INVENTORY_FABRIC_DROPPED,
            (("fabric_address",), ("fabric_bandwidth_mbps",)),
        ),
    )

    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, allow_inf_nan=False
    )

    schema_version: Literal[1]
    observed_at: datetime
    disk_total_bytes: int = Field(ge=0, le=16 * 1024**4)
    disk_free_bytes: int = Field(ge=0, le=16 * 1024**4)
    host_memory_total_bytes: int = Field(ge=0, le=16 * 1024**4)
    host_memory_free_bytes: int = Field(ge=0, le=16 * 1024**4)
    gpu_memory_total_bytes: int = Field(ge=0, le=16 * 1024**4)
    gpu_memory_free_bytes: int = Field(ge=0, le=16 * 1024**4)
    gpu_count: int = Field(ge=0, le=64)
    memory_pool: MemoryPool
    artifact_store_read_only: bool
    capabilities: list[Capability] = Field(max_length=64)
    fabric_address: str | None = Field(default=None, max_length=45)
    fabric_bandwidth_mbps: int | None = Field(default=None, ge=1, le=1_000_000)
    # None: the agent predates this evidence (unknown); []: it reported none.
    network_interfaces: list[NetworkInterface] | None = Field(
        default=None, max_length=16
    )
    # Interface the kernel routes the NAS address through, when determinable.
    nas_route_interface: InterfaceName | None = None
    # None: the agent predates this evidence or could not observe it (unknown).
    host_settings: HostSettings | None = None
    # The installation directories on the Spark, by name. None: the agent
    # predates this evidence or could not list them (unknown); the Controller
    # then answers no verdict and nothing is removed.
    installations: list[InstallationId] | None = Field(
        default=None, max_length=MAX_REPORTED_INSTALLATIONS
    )
    nvidia_driver_version: RuntimeVersion
    container_runtime_version: RuntimeVersion

    @field_validator("observed_at", mode="before")
    @classmethod
    def parse_observed_at(cls, value: object) -> object:
        return strict_json_datetime(value)

    @model_validator(mode="after")
    def internally_consistent(self) -> InventoryRequest:
        if (
            self.disk_free_bytes > self.disk_total_bytes
            or self.host_memory_free_bytes > self.host_memory_total_bytes
            or self.gpu_memory_free_bytes > self.gpu_memory_total_bytes
            or (self.memory_pool == "shared" and self.gpu_count == 0)
            or (self.fabric_address is None) != (self.fabric_bandwidth_mbps is None)
            or len(self.capabilities) != len(set(self.capabilities))
        ):
            raise ValueError("inventory evidence is inconsistent")
        names = [item.name for item in self.network_interfaces or []]
        if len(names) != len(set(names)) or (
            self.nas_route_interface is not None
            and self.nas_route_interface not in names
        ):
            raise ValueError("inventory network evidence is inconsistent")
        if self.fabric_address is not None:
            try:
                address = ipaddress.ip_address(self.fabric_address)
            except ValueError as error:
                raise ValueError("inventory fabric address is invalid") from error
            if str(address) != self.fabric_address:
                raise ValueError("inventory fabric address is invalid")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("inventory observed_at must be timezone-aware")
        return self

    drop_invalid_optional_evidence = fail_open_on_optional_evidence()


class InventoryVerdict(WireModel):
    """The Controller's answer to an inventory that named its installations.

    The Controller is the authority for which installations exist. Of the
    directories the Spark named, it lists those no installation record owns
    (none has the id, or its record is uninstalled): the agent removes such a
    directory a day after the Controller first said so, unless a later verdict
    tracks it again or a run on the Spark names it. An inventory without
    ``installations`` gets no verdict (204).
    """

    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, allow_inf_nan=False
    )

    untracked_installations: list[InstallationId] = Field(
        max_length=MAX_REPORTED_INSTALLATIONS
    )
