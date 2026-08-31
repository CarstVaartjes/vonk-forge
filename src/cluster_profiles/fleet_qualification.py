"""Safe, resumable controller-only qualification of public recipe catalogs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .qualification_fixtures import (
    FixtureError,
    FixtureRegistry,
    RecipeFixture,
    validate_outputs,
)

_TERMINAL_OPERATION_STATES = frozenset(
    {"cancelled", "canceled", "completed", "failed", "succeeded"}
)
_SUCCESS_OPERATION_STATES = frozenset({"completed", "succeeded"})
_JOB_ADAPTERS = frozenset(
    {"artifact-job", "audio-job", "image-job", "mesh-job", "video-job"}
)
_CONTROLLER_DISK_FLOOR_BYTES = 10_000_000_000
_NODE_ID = re.compile(r"^spk_[0-9a-f]{32}$")
_EU_JURISDICTIONS = frozenset(
    {
        "AT",
        "BE",
        "BG",
        "HR",
        "CY",
        "CZ",
        "DE",
        "DK",
        "EE",
        "ES",
        "FI",
        "FR",
        "GR",
        "HU",
        "IE",
        "IT",
        "LT",
        "LU",
        "LV",
        "MT",
        "NL",
        "PL",
        "PT",
        "RO",
        "SE",
        "SI",
        "SK",
    }
)


class QualificationError(RuntimeError):
    """The runner cannot continue without violating a safety invariant."""


class ControllerClient(Protocol):
    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        extra_headers: Mapping[str, str] | None = None,
        query: Mapping[str, object] | None = None,
    ) -> dict[str, object]: ...

    def upload_file(
        self,
        path: str,
        source: Path,
        *,
        media_type: str,
        expected_sha256: str,
        expected_size: int,
    ) -> dict[str, object]: ...

    def download_file(
        self,
        path: str,
        destination: Path,
        *,
        media_type: str,
        expected_sha256: str,
        expected_size: int,
        overwrite: bool,
    ) -> dict[str, object]: ...


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _strict_json_loads(content: bytes | str) -> object:
    def reject_constant(value: str) -> object:
        raise ValueError(f"non-finite JSON number: {value}")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(
        content,
        parse_constant=reject_constant,
        object_pairs_hook=unique_object,
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise QualificationError(f"{label} must be an object")
    return value


def _list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise QualificationError(f"{label} must be an array")
    return value


@dataclass(frozen=True, slots=True)
class Blocker:
    classification: str
    code: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {
            "classification": self.classification,
            "code": self.code,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class RunnerOptions:
    jurisdiction: str | None = None
    cleanup: str = "stop"
    operation_timeout_seconds: float = 7_200
    poll_interval_seconds: float = 5
    selected_recipes: frozenset[str] = frozenset()
    allowed_node_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        jurisdiction = self.jurisdiction
        if jurisdiction is not None and (
            len(jurisdiction) != 2
            or not jurisdiction.isascii()
            or not jurisdiction.isalpha()
        ):
            raise QualificationError(
                "jurisdiction must be an uppercase ISO alpha-2 code"
            )
        if jurisdiction is not None and jurisdiction != jurisdiction.upper():
            raise QualificationError("jurisdiction must be uppercase")
        if self.cleanup not in {"none", "stop", "uninstall"}:
            raise QualificationError("cleanup must be none, stop, or uninstall")
        if not 1 <= self.operation_timeout_seconds <= 86_400:
            raise QualificationError("operation timeout must be between 1 and 86400")
        if not 0.1 <= self.poll_interval_seconds <= 60:
            raise QualificationError("poll interval must be between 0.1 and 60")
        if any(
            not isinstance(node_id, str) or _NODE_ID.fullmatch(node_id) is None
            for node_id in self.allowed_node_ids
        ):
            raise QualificationError("node pins must be exact controller Spark IDs")
        if self.allowed_node_ids and not self.selected_recipes:
            raise QualificationError("node-pinned campaigns require explicit recipes")


class EvidenceLedger:
    """Append-only hash-chained JSONL evidence with durable writes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.records = self._read()

    def _read(self) -> list[dict[str, object]]:
        if not self.path.exists():
            return []
        metadata = self.path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise QualificationError("qualification ledger must be a regular file")
        getuid = getattr(os, "getuid", None)
        if getuid is not None and metadata.st_uid != getuid():
            raise QualificationError("qualification ledger owner is invalid")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise QualificationError("qualification ledger permissions are too broad")
        records: list[dict[str, object]] = []
        previous = "0" * 64
        with self.path.open("rb") as source:
            for line_number, raw in enumerate(source, 1):
                if not raw.endswith(b"\n"):
                    raise QualificationError(
                        "qualification ledger has a partial final record"
                    )
                try:
                    value = _strict_json_loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                    raise QualificationError(
                        f"qualification ledger record {line_number} is invalid"
                    ) from error
                record = _object(value, "qualification ledger record")
                supplied = record.get("record_sha256")
                unsigned = {
                    key: item for key, item in record.items() if key != "record_sha256"
                }
                expected = _digest(unsigned)
                if (
                    record.get("sequence") != line_number
                    or record.get("previous_sha256") != previous
                    or supplied != expected
                ):
                    raise QualificationError(
                        f"qualification ledger record {line_number} failed integrity validation"
                    )
                records.append(dict(record))
                previous = expected
        return records

    def append(
        self,
        event: str,
        *,
        plan_digest: str,
        recipe: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        previous = str(self.records[-1]["record_sha256"]) if self.records else "0" * 64
        unsigned: dict[str, object] = {
            "schema_version": 1,
            "sequence": len(self.records) + 1,
            "recorded_at": _utc_now(),
            "event": event,
            "plan_digest": plan_digest,
            "recipe": recipe,
            "payload": dict(payload or {}),
            "previous_sha256": previous,
        }
        record = {**unsigned, "record_sha256": _digest(unsigned)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise QualificationError("qualification ledger cannot be opened safely")
        descriptor = os.open(
            self.path,
            os.O_APPEND
            | os.O_CREAT
            | os.O_WRONLY
            | getattr(os, "O_CLOEXEC", 0)
            | no_follow,
            0o600,
        )
        try:
            metadata = os.fstat(descriptor)
            getuid = getattr(os, "getuid", None)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or (getuid is not None and metadata.st_uid != getuid())
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise QualificationError("qualification ledger is not private")
            encoded = _canonical(record) + b"\n"
            offset = 0
            while offset < len(encoded):
                offset += os.write(descriptor, encoded[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self.records.append(record)
        return record

    def recipe_records(self, plan_digest: str, recipe: str) -> list[dict[str, object]]:
        return [
            item
            for item in self.records
            if item.get("plan_digest") == plan_digest and item.get("recipe") == recipe
        ]

    def completed_recipes(self, plan_digest: str) -> set[str]:
        return {
            str(item["recipe"])
            for item in self.records
            if item.get("plan_digest") == plan_digest
            and item.get("event") == "recipe.succeeded"
            and isinstance(item.get("recipe"), str)
        }


def load_policy(path: Path | None) -> dict[str, Blocker]:
    if path is None:
        return {}
    try:
        value = _strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise QualificationError("qualification policy is unreadable") from error
    policy = _object(value, "qualification policy")
    if policy.get("schema_version") != 1:
        raise QualificationError("qualification policy schema_version must be 1")
    blocked = _object(policy.get("blocked_recipes", {}), "blocked_recipes")
    result: dict[str, Blocker] = {}
    for key, raw in blocked.items():
        if not isinstance(key, str) or "/" not in key:
            raise QualificationError("blocked recipe keys must be publisher/slug")
        entry = _object(raw, f"blocked recipe {key}")
        classification = entry.get("classification")
        detail = entry.get("detail")
        if classification not in {"legal", "license", "manual", "security"}:
            raise QualificationError(f"blocked recipe {key} classification is invalid")
        if not isinstance(detail, str) or not 1 <= len(detail) <= 512:
            raise QualificationError(f"blocked recipe {key} detail is invalid")
        result[key] = Blocker(str(classification), "operator.policy_block", detail)
    return result


def _territorial_restrictions(
    recipe: Mapping[str, object],
) -> Mapping[str, object] | None:
    # The controller projection is expected to expose the resolved model license as
    # model_license. Accept license as well to remain compatible with catalog documents.
    for field in ("model_license", "license"):
        license_value = recipe.get(field)
        if not isinstance(license_value, Mapping):
            continue
        restrictions = license_value.get("territorial_restrictions")
        if isinstance(restrictions, Mapping):
            return restrictions
    visual = recipe.get("visual_recipe")
    if isinstance(visual, Mapping):
        return _territorial_restrictions(visual)
    return None


def legal_blockers(
    recipe: Mapping[str, object], jurisdiction: str | None
) -> list[Blocker]:
    restrictions = _territorial_restrictions(recipe)
    if restrictions is None:
        return []
    denied = restrictions.get("denied_jurisdictions")
    if (
        not isinstance(denied, list)
        or not denied
        or any(
            not isinstance(item, str) or len(item) != 2 or item != item.upper()
            for item in denied
        )
    ):
        return [
            Blocker(
                "license",
                "license.territorial_restrictions_invalid",
                "Resolved model license restrictions are malformed; qualification fails closed.",
            )
        ]
    notice = restrictions.get("notice")
    detail = (
        str(notice)[:512]
        if isinstance(notice, str) and notice
        else "The model license restricts deployment by territory."
    )
    if jurisdiction is None:
        return [
            Blocker(
                "license",
                "license.jurisdiction_required",
                "Operator jurisdiction is required for this restricted model.",
            )
        ]
    if jurisdiction in denied or ("EU" in denied and jurisdiction in _EU_JURISDICTIONS):
        return [Blocker("legal", "license.territory_denied", detail)]
    return []


def _fleet_nodes(fleet: Mapping[str, object]) -> list[Mapping[str, object]]:
    result = []
    for raw in _list(fleet.get("nodes"), "fleet nodes"):
        if not isinstance(raw, Mapping):
            raise QualificationError("fleet node must be an object")
        connection = raw.get("connection")
        if (
            isinstance(connection, Mapping)
            and connection.get("online_state") == "online"
        ):
            result.append(raw)
    return result


def _selected_fields(
    value: object, fields: tuple[str, ...]
) -> dict[str, object | None] | None:
    if not isinstance(value, Mapping):
        return None
    return {field: value.get(field) for field in fields}


def _fleet_fingerprint(fleet: Mapping[str, object]) -> dict[str, object]:
    """Select stable placement evidence and exclude generated ages/timestamps."""
    rows: list[dict[str, object]] = []
    for raw in _list(fleet.get("nodes"), "fleet nodes"):
        node = _object(raw, "fleet node")
        telemetry = node.get("telemetry")
        sample = telemetry.get("sample") if isinstance(telemetry, Mapping) else None
        rows.append(
            {
                "id": node.get("id"),
                "lifecycle": node.get("lifecycle"),
                "labels": node.get("labels"),
                "connection": _selected_fields(
                    node.get("connection"),
                    (
                        "agent_state",
                        "certificate_state",
                        "online_state",
                        "offline_reason",
                        "last_seen_at",
                    ),
                ),
                "inventory": _selected_fields(
                    node.get("inventory"),
                    (
                        "observed_at",
                        "disk_free_bytes",
                        "host_memory_free_bytes",
                        "gpu_memory_free_bytes",
                        "gpu_count",
                        "artifact_store_read_only",
                        "capabilities",
                        "fabric_address",
                        "fabric_bandwidth_mbps",
                        "nvidia_driver_version",
                        "container_runtime_version",
                    ),
                ),
                "telemetry": _selected_fields(
                    sample,
                    (
                        "boot_id",
                        "sequence",
                        "observed_at",
                        "memory_available_bytes",
                        "disk_free_bytes",
                        "gpu_memory_free_bytes",
                    ),
                ),
            }
        )
    rows.sort(key=lambda item: str(item.get("id")))
    return {
        "authority_revision": fleet.get("authority_revision"),
        "event_cursor": fleet.get("event_cursor"),
        "nodes": rows,
    }


def _node_available_memory(node: Mapping[str, object]) -> int | None:
    telemetry = node.get("telemetry")
    sample = telemetry.get("sample") if isinstance(telemetry, Mapping) else None
    if isinstance(sample, Mapping):
        available = sample.get("memory_available_bytes")
        if (
            isinstance(available, int)
            and not isinstance(available, bool)
            and available >= 0
        ):
            return available
    inventory = node.get("inventory")
    if isinstance(inventory, Mapping):
        available = inventory.get("host_memory_free_bytes")
        if (
            isinstance(available, int)
            and not isinstance(available, bool)
            and available >= 0
        ):
            return available
    return None


def _node_available_disk(node: Mapping[str, object]) -> int | None:
    telemetry = node.get("telemetry")
    sample = telemetry.get("sample") if isinstance(telemetry, Mapping) else None
    if isinstance(sample, Mapping):
        available = sample.get("disk_free_bytes")
        if (
            isinstance(available, int)
            and not isinstance(available, bool)
            and available >= 0
        ):
            return available
    inventory = node.get("inventory")
    if isinstance(inventory, Mapping):
        available = inventory.get("disk_free_bytes")
        if (
            isinstance(available, int)
            and not isinstance(available, bool)
            and available >= 0
        ):
            return available
    return None


def _node_allocatable_disk(node: Mapping[str, object]) -> int | None:
    free = _node_available_disk(node)
    reservations = node.get("reservations")
    reserved = (
        reservations.get("disk_bytes", 0) if isinstance(reservations, Mapping) else 0
    )
    if (
        not isinstance(free, int)
        or not isinstance(reserved, int)
        or isinstance(reserved, bool)
        or reserved < 0
    ):
        return None
    return max(0, free - reserved)


def _capacity_candidate_signature(
    candidate: Mapping[str, object],
) -> dict[str, object]:
    raw_node_ids = candidate.get("node_ids")
    raw_nodes = candidate.get("nodes")
    if (
        not isinstance(raw_node_ids, list)
        or not raw_node_ids
        or any(not isinstance(node_id, str) for node_id in raw_node_ids)
        or not isinstance(raw_nodes, list)
    ):
        raise QualificationError("placement candidate lacks exact rank-ordered nodes")
    role_by_node: dict[str, str] = {}
    rank_by_node: dict[str, int] = {}
    for raw_node in raw_nodes:
        node = _object(raw_node, "placement node")
        node_id = node.get("node_id")
        role = node.get("role")
        rank = node.get("rank")
        if (
            not isinstance(node_id, str)
            or not isinstance(role, str)
            or node_id in role_by_node
        ):
            raise QualificationError("placement candidate role/rank mapping is invalid")
        if rank is None and node_id in raw_node_ids:
            rank = raw_node_ids.index(node_id)
        if not isinstance(rank, int) or isinstance(rank, bool):
            raise QualificationError("placement candidate role/rank mapping is invalid")
        role_by_node[node_id] = role
        rank_by_node[node_id] = rank
    if set(role_by_node) != set(raw_node_ids):
        raise QualificationError("placement candidate nodes and role mapping differ")
    installation_ids = candidate.get("installation_ids", [])
    if not isinstance(installation_ids, list) or any(
        not isinstance(value, str) for value in installation_ids
    ):
        raise QualificationError("placement candidate installation IDs are invalid")
    return {
        "node_ids": sorted(raw_node_ids),
        "builder_node_id": raw_node_ids[0],
        "installation_ids": sorted(installation_ids),
        "role_by_node": dict(sorted(role_by_node.items())),
        "rank_by_node": dict(sorted(rank_by_node.items())),
    }


def _recipe_key(recipe: Mapping[str, object]) -> str:
    publisher = recipe.get("publisher")
    slug = recipe.get("slug")
    if not isinstance(publisher, str) or not isinstance(slug, str):
        raise QualificationError("public recipe identity is invalid")
    return f"{publisher}/{slug}"


def _local_recipe_id(recipe: Mapping[str, object]) -> str | None:
    local = recipe.get("local")
    if not isinstance(local, Mapping) or local.get("status") != "current":
        return None
    recipe_id = local.get("recipe_id")
    return recipe_id if isinstance(recipe_id, str) else None


def _artifact_identities(value: Mapping[str, object] | None) -> list[dict[str, object]]:
    if value is None:
        return []
    projected = value.get("artifact_identities")
    if isinstance(projected, list):
        result: list[dict[str, object]] = []
        for raw in projected:
            artifact = _object(raw, "recipe artifact identity")
            artifact_id = artifact.get("artifact_id")
            identity_sha256 = artifact.get("identity_sha256")
            download_bytes = artifact.get("download_bytes")
            installed_bytes = artifact.get("installed_bytes")
            roles = artifact.get("roles")
            if (
                not isinstance(artifact_id, str)
                or not artifact_id
                or not isinstance(identity_sha256, str)
                or len(identity_sha256) != 64
                or any(value not in "0123456789abcdef" for value in identity_sha256)
                or not isinstance(download_bytes, int)
                or isinstance(download_bytes, bool)
                or download_bytes <= 0
                or not isinstance(installed_bytes, int)
                or isinstance(installed_bytes, bool)
                or installed_bytes <= 0
                or not isinstance(roles, list)
                or any(not isinstance(role, str) for role in roles)
            ):
                raise QualificationError("recipe artifact identity projection is invalid")
            result.append(
                {
                    "artifact_id": artifact_id,
                    "identity_sha256": identity_sha256,
                    "download_bytes": download_bytes,
                    "installed_bytes": installed_bytes,
                    "roles": sorted(set(roles)),
                }
            )
        identities = [str(item["identity_sha256"]) for item in result]
        if len(identities) != len(set(identities)):
            raise QualificationError("recipe artifact identities are not unique")
        return sorted(result, key=lambda item: str(item["identity_sha256"]))
    visual = value.get("visual_recipe")
    document = (
        visual
        if isinstance(visual, Mapping) and isinstance(visual.get("artifacts"), list)
        else value
    )
    artifacts = document.get("artifacts")
    if not isinstance(artifacts, list):
        return []
    result: list[dict[str, object]] = []
    for raw in artifacts:
        if not isinstance(raw, Mapping):
            raise QualificationError("recipe artifact projection is invalid")
        include_paths = raw.get("include_paths", [])
        if (
            not isinstance(raw.get("id"), str)
            or not isinstance(raw.get("kind"), str)
            or not isinstance(raw.get("repository"), str)
            or not isinstance(raw.get("revision"), str)
            or not raw.get("revision")
            or not isinstance(include_paths, list)
            or any(not isinstance(value, str) for value in include_paths)
        ):
            raise QualificationError(
                "recipe artifact lacks an immutable subset identity"
            )
        identity = {
            "kind": raw.get("kind"),
            "repository": raw.get("repository"),
            "revision": raw.get("revision"),
            "include_paths": sorted(set(include_paths)),
        }
        download_bytes = raw.get("download_bytes")
        installed_bytes = raw.get("installed_bytes")
        if not (
            isinstance(download_bytes, int)
            and not isinstance(download_bytes, bool)
            and download_bytes > 0
            and isinstance(installed_bytes, int)
            and not isinstance(installed_bytes, bool)
            and installed_bytes > 0
        ):
            raise QualificationError("recipe artifact byte bounds are invalid")
        result.append(
            {
                "artifact_id": raw.get("id"),
                "identity_sha256": _digest(identity),
                "download_bytes": download_bytes,
                "installed_bytes": installed_bytes,
                "roles": sorted(str(value) for value in raw.get("roles", []))
                if isinstance(raw.get("roles", []), list)
                else [],
            }
        )
    return sorted(result, key=lambda item: str(item["identity_sha256"]))


def _temporary_build_bytes(value: Mapping[str, object] | None) -> int:
    if value is None:
        return 0
    projected = value.get("temporary_build_bytes_per_node")
    if projected is not None:
        if (
            not isinstance(projected, int)
            or isinstance(projected, bool)
            or projected < 0
        ):
            raise QualificationError("recipe temporary build bytes are invalid")
        return projected
    visual = value.get("visual_recipe")
    document = (
        visual
        if isinstance(visual, Mapping) and isinstance(visual.get("build"), Mapping)
        else value
    )
    build = document.get("build")
    temporary = build.get("temporary_bytes") if isinstance(build, Mapping) else None
    if temporary is None:
        resources_value = (
            build.get("resources") if isinstance(build, Mapping) else None
        )
        temporary = (
            resources_value.get("temporary_bytes")
            if isinstance(resources_value, Mapping)
            else None
        )
    if not isinstance(temporary, int) or isinstance(temporary, bool) or temporary < 0:
        return 0
    return temporary


def _disk_requirements_by_role(
    value: Mapping[str, object] | None,
) -> dict[str, dict[str, int]]:
    if value is None:
        return {}
    topology = value.get("topology")
    roles = topology.get("roles") if isinstance(topology, Mapping) else None
    if not isinstance(roles, list):
        roles = value.get("topology_roles")
    if not isinstance(roles, list):
        return {}
    fields = (
        "image_bytes",
        "artifact_bytes",
        "staging_bytes",
        "cache_bytes",
        "rollback_bytes",
        "safety_margin_bytes",
    )
    result: dict[str, dict[str, int]] = {}
    for raw_role in roles:
        role = _object(raw_role, "recipe role")
        name = role.get("name")
        resources = role.get("resources")
        disk = role.get("disk")
        if not isinstance(disk, Mapping):
            disk = resources.get("disk") if isinstance(resources, Mapping) else None
        if not isinstance(name, str) or not isinstance(disk, Mapping):
            raise QualificationError("recipe role lacks exact disk requirements")
        normalized: dict[str, int] = {}
        for field in fields:
            amount = disk.get(field)
            if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
                raise QualificationError(
                    f"recipe role {name} has invalid {field} requirement"
                )
            normalized[field] = amount
        if name in result:
            raise QualificationError(f"duplicate recipe role disk requirements: {name}")
        result[name] = normalized
    return result


def _validate_role_disk_requirements(
    key: str,
    recipe: Mapping[str, object],
    requirements: Mapping[str, Mapping[str, int]],
) -> None:
    roles = _list(recipe.get("topology_roles"), "public recipe topology roles")
    expected: dict[str, int] = {}
    for raw_role in roles:
        role = _object(raw_role, "public recipe topology role")
        name = role.get("name")
        count = role.get("count")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 1
            or name in expected
        ):
            raise QualificationError(f"{key} has invalid topology role metadata")
        expected[name] = count
    node_count = recipe.get("node_count")
    if not expected or sum(expected.values()) != node_count:
        raise QualificationError(f"{key} has invalid topology role counts")
    if set(requirements) != set(expected):
        missing = sorted(set(expected) - set(requirements))
        extra = sorted(set(requirements) - set(expected))
        detail = f"missing {missing[0]!r}" if missing else f"undeclared {extra[0]!r}"
        raise QualificationError(
            f"{key} lacks exact disk requirements for every topology role: {detail}"
        )


def build_plan(
    client: ControllerClient,
    options: RunnerOptions,
    policy: Mapping[str, Blocker],
    fixtures: FixtureRegistry | None = None,
) -> dict[str, object]:
    fleet = client.request("GET", "/api/v1/fleet")
    public = client.request("GET", "/api/v1/catalog/public-recipes")
    recipes = _list(public.get("recipes"), "public recipes")
    online_nodes = _fleet_nodes(fleet)
    authority_node_ids = {
        node_id
        for node in (
            _object(raw, "fleet node")
            for raw in _list(fleet.get("nodes"), "fleet nodes")
        )
        if isinstance((node_id := node.get("id")), str)
    }
    unknown_node_ids = sorted(options.allowed_node_ids - authority_node_ids)
    if unknown_node_ids:
        raise QualificationError(
            f"pinned Spark is not in controller authority: {unknown_node_ids[0]}"
        )
    campaign_online_nodes = [
        node
        for node in online_nodes
        if not options.allowed_node_ids or node.get("id") in options.allowed_node_ids
    ]
    fleet_fingerprint = _fleet_fingerprint(fleet)
    items: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in recipes:
        recipe = _object(raw, "public recipe")
        key = _recipe_key(recipe)
        if key in seen:
            raise QualificationError(f"duplicate public recipe identity: {key}")
        seen.add(key)
        if options.selected_recipes and key not in options.selected_recipes:
            continue
        blockers = legal_blockers(recipe, options.jurisdiction)
        if key in policy:
            blockers.append(policy[key])
        node_count = recipe.get("node_count")
        if options.allowed_node_ids and node_count != 1:
            raise QualificationError(
                f"node-pinned campaign recipe must require exactly one Spark: {key}"
            )
        if (
            not isinstance(node_count, int)
            or isinstance(node_count, bool)
            or node_count < 1
        ):
            blockers.append(
                Blocker(
                    "topology",
                    "topology.node_count_invalid",
                    "Recipe node count is invalid.",
                )
            )
        elif node_count > 2:
            blockers.append(
                Blocker(
                    "topology",
                    "topology.unsupported_fleet_width",
                    f"Recipe requires {node_count} Sparks; this qualification fleet supports one or two.",
                )
            )
        elif node_count > len(campaign_online_nodes):
            blockers.append(
                Blocker(
                    "topology",
                    "topology.insufficient_online_nodes",
                    f"Recipe requires {node_count} online Sparks; campaign node allowlist currently has {len(campaign_online_nodes)} online.",
                )
            )
        required_memory = recipe.get("maximum_runtime_memory_bytes_per_node")
        available_memory = [
            value
            for node in campaign_online_nodes
            if (value := _node_available_memory(node)) is not None
        ]
        if (
            isinstance(required_memory, int)
            and not isinstance(required_memory, bool)
            and available_memory
            and required_memory > max(available_memory)
        ):
            blockers.append(
                Blocker(
                    "resource",
                    "resource.memory_exceeds_fleet",
                    "Recipe's largest per-node runtime memory requirement exceeds "
                    "every online Spark's observed available memory.",
                )
            )
        if recipe.get("execution_readiness") != "executable":
            blockers.append(
                Blocker(
                    "runtime",
                    "runtime.not_executable",
                    str(
                        recipe.get("execution_readiness_detail")
                        or "Recipe is not executable."
                    )[:512],
                )
            )
        if fixtures is not None and node_count in {1, 2}:
            special = fixtures.special.get(key)
            artifact_fixture = fixtures.recipes.get(key)
            service_fixture = fixtures.service_recipes.get(key)
            known = (
                artifact_fixture is not None
                or service_fixture is not None
                or special is not None
            )
            if special is not None:
                if special.get("content_sha256") != recipe.get("content_sha256"):
                    blockers.append(
                        Blocker(
                            "fixture",
                            "fixture.recipe_digest_mismatch",
                            "The recipe changed after its special-fixture classification.",
                        )
                    )
                else:
                    blockers.append(
                        Blocker(
                            "fixture",
                            str(special.get("code") or "fixture.special_required"),
                            str(special.get("detail") or "Special fixture required")[
                                :512
                            ],
                        )
                    )
            elif (
                artifact_fixture is not None
                and artifact_fixture.content_sha256 != recipe.get("content_sha256")
            ):
                blockers.append(
                    Blocker(
                        "fixture",
                        "fixture.recipe_digest_mismatch",
                        "The artifact qualification contract is bound to a different recipe digest.",
                    )
                )
            elif (
                service_fixture is not None
                and service_fixture.content_sha256 != recipe.get("content_sha256")
            ):
                blockers.append(
                    Blocker(
                        "fixture",
                        "service_fixture.recipe_digest_mismatch",
                        "The service qualification contract is bound to a different recipe digest.",
                    )
                )
            elif not known:
                blockers.append(
                    Blocker(
                        "fixture",
                        "fixture.missing",
                        "No reviewed digest-bound qualification contract exists.",
                    )
                )
        local_recipe_id = _local_recipe_id(recipe)
        local_revision_id: str | None = None
        recipe_document: Mapping[str, object] | None = None
        if local_recipe_id is not None:
            detail = client.request(
                "GET", f"/api/v1/library/recipes/{_quote(local_recipe_id)}"
            )
            selected_revision = detail.get("selected_revision")
            if isinstance(selected_revision, Mapping):
                selected_id = selected_revision.get("id")
                selected_sha256 = selected_revision.get("content_sha256")
                if isinstance(selected_id, str) and selected_sha256 == recipe.get(
                    "content_sha256"
                ):
                    local_revision_id = selected_id
            if local_revision_id is None:
                raise QualificationError(
                    f"current local recipe revision is inconsistent: {key}"
                )
            blockers.extend(legal_blockers(detail, options.jurisdiction))
            recipe_document = detail
        import_preview: Mapping[str, object] | None = None
        if not blockers and local_revision_id is None:
            uri = recipe.get("uri")
            if not isinstance(uri, str):
                raise QualificationError(f"public recipe URI is invalid: {key}")
            import_preview = client.request(
                "POST", "/api/v1/catalog/imports/public/preview", {"uri": uri}
            )
            if import_preview.get("content_sha256") != recipe.get("content_sha256"):
                raise QualificationError(
                    f"public recipe changed during planning: {key}"
                )
            blockers.extend(legal_blockers(import_preview, options.jurisdiction))
            recipe_document = import_preview
        disk_requirements = _disk_requirements_by_role(recipe)
        _validate_role_disk_requirements(key, recipe, disk_requirements)
        artifact_identities = _artifact_identities(recipe)
        artifact_count = recipe.get("artifact_count")
        if (
            not isinstance(artifact_count, int)
            or isinstance(artifact_count, bool)
            or artifact_count != len(artifact_identities)
        ):
            raise QualificationError(f"{key} artifact identity projection is incomplete")
        temporary_build_bytes = _temporary_build_bytes(recipe)
        if not blockers:
            preview_disk_requirements = _disk_requirements_by_role(recipe_document)
            _validate_role_disk_requirements(key, recipe, preview_disk_requirements)
            if preview_disk_requirements != disk_requirements:
                raise QualificationError(
                    f"{key} exact disk requirements changed during planning"
                )
            if _artifact_identities(recipe_document) != artifact_identities:
                raise QualificationError(
                    f"{key} artifact identities changed during planning"
                )
            if _temporary_build_bytes(recipe_document) != temporary_build_bytes:
                raise QualificationError(
                    f"{key} temporary build bytes changed during planning"
                )
        items.append(
            {
                "key": key,
                "publisher": recipe.get("publisher"),
                "slug": recipe.get("slug"),
                "uri": recipe.get("uri"),
                "content_sha256": recipe.get("content_sha256"),
                "release_version": recipe.get("release_version"),
                "node_count": node_count,
                "expected_download_bytes": recipe.get("expected_download_bytes"),
                "maximum_installed_bytes_per_node": recipe.get(
                    "maximum_installed_bytes_per_node"
                ),
                "temporary_build_bytes_per_node": temporary_build_bytes,
                "disk_requirements_by_role": disk_requirements,
                "artifact_identities": artifact_identities,
                "maximum_runtime_memory_bytes_per_node": recipe.get(
                    "maximum_runtime_memory_bytes_per_node"
                ),
                "local_recipe_id": local_recipe_id,
                "local_revision_id": local_revision_id,
                "import_preview_sha256": _digest(import_preview)
                if import_preview
                else None,
                "blockers": [item.as_dict() for item in blockers],
                "planned_actions": (
                    []
                    if blockers
                    else [
                        *([] if local_revision_id else ["import"]),
                        "placement-preview",
                        "mapping",
                        "build",
                        "image-distribution",
                        "install-preview",
                        "install",
                        "run-preview",
                        "run",
                        "smoke-preview",
                        "smoke",
                        *(
                            []
                            if options.cleanup == "none"
                            else [
                                "stop-preview",
                                "stop",
                                "warm-redeploy-preview",
                                "warm-redeploy",
                                "warm-redeploy-smoke",
                                "warm-redeploy-stop-preview",
                                "warm-redeploy-stop",
                                *(
                                    ["retain-installation"]
                                    if options.cleanup == "stop"
                                    else []
                                ),
                            ]
                        ),
                        *(
                            ["uninstall-preview", "uninstall"]
                            if options.cleanup == "uninstall"
                            else []
                        ),
                    ]
                ),
            }
        )
    if options.selected_recipes:
        missing = sorted(options.selected_recipes - seen)
        if missing:
            raise QualificationError(
                f"selected recipe is not in public catalog: {missing[0]}"
            )
    items.sort(
        key=lambda item: (
            -int(item["maximum_installed_bytes_per_node"])
            if isinstance(item.get("maximum_installed_bytes_per_node"), int)
            else 0,
            str(item["key"]),
        )
    )
    intent = {
        "schema_version": 1,
        "catalog": {
            "repository": public.get("repository"),
            "commit": public.get("commit"),
        },
        "controller_authority": {
            "authority_revision": fleet.get("authority_revision"),
            "node_ids": sorted(
                str(node.get("id"))
                for node in (
                    _object(raw, "fleet node")
                    for raw in _list(fleet.get("nodes"), "fleet nodes")
                )
            ),
        },
        "recipes": [
            {
                "key": item["key"],
                "uri": item["uri"],
                "content_sha256": item["content_sha256"],
                "release_version": item["release_version"],
                "node_count": item["node_count"],
                "temporary_build_bytes_per_node": item[
                    "temporary_build_bytes_per_node"
                ],
                "disk_requirements_by_role": item["disk_requirements_by_role"],
                "artifact_identities": item["artifact_identities"],
                "immutable_blockers": [
                    blocker
                    for blocker in item["blockers"]
                    if blocker.get("code")
                    not in {
                        "topology.insufficient_online_nodes",
                        "resource.memory_exceeds_fleet",
                    }
                ],
            }
            for item in sorted(items, key=lambda value: str(value["key"]))
        ],
        "operator_policy": {
            key: value.as_dict() for key, value in sorted(policy.items())
        },
        "options": {
            "jurisdiction": options.jurisdiction,
            "cleanup": options.cleanup,
            "selected_recipes": sorted(options.selected_recipes),
            **(
                {"allowed_node_ids": sorted(options.allowed_node_ids)}
                if options.allowed_node_ids
                else {}
            ),
            "fixture_manifest_sha256": (
                fixtures.manifest_sha256 if fixtures is not None else None
            ),
        },
    }
    plan: dict[str, object] = {
        "schema_version": 1,
        "campaign_intent": intent,
        "catalog": {
            "repository": public.get("repository"),
            "commit": public.get("commit"),
        },
        "fleet": {
            "authority_revision": fleet.get("authority_revision"),
            "event_cursor": fleet.get("event_cursor"),
            "online_node_ids": [node.get("id") for node in online_nodes],
            "snapshot_sha256": _digest(fleet_fingerprint),
        },
        "options": {
            "jurisdiction": options.jurisdiction,
            "cleanup": options.cleanup,
            **(
                {"allowed_node_ids": sorted(options.allowed_node_ids)}
                if options.allowed_node_ids
                else {}
            ),
            "fixture_manifest_sha256": (
                fixtures.manifest_sha256 if fixtures is not None else None
            ),
        },
        "recipes": items,
    }
    plan["plan_digest"] = _digest(intent)
    return plan


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _request_key(plan_digest: str, recipe: str, step: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"vonk:{plan_digest}:{recipe}:{step}"))


def _payloads(
    records: Iterable[Mapping[str, object]], event: str
) -> list[Mapping[str, object]]:
    return [
        item["payload"]
        for item in records
        if item.get("event") == event and isinstance(item.get("payload"), Mapping)
    ]


def _latest_step(
    records: Iterable[Mapping[str, object]], event: str, step: str
) -> Mapping[str, object] | None:
    values = [item for item in _payloads(records, event) if item.get("step") == step]
    return values[-1] if values else None


class OperationMonitor:
    def __init__(
        self,
        client: ControllerClient,
        ledger: EvidenceLedger,
        options: RunnerOptions,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.ledger = ledger
        self.options = options
        self.clock = clock
        self.sleeper = sleeper

    def wait(
        self, plan_digest: str, recipe: str, step: str, operation_id: str
    ) -> Mapping[str, object]:
        deadline = self.clock() + self.options.operation_timeout_seconds
        while True:
            value = self.client.request(
                "GET", f"/api/v1/recipes/operations/{_quote(operation_id)}"
            )
            state = value.get("state")
            if state in _TERMINAL_OPERATION_STATES:
                self.ledger.append(
                    "operation.completed",
                    plan_digest=plan_digest,
                    recipe=recipe,
                    payload={"step": step, "operation": value},
                )
                if state not in _SUCCESS_OPERATION_STATES:
                    raise QualificationError(
                        f"{recipe} {step} operation entered terminal state {state}"
                    )
                return value
            if self.clock() >= deadline:
                self.ledger.append(
                    "operation.timeout",
                    plan_digest=plan_digest,
                    recipe=recipe,
                    payload={
                        "step": step,
                        "operation_id": operation_id,
                        "state": state,
                    },
                )
                raise QualificationError(f"{recipe} {step} operation timed out")
            self.sleeper(self.options.poll_interval_seconds)


class ArtifactJobSmokeAdapter:
    """Run exact digest-bound fixtures through the durable artifact-job lifecycle."""

    def __init__(self, fixtures: FixtureRegistry | None = None) -> None:
        self.fixtures = fixtures or FixtureRegistry.packaged()

    def preview(
        self,
        detail: Mapping[str, object],
        *,
        recipe_key: str,
        recipe_content_sha256: str,
    ) -> dict[str, object]:
        visual = detail.get("visual_recipe")
        interfaces = visual.get("interfaces") if isinstance(visual, Mapping) else None
        interface_rows = interfaces if isinstance(interfaces, list) else []
        adapters = [
            item.get("adapter") for item in interface_rows if isinstance(item, Mapping)
        ]
        adapter = next((item for item in adapters if item in _JOB_ADAPTERS), None)
        if not isinstance(adapter, str):
            return {
                "kind": "artifact-job",
                "available": False,
                "blocker": {
                    "classification": "runtime",
                    "code": "artifact_job.interface_missing",
                    "detail": "Recipe has no supported artifact-job interface.",
                },
            }
        recipe, blocker = self.fixtures.resolve(
            recipe_key, recipe_content_sha256, adapter
        )
        if recipe is None:
            return {
                "kind": "artifact-job",
                "adapter": adapter,
                "available": False,
                "blocker": blocker,
                "fixture_manifest_sha256": self.fixtures.manifest_sha256,
                "capabilities_path": "/api/v1/artifact-jobs/capabilities",
            }
        return {
            **recipe.preview(),
            "fixture_manifest_sha256": self.fixtures.manifest_sha256,
        }

    def run(
        self,
        client: ControllerClient,
        run_id: str,
        preview: Mapping[str, object],
        *,
        ledger: EvidenceLedger,
        plan_digest: str,
        recipe_key: str,
        timeout_seconds: float,
        poll_interval_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        event_prefix: str = "artifact-job",
    ) -> Mapping[str, object]:
        recipe, blocker = self.fixtures.resolve(
            recipe_key,
            str(preview.get("recipe_content_sha256")),
            str(preview.get("interface")),
        )
        if recipe is None:
            raise QualificationError(
                str(blocker["detail"] if blocker else "fixture unavailable")
            )
        cases = recipe.all_cases
        results = [
            self._run_case(
                client,
                run_id,
                case,
                ledger=ledger,
                plan_digest=plan_digest,
                recipe_key=recipe_key,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
                clock=clock,
                sleeper=sleeper,
                event_prefix=(
                    event_prefix
                    if len(cases) == 1
                    else f"{event_prefix}.case.{case.case_id}"
                ),
            )
            for case in cases
        ]
        if len(results) == 1:
            return results[0]
        return {
            "fixture_manifest_sha256": self.fixtures.manifest_sha256,
            "case_count": len(results),
            "cases": results,
        }

    def _run_case(
        self,
        client: ControllerClient,
        run_id: str,
        recipe: RecipeFixture,
        *,
        ledger: EvidenceLedger,
        plan_digest: str,
        recipe_key: str,
        timeout_seconds: float,
        poll_interval_seconds: float,
        clock: Callable[[], float],
        sleeper: Callable[[float], None],
        event_prefix: str,
    ) -> Mapping[str, object]:
        records = ledger.recipe_records(plan_digest, recipe_key)
        created_records = _payloads(records, f"{event_prefix}.created")
        request_key = _request_key(plan_digest, recipe_key, event_prefix)
        capabilities = client.request("GET", "/api/v1/artifact-jobs/capabilities")
        if capabilities.get("schema_version") != 1:
            raise QualificationError(
                "controller artifact-job capabilities are incompatible"
            )
        if created_records:
            created = _object(created_records[-1].get("job"), "artifact job")
        else:
            created = client.request(
                "POST",
                f"/api/v1/recipes/runs/{_quote(run_id)}/artifact-jobs",
                {
                    "interface": recipe.interface,
                    "parameters": recipe.parameters,
                    "inputs": [
                        fixture.declaration(slot) for slot, fixture in recipe.inputs
                    ],
                    "output_limits": recipe.output_limits,
                    "timeout_seconds": recipe.timeout_seconds,
                },
                extra_headers={"X-Request-ID": request_key},
            )
            ledger.append(
                f"{event_prefix}.created",
                plan_digest=plan_digest,
                recipe=recipe_key,
                payload={"job": created, "capabilities": capabilities},
            )
        job_id = created.get("id")
        if not isinstance(job_id, str):
            raise QualificationError("controller returned an invalid artifact job ID")
        status = client.request("GET", f"/api/v1/artifact-jobs/{_quote(job_id)}")
        uploaded = {
            str(payload.get("name"))
            for payload in _payloads(records, f"{event_prefix}.input-uploaded")
        }
        with recipe.materialize() as inputs:
            if status.get("state") == "draft":
                for declaration, source in inputs:
                    name = str(declaration["name"])
                    if name in uploaded:
                        continue
                    client.upload_file(
                        f"/api/v1/artifact-jobs/{_quote(job_id)}/inputs/{_quote(name)}",
                        source,
                        media_type=str(declaration["media_type"]),
                        expected_sha256=str(declaration["sha256"]),
                        expected_size=int(declaration["size_bytes"]),
                    )
                    ledger.append(
                        f"{event_prefix}.input-uploaded",
                        plan_digest=plan_digest,
                        recipe=recipe_key,
                        payload={"job_id": job_id, **declaration},
                    )
                status = client.request(
                    "POST", f"/api/v1/artifact-jobs/{_quote(job_id)}/finalize"
                )
                ledger.append(
                    f"{event_prefix}.finalized",
                    plan_digest=plan_digest,
                    recipe=recipe_key,
                    payload={"job": status},
                )
            if status.get("state") == "ready":
                status = client.request(
                    "POST", f"/api/v1/artifact-jobs/{_quote(job_id)}/submit"
                )
                ledger.append(
                    f"{event_prefix}.submitted",
                    plan_digest=plan_digest,
                    recipe=recipe_key,
                    payload={"job": status},
                )
            deadline = clock() + min(timeout_seconds, recipe.timeout_seconds + 300)
            while status.get("state") not in {"succeeded", "failed", "cancelled"}:
                if clock() >= deadline:
                    client.request(
                        "POST",
                        f"/api/v1/artifact-jobs/{_quote(job_id)}/cancel",
                        {"reason": "qualification smoke timed out"},
                    )
                    raise QualificationError(
                        "artifact-job smoke timed out and was cancelled"
                    )
                sleeper(poll_interval_seconds)
                status = client.request(
                    "GET", f"/api/v1/artifact-jobs/{_quote(job_id)}"
                )
            ledger.append(
                f"{event_prefix}.completed",
                plan_digest=plan_digest,
                recipe=recipe_key,
                payload={"job": status},
            )
            if status.get("state") != "succeeded":
                raise QualificationError(
                    f"artifact-job smoke entered terminal state {status.get('state')}"
                )
            result = client.request(
                "GET", f"/api/v1/artifact-jobs/{_quote(job_id)}/result"
            )
            try:
                assertions = validate_outputs(recipe, result, client)
            except FixtureError as error:
                raise QualificationError(str(error)) from error
        return {
            "case_id": recipe.case_id,
            "job_id": job_id,
            "contract_sha256": status.get("contract_sha256"),
            "input_manifest_sha256": status.get("input_manifest_sha256"),
            "fixture_manifest_sha256": self.fixtures.manifest_sha256,
            **assertions,
        }


def _service_path(value: object, path: str) -> object:
    current = value
    for part in path.split("."):
        if isinstance(current, Mapping):
            if part not in current:
                raise QualificationError(f"service assertion path is missing: {path}")
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            if index >= len(current):
                raise QualificationError(f"service assertion path is missing: {path}")
            current = current[index]
        else:
            raise QualificationError(f"service assertion path is missing: {path}")
    return current


def _assert_service_response(
    response: Mapping[str, object], raw: bytes, assertions: list[object]
) -> None:
    raw_text = raw.decode("utf-8")
    for raw_assertion in assertions:
        assertion = _object(raw_assertion, "service assertion")
        kind = assertion.get("kind")
        if kind == "raw.not-contains":
            values = assertion.get("values")
            if not isinstance(values, list) or any(
                isinstance(item, str) and item in raw_text for item in values
            ):
                raise QualificationError("service raw-token assertion failed")
            continue
        path = assertion.get("path")
        if not isinstance(path, str):
            raise QualificationError("service assertion path is invalid")
        value = _service_path(response, path)
        expected = assertion.get("value")
        failed = False
        if kind == "path.equals":
            failed = value != expected
        elif kind == "path.regex":
            failed = (
                not isinstance(value, str) or re.fullmatch(str(expected), value) is None
            )
        elif kind == "path.nonempty":
            failed = not isinstance(value, (str, list, dict)) or len(value) == 0
        elif kind == "path.empty":
            failed = value not in (None, "", [], {})
        elif kind == "path.count":
            failed = not isinstance(value, (str, list, dict)) or len(value) != expected
        elif kind == "path.lte":
            failed = (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or value > expected
            )
        elif kind == "path.json-equals":
            try:
                decoded = _strict_json_loads(value) if isinstance(value, str) else None
            except (json.JSONDecodeError, ValueError):
                decoded = None
            failed = decoded != expected
        elif kind == "array.path-count-equals":
            item_path = assertion.get("item_path")
            if not isinstance(value, list) or not isinstance(item_path, str):
                failed = True
            else:
                count = 0
                for item in value:
                    try:
                        if _service_path(item, item_path) == expected:
                            count += 1
                    except QualificationError:
                        continue
                failed = count != assertion.get("count")
        else:
            raise QualificationError(f"unsupported service assertion: {kind}")
        if failed:
            raise QualificationError(f"service assertion failed: {kind} at {path}")


class ServiceSmokeAdapter:
    """Run digest-bound, capability-aware OpenAI service acceptance cases."""

    def __init__(
        self,
        fixtures: FixtureRegistry | None = None,
        *,
        timeout_seconds: float = 180,
        opener: Any = urllib.request.urlopen,
    ):
        self.fixtures = fixtures or FixtureRegistry.packaged()
        self.timeout_seconds = timeout_seconds
        self.opener = opener

    def preview(
        self,
        detail: Mapping[str, object],
        alias: str,
        *,
        recipe_key: str,
        recipe_content_sha256: str,
    ) -> dict[str, object]:
        recipe, blocker = self.fixtures.resolve_service(
            recipe_key, recipe_content_sha256
        )
        if recipe is None:
            return {
                "kind": "openai-service",
                "available": False,
                "blocker": blocker,
                "fixture_manifest_sha256": self.fixtures.manifest_sha256,
            }
        return {
            **recipe.preview(self.fixtures.fixtures),
            "endpoint_alias": alias,
            "fixture_manifest_sha256": self.fixtures.manifest_sha256,
        }

    def run(
        self, client: ControllerClient, alias: str, preview: Mapping[str, object]
    ) -> Mapping[str, object]:
        endpoint = client.request("GET", f"/api/v1/endpoints/{_quote(alias)}")
        base = endpoint.get("api_base")
        if not isinstance(base, str):
            raise QualificationError("published endpoint API base is invalid")
        parsed = urllib.parse.urlsplit(base)
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise QualificationError(
                "published endpoint API base must be credential-free HTTPS"
            )
        cases = preview.get("cases")
        if not isinstance(cases, list) or not cases:
            raise QualificationError("service smoke has no reviewed cases")
        results: list[dict[str, object]] = []
        for raw_case in cases:
            case = _object(raw_case, "service smoke case")
            method = str(case.get("method"))
            path = str(case.get("path"))
            body_value = case.get("body")
            body = _canonical(body_value) if method == "POST" else None
            request = urllib.request.Request(
                base.rstrip("/") + path,
                data=body,
                method=method,
                headers={
                    "Accept": "application/json",
                    **(
                        {"Content-Type": "application/json"} if body is not None else {}
                    ),
                },
            )
            limit = case.get("max_response_bytes")
            timeout = case.get("timeout_seconds")
            if not isinstance(limit, int) or not isinstance(timeout, int):
                raise QualificationError("service smoke case bounds are invalid")
            started = time.monotonic()
            try:
                with self.opener(
                    request, timeout=min(float(timeout), self.timeout_seconds)
                ) as response:
                    status = int(getattr(response, "status", 200))
                    raw = response.read(limit + 1)
            except (OSError, urllib.error.URLError) as error:
                raise QualificationError(
                    f"service smoke {case.get('id')} request failed: {type(error).__name__}"
                ) from None
            latency_ms = round((time.monotonic() - started) * 1000, 3)
            if status != 200:
                raise QualificationError(
                    f"service smoke {case.get('id')} returned HTTP {status}"
                )
            if len(raw) > limit:
                raise QualificationError(
                    f"service smoke {case.get('id')} response exceeds its bound"
                )
            try:
                value = _strict_json_loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                raise QualificationError(
                    f"service smoke {case.get('id')} response is invalid JSON"
                ) from error
            response_object = _object(value, "service smoke response")
            assertions = case.get("assertions")
            if not isinstance(assertions, list):
                raise QualificationError("service smoke assertions are invalid")
            _assert_service_response(response_object, raw, assertions)
            results.append(
                {
                    "case_id": case.get("id"),
                    "method": method,
                    "path": path,
                    "http_status": status,
                    "latency_ms": latency_ms,
                    "response_bytes": len(raw),
                    "response_sha256": hashlib.sha256(raw).hexdigest(),
                    "assertions": assertions,
                }
            )
        return {
            "endpoint": endpoint,
            "fixture_manifest_sha256": preview.get("fixture_manifest_sha256"),
            "recipe_content_sha256": preview.get("recipe_content_sha256"),
            "endpoint_alias": alias,
            "model_alias": preview.get("alias"),
            "cases": results,
        }


class QualificationRunner:
    def __init__(
        self,
        client: ControllerClient,
        ledger: EvidenceLedger,
        options: RunnerOptions,
        *,
        monitor: OperationMonitor | None = None,
        artifact_smoke: ArtifactJobSmokeAdapter | None = None,
        service_smoke: ServiceSmokeAdapter | None = None,
    ) -> None:
        self.client = client
        self.ledger = ledger
        self.options = options
        self.monitor = monitor or OperationMonitor(client, ledger, options)
        self.artifact_smoke = artifact_smoke or ArtifactJobSmokeAdapter()
        self.service_smoke = service_smoke or ServiceSmokeAdapter()
        self._capacity_remaining: dict[str, int] = {}
        self._capacity_artifacts: dict[str, set[str]] = {}
        self._prepared: dict[str, tuple[str, str, Mapping[str, object]]] = {}
        self._capacity_assignments: dict[str, Mapping[str, object]] = {}
        self._capacity_execution_order: list[str] = []
        self._preflight_blocked: set[str] = set()
        self._preflight_failed: set[str] = set()

    def _node_allowed(self, node_id: object) -> bool:
        return isinstance(node_id, str) and (
            not self.options.allowed_node_ids
            or node_id in self.options.allowed_node_ids
        )

    def _candidate_allowed(self, candidate: Mapping[str, object]) -> bool:
        node_ids = candidate.get("node_ids")
        return (
            isinstance(node_ids, list)
            and bool(node_ids)
            and all(self._node_allowed(node_id) for node_id in node_ids)
        )

    def _uncertain_installation_blocks(
        self, installation: object, revision_id: str
    ) -> bool:
        if (
            not isinstance(installation, Mapping)
            or installation.get("recipe_revision_id") != revision_id
            or installation.get("state") not in {"partial", "failed", "installing"}
        ):
            return False
        if not self.options.allowed_node_ids:
            return True
        node_ids = installation.get("node_ids")
        if (
            not isinstance(node_ids, list)
            or not node_ids
            or any(
                not isinstance(node_id, str) or _NODE_ID.fullmatch(node_id) is None
                for node_id in node_ids
            )
        ):
            return True
        return bool(self.options.allowed_node_ids.intersection(node_ids))

    def _record_preview(
        self, digest: str, key: str, step: str, preview: Mapping[str, object]
    ) -> None:
        self.ledger.append(
            "step.previewed",
            plan_digest=digest,
            recipe=key,
            payload={
                "step": step,
                "preview": dict(preview),
                "preview_sha256": _digest(preview),
            },
        )

    def _operation(
        self,
        digest: str,
        key: str,
        step: str,
        path: str,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]:
        records = self.ledger.recipe_records(digest, key)
        completed = _latest_step(records, "operation.completed", step)
        if completed is not None:
            return _object(completed.get("operation"), "completed operation")
        submitted = _latest_step(records, "operation.submitted", step)
        if submitted is None:
            operation = self.client.request("POST", path, payload)
            operation_id = operation.get("id")
            if not isinstance(operation_id, str):
                raise QualificationError(f"{key} {step} did not return an operation ID")
            self.ledger.append(
                "operation.submitted",
                plan_digest=digest,
                recipe=key,
                payload={"step": step, "operation": operation},
            )
        else:
            operation = _object(submitted.get("operation"), "submitted operation")
            operation_id = operation.get("id")
            if not isinstance(operation_id, str):
                raise QualificationError("ledger operation ID is invalid")
        return self.monitor.wait(digest, key, step, operation_id)

    def _completed_operation(
        self, digest: str, key: str, step: str
    ) -> Mapping[str, object] | None:
        completed = _latest_step(
            self.ledger.recipe_records(digest, key), "operation.completed", step
        )
        return (
            _object(completed.get("operation"), "completed operation")
            if completed is not None
            else None
        )

    def _ensure_import(
        self, digest: str, item: Mapping[str, object]
    ) -> tuple[str, str]:
        key = str(item["key"])
        records = self.ledger.recipe_records(digest, key)
        imports = _payloads(records, "recipe.imported")
        if imports:
            result = _object(imports[-1].get("result"), "import result")
        elif isinstance(item.get("local_recipe_id"), str) and isinstance(
            item.get("local_revision_id"), str
        ):
            return str(item["local_recipe_id"]), str(item["local_revision_id"])
        else:
            result = self.client.request(
                "POST",
                "/api/v1/catalog/imports/public",
                {
                    "uri": item["uri"],
                    "expected_content_sha256": item["content_sha256"],
                },
            )
            if result.get("content_sha256") != item.get("content_sha256"):
                raise QualificationError(f"imported content digest mismatch: {key}")
            self.ledger.append(
                "recipe.imported",
                plan_digest=digest,
                recipe=key,
                payload={"result": result},
            )
        recipe_id = result.get("recipe_id")
        revision_id = result.get("id")
        if not isinstance(recipe_id, str) or not isinstance(revision_id, str):
            raise QualificationError(f"import result identity is invalid: {key}")
        return recipe_id, revision_id

    def _initialize_capacity_state(self, digest: str) -> None:
        if self._capacity_remaining:
            return
        fleet = self.client.request("GET", "/api/v1/fleet")
        for node in _fleet_nodes(fleet):
            node_id = node.get("id")
            free = _node_allocatable_disk(node)
            if self._node_allowed(node_id) and isinstance(free, int):
                self._capacity_remaining[node_id] = free
                self._capacity_artifacts[node_id] = set()
        for record in self.ledger.records:
            if (
                record.get("plan_digest") != digest
                or record.get("event") != "capacity.assigned"
            ):
                continue
            recipe = record.get("recipe")
            payload = record.get("payload")
            if not isinstance(recipe, str) or not isinstance(payload, Mapping):
                continue
            recipe_records = self.ledger.recipe_records(digest, recipe)
            retained = payload.get("preexisting_installation") is True or any(
                row.get("event") == "operation.completed"
                and isinstance(row.get("payload"), Mapping)
                and row["payload"].get("step") == "install"
                for row in recipe_records
            )
            if not retained:
                continue
            artifact_values = payload.get("artifact_identities_by_node")
            if isinstance(artifact_values, Mapping):
                for node_id, values in artifact_values.items():
                    if node_id in self._capacity_artifacts and isinstance(values, list):
                        self._capacity_artifacts[str(node_id)].update(
                            str(value) for value in values
                        )

    def _prepare_capacity_campaign(
        self,
        digest: str,
        plan_recipes: list[object],
        completed: set[str],
    ) -> None:
        identities = [
            {
                "key": item.get("key"),
                "content_sha256": item.get("content_sha256"),
                "node_count": item.get("node_count"),
            }
            for item in sorted(
                (
                    _object(raw, "qualification recipe")
                    for raw in plan_recipes
                    if not _list(
                        _object(raw, "qualification recipe").get("blockers"),
                        "recipe blockers",
                    )
                ),
                key=lambda value: str(value.get("key")),
            )
        ]
        jobs: list[dict[str, object]] = []
        for raw in plan_recipes:
            item = _object(raw, "qualification recipe")
            key = str(item["key"])
            if key in completed or _list(item.get("blockers"), "recipe blockers"):
                continue
            try:
                recipe_id, revision_id = self._ensure_import(digest, item)
                detail = self.client.request(
                    "GET", f"/api/v1/library/recipes/{_quote(recipe_id)}"
                )
                selected_revision = detail.get("selected_revision")
                if (
                    not isinstance(selected_revision, Mapping)
                    or selected_revision.get("id") != revision_id
                    or selected_revision.get("content_sha256")
                    != item.get("content_sha256")
                ):
                    raise QualificationError(
                        f"{key} selected revision changed during capacity planning"
                    )
            except Exception as error:  # noqa: BLE001 - isolate recipe preparation
                self.ledger.append(
                    "recipe.failed",
                    plan_digest=digest,
                    recipe=key,
                    payload={
                        "phase": "capacity-preparation",
                        "error": str(error)[:512],
                        "error_type": type(error).__name__,
                    },
                )
                self._preflight_failed.add(key)
                continue
            restrictions = legal_blockers(detail, self.options.jurisdiction)
            if restrictions:
                self.ledger.append(
                    "recipe.blocked",
                    plan_digest=digest,
                    recipe=key,
                    payload={
                        "blockers": [blocker.as_dict() for blocker in restrictions]
                    },
                )
                self._preflight_blocked.add(key)
                continue
            placement = _list(detail.get("placement"), "recipe placement")
            recommendations = (
                _list(
                    _object(placement[0], "placement projection").get(
                        "recommendations"
                    ),
                    "placement recommendations",
                )
                if len(placement) == 1
                else []
            )
            candidates = [
                _object(value, "placement recommendation")
                for value in recommendations
                if isinstance(value, Mapping)
                and value.get("eligible") is True
                and self._candidate_allowed(value)
                and len(value.get("node_ids", [])) == item.get("node_count")
            ]
            if not candidates:
                blocker = {
                    "classification": "resource",
                    "code": "placement.no_eligible_group",
                    "detail": "No eligible exact-width placement exists.",
                }
                self.ledger.append(
                    "recipe.blocked",
                    plan_digest=digest,
                    recipe=key,
                    payload={"blockers": [blocker]},
                )
                self._preflight_blocked.add(key)
                continue
            operational = detail.get("operational_state")
            installations = (
                operational.get("installations")
                if isinstance(operational, Mapping)
                else []
            )
            if isinstance(installations, list) and any(
                self._uncertain_installation_blocks(value, revision_id)
                for value in installations
            ):
                blocker = {
                    "classification": "resource",
                    "code": "resource.installation_reconciliation_required",
                    "detail": "A partial or uncertain installation must be reconciled before retained-capacity planning.",
                }
                self.ledger.append(
                    "recipe.blocked",
                    plan_digest=digest,
                    recipe=key,
                    payload={"blockers": [blocker]},
                )
                self._preflight_blocked.add(key)
                continue
            self._prepared[key] = (recipe_id, revision_id, detail)
            jobs.append({"key": key, "item": item, "candidates": candidates})

        fleet = self.client.request("GET", "/api/v1/fleet")
        current_allocatable = {
            node_id: _node_allocatable_disk(node)
            for node in _fleet_nodes(fleet)
            if self._node_allowed(node_id := node.get("id"))
        }
        existing = [
            _object(record.get("payload"), "capacity plan payload")
            for record in self.ledger.records
            if record.get("event") == "capacity.plan.created"
            and record.get("plan_digest") == digest
        ]
        for payload in reversed(existing):
            if payload.get("recipe_identities") == identities:
                assignments = payload.get("assignments")
                if not isinstance(assignments, Mapping):
                    break
                candidate_assignments = {
                    str(key): _object(value, "capacity assignment")
                    for key, value in assignments.items()
                }
                raw_order = payload.get("execution_order")
                if (
                    not isinstance(raw_order, list)
                    or len(raw_order) != len(set(raw_order))
                    or any(not isinstance(key, str) for key in raw_order)
                    or any(key not in candidate_assignments for key in raw_order)
                    or set(raw_order)
                    != {
                        key
                        for key, assignment in candidate_assignments.items()
                        if assignment.get("preexisting_installation") is not True
                    }
                ):
                    raise QualificationError(
                        "persisted capacity execution order is invalid"
                    )
                active_candidates = {
                    str(job["key"]): {
                        _digest(_capacity_candidate_signature(candidate))
                        for candidate in _list(
                            job.get("candidates"), "capacity candidates"
                        )
                    }
                    for job in jobs
                }
                order_index = {key: index for index, key in enumerate(raw_order)}
                invalid_reasons: list[str] = []
                if (
                    payload.get("baseline_allocatable_bytes_by_node")
                    != current_allocatable
                ):
                    invalid_reasons.append(
                        "controller allocatable disk changed; global placement must be recomputed"
                    )
                for key in sorted(set(active_candidates) - set(candidate_assignments)):
                    invalid_reasons.append(f"{key}: active recipe has no assignment")
                for key, assignment in candidate_assignments.items():
                    if not isinstance(
                        assignment.get("planned_available_before_by_node"), Mapping
                    ):
                        invalid_reasons.append(
                            f"{key}: cumulative capacity evidence is missing"
                        )
                    if key in self._preflight_blocked:
                        invalid_reasons.append(f"{key}: transient preflight blocker")
                        continue
                    if key in completed:
                        continue
                    candidate_signature = assignment.get("candidate_signature")
                    if not isinstance(candidate_signature, Mapping) or _digest(
                        candidate_signature
                    ) not in active_candidates.get(key, set()):
                        invalid_reasons.append(
                            f"{key}: assigned placement is no longer eligible"
                        )
                    providers = assignment.get("artifact_provider_recipes_by_node")
                    if not isinstance(providers, Mapping):
                        invalid_reasons.append(f"{key}: provider evidence is missing")
                        continue
                    for node_providers in providers.values():
                        if not isinstance(node_providers, Mapping):
                            invalid_reasons.append(
                                f"{key}: provider evidence is invalid"
                            )
                            continue
                        for provider in node_providers.values():
                            if (
                                not isinstance(provider, str)
                                or provider not in candidate_assignments
                                or provider in self._preflight_blocked
                                or order_index.get(provider, -1)
                                >= order_index.get(key, len(raw_order))
                            ):
                                invalid_reasons.append(
                                    f"{key}: artifact provider is unavailable or unordered"
                                )
                if invalid_reasons:
                    self.ledger.append(
                        "capacity.plan.invalidated",
                        plan_digest=digest,
                        payload={
                            "previous_fleet_snapshot_sha256": payload.get(
                                "fleet_snapshot_sha256"
                            ),
                            "reasons": sorted(set(invalid_reasons)),
                            "automatic_eviction": False,
                        },
                    )
                    break
                self._capacity_assignments = candidate_assignments
                self._capacity_execution_order = list(raw_order)
                return

        remaining: dict[str, int] = {}
        artifact_providers: dict[str, dict[str, str | None]] = {}
        for node in _fleet_nodes(fleet):
            node_id = node.get("id")
            free = _node_allocatable_disk(node)
            if self._node_allowed(node_id) and isinstance(free, int):
                remaining[node_id] = free
                artifact_providers[node_id] = {}
        if not remaining and jobs:
            blocker = {
                "classification": "resource",
                "code": "resource.storage_telemetry_required",
                "detail": "No controller-reported node disk capacity is available; no install was attempted.",
                "automatic_eviction": False,
            }
            self.ledger.append("capacity.blocked", plan_digest=digest, payload=blocker)
            for job in jobs:
                key = str(job["key"])
                self.ledger.append(
                    "recipe.blocked",
                    plan_digest=digest,
                    recipe=key,
                    payload={"blockers": [blocker]},
                )
                self._preflight_blocked.add(key)
            return

        definitions: dict[str, tuple[int, int]] = {}
        for job in jobs:
            item = _object(job["item"], "qualification recipe")
            artifacts = item.get("artifact_identities")
            rows = artifacts if isinstance(artifacts, list) else []
            for raw_artifact in rows:
                artifact = _object(raw_artifact, "artifact identity")
                identity = artifact.get("identity_sha256")
                sizes = (
                    artifact.get("download_bytes"),
                    artifact.get("installed_bytes"),
                )
                if not isinstance(identity, str) or not all(
                    isinstance(value, int) for value in sizes
                ):
                    raise QualificationError(
                        f"{job['key']} artifact identity is invalid"
                    )
                previous = definitions.get(identity)
                normalized = (int(sizes[0]), int(sizes[1]))
                if previous is not None and previous != normalized:
                    raise QualificationError(
                        f"{job['key']} artifact identity has conflicting byte bounds"
                    )
                definitions[identity] = normalized

        def costs(
            job: Mapping[str, object],
            candidate: Mapping[str, object],
            node_ids: tuple[str, ...],
            resident: Mapping[str, Mapping[str, str | None]],
        ) -> tuple[
            dict[str, int],
            dict[str, int],
            dict[str, list[str]],
            dict[str, dict[str, str]],
            dict[str, int],
            dict[str, int],
        ]:
            item = _object(job["item"], "qualification recipe")
            temporary = item.get("temporary_build_bytes_per_node", 0)
            if not isinstance(temporary, int) or isinstance(temporary, bool):
                raise QualificationError(f"{job['key']} storage bounds are invalid")
            artifact_rows = item.get("artifact_identities")
            artifacts = artifact_rows if isinstance(artifact_rows, list) else []
            requirements = _object(
                item.get("disk_requirements_by_role"),
                "disk requirements by role",
            )
            persistent: dict[str, int] = {}
            peak: dict[str, int] = {}
            added: dict[str, list[str]] = {}
            dependencies: dict[str, dict[str, str]] = {}
            staging: dict[str, int] = {}
            safety_floor: dict[str, int] = {}
            candidate_nodes = candidate.get("nodes")
            candidate_node_ids = candidate.get("node_ids")
            if (
                not isinstance(candidate_node_ids, list)
                or not candidate_node_ids
                or any(not isinstance(node, str) for node in candidate_node_ids)
            ):
                raise QualificationError(
                    f"{job['key']} candidate lacks rank-ordered node identities"
                )
            builder_node_id = candidate_node_ids[0]
            if builder_node_id not in node_ids:
                raise QualificationError(
                    f"{job['key']} candidate builder is outside its placement"
                )
            role_by_node = (
                {
                    str(node.get("node_id")): str(node.get("role"))
                    for node in candidate_nodes
                    if isinstance(node, Mapping)
                    and isinstance(node.get("node_id"), str)
                    and isinstance(node.get("role"), str)
                }
                if isinstance(candidate_nodes, list)
                else {}
            )
            for node_id in node_ids:
                role = role_by_node.get(node_id)
                disk = requirements.get(role) if role is not None else None
                if not isinstance(disk, Mapping):
                    raise QualificationError(
                        f"{job['key']} lacks exact disk requirements for role {role!r}"
                    )
                disk_values: dict[str, int] = {}
                for field in (
                    "image_bytes",
                    "artifact_bytes",
                    "staging_bytes",
                    "cache_bytes",
                    "rollback_bytes",
                    "safety_margin_bytes",
                ):
                    amount = disk.get(field)
                    if (
                        not isinstance(amount, int)
                        or isinstance(amount, bool)
                        or amount < 0
                    ):
                        raise QualificationError(
                            f"{job['key']} role {role} has invalid {field}"
                        )
                    disk_values[field] = amount
                role_artifacts = [
                    value
                    for value in artifacts
                    if not _object(value, "artifact").get("roles")
                    or role in _object(value, "artifact").get("roles", [])
                ]
                missing = [
                    _object(value, "artifact")
                    for value in role_artifacts
                    if _object(value, "artifact").get("identity_sha256")
                    not in resident[node_id]
                ]
                declared_artifact_bytes = sum(
                    int(_object(value, "artifact")["installed_bytes"])
                    for value in role_artifacts
                )
                artifact_overhead = max(
                    0, disk_values["artifact_bytes"] - declared_artifact_bytes
                )
                added[node_id] = [str(value["identity_sha256"]) for value in missing]
                dependencies[node_id] = {
                    str(_object(value, "artifact")["identity_sha256"]): provider
                    for value in role_artifacts
                    if (
                        provider := resident[node_id].get(
                            str(_object(value, "artifact")["identity_sha256"])
                        )
                    )
                    is not None
                }
                persistent[node_id] = (
                    disk_values["image_bytes"]
                    + disk_values["cache_bytes"]
                    + disk_values["rollback_bytes"]
                    + artifact_overhead
                    + sum(int(value["installed_bytes"]) for value in missing)
                )
                staging[node_id] = disk_values["staging_bytes"]
                safety_floor[node_id] = max(
                    _CONTROLLER_DISK_FLOOR_BYTES,
                    disk_values["safety_margin_bytes"],
                )
                peak[node_id] = (
                    persistent[node_id]
                    + staging[node_id]
                    + safety_floor[node_id]
                    + (temporary if node_id == builder_node_id else 0)
                )
            return persistent, peak, added, dependencies, staging, safety_floor

        pinned: dict[str, Mapping[str, object]] = {}
        pending: list[Mapping[str, object]] = []
        for job in jobs:
            candidates = _list(job.get("candidates"), "capacity candidates")
            preexisting = [
                _object(value, "capacity candidate")
                for value in candidates
                if isinstance(value, Mapping) and value.get("installation_ids")
            ]
            if preexisting:
                selected = min(
                    preexisting,
                    key=lambda value: tuple(
                        sorted(str(node) for node in value["node_ids"])
                    ),
                )
                node_ids = tuple(sorted(str(node) for node in selected["node_ids"]))
                candidate_signature = _capacity_candidate_signature(selected)
                builder_node_id = str(candidate_signature["builder_node_id"])
                item = _object(job["item"], "qualification recipe")
                artifact_rows = item.get("artifact_identities")
                artifacts = artifact_rows if isinstance(artifact_rows, list) else []
                candidate_nodes = selected.get("nodes")
                role_by_node = (
                    {
                        str(node.get("node_id")): str(node.get("role"))
                        for node in candidate_nodes
                        if isinstance(node, Mapping)
                        and isinstance(node.get("node_id"), str)
                        and isinstance(node.get("role"), str)
                    }
                    if isinstance(candidate_nodes, list)
                    else {}
                )
                added = {
                    node_id: [
                        str(_object(value, "artifact")["identity_sha256"])
                        for value in artifacts
                        if not _object(value, "artifact").get("roles")
                        or role_by_node.get(node_id)
                        in _object(value, "artifact").get("roles", [])
                    ]
                    for node_id in node_ids
                }
                for node_id, values in added.items():
                    artifact_providers[node_id].update(
                        {identity: None for identity in values}
                    )
                pinned[str(job["key"])] = {
                    "node_ids": list(node_ids),
                    "builder_node_id": builder_node_id,
                    "installation_ids": candidate_signature["installation_ids"],
                    "candidate_signature": candidate_signature,
                    "planned_available_before_by_node": {
                        node_id: remaining[node_id] for node_id in node_ids
                    },
                    "persistent_bytes_by_node": {node_id: 0 for node_id in node_ids},
                    "peak_bytes_by_node": {node_id: 0 for node_id in node_ids},
                    "artifact_identities_by_node": added,
                    "artifact_provider_recipes_by_node": {
                        node_id: {} for node_id in node_ids
                    },
                    "staging_bytes_by_node": {node_id: 0 for node_id in node_ids},
                    "safety_floor_bytes_by_node": {node_id: 0 for node_id in node_ids},
                    "preexisting_installation": True,
                }
            else:
                pending.append(job)

        def job_priority(job: Mapping[str, object]) -> tuple[int, int, str]:
            item = _object(job["item"], "recipe")
            temporary = item.get("temporary_build_bytes_per_node", 0)
            requirements = item.get("disk_requirements_by_role")
            transient = int(temporary) if isinstance(temporary, int) else 0
            if isinstance(requirements, Mapping):
                transient += max(
                    (
                        int(disk.get("staging_bytes", 0))
                        + max(
                            _CONTROLLER_DISK_FLOOR_BYTES,
                            int(disk.get("safety_margin_bytes", 0)),
                        )
                        for disk in requirements.values()
                        if isinstance(disk, Mapping)
                    ),
                    default=_CONTROLLER_DISK_FLOOR_BYTES,
                )
            maximum = item.get("maximum_installed_bytes_per_node", 0)
            return (
                -transient,
                -int(maximum) if isinstance(maximum, int) else 0,
                str(job["key"]),
            )

        pending.sort(key=job_priority)
        pending_by_key = {str(job["key"]): job for job in pending}
        expansions = 0
        solution: dict[str, Mapping[str, object]] | None = None
        solution_order: list[str] | None = None
        dead_states: set[tuple[object, ...]] = set()

        def search(remaining_keys: tuple[str, ...], order: tuple[str, ...]) -> bool:
            nonlocal expansions, solution, solution_order
            if not remaining_keys:
                solution = dict(pinned)
                solution_order = list(order)
                return True
            if expansions >= 250_000:
                return False
            state = (
                remaining_keys,
                tuple(sorted(remaining.items())),
                tuple(
                    (node_id, tuple(sorted(providers)))
                    for node_id, providers in sorted(artifact_providers.items())
                ),
            )
            if state in dead_states:
                return False
            for key in remaining_keys:
                job = pending_by_key[key]
                candidate_rows = []
                for raw_candidate in _list(
                    job.get("candidates"), "capacity candidates"
                ):
                    candidate = _object(raw_candidate, "capacity candidate")
                    candidate_signature = _capacity_candidate_signature(candidate)
                    raw_nodes = candidate.get("node_ids")
                    if not isinstance(raw_nodes, list) or not raw_nodes:
                        continue
                    node_ids = tuple(sorted(str(node) for node in raw_nodes))
                    if any(node_id not in remaining for node_id in node_ids):
                        continue
                    (
                        persistent,
                        peak,
                        added,
                        dependencies,
                        staging,
                        safety_floor,
                    ) = costs(job, candidate, node_ids, artifact_providers)
                    projected = [remaining[node] - peak[node] for node in node_ids]
                    if min(projected, default=-1) < 0:
                        continue
                    post_persistent = dict(remaining)
                    for node_id in node_ids:
                        post_persistent[node_id] -= persistent[node_id]
                    projected_imbalance = max(post_persistent.values()) - min(
                        post_persistent.values()
                    )
                    candidate_rows.append(
                        (
                            max(persistent.values(), default=0),
                            projected_imbalance,
                            node_ids,
                            candidate_signature,
                            persistent,
                            peak,
                            added,
                            dependencies,
                            staging,
                            safety_floor,
                        )
                    )
                candidate_rows.sort(key=lambda value: (value[0], value[1], value[2]))
                for (
                    _,
                    _,
                    node_ids,
                    candidate_signature,
                    persistent,
                    peak,
                    added,
                    dependencies,
                    staging,
                    safety_floor,
                ) in candidate_rows:
                    expansions += 1
                    builder_node_id = str(candidate_signature["builder_node_id"])
                    planned_available_before = {
                        node_id: remaining[node_id] for node_id in node_ids
                    }
                    for node_id in node_ids:
                        remaining[node_id] -= persistent[node_id]
                        artifact_providers[node_id].update(
                            {identity: key for identity in added[node_id]}
                        )
                    pinned[key] = {
                        "node_ids": list(node_ids),
                        "builder_node_id": builder_node_id,
                        "installation_ids": [],
                        "candidate_signature": candidate_signature,
                        "planned_available_before_by_node": planned_available_before,
                        "persistent_bytes_by_node": persistent,
                        "peak_bytes_by_node": peak,
                        "artifact_identities_by_node": added,
                        "artifact_provider_recipes_by_node": dependencies,
                        "staging_bytes_by_node": staging,
                        "safety_floor_bytes_by_node": safety_floor,
                        "preexisting_installation": False,
                    }
                    next_keys = tuple(value for value in remaining_keys if value != key)
                    if search(next_keys, (*order, key)):
                        return True
                    pinned.pop(key, None)
                    for node_id in node_ids:
                        remaining[node_id] += persistent[node_id]
                    artifact_providers.clear()
                    artifact_providers.update({node_id: {} for node_id in remaining})
                    for provider_key, assignment in pinned.items():
                        for node_id, values in _object(
                            assignment.get("artifact_identities_by_node"),
                            "artifacts",
                        ).items():
                            provider = (
                                None
                                if assignment.get("preexisting_installation") is True
                                else provider_key
                            )
                            artifact_providers[str(node_id)].update(
                                {str(value): provider for value in values}
                            )
                    if expansions >= 250_000:
                        return False
            dead_states.add(state)
            return False

        if not search(tuple(str(job["key"]) for job in pending), ()):
            code = (
                "resource.capacity_search_limit"
                if expansions >= 250_000
                else "resource.retained_capacity_no_global_plan"
            )
            alternatives: dict[str, list[dict[str, object]]] = {}
            for job in pending:
                rows: list[dict[str, object]] = []
                for raw_candidate in _list(
                    job.get("candidates"), "capacity candidates"
                ):
                    candidate = _object(raw_candidate, "capacity candidate")
                    raw_nodes = candidate.get("node_ids")
                    if not isinstance(raw_nodes, list):
                        continue
                    node_ids = tuple(sorted(str(node) for node in raw_nodes))
                    if any(node_id not in remaining for node_id in node_ids):
                        continue
                    persistent, peak, _, _, staging, safety_floor = costs(
                        job, candidate, node_ids, artifact_providers
                    )
                    rows.append(
                        {
                            "node_ids": list(node_ids),
                            "persistent_bytes_by_node": persistent,
                            "peak_bytes_by_node": peak,
                            "staging_bytes_by_node": staging,
                            "safety_floor_bytes_by_node": safety_floor,
                            "shortfall_bytes_by_node": {
                                node_id: max(0, peak[node_id] - remaining[node_id])
                                for node_id in node_ids
                            },
                        }
                    )
                alternatives[str(job["key"])] = rows
            evidence = {
                "classification": "resource",
                "code": code,
                "detail": "No complete retained-capacity placement was proven; no eviction was attempted.",
                "expansions": expansions,
                "node_free_bytes": remaining,
                "candidate_alternatives": alternatives,
                "automatic_eviction": False,
            }
            self.ledger.append("capacity.blocked", plan_digest=digest, payload=evidence)
            for job in pending:
                key = str(job["key"])
                self.ledger.append(
                    "recipe.blocked",
                    plan_digest=digest,
                    recipe=key,
                    payload={"blockers": [evidence]},
                )
                self._preflight_blocked.add(key)
            return
        assert solution is not None
        assert solution_order is not None
        payload = {
            "recipe_identities": identities,
            "fleet_snapshot_sha256": _digest(_fleet_fingerprint(fleet)),
            "baseline_free_bytes_by_node": {
                node_id: _node_available_disk(node)
                for node in _fleet_nodes(fleet)
                if self._node_allowed(node_id := node.get("id"))
            },
            "baseline_reserved_bytes_by_node": {
                node_id: (
                    node.get("reservations", {}).get("disk_bytes", 0)
                    if isinstance(node.get("reservations"), Mapping)
                    else 0
                )
                for node in _fleet_nodes(fleet)
                if self._node_allowed(node_id := node.get("id"))
            },
            "baseline_allocatable_bytes_by_node": {
                node_id: _node_allocatable_disk(node)
                for node in _fleet_nodes(fleet)
                if self._node_allowed(node_id := node.get("id"))
            },
            "assignments": solution,
            "execution_order": solution_order,
            "search_expansions": expansions,
            "automatic_eviction": False,
        }
        self.ledger.append("capacity.plan.created", plan_digest=digest, payload=payload)
        self._capacity_assignments = solution
        self._capacity_execution_order = list(payload["execution_order"])

    def _capacity_dependency_blockers(
        self, digest: str, key: str
    ) -> list[dict[str, object]]:
        assignment = self._capacity_assignments.get(key)
        if assignment is None:
            return []
        provider_rows = assignment.get("artifact_provider_recipes_by_node")
        if not isinstance(provider_rows, Mapping):
            return [
                {
                    "classification": "resource",
                    "code": "resource.capacity_provider_evidence_missing",
                    "detail": "The immutable capacity assignment lacks artifact-provider evidence.",
                    "automatic_eviction": False,
                }
            ]
        unavailable: dict[str, dict[str, list[str]]] = {}
        for node_id, raw_providers in provider_rows.items():
            if not isinstance(raw_providers, Mapping):
                return [
                    {
                        "classification": "resource",
                        "code": "resource.capacity_provider_evidence_invalid",
                        "detail": "The immutable capacity assignment has invalid artifact-provider evidence.",
                        "automatic_eviction": False,
                    }
                ]
            for identity, provider in raw_providers.items():
                if not isinstance(identity, str) or not isinstance(provider, str):
                    return [
                        {
                            "classification": "resource",
                            "code": "resource.capacity_provider_evidence_invalid",
                            "detail": "The immutable capacity assignment has invalid artifact-provider evidence.",
                            "automatic_eviction": False,
                        }
                    ]
                provider_assignment = self._capacity_assignments.get(provider)
                provider_ready = (
                    provider_assignment is not None
                    and provider_assignment.get("preexisting_installation") is True
                ) or self._completed_operation(digest, provider, "install") is not None
                if not provider_ready:
                    unavailable.setdefault(provider, {}).setdefault(
                        str(node_id), []
                    ).append(identity)
        if not unavailable:
            return []
        return [
            {
                "classification": "resource",
                "code": "resource.capacity_provider_unavailable",
                "detail": "A planned retained-artifact provider did not complete installation; the dependent zero-cost assignment was not used.",
                "providers": {
                    provider: {
                        node_id: sorted(identities)
                        for node_id, identities in sorted(nodes.items())
                    }
                    for provider, nodes in sorted(unavailable.items())
                },
                "automatic_eviction": False,
            }
        ]

    def _select_placement(
        self,
        digest: str,
        key: str,
        detail: Mapping[str, object],
        item: Mapping[str, object],
    ) -> Mapping[str, object]:
        placement = _list(detail.get("placement"), "recipe placement")
        if len(placement) != 1:
            raise QualificationError(f"{key} has no bounded placement projection")
        projection = _object(placement[0], "placement projection")
        recommendations = _list(projection.get("recommendations"), "recommendations")
        eligible = [
            _object(recommendation, "placement recommendation")
            for recommendation in recommendations
            if isinstance(recommendation, Mapping)
            and recommendation.get("eligible") is True
            and self._candidate_allowed(recommendation)
            and (
                not isinstance(item.get("node_count"), int)
                or len(recommendation.get("node_ids", [])) == item.get("node_count")
            )
        ]
        if not eligible:
            evidence = {
                "classification": "resource",
                "code": "placement.no_eligible_group",
                "projection": projection,
            }
            self.ledger.append(
                "recipe.blocked",
                plan_digest=digest,
                recipe=key,
                payload={"blockers": [evidence]},
            )
            raise QualificationError(
                f"{key} has no eligible one- or two-Spark placement"
            )
        planned_assignment = self._capacity_assignments.get(key)
        if planned_assignment is not None:
            planned_nodes = planned_assignment.get("node_ids")
            planned_builder = planned_assignment.get("builder_node_id")
            planned_installation_ids = planned_assignment.get("installation_ids")
            planned_signature = planned_assignment.get("candidate_signature")
            if (
                not isinstance(planned_nodes, list)
                or not isinstance(planned_builder, str)
                or not isinstance(planned_installation_ids, list)
                or not isinstance(planned_signature, Mapping)
            ):
                raise QualificationError(f"{key} capacity assignment is invalid")
            selected = next(
                (
                    candidate
                    for candidate in eligible
                    if _capacity_candidate_signature(candidate) == planned_signature
                ),
                None,
            )
            if selected is None:
                evidence = {
                    "classification": "resource",
                    "code": "resource.planned_placement_ineligible",
                    "detail": "The immutable retained-capacity placement is no longer controller-eligible; no mutation was attempted.",
                    "planned_node_ids": planned_nodes,
                    "planned_builder_node_id": planned_builder,
                    "planned_installation_ids": planned_installation_ids,
                    "eligible_node_groups": [
                        sorted(str(node) for node in candidate.get("node_ids", []))
                        for candidate in eligible
                    ],
                    "automatic_eviction": False,
                }
                self.ledger.append(
                    "recipe.blocked",
                    plan_digest=digest,
                    recipe=key,
                    payload={"blockers": [evidence]},
                )
                raise QualificationError(
                    f"{key} planned retained placement is no longer eligible"
                )
            fleet = self.client.request("GET", "/api/v1/fleet")
            available = {
                str(node.get("id")): _node_allocatable_disk(node)
                for node in _fleet_nodes(fleet)
                if isinstance(node.get("id"), str)
            }
            peak = _object(
                planned_assignment.get("peak_bytes_by_node"),
                "planned peak bytes",
            )
            planned_available = _object(
                planned_assignment.get("planned_available_before_by_node"),
                "planned available bytes",
            )
            currently_installed = bool(selected.get("installation_ids"))
            checks = {
                node_id: {
                    "available_bytes": available.get(node_id),
                    "planned_available_before_bytes": planned_available.get(node_id),
                    "baseline_preserved": isinstance(available.get(node_id), int)
                    and isinstance(planned_available.get(node_id), int)
                    and int(available[node_id]) >= int(planned_available[node_id]),
                    "peak_required_bytes": 0 if currently_installed else value,
                    "planned_peak_bytes": value,
                    "fits": isinstance(available.get(node_id), int)
                    and isinstance(planned_available.get(node_id), int)
                    and (
                        currently_installed
                        or int(available[node_id]) >= int(planned_available[node_id])
                        and isinstance(value, int)
                        and int(planned_available[node_id]) >= value
                    ),
                }
                for node_id, value in peak.items()
            }
            self.ledger.append(
                "capacity.checked",
                plan_digest=digest,
                recipe=key,
                payload={
                    "nodes": checks,
                    "fleet_snapshot_sha256": _digest(_fleet_fingerprint(fleet)),
                    "automatic_eviction": False,
                },
            )
            if not all(value["fits"] is True for value in checks.values()):
                evidence = {
                    "classification": "resource",
                    "code": "resource.capacity_drift",
                    "detail": "Current controller-reported free disk no longer satisfies the immutable capacity assignment.",
                    "nodes": checks,
                    "automatic_eviction": False,
                }
                self.ledger.append(
                    "capacity.blocked",
                    plan_digest=digest,
                    recipe=key,
                    payload=evidence,
                )
                self.ledger.append(
                    "recipe.blocked",
                    plan_digest=digest,
                    recipe=key,
                    payload={"blockers": [evidence]},
                )
                raise QualificationError(f"{key} retained-capacity assignment drifted")
            self.ledger.append(
                "capacity.assigned",
                plan_digest=digest,
                recipe=key,
                payload={
                    "node_ids": planned_nodes,
                    "incremental_bytes_by_node": {
                        node_id: 0 for node_id in planned_nodes
                    }
                    if currently_installed
                    else planned_assignment.get("persistent_bytes_by_node"),
                    "peak_bytes_by_node": {node_id: 0 for node_id in planned_nodes}
                    if currently_installed
                    else dict(peak),
                    "artifact_identities_by_node": planned_assignment.get(
                        "artifact_identities_by_node"
                    ),
                    "preexisting_installation": currently_installed
                    or planned_assignment.get("preexisting_installation"),
                    "automatic_eviction": False,
                },
            )
            self._record_preview(digest, key, "placement", selected)
            return selected
        selected = eligible[0]
        maximum_installed = item.get("maximum_installed_bytes_per_node")
        if isinstance(maximum_installed, int) and not isinstance(
            maximum_installed, bool
        ):
            self._initialize_capacity_state(digest)
            artifacts = item.get("artifact_identities")
            artifact_rows = artifacts if isinstance(artifacts, list) else []
            temporary = item.get("temporary_build_bytes_per_node", 0)
            temporary_bytes = temporary if isinstance(temporary, int) else 0
            alternatives: list[
                tuple[
                    int,
                    tuple[str, ...],
                    Mapping[str, object],
                    dict[str, int],
                    dict[str, int],
                    dict[str, list[str]],
                ]
            ] = []
            for recommendation in eligible:
                raw_node_ids = recommendation.get("node_ids")
                if not isinstance(raw_node_ids, list) or any(
                    not isinstance(node_id, str) for node_id in raw_node_ids
                ):
                    continue
                persistent: dict[str, int] = {}
                peak: dict[str, int] = {}
                identities: dict[str, list[str]] = {}
                preexisting = bool(recommendation.get("installation_ids"))
                for node_id in raw_node_ids:
                    known = self._capacity_artifacts.get(node_id, set())
                    savings = sum(
                        int(artifact.get("installed_bytes", 0))
                        for artifact in artifact_rows
                        if isinstance(artifact, Mapping)
                        and artifact.get("identity_sha256") in known
                    )
                    persistent[node_id] = (
                        0 if preexisting else max(0, maximum_installed - savings)
                    )
                    peak[node_id] = persistent[node_id] + (
                        0 if preexisting else temporary_bytes
                    )
                    identities[node_id] = [
                        str(artifact["identity_sha256"])
                        for artifact in artifact_rows
                        if isinstance(artifact, Mapping)
                        and isinstance(artifact.get("identity_sha256"), str)
                    ]
                remaining = [
                    self._capacity_remaining.get(node_id, -1) - peak[node_id]
                    for node_id in raw_node_ids
                ]
                alternatives.append(
                    (
                        min(remaining, default=-1),
                        tuple(sorted(raw_node_ids)),
                        recommendation,
                        persistent,
                        peak,
                        identities,
                    )
                )
            alternatives.sort(key=lambda row: (-row[0], row[1]))
            if not alternatives or alternatives[0][0] < 0:
                capacity_plan = {
                    "classification": "resource",
                    "code": "resource.retained_capacity_no_placement",
                    "alternatives": [
                        {
                            "node_ids": list(node_ids),
                            "minimum_remaining_bytes": remaining,
                            "incremental_bytes_by_node": persistent,
                            "peak_bytes_by_node": peak,
                        }
                        for remaining, node_ids, _, persistent, peak, _ in alternatives
                    ],
                    "automatic_eviction": False,
                }
                self.ledger.append(
                    "capacity.blocked",
                    plan_digest=digest,
                    recipe=key,
                    payload=capacity_plan,
                )
                self.ledger.append(
                    "recipe.blocked",
                    plan_digest=digest,
                    recipe=key,
                    payload={"blockers": [capacity_plan]},
                )
                raise QualificationError(
                    f"{key} has no retained-capacity placement without eviction"
                )
            _, _, selected, persistent, peak, identities = alternatives[0]
            for node_id, value in persistent.items():
                self._capacity_remaining[node_id] -= value
                self._capacity_artifacts[node_id].update(identities[node_id])
            self.ledger.append(
                "capacity.assigned",
                plan_digest=digest,
                recipe=key,
                payload={
                    "node_ids": selected.get("node_ids"),
                    "incremental_bytes_by_node": persistent,
                    "peak_bytes_by_node": peak,
                    "artifact_identities_by_node": identities,
                    "projected_remaining_bytes_by_node": {
                        node_id: self._capacity_remaining[node_id]
                        for node_id in persistent
                    },
                    "minimum_peak_remaining_bytes_by_node": {
                        node_id: self._capacity_remaining[node_id]
                        - (peak[node_id] - persistent[node_id])
                        for node_id in peak
                    },
                    "preexisting_installation": bool(selected.get("installation_ids")),
                    "automatic_eviction": False,
                },
            )
        self._record_preview(digest, key, "placement", selected)
        return selected

    def apply(
        self, plan: Mapping[str, object], expected_digest: str
    ) -> dict[str, object]:
        actual = plan.get("plan_digest")
        if actual != expected_digest:
            raise QualificationError(
                "--plan-digest does not match the current catalog/fleet plan"
            )
        intent = _object(plan.get("campaign_intent"), "campaign intent")
        if _digest(intent) != actual:
            raise QualificationError("campaign plan content does not match its digest")
        intent_options = _object(intent.get("options"), "campaign intent options")
        if (
            intent_options.get("cleanup") != self.options.cleanup
            or intent_options.get("jurisdiction") != self.options.jurisdiction
            or intent_options.get("selected_recipes")
            != sorted(self.options.selected_recipes)
            or intent_options.get("allowed_node_ids", [])
            != sorted(self.options.allowed_node_ids)
        ):
            raise QualificationError("runner options do not match campaign intent")
        manifest_digest = intent_options.get("fixture_manifest_sha256")
        if (
            manifest_digest != self.artifact_smoke.fixtures.manifest_sha256
            or manifest_digest != self.service_smoke.fixtures.manifest_sha256
        ):
            raise QualificationError(
                "qualification fixture authority does not match campaign intent"
            )
        intent_recipes = _list(intent.get("recipes"), "campaign intent recipes")
        plan_recipes = _list(plan.get("recipes"), "qualification recipes")
        projected_recipes = []
        for raw in plan_recipes:
            item = _object(raw, "qualification recipe")
            blockers = _list(item.get("blockers"), "recipe blockers")
            projected_recipes.append(
                {
                    "key": item.get("key"),
                    "uri": item.get("uri"),
                    "content_sha256": item.get("content_sha256"),
                    "release_version": item.get("release_version"),
                    "node_count": item.get("node_count"),
                    "temporary_build_bytes_per_node": item.get(
                        "temporary_build_bytes_per_node"
                    ),
                    "disk_requirements_by_role": item.get(
                        "disk_requirements_by_role"
                    ),
                    "artifact_identities": item.get("artifact_identities"),
                    "immutable_blockers": [
                        blocker
                        for blocker in blockers
                        if isinstance(blocker, Mapping)
                        and blocker.get("code")
                        not in {
                            "topology.insufficient_online_nodes",
                            "resource.memory_exceeds_fleet",
                        }
                    ],
                }
            )
        projected_recipes.sort(key=lambda item: str(item["key"]))
        if projected_recipes != intent_recipes:
            raise QualificationError(
                "actionable recipe rows do not match campaign intent"
            )
        digest = str(actual)
        self.ledger.append(
            "run.started",
            plan_digest=digest,
            payload={"catalog": plan.get("catalog"), "fleet": plan.get("fleet")},
        )
        completed = self.ledger.completed_recipes(digest)
        summary = {
            "succeeded": 0,
            "failed": 0,
            "blocked": 0,
            "resumed": len(completed),
        }
        primary_error: Exception | None = None
        inventory_error: Exception | None = None
        residency: dict[str, object] = {}
        try:
            self._prepare_capacity_campaign(digest, plan_recipes, completed)
            recipes_by_key = {
                str(_object(raw, "qualification recipe")["key"]): raw
                for raw in plan_recipes
            }
            if len(recipes_by_key) != len(plan_recipes):
                raise QualificationError(
                    "qualification plan recipe keys are not unique"
                )
            execution_keys = [
                *self._capacity_execution_order,
                *sorted(set(recipes_by_key) - set(self._capacity_execution_order)),
            ]
            for key in execution_keys:
                raw = recipes_by_key[key]
                item = _object(raw, "qualification recipe")
                if key in completed:
                    continue
                if key in self._preflight_blocked:
                    summary["blocked"] += 1
                    continue
                if key in self._preflight_failed:
                    summary["failed"] += 1
                    continue
                blockers = _list(item.get("blockers"), "recipe blockers")
                if blockers:
                    self.ledger.append(
                        "recipe.blocked",
                        plan_digest=digest,
                        recipe=key,
                        payload={"blockers": blockers},
                    )
                    summary["blocked"] += 1
                    continue
                dependency_blockers = self._capacity_dependency_blockers(digest, key)
                if dependency_blockers:
                    self.ledger.append(
                        "recipe.blocked",
                        plan_digest=digest,
                        recipe=key,
                        payload={"blockers": dependency_blockers},
                    )
                    summary["blocked"] += 1
                    continue
                before = len(self.ledger.records)
                try:
                    self._apply_recipe(digest, item)
                except Exception as error:  # noqa: BLE001 - isolate recipe failures
                    created = self.ledger.records[before:]
                    if any(row.get("event") == "recipe.blocked" for row in created):
                        summary["blocked"] += 1
                        continue
                    self.ledger.append(
                        "recipe.failed",
                        plan_digest=digest,
                        recipe=key,
                        payload={
                            "error": str(error)[:512],
                            "error_type": type(error).__name__,
                        },
                    )
                    summary["failed"] += 1
                    continue
                summary["succeeded"] += 1
        except Exception as error:  # noqa: BLE001 - preserve primary campaign failure
            primary_error = error
        finally:
            try:
                residency = self._residency_inventory(digest, plan)
                self.ledger.append(
                    "run.residency-inventoried",
                    plan_digest=digest,
                    payload=residency,
                )
            except Exception as error:  # noqa: BLE001 - inventory must not mask primary
                inventory_error = error
                self.ledger.append(
                    "run.residency-inventory-failed",
                    plan_digest=digest,
                    payload={
                        "error": str(error)[:512],
                        "error_type": type(error).__name__,
                        "original_error": str(primary_error)[:512]
                        if primary_error is not None
                        else None,
                    },
                )
        if primary_error is not None:
            raise primary_error
        if inventory_error is not None:
            raise inventory_error
        completed_payload = {**summary, "residency": residency}
        self.ledger.append(
            "run.completed-with-failures" if summary["failed"] else "run.completed",
            plan_digest=digest,
            payload=completed_payload,
        )
        if summary["failed"]:
            raise QualificationError(
                f"qualification completed with {summary['failed']} failed recipe(s); "
                "see the evidence ledger for the complete residency inventory"
            )
        return {"plan_digest": digest, **completed_payload}

    def _global_installation_inventory(
        self, campaign_rows: list[dict[str, object]]
    ) -> dict[str, object]:
        summaries: list[Mapping[str, object]] = []
        errors: list[dict[str, str]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(1_000):
            query: dict[str, object] = {"limit": 100}
            if cursor is not None:
                query["cursor"] = cursor
            try:
                snapshot = self.client.request("GET", "/api/v1/library", query=query)
            except Exception as error:  # noqa: BLE001 - record bounded inventory gaps
                errors.append(
                    {
                        "scope": "library",
                        "error": str(error)[:256],
                        "error_type": type(error).__name__,
                    }
                )
                break
            models = snapshot.get("models")
            if isinstance(models, list):
                for raw_model in models:
                    if not isinstance(raw_model, Mapping):
                        continue
                    recipes = raw_model.get("recipes")
                    if isinstance(recipes, list):
                        summaries.extend(
                            recipe for recipe in recipes if isinstance(recipe, Mapping)
                        )
            unlinked = snapshot.get("unlinked_recipes")
            if isinstance(unlinked, list):
                summaries.extend(
                    recipe for recipe in unlinked if isinstance(recipe, Mapping)
                )
            next_cursor = snapshot.get("next_cursor")
            if next_cursor is None:
                break
            if not isinstance(next_cursor, str) or next_cursor in seen_cursors:
                errors.append(
                    {
                        "scope": "library",
                        "error": "library pagination cursor is invalid",
                        "error_type": "QualificationError",
                    }
                )
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            errors.append(
                {
                    "scope": "library",
                    "error": "library pagination exceeded its bound",
                    "error_type": "QualificationError",
                }
            )

        evidence_by_revision = {
            (row.get("recipe_id"), row.get("recipe_revision_id")): row
            for row in campaign_rows
            if isinstance(row.get("recipe_id"), str)
            and isinstance(row.get("recipe_revision_id"), str)
        }
        installations: list[dict[str, object]] = []
        seen_installations: set[str] = set()
        for summary in summaries:
            recipe_id = summary.get("recipe_id")
            if not isinstance(recipe_id, str):
                continue
            selected = summary.get("selected_revision")
            selected_id = selected.get("id") if isinstance(selected, Mapping) else None
            raw_installations = summary.get("installations")
            installation_rows = (
                [item for item in raw_installations if isinstance(item, Mapping)]
                if isinstance(raw_installations, list)
                else []
            )
            if summary.get("installation_total_count", 0):
                try:
                    detail = self.client.request(
                        "GET", f"/api/v1/library/recipes/{_quote(recipe_id)}"
                    )
                    operational = detail.get("operational_state")
                    detailed = (
                        operational.get("installations")
                        if isinstance(operational, Mapping)
                        else None
                    )
                    if isinstance(detailed, list):
                        installation_rows = [
                            item for item in detailed if isinstance(item, Mapping)
                        ]
                except Exception as error:  # noqa: BLE001 - continue complete inventory
                    errors.append(
                        {
                            "scope": f"recipe:{recipe_id}",
                            "error": str(error)[:256],
                            "error_type": type(error).__name__,
                        }
                    )
            for installation in installation_rows:
                installation_id = installation.get("installation_id")
                revision_id = installation.get("recipe_revision_id")
                state = installation.get("state")
                if (
                    not isinstance(installation_id, str)
                    or installation_id in seen_installations
                ):
                    continue
                seen_installations.add(installation_id)
                evidence = evidence_by_revision.get((recipe_id, revision_id))
                retained = state != "uninstalled"
                if not retained:
                    deployability = "uninstalled"
                elif revision_id != selected_id:
                    deployability = "stale-revision-retained"
                elif evidence is not None:
                    deployability = evidence.get(
                        "deployability", "retained-unqualified"
                    )
                elif state == "installed":
                    deployability = "retained-unqualified"
                else:
                    deployability = f"retained-{state or 'unknown'}"
                installations.append(
                    {
                        "installation_id": installation_id,
                        "recipe_id": recipe_id,
                        "recipe_slug": summary.get("slug"),
                        "recipe_revision_id": revision_id,
                        "selected_recipe_revision_id": selected_id,
                        "selected_revision_match": revision_id == selected_id,
                        "state": state,
                        "node_ids": installation.get("node_ids", []),
                        "retained": retained,
                        "deployability": deployability,
                        "campaign_recipe": evidence.get("recipe")
                        if evidence is not None
                        else None,
                    }
                )
        installations.sort(key=lambda item: str(item["installation_id"]))
        return {
            "complete": not errors
            and not any(
                summary.get("installations_truncated") is True for summary in summaries
            ),
            "installations": installations,
            "errors": errors,
        }

    def _residency_inventory(
        self, digest: str, plan: Mapping[str, object]
    ) -> dict[str, object]:
        rows: list[dict[str, object]] = []
        any_installation = False
        for raw_item in _list(plan.get("recipes"), "qualification recipes"):
            item = _object(raw_item, "qualification recipe")
            key = str(item.get("key"))
            records = self.ledger.recipe_records(digest, key)
            installation_ids: set[str] = set()
            uninstalled: set[str] = set()
            recipe_id = item.get("local_recipe_id")
            revision_id = item.get("local_revision_id")
            for record in records:
                payload = record.get("payload")
                if not isinstance(payload, Mapping):
                    continue
                event = record.get("event")
                if event == "recipe.imported":
                    result = payload.get("result")
                    if isinstance(result, Mapping):
                        recipe_id = result.get("recipe_id", recipe_id)
                        revision_id = result.get("id", revision_id)
                if event == "recipe.succeeded":
                    candidate = payload.get("installation_id")
                    if isinstance(candidate, str):
                        installation_ids.add(candidate)
                    recipe_id = payload.get("recipe_id", recipe_id)
                    revision_id = payload.get("recipe_revision_id", revision_id)
                if event in {"cleanup.retained", "cleanup.skipped"}:
                    candidate = payload.get("installation_id")
                    if isinstance(candidate, str):
                        installation_ids.add(candidate)
                if event == "operation.completed":
                    operation = payload.get("operation")
                    if isinstance(operation, Mapping):
                        owner_id = operation.get("owner_id")
                        if payload.get("step") == "install" and isinstance(
                            owner_id, str
                        ):
                            installation_ids.add(owner_id)
                        if payload.get("step") == "uninstall" and isinstance(
                            owner_id, str
                        ):
                            uninstalled.add(owner_id)
                if event == "step.previewed" and payload.get("step") == "placement":
                    preview = payload.get("preview")
                    candidates = (
                        preview.get("installation_ids")
                        if isinstance(preview, Mapping)
                        else None
                    )
                    if isinstance(candidates, list):
                        installation_ids.update(
                            candidate
                            for candidate in candidates
                            if isinstance(candidate, str)
                        )
            installation_ids -= uninstalled
            any_installation = any_installation or bool(installation_ids)
            succeeded = any(
                record.get("event") == "recipe.succeeded" for record in records
            )
            blocked = any(record.get("event") == "recipe.blocked" for record in records)
            failed = any(record.get("event") == "recipe.failed" for record in records)
            warm_smoke = (
                _latest_step(records, "step.completed", "warm-redeploy-smoke")
                is not None
            )
            if installation_ids and succeeded and warm_smoke:
                deployability = "deployable-retained"
            elif installation_ids and blocked:
                deployability = "retained-blocked"
            elif installation_ids and failed:
                deployability = "retained-failed"
            elif installation_ids:
                deployability = "retained-unqualified"
            elif blocked:
                deployability = "not-installed-blocked"
            elif failed:
                deployability = "not-installed-failed"
            else:
                deployability = "not-attempted"
            operational: Mapping[str, object] = {}
            if isinstance(recipe_id, str):
                try:
                    detail = self.client.request(
                        "GET", f"/api/v1/library/recipes/{_quote(recipe_id)}"
                    )
                    candidate = detail.get("operational_state")
                    if isinstance(candidate, Mapping):
                        operational = candidate
                        installations = candidate.get("installations")
                        if isinstance(installations, list):
                            installation_ids.update(
                                str(installation.get("installation_id"))
                                for installation in installations
                                if isinstance(installation, Mapping)
                                and isinstance(installation.get("installation_id"), str)
                                and installation.get("state") != "uninstalled"
                            )
                except Exception as error:  # noqa: BLE001 - retain disposition evidence
                    operational = {
                        "inventory_error": str(error)[:256],
                        "inventory_error_type": type(error).__name__,
                    }
            any_installation = any_installation or bool(installation_ids)
            if installation_ids and succeeded and warm_smoke:
                deployability = "deployable-retained"
            elif installation_ids and blocked:
                deployability = "retained-blocked"
            elif installation_ids and failed:
                deployability = "retained-failed"
            elif installation_ids:
                deployability = "retained-unqualified"
            rows.append(
                {
                    "recipe": key,
                    "recipe_id": recipe_id,
                    "recipe_revision_id": revision_id,
                    "recipe_content_sha256": item.get("content_sha256"),
                    "installation_ids": sorted(installation_ids),
                    "retained": bool(installation_ids),
                    "deployability": deployability,
                    "disposition_events": [
                        str(record.get("event"))
                        for record in records
                        if record.get("event")
                        in {"recipe.blocked", "recipe.failed", "recipe.succeeded"}
                    ],
                    "operational_state": dict(operational),
                }
            )
        global_inventory = self._global_installation_inventory(rows)
        retained_installations = [
            installation
            for installation in global_inventory["installations"]
            if isinstance(installation, Mapping)
            and installation.get("retained") is True
        ]
        any_installation = any_installation or bool(retained_installations)
        fleet = self.client.request("GET", "/api/v1/fleet")
        assigned_bytes: dict[str, int] = {}
        artifact_ids: dict[str, set[str]] = {}
        assignments: dict[str, Mapping[str, object]] = {}
        for record in self.ledger.records:
            if (
                record.get("plan_digest") != digest
                or record.get("event") != "capacity.assigned"
                or not isinstance(record.get("payload"), Mapping)
            ):
                continue
            recipe = record.get("recipe")
            if isinstance(recipe, str):
                assignments[recipe] = _object(record["payload"], "capacity assignment")
        for payload in assignments.values():
            increments = payload.get("incremental_bytes_by_node")
            identities = payload.get("artifact_identities_by_node")
            if isinstance(increments, Mapping):
                for node_id, value in increments.items():
                    if isinstance(value, int):
                        assigned_bytes[str(node_id)] = (
                            assigned_bytes.get(str(node_id), 0) + value
                        )
            if isinstance(identities, Mapping):
                for node_id, values in identities.items():
                    if isinstance(values, list):
                        artifact_ids.setdefault(str(node_id), set()).update(
                            str(value) for value in values
                        )
        per_node: list[dict[str, object]] = []
        for node in _fleet_nodes(fleet):
            node_id = str(node.get("id"))
            inventory = node.get("inventory")
            total = (
                inventory.get("disk_total_bytes")
                if isinstance(inventory, Mapping)
                else None
            )
            free = _node_available_disk(node)
            per_node.append(
                {
                    "node_id": node_id,
                    "disk_total_bytes": total,
                    "disk_free_bytes": free,
                    "campaign_assigned_bytes": assigned_bytes.get(node_id, 0),
                    "retained_artifact_identity_count": len(
                        artifact_ids.get(node_id, set())
                    ),
                    "retained_artifact_identities": sorted(
                        artifact_ids.get(node_id, set())
                    ),
                }
            )
        capacity_blocked = any(
            record.get("plan_digest") == digest
            and record.get("event") == "capacity.blocked"
            for record in self.ledger.records
        )
        return {
            "recipes": rows,
            "installation_inventory_complete": global_inventory["complete"],
            "installations": global_inventory["installations"],
            "installation_inventory_errors": global_inventory["errors"],
            "retained_installation_count": len(retained_installations),
            "fleet_snapshot_sha256": _digest(_fleet_fingerprint(fleet)),
            "online_node_ids": [node.get("id") for node in _fleet_nodes(fleet)],
            "per_node_storage": per_node,
            "all_feasible_installations_fit": not capacity_blocked,
            "automatic_eviction": False,
        }

    def _prove_storage_capacity(
        self,
        digest: str,
        key: str,
        item: Mapping[str, object],
        node_ids: list[object],
    ) -> None:
        assignment = self._capacity_assignments.get(key)
        if assignment is not None:
            self.ledger.append(
                "capacity.previewed",
                plan_digest=digest,
                recipe=key,
                payload={
                    "plan": {
                        "source": "capacity.plan.created",
                        "node_ids": assignment.get("node_ids"),
                        "persistent_bytes_by_node": assignment.get(
                            "persistent_bytes_by_node"
                        ),
                        "peak_bytes_by_node": assignment.get("peak_bytes_by_node"),
                        "automatic_eviction": False,
                        "disposition": "fits-at-runtime-check",
                    }
                },
            )
            return
        installed = item.get("maximum_installed_bytes_per_node")
        download = item.get("expected_download_bytes")
        temporary = item.get("temporary_build_bytes_per_node", 0)
        if (
            not isinstance(installed, int)
            or isinstance(installed, bool)
            or installed < 1
        ):
            raise QualificationError(
                f"{key} lacks maximum installed storage requirements"
            )
        if not isinstance(download, int) or isinstance(download, bool) or download < 0:
            raise QualificationError(f"{key} lacks artifact download requirements")
        if (
            not isinstance(temporary, int)
            or isinstance(temporary, bool)
            or temporary < 0
        ):
            raise QualificationError(f"{key} temporary build requirement is invalid")
        required = max(installed, download) + temporary
        fleet = self.client.request("GET", "/api/v1/fleet")
        nodes = {
            node.get("id"): node
            for node in (
                _object(raw, "fleet node")
                for raw in _list(fleet.get("nodes"), "fleet nodes")
            )
        }
        capacity_rows: list[dict[str, object]] = []
        insufficient = False
        for node_id in node_ids:
            node = nodes.get(node_id)
            available = _node_available_disk(node) if node is not None else None
            fits = isinstance(available, int) and available >= required
            insufficient = insufficient or not fits
            capacity_rows.append(
                {
                    "node_id": node_id,
                    "available_bytes": available,
                    "required_bytes": required,
                    "fits": fits,
                }
            )
        capacity_plan = {
            "artifact_download_bytes": download,
            "maximum_installed_bytes_per_node": installed,
            "temporary_build_bytes_per_node": temporary,
            "required_bytes_per_node": required,
            "nodes": capacity_rows,
            "automatic_eviction": False,
            "eviction_order": [],
            "disposition": "blocked-before-eviction" if insufficient else "fits",
        }
        self.ledger.append(
            "capacity.previewed",
            plan_digest=digest,
            recipe=key,
            payload={"plan": capacity_plan},
        )
        if insufficient:
            self.ledger.append(
                "recipe.blocked",
                plan_digest=digest,
                recipe=key,
                payload={
                    "blockers": [
                        {
                            "classification": "resource",
                            "code": "resource.storage_capacity_insufficient",
                            "detail": "Controller-reported free storage cannot prove this retained installation fits; no eviction was attempted.",
                            "capacity_plan": capacity_plan,
                        }
                    ]
                },
            )
            raise QualificationError(f"{key} retained installation does not fit")

    def _apply_recipe(self, digest: str, item: Mapping[str, object]) -> None:
        key = str(item["key"])
        prepared = self._prepared.get(key)
        if prepared is None:
            recipe_id, revision_id = self._ensure_import(digest, item)
        else:
            recipe_id, revision_id, _ = prepared
        detail = self.client.request(
            "GET", f"/api/v1/library/recipes/{_quote(recipe_id)}"
        )
        selected_revision = detail.get("selected_revision")
        if (
            not isinstance(selected_revision, Mapping)
            or selected_revision.get("id") != revision_id
            or selected_revision.get("content_sha256") != item.get("content_sha256")
        ):
            raise QualificationError(
                f"{key} selected revision changed after campaign planning"
            )
        restrictions = legal_blockers(detail, self.options.jurisdiction)
        if restrictions:
            self.ledger.append(
                "recipe.blocked",
                plan_digest=digest,
                recipe=key,
                payload={"blockers": [item.as_dict() for item in restrictions]},
            )
            raise QualificationError(f"{key} is denied by model license policy")
        selected = self._select_placement(digest, key, detail, item)
        node_ids = selected.get("node_ids")
        if not isinstance(node_ids, list) or len(node_ids) != item.get("node_count"):
            raise QualificationError(f"{key} placement node identities are invalid")
        if not all(self._node_allowed(node_id) for node_id in node_ids):
            raise QualificationError(
                f"{key} placement escaped the campaign node allowlist"
            )

        mapping_id = selected.get("mapping_id")
        if not isinstance(mapping_id, str):
            mapping_preview = self.client.request(
                "POST",
                "/api/v1/recipes/mapping-plans/preview",
                {
                    "recipe_revision_id": revision_id,
                    "node_ids": node_ids,
                    "parameters": {},
                },
            )
            self._record_preview(digest, key, "mapping", mapping_preview)
            mapping = self.client.request(
                "POST",
                "/api/v1/recipes/mappings",
                {
                    "recipe_revision_id": revision_id,
                    "node_ids": node_ids,
                    "parameters": {},
                    "placement_digest": mapping_preview["placement_digest"],
                    "request_key": _request_key(digest, key, "mapping"),
                },
            )
            mapping_id = mapping.get("mapping_id")
            if not isinstance(mapping_id, str):
                raise QualificationError(f"{key} mapping ID is invalid")
            self.ledger.append(
                "step.completed",
                plan_digest=digest,
                recipe=key,
                payload={"step": "mapping", "result": mapping},
            )

        build_id = selected.get("recipe_build_id")
        if not isinstance(build_id, str):
            source = self.client.request(
                "POST",
                "/api/v1/recipes/source-checks",
                {"recipe_revision_id": revision_id},
            )
            if source.get("passed") is not True:
                raise QualificationError(f"{key} build source policy failed")
            build_preview = self.client.request(
                "POST",
                "/api/v1/recipes/build-plans/preview",
                {"recipe_revision_id": revision_id, "builder_node_id": node_ids[0]},
            )
            self._record_preview(digest, key, "build", build_preview)
            operation = self._operation(
                digest,
                key,
                "build",
                "/api/v1/recipes/builds",
                {
                    "recipe_revision_id": revision_id,
                    "builder_node_id": node_ids[0],
                    "build_input_sha256": build_preview["build_input_sha256"],
                    "request_key": _request_key(digest, key, "build"),
                },
            )
            build_id = operation.get("owner_id")
            if not isinstance(build_id, str):
                raise QualificationError(f"{key} build identity is invalid")

        mapping_generation: object = None
        operational = detail.get("operational_state")
        mappings = (
            operational.get("mappings") if isinstance(operational, Mapping) else None
        )
        if isinstance(mappings, list):
            for mapping in mappings:
                if (
                    isinstance(mapping, Mapping)
                    and mapping.get("mapping_id") == mapping_id
                ):
                    mapping_generation = mapping.get("generation")
                    break
        if not isinstance(mapping_generation, int):
            previews = _payloads(
                self.ledger.recipe_records(digest, key), "step.previewed"
            )
            for payload in reversed(previews):
                preview = payload.get("preview")
                if payload.get("step") == "mapping" and isinstance(preview, Mapping):
                    mapping_generation = preview.get("generation")
                    break
        if not isinstance(mapping_generation, int) or mapping_generation < 1:
            raise QualificationError(f"{key} mapping generation is unavailable")
        distribution_preview = self.client.request(
            "POST",
            "/api/v1/recipes/image-distribution-plans/preview",
            {
                "recipe_build_id": build_id,
                "mapping_id": mapping_id,
                "mapping_generation": mapping_generation,
            },
        )
        self._record_preview(digest, key, "image-distribution", distribution_preview)
        self._operation(
            digest,
            key,
            "image-distribution",
            "/api/v1/recipes/image-distributions",
            {
                "recipe_build_id": build_id,
                "mapping_id": mapping_id,
                "mapping_generation": mapping_generation,
                "plan_digest": distribution_preview["plan_digest"],
                "request_key": _request_key(digest, key, "image-distribution"),
            },
        )

        completed_install = self._completed_operation(digest, key, "install")
        if completed_install is not None:
            owned_installation = True
            installation_id = completed_install.get("owner_id")
            if not isinstance(installation_id, str):
                raise QualificationError(f"{key} completed installation is invalid")
        else:
            installation_ids = selected.get("installation_ids")
            installation_id = (
                installation_ids[0]
                if isinstance(installation_ids, list)
                and installation_ids
                and isinstance(installation_ids[0], str)
                else None
            )
            owned_installation = installation_id is None
        if installation_id is None:
            self._prove_storage_capacity(digest, key, item, node_ids)
            install_preview = self.client.request(
                "POST",
                "/api/v1/recipes/install-plans/preview",
                {"mapping_id": mapping_id, "recipe_build_id": build_id},
            )
            self._record_preview(digest, key, "install", install_preview)
            if install_preview.get("allowed") is not True:
                self.ledger.append(
                    "recipe.blocked",
                    plan_digest=digest,
                    recipe=key,
                    payload={
                        "blockers": [
                            {
                                "classification": "resource",
                                "code": "install.preview_blocked",
                                "detail": "The fresh controller install preview denied this exact placement.",
                                "preview": install_preview,
                            }
                        ]
                    },
                )
                raise QualificationError(f"{key} install preview is blocked")
            operation = self._operation(
                digest,
                key,
                "install",
                "/api/v1/recipes/installations",
                {
                    "mapping_id": mapping_id,
                    "recipe_build_id": build_id,
                    "plan_digest": install_preview["plan_digest"],
                    "request_key": _request_key(digest, key, "install"),
                },
            )
            installation_id = operation.get("owner_id")
            if not isinstance(installation_id, str):
                raise QualificationError(f"{key} installation identity is invalid")

        visual = detail.get("visual_recipe")
        interfaces = visual.get("interfaces") if isinstance(visual, Mapping) else []
        interface_rows = interfaces if isinstance(interfaces, list) else []
        is_job = any(
            isinstance(interface, Mapping) and interface.get("adapter") in _JOB_ADAPTERS
            for interface in interface_rows
        )
        alias = "qual-" + hashlib.sha256(key.encode()).hexdigest()[:16]
        run_step = "activate-job-run" if is_job else "run"
        operation = self._completed_operation(digest, key, run_step)
        if operation is None:
            run_preview = self.client.request(
                "POST",
                "/api/v1/recipes/run-plans/preview",
                {"installation_id": installation_id, "alias": alias},
            )
            self._record_preview(digest, key, "run", run_preview)
            if run_preview.get("allowed") is not True:
                self.ledger.append(
                    "recipe.blocked",
                    plan_digest=digest,
                    recipe=key,
                    payload={
                        "blockers": [
                            {
                                "classification": "resource",
                                "code": "run.preview_blocked",
                                "detail": "The fresh controller run preview denied activation.",
                                "preview": run_preview,
                            }
                        ]
                    },
                )
                raise QualificationError(f"{key} run preview is blocked")
            operation = self._operation(
                digest,
                key,
                run_step,
                "/api/v1/recipes/job-runs" if is_job else "/api/v1/recipes/runs",
                {
                    "installation_id": installation_id,
                    "alias": alias,
                    "plan_digest": run_preview["plan_digest"],
                    "request_key": _request_key(digest, key, run_step),
                },
            )
        run_id = operation.get("owner_id")
        if not isinstance(run_id, str):
            raise QualificationError(f"{key} run identity is invalid")

        smoke_error: Exception | None = None
        completed_smoke = _latest_step(
            self.ledger.recipe_records(digest, key), "step.completed", "smoke"
        )
        if completed_smoke is None:
            try:
                smoke = (
                    self.artifact_smoke.preview(
                        detail,
                        recipe_key=key,
                        recipe_content_sha256=str(item.get("content_sha256")),
                    )
                    if is_job
                    else self.service_smoke.preview(
                        detail,
                        alias,
                        recipe_key=key,
                        recipe_content_sha256=str(item.get("content_sha256")),
                    )
                )
                self._record_preview(digest, key, "smoke", smoke)
                if is_job:
                    if smoke.get("available") is not True:
                        blocker = smoke.get("blocker")
                        self.ledger.append(
                            "recipe.blocked",
                            plan_digest=digest,
                            recipe=key,
                            payload={
                                "blockers": [blocker]
                                if isinstance(blocker, Mapping)
                                else []
                            },
                        )
                        smoke_error = QualificationError(
                            f"{key} artifact-job smoke is not available"
                        )
                    else:
                        smoke_result = self.artifact_smoke.run(
                            self.client,
                            run_id,
                            smoke,
                            ledger=self.ledger,
                            plan_digest=digest,
                            recipe_key=key,
                            timeout_seconds=self.options.operation_timeout_seconds,
                            poll_interval_seconds=self.options.poll_interval_seconds,
                        )
                else:
                    if smoke.get("available") is not True:
                        blocker = smoke.get("blocker")
                        self.ledger.append(
                            "recipe.blocked",
                            plan_digest=digest,
                            recipe=key,
                            payload={
                                "blockers": [blocker]
                                if isinstance(blocker, Mapping)
                                else []
                            },
                        )
                        smoke_error = QualificationError(
                            f"{key} service smoke is not available"
                        )
                    else:
                        smoke_result = self.service_smoke.run(self.client, alias, smoke)
                if smoke_error is None:
                    self.ledger.append(
                        "step.completed",
                        plan_digest=digest,
                        recipe=key,
                        payload={"step": "smoke", "result": dict(smoke_result)},
                    )
            except Exception as error:  # noqa: BLE001 - release runtime after any smoke fault
                smoke_error = error

        stop_error: Exception | None = None
        if (
            self.options.cleanup in {"stop", "uninstall"}
            and self._completed_operation(digest, key, "stop") is None
        ):
            try:
                stop_preview = self.client.request(
                    "POST", "/api/v1/recipes/stop-plans/preview", {"run_id": run_id}
                )
                self._record_preview(digest, key, "stop", stop_preview)
                self._operation(
                    digest,
                    key,
                    "stop",
                    f"/api/v1/recipes/runs/{_quote(run_id)}/stop",
                    {
                        "plan_digest": stop_preview["plan_digest"],
                        "request_key": _request_key(digest, key, "stop"),
                    },
                )
                self.ledger.append(
                    "cleanup.released",
                    plan_digest=digest,
                    recipe=key,
                    payload={"step": "stop", "run_id": run_id},
                )
            except Exception as error:  # noqa: BLE001 - preserve primary smoke failure
                stop_error = error
                self.ledger.append(
                    "cleanup.release-failed",
                    plan_digest=digest,
                    recipe=key,
                    payload={
                        "step": "stop",
                        "run_id": run_id,
                        "error": str(error)[:512],
                        "original_error": str(smoke_error)[:512]
                        if smoke_error is not None
                        else None,
                    },
                )
        if smoke_error is not None:
            raise smoke_error
        if stop_error is not None:
            raise stop_error
        if self.options.cleanup in {"stop", "uninstall"}:
            redeploy_id: str | None = None
            redeploy_error: Exception | None = None
            cleanup_error: Exception | None = None
            try:
                redeploy = self._completed_operation(digest, key, "warm-redeploy")
                if redeploy is None:
                    redeploy_preview = self.client.request(
                        "POST",
                        "/api/v1/recipes/run-plans/preview",
                        {"installation_id": installation_id, "alias": alias},
                    )
                    self._record_preview(digest, key, "warm-redeploy", redeploy_preview)
                    if redeploy_preview.get("allowed") is not True:
                        raise QualificationError(
                            f"{key} warm-cache redeploy is blocked"
                        )
                    redeploy = self._operation(
                        digest,
                        key,
                        "warm-redeploy",
                        "/api/v1/recipes/job-runs"
                        if is_job
                        else "/api/v1/recipes/runs",
                        {
                            "installation_id": installation_id,
                            "alias": alias,
                            "plan_digest": redeploy_preview["plan_digest"],
                            "request_key": _request_key(digest, key, "warm-redeploy"),
                        },
                    )
                owner_id = redeploy.get("owner_id")
                if not isinstance(owner_id, str):
                    raise QualificationError(
                        f"{key} warm-cache run identity is invalid"
                    )
                redeploy_id = owner_id
                completed_redeploy_smoke = _latest_step(
                    self.ledger.recipe_records(digest, key),
                    "step.completed",
                    "warm-redeploy-smoke",
                )
                if completed_redeploy_smoke is None:
                    redeploy_smoke = (
                        self.artifact_smoke.preview(
                            detail,
                            recipe_key=key,
                            recipe_content_sha256=str(item.get("content_sha256")),
                        )
                        if is_job
                        else self.service_smoke.preview(
                            detail,
                            alias,
                            recipe_key=key,
                            recipe_content_sha256=str(item.get("content_sha256")),
                        )
                    )
                    self._record_preview(
                        digest, key, "warm-redeploy-smoke", redeploy_smoke
                    )
                    if redeploy_smoke.get("available") is not True:
                        raise QualificationError(
                            f"{key} warm-cache smoke contract is unavailable"
                        )
                    redeploy_result = (
                        self.artifact_smoke.run(
                            self.client,
                            redeploy_id,
                            redeploy_smoke,
                            ledger=self.ledger,
                            plan_digest=digest,
                            recipe_key=key,
                            timeout_seconds=self.options.operation_timeout_seconds,
                            poll_interval_seconds=self.options.poll_interval_seconds,
                            event_prefix="artifact-job.redeploy",
                        )
                        if is_job
                        else self.service_smoke.run(self.client, alias, redeploy_smoke)
                    )
                    self.ledger.append(
                        "step.completed",
                        plan_digest=digest,
                        recipe=key,
                        payload={
                            "step": "warm-redeploy-smoke",
                            "result": dict(redeploy_result),
                        },
                    )
            except Exception as error:  # noqa: BLE001 - always execute warm release
                redeploy_error = error
            finally:
                if redeploy_id is not None:
                    try:
                        if (
                            self._completed_operation(digest, key, "warm-redeploy-stop")
                            is None
                        ):
                            redeploy_stop_preview = self.client.request(
                                "POST",
                                "/api/v1/recipes/stop-plans/preview",
                                {"run_id": redeploy_id},
                            )
                            self._record_preview(
                                digest,
                                key,
                                "warm-redeploy-stop",
                                redeploy_stop_preview,
                            )
                            self._operation(
                                digest,
                                key,
                                "warm-redeploy-stop",
                                f"/api/v1/recipes/runs/{_quote(redeploy_id)}/stop",
                                {
                                    "plan_digest": redeploy_stop_preview["plan_digest"],
                                    "request_key": _request_key(
                                        digest, key, "warm-redeploy-stop"
                                    ),
                                },
                            )
                        self.ledger.append(
                            "cleanup.released",
                            plan_digest=digest,
                            recipe=key,
                            payload={
                                "step": "warm-redeploy-stop",
                                "run_id": redeploy_id,
                            },
                        )
                    except Exception as error:  # noqa: BLE001 - ledger cleanup failure
                        cleanup_error = error
                        self.ledger.append(
                            "cleanup.release-failed",
                            plan_digest=digest,
                            recipe=key,
                            payload={
                                "step": "warm-redeploy-stop",
                                "run_id": redeploy_id,
                                "error": str(error)[:512],
                                "original_error": str(redeploy_error)[:512]
                                if redeploy_error is not None
                                else None,
                            },
                        )
            if redeploy_error is not None:
                raise redeploy_error
            if cleanup_error is not None:
                raise cleanup_error
        if self.options.cleanup == "uninstall":
            if not owned_installation:
                self.ledger.append(
                    "cleanup.skipped",
                    plan_digest=digest,
                    recipe=key,
                    payload={
                        "step": "uninstall",
                        "installation_id": installation_id,
                        "reason": "preexisting installation is not runner-owned",
                    },
                )
            elif self._completed_operation(digest, key, "uninstall") is None:
                uninstall_preview = self.client.request(
                    "POST",
                    "/api/v1/recipes/uninstall-plans/preview",
                    {"installation_id": installation_id},
                )
                self._record_preview(digest, key, "uninstall", uninstall_preview)
                self._operation(
                    digest,
                    key,
                    "uninstall",
                    f"/api/v1/recipes/installations/{_quote(installation_id)}/uninstall",
                    {
                        "plan_digest": uninstall_preview["plan_digest"],
                        "request_key": _request_key(digest, key, "uninstall"),
                    },
                )
        elif self.options.cleanup == "stop":
            self.ledger.append(
                "cleanup.retained",
                plan_digest=digest,
                recipe=key,
                payload={
                    "installation_id": installation_id,
                    "owned_by_runner": owned_installation,
                    "state": "installed-runtime-stopped",
                    "reason": "default cache-retention policy",
                },
            )
        if smoke_error is not None:
            raise smoke_error
        self.ledger.append(
            "recipe.succeeded",
            plan_digest=digest,
            recipe=key,
            payload={
                "recipe_id": recipe_id,
                "recipe_revision_id": revision_id,
                "recipe_content_sha256": item.get("content_sha256"),
                "mapping_id": mapping_id,
                "recipe_build_id": build_id,
                "installation_id": installation_id,
                "run_id": run_id,
                "node_ids": node_ids,
            },
        )
