"""Contracts for fleet qualification campaign cli."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from importlib.resources import files as package_files
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from ..cli_states import ENDED_STATES, OPERATOR_WAIT_STATES
from ..fleet_qualification import (
    QualificationObservationUnknown,
)
from ..qualification_fixtures import FixtureRegistry

AUTHORITY_SCHEMA = "qualification-authority-v5.schema.json"
MANIFEST_SCHEMA = "qualification-campaign-manifest-v2.schema.json"
PROFILE_LABEL = "qualification-authority"
# Recovery modes in the order the campaign exercises them: a rank loss leaves
# the group running, a host restart takes every selected Spark down.
FAILURE_MODES = ("single-host-restart", "dual-rank-loss-recovery", "dual-host-restart")
_TERMINAL_APPLICATIONS = frozenset({*ENDED_STATES, *OPERATOR_WAIT_STATES})
_PASSED = frozenset({"passed", "covered"})


@dataclass(frozen=True, slots=True)
class RecoveryRef:
    failure_mode: str
    representative: str
    role: str


@dataclass(frozen=True, slots=True)
class Row:
    key: str
    sequence: int
    content_sha256: str
    node_count: int
    interface: str
    recovery: tuple[RecoveryRef, ...]
    licenses: tuple[Mapping[str, object], ...]


@dataclass(frozen=True, slots=True)
class Batch:
    batch_id: str
    sequence: int
    mode: str
    recipes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Campaign:
    authority_id: str
    rows: Mapping[str, Row]
    batches: tuple[Batch, ...]
    fixtures: FixtureRegistry
    timeout_seconds: float
    poll_seconds: float


@dataclass(frozen=True, slots=True)
class Lane:
    row: Row
    node_ids: tuple[str, ...]
    alias: str
    kind: str


@cache
def _validator(name: str) -> Draft202012Validator:
    raw = package_files("cluster_profiles").joinpath("schemas", name).read_bytes()
    return Draft202012Validator(json.loads(raw))


def _read_contract(path: Path, schema: str, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        raise QualificationObservationUnknown(f"{label} is not yet readable") from error
    error = next(_validator(schema).iter_errors(value), None)
    if error is not None:
        location = "/".join(str(part) for part in error.absolute_path)
        raise QualificationObservationUnknown(
            f"{label} contract is not yet observed at /{location}"
        )
    return value


def load_campaign(manifest_path: Path) -> Campaign:
    manifest = _read_contract(manifest_path, MANIFEST_SCHEMA, "campaign manifest")
    base = manifest_path.resolve().parent
    authority = _read_contract(
        base / manifest["qualification_authority"],
        AUTHORITY_SCHEMA,
        "qualification authority",
    )
    fixtures = FixtureRegistry.load(base / manifest["fixture_manifest"])
    options = manifest.get("options", {})
    rows = {
        raw["key"]: Row(
            key=raw["key"],
            sequence=raw["sequence"],
            content_sha256=raw["content_sha256"],
            node_count=raw["node_count"],
            interface=raw["interface"],
            recovery=tuple(
                RecoveryRef(
                    ref["failure_mode"], ref["representative_recipe"], ref["role"]
                )
                for ref in raw["recovery_coverage_refs"]
            ),
            licenses=tuple(raw["model_license_refs"]),
        )
        for raw in authority["recipes"]
    }
    batches = tuple(
        Batch(
            raw["id"],
            raw["sequence"],
            raw["mode"],
            tuple(
                item["recipe"]
                for item in sorted(raw["assignments"], key=lambda item: item["lane"])
            ),
        )
        for raw in sorted(authority["batches"], key=lambda item: item["sequence"])
    )
    if any(key not in rows for batch in batches for key in batch.recipes):
        raise QualificationObservationUnknown(
            "campaign batch membership is not yet observed"
        )
    return Campaign(
        authority_id=authority["authority_id"],
        rows=rows,
        batches=batches,
        fixtures=fixtures,
        timeout_seconds=float(options.get("operation_timeout_seconds", 86_400)),
        poll_seconds=float(options.get("poll_interval_seconds", 5)),
    )


class ResultsLog:
    """One JSON line per recipe or batch result; the latest line wins."""

    def __init__(self, path: Path, authority_id: str) -> None:
        self.path = path
        self.authority_id = authority_id

    def entries(self) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        entries: list[dict[str, Any]] = []
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue  # a torn final line from an interrupted write
            if (
                isinstance(entry, dict)
                and entry.get("authority_id") == self.authority_id
            ):
                entries.append(entry)
        return entries

    def append(self, **entry: object) -> dict[str, object]:
        record = {
            "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "authority_id": self.authority_id,
            **entry,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        return record

    def latest(self, step: str, recipe: str, failure_mode: str | None = None) -> str:
        result = "pending"
        for entry in self.entries():
            if (
                entry.get("step") == step
                and entry.get("recipe") == recipe
                and entry.get("failure_mode") == failure_mode
            ):
                result = str(entry.get("status"))
        return result

    def stopped(self, batch_id: str) -> bool:
        return any(
            entry.get("step") == "stop"
            and entry.get("batch") == batch_id
            and entry.get("status") == "passed"
            for entry in self.entries()
        )
