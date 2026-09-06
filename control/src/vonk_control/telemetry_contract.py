"""Shared, bounded telemetry wire and projection models.

The agent reports observations over its authenticated channel.  The
Controller adds node and receive identity when it persists the report.  A
series is deliberately self describing so a consumer never has to infer a
unit, source, or whether a value is measured from a metric name.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TelemetryScope = Literal[
    "node",
    "accelerator",
    "memory",
    "storage",
    "network",
    "runtime",
    "workload",
    "service",
    "benchmark",
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

_KEY = re.compile(r"^[a-z][a-z0-9_.-]{0,95}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SOURCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_MAX_TELEMETRY_BYTES = 16 * 1024**4
_MAX_RATE = 1_000_000_000_000_000.0
_MAX_METRICS_BYTES = 48 * 1024


class TelemetryContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TelemetrySeries(TelemetryContractModel):
    """One sampled or configured metric in canonical units."""

    # Set by the Controller on projection.  It is intentionally optional on
    # the agent payload because the authenticated mTLS identity is the source
    # of truth for the node and cannot be supplied by a producer.
    node_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    key: Annotated[str, Field(min_length=1, max_length=96)]
    scope: TelemetryScope
    device_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    process_id: int | None = Field(default=None, ge=1, le=2**31 - 1)
    process_name: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    interface_name: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    run_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    value: int | float | str | bool | None
    unit: Annotated[str, Field(min_length=1, max_length=32)]
    source: Annotated[str, Field(min_length=1, max_length=128)]
    measurement_kind: TelemetryMeasurementKind
    observed_at: datetime
    # The agent leaves this unset; the Controller fills it from the sample's
    # authenticated receipt time before exposing the series.
    received_at: datetime | None = None
    freshness: TelemetryFreshness = "fresh"
    freshness_threshold_seconds: float = Field(gt=0, le=86_400)
    support_status: TelemetrySupport
    reason: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    aggregation: Annotated[str, Field(min_length=1, max_length=32)]

    @field_validator("key")
    @classmethod
    def key_is_canonical(cls, value: str) -> str:
        if _KEY.fullmatch(value) is None:
            raise ValueError("telemetry metric key is invalid")
        return value

    @field_validator("source")
    @classmethod
    def source_is_canonical(cls, value: str) -> str:
        if _SOURCE.fullmatch(value) is None:
            raise ValueError("telemetry metric source is invalid")
        return value

    @field_validator("aggregation")
    @classmethod
    def aggregation_is_canonical(cls, value: str) -> str:
        if _KEY.fullmatch(value) is None:
            raise ValueError("telemetry aggregation is invalid")
        return value

    @field_validator("value")
    @classmethod
    def finite_numeric_value(cls, value: object) -> object:
        if isinstance(value, float) and (
            not math.isfinite(value) or abs(value) > _MAX_RATE
        ):
            raise ValueError("telemetry series value is invalid")
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and abs(value) > 2**63 - 1
        ):
            raise ValueError("telemetry series value is invalid")
        if isinstance(value, str) and len(value) > 256:
            raise ValueError("telemetry series text value is invalid")
        return value

    @model_validator(mode="after")
    def support_reason_and_scope(self) -> TelemetrySeries:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("telemetry series observation time must be timezone-aware")
        if self.received_at is not None and (
            self.received_at.tzinfo is None or self.received_at.utcoffset() is None
        ):
            raise ValueError("telemetry series receive time must be timezone-aware")
        if self.support_status == "available" and self.reason is not None:
            raise ValueError("available telemetry series cannot have a reason")
        if self.support_status != "available" and self.reason is None:
            raise ValueError("unsupported telemetry series requires a reason")
        if self.scope == "accelerator" and self.device_id is None:
            raise ValueError("accelerator telemetry series requires a device ID")
        if self.scope == "storage" and self.device_id is None:
            raise ValueError("storage telemetry series requires a device ID")
        if self.scope == "network" and self.interface_name is None:
            raise ValueError("network telemetry series requires an interface name")
        if self.scope in {"runtime", "workload", "benchmark"} and self.run_id is None:
            raise ValueError("runtime telemetry series requires a run ID")
        return self


class TelemetryCapability(TelemetryContractModel):
    """Capability inventory, including explicitly unsupported sensors."""

    node_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    key: Annotated[str, Field(min_length=1, max_length=96)]
    scope: TelemetryScope
    device_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    process_id: int | None = Field(default=None, ge=1, le=2**31 - 1)
    process_name: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    interface_name: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    run_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    unit: Annotated[str, Field(min_length=1, max_length=32)]
    source: Annotated[str, Field(min_length=1, max_length=128)]
    measurement_kind: TelemetryMeasurementKind
    supported: bool
    freshness_threshold_seconds: float = Field(gt=0, le=86_400)
    reason: Annotated[str, Field(min_length=1, max_length=256)] | None = None

    @field_validator("key")
    @classmethod
    def key_is_canonical(cls, value: str) -> str:
        if _KEY.fullmatch(value) is None:
            raise ValueError("telemetry capability key is invalid")
        return value

    @field_validator("source")
    @classmethod
    def source_is_canonical(cls, value: str) -> str:
        if _SOURCE.fullmatch(value) is None:
            raise ValueError("telemetry capability source is invalid")
        return value

    @model_validator(mode="after")
    def supported_reason(self) -> TelemetryCapability:
        if self.supported and self.reason is not None:
            raise ValueError("supported telemetry capability cannot have a reason")
        if not self.supported and self.reason is None:
            raise ValueError("unsupported telemetry capability requires a reason")
        if self.scope == "accelerator" and self.device_id is None:
            raise ValueError("accelerator telemetry capability requires a device ID")
        if self.scope == "storage" and self.device_id is None:
            raise ValueError("storage telemetry capability requires a device ID")
        if self.scope == "network" and self.interface_name is None:
            raise ValueError("network telemetry capability requires an interface name")
        return self


class TelemetryProvenance(TelemetryContractModel):
    collector: Annotated[str, Field(min_length=1, max_length=64)]
    collector_version: Annotated[str, Field(min_length=1, max_length=64)]
    host_uptime_seconds: int | None = Field(default=None, ge=0, le=2**63 - 1)
    source_observed_at: datetime | None = None

    @field_validator("collector", "collector_version")
    @classmethod
    def provenance_text(cls, value: str) -> str:
        if _IDENTIFIER.fullmatch(value) is None:
            raise ValueError("telemetry provenance is invalid")
        return value


class TelemetryRuntime(TelemetryContractModel):
    """Controller-owned runtime identity and adapter support summary."""

    run_id: Annotated[str, Field(min_length=1, max_length=128)]
    engine_id: Annotated[str, Field(min_length=1, max_length=128)]
    backend: Annotated[str, Field(min_length=1, max_length=64)]
    version: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    endpoint: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    model: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    model_version: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    recipe_revision: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    context_limit_tokens: int | None = Field(default=None, ge=1, le=2**63 - 1)
    serving_node_ids: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(
        max_length=64
    )
    ranks: list[int] = Field(max_length=64)
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


class TelemetryWorkload(TelemetryContractModel):
    """Sanitized request/job correlation to the actual serving placement."""

    request_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    job_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    run_id: Annotated[str, Field(min_length=1, max_length=128)]
    model: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    recipe_revision: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    engine_id: Annotated[str, Field(min_length=1, max_length=128)]
    state: TelemetryWorkloadState
    origin_node_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    executor_node_ids: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(
        max_length=64
    )
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


class TelemetryMetrics(TelemetryContractModel):
    """Rich per-sample metrics kept alongside legacy scalar columns."""

    schema_version: Literal[2] = 2
    series: list[TelemetrySeries] = Field(max_length=512)
    capabilities: list[TelemetryCapability] = Field(max_length=128)
    runtimes: list[TelemetryRuntime] = Field(max_length=32)
    workloads: list[TelemetryWorkload] = Field(max_length=128)
    provenance: TelemetryProvenance

    @model_validator(mode="after")
    def bounded_and_unique(self) -> TelemetryMetrics:
        identities = [
            (
                item.key,
                item.scope,
                item.device_id,
                item.process_id,
                item.interface_name,
                item.run_id,
            )
            for item in self.series
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("telemetry series identity is duplicated")
        capability_keys = [
            (
                item.key,
                item.scope,
                item.device_id,
                item.process_id,
                item.interface_name,
                item.run_id,
            )
            for item in self.capabilities
        ]
        if len(capability_keys) != len(set(capability_keys)):
            raise ValueError("telemetry capability identity is duplicated")
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > _MAX_METRICS_BYTES:
            raise ValueError("telemetry metrics payload is too large")
        return self


def empty_telemetry_metrics() -> TelemetryMetrics:
    """Build a valid empty payload for historical scalar-only samples."""

    return TelemetryMetrics(
        series=[],
        capabilities=[],
        runtimes=[],
        workloads=[],
        provenance=TelemetryProvenance(
            collector="legacy",
            collector_version="1",
        ),
    )


__all__ = [
    "TelemetryCapability",
    "TelemetryContractModel",
    "TelemetryFreshness",
    "TelemetryMeasurementKind",
    "TelemetryMetrics",
    "TelemetryProvenance",
    "TelemetryRuntime",
    "TelemetrySeries",
    "TelemetrySupport",
    "TelemetryWorkload",
    "empty_telemetry_metrics",
]
