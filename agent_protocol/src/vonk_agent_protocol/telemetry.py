"""The authenticated agent telemetry wire contract: a flat sample of host scalars."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import Field, PrivateAttr, field_validator, model_validator
from pydantic_core import ValidationError

from .contracts import (
    AgentProtocolError,
    CanonicalUUID,
    canonical_message,
)
from .gpu_clock import GpuClockObservation
from .optional_evidence import (
    EvidenceGroup,
    OptionalEvidenceModel,
    fail_open_on_optional_evidence,
)
from .reason_codes import AgentEvidenceCode
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
    offset = parsed.utcoffset()
    if parsed.tzinfo is None or offset is None:
        raise ValueError("telemetry time must include a timezone")
    if offset.total_seconds() != 0:
        raise ValueError("telemetry time must be UTC")
    return parsed


class GpuUnavailableReason(StrEnum):
    COMMAND_FAILED = "gpu.command-failed"
    COMMAND_TIMEOUT = "gpu.command-timeout"
    INVALID_OUTPUT = "gpu.invalid-output"
    REQUIRES_RESET = "gpu.requires-reset"
    UNSUPPORTED_METRICS = "gpu.unsupported-metrics"


class TelemetryWireModel(WireModel):
    pass


_COUNTER_MAX = 2**63 - 1


def _counter() -> Any:
    """A monotonic whole-number reading (per boot, or per engine process)."""

    return Field(
        default=None, ge=0, le=_COUNTER_MAX, exclude_if=lambda value: value is None
    )


def _seconds() -> Any:
    """A non-negative duration or sum of durations, in seconds."""

    return Field(
        default=None,
        ge=0,
        le=1e12,
        allow_inf_nan=False,
        exclude_if=lambda value: value is None,
    )


class EngineFormat(StrEnum):
    """Which engine's metric names the reading was taken from."""

    VLLM = "vllm"
    SGLANG = "sglang"
    TENSORFOLD = "tensorfold"
    LLAMA_CPP = "llama-cpp"


class EngineReading(OptionalEvidenceModel, TelemetryWireModel):
    """One run's engine metrics, read by the monitor from the engine's own
    ``/metrics`` on loopback (protocol 4.4).

    The counters are cumulative since the engine process started (the
    Controller's Prometheus computes rates from them; a reset is a restart).
    The ``*_seconds_sum`` / ``*_seconds_count`` pairs are a histogram's sum and
    count, so a mean over any window is the difference of the sums over the
    difference of the counts. There are no percentiles: the gateway carries
    those. An invalid reading is dropped alone.
    """

    EVIDENCE_GROUPS: ClassVar[tuple[EvidenceGroup, ...]] = tuple(
        EvidenceGroup(AgentEvidenceCode.TELEMETRY_READING_DROPPED, paths)
        for paths in (
            (("prompt_tokens",),),
            (("generation_tokens",),),
            (("cached_prompt_tokens",),),
            (("preemptions",),),
            (
                ("spec_decode_draft_tokens",),
                ("spec_decode_accepted_tokens",),
            ),
            (("requests_running",),),
            (("requests_waiting",),),
            (("kv_cache_usage_ratio",),),
            (
                ("ttft_seconds_sum",),
                ("ttft_seconds_count",),
            ),
            (
                ("time_per_output_token_seconds_sum",),
                ("time_per_output_token_seconds_count",),
            ),
            (
                ("prefill_seconds_sum",),
                ("prefill_seconds_count",),
            ),
            (
                ("decode_seconds_sum",),
                ("decode_seconds_count",),
            ),
            (
                ("e2e_seconds_sum",),
                ("e2e_seconds_count",),
            ),
        )
    )

    run_id: CanonicalUUID
    format: EngineFormat
    prompt_tokens: int | None = _counter()
    generation_tokens: int | None = _counter()
    cached_prompt_tokens: int | None = _counter()
    preemptions: int | None = _counter()
    spec_decode_draft_tokens: int | None = _counter()
    spec_decode_accepted_tokens: int | None = _counter()
    requests_running: int | None = _counter()
    requests_waiting: int | None = _counter()
    kv_cache_usage_ratio: float | None = Field(
        default=None,
        ge=0,
        le=1,
        allow_inf_nan=False,
        exclude_if=lambda value: value is None,
    )
    ttft_seconds_sum: float | None = _seconds()
    ttft_seconds_count: int | None = _counter()
    time_per_output_token_seconds_sum: float | None = _seconds()
    time_per_output_token_seconds_count: int | None = _counter()
    prefill_seconds_sum: float | None = _seconds()
    prefill_seconds_count: int | None = _counter()
    decode_seconds_sum: float | None = _seconds()
    decode_seconds_count: int | None = _counter()
    e2e_seconds_sum: float | None = _seconds()
    e2e_seconds_count: int | None = _counter()

    @model_validator(mode="after")
    def spec_decode_accepts_no_more_than_it_drafted(self) -> EngineReading:
        drafted = self.spec_decode_draft_tokens
        accepted = self.spec_decode_accepted_tokens
        if (drafted is None) is not (accepted is None) or (
            drafted is not None and accepted is not None and accepted > drafted
        ):
            raise ValueError("speculative decoding counters are inconsistent")
        return self

    drop_invalid_optional_evidence = fail_open_on_optional_evidence()


class HostResourceSample(TelemetryWireModel):
    """RAM and filesystem readings, owned by the inventory store."""

    memory_total_bytes: int | None = Field(ge=0, le=MAX_TELEMETRY_CAPACITY_BYTES)
    memory_available_bytes: int | None = Field(ge=0, le=MAX_TELEMETRY_CAPACITY_BYTES)
    disk_total_bytes: int | None = Field(ge=0, le=MAX_TELEMETRY_CAPACITY_BYTES)
    disk_free_bytes: int | None = Field(ge=0, le=MAX_TELEMETRY_CAPACITY_BYTES)

    @model_validator(mode="after")
    def capacity_pairs(self) -> HostResourceSample:
        for total, free in (
            (self.memory_total_bytes, self.memory_available_bytes),
            (self.disk_total_bytes, self.disk_free_bytes),
        ):
            if (total is None) != (free is None) or (
                total is not None and free is not None and free > total
            ):
                raise ValueError("telemetry capacity values are inconsistent")
        return self


class _TelemetryReadings(OptionalEvidenceModel, HostResourceSample):
    """The host readings of one sample, which the Controller stores per node. The
    capacity scalars are the core; the optional readings (4.1: temperature, CPU
    clocks; 4.3: GPU power, uptime, GPU process memory; 4.4: CPU time, load,
    network, fabric, disk, OOM kills, board, NVMe and NIC temperatures, fan) are
    evidence dropped when invalid.

    ``gpu_power_watts`` is the GPU's draw, not the host's. ``uptime_seconds`` is
    whole seconds since boot. ``gpu_process_memory_bytes`` is the memory the
    driver says GPU compute processes hold; it is reported only for a
    shared-pool accelerator, where it is part of ``memory_total - memory_available``.

    4.4 counters are cumulative since boot (a lower value than before is a
    reboot). ``cpu_busy_seconds`` and ``cpu_total_seconds`` are the ``/proc/stat``
    jiffies over USER_HZ. The LAN counters are the default-route interface's.
    The fabric counters are the RDMA port counters of ACTIVE ports (RoCE traffic
    bypasses the netdev counters). The disk counters are for the disk that holds
    the model store. ``board_temperature_max_c`` is the hottest unlabelled
    ``acpitz`` zone, never "CPU". ``nic_temperature_c`` is the hottest ConnectX
    ASIC and ``fan_rpm_max`` the fastest board fan.
    """

    EVIDENCE_GROUPS: ClassVar[tuple[EvidenceGroup, ...]] = (
        EvidenceGroup(AgentEvidenceCode.TELEMETRY_READING_DROPPED, (("gpu_clock",),)),
        EvidenceGroup(
            AgentEvidenceCode.TELEMETRY_READING_DROPPED,
            (("gpu_temperature_c",), ("gpu_unavailable_reason",)),
        ),
        EvidenceGroup(
            AgentEvidenceCode.TELEMETRY_READING_DROPPED,
            (
                ("cpu_frequency_avg_mhz",),
                ("cpu_frequency_min_mhz",),
                ("cpu_frequency_max_mhz",),
            ),
        ),
        EvidenceGroup(
            AgentEvidenceCode.TELEMETRY_READING_DROPPED, (("gpu_power_watts",),)
        ),
        EvidenceGroup(
            AgentEvidenceCode.TELEMETRY_READING_DROPPED, (("uptime_seconds",),)
        ),
        EvidenceGroup(
            AgentEvidenceCode.TELEMETRY_READING_DROPPED,
            (("gpu_process_memory_bytes",),),
        ),
        *(
            EvidenceGroup(AgentEvidenceCode.TELEMETRY_READING_DROPPED, paths)
            for paths in (
                (
                    ("cpu_busy_seconds",),
                    ("cpu_total_seconds",),
                ),
                (("load_average_1m",),),
                (
                    ("network_lan_rx_bytes",),
                    ("network_lan_tx_bytes",),
                ),
                (
                    ("fabric_rx_bytes",),
                    ("fabric_tx_bytes",),
                ),
                (("fabric_ports_active",),),
                (
                    ("disk_read_bytes",),
                    ("disk_written_bytes",),
                ),
                (("oom_kills",),),
                (("board_temperature_max_c",),),
                (("nvme_temperature_c",),),
                (("nic_temperature_c",),),
                (("fan_rpm_max",),),
            )
        ),
    )

    gpu_clock: GpuClockObservation | None = None

    boot_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )
    observed_at: datetime
    gpu_unavailable_reason: GpuUnavailableReason | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
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
    gpu_power_watts: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
        exclude_if=lambda value: value is None,
    )
    uptime_seconds: int | None = Field(
        default=None, ge=0, le=2**32 - 1, exclude_if=lambda value: value is None
    )
    gpu_process_memory_bytes: int | None = Field(
        default=None,
        ge=0,
        le=MAX_TELEMETRY_CAPACITY_BYTES,
        exclude_if=lambda value: value is None,
    )
    cpu_busy_seconds: float | None = _seconds()
    cpu_total_seconds: float | None = _seconds()
    load_average_1m: float | None = Field(
        default=None,
        ge=0,
        le=10_000,
        allow_inf_nan=False,
        exclude_if=lambda value: value is None,
    )
    network_lan_rx_bytes: int | None = _counter()
    network_lan_tx_bytes: int | None = _counter()
    fabric_rx_bytes: int | None = _counter()
    fabric_tx_bytes: int | None = _counter()
    fabric_ports_active: int | None = Field(
        default=None, ge=0, le=16, exclude_if=lambda value: value is None
    )
    disk_read_bytes: int | None = _counter()
    disk_written_bytes: int | None = _counter()
    oom_kills: int | None = _counter()
    board_temperature_max_c: int | None = Field(
        default=None, ge=0, le=150, exclude_if=lambda value: value is None
    )
    nvme_temperature_c: int | None = Field(
        default=None, ge=0, le=150, exclude_if=lambda value: value is None
    )
    nic_temperature_c: int | None = Field(
        default=None, ge=0, le=150, exclude_if=lambda value: value is None
    )
    fan_rpm_max: int | None = Field(
        default=None, ge=0, le=100_000, exclude_if=lambda value: value is None
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
    def internally_consistent(self) -> _TelemetryReadings:
        for total, available in (
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

    drop_invalid_optional_evidence = fail_open_on_optional_evidence()


#: The base is private so the wire export (public classes only) does not make
#: Rust generate a structural conversion for it; ``TelemetrySample`` carries
#: the same fields. The Controller derives its views from this name.
TelemetryReadings = _TelemetryReadings


class _EngineEntries(TelemetryWireModel):
    """The ``engines`` list of a sample. An entry that is invalid is dropped
    alone (counted), and a repeated ``run_id`` keeps its first entry. It is a
    base listed after the readings, so its validator runs inside the sample's
    optional-evidence recovery, which must stay outermost."""

    engines: list[EngineReading] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    _dropped_engine_entries: int = PrivateAttr(default=0)

    @property
    def dropped_engine_entries(self) -> int:
        """Engine entries this sample lost because they were invalid or repeated."""

        return self._dropped_engine_entries

    @model_validator(mode="wrap")
    @classmethod
    def keep_valid_engines(cls, data: Any, handler: Any) -> Any:
        dropped = 0
        entries = data.get("engines") if isinstance(data, Mapping) else None
        if isinstance(entries, list):
            kept: list[EngineReading] = []
            for entry in entries:
                try:
                    reading = EngineReading.model_validate_json(
                        canonical_message(entry)
                    )
                except (ValidationError, TypeError, ValueError):
                    dropped += 1
                    continue
                if any(reading.run_id == earlier.run_id for earlier in kept):
                    dropped += 1
                else:
                    kept.append(reading)
            data = {**data, "engines": kept}
        sample = handler(data)
        sample._dropped_engine_entries = dropped
        return sample


class TelemetrySample(TelemetryReadings, _EngineEntries):
    """One sample: the host readings plus the engine readings (4.4). The
    Controller stores engine readings in their own table and never puts them on
    the fleet point. ``engines`` is optional evidence: a value that is not a
    list is dropped (each list entry is already judged on its own)."""

    EVIDENCE_GROUPS: ClassVar[tuple[EvidenceGroup, ...]] = (
        *TelemetryReadings.EVIDENCE_GROUPS,
        EvidenceGroup(AgentEvidenceCode.TELEMETRY_READING_DROPPED, (("engines",),)),
    )

    drop_invalid_optional_evidence = fail_open_on_optional_evidence()


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
    "EngineFormat",
    "EngineReading",
    "TelemetryReadings",
    "TelemetryRequest",
    "TelemetrySample",
]
