"""Authenticated schema-1 inventory evidence reported by an agent."""

from __future__ import annotations

import ipaddress
from datetime import datetime
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from .wire_model import WireModel

Capability = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")]
MemoryPool = Literal["shared", "separate"]
# "fabric" is an RDMA/ConnectX port (the QSFP cable between Sparks); it never
# carries the NAS route, unlike the general-purpose "wired" RJ45 port.
NetworkInterfaceKind = Literal["wired", "wifi", "fabric", "other"]
InterfaceName = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,14}$")]
RuntimeVersion = Annotated[
    str, Field(min_length=1, max_length=256, pattern=r"^[\x00-\x7f]+$")
]


def _strict_json_datetime(value: object) -> object:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError("inventory observed_at is invalid") from error
    return value


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


class InventoryRequest(WireModel):
    """Canonical Controller body and Rust agent inventory wire shape."""

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
    nvidia_driver_version: RuntimeVersion
    container_runtime_version: RuntimeVersion

    @field_validator("observed_at", mode="before")
    @classmethod
    def parse_observed_at(cls, value: object) -> object:
        return _strict_json_datetime(value)

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
