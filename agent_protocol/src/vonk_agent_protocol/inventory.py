"""Authenticated schema-1 inventory evidence reported by an agent."""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from .wire_model import WireModel

_CAPABILITY = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")


def _strict_json_datetime(value: object) -> object:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError("inventory observed_at is invalid") from error
    return value


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
    artifact_store_read_only: bool
    capabilities: list[str] = Field(max_length=64)
    fabric_address: str | None = Field(default=None, max_length=45)
    fabric_bandwidth_mbps: int | None = Field(default=None, ge=1, le=1_000_000)
    nvidia_driver_version: str = Field(min_length=1, max_length=256)
    container_runtime_version: str = Field(min_length=1, max_length=256)

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
            or (self.fabric_address is None) != (self.fabric_bandwidth_mbps is None)
            or len(self.capabilities) != len(set(self.capabilities))
            or any(_CAPABILITY.fullmatch(item) is None for item in self.capabilities)
        ):
            raise ValueError("inventory evidence is inconsistent")
        if self.fabric_address is not None:
            try:
                address = ipaddress.ip_address(self.fabric_address)
            except ValueError as error:
                raise ValueError("inventory fabric address is invalid") from error
            if str(address) != self.fabric_address:
                raise ValueError("inventory fabric address is invalid")
        if any(
            not value or len(value) > 256 or not value.isascii()
            for value in (
                self.nvidia_driver_version,
                self.container_runtime_version,
            )
        ):
            raise ValueError("inventory runtime evidence is invalid")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("inventory observed_at must be timezone-aware")
        return self
