"""Parser for the authenticated agent telemetry envelope.

The envelope keeps the existing wire schema 1 used by enrolled agents.  Its
``metrics`` member is explicitly bound to rich metrics schema 2, so extending
the observation payload cannot be mistaken for an unversioned map.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .contracts import (
    AgentProtocolError,
    canonical_message,
    schema_validator,
)

# The sender batches toward a smaller soft target. This is an authenticated
# transport memory safeguard, not a limit on a valid metrics inventory.
MAX_TELEMETRY_REPORT_BYTES = 16 * 1024 * 1024
MAX_TELEMETRY_SCALAR_STRING_CHARS = 256
MIN_TELEMETRY_SCALAR_INTEGER = -(2**63)
MAX_TELEMETRY_SCALAR_INTEGER = 2**63 - 1


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


@dataclass(frozen=True)
class TelemetryReport:
    schema_version: int
    samples: tuple[Mapping[str, Any], ...]

    @classmethod
    def parse(cls, raw: Any) -> TelemetryReport:
        if not isinstance(raw, Mapping):
            raise AgentProtocolError("telemetry report must be an object")
        samples = raw.get("samples")
        if isinstance(samples, (list, tuple)):
            for sample in samples:
                if not isinstance(sample, Mapping):
                    continue
                metrics = sample.get("metrics")
                if not isinstance(metrics, Mapping):
                    continue
                series_items = metrics.get("series")
                if not isinstance(series_items, (list, tuple)):
                    continue
                for series in series_items:
                    if isinstance(series, Mapping) and "value" in series:
                        try:
                            validate_telemetry_scalar(series["value"])
                        except ValueError as error:
                            raise AgentProtocolError(
                                "telemetry metric value is invalid"
                            ) from error
        errors = list(schema_validator("telemetry-report.schema.json").iter_errors(raw))
        if errors:
            raise AgentProtocolError(
                f"telemetry report schema is invalid: {errors[0].message}"
            )
        encoded = canonical_message(raw)
        if len(encoded) > MAX_TELEMETRY_REPORT_BYTES:
            raise AgentProtocolError("telemetry report is too large")
        document = json.loads(encoded)
        samples = tuple(document["samples"])
        identities = [(sample["boot_id"], sample["sequence"]) for sample in samples]
        if len(identities) != len(set(identities)):
            raise AgentProtocolError("telemetry sample is duplicated")
        observed = [sample["observed_at"] for sample in samples]
        if observed != sorted(observed):
            raise AgentProtocolError("telemetry observations are not ordered")
        previous_by_boot: dict[str, int] = {}
        for sample in samples:
            prior = previous_by_boot.get(sample["boot_id"])
            if prior is not None and sample["sequence"] <= prior:
                raise AgentProtocolError("telemetry sequences are not ordered")
            previous_by_boot[sample["boot_id"]] = sample["sequence"]
        return cls(schema_version=document["schema_version"], samples=samples)

    def document(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "samples": list(self.samples)}


__all__ = [
    "MAX_TELEMETRY_REPORT_BYTES",
    "MAX_TELEMETRY_SCALAR_INTEGER",
    "MAX_TELEMETRY_SCALAR_STRING_CHARS",
    "MIN_TELEMETRY_SCALAR_INTEGER",
    "TelemetryReport",
    "validate_telemetry_scalar",
]
