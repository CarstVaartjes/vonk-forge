"""Parser for the authenticated agent telemetry envelope.

The envelope keeps the existing wire schema 1 used by enrolled agents.  Its
``metrics`` member is explicitly bound to rich metrics schema 2, so extending
the observation payload cannot be mistaken for an unversioned map.
"""

from __future__ import annotations

import json
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


@dataclass(frozen=True)
class TelemetryReport:
    schema_version: int
    samples: tuple[Mapping[str, Any], ...]

    @classmethod
    def parse(cls, raw: Any) -> TelemetryReport:
        if not isinstance(raw, Mapping):
            raise AgentProtocolError("telemetry report must be an object")
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


__all__ = ["TelemetryReport"]
