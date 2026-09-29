"""The authenticated agent telemetry wire contract: a flat sample of host scalars."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from pydantic import Field, field_validator, model_validator
from pydantic_core import ValidationError

from .contracts import (
    AgentProtocolError,
    canonical_message,
)
from .wire_model import WireModel

# Authenticated transport memory safeguard.
MAX_TELEMETRY_REPORT_BYTES = 1024 * 1024
MAX_TELEMETRY_CAPACITY_BYTES = 16 * 1024**4


def _rfc3339_datetime(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or ("T" not in value and "t" not in value):
        raise ValueError("telemetry time must be an RFC 3339 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("telemetry time must be an RFC 3339 string") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("telemetry time must include a timezone")
    if parsed.utcoffset().total_seconds() != 0:
        raise ValueError("telemetry time must be UTC")
    return parsed


class TelemetryWireModel(WireModel):
    pass


class TelemetrySample(TelemetryWireModel):
    boot_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )
    observed_at: datetime
    memory_total_bytes: int | None = Field(ge=0, le=MAX_TELEMETRY_CAPACITY_BYTES)
    memory_available_bytes: int | None = Field(ge=0, le=MAX_TELEMETRY_CAPACITY_BYTES)
    disk_total_bytes: int | None = Field(ge=0, le=MAX_TELEMETRY_CAPACITY_BYTES)
    disk_free_bytes: int | None = Field(ge=0, le=MAX_TELEMETRY_CAPACITY_BYTES)
    gpu_utilization_percent: float | None = Field(ge=0, le=100, allow_inf_nan=False)
    gpu_memory_total_bytes: int | None = Field(ge=0, le=MAX_TELEMETRY_CAPACITY_BYTES)
    gpu_memory_free_bytes: int | None = Field(ge=0, le=MAX_TELEMETRY_CAPACITY_BYTES)
    # Optional additions (protocol 4.1): older agents omit them. CPU clocks are
    # whole MHz from cpufreq; max is the hardware limit, not the current cap.
    gpu_temperature_c: int | None = Field(
        default=None, ge=0, le=150, exclude_if=lambda value: value is None
    )
    cpu_frequency_avg_mhz: int | None = Field(
        default=None, ge=1, le=20_000, exclude_if=lambda value: value is None
    )
    cpu_frequency_min_mhz: int | None = Field(
        default=None, ge=1, le=20_000, exclude_if=lambda value: value is None
    )
    cpu_frequency_max_mhz: int | None = Field(
        default=None, ge=1, le=20_000, exclude_if=lambda value: value is None
    )
    _parse_observed_at = field_validator("observed_at", mode="before")(
        _rfc3339_datetime
    )

    @field_validator("boot_id")
    @classmethod
    def nonzero_boot_id(cls, value: str) -> str:
        if uuid.UUID(value).int == 0:
            raise ValueError("telemetry boot ID cannot be nil")
        return value

    @model_validator(mode="after")
    def internally_consistent(self) -> TelemetrySample:
        for total, available in (
            (self.memory_total_bytes, self.memory_available_bytes),
            (self.disk_total_bytes, self.disk_free_bytes),
            (self.gpu_memory_total_bytes, self.gpu_memory_free_bytes),
        ):
            if (total is None) is not (available is None) or (
                total is not None and available is not None and available > total
            ):
                raise ValueError("telemetry capacity values are inconsistent")
        if (
            self.cpu_frequency_avg_mhz is not None
            and self.cpu_frequency_min_mhz is not None
            and self.cpu_frequency_min_mhz > self.cpu_frequency_avg_mhz
        ):
            raise ValueError("telemetry CPU frequencies are inconsistent")
        return self


class TelemetryRequest(TelemetryWireModel):
    samples: list[TelemetrySample] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def ordered_unique_samples(self) -> TelemetryRequest:
        previous_observed_at: datetime | None = None
        for sample in self.samples:
            if (
                previous_observed_at is not None
                and sample.observed_at <= previous_observed_at
            ):
                raise ValueError("telemetry observations are not ordered")
            previous_observed_at = sample.observed_at
        return self

    @classmethod
    def parse(cls, raw: Any) -> TelemetryRequest:
        try:
            encoded = canonical_message(raw)
        except (ValidationError, TypeError, ValueError) as error:
            raise AgentProtocolError(
                f"telemetry report schema is invalid: {error}"
            ) from error
        if len(encoded) > MAX_TELEMETRY_REPORT_BYTES:
            raise AgentProtocolError("telemetry report is too large")
        try:
            return cls.model_validate_json(encoded)
        except ValidationError as error:
            raise AgentProtocolError(
                f"telemetry report schema is invalid: {error}"
            ) from error

    def document(self) -> dict[str, Any]:
        return json.loads(canonical_message(self.model_dump(mode="json")))


__all__ = [
    "MAX_TELEMETRY_CAPACITY_BYTES",
    "MAX_TELEMETRY_REPORT_BYTES",
    "TelemetryRequest",
    "TelemetrySample",
]
