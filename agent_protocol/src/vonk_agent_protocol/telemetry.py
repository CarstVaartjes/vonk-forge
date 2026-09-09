"""The authenticated agent telemetry wire contract.

This module is the single Python authority for the strict telemetry JSON
graph. The Controller may enrich persisted projections with receipt and node
identity, but those values are not supplied by an agent.
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_core import ValidationError

from .contracts import (
    AgentProtocolError,
    canonical_message,
)
from .wire_model import WireModel

# The sender batches toward a smaller soft target. This is an authenticated
# transport memory safeguard, not a limit on a valid metrics inventory.
MAX_TELEMETRY_REPORT_BYTES = 16 * 1024 * 1024
MAX_TELEMETRY_SCALAR_STRING_CHARS = 256
MIN_TELEMETRY_SCALAR_INTEGER = -(2**63)
MAX_TELEMETRY_SCALAR_INTEGER = 2**63 - 1
MAX_TELEMETRY_CAPACITY_BYTES = 16 * 1024**4
MAX_TELEMETRY_RATE = 1_000_000_000_000_000.0

TelemetryScope = Literal[
    "node", "accelerator", "memory", "storage", "network", "runtime",
    "workload", "service", "benchmark"
]
TelemetrySupport = Literal["available", "unsupported", "unavailable", "stale"]
TelemetryFreshness = Literal["fresh", "delayed", "stale"]
TelemetryMeasurementKind = Literal["measured", "derived", "estimated", "configured"]
TelemetryRunState = Literal[
    "starting", "ready", "running", "queued", "stopped", "failed", "unknown"
]
TelemetryWorkloadState = Literal[
    "queued", "running", "completed", "failed", "cancelled", "unknown"
]
_KEY_PATTERN = r"^[a-z][a-z0-9_.-]{0,95}$"
_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"
_SOURCE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"
_NO_CONTROL_SCHEMA = {"not": {"pattern": r"[\x00-\x1f\x7f]"}}

TelemetryKey = Annotated[
    str,
    Field(
        min_length=1,
        max_length=96,
        pattern=_KEY_PATTERN,
        json_schema_extra=_NO_CONTROL_SCHEMA,
    ),
]
TelemetrySource = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=_SOURCE_PATTERN,
        json_schema_extra=_NO_CONTROL_SCHEMA,
    ),
]
TelemetryIdentifier = Annotated[
    str,
    Field(
        min_length=1,
        max_length=64,
        pattern=_IDENTIFIER_PATTERN,
        json_schema_extra=_NO_CONTROL_SCHEMA,
    ),
]
TelemetryScalar = (
    Annotated[
        int, Field(ge=MIN_TELEMETRY_SCALAR_INTEGER, le=MAX_TELEMETRY_SCALAR_INTEGER)
    ]
    | float
    | Annotated[str, Field(max_length=MAX_TELEMETRY_SCALAR_STRING_CHARS)]
    | bool
    | None
)


def validate_telemetry_scalar(value: Any) -> Any:
    """Validate and return one value from a rich telemetry series.

    The telemetry value is a JSON scalar.  Keep this validator independent of
    Pydantic and JSON Schema so agent parsing and Controller validation share
    exactly the same numeric and text contract.
    """
    if value is None or isinstance(value, bool):
        return value
    if type(value) is int:
        if not MIN_TELEMETRY_SCALAR_INTEGER <= value <= MAX_TELEMETRY_SCALAR_INTEGER:
            raise ValueError("telemetry scalar integer is outside signed64 range")
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("telemetry scalar float must be finite")
        return value
    if type(value) is str:
        if len(value) > MAX_TELEMETRY_SCALAR_STRING_CHARS:
            raise ValueError("telemetry scalar string is too long")
        return value
    raise ValueError("telemetry value must be a JSON scalar")


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


class TelemetryDetails(TelemetryWireModel):
    accelerator_name: str | None = Field(min_length=1, max_length=256)
    accelerator_performance_state: str | None = Field(min_length=1, max_length=32)


class TelemetrySeries(TelemetryWireModel):
    node_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    key: TelemetryKey
    scope: TelemetryScope
    device_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    process_id: int | None = Field(default=None, ge=1, le=2**31 - 1)
    process_name: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    interface_name: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    run_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    value: TelemetryScalar
    unit: Annotated[str, Field(min_length=1, max_length=32)]
    source: TelemetrySource
    measurement_kind: TelemetryMeasurementKind
    observed_at: datetime
    received_at: datetime | None = None
    freshness: TelemetryFreshness = "fresh"
    freshness_threshold_seconds: float = Field(gt=0, le=86_400)
    support_status: TelemetrySupport
    reason: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    aggregation: Annotated[
        str,
        Field(
            min_length=1,
            max_length=32,
            pattern=_KEY_PATTERN,
            json_schema_extra=_NO_CONTROL_SCHEMA,
        ),
    ]
    _parse_observed_at = field_validator("observed_at", "received_at", mode="before")(_rfc3339_datetime)

    @field_validator("value", mode="before")
    @classmethod
    def scalar_value(cls, value: object) -> object:
        return validate_telemetry_scalar(value)

    @model_validator(mode="after")
    def support_reason_and_scope(self) -> TelemetrySeries:
        if self.support_status == "available" and self.reason is not None:
            raise ValueError("available telemetry series cannot have a reason")
        if self.support_status != "available" and self.reason is None:
            raise ValueError("unsupported telemetry series requires a reason")
        if self.scope in {"accelerator", "storage"} and self.device_id is None:
            raise ValueError(f"{self.scope} telemetry series requires a device ID")
        if self.scope == "network" and self.interface_name is None:
            raise ValueError("network telemetry series requires an interface name")
        if self.scope in {"runtime", "workload", "benchmark"} and self.run_id is None:
            raise ValueError("runtime telemetry series requires a run ID")
        return self


class TelemetryCapability(TelemetryWireModel):
    node_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    key: TelemetryKey
    scope: TelemetryScope
    device_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    process_id: int | None = Field(default=None, ge=1, le=2**31 - 1)
    process_name: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    interface_name: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    run_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    unit: Annotated[str, Field(min_length=1, max_length=32)]
    source: TelemetrySource
    measurement_kind: TelemetryMeasurementKind
    supported: bool
    freshness_threshold_seconds: float = Field(gt=0, le=86_400)
    reason: Annotated[str, Field(min_length=1, max_length=256)] | None = None

    @model_validator(mode="after")
    def supported_reason(self) -> TelemetryCapability:
        if self.supported and self.reason is not None:
            raise ValueError("supported telemetry capability cannot have a reason")
        if not self.supported and self.reason is None:
            raise ValueError("unsupported telemetry capability requires a reason")
        if self.scope in {"accelerator", "storage"} and self.device_id is None:
            raise ValueError(f"{self.scope} telemetry capability requires a device ID")
        if self.scope == "network" and self.interface_name is None:
            raise ValueError("network telemetry capability requires an interface name")
        return self


class TelemetryProvenance(TelemetryWireModel):
    collector: TelemetryIdentifier
    collector_version: TelemetryIdentifier
    host_uptime_seconds: int | None = Field(default=None, ge=0, le=2**63 - 1)
    source_observed_at: datetime | None = None
    _parse_source_observed_at = field_validator("source_observed_at", mode="before")(_rfc3339_datetime)


class TelemetryRuntime(TelemetryWireModel):
    run_id: Annotated[str, Field(min_length=1, max_length=128)]
    engine_id: Annotated[str, Field(min_length=1, max_length=128)]
    backend: Annotated[str, Field(min_length=1, max_length=64)]
    version: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    endpoint: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    model: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    model_version: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    recipe_revision: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    context_limit_tokens: int | None = Field(default=None, ge=1, le=2**63 - 1)
    serving_node_ids: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(max_length=64)
    # Rank identity is shared with the compiled placement and native u32 rank.
    ranks: list[Annotated[int, Field(ge=0, le=2**32 - 1)]] = Field(max_length=64)
    readiness: TelemetryRunState
    error: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    adapter: Annotated[str, Field(min_length=1, max_length=64)]
    adapter_version: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    adapter_supported: bool
    adapter_reason: Annotated[str, Field(min_length=1, max_length=256)] | None = None

    @model_validator(mode="after")
    def adapter_reason_consistency(self) -> TelemetryRuntime:
        if self.adapter_supported and self.adapter_reason is not None:
            raise ValueError("supported runtime adapter cannot have a reason")
        if not self.adapter_supported and self.adapter_reason is None:
            raise ValueError("unsupported runtime adapter requires a reason")
        if len(set(self.ranks)) != len(self.ranks):
            raise ValueError("runtime ranks must be unique")
        return self


class TelemetryWorkload(TelemetryWireModel):
    request_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    job_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    run_id: Annotated[str, Field(min_length=1, max_length=128)]
    model: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    recipe_revision: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    engine_id: Annotated[str, Field(min_length=1, max_length=128)]
    state: TelemetryWorkloadState
    origin_node_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    executor_node_ids: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(max_length=64)
    created_at: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    elapsed_seconds: float | None = Field(default=None, ge=0, le=86_400 * 365)
    failure: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    title: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    progress_value: float | None = Field(default=None, ge=0, le=1)
    progress_max: float | None = Field(default=None, gt=0, le=1_000_000)
    eta_seconds: float | None = Field(default=None, ge=0, le=86_400 * 365)
    eta_source: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    _parse_times = field_validator("created_at", "started_at", "ended_at", mode="before")(_rfc3339_datetime)

    @model_validator(mode="after")
    def workload_identity_and_state(self) -> TelemetryWorkload:
        if self.request_id is None and self.job_id is None:
            raise ValueError("telemetry workload requires a request or job ID")
        if len(set(self.executor_node_ids)) != len(self.executor_node_ids):
            raise ValueError("telemetry executor nodes must be unique")
        if self.state == "failed" and self.failure is None:
            raise ValueError("failed telemetry workload requires a sanitized failure")
        if self.progress_value is not None and self.progress_max is None:
            raise ValueError("telemetry progress maximum is required with progress")
        return self


class TelemetryMetrics(TelemetryWireModel):
    schema_version: Literal[2]
    series: list[TelemetrySeries] = Field(max_length=512)
    capabilities: list[TelemetryCapability] = Field(max_length=128)
    runtimes: list[TelemetryRuntime] = Field(max_length=32)
    workloads: list[TelemetryWorkload] = Field(max_length=128)
    provenance: TelemetryProvenance

    @model_validator(mode="after")
    def bounded_and_unique(self) -> TelemetryMetrics:
        def identity(item: TelemetrySeries | TelemetryCapability) -> tuple[object, ...]:
            return (item.key, item.scope, item.device_id, item.process_id, item.interface_name, item.run_id)
        series = [identity(item) for item in self.series]
        capabilities = [identity(item) for item in self.capabilities]
        if len(series) != len(set(series)):
            raise ValueError("telemetry series identity is duplicated")
        if len(capabilities) != len(set(capabilities)):
            raise ValueError("telemetry capability identity is duplicated")
        return self


class TelemetrySample(TelemetryWireModel):
    boot_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    observed_at: datetime
    cpu_utilization_percent: float | None = Field(ge=0, le=100, allow_inf_nan=False)
    load_average_1m: float | None = Field(ge=0, le=1_000_000, allow_inf_nan=False)
    memory_total_bytes: int | None = Field(ge=0, le=MAX_TELEMETRY_CAPACITY_BYTES)
    memory_available_bytes: int | None = Field(ge=0, le=MAX_TELEMETRY_CAPACITY_BYTES)
    disk_total_bytes: int | None = Field(ge=0, le=MAX_TELEMETRY_CAPACITY_BYTES)
    disk_free_bytes: int | None = Field(ge=0, le=MAX_TELEMETRY_CAPACITY_BYTES)
    gpu_utilization_percent: float | None = Field(ge=0, le=100, allow_inf_nan=False)
    gpu_memory_total_bytes: int | None = Field(ge=0, le=MAX_TELEMETRY_CAPACITY_BYTES)
    gpu_memory_free_bytes: int | None = Field(ge=0, le=MAX_TELEMETRY_CAPACITY_BYTES)
    temperature_c: float | None = Field(ge=-100, le=300, allow_inf_nan=False)
    power_watts: float | None = Field(ge=0, le=100_000, allow_inf_nan=False)
    network_receive_bytes_per_second: float | None = Field(ge=0, le=MAX_TELEMETRY_RATE, allow_inf_nan=False)
    network_transmit_bytes_per_second: float | None = Field(ge=0, le=MAX_TELEMETRY_RATE, allow_inf_nan=False)
    gap_samples: int = Field(ge=0, le=MAX_TELEMETRY_SCALAR_INTEGER)
    details: TelemetryDetails
    metrics: TelemetryMetrics
    _parse_observed_at = field_validator("observed_at", mode="before")(_rfc3339_datetime)

    @field_validator("boot_id")
    @classmethod
    def nonzero_boot_id(cls, value: str) -> str:
        if uuid.UUID(value).int == 0:
            raise ValueError("telemetry boot ID cannot be nil")
        return value

    @model_validator(mode="after")
    def internally_consistent(self) -> TelemetrySample:
        for total, available in ((self.memory_total_bytes, self.memory_available_bytes), (self.disk_total_bytes, self.disk_free_bytes), (self.gpu_memory_total_bytes, self.gpu_memory_free_bytes)):
            if (total is None) is not (available is None) or (total is not None and available is not None and available > total):
                raise ValueError("telemetry capacity values are inconsistent")
        return self


class TelemetryRequest(TelemetryWireModel):
    schema_version: Literal[1]
    samples: list[TelemetrySample] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def ordered_unique_samples(self) -> TelemetryRequest:
        previous_observed_at: datetime | None = None
        for sample in self.samples:
            if previous_observed_at is not None and sample.observed_at <= previous_observed_at:
                raise ValueError("telemetry observations are not ordered")
            previous_observed_at = sample.observed_at
        return self

    @classmethod
    def parse(cls, raw: Any) -> TelemetryRequest:
        try:
            encoded = canonical_message(raw)
        except (ValidationError, TypeError, ValueError) as error:
            raise AgentProtocolError(f"telemetry report schema is invalid: {error}") from error
        if len(encoded) > MAX_TELEMETRY_REPORT_BYTES:
            raise AgentProtocolError("telemetry report is too large")
        try:
            return cls.model_validate_json(encoded)
        except ValidationError as error:
            raise AgentProtocolError(f"telemetry report schema is invalid: {error}") from error

    def document(self) -> dict[str, Any]:
        return json.loads(canonical_message(self.model_dump(mode="json")))


__all__ = [
    "MAX_TELEMETRY_CAPACITY_BYTES",
    "MAX_TELEMETRY_RATE",
    "MAX_TELEMETRY_REPORT_BYTES",
    "MAX_TELEMETRY_SCALAR_INTEGER",
    "MAX_TELEMETRY_SCALAR_STRING_CHARS",
    "MIN_TELEMETRY_SCALAR_INTEGER",
    "TelemetryCapability",
    "TelemetryDetails",
    "TelemetryFreshness",
    "TelemetryIdentifier",
    "TelemetryKey",
    "TelemetryMeasurementKind",
    "TelemetryMetrics",
    "TelemetryProvenance",
    "TelemetryRequest",
    "TelemetryRuntime",
    "TelemetrySample",
    "TelemetryScope",
    "TelemetrySeries",
    "TelemetrySource",
    "TelemetrySupport",
    "TelemetryWorkload",
    "TelemetryWorkloadState",
    "validate_telemetry_scalar",
]
