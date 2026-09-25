"""Paired-batch physical qualification of exact recipe releases.

Every workload change is an ordinary whole-Fleet profile preview/load. Paired
single-Spark lanes share one apply and smoke concurrently; disruptive recovery
stays exclusive and is recorded as durable lane evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import runpy
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from collections.abc import Set as AbstractSet
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files as package_files
from pathlib import Path, PurePosixPath
from typing import Any, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .control_client import (
    ControlClient,
    ControlClientError,
    ControlNotFound,
    ControlTransportError,
    ControlUnavailable,
)
from .fleet_qualification import (
    ArtifactJobSmokeAdapter,
    EvidenceLedger,
    QualificationError,
    ServiceSmokeAdapter,
)
from .fleet_qualification_dual_recovery import (
    DualRecoveryProgress,
    DualRecoveryTarget,
    FailedDualCanaryTarget,
    apply_dual_cleanup,
    apply_failed_dual_cleanup,
    observe_dual_batch,
    review_dual_cleanup,
    review_failed_dual_cleanup,
)
from .fleet_qualification_recovery import (
    CanaryReference,
    LaneRecoveryProgress,
    LaneRecoveryTarget,
    observe_single_lane,
    prior_partner_release_proofs,
    record_lane_cleanup,
    recover_single_lane,
    review_lane_cleanup,
    review_lane_transition,
)
from .generated_control.models.fleet_node_detail_response import FleetNodeDetailResponse
from .generated_control.models.fleet_profile_application_view import (
    FleetProfileApplicationView,
)
from .generated_control.models.fleet_profile_preview import FleetProfilePreview
from .generated_control.models.fleet_profile_view import FleetProfileView
from .generated_control.models.fleet_snapshot import FleetSnapshot
from .generated_control.models.run_switch_final_verify_result import (
    RunSwitchFinalVerifyResult,
)
from .generated_control.types import Unset
from .qualification_fixtures import FixtureError, FixtureRegistry
from .qualification_locking import ledger_lock, node_locks

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
_RECIPE_KEY = re.compile(r"[a-z0-9][a-z0-9-]{1,62}/[a-z0-9][a-z0-9-]{1,62}\Z")
_NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")
_OCI_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ALIAS = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_TERMINAL_APPLICATIONS = frozenset(
    {"succeeded", "failed", "cancelled", "waiting-for-operator"}
)
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_CATALOG_FILE_BYTES = 64 * 1024 * 1024
_MAX_FIXTURE_MANIFEST_BYTES = 2 * 1024 * 1024
_CANONICAL_GIT_READ_TIMEOUT_SECONDS = 30
_PROFILE_AUTHORITY_LABEL = "qualification-authority"
_PROFILE_LEDGER_LABEL = "qualification-ledger"


@dataclass(frozen=True, slots=True)
class RecipeAuthorityRow:
    sequence: int
    key: str
    content_sha256: str
    node_count: int
    interface: str
    recipe_version: str
    package: Mapping[str, object]
    disposition: str
    review_gates: tuple[Mapping[str, object], ...]
    operator_acceptance_required: bool
    model_license_refs: tuple[Mapping[str, object], ...]
    qualification_inputs: tuple[str, ...]
    smoke_cases: tuple[str, ...]
    runtime_stack_sha256: str
    topology_sha256: str
    recovery_coverage_refs: tuple[Mapping[str, object], ...]
    raw: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class BatchAssignment:
    recipe: str
    lane: int
    node_count: int


@dataclass(frozen=True, slots=True)
class CampaignBatch:
    sequence: int
    batch_id: str
    mode: str
    assignments: tuple[BatchAssignment, ...]
    raw: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class BatchLane:
    assignment: BatchAssignment
    row: RecipeAuthorityRow
    node_ids: tuple[str, ...]
    alias: str
    smoke_kind: str
    detail: Mapping[str, object]
    smoke_preview: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class CampaignAuthority:
    authority_id: str
    sha256: str
    catalog: Mapping[str, object]
    max_node_count: int
    excluded_topology_recipe_keys: tuple[str, ...]
    rows: tuple[RecipeAuthorityRow, ...]
    batches: tuple[CampaignBatch, ...]
    recovery_coverage: tuple[Mapping[str, object], ...]
    raw: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class CampaignManifest:
    path: Path
    sha256: str
    authority: CampaignAuthority
    fixture_manifest: Path
    cleanup: str
    operation_timeout_seconds: float
    poll_interval_seconds: float


@lru_cache(maxsize=2)
def _campaign_contract_validator(name: str) -> Draft202012Validator:
    if name not in {
        "qualification-authority-v4.schema.json",
        "qualification-campaign-manifest-v2.schema.json",
        "recovery-coverage-receipt-v1.schema.json",
    }:
        raise QualificationError("unknown qualification contract schema")
    try:
        raw = package_files("cluster_profiles").joinpath("schemas", name).read_bytes()
        schema = json.loads(raw)
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema)
    except (OSError, json.JSONDecodeError, SchemaError) as error:
        raise QualificationError(
            f"canonical qualification contract schema is unavailable: {name}"
        ) from error


def _validate_campaign_contract(value: object, schema_name: str, label: str) -> None:
    try:
        error = next(
            _campaign_contract_validator(schema_name).iter_errors(cast(Any, value)),
            None,
        )
    except (SchemaError, QualificationError) as error:
        if isinstance(error, QualificationError):
            raise
        raise QualificationError(
            f"canonical qualification contract schema is invalid: {schema_name}"
        ) from error
    if error is not None:
        location = "/".join(str(part) for part in error.absolute_path)
        suffix = f" at /{location}" if location else ""
        raise QualificationError(
            f"{label} does not match the canonical contract{suffix}: {error.message}"
        ) from error


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _contract_digest(value: object) -> str:
    """Hash the JSON representation used by canonical recipe contract helpers."""
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise QualificationError(f"qualification JSON repeats key {key}")
        result[key] = value
    return result


def _strict_read(
    path: Path, label: str, *, maximum_bytes: int = _MAX_MANIFEST_BYTES
) -> tuple[object, bytes]:
    try:
        size = path.stat().st_size
        if size > maximum_bytes:
            raise QualificationError(
                f"{label} exceeds its {maximum_bytes}-byte read bound"
            )
        raw = path.read_bytes()
    except OSError as error:
        raise QualificationError(f"{label} cannot be read: {path}") from error
    if len(raw) > maximum_bytes:
        raise QualificationError(f"{label} exceeds its {maximum_bytes}-byte read bound")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {item}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise QualificationError(f"{label} is invalid JSON: {path}") from error
    return value, raw


def _object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise QualificationError(f"{label} must be an object")
    return value


def _object_list(value: object, label: str) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        raise QualificationError(f"{label} must be a list")
    return [_object(item, label) for item in value]


def _exact_keys(
    value: Mapping[str, object],
    *,
    required: set[str],
    optional: AbstractSet[str] = frozenset(),
    label: str,
) -> None:
    missing = sorted(required - set(value))
    if missing:
        raise QualificationError(f"{label} lacks {missing[0]}")
    unknown = sorted(set(value) - required - optional)
    if unknown:
        raise QualificationError(f"{label} has unknown field {unknown[0]}")


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise QualificationError(f"{label} must be a non-empty string")
    return value


def _string_array(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise QualificationError(f"{label} must be an array of strings")
    result = tuple(value)
    if len(set(result)) != len(result):
        raise QualificationError(f"{label} contains duplicates")
    return result


def _integer(value: object, label: str, minimum: int, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise QualificationError(f"{label} must be between {minimum} and {maximum}")
    return value


def _relative_path(
    root: Path,
    value: object,
    label: str,
    *,
    allow_parent: bool = False,
    base: Path | None = None,
) -> Path:
    raw = Path(_string(value, label))
    if raw.is_absolute() or (not allow_parent and ".." in raw.parts) or not raw.parts:
        raise QualificationError(f"{label} must be a safe repository-relative path")
    repository_root = root.resolve(strict=True)
    current = (base or repository_root).resolve(strict=True)
    if not current.is_relative_to(repository_root):
        raise QualificationError(f"{label} base is outside the recipe repository")
    for part in raw.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            current = current.parent
            if not current.is_relative_to(repository_root):
                raise QualificationError(f"{label} escapes the recipe repository")
            continue
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError as error:
            raise QualificationError(f"{label} is unavailable") from error
        if stat.S_ISLNK(mode):
            raise QualificationError(f"{label} must not traverse a symbolic link")
    resolved = current.resolve(strict=True)
    if not resolved.is_relative_to(repository_root):
        raise QualificationError(f"{label} escapes the recipe repository")
    if not stat.S_ISREG(resolved.stat().st_mode):
        raise QualificationError(f"{label} must name a regular file")
    return resolved


def _load_authority(path: Path) -> CampaignAuthority:
    value, raw = _strict_read(path, "qualification authority")
    _validate_campaign_contract(
        value, "qualification-authority-v4.schema.json", "qualification authority"
    )
    root = _object(value, "qualification authority")
    _exact_keys(
        root,
        required={
            "schema_version",
            "authority_id",
            "catalog",
            "scope",
            "recipes",
            "batches",
            "recovery_coverage",
        },
        label="qualification authority",
    )
    if type(root["schema_version"]) is not int or root["schema_version"] != 4:
        raise QualificationError("qualification authority schema_version must be 4")
    authority_id = _string(root["authority_id"], "authority_id")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", authority_id) is None:
        raise QualificationError("qualification authority_id is invalid")

    catalog = _object(root["catalog"], "authority catalog")
    _exact_keys(
        catalog,
        required={
            "repository",
            "commit",
            "release_tag",
            "source_commit",
            "catalog_index_sha256",
            "qualification_index_sha256",
            "recipe_count",
        },
        label="authority catalog",
    )
    _string(catalog["repository"], "authority catalog repository")
    _string(catalog["release_tag"], "authority release tag")
    for field in ("commit", "source_commit"):
        if _GIT_SHA.fullmatch(_string(catalog[field], f"catalog {field}")) is None:
            raise QualificationError(f"authority catalog {field} is invalid")
    for field in ("catalog_index_sha256", "qualification_index_sha256"):
        if _SHA256.fullmatch(_string(catalog[field], f"catalog {field}")) is None:
            raise QualificationError(f"authority catalog {field} is invalid")
    catalog_count = _integer(
        catalog["recipe_count"], "catalog recipe_count", 1, 2**63 - 1
    )

    scope = _object(root["scope"], "authority scope")
    _exact_keys(
        scope,
        required={
            "maximum_node_count",
            "recipe_count",
            "excluded_topology_recipe_keys",
        },
        label="authority scope",
    )
    maximum = _integer(scope["maximum_node_count"], "maximum_node_count", 1, 2)
    recipe_count = _integer(scope["recipe_count"], "scope recipe_count", 1, 2**63 - 1)
    excluded = _string_array(
        scope["excluded_topology_recipe_keys"], "excluded topology recipe keys"
    )
    if any(_RECIPE_KEY.fullmatch(key) is None for key in excluded):
        raise QualificationError("excluded topology recipe key is invalid")

    raw_rows = root["recipes"]
    if not isinstance(raw_rows, list) or len(raw_rows) != recipe_count:
        raise QualificationError("authority recipe rows do not match scope count")
    rows: list[RecipeAuthorityRow] = []
    seen: set[str] = set()
    for position, raw_row in enumerate(raw_rows, 1):
        row = _object(raw_row, f"authority recipes[{position - 1}]")
        sequence = _integer(row["sequence"], f"recipe {position} sequence", 1, position)
        key = _string(row["key"], f"recipe {position} key")
        if sequence != position or _RECIPE_KEY.fullmatch(key) is None or key in seen:
            raise QualificationError(
                f"authority recipe order/identity is invalid: {key}"
            )
        seen.add(key)
        digest = _string(row["content_sha256"], f"{key} recipe digest")
        if _SHA256.fullmatch(digest) is None:
            raise QualificationError(f"{key} recipe digest is invalid")
        node_count = _integer(row["node_count"], f"{key} node_count", 1, maximum)
        interface = _string(row["interface"], f"{key} interface")
        recipe_version = _string(row["recipe_version"], f"{key} recipe version")
        package = _object(row["package"], f"{key} package")
        _exact_keys(
            package,
            required={"path", "sha256", "expected_bytes", "media_type"},
            label=f"{key} package",
        )
        _string(package["path"], f"{key} package path")
        if (
            _SHA256.fullmatch(_string(package["sha256"], f"{key} package sha256"))
            is None
        ):
            raise QualificationError(f"{key} package sha256 is invalid")
        _integer(package["expected_bytes"], f"{key} package bytes", 1, 2**63 - 1)
        _string(package["media_type"], f"{key} package media type")
        disposition = _string(row["disposition"], f"{key} disposition")
        if disposition not in {
            "actionable",
            "capacity-review",
            "operator-acceptance-required",
        }:
            raise QualificationError(f"{key} disposition is invalid")
        if "disposition_reason" in row:
            _string(row["disposition_reason"], f"{key} disposition reason")

        raw_gates = row["review_gates"]
        if not isinstance(raw_gates, list):
            raise QualificationError(f"{key} review_gates must be an array")
        gates: list[Mapping[str, object]] = []
        gate_kinds: set[str] = set()
        for raw_gate in raw_gates:
            gate = _object(raw_gate, f"{key} review gate")
            _exact_keys(gate, required={"kind", "reason"}, label=f"{key} review gate")
            kind = _string(gate["kind"], f"{key} review gate kind")
            if kind not in {"capacity-review", "operator-acceptance-required"}:
                raise QualificationError(f"{key} review gate kind is invalid")
            _string(gate["reason"], f"{key} review gate reason")
            if kind in gate_kinds:
                raise QualificationError(f"{key} repeats a review gate")
            gate_kinds.add(kind)
            gates.append(dict(gate))

        raw_models = row["model_license_refs"]
        if not isinstance(raw_models, list) or not raw_models:
            raise QualificationError(
                f"{key} must bind at least one exact Model license"
            )
        model_refs: list[Mapping[str, object]] = []
        model_identities: set[tuple[str, str]] = set()
        model_acceptance = False
        for raw_model in raw_models:
            model = _object(raw_model, f"{key} Model license reference")
            _exact_keys(
                model,
                required={
                    "key",
                    "content_sha256",
                    "spdx",
                    "url",
                    "operator_acceptance_required",
                    "attribution",
                },
                optional={"territorial_restrictions"},
                label=f"{key} Model license reference",
            )
            model_key = _string(model["key"], f"{key} Model selector")
            model_digest = _string(model["content_sha256"], f"{key} Model digest")
            if (
                _RECIPE_KEY.fullmatch(model_key) is None
                or _SHA256.fullmatch(model_digest) is None
            ):
                raise QualificationError(f"{key} Model identity is invalid")
            identity = (model_key, model_digest)
            if identity in model_identities:
                raise QualificationError(f"{key} repeats an exact Model reference")
            model_identities.add(identity)
            _string(model["spdx"], f"{key} Model SPDX")
            _string(model["url"], f"{key} Model license URL")
            attribution = _string_array(
                model["attribution"], f"{key} Model attribution"
            )
            del attribution
            accepted = model["operator_acceptance_required"]
            if not isinstance(accepted, bool):
                raise QualificationError(f"{key} Model acceptance flag is invalid")
            model_acceptance = model_acceptance or accepted
            if "territorial_restrictions" in model:
                _object(
                    model["territorial_restrictions"], f"{key} territorial restrictions"
                )
            model_refs.append(dict(model))
        acceptance = row["operator_acceptance_required"]
        if not isinstance(acceptance, bool) or acceptance != model_acceptance:
            raise QualificationError(f"{key} Model acceptance gate does not close")
        if acceptance != ("operator-acceptance-required" in gate_kinds):
            raise QualificationError(
                f"{key} operator review gate does not match its Models"
            )
        expected_disposition = (
            "operator-acceptance-required"
            if acceptance
            else "capacity-review"
            if "capacity-review" in gate_kinds
            else "actionable"
        )
        if disposition != expected_disposition:
            raise QualificationError(
                f"{key} disposition does not match its review gates"
            )
        if expected_disposition != "actionable" and "disposition_reason" not in row:
            raise QualificationError(f"{key} non-actionable disposition lacks a reason")

        qualification_inputs = _string_array(
            row["qualification_inputs"], f"{key} qualification inputs"
        )
        smoke_cases = _string_array(row["smoke_cases"], f"{key} smoke cases")
        if not smoke_cases:
            raise QualificationError(f"{key} lacks reviewed smoke cases")
        recovery_refs = row["recovery_coverage_refs"]
        if not isinstance(recovery_refs, list) or any(
            not isinstance(item, Mapping) for item in recovery_refs
        ):
            raise QualificationError(f"{key} recovery coverage references are invalid")
        runtime_stack_sha256 = _string(
            row["runtime_stack_sha256"], f"{key} runtime stack identity"
        )
        topology_sha256 = _string(row["topology_sha256"], f"{key} topology identity")
        if (
            _SHA256.fullmatch(runtime_stack_sha256) is None
            or _SHA256.fullmatch(topology_sha256) is None
        ):
            raise QualificationError(
                f"{key} runtime stack/topology identity is invalid"
            )
        rows.append(
            RecipeAuthorityRow(
                sequence,
                key,
                digest,
                node_count,
                interface,
                recipe_version,
                dict(package),
                disposition,
                tuple(gates),
                acceptance,
                tuple(model_refs),
                qualification_inputs,
                smoke_cases,
                runtime_stack_sha256,
                topology_sha256,
                tuple(dict(item) for item in recovery_refs),
                dict(row),
            )
        )
    if len(rows) != recipe_count or catalog_count - len(excluded) != recipe_count:
        raise QualificationError("authority scope does not close over its catalog")
    if seen & set(excluded):
        raise QualificationError("excluded topology recipes appear in the test scope")

    rows_by_key = {row.key: row for row in rows}
    raw_batches = root["batches"]
    if not isinstance(raw_batches, list) or not raw_batches:
        raise QualificationError("qualification authority must contain batches")
    batches: list[CampaignBatch] = []
    batch_ids: set[str] = set()
    assigned_recipes: list[str] = []
    for position, raw_batch in enumerate(raw_batches, 1):
        batch = _object(raw_batch, f"qualification batch {position}")
        sequence = _integer(batch.get("sequence"), "batch sequence", 1, position)
        batch_id = _string(batch.get("id"), "batch ID")
        mode = _string(batch.get("mode"), "batch mode")
        raw_assignments = batch.get("assignments")
        if (
            sequence != position
            or batch_id in batch_ids
            or not isinstance(raw_assignments, list)
            or not raw_assignments
        ):
            raise QualificationError(
                f"qualification batch order/identity is invalid: {batch_id}"
            )
        batch_ids.add(batch_id)
        assignments: list[BatchAssignment] = []
        for raw_assignment in raw_assignments:
            assignment = _object(raw_assignment, f"{batch_id} assignment")
            recipe = _string(assignment.get("recipe"), f"{batch_id} recipe")
            lane = _integer(assignment.get("lane"), f"{batch_id} lane", 1, 2)
            node_count = _integer(
                assignment.get("node_count"), f"{batch_id} node_count", 1, maximum
            )
            authority_row = rows_by_key.get(recipe)
            if authority_row is None or authority_row.node_count != node_count:
                raise QualificationError(
                    f"{batch_id} assignment differs from its reviewed recipe row: {recipe}"
                )
            assignments.append(BatchAssignment(recipe, lane, node_count))
            assigned_recipes.append(recipe)
        if [item.lane for item in assignments] != list(range(1, len(assignments) + 1)):
            raise QualificationError(f"{batch_id} lanes must be ordered and contiguous")
        if mode == "paired-single":
            if len(assignments) != 2 or any(
                item.node_count != 1 for item in assignments
            ):
                raise QualificationError(
                    f"{batch_id} paired-single mode requires two single-node assignments"
                )
        elif mode == "single":
            if len(assignments) != 1 or assignments[0].node_count != 1:
                raise QualificationError(
                    f"{batch_id} single mode requires one single-node assignment"
                )
        elif mode == "exclusive-dual":
            if len(assignments) != 1 or assignments[0].node_count != 2:
                raise QualificationError(
                    f"{batch_id} exclusive-dual mode requires one dual-node assignment"
                )
        else:
            raise QualificationError(f"{batch_id} batch mode is unsupported")
        batches.append(
            CampaignBatch(sequence, batch_id, mode, tuple(assignments), dict(batch))
        )
    if (
        len(assigned_recipes) != len(rows)
        or len(set(assigned_recipes)) != len(assigned_recipes)
        or set(assigned_recipes) != set(rows_by_key)
    ):
        raise QualificationError(
            "qualification batches must assign every in-scope recipe exactly once"
        )
    raw_coverage = root["recovery_coverage"]
    if (
        not isinstance(raw_coverage, list)
        or not raw_coverage
        or any(not isinstance(item, Mapping) for item in raw_coverage)
    ):
        raise QualificationError("qualification recovery coverage is invalid")
    invalidation_fields = {
        "recipe_content_sha256",
        "package_sha256",
        "model_content_sha256s",
        "runtime_stack_sha256",
        "topology_sha256",
        "coverage_membership",
        "runtime_image_digest",
        "platform_build_sha256",
        "agent_build_sha256",
        "target_node_ids",
        "smoke_receipt_sha256",
    }
    coverage_by_id: dict[str, Mapping[str, object]] = {}
    for item in raw_coverage:
        coverage = _object(item, "recovery coverage definition")
        _exact_keys(
            coverage,
            required={
                "coverage_id",
                "failure_mode",
                "representative_recipe",
                "members",
                "shared",
                "equivalence_rationale",
                "invalidated_by",
            },
            label="recovery coverage definition",
        )
        coverage_id = _string(coverage.get("coverage_id"), "coverage ID")
        if _SHA256.fullmatch(coverage_id) is None or coverage_id in coverage_by_id:
            raise QualificationError(
                "recovery coverage identity is invalid or duplicated"
            )
        unsigned_coverage = {
            key: value for key, value in coverage.items() if key != "coverage_id"
        }
        if _contract_digest(unsigned_coverage) != coverage_id:
            raise QualificationError(
                "recovery coverage ID does not bind its exact definition"
            )
        failure_mode = _string(coverage.get("failure_mode"), "coverage failure mode")
        if failure_mode not in {
            "single-host-restart",
            "dual-rank-loss-recovery",
            "dual-host-restart",
        }:
            raise QualificationError("recovery coverage failure mode is unsupported")
        representative = _string(
            coverage.get("representative_recipe"), "coverage representative"
        )
        shared = coverage.get("shared")
        if not isinstance(shared, bool):
            raise QualificationError("recovery coverage shared flag is invalid")
        rationale = _string(coverage.get("equivalence_rationale"), "coverage rationale")
        if not rationale.strip():
            raise QualificationError("recovery coverage rationale is empty")
        invalidated_by = coverage.get("invalidated_by")
        if (
            not isinstance(invalidated_by, list)
            or len(invalidated_by) != len(invalidation_fields)
            or set(invalidated_by) != invalidation_fields
        ):
            raise QualificationError(
                "recovery coverage must invalidate on every bound evidence identity"
            )
        raw_members = coverage.get("members")
        if not isinstance(raw_members, list) or not raw_members:
            raise QualificationError("recovery coverage members are invalid")
        member_keys: list[str] = []
        for raw_member in raw_members:
            member = _object(raw_member, "recovery coverage member")
            _exact_keys(
                member,
                required={
                    "recipe",
                    "recipe_content_sha256",
                    "package_sha256",
                    "model_content_sha256s",
                    "runtime_stack_sha256",
                    "topology_sha256",
                },
                label="recovery coverage member",
            )
            recipe = _string(member.get("recipe"), "coverage member recipe")
            authority_row = rows_by_key.get(recipe)
            member_models = _string_array(
                member.get("model_content_sha256s"), "coverage member Models"
            )
            model_digests = (
                sorted(
                    {
                        _string(model.get("content_sha256"), "Model content identity")
                        for model in authority_row.model_license_refs
                    }
                )
                if authority_row is not None
                else []
            )
            member_identity = (
                member.get("recipe_content_sha256"),
                member.get("package_sha256"),
                list(member_models),
                member.get("runtime_stack_sha256"),
                member.get("topology_sha256"),
            )
            expected_identity = (
                (
                    authority_row.content_sha256,
                    authority_row.package.get("sha256"),
                    model_digests,
                    authority_row.runtime_stack_sha256,
                    authority_row.topology_sha256,
                )
                if authority_row is not None
                else None
            )
            if (
                authority_row is None
                or _RECIPE_KEY.fullmatch(recipe) is None
                or member_models != tuple(sorted(set(member_models)))
                or any(_SHA256.fullmatch(value) is None for value in member_models)
                or member_identity != expected_identity
            ):
                raise QualificationError(
                    f"{recipe} recovery coverage binds another recipe identity"
                )
            member_keys.append(recipe)
        if member_keys != sorted(set(member_keys)):
            raise QualificationError(
                "recovery coverage members must be unique and sorted"
            )
        if (
            representative not in member_keys
            or shared is not (len(member_keys) > 1)
            or (failure_mode == "dual-rank-loss-recovery" and shared)
        ):
            raise QualificationError("recovery coverage scope is inconsistent")
        if (
            shared
            and len(
                {
                    (
                        _string(
                            _object(member, "recovery coverage member").get(
                                "runtime_stack_sha256"
                            ),
                            "member runtime stack digest",
                        ),
                        _string(
                            _object(member, "recovery coverage member").get(
                                "topology_sha256"
                            ),
                            "member topology digest",
                        ),
                    )
                    for member in raw_members
                }
            )
            != 1
        ):
            raise QualificationError(
                "shared recovery coverage requires identical runtime-stack and topology identities"
            )
        coverage_by_id[coverage_id] = coverage
    expected_modes = {
        1: {"single-host-restart"},
        2: {"dual-rank-loss-recovery", "dual-host-restart"},
    }
    seen_refs: set[tuple[str, str, str]] = set()
    for row in rows:
        references_by_mode: dict[str, Mapping[str, object]] = {}
        for reference in row.recovery_coverage_refs:
            _exact_keys(
                reference,
                required={
                    "coverage_id",
                    "failure_mode",
                    "representative_recipe",
                    "role",
                },
                label=f"{row.key} recovery coverage reference",
            )
            coverage_id = reference.get("coverage_id")
            failure_mode = reference.get("failure_mode")
            if (
                not isinstance(coverage_id, str)
                or coverage_id not in coverage_by_id
                or not isinstance(failure_mode, str)
                or failure_mode in references_by_mode
            ):
                raise QualificationError(
                    f"{row.key} references an unknown recovery coverage identity"
                )
            coverage = coverage_by_id[coverage_id]
            if coverage.get("failure_mode") != failure_mode:
                raise QualificationError(
                    f"{row.key} references a coverage definition for another failure mode"
                )
            representative = coverage.get("representative_recipe")
            if reference.get("representative_recipe") != representative:
                raise QualificationError(
                    f"{row.key} recovery coverage has another representative"
                )
            members = coverage.get("members")
            assert isinstance(members, list)
            member_keys = [
                _string(
                    _object(member, "recovery coverage member").get("recipe"),
                    "member recipe",
                )
                for member in members
            ]
            if row.key not in member_keys:
                raise QualificationError(
                    f"{row.key} recovery reference is outside its exact coverage scope"
                )
            role = (
                "representative"
                if coverage.get("shared") is True and row.key == representative
                else "shared-member"
                if coverage.get("shared") is True
                else "dedicated"
            )
            if reference.get("role") != role:
                raise QualificationError(
                    f"{row.key} recovery reference role is invalid"
                )
            references_by_mode[failure_mode] = reference
            seen_refs.add((row.key, failure_mode, coverage_id))
        if set(references_by_mode) != expected_modes[row.node_count]:
            raise QualificationError(
                f"{row.key} recovery references do not cover its typed failure modes"
            )
    expected_refs = {
        (row.key, str(reference.get("failure_mode")), str(reference.get("coverage_id")))
        for row in rows
        for reference in row.recovery_coverage_refs
    }
    if seen_refs != expected_refs:
        raise QualificationError(
            "recovery definitions and recipe references do not close exactly"
        )
    for coverage in coverage_by_id.values():
        failure_mode = str(coverage.get("failure_mode"))
        members = coverage.get("members")
        assert isinstance(members, list)
        for member in members:
            recipe = _string(
                _object(member, "recovery coverage member").get("recipe"),
                "member recipe",
            )
            coverage_id = _string(coverage.get("coverage_id"), "coverage ID")
            if (recipe, failure_mode, coverage_id) not in seen_refs:
                raise QualificationError(
                    "recovery definition includes a member with no exact row reference"
                )
    return CampaignAuthority(
        authority_id,
        hashlib.sha256(raw).hexdigest(),
        dict(catalog),
        maximum,
        excluded,
        tuple(rows),
        tuple(batches),
        tuple(dict(item) for item in raw_coverage),
        dict(root),
    )


def load_manifest(path: Path, library_root: Path) -> CampaignManifest:
    value, raw = _strict_read(path, "campaign manifest")
    _validate_campaign_contract(
        value,
        "qualification-campaign-manifest-v2.schema.json",
        "campaign manifest",
    )
    root = _object(value, "campaign manifest")
    library_root = library_root.resolve(strict=True)
    campaign_path = path.resolve(strict=True)
    if not campaign_path.is_relative_to(library_root):
        raise QualificationError(
            "campaign manifest must be inside the recipe repository"
        )
    campaign_path = _relative_path(
        library_root,
        str(campaign_path.relative_to(library_root)),
        "campaign manifest",
    )
    authority_path = _relative_path(
        library_root,
        root["qualification_authority"],
        "authority path",
        allow_parent=True,
        base=campaign_path.parent,
    )
    fixtures_path = _relative_path(
        library_root,
        root["fixture_manifest"],
        "fixture manifest path",
        allow_parent=True,
        base=campaign_path.parent,
    )
    if not authority_path.is_relative_to(
        library_root
    ) or not fixtures_path.is_relative_to(library_root):
        raise QualificationError(
            "campaign inputs must remain inside the recipe repository"
        )
    authority = _load_authority(authority_path)
    options = _object(root.get("options", {}), "campaign options")
    cleanup = options.get("cleanup", "stop")
    if cleanup != "stop":
        raise QualificationError(
            "sequential qualification requires cleanup=stop to retain exact cached assets"
        )
    return CampaignManifest(
        campaign_path,
        hashlib.sha256(raw).hexdigest(),
        authority,
        fixtures_path,
        "stop",
        float(
            _integer(
                options.get("operation_timeout_seconds", 86_400),
                "operation_timeout_seconds",
                1,
                86_400,
            )
        ),
        float(
            _integer(
                options.get("poll_interval_seconds", 5),
                "poll_interval_seconds",
                1,
                60,
            )
        ),
    )


def _safe_file_digest(path: Path, expected_bytes: int) -> str:
    digest = hashlib.sha256()
    observed = 0
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                observed += len(chunk)
                digest.update(chunk)
    except OSError as error:
        raise QualificationError(f"recipe package cannot be read: {path}") from error
    if observed != expected_bytes:
        raise QualificationError(
            f"recipe package byte count changed: expected {expected_bytes}, observed {observed}"
        )
    return digest.hexdigest()


def _read_verified_package(
    path: Path, expected_bytes: int, expected_sha256: str
) -> bytes:
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    observed = 0
    try:
        with path.open("rb") as source:
            while True:
                chunk = source.read(min(1024 * 1024, expected_bytes - observed + 1))
                if not chunk:
                    break
                observed += len(chunk)
                if observed > expected_bytes:
                    raise QualificationError(
                        "recipe package byte count changed: "
                        f"expected {expected_bytes}, observed more than expected"
                    )
                digest.update(chunk)
                chunks.append(chunk)
    except OSError as error:
        raise QualificationError(f"recipe package cannot be read: {path}") from error
    if observed != expected_bytes:
        raise QualificationError(
            f"recipe package byte count changed: expected {expected_bytes}, observed {observed}"
        )
    observed_sha256 = digest.hexdigest()
    if observed_sha256 != expected_sha256:
        raise QualificationError("recipe package digest differs from authority")
    return b"".join(chunks)


@contextmanager
def _canonical_recipe_package_tools(
    library_root: Path,
    source_commit: str,
) -> Iterator[
    tuple[
        Callable[[bytes, dict[str, object], dict[str, dict[str, object]]], None],
        Any,
        Any,
        Callable[[Any], str],
        str,
    ]
]:
    if _GIT_SHA.fullmatch(source_commit) is None:
        raise QualificationError("canonical recipe source commit is invalid")
    root = library_root.resolve(strict=True)
    if not root.is_dir():
        raise QualificationError("recipe library root must be a directory")

    # Ignore ambient Git selectors/configuration. The library root and commit
    # are explicit inputs; a shell environment must not redirect object lookup.
    git_environment = {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }
    git_environment["GIT_CONFIG_NOSYSTEM"] = "1"
    git_environment["GIT_CONFIG_GLOBAL"] = os.devnull
    git_environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    git_environment["GIT_NO_LAZY_FETCH"] = "1"

    def git(*arguments: str) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                ["git", "-C", str(root), *arguments],
                check=False,
                capture_output=True,
                env=git_environment,
                timeout=_CANONICAL_GIT_READ_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            operation = arguments[0] if arguments else "read"
            raise QualificationError(
                "canonical recipe Git "
                f"{operation} timed out after "
                f"{_CANONICAL_GIT_READ_TIMEOUT_SECONDS} seconds; verify the pinned "
                "local repository/object store is responsive, then retry"
            ) from error
        except OSError as error:
            raise QualificationError(
                "canonical recipe source repository cannot be read"
            ) from error

    repository_root = git("rev-parse", "--show-toplevel")
    if repository_root.returncode != 0:
        raise QualificationError("recipe library root is not a Git repository")
    try:
        resolved_repository_root = Path(
            repository_root.stdout.decode("utf-8").rstrip("\n")
        ).resolve(strict=True)
    except (OSError, UnicodeError) as error:
        raise QualificationError("recipe library Git root is invalid") from error
    if resolved_repository_root != root:
        raise QualificationError("recipe library root is not the selected Git root")

    commit_type = git("cat-file", "-t", source_commit)
    if commit_type.returncode != 0 or commit_type.stdout.strip() != b"commit":
        raise QualificationError(
            "canonical recipe source commit is unavailable in the selected repository"
        )
    tree = git(
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        source_commit,
        "--",
        "tools/build-catalog-index",
        "contracts/src/vonk_forge_contracts",
    )
    if tree.returncode != 0 or not tree.stdout.endswith(b"\0"):
        raise QualificationError("canonical recipe source tree cannot be read")

    tool_relative = "tools/build-catalog-index"
    contracts_prefix = "contracts/src/vonk_forge_contracts/"
    required_paths = {
        tool_relative,
        f"{contracts_prefix}__init__.py",
    }
    seen_paths: set[str] = set()
    tree_entries: list[tuple[str, str]] = []
    for encoded_entry in tree.stdout[:-1].split(b"\0"):
        try:
            metadata, encoded_path = encoded_entry.split(b"\t", 1)
            mode_bytes, object_type_bytes, object_id_bytes = metadata.split()
            path = encoded_path.decode("utf-8")
            mode = mode_bytes.decode("ascii")
            object_type = object_type_bytes.decode("ascii")
            object_id = object_id_bytes.decode("ascii")
        except (UnicodeError, ValueError) as error:
            raise QualificationError(
                "canonical recipe source tree is malformed"
            ) from error
        pure_path = PurePosixPath(path)
        allowed_path = path == tool_relative or path.startswith(contracts_prefix)
        if (
            not allowed_path
            or pure_path.is_absolute()
            or path != pure_path.as_posix()
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or mode not in {"100644", "100755"}
            or object_type != "blob"
            or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", object_id) is None
            or path in seen_paths
        ):
            raise QualificationError(
                "canonical recipe source tree contains an unsafe entry"
            )
        seen_paths.add(path)
        tree_entries.append((path, object_id))
    if not required_paths.issubset(seen_paths):
        raise QualificationError("canonical recipe source tree is incomplete")

    with tempfile.TemporaryDirectory(prefix="vonk-campaign-canonical-") as temporary:
        snapshot_root = Path(temporary).resolve(strict=True)
        for relative, object_id in tree_entries:
            content = git("cat-file", "blob", object_id)
            if content.returncode != 0:
                raise QualificationError("canonical recipe source blob cannot be read")
            destination = snapshot_root.joinpath(*PurePosixPath(relative).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content.stdout)

        validator_path = snapshot_root / tool_relative
        contracts_init = snapshot_root / f"{contracts_prefix}__init__.py"
        contract_source = str(contracts_init.parent.parent)
        original_sys_path = sys.path.copy()
        original_contract_modules = {
            name: module
            for name, module in sys.modules.items()
            if name == "vonk_forge_contracts"
            or name.startswith("vonk_forge_contracts.")
        }
        try:
            try:
                for module_name in tuple(sys.modules):
                    if module_name == "vonk_forge_contracts" or module_name.startswith(
                        "vonk_forge_contracts."
                    ):
                        del sys.modules[module_name]
                sys.path.insert(0, contract_source)
                tool_namespace = runpy.run_path(str(validator_path))
                import vonk_forge_contracts
                from vonk_forge_contracts import (
                    ModelDefinition,
                    RecipeDefinition,
                    content_sha256,
                )
            except Exception as error:
                raise QualificationError(
                    "canonical recipe contracts or package validator cannot be loaded"
                ) from error

            if Path(vonk_forge_contracts.__file__).resolve() != contracts_init:
                raise QualificationError(
                    "loaded recipe contracts do not belong to the pinned source commit"
                )
            candidate_validator = tool_namespace.get("validate_recipe_archive")
            if not callable(candidate_validator):
                raise QualificationError(
                    "canonical recipe package validator is unavailable"
                )
            validator = cast(
                Callable[
                    [bytes, dict[str, object], dict[str, dict[str, object]]], None
                ],
                candidate_validator,
            )
            package_media_type = _string(
                tool_namespace.get("PACKAGE_MEDIA_TYPE"),
                "canonical recipe package media type",
            )
            yield (
                validator,
                RecipeDefinition,
                ModelDefinition,
                content_sha256,
                package_media_type,
            )
        finally:
            sys.path[:] = original_sys_path
            for module_name in tuple(sys.modules):
                if module_name == "vonk_forge_contracts" or module_name.startswith(
                    "vonk_forge_contracts."
                ):
                    del sys.modules[module_name]
            sys.modules.update(original_contract_modules)


def _catalog_recipe_entries(
    catalog_document: Mapping[str, object],
) -> dict[str, tuple[Mapping[str, object], Mapping[str, object]]]:
    raw_entries = catalog_document.get("recipes")
    if not isinstance(raw_entries, list):
        raise QualificationError("catalog index recipes must be an array")
    entries: dict[str, tuple[Mapping[str, object], Mapping[str, object]]] = {}
    for position, raw_entry in enumerate(raw_entries):
        entry = _object(raw_entry, f"catalog recipe {position}")
        recipe = _object(entry.get("document"), f"catalog recipe {position} document")
        identity = _object(
            recipe.get("identity"), f"catalog recipe {position} identity"
        )
        publisher = _string(
            identity.get("publisher"), f"catalog recipe {position} publisher"
        )
        slug = _string(identity.get("slug"), f"catalog recipe {position} slug")
        key = f"{publisher}/{slug}"
        if key in entries:
            raise QualificationError(f"catalog index repeats recipe identity {key}")
        entries[key] = (entry, recipe)
    return entries


def _catalog_model_entries(
    catalog_document: Mapping[str, object],
) -> dict[str, tuple[Mapping[str, object], Mapping[str, object]]]:
    raw_entries = catalog_document.get("catalog_entities")
    if not isinstance(raw_entries, list):
        raise QualificationError("catalog index catalog_entities must be an array")
    entries: dict[str, tuple[Mapping[str, object], Mapping[str, object]]] = {}
    for position, raw_entry in enumerate(raw_entries):
        entry = _object(raw_entry, f"catalog entity {position}")
        model = _object(entry.get("document"), f"catalog entity {position} document")
        identity = _object(model.get("identity"), f"catalog entity {position} identity")
        publisher = _string(
            identity.get("publisher"), f"catalog entity {position} publisher"
        )
        slug = _string(identity.get("slug"), f"catalog entity {position} slug")
        key = f"{publisher}/{slug}"
        if key in entries:
            raise QualificationError(f"catalog index repeats Model identity {key}")
        entries[key] = (entry, model)
    return entries


def _recipe_model_closure(
    recipe: Any,
    catalog_models: Mapping[str, tuple[Mapping[str, object], Mapping[str, object]]],
    model_type: Any,
    content_sha256: Callable[[Any], str],
    recipe_key: str,
) -> dict[str, dict[str, object]]:
    pending: list[tuple[str, str]] = [
        (selection.model.publisher, selection.model.slug) for selection in recipe.models
    ]
    result: dict[str, dict[str, object]] = {}
    while pending:
        publisher, slug = pending.pop()
        key = f"{publisher}/{slug}"
        if key in result:
            continue
        catalog_entry = catalog_models.get(key)
        if catalog_entry is None:
            raise QualificationError(
                f"{recipe_key} catalog is missing exact Model {key}"
            )
        raw_entry, raw_model = catalog_entry
        try:
            model = model_type.model_validate(raw_model)
        except Exception as error:
            raise QualificationError(
                f"{recipe_key} catalog Model {key} is invalid"
            ) from error
        model_digest = content_sha256(model)
        if raw_entry.get("content_sha256") != model_digest:
            raise QualificationError(
                f"{recipe_key} catalog Model {key} digest is stale"
            )
        result[key] = model.model_dump(
            mode="json", exclude_unset=False, exclude_none=False
        )
        pending.extend(
            (reference.publisher, reference.slug) for reference in model.dependencies
        )
        if model.supersedes is not None:
            pending.append((model.supersedes.publisher, model.supersedes.slug))
    return result


def _validate_repository_recipe_packages(
    root: Path,
    rows: Sequence[RecipeAuthorityRow],
    recipe_entries: Mapping[str, tuple[Mapping[str, object], Mapping[str, object]]],
    catalog_models: Mapping[str, tuple[Mapping[str, object], Mapping[str, object]]],
    canonical_tools: tuple[
        Callable[[bytes, dict[str, object], dict[str, dict[str, object]]], None],
        Any,
        Any,
        Callable[[Any], str],
        str,
    ],
) -> None:
    (
        validate_recipe_archive,
        recipe_type,
        model_type,
        content_sha256,
        expected_package_media_type,
    ) = canonical_tools
    for row in rows:
        catalog_recipe = recipe_entries.get(row.key)
        if catalog_recipe is None:
            raise QualificationError(f"{row.key} is absent from the catalog index")
        catalog_entry, recipe_document = catalog_recipe
        try:
            recipe = recipe_type.model_validate(recipe_document)
        except Exception as error:
            raise QualificationError(f"{row.key} catalog Recipe is invalid") from error
        canonical_recipe_digest = content_sha256(recipe)
        catalog_digest = _string(
            catalog_entry.get("content_sha256"), f"{row.key} catalog recipe digest"
        )
        if catalog_digest != canonical_recipe_digest:
            raise QualificationError(f"{row.key} catalog Recipe digest is stale")
        recipe_identity = f"{recipe.identity.publisher}/{recipe.identity.slug}"
        if recipe_identity != row.key:
            raise QualificationError(f"{row.key} catalog Recipe identity differs")
        if row.content_sha256 != canonical_recipe_digest:
            raise QualificationError(f"{row.key} Recipe digest differs from catalog")
        if row.recipe_version != recipe.release.version:
            raise QualificationError(f"{row.key} Recipe version differs from catalog")

        catalog_package = _object(
            catalog_entry.get("package"), f"{row.key} catalog package"
        )
        catalog_package_path = _string(
            catalog_package.get("path"), f"{row.key} catalog package path"
        )
        catalog_package_digest = _string(
            catalog_package.get("sha256"), f"{row.key} catalog package digest"
        )
        if _SHA256.fullmatch(catalog_package_digest) is None:
            raise QualificationError(f"{row.key} catalog package digest is invalid")
        catalog_package_bytes = _integer(
            catalog_package.get("expected_bytes"),
            f"{row.key} catalog package bytes",
            1,
            2**63 - 1,
        )
        catalog_package_media_type = _string(
            catalog_package.get("media_type"),
            f"{row.key} catalog package media type",
        )
        if catalog_package_media_type != expected_package_media_type:
            raise QualificationError(
                f"{row.key} catalog package media type is not canonical"
            )
        if catalog_package_path != row.package["path"]:
            raise QualificationError(f"{row.key} package path differs from catalog")
        if catalog_package_digest != row.package["sha256"]:
            raise QualificationError(f"{row.key} package sha256 differs from catalog")
        if catalog_package_bytes != row.package["expected_bytes"]:
            raise QualificationError(
                f"{row.key} package expected_bytes differs from catalog"
            )
        if catalog_package_media_type != row.package["media_type"]:
            raise QualificationError(
                f"{row.key} package media_type differs from catalog"
            )
        catalog_recipe_digest = _string(
            catalog_package.get("recipe_content_sha256"),
            f"{row.key} catalog package Recipe digest",
        )
        if catalog_recipe_digest != canonical_recipe_digest:
            raise QualificationError(
                f"{row.key} catalog package Recipe digest differs from catalog"
            )

        package_path = _relative_path(root, row.package["path"], f"{row.key} package")
        expected_bytes = _integer(
            row.package["expected_bytes"], f"{row.key} package bytes", 1, 2**63 - 1
        )
        package_payload = _read_verified_package(
            package_path, expected_bytes, str(row.package["sha256"])
        )
        model_closure = _recipe_model_closure(
            recipe,
            catalog_models,
            model_type,
            content_sha256,
            row.key,
        )
        try:
            validate_recipe_archive(
                package_payload,
                recipe.model_dump(mode="json", exclude_unset=False, exclude_none=False),
                model_closure,
            )
        except SystemExit as error:
            raise QualificationError(
                f"{row.key} package closure is invalid: {error}"
            ) from error
        except Exception as error:
            raise QualificationError(
                f"{row.key} package closure is invalid: {error}"
            ) from error


def _bind_repository_inputs(
    manifest: CampaignManifest, library_root: Path, fixtures: FixtureRegistry
) -> None:
    root = library_root.resolve(strict=True)
    if not root.is_dir():
        raise QualificationError("recipe library root must be a directory")
    if not manifest.path.is_relative_to(root):
        raise QualificationError(
            "campaign manifest must be inside the recipe repository"
        )
    if not manifest.fixture_manifest.is_relative_to(root):
        raise QualificationError(
            "fixture manifest must be inside the recipe repository"
        )
    manifest_value = _object(
        _strict_read(manifest.path, "campaign manifest")[0], "manifest"
    )
    resolved_authority = _relative_path(
        root,
        manifest_value["qualification_authority"],
        "authority path",
        allow_parent=True,
        base=manifest.path.parent,
    )
    if not resolved_authority.is_relative_to(root):
        raise QualificationError(
            "qualification authority must be inside the recipe repository"
        )

    catalog_path = _relative_path(root, "catalog-index.json", "catalog index")
    index_path = _relative_path(
        root, "qualification/qualification-index.json", "qualification index"
    )
    catalog_size = catalog_path.stat().st_size
    if catalog_size > _MAX_CATALOG_FILE_BYTES:
        raise QualificationError(
            f"catalog index exceeds its {_MAX_CATALOG_FILE_BYTES}-byte read bound"
        )
    fixture_size = index_path.stat().st_size
    if fixture_size > _MAX_FIXTURE_MANIFEST_BYTES:
        raise QualificationError(
            f"qualification index exceeds its {_MAX_FIXTURE_MANIFEST_BYTES}-byte read bound"
        )
    catalog_digest = _safe_file_digest(catalog_path, catalog_size)
    qualification_digest = _safe_file_digest(index_path, fixture_size)
    if catalog_digest != manifest.authority.catalog["catalog_index_sha256"]:
        raise QualificationError("catalog index differs from the reviewed authority")
    if qualification_digest != manifest.authority.catalog["qualification_index_sha256"]:
        raise QualificationError("qualification fixture index differs from authority")
    if fixtures.manifest_sha256 != qualification_digest:
        raise QualificationError("loaded fixture manifest is not the reviewed index")
    catalog_value, _catalog_raw = _strict_read(
        catalog_path,
        "catalog index",
        maximum_bytes=_MAX_CATALOG_FILE_BYTES,
    )
    catalog_document = _object(catalog_value, "catalog index")
    source_commit = _string(
        catalog_document.get("source_commit"), "catalog source commit"
    )
    authority_source_commit = _string(
        manifest.authority.catalog.get("source_commit"),
        "authority catalog source commit",
    )
    if source_commit != authority_source_commit:
        raise QualificationError("catalog source commit differs from its authority")

    recipe_entries = _catalog_recipe_entries(catalog_document)
    catalog_models = _catalog_model_entries(catalog_document)
    with _canonical_recipe_package_tools(root, source_commit) as canonical_tools:
        _validate_repository_recipe_packages(
            root,
            manifest.authority.rows,
            recipe_entries,
            catalog_models,
            canonical_tools,
        )


def _service_fixture_inputs(value: object, result: list[str]) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in {"$fixture_data_uri", "$fixture_base64"}:
                if not isinstance(child, str):
                    raise QualificationError("service fixture reference is invalid")
                if child not in result:
                    result.append(child)
            else:
                _service_fixture_inputs(child, result)
    elif isinstance(value, list):
        for child in value:
            _service_fixture_inputs(child, result)


def _fixture_bindings(
    row: RecipeAuthorityRow, registry: FixtureRegistry
) -> tuple[str, Mapping[str, object]]:
    artifact = registry.recipes.get(row.key)
    service = registry.service_recipes.get(row.key)
    if (artifact is None) == (service is None):
        raise QualificationError(
            f"{row.key} must have exactly one executable smoke-fixture contract"
        )
    if artifact is not None:
        if artifact.interface != row.interface:
            raise QualificationError(
                f"{row.key} fixture interface differs from authority"
            )
        if artifact.content_sha256 != row.content_sha256:
            raise QualificationError(f"{row.key} fixture digest differs from authority")
        cases = artifact.all_cases
        case_ids = tuple(case.case_id for case in cases)
        inputs: list[str] = []
        for case in cases:
            for _slot, fixture in case.inputs:
                if fixture.fixture_id not in inputs:
                    inputs.append(fixture.fixture_id)
        preview = artifact.preview()
        kind = "artifact-job"
    else:
        assert service is not None
        if row.interface != "openai-service":
            raise QualificationError(
                f"{row.key} service interface differs from authority"
            )
        if service.content_sha256 != row.content_sha256:
            raise QualificationError(
                f"{row.key} service fixture digest differs from authority"
            )
        case_ids = tuple(case.case_id for case in service.cases)
        inputs = []
        for case in service.cases:
            _service_fixture_inputs(case.body, inputs)
            _service_fixture_inputs(case.assertions, inputs)
        preview = service.preview(registry.fixtures)
        kind = "openai-service"
    if tuple(inputs) != row.qualification_inputs:
        raise QualificationError(f"{row.key} fixture identities differ from authority")
    if case_ids != row.smoke_cases:
        raise QualificationError(f"{row.key} smoke case order differs from authority")
    return kind, preview


def _model_license_rows(
    detail: Mapping[str, object],
) -> tuple[Mapping[str, object], ...]:
    raw_models = detail.get("model_documents")
    if not isinstance(raw_models, list):
        raise QualificationError("Controller recipe detail lacks exact Model documents")
    rows: list[Mapping[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for raw_model in raw_models:
        model = _object(raw_model, "Controller recipe Model")
        definition = _object(model.get("model_document"), "Model definition")
        identity = _object(definition.get("identity"), "Model identity")
        selection = _object(model.get("selection"), "recipe Model selection")
        reference = _object(selection.get("model"), "recipe Model reference")
        license_value = _object(definition.get("license"), "Model license")
        key = f"{_string(reference.get('publisher'), 'Model publisher')}/{_string(reference.get('slug'), 'Model slug')}"
        content_sha256 = _string(
            reference.get("content_sha256"), "Model content digest"
        )
        if (
            _SHA256.fullmatch(content_sha256) is None
            or identity.get("publisher") != reference.get("publisher")
            or identity.get("slug") != reference.get("slug")
        ):
            raise QualificationError("Controller Model identity/reference mismatch")
        pair = (key, content_sha256)
        if pair in seen:
            raise QualificationError(f"Controller recipe repeats Model {key}")
        seen.add(pair)
        license_row: dict[str, object] = {
            "key": key,
            "content_sha256": content_sha256,
            "spdx": _string(license_value.get("spdx"), f"{key} SPDX"),
            "url": _string(license_value.get("url"), f"{key} license URL"),
            "operator_acceptance_required": license_value.get(
                "operator_acceptance_required"
            ),
            "attribution": license_value.get("attribution"),
        }
        if not isinstance(license_row["operator_acceptance_required"], bool):
            raise QualificationError(f"{key} operator acceptance fact is invalid")
        if not isinstance(license_row["attribution"], list) or any(
            not isinstance(item, str) for item in license_row["attribution"]
        ):
            raise QualificationError(f"{key} attribution is invalid")
        restrictions = license_value.get("territorial_restrictions")
        if restrictions is not None and not isinstance(restrictions, Unset):
            if not isinstance(restrictions, Mapping):
                raise QualificationError(f"{key} territorial restrictions are invalid")
            license_row["territorial_restrictions"] = dict(restrictions)
        rows.append(license_row)
    return tuple(rows)


def _validate_current_recipe(
    client: Any, row: RecipeAuthorityRow
) -> tuple[dict[str, object], dict[str, object]]:
    detail = client.request(
        "GET", f"/api/recipe/{urllib.parse.quote(row.key, safe='')}"
    )
    try:
        from .generated_control.models.recipe_detail_response import (
            RecipeDetailResponse,
        )

        typed = RecipeDetailResponse.from_dict(detail)
    except (KeyError, TypeError, ValueError) as error:
        raise QualificationError(
            f"{row.key} current Controller recipe is invalid"
        ) from error
    definition = typed.document.to_dict()
    identity = typed.identity.to_dict()
    current_digest = identity.get("content_sha256")
    if (
        detail.get("selector") != row.key
        or current_digest != row.content_sha256
        or _digest(definition) != row.content_sha256
    ):
        raise QualificationError(f"{row.key} current Controller recipe digest changed")
    if definition.get("release", {}).get("version") != row.recipe_version:
        raise QualificationError(f"{row.key} release version differs from authority")
    observed_models = _model_license_rows(detail)
    expected_models = tuple(dict(item) for item in row.model_license_refs)
    observed_by_identity = {
        (str(item["key"]), str(item["content_sha256"])): item
        for item in observed_models
    }
    expected_by_identity = {
        (str(item["key"]), str(item["content_sha256"])): item
        for item in expected_models
    }
    if observed_by_identity != expected_by_identity:
        raise QualificationError(f"{row.key} exact Model license facts changed")
    model_digests = {str(item["content_sha256"]) for item in row.model_license_refs}
    selected_digests = {
        str(model.get("content_sha256"))
        for item in definition.get("models", [])
        if isinstance(item, Mapping)
        if isinstance((model := item.get("model")), Mapping)
    }
    if selected_digests != model_digests:
        raise QualificationError(f"{row.key} selected Models differ from authority")
    return dict(detail), definition


def _typed_fleet(client: Any) -> dict[str, object]:
    value = client.request("GET", "/api/fleet")
    try:
        return FleetSnapshot.from_dict(value).to_dict()
    except (KeyError, TypeError, ValueError) as error:
        raise QualificationError("Controller Fleet snapshot is invalid") from error


def _deployment_provenance_by_node(
    client: Any, node_ids: Sequence[str]
) -> dict[str, Mapping[str, object]]:
    provenance_by_node: dict[str, Mapping[str, object]] = {}
    for node_id in sorted(set(node_ids)):
        selector = urllib.parse.quote(node_id, safe="")
        raw = client.request("GET", f"/api/fleet/{selector}")
        try:
            detail = FleetNodeDetailResponse.from_dict(raw)
            typed = detail.to_dict()
        except (KeyError, TypeError, ValueError) as error:
            raise QualificationError(
                f"Controller deployment provenance for {node_id} is invalid"
            ) from error
        if typed.get("id") != node_id:
            raise QualificationError(
                "Controller deployment provenance changed Spark identity"
            )
        provenance = typed.get("provenance")
        if not isinstance(provenance, Mapping):
            raise QualificationError(
                f"Controller deployment provenance is unavailable for {node_id}"
            )
        provenance_by_node[node_id] = dict(provenance)
    return provenance_by_node


def _nodes(fleet: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    raw = fleet.get("nodes")
    if not isinstance(raw, list):
        raise QualificationError("Controller Fleet snapshot has no node roster")
    nodes: dict[str, Mapping[str, object]] = {}
    for item in raw:
        node = _object(item, "Fleet node")
        node_id = _string(node.get("id"), "Fleet node ID")
        if _NODE_ID.fullmatch(node_id) is None or node_id in nodes:
            raise QualificationError("Fleet node identity is invalid or duplicated")
        nodes[node_id] = node
    if not nodes:
        raise QualificationError("Controller Fleet roster is empty")
    return nodes


def _loaded_presences(node: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw = node.get("loaded")
    if not isinstance(raw, list):
        raise QualificationError("Fleet node has no loaded-workload list")
    return [_object(item, "Fleet loaded workload") for item in raw]


def _validate_stopped_rank_receipt(
    final: Mapping[str, object],
    *,
    run_id: str,
    node_to_rank: Mapping[str, int],
    label: str,
) -> None:
    """Validate stop evidence through the generated Controller wire model."""
    try:
        typed = RunSwitchFinalVerifyResult.from_dict(cast(Any, final))
    except (KeyError, TypeError, ValueError) as error:
        raise QualificationError(
            f"{label} is not a typed final verification"
        ) from error
    if (
        typed.final_verified is not True
        or typed.phase != "final_verify"
        or typed.run_id != run_id
        or typed.state != "stopped"
        or typed.route_state != "withdrawn"
    ):
        raise QualificationError(f"{label} does not prove the exact run stopped")
    observed: dict[str, int] = {}
    for rank in typed.ranks:
        node_id = _string(rank.node_id, f"{label} node ID")
        rank_number = _integer(rank.rank, f"{label} rank", 0, 31)
        _string(rank.role, f"{label} rank role")
        if rank.state != "stopped" or rank.fresh is False or node_id in observed:
            raise QualificationError(f"{label} has invalid stopped rank evidence")
        observed[node_id] = rank_number
    if observed != dict(node_to_rank):
        raise QualificationError(f"{label} changed its exact node/rank assignment")


def _online(node: Mapping[str, object]) -> bool:
    connection = node.get("connection")
    return (
        isinstance(connection, Mapping) and connection.get("online_state") == "online"
    )


def _boot_id(node: Mapping[str, object], *, require_live: bool = True) -> str | None:
    telemetry = node.get("telemetry")
    if not isinstance(telemetry, Mapping):
        return None
    if require_live and telemetry.get("freshness") != "live":
        return None
    sample = telemetry.get("sample")
    if not isinstance(sample, Mapping):
        return None
    candidate = sample.get("boot_id")
    return candidate if isinstance(candidate, str) and candidate else None


def _run_presences(
    fleet: Mapping[str, object], run_id: str
) -> list[tuple[str, Mapping[str, object]]]:
    result: list[tuple[str, Mapping[str, object]]] = []
    for node_id, node in _nodes(fleet).items():
        loaded = node.get("loaded")
        if not isinstance(loaded, list):
            raise QualificationError(f"Fleet loaded-run list is invalid for {node_id}")
        for raw_presence in loaded:
            presence = _object(raw_presence, "Fleet run presence")
            if presence.get("run_id") == run_id:
                result.append((node_id, presence))
    return result


def _all_loaded_runs(fleet: Mapping[str, object]) -> set[str]:
    result: set[str] = set()
    for node_id, node in _nodes(fleet).items():
        loaded = node.get("loaded")
        if not isinstance(loaded, list):
            raise QualificationError(f"Fleet loaded-run list is invalid for {node_id}")
        for raw_presence in loaded:
            presence = _object(raw_presence, "Fleet run presence")
            result.add(_string(presence.get("run_id"), "loaded run ID"))
    return result


def _assert_fleet_exclusive(
    fleet: Mapping[str, object],
    *,
    owned_run_ids: AbstractSet[str] = frozenset(),
    replace_run_id: str | None = None,
) -> dict[str, object] | None:
    foreign = _all_loaded_runs(fleet) - owned_run_ids
    if not foreign:
        if replace_run_id is not None:
            raise QualificationError(
                "acknowledged run is no longer the sole current foreign loaded run"
            )
        return None
    if replace_run_id is None:
        raise QualificationError(
            "whole-fleet profile is unsafe: loaded run is present; pass "
            "--replace-run-id with the exact run ID to acknowledge replacement"
        )
    if foreign != {replace_run_id}:
        if replace_run_id not in foreign:
            raise QualificationError(
                "acknowledged run does not match the sole current foreign loaded run"
            )
        raise QualificationError(
            "whole-fleet profile replacement requires the acknowledged run to be "
            "the sole current foreign run"
        )

    nodes = _nodes(fleet)
    presences = _run_presences(fleet, replace_run_id)
    if not presences:
        raise QualificationError("acknowledged run has no complete run membership")
    roster = set(nodes)
    observed_node_ids = [node_id for node_id, _presence in presences]
    if len(observed_node_ids) != len(set(observed_node_ids)):
        raise QualificationError(
            "acknowledged run has duplicate or conflicting rank membership"
        )

    first = presences[0][1]
    raw_members = first.get("member_node_ids")
    if not isinstance(raw_members, list) or any(
        not isinstance(item, str) or _NODE_ID.fullmatch(item) is None
        for item in raw_members
    ):
        raise QualificationError("acknowledged run has invalid complete run membership")
    members = tuple(raw_members)
    if len(members) != len(set(members)) or tuple(sorted(members)) != members:
        raise QualificationError("acknowledged run membership is not canonical")
    if not set(members) <= roster:
        raise QualificationError(
            "acknowledged run has complete run membership outside the current Fleet"
        )
    expected_rank_count = first.get("expected_rank_count")
    if (
        not isinstance(expected_rank_count, int)
        or isinstance(expected_rank_count, bool)
        or expected_rank_count < 1
        or expected_rank_count > len(roster)
        or expected_rank_count != len(members)
    ):
        raise QualificationError("acknowledged run has invalid complete run membership")
    if set(observed_node_ids) != set(members) or len(presences) != expected_rank_count:
        raise QualificationError(
            "acknowledged run is not fully present across its complete run membership"
        )

    raw_ranks = first.get("present_ranks")
    if (
        not isinstance(raw_ranks, list)
        or any(
            not isinstance(rank, int) or isinstance(rank, bool) for rank in raw_ranks
        )
        or sorted(raw_ranks) != list(range(expected_rank_count))
    ):
        raise QualificationError(
            "acknowledged run has invalid complete rank membership"
        )
    ranks: list[int] = []
    identity_keys = (
        "installation_id",
        "recipe_revision_id",
        "alias",
        "expected_rank_count",
        "member_node_ids",
        "present_ranks",
    )
    identity = tuple(first.get(key) for key in identity_keys)
    for node_id, presence in presences:
        if tuple(presence.get(key) for key in identity_keys) != identity:
            raise QualificationError(
                "acknowledged run has conflicting complete run membership"
            )
        rank = presence.get("rank")
        if not isinstance(rank, int) or isinstance(rank, bool):
            raise QualificationError("acknowledged run has invalid rank membership")
        ranks.append(rank)
        if (
            presence.get("healthy") is not True
            or presence.get("group_state") != "healthy"
            or presence.get("run_state") != "running"
            or presence.get("rank_state") != "running"
            or presence.get("route_state") != "published"
            or presence.get("rank_fresh") is not True
        ):
            raise QualificationError(
                "only a fully observed healthy run can be explicitly replaced"
            )
    if sorted(ranks) != list(range(expected_rank_count)):
        raise QualificationError("acknowledged run has incomplete rank membership")

    return {
        "run_id": replace_run_id,
        "member_node_ids": list(members),
        "expected_rank_count": expected_rank_count,
        "present_ranks": list(raw_ranks),
    }


def _assert_batch_fleet_exclusive(
    fleet: Mapping[str, object],
    *,
    replace_run_ids: Sequence[str] = (),
) -> list[dict[str, object]]:
    """Bind every currently loaded run to an explicitly selected healthy receipt."""
    selected = list(replace_run_ids)
    if len(selected) != len(set(selected)) or any(not value for value in selected):
        raise QualificationError("replacement run IDs must be unique exact identities")
    foreign = _all_loaded_runs(fleet)
    if not selected:
        _assert_fleet_exclusive(fleet)
        return []
    if foreign != set(selected):
        missing = sorted(set(selected) - foreign)
        unreviewed = sorted(foreign - set(selected))
        raise QualificationError(
            "whole-Fleet batch replacement differs from live runs "
            f"(missing={missing}, unreviewed={unreviewed})"
        )
    runs: list[dict[str, object]] = []
    assigned_nodes: set[str] = set()
    aliases: set[str] = set()
    for run_id in selected:
        # Reuse the complete single-run proof while isolating this exact run
        # from the other independently acknowledged runs in the snapshot.
        isolated = dict(fleet)
        isolated_nodes: list[dict[str, object]] = []
        for node_id, node in _nodes(fleet).items():
            loaded = node.get("loaded")
            if not isinstance(loaded, list):
                raise QualificationError(
                    f"Fleet loaded-run list is invalid for {node_id}"
                )
            isolated_nodes.append(
                {
                    **dict(node),
                    "loaded": [
                        dict(_object(item, "Fleet run presence"))
                        for item in loaded
                        if _object(item, "Fleet run presence").get("run_id") == run_id
                    ],
                }
            )
        isolated["nodes"] = isolated_nodes
        identity = _assert_fleet_exclusive(isolated, replace_run_id=run_id)
        assert identity is not None
        presence = _run_presences(isolated, run_id)[0][1]
        alias = _string(presence.get("alias"), "replacement workload alias")
        members = _string_array(
            identity.get("member_node_ids"), "replacement workload member nodes"
        )
        if assigned_nodes & set(members) or alias in aliases:
            raise QualificationError(
                "replacement workloads overlap node or route identities"
            )
        assigned_nodes.update(members)
        aliases.add(alias)
        runs.append({**identity, "alias": alias})
    return sorted(runs, key=lambda item: str(item["run_id"]))


def _qualification_lock_nodes(
    fleet: Mapping[str, object], exact_node_ids: Sequence[str]
) -> list[str]:
    """Serialize profile operations against the complete observed Fleet.

    Profile preview/load owns a whole-Fleet desired profile even when only one
    or two nodes run the recipe. Locking just the assigned nodes would let two
    ledgers with disjoint assignments race through whole-Fleet profile state.
    """

    roster = set(_nodes(fleet))
    exact = set(exact_node_ids)
    if exact - roster:
        raise QualificationError(
            "qualification evidence refers to a Spark absent from current Fleet authority"
        )
    return sorted(roster | exact)


def _require_locked_fleet_roster(
    fleet: Mapping[str, object], locked_node_ids: Sequence[str]
) -> None:
    if set(_nodes(fleet)) != set(locked_node_ids):
        raise QualificationError(
            "Fleet membership changed while acquiring qualification locks; rerun with a fresh preview"
        )


def _labels(profile: Mapping[str, object]) -> Mapping[str, object]:
    return _object(profile.get("labels"), "profile labels")


def _profile_view(client: Any, number: int) -> dict[str, object]:
    value = client.request("GET", f"/api/profile/{number}")
    try:
        typed = FleetProfileView.from_dict(value)
    except (KeyError, TypeError, ValueError) as error:
        raise QualificationError("dedicated Fleet profile is invalid") from error
    return typed.to_dict()


def _assert_profile_owner(
    profile: Mapping[str, object], authority_id: str, ledger_id: str
) -> None:
    if profile.get("status") == "not-created":
        raise QualificationError(
            "profile number is not created; pre-create and label a dedicated "
            "qualification profile before running"
        )
    labels = _labels(profile)
    if (
        labels.get(_PROFILE_AUTHORITY_LABEL) != authority_id
        or labels.get(_PROFILE_LEDGER_LABEL) != ledger_id
    ):
        raise QualificationError(
            "profile is not explicitly dedicated to this authority and evidence ledger"
        )


def _profile_assignment(
    row: RecipeAuthorityRow, node_ids: Sequence[str], alias: str
) -> dict[str, object]:
    return {
        "recipe_selector": row.key,
        "spark_ids": sorted(node_ids),
        "assignment_name": alias,
        "desired_state": "running",
    }


def _profile_assignments_equal(
    profile: Mapping[str, object], expected: Sequence[Mapping[str, object]]
) -> bool:
    current = profile.get("assignments")
    if not isinstance(current, list) or len(current) != len(expected):
        return False
    normalized: list[dict[str, object]] = []
    for raw in current:
        assignment = _object(raw, "profile assignment")
        model = assignment.get("model")
        variant = model.get("variant") if isinstance(model, Mapping) else None
        normalized.append(
            {
                "recipe_selector": assignment.get("recipe_selector"),
                "spark_ids": assignment.get("spark_ids"),
                "assignment_name": assignment.get("selector"),
                "desired_state": "running",
                **({"model_variant": variant} if variant is not None else {}),
            }
        )
    return normalized == list(expected)


def _save_profile(
    client: Any,
    profile: Mapping[str, object],
    *,
    assignments: Sequence[Mapping[str, object]],
    authority_id: str,
    ledger_id: str,
) -> dict[str, object]:
    body: dict[str, object] = {
        "name": str(profile.get("name")),
        "description": f"Sequential physical qualification: {authority_id}",
        "installation_policy": "keep-cached",
        "labels": {
            _PROFILE_AUTHORITY_LABEL: authority_id,
            _PROFILE_LEDGER_LABEL: ledger_id,
        },
        "favorite": False,
        "expected_revision": profile.get("revision"),
        "assignments": [dict(item) for item in assignments],
    }
    result = client.request("PUT", f"/api/profile/{profile['number']}", body)
    try:
        return FleetProfileView.from_dict(result).to_dict()
    except (KeyError, TypeError, ValueError) as error:
        raise QualificationError(
            "Controller did not return a valid saved profile"
        ) from error


def _validate_preparations(
    preview: Mapping[str, object],
    row: RecipeAuthorityRow,
    node_ids: Sequence[str],
) -> dict[str, object]:
    raw = preview.get("preparations")
    if not isinstance(raw, list) or len(raw) != 1:
        raise QualificationError(
            f"{row.key} profile preview lacks one exact cache preparation receipt"
        )
    preparation_entry = _object(raw[0], "profile preparation entry")
    preparation = _object(preparation_entry.get("preparation"), "rollout preparation")
    if preparation.get("controller_ready") is not True:
        raise QualificationError(
            f"{row.key} exact model/image assets are not verified in the Controller cache"
        )
    if preparation.get("target_node_ids") != sorted(node_ids):
        raise QualificationError(
            f"{row.key} preparation does not cover the exact selected Sparks"
        )
    model = _object(preparation.get("model"), "model preparation")
    if model.get("recipe_revision_sha256") != row.content_sha256:
        raise QualificationError(f"{row.key} preparation is bound to another revision")
    model_digests = {str(item["content_sha256"]) for item in row.model_license_refs}
    observed_model_digests = {str(model.get("model_content_sha256"))}
    dependencies = model.get("dependency_model_content_sha256", [])
    if not isinstance(dependencies, list) or any(
        not isinstance(value, str) for value in dependencies
    ):
        raise QualificationError(
            f"{row.key} preparation Model dependency identities are invalid"
        )
    observed_model_digests.update(dependencies)
    if observed_model_digests != model_digests:
        raise QualificationError(
            f"{row.key} prepared exact Models differ from authority"
        )
    artifact_set_sha256 = model.get("artifact_set_sha256")
    if (
        not isinstance(artifact_set_sha256, str)
        or _SHA256.fullmatch(artifact_set_sha256) is None
    ):
        raise QualificationError(f"{row.key} model artifact-set identity is invalid")
    model_controller = _object(model.get("controller"), "Controller model cache")
    if (
        model_controller.get("state") != "ready"
        or model_controller.get("verified_sha256") != artifact_set_sha256
        or model_controller.get("verified_at") is None
        or model_controller.get("expected_bytes") != model.get("artifact_set_bytes")
        or model_controller.get("verified_bytes") != model.get("artifact_set_bytes")
        or model_controller.get("missing_bytes") != 0
    ):
        raise QualificationError(
            f"{row.key} Controller model cache lacks exact verification"
        )
    model_targets = model.get("targets")
    if not isinstance(model_targets, list):
        raise QualificationError(f"{row.key} model target evidence is invalid")
    if len(model_targets) != len(node_ids):
        raise QualificationError(f"{row.key} model target evidence is incomplete")
    model_target_map: dict[str, Mapping[str, object]] = {}
    for raw_target in model_targets:
        target = _object(raw_target, "model target")
        target_node_id = _string(target.get("node_id"), "model target Spark ID")
        if target_node_id in model_target_map:
            raise QualificationError(f"{row.key} model target evidence is duplicated")
        model_target_map[target_node_id] = target
    if set(model_target_map) != set(node_ids):
        raise QualificationError(f"{row.key} model target scope differs from selection")
    for target in model_target_map.values():
        state = target.get("state")
        if state == "ready":
            if (
                target.get("verified_sha256") != artifact_set_sha256
                or target.get("verified_at") is None
            ):
                raise QualificationError(
                    f"{row.key} selected Spark has mismatched model verification"
                )
        elif state in {"unknown", "missing"}:
            if (
                target.get("verified_sha256") is not None
                or target.get("verified_at") is not None
            ):
                raise QualificationError(
                    f"{row.key} unready model target claims verified bytes"
                )
        else:
            raise QualificationError(
                f"{row.key} model target state {state!r} is not eligible for exact transfer"
            )
    image = _object(preparation.get("runtime_image"), "runtime image preparation")
    image_digest = image.get("image_digest")
    architecture = image.get("architecture")
    image_layout = image.get("oci_layout_sha256")
    image_bytes = image.get("image_bytes")
    if (
        not isinstance(image_digest, str)
        or _OCI_DIGEST.fullmatch(image_digest) is None
        or architecture != "linux-arm64"
        or not isinstance(image_layout, str)
        or _SHA256.fullmatch(image_layout) is None
        or type(image_bytes) is not int
        or image_bytes < 0
    ):
        raise QualificationError(f"{row.key} runtime image identity is invalid")
    image_controller = _object(
        image.get("controller"), "Controller runtime image cache"
    )
    if (
        image_controller.get("state") != "ready"
        or image_controller.get("verified_sha256") != image_layout
        or image_controller.get("verified_at") is None
        or image_controller.get("expected_bytes") != image_bytes
        or image_controller.get("verified_bytes") != image_bytes
        or image_controller.get("missing_bytes") != 0
    ):
        raise QualificationError(f"{row.key} Controller runtime image is not verified")
    image_targets = image.get("targets")
    if not isinstance(image_targets, list):
        raise QualificationError(f"{row.key} runtime image target evidence is invalid")
    if len(image_targets) != len(node_ids):
        raise QualificationError(
            f"{row.key} runtime image target evidence is incomplete"
        )
    image_target_map: dict[str, Mapping[str, object]] = {}
    for raw_target in image_targets:
        target = _object(raw_target, "runtime image target")
        target_node_id = _string(target.get("node_id"), "runtime image target Spark ID")
        if target_node_id in image_target_map:
            raise QualificationError(
                f"{row.key} runtime image target evidence is duplicated"
            )
        image_target_map[target_node_id] = target
    if set(image_target_map) != set(node_ids):
        raise QualificationError(
            f"{row.key} runtime image target scope differs from selection"
        )
    for target in image_target_map.values():
        state = target.get("state")
        if state == "ready":
            if (
                target.get("verified_sha256") != image_layout
                or target.get("imported_image_digest") != image_digest
                or target.get("verified_at") is None
            ):
                raise QualificationError(
                    f"{row.key} selected Spark has mismatched runtime image verification"
                )
        elif state in {"unknown", "missing"}:
            if (
                target.get("verified_sha256") is not None
                or target.get("imported_image_digest") is not None
                or target.get("verified_at") is not None
            ):
                raise QualificationError(
                    f"{row.key} unready runtime image target claims verified bytes"
                )
        else:
            raise QualificationError(
                f"{row.key} runtime image target state {state!r} is not eligible for exact transfer"
            )

    all_targets_ready = all(
        target.get("state") == "ready"
        for target in (*model_target_map.values(), *image_target_map.values())
    )
    if preparation.get("targets_ready") is not all_targets_ready:
        raise QualificationError(
            f"{row.key} target readiness does not match asset evidence"
        )
    exceptions = preparation.get("exceptions", [])
    if not isinstance(exceptions, list):
        raise QualificationError(f"{row.key} preparation exception evidence is invalid")
    exceptions_ready = True
    for raw_exception in exceptions:
        exception = _object(raw_exception, "preparation exception")
        if exception.get("state") != "ready":
            exceptions_ready = False
    reasons = preparation.get("reasons", [])
    if not isinstance(reasons, list):
        raise QualificationError(f"{row.key} preparation reasons are invalid")
    blockers_absent = True
    for raw_reason in reasons:
        reason = _object(raw_reason, "preparation reason")
        if reason.get("severity") == "blocker":
            blockers_absent = False
    expected_ready = all_targets_ready and exceptions_ready and blockers_absent
    if preparation.get("ready") is not expected_ready:
        raise QualificationError(
            f"{row.key} rollout readiness does not match asset evidence"
        )
    if not exceptions_ready or not blockers_absent:
        raise QualificationError(
            f"{row.key} preparation contains a non-transfer blocker"
        )
    return {
        "recipe_revision_sha256": row.content_sha256,
        "model_content_sha256": model.get("model_content_sha256"),
        "dependency_model_content_sha256": sorted(dependencies),
        "artifact_set_sha256": artifact_set_sha256,
        "image_digest": image_digest,
        "architecture": architecture,
        "oci_layout_sha256": image_layout,
        "target_node_ids": sorted(node_ids),
    }


def _request_key(campaign_id: str, key: str, phase: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL, f"vonk-qualification:{campaign_id}:{key}:{phase}"
        )
    )


def _application_plan_digest(
    reconciliation_digest: str,
    request_key: str,
    *,
    retry_of_application_id: str | None = None,
) -> str:
    """Mirror Controller's execution identity layered over the preview digest."""
    return _digest(
        {
            "schema_version": 2,
            "reconciliation_digest": reconciliation_digest,
            "retry_of_application_id": retry_of_application_id,
            "request_key": request_key,
        }
    )


def _validated_load_application(
    value: Mapping[str, object],
    *,
    request_key: str,
    plan_digest: str,
    profile_id: str,
    profile_digest: str,
) -> dict[str, object]:
    application = _object(value, "Controller profile application")
    if (
        not isinstance(application.get("id"), str)
        or not application["id"]
        or application.get("profile_id") != profile_id
        or application.get("profile_digest") != profile_digest
        or application.get("plan_digest")
        != _application_plan_digest(plan_digest, request_key)
    ):
        raise QualificationError(
            "Controller profile application does not match the exact reviewed plan and request"
        )
    return dict(application)


def _lookup_load_request(
    client: Any,
    number: int,
    request_key: str,
    *,
    plan_digest: str,
    profile_id: str,
    profile_digest: str,
) -> dict[str, object] | None:
    try:
        value = client.request(
            "GET",
            f"/api/profile/{number}/requests/{urllib.parse.quote(request_key, safe='')}",
        )
    except ControlNotFound:
        return None
    return _validated_load_application(
        value,
        request_key=request_key,
        plan_digest=plan_digest,
        profile_id=profile_id,
        profile_digest=profile_digest,
    )


def _submit_load(
    client: Any,
    number: int,
    request_key: str,
    *,
    plan_digest: str,
    profile_id: str,
    profile_digest: str,
) -> dict[str, object]:
    """Submit or adopt exactly one reviewed profile request.

    A request-key lookup precedes submission so a restarted runner adopts a
    committed application even when its local submitted event was not written.
    An ambiguous POST is reconciled by that same lookup; only a 404 permits one
    replay, with the same durable request key and reviewed digest.
    """

    if type(number) is not int or number < 1:
        raise QualificationError("profile load number is invalid")
    try:
        normalized_request_key = str(uuid.UUID(request_key))
    except (AttributeError, TypeError, ValueError):
        normalized_request_key = None
    if (
        normalized_request_key != request_key
        or not isinstance(plan_digest, str)
        or _SHA256.fullmatch(plan_digest) is None
        or not isinstance(profile_digest, str)
        or _SHA256.fullmatch(profile_digest) is None
        or not isinstance(profile_id, str)
        or not profile_id
    ):
        raise QualificationError(
            "profile load intent lacks an exact request or preview identity"
        )

    path = f"/api/profile/{number}/load"
    payload = {"plan_digest": plan_digest, "request_key": request_key}
    found = _lookup_load_request(
        client,
        number,
        request_key,
        plan_digest=plan_digest,
        profile_id=profile_id,
        profile_digest=profile_digest,
    )
    if found is not None:
        return found

    lost_errors = (ControlTransportError, ControlUnavailable, OSError)
    for attempt in range(2):
        try:
            accepted = client.request("POST", path, payload)
        except lost_errors as lost:
            try:
                observed = _lookup_load_request(
                    client,
                    number,
                    request_key,
                    plan_digest=plan_digest,
                    profile_id=profile_id,
                    profile_digest=profile_digest,
                )
            except lost_errors:
                raise lost from None
            if observed is not None:
                return observed
            if attempt == 1:
                raise lost from None
            continue
        return _validated_load_application(
            accepted,
            request_key=request_key,
            plan_digest=plan_digest,
            profile_id=profile_id,
            profile_digest=profile_digest,
        )
    raise AssertionError("bounded profile load retry loop exhausted")


def _await_application(
    client: Any,
    accepted: Mapping[str, object],
    *,
    ledger: EvidenceLedger,
    campaign_id: str,
    key: str,
    timeout: float,
    interval: float,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> dict[str, object]:
    application_id = accepted.get("id")
    if not isinstance(application_id, str) or not application_id:
        raise QualificationError("profile load did not return a durable application ID")
    deadline = clock() + timeout
    last_signature: tuple[object, object] | None = None
    while True:
        value = client.request(
            "GET",
            f"/api/profile/applications/{urllib.parse.quote(application_id, safe='')}",
        )
        try:
            application = FleetProfileApplicationView.from_dict(value).to_dict()
        except (KeyError, TypeError, ValueError) as error:
            raise QualificationError(
                "Controller profile application is invalid"
            ) from error
        state = application.get("state")
        signature = (state, application.get("current_step"))
        if signature != last_signature:
            ledger.append(
                "profile.application.observed",
                plan_digest=campaign_id,
                recipe=key,
                payload={
                    "application_id": application_id,
                    "state": state,
                    "current_step": application.get("current_step"),
                    "total_steps": application.get("total_steps"),
                    "status_reason": application.get("status_reason"),
                },
            )
            last_signature = signature
        if state in _TERMINAL_APPLICATIONS:
            if state != "succeeded":
                raise QualificationError(
                    f"profile application entered {state}: {application.get('status_reason')}"
                )
            return application
        if clock() >= deadline:
            raise QualificationError(
                f"profile application {application_id} exceeded its bounded timeout"
            )
        sleeper(interval)


def _endpoint_exists(client: Any, alias: str) -> bool:
    try:
        client.request("GET", f"/api/endpoints/{urllib.parse.quote(alias, safe='')}")
    except ControlNotFound:
        return False
    return True


def _check_serving_fleet(
    fleet: Mapping[str, object],
    *,
    run_id: str,
    revision_id: str,
    alias: str,
    node_ids: Sequence[str],
    expected_run_state: str,
    expected_route_state: str,
    expected_health: bool,
) -> list[dict[str, object]]:
    presences = _run_presences(fleet, run_id)
    if len(presences) != len(node_ids):
        raise QualificationError("Fleet does not show the exact selected recipe ranks")
    observed_nodes: set[str] = set()
    rows: list[dict[str, object]] = []
    expected_ranks = set(range(len(node_ids)))
    for node_id, presence in presences:
        if node_id not in node_ids or node_id in observed_nodes:
            raise QualificationError("Fleet run presence includes an unexpected rank")
        observed_nodes.add(node_id)
        if (
            presence.get("recipe_revision_id") != revision_id
            or presence.get("alias") != alias
            or presence.get("expected_rank_count") != len(node_ids)
            or presence.get("run_state") != expected_run_state
            or presence.get("route_state") != expected_route_state
            or presence.get("healthy") is not expected_health
            or not isinstance(presence.get("rank"), int)
        ):
            raise QualificationError("Fleet run presence identity or state differs")
        present_ranks = presence.get("present_ranks")
        if expected_health and (
            presence.get("rank_state") != "running"
            or presence.get("rank_fresh") is not True
            or presence.get("group_state") != "healthy"
            or not isinstance(present_ranks, list)
            or set(present_ranks) != expected_ranks
        ):
            raise QualificationError(
                "Fleet lacks fresh healthy evidence from every rank"
            )
        rows.append({**dict(presence), "node_id": node_id})
    if observed_nodes != set(node_ids):
        raise QualificationError("Fleet run presence node set differs from assignment")
    return rows


def _rank_presence_bindings(
    raw_presences: object,
) -> dict[str, int]:
    if not isinstance(raw_presences, list):
        raise QualificationError("Fleet rank presence evidence is not a list")
    bindings: dict[str, int] = {}
    for raw_presence in raw_presences:
        presence = _object(raw_presence, "Fleet rank presence")
        node_id = presence.get("node_id")
        rank = presence.get("rank")
        if (
            not isinstance(node_id, str)
            or not node_id
            or node_id in bindings
            or type(rank) is not int
        ):
            raise QualificationError(
                "Fleet rank presence lacks an exact Spark/rank identity"
            )
        bindings[node_id] = rank
    return bindings


def _require_rank_presence_bindings(
    raw_presences: object,
    expected_node_to_rank: Mapping[str, int],
    *,
    label: str,
) -> None:
    if _rank_presence_bindings(raw_presences) != dict(expected_node_to_rank):
        raise QualificationError(f"{label} changed the exact canary Spark/rank mapping")


def _latest_payload(
    ledger: EvidenceLedger, campaign_id: str, key: str, event: str
) -> Mapping[str, object] | None:
    for record in reversed(ledger.recipe_records(campaign_id, key)):
        if record.get("event") == event:
            return _object(record.get("payload"), f"{event} payload")
    return None


def _make_campaign_id(
    manifest: CampaignManifest,
    fixtures: FixtureRegistry,
    library_root: Path,
    ledger_path: Path,
) -> tuple[str, str]:
    ledger_id = _ledger_identity(ledger_path)
    campaign_id = _digest(
        {
            "manifest_sha256": manifest.sha256,
            "authority_sha256": manifest.authority.sha256,
            "fixture_manifest_sha256": fixtures.manifest_sha256,
            "library_root": str(library_root.resolve()),
            "ledger_id": ledger_id,
        }
    )
    return campaign_id, ledger_id


def _ledger_identity(ledger_path: Path) -> str:
    # Fleet profile label values are limited to 63 characters; a SHA-256
    # prefix keeps the evidence-ledger path identity within that contract.
    return hashlib.sha256(str(ledger_path.resolve()).encode()).hexdigest()[:63]


def _resolve_ledger_path(path: Path, library_root: Path) -> Path:
    candidate = path if path.is_absolute() else Path.cwd() / path
    if candidate.is_symlink():
        raise QualificationError("evidence ledger path must not be a symbolic link")
    if candidate.exists():
        metadata = candidate.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise QualificationError("evidence ledger path must name a regular file")
    resolved = candidate.resolve(strict=False)
    if resolved.is_relative_to(library_root.resolve(strict=True)):
        raise QualificationError(
            "evidence ledger must be outside the recipe-controlled repository"
        )
    return resolved


def _completed_keys(ledger: EvidenceLedger, campaign_id: str) -> set[str]:
    return {
        str(record["recipe"])
        for record in ledger.records
        if record.get("plan_digest") == campaign_id
        and record.get("event") == "recipe.spark-accepted"
        and isinstance(record.get("recipe"), str)
    }


def _row_terminal_block_or_failure(
    ledger: EvidenceLedger, campaign_id: str, row: RecipeAuthorityRow
) -> str | None:
    if (
        _latest_payload(ledger, campaign_id, row.key, "recipe.spark-accepted")
        is not None
    ):
        return "accepted"
    records = ledger.recipe_records(campaign_id, row.key)
    latest_outcome = next(
        (
            str(record.get("event"))
            for record in reversed(records)
            if record.get("event")
            in {
                "recipe.blocked",
                "recipe.failed",
                "canary.failed",
                "canary.completed",
            }
        ),
        None,
    )
    # Canary failure is retryable at the same exact batch identity. Only an
    # explicit recipe-local disposition closes the lane as failed/blocked.
    return (
        latest_outcome
        if latest_outcome in {"recipe.blocked", "recipe.failed"}
        else None
    )


def _latest_batch_payload(
    ledger: EvidenceLedger, campaign_id: str, batch_id: str, event: str
) -> Mapping[str, object] | None:
    for record in reversed(ledger.records):
        if (
            record.get("plan_digest") == campaign_id
            and record.get("recipe") is None
            and record.get("event") == event
        ):
            payload = _object(record.get("payload"), f"{event} batch payload")
            if payload.get("batch_id") == batch_id:
                return payload
    return None


def _record_payload_matches(
    record: Mapping[str, object], expected: Mapping[str, object]
) -> bool:
    payload = record.get("payload")
    return isinstance(payload, Mapping) and all(
        payload.get(key) == value for key, value in expected.items()
    )


def _batch_release_recorded(
    ledger: EvidenceLedger, campaign_id: str, batch: CampaignBatch
) -> bool:
    payload = _latest_batch_payload(
        ledger, campaign_id, batch.batch_id, "batch.cleanup.completed"
    )
    return (
        payload is not None
        and payload.get("all_batch_runs_absent") is True
        and payload.get("all_batch_routes_absent") is True
        and payload.get("profile_assignments_empty") is True
    )


def _batch_execution_started(
    ledger: EvidenceLedger, campaign_id: str, batch: CampaignBatch
) -> bool:
    """Return whether this batch has a durable effect intent or physical evidence."""
    effect_events = {
        "profile.load.requested",
        "profile.load.submitted",
        "canary.completed",
        "canary.failed",
        "rank-loss.pending",
        "rank-loss.observed",
        "rank-recovery.smoke-completed",
        "lane_recovery.intent",
        "lane_recovery.transitioned",
        "lane_recovery.cleanup.intent",
        "lane_recovery.cleanup.completed",
        "dual_recovery.cleanup.intent",
        "dual_recovery.cleanup.completed",
    }
    return any(
        record.get("plan_digest") == campaign_id
        and record.get("event") in effect_events
        and _record_payload_matches(record, {"batch_id": batch.batch_id})
        for record in ledger.records
    )


def _current_batch(
    manifest: CampaignManifest,
    ledger: EvidenceLedger,
    campaign_id: str,
    requested_id: str | None,
) -> CampaignBatch | None:
    batches = manifest.authority.batches
    if requested_id is None:
        # Resume an in-flight batch before selecting new work. Otherwise, choose
        # the next actionable lane in authority order; provider-blocked rows do
        # not create fictitious execution barriers for independent batches.
        selected = next(
            (
                batch
                for batch in batches
                if _batch_execution_started(ledger, campaign_id, batch)
                and not _batch_release_recorded(ledger, campaign_id, batch)
            ),
            None,
        )
        if selected is None:
            rows_by_key = {row.key: row for row in manifest.authority.rows}
            selected = next(
                (
                    batch
                    for batch in batches
                    if any(
                        rows_by_key[item.recipe].disposition == "actionable"
                        and _latest_payload(
                            ledger,
                            campaign_id,
                            item.recipe,
                            "recipe.spark-accepted",
                        )
                        is None
                        and _row_terminal_block_or_failure(
                            ledger, campaign_id, rows_by_key[item.recipe]
                        )
                        is None
                        for item in batch.assignments
                    )
                ),
                None,
            )
    else:
        selected = next(
            (batch for batch in batches if batch.batch_id == requested_id), None
        )
        if selected is None:
            raise QualificationError(
                f"batch is not in the reviewed authority: {requested_id}"
            )
    if selected is None:
        return None
    for other in batches:
        if other.batch_id == selected.batch_id:
            continue
        if not _batch_execution_started(ledger, campaign_id, other):
            continue
        if not _batch_release_recorded(ledger, campaign_id, other):
            raise QualificationError(
                f"batch {other.batch_id} has started but has no whole-Fleet cleanup receipt"
            )
        for assignment in other.assignments:
            row = next(
                item
                for item in manifest.authority.rows
                if item.key == assignment.recipe
            )
            status = _row_terminal_block_or_failure(ledger, campaign_id, row)
            if status is None:
                raise QualificationError(
                    f"batch {other.batch_id} still has an unfinished lane; complete or record its recipe-local failure before continuing"
                )
    return selected


def _batch_evidence_nodes(
    ledger: EvidenceLedger, campaign_id: str, batch: CampaignBatch
) -> list[str]:
    result: list[str] = []
    for assignment in batch.assignments:
        latest_canary = _latest_payload(
            ledger, campaign_id, assignment.recipe, "canary.completed"
        )
        if latest_canary is not None:
            result.extend(
                _ordered_rank_nodes(
                    latest_canary.get("node_to_rank"), assignment.node_count
                )
            )
            continue
        request = _latest_payload(
            ledger, campaign_id, assignment.recipe, "profile.load.requested"
        )
        if request is None:
            continue
        raw_nodes = request.get("node_ids")
        if not isinstance(raw_nodes, list) or any(
            not isinstance(value, str) for value in raw_nodes
        ):
            raise QualificationError(
                "durable batch intent has invalid Spark identities"
            )
        if len(raw_nodes) != assignment.node_count or len(set(raw_nodes)) != len(
            raw_nodes
        ):
            raise QualificationError("durable batch intent has invalid lane membership")
        result.extend(raw_nodes)
    if len(result) != len(set(result)):
        raise QualificationError("durable batch lanes overlap in Spark identity")
    if any(_NODE_ID.fullmatch(value) is None for value in result):
        raise QualificationError("durable batch evidence has invalid Spark IDs")
    return sorted(result)


def _ordered_rank_nodes(raw_ranks: object, node_count: int) -> list[str]:
    if not isinstance(raw_ranks, Mapping) or len(raw_ranks) != node_count:
        raise QualificationError("canary evidence lacks exact Spark rank identities")
    pairs: list[tuple[str, int]] = []
    for raw_node_id, rank in raw_ranks.items():
        if (
            not isinstance(raw_node_id, str)
            or _NODE_ID.fullmatch(raw_node_id) is None
            or type(rank) is not int
        ):
            raise QualificationError("canary rank-to-Spark binding is malformed")
        pairs.append((raw_node_id, rank))
    if {rank for _node_id, rank in pairs} != set(range(node_count)):
        raise QualificationError("canary ranks are not contiguous for the exact group")
    return [node_id for node_id, _rank in sorted(pairs, key=lambda item: item[1])]


def _exact_nodes(
    client: Any,
    fleet: Mapping[str, object],
    row: RecipeAuthorityRow,
    supplied: Sequence[str],
) -> list[str]:
    if len(supplied) != row.node_count or len(set(supplied)) != len(supplied):
        raise QualificationError(
            f"{row.key} requires exactly {row.node_count} distinct --spark IDs"
        )
    if any(_NODE_ID.fullmatch(value) is None for value in supplied):
        raise QualificationError("--spark requires exact Controller Spark IDs")
    nodes = _nodes(fleet)
    if not set(supplied) <= set(nodes):
        raise QualificationError(
            "selected Spark is not in current Controller Fleet authority"
        )
    if any(not _online(nodes[node_id]) for node_id in supplied):
        raise QualificationError(
            "all selected Sparks must be online before recipe preview"
        )
    if any(_boot_id(nodes[node_id]) is None for node_id in supplied):
        raise QualificationError(
            "selected Sparks require live serialized boot-ID telemetry"
        )
    return sorted(supplied)


def _exact_batch_nodes(
    client: Any,
    fleet: Mapping[str, object],
    batch: CampaignBatch,
    supplied: Sequence[str],
    rows_by_key: Mapping[str, RecipeAuthorityRow],
) -> dict[str, tuple[str, ...]]:
    expected_count = sum(assignment.node_count for assignment in batch.assignments)
    if len(supplied) != expected_count or len(set(supplied)) != expected_count:
        raise QualificationError(
            f"batch {batch.batch_id} requires exactly {expected_count} distinct --spark IDs in lane order"
        )
    cursor = 0
    selected: dict[str, tuple[str, ...]] = {}
    for assignment in batch.assignments:
        end = cursor + assignment.node_count
        row = rows_by_key.get(assignment.recipe)
        if row is None:
            raise QualificationError(
                f"batch {batch.batch_id} references a missing authority row"
            )
        nodes = _exact_nodes(client, fleet, row, supplied[cursor:end])
        selected[assignment.recipe] = tuple(nodes)
        cursor = end
    flattened = [node_id for values in selected.values() for node_id in values]
    if len(flattened) != len(set(flattened)):
        raise QualificationError("batch assignments must use disjoint Spark IDs")
    return selected


def _operator_gate(
    args: argparse.Namespace, rows: Sequence[RecipeAuthorityRow]
) -> None:
    # Preview supplies the evidence needed for acceptance; only apply consumes it.
    if not args.apply:
        return
    row_keys = {row.key for row in rows}
    acknowledgments = set(args.accept_operator_gate)
    if acknowledgments - row_keys:
        raise QualificationError(
            "operator acceptance must name only exact recipes in this batch"
        )
    capacity_acknowledgments = set(args.accept_capacity_review)
    if capacity_acknowledgments - row_keys:
        raise QualificationError(
            "capacity review must name only exact recipes in this batch"
        )
    for row in rows:
        if row.operator_acceptance_required and row.key not in acknowledgments:
            raise QualificationError(
                f"{row.key} requires --accept-operator-gate {row.key}"
            )
        if not row.operator_acceptance_required and row.key in acknowledgments:
            raise QualificationError(
                f"{row.key} does not declare an operator-acceptance gate"
            )
        capacity_required = any(
            gate.get("kind") == "capacity-review" for gate in row.review_gates
        )
        if capacity_required and row.key not in capacity_acknowledgments:
            raise QualificationError(
                f"{row.key} requires --accept-capacity-review {row.key} after operator capacity review"
            )
        if not capacity_required and row.key in capacity_acknowledgments:
            raise QualificationError(
                f"{row.key} does not declare a capacity-review gate"
            )


def _prepare_batch_profile(
    *,
    client: Any,
    number: int,
    authority_id: str,
    ledger_id: str,
    lanes: Sequence[BatchLane],
) -> dict[str, object]:
    profile = _profile_view(client, number)
    _assert_profile_owner(profile, authority_id, ledger_id)
    if profile.get("installation_policy") != "keep-cached":
        raise QualificationError(
            "dedicated qualification profile must retain cached assets"
        )
    current = profile.get("assignments")
    if not isinstance(current, list):
        raise QualificationError("dedicated profile assignment list is invalid")
    expected = [
        _profile_assignment(lane.row, lane.node_ids, lane.alias) for lane in lanes
    ]
    if current:
        if not _profile_assignments_equal(profile, expected):
            raise QualificationError(
                "dedicated profile contains assignments outside this exact batch"
            )
    else:
        profile = _save_profile(
            client,
            profile,
            assignments=expected,
            authority_id=authority_id,
            ledger_id=ledger_id,
        )
    return profile


def _check_batch_preview(
    raw_preview: Mapping[str, object],
    *,
    profile: Mapping[str, object],
    fleet: Mapping[str, object],
    lanes: Sequence[BatchLane],
    replacements: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    try:
        preview = FleetProfilePreview.from_dict(raw_preview).to_dict()
    except (KeyError, TypeError, ValueError) as error:
        raise QualificationError(
            "Controller batch profile preview is invalid"
        ) from error
    if preview.get("allowed") is not True:
        reasons = preview.get("reasons")
        raise QualificationError(
            f"whole-Fleet batch profile preview is blocked: {reasons}"
        )
    expected_scope = sorted(_nodes(fleet))
    scope = _object(preview.get("scope"), "batch profile preview scope")
    if scope.get("node_ids") != expected_scope:
        raise QualificationError(
            "batch preview does not bind the complete current Fleet"
        )
    lane_nodes = [node_id for lane in lanes for node_id in lane.node_ids]
    if len(lane_nodes) != len(set(lane_nodes)):
        raise QualificationError("batch lane assignments overlap in Spark identity")
    if scope.get("idle_node_ids") != sorted(set(expected_scope) - set(lane_nodes)):
        raise QualificationError("batch preview does not leave unassigned Sparks idle")
    if preview.get("profile_id") != profile.get("id") or preview.get(
        "profile_digest"
    ) != profile.get("profile_digest"):
        raise QualificationError("batch preview is bound to another saved profile")
    summary = _object(preview.get("summary"), "batch preview summary")
    replacement_interruption: dict[str, object] | None = None
    if not replacements:
        _assert_batch_fleet_exclusive(fleet)
        if summary.get("stops") != 0:
            raise QualificationError(
                "batch profile preview contains an unacknowledged workload stop"
            )
    else:
        run_ids = [
            _string(item.get("run_id"), "acknowledged run ID") for item in replacements
        ]
        observed = _assert_batch_fleet_exclusive(fleet, replace_run_ids=run_ids)
        if observed != [
            dict(item)
            for item in sorted(replacements, key=lambda item: str(item.get("run_id")))
        ]:
            raise QualificationError(
                "acknowledged workloads changed during batch preview"
            )
        if (
            summary.get("stops") != len(replacements)
            or summary.get("starts") != len(lanes)
            or summary.get("uninstalls") != 0
        ):
            raise QualificationError(
                "batch replacement preview does not contain the exact stops and lane starts"
            )
        effects = _object(preview.get("effects"), "batch preview effects")
        raw_run_effects = effects.get("runs")
        if not isinstance(raw_run_effects, list) or len(raw_run_effects) != len(
            replacements
        ):
            raise QualificationError("batch replacement preview has extra run effects")
        effects_by_id: dict[str, Mapping[str, object]] = {}
        for raw_effect in raw_run_effects:
            run_effect = _object(raw_effect, "batch replacement run effect")
            run_id = _string(run_effect.get("run_id"), "batch replacement run ID")
            if run_id in effects_by_id:
                raise QualificationError(
                    "batch replacement preview repeats a run effect"
                )
            effects_by_id[run_id] = run_effect
        expected_replacements = {str(item["run_id"]): item for item in replacements}
        if set(effects_by_id) != set(expected_replacements):
            raise QualificationError(
                "batch replacement preview changed exact stopped runs"
            )
        for run_id, replacement in expected_replacements.items():
            run_effect = effects_by_id[run_id]
            if (
                run_effect.get("alias") != replacement.get("alias")
                or run_effect.get("node_ids") != replacement.get("member_node_ids")
                or run_effect.get("action") != "stop"
            ):
                raise QualificationError(
                    "batch replacement preview changed an exact acknowledged workload"
                )
        raw_steps = preview.get("steps")
        if not isinstance(raw_steps, list) or len(raw_steps) != 1:
            raise QualificationError(
                "batch replacement preview lacks one aggregate switch step"
            )
        step = _object(raw_steps[0], "batch replacement step")
        step_nodes = _string_array(step.get("node_ids"), "batch replacement node IDs")
        if step.get("kind") != "switch" or not set(step_nodes) <= set(expected_scope):
            raise QualificationError(
                "batch replacement switch scope differs from exact lane effects"
            )
        expected_switch_nodes = {
            node_id
            for item in replacements
            for node_id in _string_array(
                item.get("member_node_ids"), "acknowledged run member node IDs"
            )
        } | set(lane_nodes)
        if set(step_nodes) != expected_switch_nodes:
            raise QualificationError(
                "batch replacement switch scope differs from exact stopped and lane identities"
            )
        replacement_interruption = {
            "runs": [
                dict(item)
                for item in sorted(
                    replacements, key=lambda item: str(item.get("run_id"))
                )
            ],
            "switch_node_ids": step_nodes,
            "stop_count": len(replacements),
            "start_count": len(lanes),
            "profile_plan_digest": _string(
                preview.get("plan_digest"), "batch profile plan digest"
            ),
        }
    if summary.get("uninstalls") != 0:
        raise QualificationError("batch preview would uninstall cached assets")
    raw_assignments = preview.get("assignments")
    raw_preparations = preview.get("preparations")
    if (
        not isinstance(raw_assignments, list)
        or len(raw_assignments) != len(lanes)
        or not isinstance(raw_preparations, list)
        or len(raw_preparations) != len(lanes)
    ):
        raise QualificationError("batch preview does not contain every exact lane")
    assignments_by_nodes: dict[tuple[str, ...], Mapping[str, object]] = {}
    for raw_assignment in raw_assignments:
        assignment = _object(raw_assignment, "batch assignment preview")
        node_values = assignment.get("node_ids")
        if not isinstance(node_values, list) or any(
            not isinstance(node_id, str) for node_id in node_values
        ):
            raise QualificationError("batch assignment preview has invalid Spark IDs")
        key = tuple(sorted(node_values))
        if key in assignments_by_nodes:
            raise QualificationError("batch preview duplicates a lane assignment")
        assignments_by_nodes[key] = assignment
    exact_preparations: dict[str, object] = {}
    lane_revisions: dict[str, str] = {}
    for lane in lanes:
        key = tuple(sorted(lane.node_ids))
        assignment = assignments_by_nodes.get(key)
        if assignment is None or assignment.get("desired_state") != "running":
            raise QualificationError(
                f"{lane.row.key} preview changed its exact lane or desired state"
            )
        assignment_id = assignment.get("assignment_id")
        revision_id = assignment.get("recipe_revision_id")
        if not isinstance(assignment_id, str) or not assignment_id:
            raise QualificationError(
                f"{lane.row.key} preview lacks assignment identity"
            )
        expected_identity = lane.detail.get("identity")
        expected_recipe_id = (
            expected_identity.get("recipe_id")
            if isinstance(expected_identity, Mapping)
            else None
        )
        if not isinstance(revision_id, str) or not revision_id:
            raise QualificationError(f"{lane.row.key} preview lacks exact revision ID")
        if assignment.get("recipe_id") not in {None, expected_recipe_id}:
            raise QualificationError(f"{lane.row.key} preview resolved another recipe")
        matching_preparations = [
            _object(item, "batch preparation entry")
            for item in raw_preparations
            if _object(item, "batch preparation entry").get("assignment_id")
            == assignment_id
        ]
        if len(matching_preparations) != 1:
            raise QualificationError(
                f"{lane.row.key} lacks one assignment-bound preparation receipt"
            )
        preparation = _validate_preparations(
            {"preparations": matching_preparations}, lane.row, lane.node_ids
        )
        if preparation.get("recipe_revision_sha256") != lane.row.content_sha256:
            raise QualificationError(
                f"{lane.row.key} preparation changed its exact recipe content"
            )
        exact_preparations[lane.row.key] = preparation
        lane_revisions[lane.row.key] = revision_id
    if set(assignments_by_nodes) != {tuple(sorted(lane.node_ids)) for lane in lanes}:
        raise QualificationError("batch preview contains an unreviewed lane assignment")
    checked = {
        **preview,
        "exact_preparations": exact_preparations,
        "lane_revision_ids": lane_revisions,
    }
    if replacement_interruption is not None:
        checked["replacement_interruption"] = replacement_interruption
    return checked


def _batch_preview_digest(
    *,
    manifest: CampaignManifest,
    fixtures: FixtureRegistry,
    batch: CampaignBatch,
    lanes: Sequence[BatchLane],
    profile: Mapping[str, object],
    preview: Mapping[str, object],
    profile_number: int,
    failure_node_id: str | None,
) -> str:
    return _digest(
        {
            "schema_version": 2,
            "campaign_manifest_sha256": manifest.sha256,
            "authority_sha256": manifest.authority.sha256,
            "fixture_manifest_sha256": fixtures.manifest_sha256,
            "batch": dict(batch.raw),
            "lanes": [
                {
                    "recipe": lane.row.key,
                    "recipe_content_sha256": lane.row.content_sha256,
                    "package_sha256": lane.row.package.get("sha256"),
                    "lane": lane.assignment.lane,
                    "node_ids": list(lane.node_ids),
                    "alias": lane.alias,
                    "smoke_kind": lane.smoke_kind,
                    "smoke_preview": dict(lane.smoke_preview),
                }
                for lane in lanes
            ],
            "profile_number": profile_number,
            "failure_node_id": failure_node_id,
            "profile_id": profile.get("id"),
            "profile_digest": profile.get("profile_digest"),
            "profile_plan_digest": preview.get("plan_digest"),
            "exact_preparations": preview.get("exact_preparations"),
            "lane_revision_ids": preview.get("lane_revision_ids"),
            "replacement_interruption": preview.get("replacement_interruption"),
        }
    )


def _fresh_batch_preview(
    *,
    client: Any,
    manifest: CampaignManifest,
    fixtures: FixtureRegistry,
    batch: CampaignBatch,
    selected_nodes: Mapping[str, tuple[str, ...]],
    library_root: Path,
    profile_number: int,
    authority_id: str,
    ledger_id: str,
    campaign_id: str,
    ledger: EvidenceLedger,
    expected_fleet_node_ids: Sequence[str],
    replace_run_ids: Sequence[str],
    failure_node_id: str | None,
) -> tuple[list[BatchLane], dict[str, object], dict[str, object]]:
    lanes: list[BatchLane] = []
    for assignment in batch.assignments:
        row = next(
            (item for item in manifest.authority.rows if item.key == assignment.recipe),
            None,
        )
        if row is None:
            raise QualificationError(
                f"batch {batch.batch_id} references a missing authority row"
            )
        nodes = selected_nodes.get(row.key)
        if nodes is None or len(nodes) != assignment.node_count:
            raise QualificationError(f"{row.key} lacks its exact batch lane assignment")
        _kind, _definition = _fixture_bindings(row, fixtures)
        detail, definition = _validate_current_recipe(client, row)
        kind = _kind
        alias = (
            str(fixtures.service_recipes[row.key].alias)
            if kind == "openai-service"
            else f"q{row.sequence}"
        )
        if _ALIAS.fullmatch(alias) is None:
            raise QualificationError(f"{row.key} smoke alias is invalid")
        adapter: ArtifactJobSmokeAdapter | ServiceSmokeAdapter
        if kind == "artifact-job":
            adapter = ArtifactJobSmokeAdapter(fixtures)
            smoke_preview = adapter.preview(
                {"definition": definition},
                recipe_key=row.key,
                recipe_content_sha256=row.content_sha256,
            )
        else:
            adapter = ServiceSmokeAdapter(fixtures)
            smoke_preview = adapter.preview(
                {"definition": definition},
                alias,
                recipe_key=row.key,
                recipe_content_sha256=row.content_sha256,
            )
        if (
            smoke_preview.get("available") is not True
            or smoke_preview.get("fixture_manifest_sha256") != fixtures.manifest_sha256
        ):
            raise QualificationError(f"{row.key} reviewed smoke fixture is unavailable")
        lanes.append(
            BatchLane(assignment, row, nodes, alias, kind, detail, smoke_preview)
        )
    fleet = _typed_fleet(client)
    _require_locked_fleet_roster(fleet, expected_fleet_node_ids)
    if any(not _online(node) for node in _nodes(fleet).values()):
        raise QualificationError(
            "whole-Fleet batch preview requires every enrolled Spark online"
        )
    replacements = _assert_batch_fleet_exclusive(fleet, replace_run_ids=replace_run_ids)
    profile = _prepare_batch_profile(
        client=client,
        number=profile_number,
        authority_id=authority_id,
        ledger_id=ledger_id,
        lanes=lanes,
    )
    fleet = _typed_fleet(client)
    _require_locked_fleet_roster(fleet, expected_fleet_node_ids)
    confirmed_replacements = _assert_batch_fleet_exclusive(
        fleet, replace_run_ids=replace_run_ids
    )
    if confirmed_replacements != replacements:
        raise QualificationError(
            "the acknowledged run or whole-Fleet membership changed while preparing the batch"
        )
    if any(not _online(node) for node in _nodes(fleet).values()):
        raise QualificationError(
            "whole-Fleet batch preview requires every enrolled Spark online"
        )
    raw_preview = client.request("POST", f"/api/profile/{profile_number}/preview")
    preview_fleet = _typed_fleet(client)
    _require_locked_fleet_roster(preview_fleet, expected_fleet_node_ids)
    if any(not _online(node) for node in _nodes(preview_fleet).values()):
        raise QualificationError(
            "whole-Fleet batch preview requires every enrolled Spark online"
        )
    if (
        _assert_batch_fleet_exclusive(preview_fleet, replace_run_ids=replace_run_ids)
        != replacements
    ):
        raise QualificationError(
            "the acknowledged run or whole-Fleet membership changed during batch preview"
        )
    checked = _check_batch_preview(
        raw_preview,
        profile=profile,
        fleet=preview_fleet,
        lanes=lanes,
        replacements=replacements,
    )
    campaign_digest = _batch_preview_digest(
        manifest=manifest,
        fixtures=fixtures,
        batch=batch,
        lanes=lanes,
        profile=profile,
        preview=checked,
        profile_number=profile_number,
        failure_node_id=failure_node_id,
    )
    ledger.append(
        "batch.plan.generated",
        plan_digest=campaign_id,
        payload={
            "batch_id": batch.batch_id,
            "batch_sequence": batch.sequence,
            "batch": dict(batch.raw),
            "campaign_digest": campaign_digest,
            "failure_node_id": failure_node_id,
            "profile_number": profile_number,
            "profile_id": profile.get("id"),
            "profile_digest": profile.get("profile_digest"),
            "preview": checked,
            "lane_inputs": [
                {
                    "recipe": lane.row.key,
                    "lane": lane.assignment.lane,
                    "recipe_content_sha256": lane.row.content_sha256,
                    "package_sha256": lane.row.package.get("sha256"),
                    "node_ids": list(lane.node_ids),
                    "alias": lane.alias,
                    "smoke_preview": dict(lane.smoke_preview),
                }
                for lane in lanes
            ],
        },
    )
    for lane in lanes:
        revision = _object(
            checked.get("lane_revision_ids"), "batch recipe revision map"
        ).get(lane.row.key)
        identity = lane.detail.get("identity")
        ledger.append(
            "plan.generated",
            plan_digest=campaign_id,
            recipe=lane.row.key,
            payload={
                "batch_id": batch.batch_id,
                "lane_id": lane.assignment.lane,
                "campaign_digest": campaign_digest,
                "failure_node_id": failure_node_id,
                "sequence": lane.row.sequence,
                "authority_row": dict(lane.row.raw),
                "controller_recipe_identity": {
                    "recipe_id": identity.get("recipe_id")
                    if isinstance(identity, Mapping)
                    else None,
                    "recipe_revision_id": revision,
                    "content_sha256": lane.row.content_sha256,
                    "release_version": lane.row.recipe_version,
                },
                "profile_number": profile_number,
                "profile_id": profile.get("id"),
                "profile_digest": profile.get("profile_digest"),
                "preview": checked,
                "replacement_interruption": checked.get("replacement_interruption"),
                "smoke_preview": dict(lane.smoke_preview),
            },
        )
    return lanes, checked, {"campaign_digest": campaign_digest, "profile": profile}


def _lane_final_verifications(
    application: Mapping[str, object],
    lanes: Sequence[BatchLane],
    preview: Mapping[str, object],
) -> dict[str, Mapping[str, object]]:
    progress = _object(application.get("progress"), "profile application progress")
    intended = _object(progress.get("intended_profile"), "intended profile")
    raw_intended = intended.get("assignments")
    step_results = progress.get("step_results")
    if not isinstance(raw_intended, list) or not isinstance(step_results, Mapping):
        raise QualificationError(
            "batch application lacks immutable lane assignments or switch receipts"
        )
    by_assignment_id: dict[str, tuple[BatchLane, str, tuple[str, ...]]] = {}
    raw_assignments = preview.get("assignments")
    if not isinstance(raw_assignments, list):
        raise QualificationError("reviewed batch assignments are invalid")
    assignments = raw_assignments
    for lane in lanes:
        expected_nodes = tuple(sorted(lane.node_ids))
        assignment_preview = next(
            (
                _object(item, "reviewed assignment")
                for item in assignments
                if tuple(
                    sorted(
                        _string_array(
                            _object(item, "reviewed assignment").get("node_ids"),
                            "preview assignment node IDs",
                        )
                    )
                )
                == expected_nodes
            ),
            None,
        )
        if assignment_preview is None:
            raise QualificationError(
                f"{lane.row.key} has no reviewed profile assignment"
            )
        assignment_id = assignment_preview.get("assignment_id")
        revision_id = _object(
            preview.get("lane_revision_ids"), "reviewed lane revision IDs"
        ).get(lane.row.key)
        if (
            not isinstance(assignment_id, str)
            or not assignment_id
            or not isinstance(revision_id, str)
        ):
            raise QualificationError(
                f"{lane.row.key} reviewed assignment is incomplete"
            )
        by_assignment_id[assignment_id] = (lane, revision_id, expected_nodes)
    intended_assignments: dict[str, tuple[str, tuple[str, ...]]] = {}
    for raw_assignment in raw_intended:
        assignment = _object(raw_assignment, "intended profile assignment")
        assignment_id = _string(assignment.get("id"), "intended assignment ID")
        raw_nodes = assignment.get("nodes")
        if not isinstance(raw_nodes, list):
            raise QualificationError("intended profile lane lacks exact Spark nodes")
        node_ids = tuple(
            sorted(
                _string(
                    _object(item, "intended profile Spark").get("node_id"), "Spark ID"
                )
                for item in raw_nodes
            )
        )
        revision_id = _string(
            assignment.get("recipe_revision_id"), "intended recipe revision ID"
        )
        if assignment_id in intended_assignments:
            raise QualificationError("intended profile repeats a lane assignment ID")
        intended_assignments[assignment_id] = (revision_id, node_ids)
    if set(intended_assignments) != set(by_assignment_id):
        raise QualificationError(
            "accepted batch application changed its exact reviewed assignment set"
        )
    for assignment_id, (_lane, revision_id, nodes) in by_assignment_id.items():
        if intended_assignments[assignment_id] != (revision_id, nodes):
            raise QualificationError(
                "accepted batch application changed a lane revision or Spark group"
            )

    results: dict[str, Mapping[str, object]] = {}
    for raw_step in step_results.values():
        step = _object(raw_step, "profile switch step receipt")
        switch_result = step.get("result")
        if not isinstance(switch_result, Mapping):
            continue
        assignment_ids = switch_result.get("assignment_ids")
        children = switch_result.get("children")
        if not isinstance(assignment_ids, list) or not isinstance(children, list):
            continue
        if len(assignment_ids) != len(by_assignment_id):
            raise QualificationError("batch switch result changed its assignment map")
        for raw_child in children:
            child = _object(raw_child, "batch switch child")
            kind = child.get("kind")
            if kind == "stop":
                # Whole-Fleet profile reconciliation may stop an explicitly
                # reviewed prior workload before starting the batch lanes.
                # Stop evidence is consumed separately by the exclusive
                # recovery transition; it is not an assignment-indexed run.
                continue
            if kind != "run":
                raise QualificationError("batch switch returned an unknown child kind")
            child_result = _object(child.get("result"), "batch switch child result")
            run_switch = _object(child_result.get("run_switch"), "lane switch receipt")
            item_index = run_switch.get("item_index")
            if type(item_index) is not int or not 0 <= item_index < len(assignment_ids):
                raise QualificationError(
                    "batch switch receipt has an invalid item index"
                )
            assignment_id = assignment_ids[item_index]
            if (
                not isinstance(assignment_id, str)
                or assignment_id not in by_assignment_id
            ):
                raise QualificationError("batch switch receipt has an unknown lane ID")
            lane, revision_id, node_ids = by_assignment_id[assignment_id]
            if run_switch.get("profile_application_id") != application.get("id"):
                raise QualificationError(
                    "lane switch receipt belongs to another application"
                )

            def find_final_receipts(value: object) -> list[Mapping[str, object]]:
                found: list[Mapping[str, object]] = []
                if isinstance(value, Mapping):
                    if value.get("phase") == "final_verify" and isinstance(
                        value.get("run_id"), str
                    ):
                        found.append(value)
                    for child_value in value.values():
                        found.extend(find_final_receipts(child_value))
                elif isinstance(value, list):
                    for child_value in value:
                        found.extend(find_final_receipts(child_value))
                return found

            final_receipts = find_final_receipts(run_switch)
            if len(final_receipts) != 1:
                raise QualificationError(
                    f"{lane.row.key} switch receipt lacks one final verification"
                )
            final = final_receipts[0]
            ranks = final.get("ranks")
            if (
                final.get("final_verified") is not True
                or final.get("healthy") is not True
                or final.get("state") != "running"
                or final.get("route_state") != "published"
                or not isinstance(ranks, list)
                or len(ranks) != lane.row.node_count
            ):
                raise QualificationError(
                    f"{lane.row.key} lacks a complete healthy serving receipt"
                )
            observed_nodes = tuple(
                sorted(
                    _string(
                        _object(rank, "final rank receipt").get("node_id"),
                        "rank Spark ID",
                    )
                    for rank in ranks
                )
            )
            if observed_nodes != node_ids or lane.row.key in results:
                raise QualificationError(
                    "batch final verification duplicated or changed an exact lane"
                )
            if any(
                type(_object(rank, "final rank receipt").get("rank")) is not int
                for rank in ranks
            ):
                raise QualificationError(
                    f"{lane.row.key} final rank values are invalid"
                )
            results[lane.row.key] = {
                **dict(final),
                "recipe_revision_id": revision_id,
                "assignment_id": assignment_id,
            }
    if set(results) != {lane.row.key for lane in lanes}:
        raise QualificationError(
            "batch profile application lacks a final receipt per lane"
        )
    run_ids = [str(value.get("run_id")) for value in results.values()]
    if len(run_ids) != len(set(run_ids)):
        raise QualificationError("paired lanes resolved to a duplicate run identity")
    return results


def _load_batch_and_smoke(
    *,
    client: Any,
    batch: CampaignBatch,
    lanes: Sequence[BatchLane],
    fixtures: FixtureRegistry,
    preview: Mapping[str, object],
    profile_number: int,
    campaign_id: str,
    campaign_digest: str,
    ledger: EvidenceLedger,
    options: CampaignManifest,
    failure_node_id: str | None,
    allow_submit: bool = True,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> tuple[Mapping[str, object], dict[str, Mapping[str, object]], dict[str, str]]:
    request_key = _request_key(campaign_id, batch.batch_id, "load")
    profile_id = _string(preview.get("profile_id"), "reviewed batch profile ID")
    profile_digest = _string(
        preview.get("profile_digest"), "reviewed batch profile digest"
    )
    plan_digest = _string(preview.get("plan_digest"), "reviewed batch plan digest")
    intents: dict[str, Mapping[str, object]] = {}
    submitted: dict[str, Mapping[str, object] | None] = {}
    prior_canaries: dict[str, Mapping[str, object] | None] = {}
    prior_failures: dict[str, Mapping[str, object] | None] = {}
    for lane in lanes:
        existing = _latest_payload(
            ledger, campaign_id, lane.row.key, "profile.load.submitted"
        )
        intent = _latest_payload(
            ledger, campaign_id, lane.row.key, "profile.load.requested"
        )
        if existing is not None and intent is None:
            raise QualificationError(
                f"{lane.row.key} application receipt lacks its durable batch intent"
            )
        expected_intent = {
            "batch_id": batch.batch_id,
            "lane_id": lane.assignment.lane,
            "request_key": request_key,
            "profile_id": profile_id,
            "profile_digest": profile_digest,
            "plan_digest": plan_digest,
            "campaign_digest": campaign_digest,
            "alias": lane.alias,
            "smoke_kind": lane.smoke_kind,
            "node_ids": sorted(lane.node_ids),
            "failure_node_id": failure_node_id,
            "operator_gate_accepted": lane.row.operator_acceptance_required,
            "capacity_review_accepted": any(
                gate.get("kind") == "capacity-review" for gate in lane.row.review_gates
            ),
            "preview": dict(preview),
            "smoke_preview": dict(lane.smoke_preview),
        }
        if intent is not None and any(
            intent.get(key) != value for key, value in expected_intent.items()
        ):
            raise QualificationError(
                f"{lane.row.key} durable load intent differs from its exact batch preview"
            )
        if intent is None:
            intent = expected_intent
            ledger.append(
                "profile.load.requested",
                plan_digest=campaign_id,
                recipe=lane.row.key,
                payload=intent,
            )
        if existing is not None and (
            existing.get("request_key") != request_key
            or existing.get("profile_digest") != profile_digest
            or existing.get("plan_digest") != plan_digest
        ):
            raise QualificationError(
                f"{lane.row.key} submitted profile load differs from its durable intent"
            )
        canary_record = _latest_batch_canary_outcome(
            ledger,
            campaign_id=campaign_id,
            batch_id=batch.batch_id,
            recipe=lane.row.key,
            lane_id=lane.assignment.lane,
        )
        canary = (
            _object(canary_record.get("payload"), "lane canary receipt")
            if canary_record is not None
            and canary_record.get("event") == "canary.completed"
            else None
        )
        failure = (
            _object(canary_record.get("payload"), "lane canary failure")
            if canary_record is not None
            and canary_record.get("event") == "canary.failed"
            else None
        )
        terminal = canary if canary is not None else failure
        if terminal is not None and (
            terminal.get("batch_id") != batch.batch_id
            or terminal.get("lane_id") != lane.assignment.lane
            or terminal.get("recipe_content_sha256") != lane.row.content_sha256
            or terminal.get("package_sha256") != lane.row.package.get("sha256")
            or terminal.get("alias") != lane.alias
            or (
                terminal.get("node_to_rank") is not None
                and set(
                    _ordered_rank_nodes(
                        terminal.get("node_to_rank"), lane.row.node_count
                    )
                )
                != set(lane.node_ids)
            )
        ):
            raise QualificationError(
                f"{lane.row.key} terminal lane receipt belongs to another exact lane"
            )
        prior_canaries[lane.row.key] = canary
        prior_failures[lane.row.key] = failure
        intents[lane.row.key] = intent
        submitted[lane.row.key] = existing
    application_ids = {
        str(item.get("application_id"))
        for item in submitted.values()
        if isinstance(item, Mapping) and isinstance(item.get("application_id"), str)
    }
    if len(application_ids) > 1:
        raise QualificationError(
            "batch lanes disagree about their accepted application"
        )
    if application_ids:
        accepted: Mapping[str, object] = {"id": next(iter(application_ids))}
    elif not allow_submit:
        accepted = (
            _lookup_load_request(
                client,
                profile_number,
                request_key,
                plan_digest=plan_digest,
                profile_id=profile_id,
                profile_digest=profile_digest,
            )
            or {}
        )
        if not accepted:
            raise QualificationError(
                "the accepted batch load is not visible yet; observe again or explicitly reapply its reviewed batch"
            )
    else:
        accepted = _submit_load(
            client,
            profile_number,
            request_key,
            plan_digest=plan_digest,
            profile_id=profile_id,
            profile_digest=profile_digest,
        )
    application_id = _string(accepted.get("id"), "accepted batch application ID")
    for lane in lanes:
        if submitted[lane.row.key] is None:
            ledger.append(
                "profile.load.submitted",
                plan_digest=campaign_id,
                recipe=lane.row.key,
                payload={
                    "batch_id": batch.batch_id,
                    "request_key": request_key,
                    "application_id": application_id,
                    "profile_digest": profile_digest,
                    "plan_digest": plan_digest,
                },
            )
    application = _await_application(
        client,
        accepted,
        ledger=ledger,
        campaign_id=campaign_id,
        key=f"batch:{batch.batch_id}",
        timeout=options.operation_timeout_seconds,
        interval=options.poll_interval_seconds,
        clock=clock,
        sleeper=sleeper,
    )
    if (
        application.get("profile_id") != profile_id
        or application.get("profile_digest") != profile_digest
        or application.get("plan_digest")
        != _application_plan_digest(plan_digest, request_key)
        or application.get("request_key") != request_key
    ):
        raise QualificationError(
            "accepted batch application changed its exact identity"
        )
    lane_finals = _lane_final_verifications(application, lanes, preview)
    errors: dict[str, str] = {
        key: str(value.get("error", "lane canary previously failed"))
        for key, value in prior_failures.items()
        if value is not None
    }
    receipts: dict[str, Mapping[str, object]] = {
        key: canary for key, canary in prior_canaries.items() if canary is not None
    }
    prepared: dict[str, dict[str, object]] = {}
    lanes_to_smoke = [
        lane
        for lane in lanes
        if prior_canaries[lane.row.key] is None and prior_failures[lane.row.key] is None
    ]
    # Resolve every physical lane before starting model smoke. A stale Fleet,
    # assignment, or route is a batch-wide safety failure, never a local
    # fixture outcome that would let the other lane proceed.
    fleet = _typed_fleet(client) if lanes_to_smoke else {}
    if lanes_to_smoke:
        scope = _object(preview.get("scope"), "batch preview scope")
        raw_roster = scope.get("node_ids")
        if not isinstance(raw_roster, list) or any(
            not isinstance(node_id, str) for node_id in raw_roster
        ):
            raise QualificationError("batch preview has no exact Fleet roster")
        _require_locked_fleet_roster(fleet, raw_roster)
    for lane in lanes_to_smoke:
        final = lane_finals[lane.row.key]
        run_id = _string(final.get("run_id"), "lane run ID")
        ranks = final.get("ranks")
        if not isinstance(ranks, list):
            raise QualificationError(f"{lane.row.key} final ranks are invalid")
        node_to_rank: dict[str, int] = {}
        for value in ranks:
            rank = _object(value, "final rank receipt")
            node_id = _string(rank.get("node_id"), "rank Spark ID")
            rank_id = rank.get("rank")
            if type(rank_id) is not int or node_id in node_to_rank:
                raise QualificationError(
                    f"{lane.row.key} final rank mapping is invalid"
                )
            node_to_rank[node_id] = rank_id
        if set(node_to_rank) != set(lane.node_ids) or set(node_to_rank.values()) != set(
            range(lane.row.node_count)
        ):
            raise QualificationError(
                f"{lane.row.key} final ranks changed its Spark group"
            )
        presences = _check_serving_fleet(
            fleet,
            run_id=run_id,
            revision_id=_string(final.get("recipe_revision_id"), "lane revision ID"),
            alias=lane.alias,
            node_ids=lane.node_ids,
            expected_run_state="running",
            expected_route_state="published",
            expected_health=True,
        )
        _require_rank_presence_bindings(
            presences, node_to_rank, label="paired serving canary"
        )
        prepared[lane.row.key] = {
            "batch_id": batch.batch_id,
            "lane_id": lane.assignment.lane,
            "application": application,
            "application_id": application_id,
            "application_request_key": request_key,
            "run_id": run_id,
            "alias": lane.alias,
            "smoke_kind": lane.smoke_kind,
            "review_acknowledgements": {
                "operator_acceptance": lane.row.operator_acceptance_required,
                "capacity_review": any(
                    gate.get("kind") == "capacity-review"
                    for gate in lane.row.review_gates
                ),
            },
            "recipe_revision_id": final.get("recipe_revision_id"),
            "recipe_content_sha256": lane.row.content_sha256,
            "package_sha256": lane.row.package.get("sha256"),
            "node_ids": list(lane.node_ids),
            "node_to_rank": node_to_rank,
            "assigned_node_id": lane.node_ids[0],
            "assigned_rank": node_to_rank[lane.node_ids[0]],
            "fleet_rank_presence": [dict(item) for item in presences],
            "exact_preparations": _object(
                preview.get("exact_preparations"), "batch exact preparations"
            ).get(lane.row.key),
        }

    def smoke_lane(lane: BatchLane) -> Mapping[str, object]:
        if lane.smoke_kind == "artifact-job":
            smoke = ArtifactJobSmokeAdapter(fixtures).run(
                client,
                str(prepared[lane.row.key]["run_id"]),
                lane.smoke_preview,
                ledger=ledger,
                plan_digest=campaign_id,
                recipe_key=lane.row.key,
                timeout_seconds=options.operation_timeout_seconds,
                poll_interval_seconds=options.poll_interval_seconds,
                clock=clock,
                sleeper=sleeper,
            )
        else:
            smoke = ServiceSmokeAdapter(fixtures).run(
                client, lane.alias, lane.smoke_preview
            )
        return dict(smoke)

    with ThreadPoolExecutor(max_workers=max(1, len(lanes_to_smoke))) as executor:
        futures = {executor.submit(smoke_lane, lane): lane for lane in lanes_to_smoke}
        for future in as_completed(futures):
            lane = futures[future]
            try:
                smoke = future.result()
            except (ControlNotFound, QualificationError) as error:
                message = str(error).strip()[:1024] or type(error).__name__
                failure = {
                    **prepared[lane.row.key],
                    "error": message,
                    "smoke_status": "failed",
                }
                ledger.append(
                    "canary.failed",
                    plan_digest=campaign_id,
                    recipe=lane.row.key,
                    payload=failure,
                )
                errors[lane.row.key] = message
                continue
            receipt = {**prepared[lane.row.key], "smoke": dict(smoke)}
            ledger.append(
                "canary.completed",
                plan_digest=campaign_id,
                recipe=lane.row.key,
                payload=receipt,
            )
            receipts[lane.row.key] = receipt
    return application, receipts, errors


def _durable_batch_lanes(
    *,
    client: Any,
    batch: CampaignBatch,
    manifest: CampaignManifest,
    fixtures: FixtureRegistry,
    ledger: EvidenceLedger,
    campaign_id: str,
) -> tuple[list[BatchLane], Mapping[str, object], Mapping[str, object]]:
    """Rebuild the reviewed lane projection from current authority + durable intent."""
    plan = _latest_batch_payload(
        ledger, campaign_id, batch.batch_id, "batch.plan.generated"
    )
    if plan is None:
        raise QualificationError(f"{batch.batch_id} has no durable reviewed batch plan")
    raw_lanes = plan.get("lane_inputs")
    preview = plan.get("preview")
    if not isinstance(raw_lanes, list) or not isinstance(preview, Mapping):
        raise QualificationError("durable batch plan lacks its exact lanes or preview")
    if plan.get("batch") != dict(batch.raw):
        raise QualificationError("durable batch plan differs from current authority")
    if len(raw_lanes) != len(batch.assignments):
        raise QualificationError("durable batch plan has an incomplete lane list")
    rows_by_key = {row.key: row for row in manifest.authority.rows}
    lanes: list[BatchLane] = []
    used_nodes: set[str] = set()
    for assignment, raw in zip(batch.assignments, raw_lanes, strict=True):
        item = _object(raw, "durable batch lane")
        row = rows_by_key.get(assignment.recipe)
        if row is None:
            raise QualificationError("durable batch lane lost its authority row")
        if item.get("recipe") != row.key or item.get("lane") != assignment.lane:
            raise QualificationError("durable batch lane order differs from authority")
        nodes = _string_array(item.get("node_ids"), "durable lane Spark IDs")
        if (
            len(nodes) != assignment.node_count
            or nodes != sorted(set(nodes))
            or used_nodes & set(nodes)
            or any(_NODE_ID.fullmatch(node) is None for node in nodes)
        ):
            raise QualificationError("durable batch lane has invalid Spark identities")
        used_nodes.update(nodes)
        kind, _definition = _fixture_bindings(row, fixtures)
        alias = (
            str(fixtures.service_recipes[row.key].alias)
            if kind == "openai-service"
            else f"q{row.sequence}"
        )
        if item.get("alias") != alias or item.get("smoke_kind") != kind:
            raise QualificationError("durable batch lane changed its reviewed fixture")
        detail, definition = _validate_current_recipe(client, row)
        adapter: ArtifactJobSmokeAdapter | ServiceSmokeAdapter
        if kind == "artifact-job":
            adapter = ArtifactJobSmokeAdapter(fixtures)
            smoke_preview = adapter.preview(
                {"definition": definition},
                recipe_key=row.key,
                recipe_content_sha256=row.content_sha256,
            )
        else:
            adapter = ServiceSmokeAdapter(fixtures)
            smoke_preview = adapter.preview(
                {"definition": definition},
                alias,
                recipe_key=row.key,
                recipe_content_sha256=row.content_sha256,
            )
        if (
            item.get("smoke_preview") != smoke_preview
            or smoke_preview.get("available") is not True
            or smoke_preview.get("fixture_manifest_sha256") != fixtures.manifest_sha256
        ):
            raise QualificationError("durable batch fixture changed after review")
        lanes.append(
            BatchLane(
                assignment,
                row,
                tuple(nodes),
                alias,
                kind,
                detail,
                smoke_preview,
            )
        )
    return lanes, dict(preview), plan


def _batch_lane_outcomes(
    *,
    campaign_id: str,
    batch: CampaignBatch,
    lanes: Sequence[BatchLane],
    ledger: EvidenceLedger,
    receipts: Mapping[str, Mapping[str, object]],
    errors: Mapping[str, str],
) -> list[dict[str, object]]:
    outcomes: list[dict[str, object]] = []
    for lane in lanes:
        recipe = lane.row.key
        receipt = receipts.get(recipe)
        if receipt is not None:
            outcomes.append(
                {
                    "recipe": recipe,
                    "lane": lane.assignment.lane,
                    "status": "canary-completed",
                    "receipt": dict(receipt),
                }
            )
            continue
        message = errors.get(recipe)
        failure_record = next(
            (
                record
                for record in reversed(ledger.recipe_records(campaign_id, recipe))
                if record.get("event") == "canary.failed"
                and _object(record.get("payload"), "canary failure").get("batch_id")
                == batch.batch_id
            ),
            None,
        )
        failure: dict[str, object] = {
            "recipe": recipe,
            "lane": lane.assignment.lane,
            "status": "pending",
        }
        if failure_record is not None:
            failure_payload = _object(failure_record.get("payload"), "canary failure")
            failure["status"] = "canary-failed"
            failure["error"] = message or failure_payload.get("error")
            failure["record_sha256"] = failure_record.get("record_sha256")
            failure["run_id"] = failure_payload.get("run_id")
        outcomes.append(failure)
    return outcomes


def _latest_batch_canary_outcome(
    ledger: EvidenceLedger,
    *,
    campaign_id: str,
    batch_id: str,
    recipe: str,
    lane_id: int,
) -> Mapping[str, object] | None:
    for record in reversed(ledger.recipe_records(campaign_id, recipe)):
        payload = record.get("payload")
        if (
            record.get("event") in {"canary.completed", "canary.failed"}
            and isinstance(payload, Mapping)
            and payload.get("batch_id") == batch_id
            and payload.get("lane_id") == lane_id
        ):
            return record
    return None


def _batch_canary_references(
    *,
    batch: CampaignBatch,
    lanes: Sequence[BatchLane],
    campaign_id: str,
    ledger: EvidenceLedger,
) -> tuple[CanaryReference, ...]:
    references: list[CanaryReference] = []
    for lane in lanes:
        record = next(
            (
                item
                for item in reversed(ledger.recipe_records(campaign_id, lane.row.key))
                if item.get("event") in {"canary.completed", "canary.failed"}
                and _record_payload_matches(
                    item,
                    {"batch_id": batch.batch_id, "lane_id": lane.assignment.lane},
                )
            ),
            None,
        )
        if record is None:
            raise QualificationError(
                f"{lane.row.key} has no durable terminal canary outcome for {batch.batch_id}"
            )
        payload = _object(record.get("payload"), "batch canary outcome")
        node_ids = _string_array(payload.get("node_ids"), "canary Spark identities")
        if len(node_ids) != 1 or node_ids[0] != lane.node_ids[0]:
            raise QualificationError(
                "single-Spark lane canary changed its authority assignment"
            )
        assigned_node_id = _string(
            payload.get("assigned_node_id"), "canary assigned Spark ID"
        )
        assigned_rank = _integer(
            payload.get("assigned_rank"), "canary assigned rank", 0, 1
        )
        node_to_rank_raw = payload.get("node_to_rank")
        node_to_rank: dict[str, int] | None = None
        if node_to_rank_raw is not None:
            raw_ranks = _object(node_to_rank_raw, "canary lane rank map")
            if set(raw_ranks) != {assigned_node_id}:
                raise QualificationError(
                    "single-Spark canary rank map has another node"
                )
            node_to_rank = {
                assigned_node_id: _integer(
                    raw_ranks[assigned_node_id], "canary lane rank", 0, 0
                )
            }
            if node_to_rank[assigned_node_id] != assigned_rank:
                raise QualificationError("canary assignment rank changed its rank map")
        event = _string(record.get("event"), "canary outcome event")
        recipe_revision_id = payload.get("recipe_revision_id")
        run_id = payload.get("run_id")
        alias = payload.get("alias")
        if event == "canary.completed" and (
            not isinstance(recipe_revision_id, str)
            or not isinstance(run_id, str)
            or not isinstance(alias, str)
            or node_to_rank is None
        ):
            raise QualificationError("passing canary lacks exact run and rank identity")
        references.append(
            CanaryReference(
                lane_id=lane.assignment.lane,
                record_sha256=_string(
                    record.get("record_sha256"), "canary record digest"
                ),
                recipe_key=lane.row.key,
                recipe_content_sha256=lane.row.content_sha256,
                package_sha256=_string(
                    lane.row.package.get("sha256"), "recipe package digest"
                ),
                assigned_node_id=assigned_node_id,
                assigned_rank=assigned_rank,
                run_id=run_id if isinstance(run_id, str) else None,
                recipe_revision_id=(
                    recipe_revision_id if isinstance(recipe_revision_id, str) else None
                ),
                alias=alias if isinstance(alias, str) else None,
                node_to_rank=node_to_rank,
                outcome_event=event,
            )
        )
    return tuple(references)


def _lane_smoke_case_ids(lane: BatchLane, fixtures: FixtureRegistry) -> tuple[str, ...]:
    if lane.smoke_kind == "artifact-job":
        interface = lane.smoke_preview.get("interface")
        recipe, blocker = fixtures.resolve(
            lane.row.key, lane.row.content_sha256, str(interface)
        )
        if recipe is None:
            detail = blocker.get("detail") if blocker is not None else "unknown blocker"
            raise QualificationError(
                f"{lane.row.key} recovery fixture is unavailable: {detail}"
            )
        return tuple(case.case_id for case in recipe.all_cases)
    raw_cases = lane.smoke_preview.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise QualificationError(
            f"{lane.row.key} reviewed service fixture has no cases"
        )
    return tuple(
        _string(_object(item, "service smoke case").get("id"), "service smoke case ID")
        for item in raw_cases
    )


def _lane_recovery_target(
    *,
    batch: CampaignBatch,
    lanes: Sequence[BatchLane],
    lane_number: int,
    campaign_id: str,
    ledger: EvidenceLedger,
    profile_number: int,
    fleet_node_ids: Sequence[str],
    fixtures: FixtureRegistry,
    allow_failed_own_lane: bool = False,
) -> tuple[LaneRecoveryTarget, BatchLane, tuple[CanaryReference, ...]]:
    references = _batch_canary_references(
        batch=batch, lanes=lanes, campaign_id=campaign_id, ledger=ledger
    )
    lane = next((item for item in lanes if item.assignment.lane == lane_number), None)
    if lane is None or lane.row.node_count != 1:
        raise QualificationError(
            "host-restart recovery requires an exact single-Spark lane"
        )
    own = next(item for item in references if item.lane_id == lane_number)
    if own.outcome_event != "canary.completed" and not (
        allow_failed_own_lane and own.outcome_event == "canary.failed"
    ):
        raise QualificationError(
            f"lane {lane_number} has no passing canary; its failure remains lane-local"
        )
    if own.outcome_event == "canary.completed" and (
        own.node_to_rank is None
        or own.run_id is None
        or own.recipe_revision_id is None
        or own.alias is None
    ):
        raise QualificationError("passing lane canary lacks durable run identity")
    if (
        allow_failed_own_lane
        and own.outcome_event == "canary.failed"
        and (own.run_id is None or own.node_to_rank is None)
    ):
        raise QualificationError(
            "failed lane has no applied run identity; reconcile the partial application before cleanup"
        )
    batch_plan = _latest_batch_payload(
        ledger, campaign_id, batch.batch_id, "batch.plan.generated"
    )
    if batch_plan is None:
        raise QualificationError("batch recovery has no durable paired profile plan")
    original_preview = _object(batch_plan.get("preview"), "original batch preview")
    partner = [item for item in references if item.lane_id != lane_number]
    target = LaneRecoveryTarget(
        campaign_id=campaign_id,
        batch_id=batch.batch_id,
        lane_id=lane_number,
        recipe_key=lane.row.key,
        recipe_content_sha256=lane.row.content_sha256,
        package_sha256=_string(lane.row.package.get("sha256"), "recipe package digest"),
        original_run_id=own.run_id,
        recipe_revision_id=own.recipe_revision_id
        or _string(
            _object(lane.detail.get("identity"), "lane recipe identity").get(
                "recipe_revision_id"
            ),
            "lane recipe revision ID",
        ),
        alias=own.alias or lane.alias,
        node_id=own.assigned_node_id,
        node_to_rank=dict(
            own.node_to_rank or {own.assigned_node_id: own.assigned_rank}
        ),
        smoke_case_ids=_lane_smoke_case_ids(lane, fixtures),
        fleet_node_ids=tuple(sorted(fleet_node_ids)),
        partner_run_ids=tuple(
            sorted(item.run_id for item in partner if item.run_id is not None)
        ),
        partner_aliases=tuple(
            sorted(item.alias for item in partner if item.alias is not None)
        ),
        canaries=references,
        profile_number=profile_number,
        profile_id=_string(batch_plan.get("profile_id"), "original paired profile ID"),
        profile_digest=_string(
            batch_plan.get("profile_digest"), "original paired profile digest"
        ),
        plan_digest=_string(
            original_preview.get("plan_digest"), "original paired plan digest"
        ),
        allow_reactivation=True,
    )
    return target, lane, references


def _recovery_coverage_receipts(
    *,
    lane: BatchLane,
    batch: CampaignBatch,
    campaign_id: str,
    ledger: EvidenceLedger,
    client: Any,
    manifest: CampaignManifest,
) -> list[dict[str, object]]:
    from .fleet_qualification_coverage import (
        build_recovery_coverage_receipt,
        validate_recovery_coverage_receipt,
    )

    canary_record = next(
        (
            record
            for record in reversed(ledger.recipe_records(campaign_id, lane.row.key))
            if record.get("event") == "canary.completed"
            and _record_payload_matches(
                record,
                {"batch_id": batch.batch_id, "lane_id": lane.assignment.lane},
            )
        ),
        None,
    )
    if canary_record is None:
        raise QualificationError("recovery coverage requires the exact passing canary")
    canary = _object(canary_record.get("payload"), "recovery coverage canary")
    exact_preparation = _object(
        canary.get("exact_preparations"), "exact accepted runtime preparation"
    )
    if (
        exact_preparation.get("recipe_revision_sha256") != lane.row.content_sha256
        or exact_preparation.get("image_digest") is None
        or exact_preparation.get("architecture") != "linux-arm64"
        or exact_preparation.get("target_node_ids") != sorted(lane.node_ids)
    ):
        raise QualificationError(
            "recovery coverage lost its accepted runtime preparation"
        )
    definitions = {
        str(_object(item, "recovery coverage definition").get("coverage_id")): item
        for item in manifest.authority.recovery_coverage
    }
    deployment_by_node = _deployment_provenance_by_node(client, lane.node_ids)
    receipt_schema = _campaign_contract_validator(
        "recovery-coverage-receipt-v1.schema.json"
    ).schema
    if not isinstance(receipt_schema, Mapping):
        raise QualificationError(
            "canonical recovery coverage receipt schema is invalid"
        )
    # The receipt builder revalidates event hashes and sequence continuity, so
    # give it the full verified hash chain before it scopes exact lane events.
    ledger_records = list(ledger.records)
    result: list[dict[str, object]] = []
    for raw_reference in lane.row.recovery_coverage_refs:
        reference = dict(raw_reference)
        coverage_id = _string(reference.get("coverage_id"), "recovery coverage ID")
        definition_raw = definitions.get(coverage_id)
        if definition_raw is None:
            raise QualificationError(
                "recovery coverage reference has no exact definition"
            )
        definition = _object(definition_raw, "recovery coverage definition")
        if (
            reference.get("failure_mode") != definition.get("failure_mode")
            or definition.get("shared") is not False
            or reference.get("role") != "dedicated"
        ):
            raise QualificationError(
                "recovery coverage receipt requires one dedicated exact definition"
            )
        envelope = build_recovery_coverage_receipt(
            coverage_definition=definition,
            coverage_reference=reference,
            authority_row=lane.row.raw,
            campaign_id=campaign_id,
            batch_id=batch.batch_id,
            lane_id=lane.assignment.lane,
            ledger_records=ledger_records,
            exact_preparation=exact_preparation,
            deployment_provenance_by_node=deployment_by_node,
            receipt_schema=receipt_schema,
        )
        validate_recovery_coverage_receipt(
            envelope,
            receipt_schema=receipt_schema,
            coverage_definition=definition,
            coverage_reference=reference,
            authority_row=lane.row.raw,
        )
        result.append(dict(envelope))
    if not result:
        raise QualificationError(
            "recipe acceptance requires typed recovery coverage receipts"
        )
    return result


def _dual_recovery_target(
    *,
    batch: CampaignBatch,
    lane: BatchLane,
    campaign_id: str,
    ledger: EvidenceLedger,
    profile_number: int,
    fleet_node_ids: Sequence[str],
    fixtures: FixtureRegistry,
) -> DualRecoveryTarget:
    if batch.mode != "exclusive-dual" or lane.row.node_count != 2:
        raise QualificationError("dual recovery requires one exact exclusive-dual lane")
    canary_record = _latest_batch_canary_outcome(
        ledger,
        campaign_id=campaign_id,
        batch_id=batch.batch_id,
        recipe=lane.row.key,
        lane_id=lane.assignment.lane,
    )
    if canary_record is None or canary_record.get("event") != "canary.completed":
        raise QualificationError("dual recovery requires its exact passing lane canary")
    canary = _object(canary_record.get("payload"), "dual lane canary")
    raw_node_to_rank = _object(canary.get("node_to_rank"), "dual canary rank map")
    node_to_rank: dict[str, int] = {}
    for raw_node_id, raw_rank in raw_node_to_rank.items():
        node_id = _string(raw_node_id, "dual canary Spark ID")
        if type(raw_rank) is not int or node_id in node_to_rank:
            raise QualificationError("dual canary rank map is invalid")
        node_to_rank[node_id] = raw_rank
    if set(node_to_rank) != set(lane.node_ids) or set(node_to_rank.values()) != {0, 1}:
        raise QualificationError(
            "dual canary rank map changed its exact selected Sparks"
        )
    rank_ordered_nodes = tuple(
        node_id
        for node_id, _rank in sorted(node_to_rank.items(), key=lambda item: item[1])
    )
    raw_canary_nodes = canary.get("node_ids")
    if (
        canary.get("batch_id") != batch.batch_id
        or canary.get("lane_id") != lane.assignment.lane
        or canary.get("recipe_content_sha256") != lane.row.content_sha256
        or canary.get("package_sha256") != lane.row.package.get("sha256")
        or canary.get("alias") != lane.alias
        or not isinstance(raw_canary_nodes, list)
        or set(raw_canary_nodes) != set(lane.node_ids)
        or set(rank_ordered_nodes) != set(lane.node_ids)
    ):
        raise QualificationError(
            "dual canary changed its exact authority lane identity"
        )
    if len(rank_ordered_nodes) != 2:
        raise QualificationError("dual canary must bind exactly two Sparks")
    dual_node_ids = (rank_ordered_nodes[0], rank_ordered_nodes[1])
    run_id = _string(canary.get("run_id"), "dual canary run ID")
    revision_id = _string(canary.get("recipe_revision_id"), "dual canary revision ID")
    plan = _latest_batch_payload(
        ledger, campaign_id, batch.batch_id, "batch.plan.generated"
    )
    if plan is None:
        raise QualificationError("dual recovery has no durable original batch plan")
    preview = _object(plan.get("preview"), "durable dual batch preview")
    failure_node_id = _string(plan.get("failure_node_id"), "dual failure Spark ID")
    request = _latest_payload(
        ledger, campaign_id, lane.row.key, "profile.load.requested"
    )
    if (
        request is None
        or request.get("batch_id") != batch.batch_id
        or request.get("lane_id") != lane.assignment.lane
        or request.get("profile_id") != plan.get("profile_id")
        or request.get("profile_digest") != plan.get("profile_digest")
        or request.get("plan_digest") != preview.get("plan_digest")
        or request.get("failure_node_id") != failure_node_id
    ):
        raise QualificationError(
            "dual canary is detached from its exact accepted batch request"
        )
    smoke = _object(canary.get("smoke"), "dual canary smoke receipt")
    raw_smoke_cases = smoke.get("cases")
    smoke_cases = (
        [
            _string(
                _object(item, "dual canary smoke case").get("case_id"),
                "dual smoke case ID",
            )
            for item in raw_smoke_cases
        ]
        if isinstance(raw_smoke_cases, list)
        else []
    )
    required_cases = _lane_smoke_case_ids(lane, fixtures)
    if (
        smoke.get("endpoint_alias") != lane.alias
        or smoke.get("recipe_content_sha256") != lane.row.content_sha256
        or smoke_cases != list(required_cases)
    ):
        raise QualificationError(
            "dual canary smoke receipt is not the exact reviewed fixture"
        )
    return DualRecoveryTarget(
        campaign_id=campaign_id,
        batch_id=batch.batch_id,
        lane_id=lane.assignment.lane,
        recipe_key=lane.row.key,
        recipe_content_sha256=lane.row.content_sha256,
        package_sha256=_string(lane.row.package.get("sha256"), "dual package SHA-256"),
        canary_record_sha256=_string(
            canary_record.get("record_sha256"), "dual canary event digest"
        ),
        run_id=run_id,
        recipe_revision_id=revision_id,
        alias=lane.alias,
        node_ids=dual_node_ids,
        node_to_rank=node_to_rank,
        failure_node_id=failure_node_id,
        smoke_case_ids=required_cases,
        fleet_node_ids=tuple(sorted(fleet_node_ids)),
        profile_number=profile_number,
        profile_id=_string(plan.get("profile_id"), "dual batch profile ID"),
        profile_digest=_string(plan.get("profile_digest"), "dual batch profile digest"),
        plan_digest=_string(preview.get("plan_digest"), "dual batch plan digest"),
    )


def _failed_dual_canary_target(
    *,
    batch: CampaignBatch,
    lane: BatchLane,
    campaign_id: str,
    ledger: EvidenceLedger,
    profile_number: int,
    fleet_node_ids: Sequence[str],
) -> FailedDualCanaryTarget:
    """Bind failed fixture cleanup to its one accepted dual canary application."""
    if batch.mode != "exclusive-dual" or lane.row.node_count != 2:
        raise QualificationError(
            "failed dual cleanup requires an exclusive two-Spark lane"
        )
    record = _latest_batch_canary_outcome(
        ledger,
        campaign_id=campaign_id,
        batch_id=batch.batch_id,
        recipe=lane.row.key,
        lane_id=lane.assignment.lane,
    )
    if record is None or record.get("event") != "canary.failed":
        raise QualificationError(
            "failed dual cleanup requires its exact canary.failed record"
        )
    payload = _object(record.get("payload"), "failed dual canary")
    if (
        payload.get("batch_id") != batch.batch_id
        or payload.get("lane_id") != lane.assignment.lane
        or payload.get("recipe_content_sha256") != lane.row.content_sha256
        or payload.get("package_sha256") != lane.row.package.get("sha256")
        or payload.get("alias") != lane.alias
        or payload.get("smoke_kind") != lane.smoke_kind
        or payload.get("smoke_status") != "failed"
    ):
        raise QualificationError("failed dual canary belongs to another authority lane")
    raw_nodes = _string_array(payload.get("node_ids"), "failed canary Spark IDs")
    if raw_nodes != tuple(sorted(lane.node_ids)) or len(raw_nodes) != 2:
        raise QualificationError(
            "failed dual canary changed its exact authority Sparks"
        )
    raw_ranks = _object(payload.get("node_to_rank"), "failed canary rank map")
    node_to_rank: dict[str, int] = {}
    for node_id, raw_rank in raw_ranks.items():
        exact_node_id = _string(node_id, "failed canary rank Spark ID")
        if type(raw_rank) is not int or exact_node_id in node_to_rank:
            raise QualificationError("failed dual canary rank map is invalid")
        node_to_rank[exact_node_id] = raw_rank
    if set(node_to_rank) != set(raw_nodes) or set(node_to_rank.values()) != {0, 1}:
        raise QualificationError("failed dual canary does not bind both exact ranks")
    assigned_node_id = _string(
        payload.get("assigned_node_id"), "failed canary assigned Spark ID"
    )
    assigned_rank = _integer(
        payload.get("assigned_rank"), "failed canary assigned rank", 0, 1
    )
    if node_to_rank.get(assigned_node_id) != assigned_rank:
        raise QualificationError(
            "failed canary selected node/rank identity is inconsistent"
        )
    # Current canary failures are emitted only after an accepted run has passed
    # exact healthy serving verification. Missing identity therefore requires
    # original-application reconciliation, never an invented no-effects proof.
    raw_run_id = payload.get("run_id")
    run_id = raw_run_id if isinstance(raw_run_id, str) and raw_run_id else None
    recipe_revision_id = _string(
        payload.get("recipe_revision_id"), "failed canary recipe revision ID"
    )
    application = _object(payload.get("application"), "failed canary application")
    application_id = _string(
        payload.get("application_id"), "failed canary application ID"
    )
    request_key = _string(
        payload.get("application_request_key"), "failed canary application request key"
    )
    try:
        if str(uuid.UUID(request_key)) != request_key:
            raise ValueError("non-canonical UUID")
    except (TypeError, ValueError) as error:
        raise QualificationError(
            "failed canary application request key is invalid"
        ) from error
    batch_plan = _latest_batch_payload(
        ledger, campaign_id, batch.batch_id, "batch.plan.generated"
    )
    if batch_plan is None:
        raise QualificationError("failed dual cleanup has no exact original batch plan")
    if (
        application.get("id") != application_id
        or application.get("request_key") != request_key
        or application.get("state") != "succeeded"
        or application.get("profile_id") != batch_plan.get("profile_id")
        or application.get("profile_digest") != batch_plan.get("profile_digest")
    ):
        raise QualificationError(
            "failed canary is detached from its succeeded batch application"
        )
    preview = _object(batch_plan.get("preview"), "failed dual original preview")
    plan_digest = _string(
        preview.get("plan_digest"), "failed dual original load plan digest"
    )
    profile_id = _string(
        batch_plan.get("profile_id"), "failed dual original profile ID"
    )
    profile_digest = _string(
        batch_plan.get("profile_digest"), "failed dual original profile digest"
    )
    if (
        application.get("plan_digest")
        != _application_plan_digest(plan_digest, request_key)
        or profile_number != batch_plan.get("profile_number")
        or application.get("updated_at") is None
    ):
        raise QualificationError(
            "failed canary application changed its accepted plan identity"
        )
    serving = payload.get("fleet_rank_presence")
    if not isinstance(serving, list) or len(serving) != 2:
        raise QualificationError(
            "failed dual canary lacks exact healthy two-rank serving evidence"
        )
    _require_rank_presence_bindings(
        serving,
        node_to_rank,
        label="failed dual canary",
    )
    roster = tuple(sorted(fleet_node_ids))
    if not set(raw_nodes).issubset(roster):
        raise QualificationError(
            "failed canary Sparks are outside the current Fleet roster"
        )
    return FailedDualCanaryTarget(
        campaign_id=campaign_id,
        batch_id=batch.batch_id,
        lane_id=lane.assignment.lane,
        recipe_key=lane.row.key,
        recipe_content_sha256=lane.row.content_sha256,
        package_sha256=_string(
            lane.row.package.get("sha256"), "failed canary package digest"
        ),
        canary_record_sha256=_string(
            record.get("record_sha256"), "failed canary record digest"
        ),
        application_id=application_id,
        application_request_key=request_key,
        application_updated_at=_string(
            application.get("updated_at"), "failed canary application time"
        ),
        run_id=run_id,
        recipe_revision_id=recipe_revision_id,
        alias=lane.alias,
        node_ids=(raw_nodes[0], raw_nodes[1]),
        node_to_rank=node_to_rank,
        assigned_node_id=assigned_node_id,
        assigned_rank=assigned_rank,
        fleet_node_ids=roster,
        profile_number=profile_number,
        profile_id=profile_id,
        profile_digest=profile_digest,
        plan_digest=plan_digest,
    )


def _dual_recovery_callbacks(
    *,
    client: Any,
    target: DualRecoveryTarget,
    lane: BatchLane,
    fixtures: FixtureRegistry,
) -> dict[str, Callable[..., Any]]:
    def observe_fleet() -> Mapping[str, object]:
        return _typed_fleet(client)

    def endpoint_exists(alias: str) -> bool:
        return _endpoint_exists(client, alias)

    def verify_serving(request: Mapping[str, object]) -> Mapping[str, object]:
        snapshot = _object(request.get("fleet_snapshot"), "dual serving Fleet snapshot")
        try:
            fleet = FleetSnapshot.from_dict(snapshot).to_dict()
        except (KeyError, TypeError, ValueError) as error:
            raise QualificationError(
                "dual serving Fleet snapshot is invalid"
            ) from error
        presences = _check_serving_fleet(
            fleet,
            run_id=target.run_id,
            revision_id=target.recipe_revision_id,
            alias=target.alias,
            node_ids=list(target.node_ids),
            expected_run_state="running",
            expected_route_state="published",
            expected_health=True,
        )
        _require_rank_presence_bindings(
            presences, target.node_to_rank, label="dual recovered serving"
        )
        endpoint = client.request(
            "GET", f"/api/endpoints/{urllib.parse.quote(target.alias, safe='')}"
        )
        if not isinstance(endpoint.get("api_base"), str):
            raise QualificationError("dual recovered service route has no API base")
        return {
            "state": "succeeded",
            "run_id": target.run_id,
            "recipe_key": target.recipe_key,
            "recipe_content_sha256": target.recipe_content_sha256,
            "package_sha256": target.package_sha256,
            "recipe_revision_id": target.recipe_revision_id,
            "alias": target.alias,
            "node_to_rank": dict(target.node_to_rank),
            "rank_presence": presences,
            "endpoint": dict(endpoint),
        }

    def run_fixture_smoke(request: Mapping[str, object]) -> Mapping[str, object]:
        node_id = _string(request.get("node_id"), "dual smoke Spark ID")
        rank = _integer(request.get("rank"), "dual smoke rank", 0, 1)
        if (
            request.get("run_id") != target.run_id
            or request.get("alias") != target.alias
            or target.node_to_rank.get(node_id) != rank
            or _string_array(request.get("case_ids"), "dual smoke cases")
            != target.smoke_case_ids
        ):
            raise QualificationError(
                "dual smoke request changed its exact rank or fixtures"
            )
        smoke = ServiceSmokeAdapter(fixtures).run(
            client, target.alias, lane.smoke_preview
        )
        cases_raw = smoke.get("cases")
        if not isinstance(cases_raw, list):
            raise QualificationError("dual service smoke lacks per-case receipts")
        cases = [
            {
                "case_id": _string(
                    _object(item, "dual service smoke case").get("case_id"),
                    "dual smoke case ID",
                ),
                "passed": True,
            }
            for item in cases_raw
        ]
        if [item["case_id"] for item in cases] != list(target.smoke_case_ids):
            raise QualificationError(
                "dual service smoke changed its reviewed fixture order"
            )
        fixture_receipt = {
            **dict(smoke),
            "endpoint_alias": target.alias,
            "recipe_content_sha256": target.recipe_content_sha256,
            "cases": cases,
        }
        smoke_identity = {
            "run_id": target.run_id,
            "alias": target.alias,
            "node_id": node_id,
            "rank": rank,
            "case_ids": list(target.smoke_case_ids),
            "fixture_receipt": fixture_receipt,
        }
        return {
            "passed": True,
            **smoke_identity,
            "record_sha256": _digest(smoke_identity),
        }

    return {
        "observe_fleet": observe_fleet,
        "endpoint_exists": endpoint_exists,
        "verify_serving": verify_serving,
        "run_fixture_smoke": run_fixture_smoke,
    }


def _dual_cleanup_callbacks(
    *,
    client: Any,
    target: DualRecoveryTarget,
    lane: BatchLane,
    authority_id: str,
    ledger_id: str,
    campaign_id: str,
    ledger: EvidenceLedger,
    options: CampaignManifest,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> dict[str, Callable[..., Any]]:
    context: dict[str, object] = {}

    def prepare_cleanup(request: Mapping[str, object]) -> Mapping[str, object]:
        request_key = _string(request.get("request_key"), "dual cleanup request key")
        if (
            request.get("cleanup_mode") != "dual-lane"
            or request.get("terminal_event") != "rank-recovery.smoke-completed"
            or request.get("active_run_id") != target.run_id
            or request.get("stop_run_ids") != [target.run_id]
            or request.get("stop_aliases") != [target.alias]
            or request.get("fleet_node_ids") != sorted(target.fleet_node_ids)
        ):
            raise QualificationError(
                "dual cleanup intent changed its exact run or lane"
            )
        fleet = _typed_fleet(client)
        roster = sorted(_nodes(fleet))
        if roster != list(target.fleet_node_ids):
            raise QualificationError(
                "dual cleanup preview changed the locked Fleet roster"
            )
        runs = _all_loaded_runs(fleet)
        if runs != {target.run_id}:
            raise QualificationError(
                "dual cleanup Fleet must contain only the reviewed dual run"
            )
        _check_serving_fleet(
            fleet,
            run_id=target.run_id,
            revision_id=target.recipe_revision_id,
            alias=target.alias,
            node_ids=list(target.node_ids),
            expected_run_state="running",
            expected_route_state="published",
            expected_health=True,
        )
        profile = _profile_view(client, target.profile_number)
        _assert_profile_owner(profile, authority_id, ledger_id)
        exact_assignment = _profile_assignment(lane.row, lane.node_ids, lane.alias)
        if profile.get("installation_policy") != "keep-cached" or not (
            _profile_assignments_equal(profile, [exact_assignment])
            or _profile_assignments_equal(profile, [])
        ):
            raise QualificationError(
                "dual cleanup profile changed outside its exact campaign"
            )
        if not _profile_assignments_equal(profile, []):
            profile = _save_profile(
                client,
                profile,
                assignments=[],
                authority_id=authority_id,
                ledger_id=ledger_id,
            )
        after_save = _typed_fleet(client)
        if sorted(_nodes(after_save)) != roster or _all_loaded_runs(after_save) != runs:
            raise QualificationError(
                "dual workload changed while saving cleanup profile"
            )
        raw_preview = client.request(
            "POST", f"/api/profile/{target.profile_number}/preview"
        )
        preview_fleet = _typed_fleet(client)
        if (
            sorted(_nodes(preview_fleet)) != roster
            or _all_loaded_runs(preview_fleet) != runs
        ):
            raise QualificationError(
                "dual workload changed while reviewing cleanup profile"
            )
        try:
            preview = FleetProfilePreview.from_dict(raw_preview).to_dict()
        except (KeyError, TypeError, ValueError) as error:
            raise QualificationError(
                "Controller dual cleanup preview is invalid"
            ) from error
        if preview.get("allowed") is not True:
            raise QualificationError(
                f"dual cleanup profile is blocked: {preview.get('reasons')}"
            )
        scope = _object(preview.get("scope"), "dual cleanup preview scope")
        summary = _object(preview.get("summary"), "dual cleanup preview summary")
        effects = _object(preview.get("effects"), "dual cleanup preview effects")
        raw_runs = effects.get("runs")
        if (
            scope.get("node_ids") != roster
            or scope.get("idle_node_ids") != roster
            or summary.get("stops") != 1
            or summary.get("starts") != 0
            or summary.get("placements") != 0
            or summary.get("builds") != 0
            or summary.get("distributions") != 0
            or summary.get("installs") != 0
            or summary.get("uninstalls") != 0
            or not isinstance(raw_runs, list)
            or len(raw_runs) != 1
        ):
            raise QualificationError("dual cleanup preview contains unreviewed effects")
        effect = _object(raw_runs[0], "dual cleanup run effect")
        if (
            effect.get("run_id") != target.run_id
            or effect.get("action") != "stop"
            or effect.get("alias") != target.alias
            or effect.get("node_ids") != sorted(target.node_ids)
        ):
            raise QualificationError(
                "dual cleanup preview changed the exact two-rank stop"
            )
        steps = preview.get("steps")
        if not isinstance(steps, list) or len(steps) != 1:
            raise QualificationError(
                "dual cleanup requires one whole-Fleet switch plan"
            )
        step = _object(steps[0], "dual cleanup switch step")
        if step.get("kind") != "switch" or set(
            _string_array(step.get("node_ids"), "dual cleanup switch nodes")
        ) != set(target.node_ids):
            raise QualificationError(
                "dual cleanup switch exceeded its exact two-rank scope"
            )
        receipt = {
            "request_key": request_key,
            "reviewed": True,
            "profile_number": target.profile_number,
            "profile_id": _string(profile.get("id"), "dual cleanup profile ID"),
            "profile_digest": _string(
                profile.get("profile_digest"), "dual cleanup profile digest"
            ),
            "plan_digest": _string(
                preview.get("plan_digest"), "dual cleanup plan digest"
            ),
            "cleanup_mode": "dual-lane",
            "terminal_event": "rank-recovery.smoke-completed",
            "terminal_record_sha256": request.get("terminal_record_sha256"),
            "active_run_id": target.run_id,
            "stop_run_ids": [target.run_id],
            "stop_aliases": [target.alias],
            "fleet_node_ids": roster,
        }
        context.clear()
        context.update(
            {
                "receipt": receipt,
                "preview": preview,
                "profile": profile,
                "pre_fleet": preview_fleet,
            }
        )
        return receipt

    def _application_receipt(
        request: Mapping[str, object], *, resume: bool
    ) -> Mapping[str, object]:
        resume_reviewed = not resume and request.get("resume_reviewed") is True
        if resume:
            receipt = _object(
                request.get("prior_receipt"), "prior dual cleanup receipt"
            )
            request_key = _string(
                request.get("request_key"), "dual cleanup request key"
            )
            review_digest = _string(
                request.get("review_digest"), "dual cleanup review digest"
            )
            profile_number = _integer(
                request.get("profile_number"), "dual profile number", 1, 2**31 - 1
            )
            profile_id = _string(request.get("profile_id"), "dual profile ID")
            profile_digest = _string(
                request.get("profile_digest"), "dual profile digest"
            )
            plan_digest = _string(request.get("plan_digest"), "dual plan digest")
            expected_application_id = _string(
                request.get("application_id"), "dual application ID"
            )
            expected_updated_at = _string(
                request.get("application_updated_at"), "dual application update time"
            )
        else:
            receipt = (
                _object(request.get("review"), "durable dual cleanup review")
                if resume_reviewed
                else _object(context.get("receipt"), "prepared dual cleanup review")
            )
            preview = (
                {}
                if resume_reviewed
                else _object(context.get("preview"), "prepared dual cleanup preview")
            )
            profile = (
                {
                    "id": request.get("profile_id"),
                    "profile_digest": request.get("profile_digest"),
                }
                if resume_reviewed
                else _object(context.get("profile"), "prepared dual cleanup profile")
            )
            review_digest = (
                _string(request.get("review_digest"), "dual cleanup review digest")
                if resume_reviewed
                else _digest(receipt)
            )
            request_key = _string(
                request.get("request_key"), "dual cleanup request key"
            )
            profile_number = _integer(
                request.get("profile_number", target.profile_number),
                "dual profile number",
                1,
                2**31 - 1,
            )
            profile_id = _string(profile.get("id"), "dual profile ID")
            profile_digest = _string(
                profile.get("profile_digest"), "dual profile digest"
            )
            plan_digest = _string(
                request.get("plan_digest")
                if resume_reviewed
                else preview.get("plan_digest"),
                "dual plan digest",
            )
            expected_application_id = ""
            expected_updated_at = ""
            if (
                request.get("review_digest") != review_digest
                or request.get("review") != dict(receipt)
                or request.get("request_key") != receipt.get("request_key")
            ):
                raise QualificationError(
                    "dual cleanup apply differs from its exact review"
                )
        accepted = _lookup_load_request(
            client,
            profile_number,
            request_key,
            plan_digest=plan_digest,
            profile_id=profile_id,
            profile_digest=profile_digest,
        )
        if accepted is None:
            if resume:
                raise QualificationError("original dual cleanup request is absent")
            profile = _profile_view(client, profile_number)
            _assert_profile_owner(profile, authority_id, ledger_id)
            if (
                profile.get("id") != profile_id
                or profile.get("profile_digest") != profile_digest
                or not _profile_assignments_equal(profile, [])
            ):
                raise QualificationError(
                    "dual cleanup profile changed after exact review"
                )
            expected_live = set(
                _string_array(request.get("stop_run_ids"), "reviewed dual stop run IDs")
            )
            if _all_loaded_runs(_typed_fleet(client)) != expected_live:
                raise QualificationError(
                    "dual run changed before reviewed cleanup apply"
                )
            accepted = _submit_load(
                client,
                profile_number,
                request_key,
                plan_digest=plan_digest,
                profile_id=profile_id,
                profile_digest=profile_digest,
            )
        if resume and accepted.get("id") != expected_application_id:
            raise QualificationError(
                "original dual cleanup request maps to another application"
            )
        application = _await_application(
            client,
            accepted,
            ledger=ledger,
            campaign_id=campaign_id,
            key=f"dual-cleanup:{target.batch_id}:{target.lane_id}",
            timeout=options.operation_timeout_seconds,
            interval=options.poll_interval_seconds,
            clock=clock,
            sleeper=sleeper,
        )
        if (
            application.get("state") != "succeeded"
            or application.get("request_key") != request_key
            or application.get("profile_id") != profile_id
            or application.get("profile_digest") != profile_digest
            or application.get("plan_digest")
            != _application_plan_digest(plan_digest, request_key)
        ):
            raise QualificationError(
                "dual cleanup application changed its reviewed identity"
            )
        application_id = _string(application.get("id"), "dual cleanup application ID")
        updated_at = _string(
            application.get("updated_at"), "dual cleanup application update time"
        )
        if resume and (
            application_id != expected_application_id
            or updated_at != expected_updated_at
        ):
            raise QualificationError(
                "reconciled dual cleanup changed its original application"
            )
        stops = _profile_stop_receipts(application)
        if [item.get("run_id") for item in stops] != [target.run_id]:
            raise QualificationError(
                "dual cleanup lacks its exact terminal run-stop receipt"
            )
        post_fleet = _typed_fleet(client)
        if _all_loaded_runs(post_fleet):
            raise QualificationError("dual cleanup Fleet still contains a loaded run")
        if any(
            _object(node.get("reservations"), "Fleet node reservations").get(name) != 0
            for node in _nodes(post_fleet).values()
            for name in (
                "unified_memory_bytes",
                "host_memory_bytes",
                "gpu_memory_bytes",
                "port_count",
            )
        ):
            raise QualificationError(
                "dual cleanup Fleet still has active runtime reservations"
            )
        if _endpoint_exists(client, target.alias):
            raise QualificationError("dual cleanup left the exact endpoint published")
        return {
            "request_key": request_key,
            "review_digest": review_digest,
            "application_state": "succeeded",
            "application_id": application_id,
            "profile_number": profile_number,
            "profile_id": profile_id,
            "profile_digest": profile_digest,
            "plan_digest": plan_digest,
            "application_updated_at": updated_at,
            "stop_receipts": stops,
            "fleet_snapshot": post_fleet,
        }

    def cleanup_to_idle(request: Mapping[str, object]) -> Mapping[str, object]:
        return _application_receipt(request, resume=False)

    def reconcile_cleanup(request: Mapping[str, object]) -> Mapping[str, object]:
        if request.get("resume_completed") is not True:
            raise QualificationError(
                "dual cleanup reconciliation lacks its resume marker"
            )
        return _application_receipt(request, resume=True)

    return {
        "prepare_cleanup": prepare_cleanup,
        "cleanup_to_idle": cleanup_to_idle,
        "reconcile_cleanup": reconcile_cleanup,
    }


def _failed_dual_cleanup_callbacks(
    *,
    client: Any,
    target: FailedDualCanaryTarget,
    lane: BatchLane,
    authority_id: str,
    ledger_id: str,
    campaign_id: str,
    ledger: EvidenceLedger,
    options: CampaignManifest,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> dict[str, Callable[..., Any]]:
    """Review and reconcile only the exact run from a failed dual canary."""
    context: dict[str, object] = {}
    # The failed-canary helper owns the actionable missing-run refusal and
    # invokes no callbacks until it has rejected that malformed target.
    target_run_id = target.run_id if isinstance(target.run_id, str) else ""

    def require_target_request(request: Mapping[str, object]) -> None:
        resume_completed = request.get("resume_completed") is True
        if (
            request.get("campaign_id") != campaign_id
            or request.get("batch_id") != target.batch_id
            or request.get("lane_id") != target.lane_id
            or request.get("recipe_key") != target.recipe_key
            or request.get("recipe_content_sha256") != target.recipe_content_sha256
            or request.get("package_sha256") != target.package_sha256
            or request.get("canary_record_sha256") != target.canary_record_sha256
            or request.get("source_application_id") != target.application_id
            or request.get("source_application_request_key")
            != target.application_request_key
            or (
                not resume_completed
                and request.get("source_application_updated_at")
                != target.application_updated_at
            )
            or request.get("active_run_id") != target_run_id
            or request.get("stop_run_ids") != [target_run_id]
            or request.get("stop_aliases") != [target.alias]
            or request.get("node_ids") != list(target.node_ids)
            or request.get("node_to_rank") != dict(target.node_to_rank)
            or request.get("assigned_node_id") != target.assigned_node_id
            or request.get("assigned_rank") != target.assigned_rank
            or request.get("fleet_node_ids") != sorted(target.fleet_node_ids)
            or (
                not resume_completed
                and request.get("cleanup_mode") != "failed-dual-lane"
            )
            or request.get("terminal_event") != "canary.failed"
            or request.get("terminal_record_sha256") != target.canary_record_sha256
        ):
            raise QualificationError(
                "failed dual cleanup changed its exact canary, application or rank scope"
            )

    def reconcile_source_application() -> Mapping[str, object]:
        accepted = _lookup_load_request(
            client,
            target.profile_number,
            target.application_request_key,
            plan_digest=target.plan_digest,
            profile_id=target.profile_id,
            profile_digest=target.profile_digest,
        )
        if accepted is None or accepted.get("id") != target.application_id:
            raise QualificationError(
                "reconcile the original Controller profile application before failed-canary cleanup"
            )
        application = _await_application(
            client,
            accepted,
            ledger=ledger,
            campaign_id=campaign_id,
            key=f"failed-canary-source:{target.batch_id}:{target.lane_id}",
            timeout=options.operation_timeout_seconds,
            interval=options.poll_interval_seconds,
            clock=clock,
            sleeper=sleeper,
        )
        if (
            application.get("id") != target.application_id
            or application.get("request_key") != target.application_request_key
            or application.get("state") != "succeeded"
            or application.get("profile_id") != target.profile_id
            or application.get("profile_digest") != target.profile_digest
            or application.get("plan_digest")
            != _application_plan_digest(
                target.plan_digest, target.application_request_key
            )
            or application.get("updated_at") != target.application_updated_at
        ):
            raise QualificationError(
                "original failed-canary application no longer matches its durable identity"
            )
        return application

    def prepare_cleanup(request: Mapping[str, object]) -> Mapping[str, object]:
        require_target_request(request)
        request_key = _string(request.get("request_key"), "failed cleanup request key")
        if request.get("profile_number") != target.profile_number:
            raise QualificationError(
                "failed cleanup changed the campaign profile number"
            )
        reconcile_source_application()
        fleet = _typed_fleet(client)
        roster = sorted(_nodes(fleet))
        if roster != sorted(target.fleet_node_ids):
            raise QualificationError(
                "failed cleanup preview changed the locked Fleet roster"
            )
        if _all_loaded_runs(fleet) != {target_run_id}:
            raise QualificationError(
                "failed cleanup preview requires only its exact failed canary run"
            )
        _check_serving_fleet(
            fleet,
            run_id=target_run_id,
            revision_id=target.recipe_revision_id,
            alias=target.alias,
            node_ids=target.node_ids,
            expected_run_state="running",
            expected_route_state="published",
            expected_health=True,
        )
        profile = _profile_view(client, target.profile_number)
        _assert_profile_owner(profile, authority_id, ledger_id)
        if profile.get("installation_policy") != "keep-cached":
            raise QualificationError(
                "failed cleanup profile changed its installation policy"
            )
        exact_assignment = _profile_assignment(lane.row, lane.node_ids, lane.alias)
        if not (
            _profile_assignments_equal(profile, [exact_assignment])
            or _profile_assignments_equal(profile, [])
        ):
            raise QualificationError(
                "failed cleanup profile contains an unreviewed assignment"
            )
        if not _profile_assignments_equal(profile, []):
            profile = _save_profile(
                client,
                profile,
                assignments=[],
                authority_id=authority_id,
                ledger_id=ledger_id,
            )
        after_save = _typed_fleet(client)
        if sorted(_nodes(after_save)) != roster or _all_loaded_runs(after_save) != {
            target_run_id
        }:
            raise QualificationError(
                "failed canary workload changed while saving cleanup profile"
            )
        raw_preview = client.request(
            "POST", f"/api/profile/{target.profile_number}/preview"
        )
        preview_fleet = _typed_fleet(client)
        if sorted(_nodes(preview_fleet)) != roster or _all_loaded_runs(
            preview_fleet
        ) != {target_run_id}:
            raise QualificationError(
                "failed canary workload changed during cleanup review"
            )
        try:
            preview = FleetProfilePreview.from_dict(raw_preview).to_dict()
        except (KeyError, TypeError, ValueError) as error:
            raise QualificationError(
                "Controller failed-canary cleanup preview is invalid"
            ) from error
        if preview.get("allowed") is not True:
            raise QualificationError(
                f"failed-canary cleanup profile is blocked: {preview.get('reasons')}"
            )
        scope = _object(preview.get("scope"), "failed cleanup preview scope")
        summary = _object(preview.get("summary"), "failed cleanup preview summary")
        effects = _object(preview.get("effects"), "failed cleanup preview effects")
        raw_runs = effects.get("runs")
        if (
            scope.get("node_ids") != roster
            or scope.get("idle_node_ids") != roster
            or summary.get("stops") != 1
            or summary.get("starts") != 0
            or summary.get("placements") != 0
            or summary.get("builds") != 0
            or summary.get("distributions") != 0
            or summary.get("installs") != 0
            or summary.get("uninstalls") != 0
            or not isinstance(raw_runs, list)
            or len(raw_runs) != 1
        ):
            raise QualificationError(
                "failed cleanup preview contains unreviewed effects"
            )
        effect = _object(raw_runs[0], "failed cleanup run effect")
        if (
            effect.get("run_id") != target_run_id
            or effect.get("action") != "stop"
            or effect.get("alias") != target.alias
            or effect.get("node_ids") != sorted(target.node_ids)
        ):
            raise QualificationError(
                "failed cleanup preview changed the exact two-rank stop"
            )
        steps = preview.get("steps")
        if not isinstance(steps, list) or len(steps) != 1:
            raise QualificationError(
                "failed cleanup requires one whole-Fleet switch plan"
            )
        step = _object(steps[0], "failed cleanup switch step")
        if step.get("kind") != "switch" or set(
            _string_array(step.get("node_ids"), "failed cleanup switch nodes")
        ) != set(target.node_ids):
            raise QualificationError(
                "failed cleanup switch exceeded its exact two-rank scope"
            )
        receipt: dict[str, object] = {
            "request_key": request_key,
            "reviewed": True,
            "canary_record_sha256": target.canary_record_sha256,
            "terminal_event": "canary.failed",
            "terminal_record_sha256": target.canary_record_sha256,
            "source_application_id": target.application_id,
            "source_application_request_key": target.application_request_key,
            "source_application_updated_at": target.application_updated_at,
            "profile_number": target.profile_number,
            "profile_id": _string(profile.get("id"), "failed cleanup profile ID"),
            "profile_digest": _string(
                profile.get("profile_digest"), "failed cleanup profile digest"
            ),
            "plan_digest": _string(
                preview.get("plan_digest"), "failed cleanup plan digest"
            ),
            "cleanup_mode": "failed-dual-lane",
            "active_run_id": target_run_id,
            "stop_run_ids": [target_run_id],
            "stop_aliases": [target.alias],
            "node_ids": list(target.node_ids),
            "node_to_rank": dict(target.node_to_rank),
            "assigned_node_id": target.assigned_node_id,
            "assigned_rank": target.assigned_rank,
            "fleet_node_ids": roster,
        }
        context.clear()
        context.update({"receipt": receipt, "preview": preview, "profile": profile})
        return receipt

    def cleanup_application(
        request: Mapping[str, object], *, resume_completed: bool
    ) -> Mapping[str, object]:
        require_target_request(request)
        request_key = _string(request.get("request_key"), "failed cleanup request key")
        raw_review = request.get("review")
        if not isinstance(raw_review, Mapping):
            durable_review = _durable_failed_dual_cleanup_review(
                ledger,
                campaign_id=campaign_id,
                recipe=lane.row.key,
                batch_id=target.batch_id,
                lane_id=target.lane_id,
            )
            if durable_review is None:
                raise QualificationError("failed cleanup has no durable reviewed plan")
            raw_review = durable_review.get("review")
        review = _object(raw_review, "failed cleanup reviewed plan")
        review_digest = _string(
            request.get("review_digest"), "failed cleanup review digest"
        )
        if _digest(review) != review_digest:
            raise QualificationError("failed cleanup review digest is invalid")
        if resume_completed:
            prior_receipt = _object(
                request.get("prior_receipt"), "prior failed cleanup receipt"
            )
            if (
                prior_receipt.get("source_application_updated_at")
                != target.application_updated_at
                or prior_receipt.get("review_digest") != review_digest
            ):
                raise QualificationError(
                    "failed cleanup replay changed its durable source binding"
                )
        else:
            durable_review = _durable_failed_dual_cleanup_review(
                ledger,
                campaign_id=campaign_id,
                recipe=lane.row.key,
                batch_id=target.batch_id,
                lane_id=target.lane_id,
            )
            if (
                durable_review is None
                or durable_review.get("review_digest") != review_digest
                or durable_review.get("review") != dict(review)
            ):
                raise QualificationError(
                    "failed cleanup application is not bound to its durable review"
                )
        context_review = context.get("receipt")
        if context_review is not None and dict(
            _object(context_review, "failed cleanup prepared receipt")
        ) != dict(review):
            raise QualificationError(
                "failed cleanup application changed its prepared review"
            )
        profile_number = _integer(
            request.get("profile_number"), "failed cleanup profile number", 1, 2**31 - 1
        )
        profile_id = _string(request.get("profile_id"), "failed cleanup profile ID")
        profile_digest = _string(
            request.get("profile_digest"), "failed cleanup profile digest"
        )
        plan_digest = _string(
            request.get("plan_digest"), "failed cleanup profile plan digest"
        )
        if (
            profile_number != target.profile_number
            or review.get("profile_id") != profile_id
            or review.get("profile_digest") != profile_digest
            or review.get("plan_digest") != plan_digest
        ):
            raise QualificationError(
                "failed cleanup application changed its reviewed profile"
            )
        expected_app_id = (
            _string(
                request.get("application_id"), "persisted failed cleanup application ID"
            )
            if resume_completed
            else None
        )
        expected_updated_at = (
            _string(
                request.get("application_updated_at"),
                "persisted failed cleanup update time",
            )
            if resume_completed
            else None
        )
        accepted = _lookup_load_request(
            client,
            profile_number,
            request_key,
            plan_digest=plan_digest,
            profile_id=profile_id,
            profile_digest=profile_digest,
        )
        if accepted is None:
            if resume_completed:
                raise QualificationError(
                    "original failed-canary cleanup request is absent"
                )
            current_profile = _profile_view(client, profile_number)
            _assert_profile_owner(current_profile, authority_id, ledger_id)
            if (
                current_profile.get("id") != profile_id
                or current_profile.get("profile_digest") != profile_digest
                or not _profile_assignments_equal(current_profile, [])
            ):
                raise QualificationError(
                    "failed cleanup profile changed after its reviewed plan"
                )
            current_fleet = _typed_fleet(client)
            if _all_loaded_runs(current_fleet) != {target_run_id}:
                raise QualificationError(
                    "failed canary run changed before reviewed cleanup apply"
                )
            accepted = _submit_load(
                client,
                profile_number,
                request_key,
                plan_digest=plan_digest,
                profile_id=profile_id,
                profile_digest=profile_digest,
            )
        if expected_app_id is not None and accepted.get("id") != expected_app_id:
            raise QualificationError(
                "failed cleanup request resolved to another application"
            )
        application = _await_application(
            client,
            accepted,
            ledger=ledger,
            campaign_id=campaign_id,
            key=f"failed-cleanup:{target.batch_id}:{target.lane_id}",
            timeout=options.operation_timeout_seconds,
            interval=options.poll_interval_seconds,
            clock=clock,
            sleeper=sleeper,
        )
        if (
            application.get("state") != "succeeded"
            or application.get("request_key") != request_key
            or application.get("profile_id") != profile_id
            or application.get("profile_digest") != profile_digest
            or application.get("plan_digest")
            != _application_plan_digest(plan_digest, request_key)
        ):
            raise QualificationError(
                "failed cleanup application changed its reviewed identity"
            )
        application_id = _string(application.get("id"), "failed cleanup application ID")
        updated_at = _string(
            application.get("updated_at"), "failed cleanup application update time"
        )
        if expected_app_id is not None and (
            application_id != expected_app_id or updated_at != expected_updated_at
        ):
            raise QualificationError(
                "reconciled failed cleanup changed its original application"
            )
        stops = _profile_stop_receipts(application)
        if [item.get("run_id") for item in stops] != [target_run_id]:
            raise QualificationError(
                "failed cleanup lacks its exact terminal run-stop receipt"
            )
        final = _object(
            stops[0].get("final_observation"), "failed cleanup final stop proof"
        )
        if (
            stops[0].get("operation_state") != "succeeded"
            or final.get("run_id") != target_run_id
            or final.get("state") != "stopped"
            or final.get("route_state") != "withdrawn"
        ):
            raise QualificationError(
                "failed cleanup stop does not prove both exact ranks stopped"
            )
        _validate_stopped_rank_receipt(
            final,
            run_id=target_run_id,
            node_to_rank=target.node_to_rank,
            label="failed dual cleanup final stop proof",
        )
        post_fleet = _typed_fleet(client)
        if _all_loaded_runs(post_fleet):
            raise QualificationError("failed cleanup Fleet still contains a loaded run")
        if any(
            isinstance(presence, Mapping) and presence.get("route_state") == "published"
            for node in _nodes(post_fleet).values()
            for presence in _loaded_presences(node)
        ) or _endpoint_exists(client, target.alias):
            raise QualificationError("failed cleanup left a published route")
        runtime_reservation_names = (
            "unified_memory_bytes",
            "host_memory_bytes",
            "gpu_memory_bytes",
            "port_count",
        )
        for node_id in target.fleet_node_ids:
            node = _nodes(post_fleet)[node_id]
            reservations = _object(
                node.get("reservations"), "failed cleanup reservations"
            )
            if any(reservations.get(name) != 0 for name in runtime_reservation_names):
                raise QualificationError(
                    "failed cleanup Fleet still has active runtime claims"
                )
        return {
            **{
                key: review[key]
                for key in (
                    "canary_record_sha256",
                    "terminal_event",
                    "terminal_record_sha256",
                    "source_application_id",
                    "source_application_request_key",
                    "source_application_updated_at",
                    "cleanup_mode",
                    "active_run_id",
                    "stop_run_ids",
                    "stop_aliases",
                    "node_ids",
                    "node_to_rank",
                    "assigned_node_id",
                    "assigned_rank",
                    "fleet_node_ids",
                )
            },
            "request_key": request_key,
            "review_digest": review_digest,
            "application_state": "succeeded",
            "application_id": application_id,
            "application_updated_at": updated_at,
            "profile_number": profile_number,
            "profile_id": profile_id,
            "profile_digest": profile_digest,
            "plan_digest": plan_digest,
            "stop_receipts": stops,
            "fleet_snapshot": post_fleet,
        }

    def cleanup_to_idle(request: Mapping[str, object]) -> Mapping[str, object]:
        return cleanup_application(request, resume_completed=False)

    def reconcile_cleanup(request: Mapping[str, object]) -> Mapping[str, object]:
        if request.get("resume_completed") is not True:
            raise QualificationError(
                "failed cleanup reconciliation lacks its resume marker"
            )
        return cleanup_application(request, resume_completed=True)

    return {
        "prepare_cleanup": prepare_cleanup,
        "cleanup_to_idle": cleanup_to_idle,
        "reconcile_cleanup": reconcile_cleanup,
    }


def _observe_dual_recovery(
    *,
    client: Any,
    batch: CampaignBatch,
    lanes: Sequence[BatchLane],
    manifest: CampaignManifest,
    fixtures: FixtureRegistry,
    profile_number: int,
    campaign_id: str,
    ledger: EvidenceLedger,
) -> dict[str, object]:
    if len(lanes) != 1:
        raise QualificationError("exclusive dual batch must contain exactly one lane")
    lane = lanes[0]
    canary = _latest_batch_canary_outcome(
        ledger,
        campaign_id=campaign_id,
        batch_id=batch.batch_id,
        recipe=lane.row.key,
        lane_id=lane.assignment.lane,
    )
    if canary is None:
        raise QualificationError("dual observe requires a durable terminal canary")
    if canary.get("event") == "canary.failed":
        return {
            "schema_version": 1,
            "mode": "observe",
            "status": "failed-canary-cleanup-required",
            "batch_id": batch.batch_id,
            "lane_results": [
                {
                    "lane": lane.assignment.lane,
                    "recipe": lane.row.key,
                    "status": "canary-failed",
                }
            ],
            "next": {
                "checkpoint": "failed-exclusive-dual-cleanup",
                "instruction": "The failed dual canary remains lane-local; exact reviewed cleanup is required before the batch can advance.",
            },
            "spark_accepted": False,
        }
    fleet = _typed_fleet(client)
    target = _dual_recovery_target(
        batch=batch,
        lane=lane,
        campaign_id=campaign_id,
        ledger=ledger,
        profile_number=profile_number,
        fleet_node_ids=sorted(_nodes(fleet)),
        fixtures=fixtures,
    )
    callbacks = _dual_recovery_callbacks(
        client=client, target=target, lane=lane, fixtures=fixtures
    )
    progress = observe_dual_batch(
        target,
        ledger,
        observe_fleet=callbacks["observe_fleet"],
        endpoint_exists=callbacks["endpoint_exists"],
        verify_serving=callbacks["verify_serving"],
        run_fixture_smoke=callbacks["run_fixture_smoke"],
    )
    if progress.status == "complete":
        next_step = {
            "checkpoint": "explicit-dual-finalization",
            "lane": lane.assignment.lane,
            "instruction": "Repeat --cleanup-lane with --apply and the exact cleanup review digest to finalize acceptance and whole-Fleet release.",
        }
        status = "finalization-required"
    elif progress.status in {"awaiting-cleanup", "host-restarts-complete"}:
        next_step = {
            "checkpoint": "dual-cleanup",
            "lane": lane.assignment.lane,
            "instruction": "Review and apply --cleanup-lane for this exclusive dual lane before the idle-host restart sequence.",
        }
        status = "cleanup-required"
    else:
        next_step = {
            "checkpoint": progress.checkpoint or progress.status,
            "lane": lane.assignment.lane,
            "node_id": progress.node_id,
            "instruction": progress.reason,
        }
        status = "checkpoint-required"
    return {
        "schema_version": 1,
        "mode": "observe",
        "status": status,
        "batch_id": batch.batch_id,
        "lane_results": [
            {
                "lane": lane.assignment.lane,
                "recipe": lane.row.key,
                "status": progress.status,
                "checkpoint": progress.checkpoint,
                "node_id": progress.node_id,
                "receipt_sha256": progress.receipt_sha256,
                "reason": progress.reason,
            }
        ],
        "next": next_step,
        "spark_accepted": False,
    }


def _durable_dual_cleanup_review(
    ledger: EvidenceLedger,
    *,
    campaign_id: str,
    recipe: str,
    batch_id: str,
    lane_id: int,
) -> Mapping[str, object] | None:
    for record in reversed(ledger.recipe_records(campaign_id, recipe)):
        if record.get(
            "event"
        ) == "dual_recovery.cleanup.plan_reviewed" and _record_payload_matches(
            record, {"batch_id": batch_id, "lane_id": lane_id}
        ):
            return _object(record["payload"], "durable dual cleanup review")
    return None


def _durable_failed_dual_cleanup_review(
    ledger: EvidenceLedger,
    *,
    campaign_id: str,
    recipe: str,
    batch_id: str,
    lane_id: int,
) -> Mapping[str, object] | None:
    for record in reversed(ledger.recipe_records(campaign_id, recipe)):
        if record.get(
            "event"
        ) == "dual_recovery.failed_cleanup.plan_reviewed" and _record_payload_matches(
            record, {"batch_id": batch_id, "lane_id": lane_id}
        ):
            return _object(record["payload"], "durable failed-dual cleanup review")
    return None


def _review_or_apply_dual_cleanup(
    *,
    client: Any,
    batch: CampaignBatch,
    lane: BatchLane,
    manifest: CampaignManifest,
    fixtures: FixtureRegistry,
    profile_number: int,
    authority_id: str,
    ledger_id: str,
    campaign_id: str,
    ledger: EvidenceLedger,
    apply: bool,
    supplied_digest: str | None,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> dict[str, object]:
    canary = _latest_batch_canary_outcome(
        ledger,
        campaign_id=campaign_id,
        batch_id=batch.batch_id,
        recipe=lane.row.key,
        lane_id=lane.assignment.lane,
    )
    if canary is not None and canary.get("event") == "canary.failed":
        return _review_or_apply_failed_dual_cleanup(
            client=client,
            batch=batch,
            lane=lane,
            manifest=manifest,
            profile_number=profile_number,
            authority_id=authority_id,
            ledger_id=ledger_id,
            campaign_id=campaign_id,
            ledger=ledger,
            apply=apply,
            supplied_digest=supplied_digest,
            clock=clock,
            sleeper=sleeper,
        )
    fleet = _typed_fleet(client)
    target = _dual_recovery_target(
        batch=batch,
        lane=lane,
        campaign_id=campaign_id,
        ledger=ledger,
        profile_number=profile_number,
        fleet_node_ids=sorted(_nodes(fleet)),
        fixtures=fixtures,
    )
    recovery_callbacks = _dual_recovery_callbacks(
        client=client, target=target, lane=lane, fixtures=fixtures
    )
    cleanup_callbacks = _dual_cleanup_callbacks(
        client=client,
        target=target,
        lane=lane,
        authority_id=authority_id,
        ledger_id=ledger_id,
        campaign_id=campaign_id,
        ledger=ledger,
        options=manifest,
        clock=clock,
        sleeper=sleeper,
    )
    if apply:
        durable = _durable_dual_cleanup_review(
            ledger,
            campaign_id=campaign_id,
            recipe=lane.row.key,
            batch_id=batch.batch_id,
            lane_id=lane.assignment.lane,
        )
        if durable is None:
            raise QualificationError("review exact dual cleanup before applying it")
        review_digest = _string(
            durable.get("review_digest"), "dual cleanup review digest"
        )
        if supplied_digest != review_digest:
            raise QualificationError(
                "--campaign-digest does not match the durable dual cleanup review"
            )
        progress = apply_dual_cleanup(
            target,
            ledger,
            prepare_cleanup=cleanup_callbacks["prepare_cleanup"],
            apply_authorized=True,
            cleanup_to_idle=cleanup_callbacks["cleanup_to_idle"],
            reconcile_cleanup=cleanup_callbacks["reconcile_cleanup"],
            endpoint_exists=recovery_callbacks["endpoint_exists"],
        )
        # Applying or reconciling the exact stop is followed by one read-only
        # recovery observation. The dual helper owns all host checkpoints.
        progress = observe_dual_batch(
            target,
            ledger,
            observe_fleet=recovery_callbacks["observe_fleet"],
            endpoint_exists=recovery_callbacks["endpoint_exists"],
            verify_serving=recovery_callbacks["verify_serving"],
            run_fixture_smoke=recovery_callbacks["run_fixture_smoke"],
        )
        mode = "apply"
    else:
        review = review_dual_cleanup(
            target, ledger, prepare_cleanup=cleanup_callbacks["prepare_cleanup"]
        )
        progress = DualRecoveryProgress(
            "awaiting-explicit-cleanup",
            review.target_digest,
            checkpoint="reviewed-cleanup",
        )
        mode = "preview"
    durable = _durable_dual_cleanup_review(
        ledger,
        campaign_id=campaign_id,
        recipe=lane.row.key,
        batch_id=batch.batch_id,
        lane_id=lane.assignment.lane,
    )
    cleanup_digest = (
        _string(durable.get("review_digest"), "dual cleanup review digest")
        if durable is not None
        else None
    )
    result: dict[str, object] = {
        "schema_version": 1,
        "mode": mode,
        "status": "not-accepted" if mode == "preview" else "checkpoint-required",
        "batch_id": batch.batch_id,
        "lane": lane.assignment.lane,
        "recipe": lane.row.key,
        "campaign_digest": cleanup_digest,
        "cleanup": {
            "status": progress.status,
            "checkpoint": progress.checkpoint,
            "node_id": progress.node_id,
            "receipt_sha256": progress.receipt_sha256,
            "reason": progress.reason,
        },
        "spark_accepted": False,
    }
    if mode == "apply" and progress.status == "complete":
        acceptance = _record_dual_lane_acceptance(
            batch=batch,
            lane=lane,
            campaign_id=campaign_id,
            ledger=ledger,
            target=target,
            manifest=manifest,
            fixtures=fixtures,
            client=client,
        )
        release = _record_batch_release(
            client=client,
            profile_number=profile_number,
            authority_id=authority_id,
            ledger_id=ledger_id,
            batch=batch,
            campaign_id=campaign_id,
            ledger=ledger,
        )
        result.update(
            {
                "status": "lane-terminal",
                "acceptance": acceptance,
                "batch_release": dict(release),
                "spark_accepted": True,
            }
        )
    else:
        result["next"] = {
            "checkpoint": progress.checkpoint or progress.status,
            "node_id": progress.node_id,
            "instruction": progress.reason,
        }
    return result


def _review_or_apply_failed_dual_cleanup(
    *,
    client: Any,
    batch: CampaignBatch,
    lane: BatchLane,
    manifest: CampaignManifest,
    profile_number: int,
    authority_id: str,
    ledger_id: str,
    campaign_id: str,
    ledger: EvidenceLedger,
    apply: bool,
    supplied_digest: str | None,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> dict[str, object]:
    if batch.mode != "exclusive-dual":
        raise QualificationError(
            "failed dual cleanup is scoped to an exclusive dual batch"
        )
    fleet = _typed_fleet(client)
    target = _failed_dual_canary_target(
        batch=batch,
        lane=lane,
        campaign_id=campaign_id,
        ledger=ledger,
        profile_number=profile_number,
        fleet_node_ids=sorted(_nodes(fleet)),
    )
    callbacks = _failed_dual_cleanup_callbacks(
        client=client,
        target=target,
        lane=lane,
        authority_id=authority_id,
        ledger_id=ledger_id,
        campaign_id=campaign_id,
        ledger=ledger,
        options=manifest,
        clock=clock,
        sleeper=sleeper,
    )
    if apply:
        durable = _durable_failed_dual_cleanup_review(
            ledger,
            campaign_id=campaign_id,
            recipe=lane.row.key,
            batch_id=batch.batch_id,
            lane_id=lane.assignment.lane,
        )
        if durable is None:
            raise QualificationError(
                "review exact failed-dual cleanup before applying it"
            )
        expected_digest = _string(
            durable.get("review_digest"), "failed-dual cleanup review digest"
        )
        if supplied_digest != expected_digest:
            raise QualificationError(
                "--campaign-digest does not match the durable failed-dual cleanup review"
            )
        progress = apply_failed_dual_cleanup(
            target,
            ledger,
            prepare_cleanup=callbacks["prepare_cleanup"],
            apply_authorized=True,
            cleanup_to_idle=callbacks["cleanup_to_idle"],
            reconcile_cleanup=callbacks["reconcile_cleanup"],
            endpoint_exists=lambda alias: _endpoint_exists(client, alias),
        )
        mode = "apply"
    else:
        review = review_failed_dual_cleanup(
            target, ledger, prepare_cleanup=callbacks["prepare_cleanup"]
        )
        progress = DualRecoveryProgress(
            "awaiting-explicit-failed-cleanup",
            review.target_digest,
            checkpoint="failed-cleanup-reviewed",
            reason="the exact failed-canary cleanup is reviewed; explicit apply is required",
        )
        mode = "preview"
    durable = _durable_failed_dual_cleanup_review(
        ledger,
        campaign_id=campaign_id,
        recipe=lane.row.key,
        batch_id=batch.batch_id,
        lane_id=lane.assignment.lane,
    )
    review_digest = (
        _string(durable.get("review_digest"), "failed-dual cleanup review digest")
        if durable is not None
        else None
    )
    result: dict[str, object] = {
        "schema_version": 1,
        "mode": mode,
        "status": "not-accepted" if mode == "preview" else "cleanup-required",
        "batch_id": batch.batch_id,
        "lane": lane.assignment.lane,
        "recipe": lane.row.key,
        "campaign_digest": review_digest,
        "cleanup": {
            "status": progress.status,
            "target_digest": progress.target_digest,
            "receipt_sha256": progress.receipt_sha256,
            "reason": progress.reason,
        },
        "spark_accepted": False,
    }
    if mode == "apply" and progress.status == "failed-cleanup-completed":
        terminal = next(
            (
                record
                for record in reversed(ledger.recipe_records(campaign_id, lane.row.key))
                if record.get("record_sha256") == progress.receipt_sha256
                and record.get("event")
                in {
                    "dual_recovery.failed_cleanup.completed",
                    "dual_recovery.failed_cleanup.reconciled",
                }
            ),
            None,
        )
        if terminal is None:
            raise QualificationError("failed-dual cleanup completion receipt is absent")
        existing_failure = _latest_payload(
            ledger, campaign_id, lane.row.key, "recipe.failed"
        )
        if existing_failure is not None and (
            existing_failure.get("batch_id") != batch.batch_id
            or existing_failure.get("lane") != lane.assignment.lane
            or existing_failure.get("canary_record_sha256")
            != target.canary_record_sha256
        ):
            raise QualificationError(
                "existing recipe failure belongs to another batch lane"
            )
        if existing_failure is None:
            canary_outcome = _latest_batch_canary_outcome(
                ledger,
                campaign_id=campaign_id,
                batch_id=batch.batch_id,
                recipe=lane.row.key,
                lane_id=lane.assignment.lane,
            )
            if canary_outcome is None:
                raise QualificationError("failed dual canary outcome is absent")
            canary_error = _object(
                canary_outcome.get("payload"), "failed dual canary"
            ).get("error")
            failure = ledger.append(
                "recipe.failed",
                plan_digest=campaign_id,
                recipe=lane.row.key,
                payload={
                    "batch_id": batch.batch_id,
                    "lane": lane.assignment.lane,
                    "recipe_content_sha256": lane.row.content_sha256,
                    "package_sha256": lane.row.package.get("sha256"),
                    "canary_record_sha256": target.canary_record_sha256,
                    "cleanup_record_sha256": terminal.get("record_sha256"),
                    "error": canary_error,
                    "disposition": "failed-after-reviewed-cleanup",
                },
            )
            existing_failure = _object(
                failure.get("payload"), "recorded recipe failure"
            )
        release = _record_batch_release(
            client=client,
            profile_number=profile_number,
            authority_id=authority_id,
            ledger_id=ledger_id,
            batch=batch,
            campaign_id=campaign_id,
            ledger=ledger,
        )
        result.update(
            {
                "status": "lane-terminal",
                "lane_outcome": {"status": "recipe.failed", **dict(existing_failure)},
                "batch_release": dict(release),
            }
        )
    else:
        result["next"] = {
            "checkpoint": progress.checkpoint or progress.status,
            "instruction": progress.reason
            or "Apply the exact reviewed cleanup or reconcile its original request.",
        }
    return result


def _canary_runs_in_fleet(
    fleet: Mapping[str, object], references: Sequence[CanaryReference]
) -> dict[str, CanaryReference]:
    expected = {item.run_id: item for item in references if item.run_id is not None}
    observed = _all_loaded_runs(fleet)
    if not observed <= set(expected):
        raise QualificationError(
            "whole-Fleet snapshot contains a run outside paired canary evidence"
        )
    for run_id in observed:
        reference = expected[run_id]
        presences = _run_presences(fleet, run_id)
        if not presences:
            raise QualificationError(
                "Fleet run index is inconsistent with loaded-run evidence"
            )
        expected_ranks = dict(
            reference.node_to_rank
            or {reference.assigned_node_id: reference.assigned_rank}
        )
        observed_ranks: dict[str, int] = {}
        for node_id, presence in presences:
            rank = presence.get("rank")
            if type(rank) is not int or node_id in observed_ranks:
                raise QualificationError(
                    "paired canary has duplicate or invalid Fleet rank presence"
                )
            observed_ranks[node_id] = rank
            if (
                presence.get("alias") != reference.alias
                or presence.get("recipe_revision_id") != reference.recipe_revision_id
                or presence.get("member_node_ids") != sorted(expected_ranks)
            ):
                raise QualificationError(
                    "paired Fleet run changed its exact canary identity"
                )
        if observed_ranks != expected_ranks:
            raise QualificationError(
                "paired Fleet run changed its exact canary node/rank map"
            )
    return {run_id: expected[run_id] for run_id in observed}


def _profile_assignment_state(
    profile: Mapping[str, object], lanes: Sequence[BatchLane]
) -> bool:
    original = [
        _profile_assignment(lane.row, lane.node_ids, lane.alias) for lane in lanes
    ]
    return _profile_assignments_equal(profile, original) or any(
        _profile_assignments_equal(
            profile,
            [_profile_assignment(lane.row, lane.node_ids, lane.alias)],
        )
        for lane in lanes
    )


def _recovery_plan_review(
    *,
    client: Any,
    request: Mapping[str, object],
    target: LaneRecoveryTarget,
    lane: BatchLane,
    lanes: Sequence[BatchLane],
    references: Sequence[CanaryReference],
    ledger: EvidenceLedger,
    authority_id: str,
    ledger_id: str,
) -> tuple[
    Mapping[str, object], dict[str, object], Mapping[str, object], dict[str, object]
]:
    """Save and validate a one-lane profile while preserving only exact batch effects."""
    profile_number = _integer(
        request.get("profile_number"), "recovery profile number", 1, 2**31 - 1
    )
    fleet = _typed_fleet(client)
    roster = sorted(_nodes(fleet))
    if roster != list(target.fleet_node_ids):
        raise QualificationError(
            "recovery preview changed the locked whole-Fleet roster"
        )
    if any(not _online(node) for node in _nodes(fleet).values()):
        raise QualificationError(
            "exclusive recovery review requires every enrolled Spark online"
        )
    before_runs = _canary_runs_in_fleet(fleet, references)
    target_ref = next(item for item in references if item.lane_id == target.lane_id)
    target_is_active = (
        target_ref.run_id in before_runs if target_ref.run_id is not None else False
    )
    if target_is_active:
        _check_serving_fleet(
            fleet,
            run_id=_string(target_ref.run_id, "target canary run ID"),
            revision_id=target.recipe_revision_id,
            alias=target.alias,
            node_ids=[target.node_id],
            expected_run_state="running",
            expected_route_state="published",
            expected_health=True,
        )
        if dict(target_ref.node_to_rank or {}) != dict(target.node_to_rank):
            raise QualificationError("target lane canary rank identity changed")
    else:
        if not target.allow_reactivation:
            raise QualificationError(
                "target canary run is absent and reactivation is not authorized"
            )
    partner_refs = {
        item.run_id: item
        for item in references
        if item.lane_id != target.lane_id and item.run_id is not None
    }
    if any(
        item.lane_id != target.lane_id and item.run_id is None for item in references
    ):
        raise QualificationError(
            "a runless partner canary must be reconciled before exclusive lane recovery"
        )
    expected_partner_runs = set(partner_refs)
    partner_runs = sorted(set(before_runs) & expected_partner_runs)
    missing_partner_runs = sorted(expected_partner_runs - set(partner_runs))
    released_partner_proofs = list(
        prior_partner_release_proofs(
            target,
            # The helper verifies each missing run against its exact earlier
            # typed stop, same-batch cleanup receipt, and fresh Fleet absence.
            ledger,
            fleet,
            candidate_run_ids=missing_partner_runs,
        )
    )
    expected_profile = _profile_view(client, profile_number)
    _assert_profile_owner(expected_profile, authority_id, ledger_id)
    profile_is_prior_cleanup = bool(
        released_partner_proofs
    ) and _profile_assignments_equal(expected_profile, [])
    if expected_profile.get("installation_policy") != "keep-cached" or not (
        _profile_assignment_state(expected_profile, lanes) or profile_is_prior_cleanup
    ):
        raise QualificationError("dedicated profile changed outside this exact batch")
    assignment = _profile_assignment(lane.row, lane.node_ids, lane.alias)
    saved = expected_profile
    if not _profile_assignments_equal(expected_profile, [assignment]):
        saved = _save_profile(
            client,
            expected_profile,
            assignments=[assignment],
            authority_id=authority_id,
            ledger_id=ledger_id,
        )
    _assert_profile_owner(saved, authority_id, ledger_id)
    if not _profile_assignments_equal(saved, [assignment]):
        raise QualificationError(
            "exclusive recovery profile did not retain its exact lane"
        )
    profile_digest = _string(saved.get("profile_digest"), "recovery profile digest")
    profile_id = _string(saved.get("id"), "recovery profile ID")
    current_fleet = _typed_fleet(client)
    if (
        sorted(_nodes(current_fleet)) != roster
        or _canary_runs_in_fleet(current_fleet, references) != before_runs
    ):
        raise QualificationError(
            "canary workloads changed while saving the recovery profile"
        )
    raw_preview = client.request("POST", f"/api/profile/{profile_number}/preview")
    preview_fleet = _typed_fleet(client)
    if (
        sorted(_nodes(preview_fleet)) != roster
        or _canary_runs_in_fleet(preview_fleet, references) != before_runs
    ):
        raise QualificationError(
            "canary workloads changed while reviewing the recovery profile"
        )
    try:
        preview = FleetProfilePreview.from_dict(raw_preview).to_dict()
    except (KeyError, TypeError, ValueError) as error:
        raise QualificationError(
            "Controller one-lane recovery preview is invalid"
        ) from error
    if preview.get("allowed") is not True:
        raise QualificationError(
            f"one-lane recovery profile is blocked: {preview.get('reasons')}"
        )
    scope = _object(preview.get("scope"), "recovery profile preview scope")
    if (
        scope.get("node_ids") != roster
        or scope.get("idle_node_ids") != sorted(set(roster) - set(lane.node_ids))
        or preview.get("profile_id") != profile_id
        or preview.get("profile_digest") != profile_digest
    ):
        raise QualificationError(
            "recovery preview changed its exact whole-Fleet scope or profile"
        )
    raw_assignments = preview.get("assignments")
    raw_preparations = preview.get("preparations")
    if not isinstance(raw_assignments, list) or len(raw_assignments) != 1:
        raise QualificationError(
            "recovery preview does not contain exactly one target lane"
        )
    if not isinstance(raw_preparations, list) or len(raw_preparations) != 1:
        raise QualificationError("recovery preview lacks one exact cache preparation")
    assignment_preview = _object(raw_assignments[0], "recovery assignment preview")
    revision_id = assignment_preview.get("recipe_revision_id")
    expected_identity = lane.detail.get("identity")
    expected_recipe_id = (
        expected_identity.get("recipe_id")
        if isinstance(expected_identity, Mapping)
        else None
    )
    if (
        assignment_preview.get("node_ids") != sorted(lane.node_ids)
        or assignment_preview.get("desired_state") != "running"
        or assignment_preview.get("assignment_name") not in {None, lane.alias}
        or assignment_preview.get("recipe_id") not in {None, expected_recipe_id}
        or revision_id != target.recipe_revision_id
    ):
        raise QualificationError(
            "recovery preview changed exact recipe, revision, node, or route"
        )
    assignment_id = _string(
        assignment_preview.get("assignment_id"), "recovery assignment ID"
    )
    preparation = _validate_preparations(
        {
            "preparations": [
                {
                    "assignment_id": _object(
                        raw_preparations[0], "recovery preparation entry"
                    ).get("assignment_id"),
                    "preparation": _object(
                        raw_preparations[0], "recovery preparation entry"
                    ).get("preparation"),
                }
            ]
        },
        lane.row,
        lane.node_ids,
    )
    if (
        _object(raw_preparations[0], "recovery preparation entry").get("assignment_id")
        != assignment_id
    ):
        raise QualificationError(
            "recovery cache preparation is bound to another assignment"
        )
    summary = _object(preview.get("summary"), "recovery preview summary")
    effects = _object(preview.get("effects"), "recovery preview effects")
    raw_run_effects = effects.get("runs")
    if not isinstance(raw_run_effects, list) or len(raw_run_effects) != len(
        partner_runs
    ):
        raise QualificationError(
            "recovery preview contains an extra or missing workload stop"
        )
    effects_by_run: dict[str, Mapping[str, object]] = {}
    for raw_effect in raw_run_effects:
        effect = _object(raw_effect, "recovery run effect")
        run_id = _string(effect.get("run_id"), "recovery stopped run ID")
        if run_id in effects_by_run:
            raise QualificationError("recovery preview repeats a stopped run")
        effects_by_run[run_id] = effect
    if set(effects_by_run) != set(partner_runs):
        raise QualificationError(
            "recovery preview does not stop the exact paired partner"
        )
    for run_id, effect in effects_by_run.items():
        reference = partner_refs[run_id]
        expected_nodes = sorted(
            reference.node_to_rank
            or {reference.assigned_node_id: reference.assigned_rank}
        )
        if (
            effect.get("action") != "stop"
            or effect.get("alias") != reference.alias
            or effect.get("node_ids") != expected_nodes
        ):
            raise QualificationError(
                "recovery stop effect changed exact partner identity"
            )
    expected_starts = 0 if target_is_active else 1
    if (
        summary.get("stops") != len(partner_runs)
        or summary.get("starts") != expected_starts
        or summary.get("uninstalls") != 0
    ):
        raise QualificationError(
            "recovery plan has unreviewed start, stop, or uninstall effects"
        )
    raw_steps = preview.get("steps")
    expected_switch_nodes = set(lane.node_ids) | {
        node_id
        for run_id in partner_runs
        for node_id in (
            partner_refs[run_id].node_to_rank
            or {
                partner_refs[run_id].assigned_node_id: partner_refs[
                    run_id
                ].assigned_rank
            }
        )
    }
    if partner_runs or expected_starts:
        if not isinstance(raw_steps, list) or len(raw_steps) != 1:
            raise QualificationError(
                "recovery plan lacks one aggregate whole-Fleet switch"
            )
        step = _object(raw_steps[0], "recovery switch step")
        step_nodes = _string_array(step.get("node_ids"), "recovery switch node IDs")
        if step.get("kind") != "switch" or set(step_nodes) != expected_switch_nodes:
            raise QualificationError("recovery switch scope changed exact paired nodes")
    elif raw_steps != []:
        raise QualificationError(
            "no-op recovery preview contains unreviewed plan steps"
        )
    checked = {
        **preview,
        "lane_revision_ids": {lane.row.key: target.recipe_revision_id},
        "exact_preparations": {lane.row.key: preparation},
    }
    receipt: dict[str, object] = {
        "request_key": _string(request.get("request_key"), "recovery request key"),
        "reviewed": True,
        "profile_number": profile_number,
        "profile_id": profile_id,
        "profile_digest": profile_digest,
        "plan_digest": _string(preview.get("plan_digest"), "recovery plan digest"),
        "fleet_node_ids": roster,
        "assignment": {
            "recipe_key": lane.row.key,
            "recipe_content_sha256": lane.row.content_sha256,
            "package_sha256": _string(
                lane.row.package.get("sha256"), "recipe package digest"
            ),
            "recipe_revision_id": target.recipe_revision_id,
            "alias": lane.alias,
            "node_to_rank": dict(target.node_to_rank),
        },
        "keep_run_id": target.original_run_id if target_is_active else None,
        "stop_run_ids": partner_runs,
        "stop_aliases": sorted(
            _string(partner_refs[run_id].alias, "active partner alias")
            for run_id in partner_runs
        ),
        "released_partner_proofs": released_partner_proofs,
    }
    return receipt, checked, preview_fleet, {"profile": saved, "preview": checked}


def _find_final_verifications(value: object) -> list[Mapping[str, object]]:
    found: list[Mapping[str, object]] = []
    if isinstance(value, Mapping):
        if value.get("phase") == "final_verify":
            found.append(value)
        for child in value.values():
            found.extend(_find_final_verifications(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_find_final_verifications(child))
    return found


def _profile_stop_receipts(
    application: Mapping[str, object],
) -> list[dict[str, object]]:
    progress = _object(application.get("progress"), "profile application progress")
    adapter = _object(progress.get("switch_adapter"), "profile switch adapter progress")
    result = adapter.get("result")
    if isinstance(result, Mapping):
        children = result.get("children")
    else:
        children = adapter.get("children")
    if not isinstance(children, list):
        raise QualificationError(
            "profile application lacks durable switch child receipts"
        )
    receipts: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw_child in children:
        child = _object(raw_child, "profile switch child")
        if child.get("kind") != "stop":
            continue
        state = child.get("state")
        child_result = _object(child.get("result"), "profile stop child result")
        run_switch = _object(child_result.get("run_switch"), "run stop receipt")
        final = run_switch.get("final_observation")
        if (
            not isinstance(final, Mapping)
            or final.get("phase") != "final_verify"
            or final.get("final_verified") is not True
        ):
            phase_results = run_switch.get("phase_results")
            candidates = (
                [item for item in phase_results if isinstance(item, Mapping)]
                if isinstance(phase_results, list)
                else []
            )
            final = next(
                (
                    item
                    for item in reversed(candidates)
                    if item.get("phase") == "final_verify"
                    and item.get("final_verified") is True
                ),
                None,
            )
        if not isinstance(final, Mapping):
            raise QualificationError(
                "profile stop child lacks a terminal final verification"
            )
        final_receipt = dict(final)
        run_id = _string(final_receipt.get("run_id"), "stopped run ID")
        if run_id in seen:
            raise QualificationError(
                "profile application repeats a stopped run receipt"
            )
        if state != "succeeded":
            raise QualificationError("profile application stop child did not succeed")
        seen.add(run_id)
        receipts.append(
            {
                "run_id": run_id,
                "operation_state": state,
                "final_observation": final_receipt,
            }
        )
    return receipts


def _run_reactivation_smoke(
    *,
    client: Any,
    target: LaneRecoveryTarget,
    lane: BatchLane,
    fixtures: FixtureRegistry,
    ledger: EvidenceLedger,
    active: Mapping[str, object],
    boot_id: str,
    request_key: str,
    application_id: str,
    campaign_id: str,
    options: CampaignManifest,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> Mapping[str, object]:
    existing = next(
        (
            record
            for record in reversed(ledger.recipe_records(campaign_id, lane.row.key))
            if record.get("event") == "lane_recovery.reactivation_smoke.completed"
            and _record_payload_matches(
                record,
                {
                    "batch_id": target.batch_id,
                    "lane_id": target.lane_id,
                    "run_id": active.get("run_id"),
                    "request_key": request_key,
                },
            )
        ),
        None,
    )
    if existing is not None:
        payload = _object(existing.get("payload"), "reactivation smoke receipt")
        receipt = _object(payload.get("smoke_receipt"), "reactivation fixture smoke")
        return {
            "passed": True,
            "case_ids": list(target.smoke_case_ids),
            "record_sha256": _string(
                existing.get("record_sha256"), "reactivation smoke record digest"
            ),
            "run_id": active.get("run_id"),
            "recipe_key": target.recipe_key,
            "recipe_content_sha256": target.recipe_content_sha256,
            "package_sha256": target.package_sha256,
            "recipe_revision_id": target.recipe_revision_id,
            "alias": target.alias,
            "node_to_rank": dict(target.node_to_rank),
            "boot_id": boot_id,
            "cases": receipt.get("cases"),
            "application_id": application_id,
        }
    request = {
        "request_key": request_key,
        "batch_id": target.batch_id,
        "lane_id": target.lane_id,
        "run_id": active.get("run_id"),
        "recipe_key": target.recipe_key,
        "recipe_content_sha256": target.recipe_content_sha256,
        "package_sha256": target.package_sha256,
        "recipe_revision_id": target.recipe_revision_id,
        "alias": target.alias,
        "node_to_rank": dict(target.node_to_rank),
        "boot_id": boot_id,
        "case_ids": list(target.smoke_case_ids),
        "application_id": application_id,
    }
    prior_intent = next(
        (
            record
            for record in reversed(ledger.recipe_records(campaign_id, lane.row.key))
            if record.get("event") == "lane_recovery.reactivation_smoke.requested"
            and _record_payload_matches(record, {"request_key": request_key})
        ),
        None,
    )
    if prior_intent is None:
        ledger.append(
            "lane_recovery.reactivation_smoke.requested",
            plan_digest=campaign_id,
            recipe=lane.row.key,
            payload=request,
        )
    elif prior_intent.get("payload") != request:
        raise QualificationError(
            "reactivation smoke intent changed its exact active lane"
        )
    smoke_cases: list[Mapping[str, object]]
    if lane.smoke_kind == "artifact-job":
        smoke = ArtifactJobSmokeAdapter(fixtures).run(
            client,
            _string(active.get("run_id"), "reactivated run ID"),
            lane.smoke_preview,
            ledger=ledger,
            plan_digest=campaign_id,
            recipe_key=lane.row.key,
            timeout_seconds=options.operation_timeout_seconds,
            poll_interval_seconds=options.poll_interval_seconds,
            clock=clock,
            sleeper=sleeper,
            event_prefix=f"reactivation.{target.batch_id}.{target.lane_id}.{active.get('run_id')!s}",
        )
        raw_case_value = smoke.get("cases")
        raw_cases: list[object] = (
            raw_case_value if isinstance(raw_case_value, list) else [smoke]
        )
        smoke_cases = [
            {
                "case_id": _object(item, "reactivation artifact case").get("case_id"),
                "passed": True,
            }
            for item in raw_cases
        ]
    else:
        smoke = ServiceSmokeAdapter(fixtures).run(
            client, lane.alias, lane.smoke_preview
        )
        service_cases = smoke.get("cases")
        if not isinstance(service_cases, list):
            raise QualificationError(
                "reactivation service smoke lacks per-case results"
            )
        smoke_cases = [
            {
                "case_id": _object(item, "reactivation service case").get("case_id"),
                "passed": True,
            }
            for item in service_cases
        ]
    if [item.get("case_id") for item in smoke_cases] != list(target.smoke_case_ids):
        raise QualificationError(
            "reactivation smoke did not run every reviewed fixture case"
        )
    payload = {**request, "smoke_receipt": {**dict(smoke), "cases": smoke_cases}}
    record = ledger.append(
        "lane_recovery.reactivation_smoke.completed",
        plan_digest=campaign_id,
        recipe=lane.row.key,
        payload=payload,
    )
    return {
        "passed": True,
        "case_ids": list(target.smoke_case_ids),
        "record_sha256": _string(
            record.get("record_sha256"), "reactivation smoke record digest"
        ),
        "run_id": active.get("run_id"),
        "recipe_key": target.recipe_key,
        "recipe_content_sha256": target.recipe_content_sha256,
        "package_sha256": target.package_sha256,
        "recipe_revision_id": target.recipe_revision_id,
        "alias": target.alias,
        "node_to_rank": dict(target.node_to_rank),
        "boot_id": boot_id,
        "cases": smoke_cases,
        "application_id": application_id,
    }


def _lane_recovery_callbacks(
    *,
    client: Any,
    target: LaneRecoveryTarget,
    lane: BatchLane,
    lanes: Sequence[BatchLane],
    references: Sequence[CanaryReference],
    fixtures: FixtureRegistry,
    profile_number: int,
    authority_id: str,
    ledger_id: str,
    campaign_id: str,
    ledger: EvidenceLedger,
    options: CampaignManifest,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> dict[str, Any]:
    context: dict[str, object] = {}

    def prepare_transition(request: Mapping[str, object]) -> Mapping[str, object]:
        receipt, checked, pre_fleet, details = _recovery_plan_review(
            client=client,
            request=request,
            target=target,
            lane=lane,
            lanes=lanes,
            references=references,
            ledger=ledger,
            authority_id=authority_id,
            ledger_id=ledger_id,
        )
        context.clear()
        context.update(
            {
                "receipt": receipt,
                "preview": checked,
                "pre_fleet": pre_fleet,
                "profile": details["profile"],
                "reviewed_digest": _digest(receipt),
            }
        )
        return receipt

    def transition_to_lane(request: Mapping[str, object]) -> Mapping[str, object]:
        receipt = _object(context.get("receipt"), "prepared recovery review")
        profile = _object(context.get("profile"), "prepared recovery profile")
        preview = _object(context.get("preview"), "prepared recovery preview")
        if (
            request.get("review_digest") != context.get("reviewed_digest")
            or request.get("review") != dict(receipt)
            or request.get("request_key") != receipt.get("request_key")
        ):
            raise QualificationError(
                "recovery apply differs from its exact reviewed lane plan"
            )
        request_key = _string(request.get("request_key"), "recovery request key")
        profile_id = _string(profile.get("id"), "recovery profile ID")
        profile_digest = _string(
            profile.get("profile_digest"), "recovery profile digest"
        )
        plan_digest = _string(preview.get("plan_digest"), "recovery plan digest")
        accepted = _submit_load(
            client,
            profile_number,
            request_key,
            plan_digest=plan_digest,
            profile_id=profile_id,
            profile_digest=profile_digest,
        )
        application = _await_application(
            client,
            accepted,
            ledger=ledger,
            campaign_id=campaign_id,
            key=f"recovery:{target.batch_id}:{target.lane_id}",
            timeout=options.operation_timeout_seconds,
            interval=options.poll_interval_seconds,
            clock=clock,
            sleeper=sleeper,
        )
        if (
            application.get("state") != "succeeded"
            or application.get("profile_id") != profile_id
            or application.get("profile_digest") != profile_digest
            or application.get("plan_digest")
            != _application_plan_digest(plan_digest, request_key)
            or application.get("request_key") != request_key
        ):
            raise QualificationError(
                "accepted recovery application changed its exact profile plan"
            )
        application_id = _string(application.get("id"), "recovery application ID")
        pre_fleet = _object(context.get("pre_fleet"), "pre-transition Fleet snapshot")
        post_fleet = _typed_fleet(client)
        target_ref = next(item for item in references if item.lane_id == target.lane_id)
        target_was_active = (
            target_ref.run_id in _all_loaded_runs(pre_fleet)
            if target_ref.run_id
            else False
        )
        active: dict[str, object]
        if target_was_active:
            active = {
                "run_id": target.original_run_id,
                "recipe_key": target.recipe_key,
                "recipe_content_sha256": target.recipe_content_sha256,
                "package_sha256": target.package_sha256,
                "recipe_revision_id": target.recipe_revision_id,
                "alias": target.alias,
                "node_to_rank": dict(target.node_to_rank),
                "reactivated": False,
            }
        else:
            final_by_recipe = _lane_final_verifications(application, [lane], preview)
            final = final_by_recipe[lane.row.key]
            raw_ranks = final.get("ranks")
            if not isinstance(raw_ranks, list):
                raise QualificationError(
                    "reactivated lane lacks final Spark rank receipts"
                )
            node_to_rank: dict[str, int] = {}
            for raw_rank in raw_ranks:
                rank = _object(raw_rank, "reactivated lane final rank")
                node_id = _string(rank.get("node_id"), "reactivated lane Spark ID")
                rank_value = rank.get("rank")
                if type(rank_value) is not int or node_id in node_to_rank:
                    raise QualificationError("reactivated lane rank receipt is invalid")
                node_to_rank[node_id] = rank_value
            if node_to_rank != dict(target.node_to_rank):
                raise QualificationError("reactivated lane changed its exact node/rank")
            active = {
                "run_id": _string(final.get("run_id"), "reactivated lane run ID"),
                "recipe_key": target.recipe_key,
                "recipe_content_sha256": target.recipe_content_sha256,
                "package_sha256": target.package_sha256,
                "recipe_revision_id": target.recipe_revision_id,
                "alias": target.alias,
                "node_to_rank": node_to_rank,
                "reactivated": True,
            }
            presences = _check_serving_fleet(
                post_fleet,
                run_id=str(active["run_id"]),
                revision_id=target.recipe_revision_id,
                alias=target.alias,
                node_ids=[target.node_id],
                expected_run_state="running",
                expected_route_state="published",
                expected_health=True,
            )
            _require_rank_presence_bindings(
                presences, node_to_rank, label="reactivated lane canary"
            )
            boot_id = _boot_id(_nodes(post_fleet)[target.node_id])
            if boot_id is None:
                raise QualificationError("reactivated lane has no live boot identity")
            reactivation_key = _request_key(
                campaign_id,
                f"{target.batch_id}:{target.lane_id}:{active['run_id']}",
                "reactivation-smoke",
            )
            smoke = _run_reactivation_smoke(
                client=client,
                target=target,
                lane=lane,
                fixtures=fixtures,
                ledger=ledger,
                active=active,
                boot_id=boot_id,
                request_key=reactivation_key,
                application_id=application_id,
                campaign_id=campaign_id,
                options=options,
                clock=clock,
                sleeper=sleeper,
            )
            active["reactivation_smoke"] = dict(smoke)
        stop_receipts = _profile_stop_receipts(application)
        expected_stops = _string_array(
            _object(request.get("review"), "reviewed recovery profile").get(
                "stop_run_ids"
            ),
            "reviewed recovery stop run IDs",
        )
        if sorted(str(item.get("run_id")) for item in stop_receipts) != list(
            expected_stops
        ):
            raise QualificationError(
                "recovery application lacks exact partner final-stop receipts"
            )
        updated_at = _string(
            application.get("updated_at"), "recovery application update time"
        )
        return {
            "request_key": request_key,
            "review_digest": request.get("review_digest"),
            "application_state": "succeeded",
            "application_id": application_id,
            "profile_number": profile_number,
            "profile_id": profile_id,
            "profile_digest": profile_digest,
            "plan_digest": plan_digest,
            "application_updated_at": updated_at,
            "active": active,
            "pre_transition_fleet_snapshot": pre_fleet,
            "fleet_snapshot": post_fleet,
            "partner_stop_receipts": stop_receipts,
        }

    def observe_fleet() -> Mapping[str, object]:
        reviewed = next(
            (
                _object(record.get("payload"), "durable lane recovery review")
                for record in reversed(
                    ledger.recipe_records(campaign_id, target.recipe_key)
                )
                if record.get("event") == "lane_recovery.plan_reviewed"
                and _record_payload_matches(
                    record,
                    {"batch_id": target.batch_id, "lane_id": target.lane_id},
                )
            ),
            None,
        )
        if reviewed is None:
            raise QualificationError("lane recovery has no durable reviewed transition")
        review_body = _object(reviewed.get("review"), "durable reviewed profile plan")
        profile = _profile_view(client, profile_number)
        return {
            "snapshot_freshness": "live",
            "profile_number": profile_number,
            "profile_id": profile.get("id"),
            "profile_digest": profile.get("profile_digest"),
            "plan_digest": review_body.get("plan_digest"),
            "fleet_snapshot": _typed_fleet(client),
        }

    def verify_serving(request: Mapping[str, object]) -> Mapping[str, object]:
        active = _object(request.get("active"), "lane serving identity")
        run_id = _string(active.get("run_id"), "lane serving run ID")
        boot_id = _string(request.get("boot_id"), "lane serving boot ID")
        fleet = _typed_fleet(client)
        presences = _check_serving_fleet(
            fleet,
            run_id=run_id,
            revision_id=target.recipe_revision_id,
            alias=target.alias,
            node_ids=[target.node_id],
            expected_run_state="running",
            expected_route_state="published",
            expected_health=True,
        )
        _require_rank_presence_bindings(
            presences, target.node_to_rank, label="post-restart serving receipt"
        )
        if _boot_id(_nodes(fleet)[target.node_id]) != boot_id:
            raise QualificationError(
                "serving receipt was observed under another Spark boot"
            )
        return {
            "state": "succeeded",
            "run_id": run_id,
            "recipe_key": target.recipe_key,
            "recipe_content_sha256": target.recipe_content_sha256,
            "package_sha256": target.package_sha256,
            "recipe_revision_id": target.recipe_revision_id,
            "alias": target.alias,
            "node_to_rank": dict(target.node_to_rank),
            "boot_id": boot_id,
            "route_state": "published",
            "healthy": True,
        }

    def run_fixture_smoke(request: Mapping[str, object]) -> Mapping[str, object]:
        active = _object(request.get("active"), "recovered lane identity")
        run_id = _string(active.get("run_id"), "recovered lane run ID")
        boot_id = _string(request.get("boot_id"), "recovered lane boot ID")
        case_ids = _string_array(request.get("case_ids"), "recovery smoke case IDs")
        if list(case_ids) != list(target.smoke_case_ids):
            raise QualificationError(
                "recovery smoke request changed its reviewed cases"
            )
        serving = verify_serving({"active": active, "boot_id": boot_id})
        if serving.get("state") != "succeeded":
            raise QualificationError(
                "exact lane is not serving before post-restart smoke"
            )
        request_key = _string(request.get("request_key"), "recovery smoke request key")
        if lane.smoke_kind == "artifact-job":
            smoke = ArtifactJobSmokeAdapter(fixtures).run(
                client,
                run_id,
                lane.smoke_preview,
                ledger=ledger,
                plan_digest=campaign_id,
                recipe_key=lane.row.key,
                timeout_seconds=options.operation_timeout_seconds,
                poll_interval_seconds=options.poll_interval_seconds,
                clock=clock,
                sleeper=sleeper,
                event_prefix=f"recovery.{target.batch_id}.{target.lane_id}.{run_id}",
            )
            raw_cases = (
                smoke.get("cases") if isinstance(smoke.get("cases"), list) else [smoke]
            )
        else:
            smoke = ServiceSmokeAdapter(fixtures).run(
                client, lane.alias, lane.smoke_preview
            )
            raw_cases = smoke.get("cases")
        if not isinstance(raw_cases, list):
            raise QualificationError("recovery fixture smoke lacks case receipts")
        cases = [
            {
                "case_id": _object(item, "recovery fixture case").get("case_id"),
                "passed": True,
            }
            for item in raw_cases
        ]
        return {
            "request_key": request_key,
            "run_id": run_id,
            "recipe_key": target.recipe_key,
            "recipe_content_sha256": target.recipe_content_sha256,
            "package_sha256": target.package_sha256,
            "recipe_revision_id": target.recipe_revision_id,
            "alias": target.alias,
            "node_to_rank": dict(target.node_to_rank),
            "boot_id": boot_id,
            "cases": cases,
            "fixture_receipt": dict(smoke),
        }

    return {
        "prepare_transition": prepare_transition,
        "transition_to_lane": transition_to_lane,
        "observe_fleet": observe_fleet,
        "verify_serving": verify_serving,
        "run_fixture_smoke": run_fixture_smoke,
        "context": context,
    }


def _durable_lane_recovery_review(
    ledger: EvidenceLedger,
    *,
    campaign_id: str,
    recipe: str,
    batch_id: str,
    lane_id: int,
) -> Mapping[str, object] | None:
    for record in reversed(ledger.recipe_records(campaign_id, recipe)):
        if record.get(
            "event"
        ) == "lane_recovery.plan_reviewed" and _record_payload_matches(
            record, {"batch_id": batch_id, "lane_id": lane_id}
        ):
            return _object(record["payload"], "durable lane recovery review")
    return None


def _review_or_apply_lane_recovery(
    *,
    client: Any,
    batch: CampaignBatch,
    lane_number: int,
    manifest: CampaignManifest,
    fixtures: FixtureRegistry,
    profile_number: int,
    authority_id: str,
    ledger_id: str,
    campaign_id: str,
    ledger: EvidenceLedger,
    apply: bool,
    supplied_digest: str | None,
    args: argparse.Namespace,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> dict[str, object]:
    lanes, _batch_preview, _batch_plan = _durable_batch_lanes(
        client=client,
        batch=batch,
        manifest=manifest,
        fixtures=fixtures,
        ledger=ledger,
        campaign_id=campaign_id,
    )
    fleet = _typed_fleet(client)
    target, lane, references = _lane_recovery_target(
        batch=batch,
        lanes=lanes,
        lane_number=lane_number,
        campaign_id=campaign_id,
        ledger=ledger,
        profile_number=profile_number,
        fleet_node_ids=sorted(_nodes(fleet)),
        fixtures=fixtures,
    )
    callbacks = _lane_recovery_callbacks(
        client=client,
        target=target,
        lane=lane,
        lanes=lanes,
        references=references,
        fixtures=fixtures,
        profile_number=profile_number,
        authority_id=authority_id,
        ledger_id=ledger_id,
        campaign_id=campaign_id,
        ledger=ledger,
        options=manifest,
        clock=clock,
        sleeper=sleeper,
    )
    if apply:
        durable_review = _durable_lane_recovery_review(
            ledger,
            campaign_id=campaign_id,
            recipe=lane.row.key,
            batch_id=batch.batch_id,
            lane_id=lane_number,
        )
        if durable_review is None:
            raise QualificationError(
                "review the exact exclusive lane recovery before applying it"
            )
        expected_digest = _string(
            durable_review.get("review_digest"), "durable recovery review digest"
        )
        if supplied_digest != expected_digest:
            raise QualificationError(
                "--campaign-digest does not match the durable exclusive lane review"
            )
        progress = recover_single_lane(
            target,
            ledger,
            prepare_transition=callbacks["prepare_transition"],
            apply_authorized=True,
            transition_to_lane=callbacks["transition_to_lane"],
            observe_fleet=callbacks["observe_fleet"],
            verify_serving=callbacks["verify_serving"],
            run_fixture_smoke=callbacks["run_fixture_smoke"],
        )
        mode = "apply"
    else:
        review = review_lane_transition(
            target, ledger, prepare_transition=callbacks["prepare_transition"]
        )
        progress = LaneRecoveryProgress("awaiting-explicit-apply", review.target_digest)
        mode = "preview"
    durable_review = _durable_lane_recovery_review(
        ledger,
        campaign_id=campaign_id,
        recipe=lane.row.key,
        batch_id=batch.batch_id,
        lane_id=lane_number,
    )
    review_digest = (
        _string(durable_review.get("review_digest"), "recovery review digest")
        if durable_review is not None
        else None
    )
    if progress.status == "completed":
        status = "cleanup-required"
        next_step: dict[str, object] = {
            "checkpoint": "exclusive-lane-cleanup",
            "lane": lane_number,
            "instruction": "Review and apply --cleanup-lane for this exact lane before recording its acceptance.",
        }
    elif mode == "preview":
        status = "not-accepted"
        next_step = {
            "checkpoint": "exclusive-lane-recovery-apply",
            "lane": lane_number,
            "instruction": "Apply this exact one-lane recovery review with --recover-lane and its campaign digest.",
        }
    else:
        status = "checkpoint-required"
        next_step = {
            "checkpoint": progress.status,
            "lane": lane_number,
            "instruction": progress.reason,
        }
    return {
        "schema_version": 1,
        "mode": mode,
        "status": status,
        "batch_id": batch.batch_id,
        "lane": lane_number,
        "recipe": lane.row.key,
        "campaign_digest": review_digest,
        "review": (
            dict(_object(durable_review.get("review"), "durable recovery review"))
            if durable_review
            else None
        ),
        "recovery": {
            "status": progress.status,
            "target_digest": progress.target_digest,
            "active_run_id": progress.active_run_id,
            "baseline_boot_id": progress.baseline_boot_id,
            "recovered_boot_id": progress.recovered_boot_id,
            "receipt_sha256": progress.receipt_sha256,
            "reason": progress.reason,
        },
        "next": next_step,
        "spark_accepted": False,
    }


def _observe_batch_recovery(
    *,
    client: Any,
    batch: CampaignBatch,
    lanes: Sequence[BatchLane],
    manifest: CampaignManifest,
    fixtures: FixtureRegistry,
    profile_number: int,
    authority_id: str,
    ledger_id: str,
    campaign_id: str,
    ledger: EvidenceLedger,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> dict[str, object]:
    del authority_id, ledger_id
    lane_results: list[dict[str, object]] = []
    next_lane: BatchLane | None = None
    failed_cleanup_lane: BatchLane | None = None
    for lane in lanes:
        if (
            _latest_payload(ledger, campaign_id, lane.row.key, "recipe.spark-accepted")
            is not None
        ):
            lane_results.append(
                {
                    "lane": lane.assignment.lane,
                    "recipe": lane.row.key,
                    "status": "spark-accepted",
                }
            )
            continue
        row_terminal = _row_terminal_block_or_failure(ledger, campaign_id, lane.row)
        if row_terminal is not None:
            lane_results.append(
                {
                    "lane": lane.assignment.lane,
                    "recipe": lane.row.key,
                    "status": row_terminal,
                }
            )
            continue
        canary_outcome = _latest_batch_canary_outcome(
            ledger,
            campaign_id=campaign_id,
            batch_id=batch.batch_id,
            recipe=lane.row.key,
            lane_id=lane.assignment.lane,
        )
        if (
            canary_outcome is not None
            and canary_outcome.get("event") == "canary.failed"
        ):
            lane_results.append(
                {
                    "lane": lane.assignment.lane,
                    "recipe": lane.row.key,
                    "status": "failed-canary-cleanup-required",
                }
            )
            failed_cleanup_lane = failed_cleanup_lane or lane
            continue
        if (
            _durable_lane_recovery_review(
                ledger,
                campaign_id=campaign_id,
                recipe=lane.row.key,
                batch_id=batch.batch_id,
                lane_id=lane.assignment.lane,
            )
            is not None
        ):
            next_lane = lane
            break
        lane_results.append(
            {
                "lane": lane.assignment.lane,
                "recipe": lane.row.key,
                "status": "recovery-not-reviewed",
            }
        )
    if next_lane is None:
        if failed_cleanup_lane is not None:
            return {
                "schema_version": 1,
                "mode": "observe",
                "status": "cleanup-required",
                "batch_id": batch.batch_id,
                "lane_results": lane_results,
                "next": {
                    "checkpoint": "failed-lane-cleanup",
                    "lane": failed_cleanup_lane.assignment.lane,
                    "instruction": "Review and apply --cleanup-lane for the failed lane before recording its recipe-local failure.",
                },
                "spark_accepted": False,
            }
        pending = next(
            (
                item
                for item in lanes
                if _latest_payload(
                    ledger, campaign_id, item.row.key, "recipe.spark-accepted"
                )
                is None
            ),
            None,
        )
        if pending is not None:
            return {
                "schema_version": 1,
                "mode": "observe",
                "status": "recovery-required",
                "batch_id": batch.batch_id,
                "lane_results": lane_results,
                "next": {
                    "checkpoint": "exclusive-lane-recovery-review",
                    "lane": pending.assignment.lane,
                    "instruction": "Review the next exact lane with --recover-lane before applying it.",
                },
                "spark_accepted": False,
            }
        released = _batch_release_recorded(ledger, campaign_id, batch)
        return {
            "schema_version": 1,
            "mode": "observe",
            "status": "batch-complete" if released else "batch-finalization-required",
            "batch_id": batch.batch_id,
            "lane_results": lane_results,
            "next": None
            if released
            else {
                "checkpoint": "explicit-finalization",
                "instruction": "Repeat --cleanup-lane with --apply and the exact reviewed digest to record terminal acceptance and whole-Fleet release.",
            },
            "spark_accepted": released,
        }
    fleet = _typed_fleet(client)
    target, lane, references = _lane_recovery_target(
        batch=batch,
        lanes=lanes,
        lane_number=next_lane.assignment.lane,
        campaign_id=campaign_id,
        ledger=ledger,
        profile_number=profile_number,
        fleet_node_ids=sorted(_nodes(fleet)),
        fixtures=fixtures,
    )
    callbacks = _lane_recovery_callbacks(
        client=client,
        target=target,
        lane=lane,
        lanes=lanes,
        references=references,
        fixtures=fixtures,
        profile_number=profile_number,
        authority_id=manifest.authority.authority_id,
        ledger_id=_ledger_identity(ledger.path),
        campaign_id=campaign_id,
        ledger=ledger,
        options=manifest,
        clock=clock,
        sleeper=sleeper,
    )
    progress = observe_single_lane(
        target,
        ledger,
        observe_fleet=callbacks["observe_fleet"],
        verify_serving=callbacks["verify_serving"],
        run_fixture_smoke=callbacks["run_fixture_smoke"],
    )
    lane_results.append(
        {
            "lane": lane.assignment.lane,
            "recipe": lane.row.key,
            "status": progress.status,
            "baseline_boot_id": progress.baseline_boot_id,
            "recovered_boot_id": progress.recovered_boot_id,
            "reason": progress.reason,
        }
    )
    if progress.status == "completed":
        next_step = {
            "checkpoint": "exclusive-lane-cleanup",
            "lane": lane.assignment.lane,
            "instruction": "Review and apply --cleanup-lane before recording this recipe's acceptance.",
        }
        status = "cleanup-required"
    else:
        next_step = {
            "checkpoint": progress.status,
            "lane": lane.assignment.lane,
            "instruction": progress.reason,
        }
        status = "checkpoint-required"
    return {
        "schema_version": 1,
        "mode": "observe",
        "status": status,
        "batch_id": batch.batch_id,
        "lane_results": lane_results,
        "next": next_step,
        "spark_accepted": False,
    }


def _lane_cleanup_callbacks(
    *,
    client: Any,
    target: LaneRecoveryTarget,
    lane: BatchLane,
    lanes: Sequence[BatchLane],
    references: Sequence[CanaryReference],
    profile_number: int,
    authority_id: str,
    ledger_id: str,
    campaign_id: str,
    ledger: EvidenceLedger,
    options: CampaignManifest,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> dict[str, Any]:
    context: dict[str, object] = {}

    def run_identity(run_id: str) -> tuple[str, list[str], dict[str, int]]:
        for reference in references:
            if reference.run_id == run_id:
                return (
                    _string(reference.alias, "canary cleanup alias"),
                    sorted(
                        reference.node_to_rank
                        or {reference.assigned_node_id: reference.assigned_rank}
                    ),
                    dict(
                        reference.node_to_rank
                        or {reference.assigned_node_id: reference.assigned_rank}
                    ),
                )
        completed = _latest_payload(
            ledger, campaign_id, lane.row.key, "lane_recovery.completed"
        )
        if completed is not None and completed.get("batch_id") == target.batch_id:
            active = _object(
                completed.get("active_assignment"), "recovered lane assignment"
            )
            if active.get("run_id") == run_id:
                alias = _string(active.get("alias"), "recovered lane alias")
                raw_ranks = _object(active.get("node_to_rank"), "recovered lane ranks")
                ranks = {
                    _string(node, "recovered Spark ID"): _integer(
                        rank, "recovered rank", 0, 1
                    )
                    for node, rank in raw_ranks.items()
                }
                return alias, sorted(ranks), ranks
        raise QualificationError(
            "cleanup request names a run without exact campaign identity"
        )

    def prepare_cleanup(request: Mapping[str, object]) -> Mapping[str, object]:
        request_key = _string(request.get("request_key"), "cleanup request key")
        expected_stop_run_ids = _string_array(
            request.get("expected_stop_run_ids"), "cleanup candidate run IDs"
        )
        expected_stop_aliases = _string_array(
            request.get("expected_stop_aliases"), "cleanup candidate aliases"
        )
        if expected_stop_run_ids != tuple(
            sorted(set(expected_stop_run_ids))
        ) or expected_stop_aliases != tuple(sorted(set(expected_stop_aliases))):
            raise QualificationError("cleanup request identities are not canonical")
        fleet = _typed_fleet(client)
        roster = sorted(_nodes(fleet))
        if roster != list(target.fleet_node_ids):
            raise QualificationError(
                "cleanup preview changed the locked whole-Fleet roster"
            )
        live_runs = _all_loaded_runs(fleet)
        if not live_runs <= set(expected_stop_run_ids):
            raise QualificationError(
                "cleanup Fleet contains a workload outside its reviewed release set"
            )
        active_run_id = _string(request.get("active_run_id"), "cleanup active run ID")
        if active_run_id not in live_runs:
            raise QualificationError(
                "the exact terminal lane run is absent; reconcile it before cleanup"
            )
        identities = {run_id: run_identity(run_id) for run_id in expected_stop_run_ids}
        if {identity[0] for identity in identities.values()} != set(
            expected_stop_aliases
        ):
            raise QualificationError(
                "cleanup candidate aliases differ from their exact runs"
            )
        stop_run_ids = sorted(live_runs)
        stop_aliases = sorted({identities[run_id][0] for run_id in stop_run_ids})
        missing_release_ids = sorted(set(expected_stop_run_ids) - live_runs)
        released_run_proofs = list(
            prior_partner_release_proofs(
                target,
                ledger,
                fleet,
                candidate_run_ids=missing_release_ids,
            )
        )
        for run_id in live_runs:
            alias, expected_nodes, expected_ranks = identities[run_id]
            presences = _run_presences(fleet, run_id)
            observed_ranks: dict[str, int] = {}
            for node_id, presence in presences:
                rank = presence.get("rank")
                if (
                    presence.get("alias") != alias
                    or type(rank) is not int
                    or node_id in observed_ranks
                ):
                    raise QualificationError(
                        "cleanup run presence changed its exact identity"
                    )
                observed_ranks[node_id] = rank
            if (
                observed_ranks != expected_ranks
                or sorted(observed_ranks) != expected_nodes
            ):
                raise QualificationError(
                    "cleanup Fleet run changed its exact node/rank assignment"
                )
        profile = _profile_view(client, profile_number)
        _assert_profile_owner(profile, authority_id, ledger_id)
        if profile.get("installation_policy") != "keep-cached":
            raise QualificationError(
                "dedicated cleanup profile changed its cache policy"
            )
        profile_is_prior_cleanup = bool(
            released_run_proofs
        ) and _profile_assignments_equal(profile, [])
        if (
            not _profile_assignment_state(profile, lanes)
            and not profile_is_prior_cleanup
            and not _profile_assignments_equal(profile, [])
        ):
            raise QualificationError(
                "dedicated cleanup profile changed outside this campaign"
            )
        if not _profile_assignments_equal(profile, []):
            profile = _save_profile(
                client,
                profile,
                assignments=[],
                authority_id=authority_id,
                ledger_id=ledger_id,
            )
        after_save = _typed_fleet(client)
        if (
            sorted(_nodes(after_save)) != roster
            or _all_loaded_runs(after_save) != live_runs
        ):
            raise QualificationError(
                "campaign workloads changed while saving cleanup profile"
            )
        raw_preview = client.request("POST", f"/api/profile/{profile_number}/preview")
        preview_fleet = _typed_fleet(client)
        if (
            sorted(_nodes(preview_fleet)) != roster
            or _all_loaded_runs(preview_fleet) != live_runs
        ):
            raise QualificationError(
                "campaign workloads changed while reviewing cleanup profile"
            )
        try:
            preview = FleetProfilePreview.from_dict(raw_preview).to_dict()
        except (KeyError, TypeError, ValueError) as error:
            raise QualificationError("Controller cleanup preview is invalid") from error
        if preview.get("allowed") is not True:
            raise QualificationError(
                f"cleanup profile is blocked: {preview.get('reasons')}"
            )
        scope = _object(preview.get("scope"), "cleanup preview scope")
        summary = _object(preview.get("summary"), "cleanup preview summary")
        if (
            scope.get("node_ids") != roster
            or scope.get("idle_node_ids") != roster
            or summary.get("stops") != len(live_runs)
            or summary.get("starts") != 0
            or summary.get("placements") != 0
            or summary.get("builds") != 0
            or summary.get("distributions") != 0
            or summary.get("installs") != 0
            or summary.get("uninstalls") != 0
        ):
            raise QualificationError(
                "cleanup preview contains effects beyond stopping exact campaign runs"
            )
        effects = _object(preview.get("effects"), "cleanup effects")
        raw_effects = effects.get("runs")
        if not isinstance(raw_effects, list) or len(raw_effects) != len(live_runs):
            raise QualificationError("cleanup preview lacks exact active run stops")
        effect_ids: set[str] = set()
        effect_nodes: set[str] = set()
        for raw_effect in raw_effects:
            effect = _object(raw_effect, "cleanup run effect")
            run_id = _string(effect.get("run_id"), "cleanup effect run ID")
            alias, expected_nodes, _ranks = identities.get(run_id, (None, [], {}))
            nodes = _string_array(effect.get("node_ids"), "cleanup effect Spark IDs")
            if (
                run_id in effect_ids
                or run_id not in live_runs
                or effect.get("action") != "stop"
                or effect.get("alias") != alias
                or list(nodes) != expected_nodes
            ):
                raise QualificationError(
                    "cleanup preview changed an exact active run stop"
                )
            effect_ids.add(run_id)
            effect_nodes.update(nodes)
        if effect_ids != live_runs:
            raise QualificationError("cleanup preview omits a live campaign run")
        steps = preview.get("steps")
        if not isinstance(steps, list):
            raise QualificationError("cleanup preview has invalid plan steps")
        if live_runs:
            if len(steps) != 1:
                raise QualificationError(
                    "cleanup must be one reviewed whole-Fleet switch"
                )
            step = _object(steps[0], "cleanup switch step")
            if (
                step.get("kind") != "switch"
                or set(_string_array(step.get("node_ids"), "cleanup switch Spark IDs"))
                != effect_nodes
            ):
                raise QualificationError(
                    "cleanup switch exceeds its exact campaign run scope"
                )
        elif steps:
            raise QualificationError(
                "empty cleanup preview contains unreviewed plan steps"
            )
        if not _profile_assignments_equal(profile, []):
            raise QualificationError("cleanup profile retained a workload assignment")
        receipt = {
            "request_key": request_key,
            "reviewed": True,
            "profile_number": profile_number,
            "profile_id": _string(profile.get("id"), "cleanup profile ID"),
            "profile_digest": _string(
                profile.get("profile_digest"), "cleanup profile digest"
            ),
            "plan_digest": _string(preview.get("plan_digest"), "cleanup plan digest"),
            "fleet_node_ids": roster,
            "cleanup_mode": request.get("cleanup_mode"),
            "terminal_event": request.get("terminal_event"),
            "terminal_record_sha256": request.get("terminal_record_sha256"),
            "active_run_id": request.get("active_run_id"),
            "stop_run_ids": list(stop_run_ids),
            "stop_aliases": list(stop_aliases),
            "released_run_proofs": released_run_proofs,
            "pre_cleanup_fleet_snapshot": preview_fleet,
        }
        context.clear()
        context.update(
            {
                "receipt": receipt,
                "preview": preview,
                "profile": profile,
                "pre_fleet": preview_fleet,
                "identities": identities,
                "live_runs": sorted(live_runs),
                "released_run_proofs": released_run_proofs,
            }
        )
        return receipt

    def cleanup_to_idle(request: Mapping[str, object]) -> Mapping[str, object]:
        resumed_review = request.get("resume_reviewed") is True
        receipt = (
            _object(request.get("review"), "durable cleanup review")
            if resumed_review
            else _object(context.get("receipt"), "prepared cleanup review")
        )
        preview = (
            _object(context.get("preview"), "prepared cleanup preview")
            if not resumed_review
            else {}
        )
        profile = (
            _object(context.get("profile"), "prepared cleanup profile")
            if not resumed_review
            else {
                "id": request.get("profile_id"),
                "profile_digest": request.get("profile_digest"),
            }
        )
        review_digest = (
            _string(request.get("review_digest"), "durable cleanup review digest")
            if resumed_review
            else _digest(receipt)
        )
        if (
            request.get("review_digest") != review_digest
            or request.get("review") != receipt
            or request.get("request_key") != receipt.get("request_key")
            or request.get("released_run_proofs") != receipt.get("released_run_proofs")
        ):
            raise QualificationError(
                "cleanup apply differs from its exact reviewed plan"
            )
        request_key = _string(request.get("request_key"), "cleanup request key")
        profile_id = _string(profile.get("id"), "cleanup profile ID")
        profile_digest = _string(
            profile.get("profile_digest"), "cleanup profile digest"
        )
        plan_digest = _string(
            request.get("plan_digest")
            if resumed_review
            else preview.get("plan_digest"),
            "cleanup plan digest",
        )
        accepted = _lookup_load_request(
            client,
            profile_number,
            request_key,
            plan_digest=plan_digest,
            profile_id=profile_id,
            profile_digest=profile_digest,
        )
        if accepted is None:
            current_profile = _profile_view(client, profile_number)
            _assert_profile_owner(current_profile, authority_id, ledger_id)
            if (
                current_profile.get("id") != profile_id
                or current_profile.get("profile_digest") != profile_digest
                or not _profile_assignments_equal(current_profile, [])
            ):
                raise QualificationError(
                    "cleanup profile changed after its exact review"
                )
            current_fleet = _typed_fleet(client)
            if _all_loaded_runs(current_fleet) != set(
                _string_array(request.get("stop_run_ids"), "reviewed cleanup run IDs")
            ):
                raise QualificationError("Fleet workloads changed after cleanup review")
            accepted = _submit_load(
                client,
                profile_number,
                request_key,
                plan_digest=plan_digest,
                profile_id=profile_id,
                profile_digest=profile_digest,
            )
        application = _await_application(
            client,
            accepted,
            ledger=ledger,
            campaign_id=campaign_id,
            key=f"cleanup:{target.batch_id}:{target.lane_id}",
            timeout=options.operation_timeout_seconds,
            interval=options.poll_interval_seconds,
            clock=clock,
            sleeper=sleeper,
        )
        if (
            application.get("state") != "succeeded"
            or application.get("profile_id") != profile_id
            or application.get("profile_digest") != profile_digest
            or application.get("plan_digest")
            != _application_plan_digest(plan_digest, request_key)
            or application.get("request_key") != request_key
        ):
            raise QualificationError(
                "cleanup application changed its exact reviewed identity"
            )
        application_id = _string(application.get("id"), "cleanup application ID")
        stop_receipts = _profile_stop_receipts(application)
        expected_stop_ids = list(
            _string_array(request.get("stop_run_ids"), "reviewed cleanup run IDs")
        )
        if (
            sorted(str(item.get("run_id")) for item in stop_receipts)
            != expected_stop_ids
        ):
            raise QualificationError(
                "cleanup application lacks exact final stop receipts"
            )
        post_fleet = _typed_fleet(client)
        if _all_loaded_runs(post_fleet):
            raise QualificationError(
                "cleanup Fleet snapshot still contains a loaded run"
            )
        published = {
            str(presence.get("alias"))
            for node in _nodes(post_fleet).values()
            for presence in _loaded_presences(node)
            if presence.get("route_state") == "published"
        }
        if published or any(
            _endpoint_exists(client, alias)
            for alias in _string_array(
                request.get("stop_aliases"), "cleanup stop aliases"
            )
        ):
            raise QualificationError(
                "cleanup Fleet snapshot still has a published route"
            )
        return {
            "request_key": request_key,
            "review_digest": review_digest,
            "application_state": "succeeded",
            "application_id": application_id,
            "profile_number": profile_number,
            "profile_id": profile_id,
            "profile_digest": profile_digest,
            "plan_digest": plan_digest,
            "application_updated_at": _string(
                application.get("updated_at"), "cleanup application update time"
            ),
            "stop_receipts": stop_receipts,
            "released_run_proofs": list(
                _object_list(request.get("released_run_proofs"), "released run proofs")
            ),
            "fleet_snapshot": post_fleet,
        }

    def reconcile_cleanup(request: Mapping[str, object]) -> Mapping[str, object]:
        """Resume a completed cleanup by reading only its original request."""
        if request.get("resume_completed") is not True:
            raise QualificationError("cleanup reconciliation lacks its resume marker")
        request_key = _string(request.get("request_key"), "cleanup request key")
        review_digest = _string(request.get("review_digest"), "cleanup review digest")
        profile_number_value = _integer(
            request.get("profile_number"), "cleanup profile number", 1, 2**31 - 1
        )
        profile_id = _string(request.get("profile_id"), "cleanup profile ID")
        profile_digest = _string(
            request.get("profile_digest"), "cleanup profile digest"
        )
        plan_digest = _string(request.get("plan_digest"), "cleanup plan digest")
        application_id = _string(
            request.get("application_id"), "persisted cleanup application ID"
        )
        application_updated_at = _string(
            request.get("application_updated_at"), "persisted cleanup application time"
        )
        stop_run_ids = _string_array(
            request.get("stop_run_ids"), "persisted cleanup run IDs"
        )
        stop_aliases = _string_array(
            request.get("stop_aliases"), "persisted cleanup route aliases"
        )
        released_run_proofs = request.get("released_run_proofs")
        if not isinstance(released_run_proofs, list):
            raise QualificationError("persisted cleanup release proofs are invalid")
        persisted_receipt = _object(
            request.get("persisted_receipt"), "persisted cleanup receipt"
        )
        if (
            profile_number_value != profile_number
            or persisted_receipt.get("request_key") != request_key
            or persisted_receipt.get("review_digest") != review_digest
            or persisted_receipt.get("application_id") != application_id
            or persisted_receipt.get("application_updated_at") != application_updated_at
            or persisted_receipt.get("profile_number") != profile_number_value
            or persisted_receipt.get("profile_id") != profile_id
            or persisted_receipt.get("profile_digest") != profile_digest
            or persisted_receipt.get("plan_digest") != plan_digest
            or persisted_receipt.get("released_run_proofs") != released_run_proofs
        ):
            raise QualificationError(
                "persisted cleanup reconciliation changed its application"
            )
        accepted = _lookup_load_request(
            client,
            profile_number_value,
            request_key,
            plan_digest=plan_digest,
            profile_id=profile_id,
            profile_digest=profile_digest,
        )
        if accepted is None or accepted.get("id") != application_id:
            raise QualificationError(
                "the original cleanup request is absent or resolves to another application"
            )
        application = _await_application(
            client,
            accepted,
            ledger=ledger,
            campaign_id=campaign_id,
            key=f"cleanup:{target.batch_id}:{target.lane_id}",
            timeout=options.operation_timeout_seconds,
            interval=options.poll_interval_seconds,
            clock=clock,
            sleeper=sleeper,
        )
        if (
            application.get("id") != application_id
            or application.get("updated_at") != application_updated_at
            or application.get("state") != "succeeded"
            or application.get("profile_id") != profile_id
            or application.get("profile_digest") != profile_digest
            or application.get("plan_digest")
            != _application_plan_digest(plan_digest, request_key)
            or application.get("request_key") != request_key
        ):
            raise QualificationError(
                "reconciled cleanup application changed its durable identity"
            )
        stop_receipts = _profile_stop_receipts(application)
        if sorted(str(item.get("run_id")) for item in stop_receipts) != list(
            stop_run_ids
        ):
            raise QualificationError(
                "reconciled cleanup lost an exact final stop receipt"
            )
        post_fleet = _typed_fleet(client)
        if _all_loaded_runs(post_fleet):
            raise QualificationError(
                "reconciled cleanup Fleet still contains a loaded run"
            )
        published = {
            str(presence.get("alias"))
            for node in _nodes(post_fleet).values()
            for presence in _loaded_presences(node)
            if presence.get("route_state") == "published"
        }
        if published or any(_endpoint_exists(client, alias) for alias in stop_aliases):
            raise QualificationError(
                "reconciled cleanup Fleet still contains a published route"
            )
        return {
            "request_key": request_key,
            "review_digest": review_digest,
            "application_state": "succeeded",
            "application_id": application_id,
            "profile_number": profile_number_value,
            "profile_id": profile_id,
            "profile_digest": profile_digest,
            "plan_digest": plan_digest,
            "application_updated_at": application_updated_at,
            "stop_receipts": stop_receipts,
            "released_run_proofs": released_run_proofs,
            "fleet_snapshot": post_fleet,
        }

    return {
        "prepare_cleanup": prepare_cleanup,
        "cleanup_to_idle": cleanup_to_idle,
        "reconcile_cleanup": reconcile_cleanup,
        "context": context,
    }


def _durable_lane_cleanup_review(
    ledger: EvidenceLedger,
    *,
    campaign_id: str,
    recipe: str,
    batch_id: str,
    lane_id: int,
) -> Mapping[str, object] | None:
    for record in reversed(ledger.recipe_records(campaign_id, recipe)):
        if record.get(
            "event"
        ) == "lane_recovery.cleanup.plan_reviewed" and _record_payload_matches(
            record, {"batch_id": batch_id, "lane_id": lane_id}
        ):
            return _object(record["payload"], "durable lane cleanup review")
    return None


def _record_lane_acceptance(
    *,
    batch: CampaignBatch,
    lane: BatchLane,
    campaign_id: str,
    ledger: EvidenceLedger,
    target: LaneRecoveryTarget,
    client: Any,
    manifest: CampaignManifest,
) -> dict[str, object]:
    accepted = _latest_payload(
        ledger, campaign_id, lane.row.key, "recipe.spark-accepted"
    )
    if accepted is not None:
        if (
            accepted.get("batch_id") != batch.batch_id
            or accepted.get("lane") != lane.assignment.lane
        ):
            raise QualificationError(
                "existing acceptance belongs to another authority lane"
            )
        return {"status": "spark-accepted", **dict(accepted)}
    records = ledger.recipe_records(campaign_id, lane.row.key)
    canary_record = next(
        (
            record
            for record in reversed(records)
            if record.get("event") == "canary.completed"
            and _record_payload_matches(
                record,
                {"batch_id": batch.batch_id, "lane_id": lane.assignment.lane},
            )
        ),
        None,
    )
    if canary_record is None:
        raise QualificationError("lane acceptance lacks its exact passing canary")
    canary = _object(canary_record.get("payload"), "lane canary receipt")
    ranks = _object(canary.get("node_to_rank"), "lane canary rank map")
    smoke = _object(canary.get("smoke"), "lane smoke receipt")
    raw_cases = smoke.get("cases")
    case_ids = (
        [_object(item, "lane smoke case").get("case_id") for item in raw_cases]
        if isinstance(raw_cases, list)
        else [smoke.get("case_id")]
    )
    if (
        canary.get("recipe_content_sha256") != lane.row.content_sha256
        or canary.get("package_sha256") != lane.row.package.get("sha256")
        or canary.get("alias") != lane.alias
        or canary.get("lane_id") != lane.assignment.lane
        or canary.get("node_ids") != list(lane.node_ids)
        or dict(ranks) != dict(target.node_to_rank)
        or case_ids != list(lane.row.smoke_cases)
    ):
        raise QualificationError(
            "lane canary changed its exact recipe, assignment, or fixtures"
        )
    recovery_record = next(
        (
            record
            for record in reversed(records)
            if record.get("event") == "lane_recovery.completed"
            and _record_payload_matches(
                record,
                {"batch_id": batch.batch_id, "lane_id": lane.assignment.lane},
            )
        ),
        None,
    )
    cleanup_record = next(
        (
            record
            for record in reversed(records)
            if record.get("event") == "lane_recovery.cleanup.completed"
            and _record_payload_matches(
                record,
                {"batch_id": batch.batch_id, "lane_id": lane.assignment.lane},
            )
        ),
        None,
    )
    if recovery_record is None or cleanup_record is None:
        raise QualificationError(
            "lane acceptance requires complete recovery and release evidence"
        )
    recovery = _object(recovery_record.get("payload"), "completed lane recovery")
    receipt_body = dict(recovery)
    receipt_digest = receipt_body.pop("receipt_sha256", None)
    if not isinstance(receipt_digest, str) or _digest(receipt_body) != receipt_digest:
        raise QualificationError("completed recovery receipt digest is invalid")
    event_refs = _object(recovery.get("event_refs"), "recovery source event references")
    expected_recovery_events = {
        "intent": "lane_recovery.intent",
        "transitioned": "lane_recovery.transitioned",
        "baseline": "lane_recovery.baseline",
        "offline": "lane_recovery.offline_observed",
        "boot_observed": "lane_recovery.boot_observed",
        "recovered": "lane_recovery.recovered",
        "smoke": "lane_recovery.smoke_completed",
    }
    record_by_sha = {str(record.get("record_sha256")): record for record in records}
    for name, event in expected_recovery_events.items():
        reference = event_refs.get(name)
        record = (
            record_by_sha.get(str(reference)) if isinstance(reference, str) else None
        )
        if record is None or record.get("event") != event:
            raise QualificationError(
                "recovery receipt references missing or wrong ledger evidence"
            )
        payload = dict(_object(record.get("payload"), "recovery source event"))
        if (
            payload.get("batch_id") != batch.batch_id
            or payload.get("lane_id") != lane.assignment.lane
        ):
            raise QualificationError("recovery source event belongs to another lane")
    cleanup = _object(cleanup_record.get("payload"), "lane cleanup completion")
    cleanup_receipt = _object(cleanup.get("receipt"), "lane cleanup receipt")
    if (
        cleanup.get("review_digest") is None
        or cleanup_receipt.get("application_state") != "succeeded"
        or not cleanup_receipt.get("application_id")
    ):
        raise QualificationError(
            "lane cleanup evidence is not a successful reviewed release"
        )
    coverage_receipts = _recovery_coverage_receipts(
        lane=lane,
        batch=batch,
        campaign_id=campaign_id,
        ledger=ledger,
        client=client,
        manifest=manifest,
    )
    payload: dict[str, object] = {
        "batch_id": batch.batch_id,
        "lane": lane.assignment.lane,
        "recipe_content_sha256": lane.row.content_sha256,
        "package_sha256": lane.row.package.get("sha256"),
        "node_count": lane.row.node_count,
        "node_ids": list(lane.node_ids),
        "canary_record_sha256": canary_record.get("record_sha256"),
        "recovery_record_sha256": recovery_record.get("record_sha256"),
        "recovery_receipt_sha256": receipt_digest,
        "cleanup_record_sha256": cleanup_record.get("record_sha256"),
        "recovery_coverage_receipts": coverage_receipts,
        "acceptance_gate": "paired-canary-active-workload-restart-postrestart-smoke-and-whole-fleet-release",
    }
    ledger.append(
        "recipe.spark-accepted",
        plan_digest=campaign_id,
        recipe=lane.row.key,
        payload=payload,
    )
    return {"status": "spark-accepted", **payload}


def _record_dual_lane_acceptance(
    *,
    batch: CampaignBatch,
    lane: BatchLane,
    campaign_id: str,
    ledger: EvidenceLedger,
    target: DualRecoveryTarget,
    manifest: CampaignManifest,
    fixtures: FixtureRegistry,
    client: Any,
) -> dict[str, object]:
    accepted = _latest_payload(
        ledger, campaign_id, lane.row.key, "recipe.spark-accepted"
    )
    if accepted is not None:
        if (
            accepted.get("batch_id") != batch.batch_id
            or accepted.get("lane") != lane.assignment.lane
        ):
            raise QualificationError(
                "existing acceptance belongs to another exclusive-dual lane"
            )
        return {"status": "spark-accepted", **dict(accepted)}

    def exact_record(event: str) -> Mapping[str, object]:
        matches = [
            record
            for record in ledger.recipe_records(campaign_id, lane.row.key)
            if record.get("event") == event
            and _record_payload_matches(
                record,
                {"batch_id": batch.batch_id, "lane_id": lane.assignment.lane},
            )
        ]
        if len(matches) != 1:
            raise QualificationError(
                f"dual acceptance requires exactly one {event} receipt"
            )
        return matches[0]

    canary = exact_record("canary.completed")
    rank_recovery = exact_record("rank-recovery.smoke-completed")
    completion = exact_record("dual_recovery.completed")
    cleanup = exact_record("dual_recovery.cleanup.completed")
    completion_payload = _object(completion.get("payload"), "dual recovery completion")
    cleanup_payload = _object(cleanup.get("payload"), "dual cleanup completion")
    cleanup_receipt = _object(cleanup_payload.get("receipt"), "dual cleanup receipt")
    if (
        completion_payload.get("canary_record_sha256") != canary.get("record_sha256")
        or cleanup_payload.get("terminal_record_sha256")
        != rank_recovery.get("record_sha256")
        or cleanup_receipt.get("application_state") != "succeeded"
        or not isinstance(cleanup_receipt.get("application_id"), str)
    ):
        raise QualificationError(
            "dual acceptance is detached from its exact recovery and cleanup"
        )
    coverage_receipts = _recovery_coverage_receipts(
        lane=lane,
        batch=batch,
        campaign_id=campaign_id,
        ledger=ledger,
        client=client,
        manifest=manifest,
    )
    payload: dict[str, object] = {
        "batch_id": batch.batch_id,
        "lane": lane.assignment.lane,
        "recipe_content_sha256": lane.row.content_sha256,
        "package_sha256": lane.row.package.get("sha256"),
        "node_count": lane.row.node_count,
        "node_ids": list(lane.node_ids),
        "canary_record_sha256": canary.get("record_sha256"),
        "dual_recovery_record_sha256": completion.get("record_sha256"),
        "dual_cleanup_record_sha256": cleanup.get("record_sha256"),
        "recovery_coverage_receipts": coverage_receipts,
        "acceptance_gate": "exclusive-dual-rank-loss-recovery-smoke-offline-idle-restart-and-whole-fleet-release",
    }
    record = ledger.append(
        "recipe.spark-accepted",
        plan_digest=campaign_id,
        recipe=lane.row.key,
        payload=payload,
    )
    return {
        "status": "spark-accepted",
        **payload,
        "record_sha256": record.get("record_sha256"),
    }


def _review_or_apply_lane_cleanup(
    *,
    client: Any,
    batch: CampaignBatch,
    lane_number: int,
    manifest: CampaignManifest,
    fixtures: FixtureRegistry,
    profile_number: int,
    authority_id: str,
    ledger_id: str,
    campaign_id: str,
    ledger: EvidenceLedger,
    apply: bool,
    supplied_digest: str | None,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> dict[str, object]:
    lanes, _batch_preview, _batch_plan = _durable_batch_lanes(
        client=client,
        batch=batch,
        manifest=manifest,
        fixtures=fixtures,
        ledger=ledger,
        campaign_id=campaign_id,
    )
    fleet = _typed_fleet(client)
    target, lane, references = _lane_recovery_target(
        batch=batch,
        lanes=lanes,
        lane_number=lane_number,
        campaign_id=campaign_id,
        ledger=ledger,
        profile_number=profile_number,
        fleet_node_ids=sorted(_nodes(fleet)),
        fixtures=fixtures,
        allow_failed_own_lane=True,
    )
    callbacks = _lane_cleanup_callbacks(
        client=client,
        target=target,
        lane=lane,
        lanes=lanes,
        references=references,
        profile_number=profile_number,
        authority_id=authority_id,
        ledger_id=ledger_id,
        campaign_id=campaign_id,
        ledger=ledger,
        options=manifest,
        clock=clock,
        sleeper=sleeper,
    )
    if apply:
        durable_review = _durable_lane_cleanup_review(
            ledger,
            campaign_id=campaign_id,
            recipe=lane.row.key,
            batch_id=batch.batch_id,
            lane_id=lane_number,
        )
        if durable_review is None:
            raise QualificationError("review exact lane cleanup before applying it")
        expected_digest = _string(
            durable_review.get("review_digest"), "cleanup review digest"
        )
        if supplied_digest != expected_digest:
            raise QualificationError(
                "--campaign-digest does not match the durable cleanup review"
            )
        progress = record_lane_cleanup(
            target,
            ledger,
            prepare_cleanup=callbacks["prepare_cleanup"],
            apply_authorized=True,
            cleanup_to_idle=callbacks["cleanup_to_idle"],
            reconcile_cleanup=callbacks["reconcile_cleanup"],
        )
        mode = "apply"
    else:
        review = review_lane_cleanup(
            target, ledger, prepare_cleanup=callbacks["prepare_cleanup"]
        )
        progress = LaneRecoveryProgress(
            "awaiting-explicit-cleanup", review.target_digest
        )
        mode = "preview"
    durable_review = _durable_lane_cleanup_review(
        ledger,
        campaign_id=campaign_id,
        recipe=lane.row.key,
        batch_id=batch.batch_id,
        lane_id=lane_number,
    )
    review_digest = (
        _string(durable_review.get("review_digest"), "cleanup review digest")
        if durable_review is not None
        else None
    )
    result: dict[str, object] = {
        "schema_version": 1,
        "mode": mode,
        "status": "not-accepted" if mode == "preview" else "cleanup-completed",
        "batch_id": batch.batch_id,
        "lane": lane_number,
        "recipe": lane.row.key,
        "campaign_digest": review_digest,
        "cleanup": {
            "status": progress.status,
            "target_digest": progress.target_digest,
            "receipt_sha256": progress.receipt_sha256,
            "reason": progress.reason,
        },
        "spark_accepted": False,
    }
    if progress.status == "cleanup-completed":
        own_reference = next(
            (item for item in references if item.lane_id == lane_number),
            None,
        )
        if own_reference is not None and own_reference.outcome_event == "canary.failed":
            canary_failure = next(
                item
                for item in reversed(ledger.recipe_records(campaign_id, lane.row.key))
                if item.get("record_sha256") == own_reference.record_sha256
            )
            cleanup_record = next(
                item
                for item in reversed(ledger.recipe_records(campaign_id, lane.row.key))
                if item.get("event") == "lane_recovery.cleanup.completed"
                and _record_payload_matches(
                    item, {"batch_id": batch.batch_id, "lane_id": lane_number}
                )
            )
            failure_payload = {
                "batch_id": batch.batch_id,
                "lane": lane_number,
                "recipe_content_sha256": lane.row.content_sha256,
                "package_sha256": lane.row.package.get("sha256"),
                "canary_record_sha256": canary_failure.get("record_sha256"),
                "cleanup_record_sha256": cleanup_record.get("record_sha256"),
                "error": _object(canary_failure.get("payload"), "canary failure").get(
                    "error"
                ),
                "disposition": "failed-after-reviewed-cleanup",
            }
            if (
                _latest_payload(ledger, campaign_id, lane.row.key, "recipe.failed")
                is None
            ):
                ledger.append(
                    "recipe.failed",
                    plan_digest=campaign_id,
                    recipe=lane.row.key,
                    payload=failure_payload,
                )
            result["lane_outcome"] = {"status": "recipe.failed", **failure_payload}
        else:
            result["acceptance"] = _record_lane_acceptance(
                batch=batch,
                lane=lane,
                campaign_id=campaign_id,
                ledger=ledger,
                target=target,
                client=client,
                manifest=manifest,
            )
        all_terminal = all(
            _row_terminal_block_or_failure(
                ledger,
                campaign_id,
                item.row,
            )
            is not None
            for item in lanes
        )
        if all_terminal:
            _record_batch_release(
                client=client,
                profile_number=profile_number,
                authority_id=authority_id,
                ledger_id=ledger_id,
                batch=batch,
                campaign_id=campaign_id,
                ledger=ledger,
            )
            result["status"] = "lane-terminal"
            result["spark_accepted"] = all(
                _latest_payload(
                    ledger, campaign_id, item.row.key, "recipe.spark-accepted"
                )
                is not None
                for item in lanes
            )
    return result


def _record_batch_release(
    *,
    client: Any,
    profile_number: int,
    authority_id: str,
    ledger_id: str,
    batch: CampaignBatch,
    campaign_id: str,
    ledger: EvidenceLedger,
) -> Mapping[str, object]:
    """Close the batch barrier only after fresh whole-Fleet reconciliation."""
    existing = _latest_batch_payload(
        ledger, campaign_id, batch.batch_id, "batch.cleanup.completed"
    )
    fleet = _typed_fleet(client)
    roster = sorted(_nodes(fleet))
    aliases: set[str] = set()
    batch_run_ids: set[str] = set()
    for assignment in batch.assignments:
        for event_name in ("canary.completed", "canary.failed"):
            outcome = _latest_payload(
                ledger, campaign_id, assignment.recipe, event_name
            )
            if outcome is not None and outcome.get("batch_id") == batch.batch_id:
                if isinstance(outcome.get("alias"), str):
                    aliases.add(str(outcome["alias"]))
                if isinstance(outcome.get("run_id"), str):
                    batch_run_ids.add(str(outcome["run_id"]))
    loaded_run_ids = sorted(_all_loaded_runs(fleet))
    published_route_aliases = sorted(
        {
            str(presence.get("alias"))
            for node in _nodes(fleet).values()
            for presence in _loaded_presences(node)
            if presence.get("route_state") == "published"
            and isinstance(presence.get("alias"), str)
        }
    )
    if batch_run_ids & set(loaded_run_ids) or any(
        _endpoint_exists(client, alias) for alias in aliases
    ):
        raise QualificationError(
            f"{batch.batch_id} cannot advance until every campaign run and route is absent"
        )
    if loaded_run_ids:
        raise QualificationError(
            "batch cleanup cannot advance while any workload remains loaded across the Fleet"
        )
    if published_route_aliases:
        raise QualificationError(
            "batch cleanup cannot advance while any Fleet route remains published"
        )
    profile = _profile_view(client, profile_number)
    _assert_profile_owner(profile, authority_id, ledger_id)
    if not _profile_assignments_equal(profile, []):
        raise QualificationError(
            "batch cleanup left assignments in its dedicated profile"
        )
    profile_id = _string(profile.get("id"), "released batch profile ID")
    profile_digest = _string(
        profile.get("profile_digest"), "released batch profile digest"
    )
    node_reservations = {
        node_id: dict(_object(node.get("reservations"), "Fleet reservations"))
        for node_id, node in sorted(_nodes(fleet).items())
    }
    payload: dict[str, object] = {
        "batch_id": batch.batch_id,
        "all_batch_runs_absent": True,
        "all_batch_routes_absent": True,
        "profile_assignments_empty": True,
        "profile_number": profile_number,
        "profile_id": profile_id,
        "profile_digest": profile_digest,
        "fleet_node_ids": roster,
        "loaded_run_ids": loaded_run_ids,
        "published_route_aliases": published_route_aliases,
        "batch_run_ids": sorted(batch_run_ids),
        "batch_aliases": sorted(aliases),
        "node_reservations": node_reservations,
        "authority_revision": fleet.get("authority_revision"),
        "event_cursor": fleet.get("event_cursor"),
        "generated_at": fleet.get("generated_at"),
    }
    if existing is not None:
        if any(
            existing.get(key) != payload[key]
            for key in (
                "batch_id",
                "all_batch_runs_absent",
                "all_batch_routes_absent",
                "profile_assignments_empty",
                "profile_number",
                "profile_id",
                "profile_digest",
                "fleet_node_ids",
                "loaded_run_ids",
                "published_route_aliases",
                "batch_run_ids",
                "batch_aliases",
                "node_reservations",
                "authority_revision",
            )
        ):
            raise QualificationError(
                "durable batch cleanup differs from fresh Fleet evidence"
            )
        return existing
    record = ledger.append(
        "batch.cleanup.completed",
        plan_digest=campaign_id,
        recipe=None,
        payload=payload,
    )
    return _object(record.get("payload", payload), "batch cleanup record")


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Qualify one exact authority batch through a dedicated whole-Fleet "
            "profile; paired lane smoke runs concurrently and recovery is exclusive."
        )
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--library-root", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--profile-number", type=int, required=True)
    parser.add_argument("--batch")
    parser.add_argument(
        "--recover-lane",
        type=int,
        help="Review or apply an exclusive recovery transition for this authority lane",
    )
    parser.add_argument(
        "--cleanup-lane",
        type=int,
        help="Review or apply exact batch workload cleanup after lane evidence is complete",
    )
    parser.add_argument("--spark", action="append", default=[])
    parser.add_argument("--failure-spark")
    parser.add_argument("--accept-operator-gate", action="append", default=[])
    parser.add_argument("--accept-capacity-review", action="append", default=[])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--observe", action="store_true")
    parser.add_argument(
        "--campaign-digest",
        help="Required with --apply; copy from the fresh preview for this exact batch and Spark assignment",
    )
    parser.add_argument(
        "--replace-run-id",
        action="append",
        default=[],
        help=(
            "Explicitly acknowledge replacement of this exact currently loaded "
            "run; repeat once for every workload included in the reviewed stop set"
        ),
    )
    args = parser.parse_args(argv)
    if type(args.profile_number) is not int or args.profile_number < 1:
        parser.error("--profile-number must be a positive existing profile number")
    if args.campaign_digest and not args.apply:
        parser.error("--campaign-digest is only valid with --apply")
    if args.apply and not args.campaign_digest:
        parser.error("--apply requires --campaign-digest from a fresh batch preview")
    if not args.apply and (args.accept_operator_gate or args.accept_capacity_review):
        parser.error("review acknowledgements are only accepted with --apply")
    if args.observe and (
        args.spark
        or args.failure_spark
        or args.recover_lane is not None
        or args.cleanup_lane is not None
        or args.accept_operator_gate
        or args.accept_capacity_review
        or args.replace_run_id
    ):
        parser.error(
            "--observe resumes durable evidence for the batch; it takes no Spark, "
            "recovery-lane, replacement, or review-gate flags"
        )
    if args.recover_lane is not None:
        if args.batch is None:
            parser.error("--recover-lane requires --batch")
        if type(args.recover_lane) is not int or not 1 <= args.recover_lane <= 2:
            parser.error("--recover-lane must be an authority lane number (1 or 2)")
        if args.spark or args.failure_spark or args.replace_run_id:
            parser.error(
                "exclusive lane recovery uses durable batch identities; it takes no "
                "--spark, --failure-spark, or --replace-run-id"
            )
    if args.cleanup_lane is not None:
        if args.batch is None:
            parser.error("--cleanup-lane requires --batch")
        if type(args.cleanup_lane) is not int or not 1 <= args.cleanup_lane <= 2:
            parser.error("--cleanup-lane must be an authority lane number (1 or 2)")
        if args.spark or args.failure_spark or args.replace_run_id:
            parser.error(
                "batch cleanup uses durable batch identities; it takes no "
                "--spark, --failure-spark, or --replace-run-id"
            )
    if args.recover_lane is not None and args.cleanup_lane is not None:
        parser.error("--recover-lane and --cleanup-lane are mutually exclusive")
    if (
        not args.observe
        and args.recover_lane is None
        and args.cleanup_lane is None
        and not args.spark
    ):
        parser.error("batch preview and apply require exact --spark IDs")
    return args


def run(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[], Any] = ControlClient.from_environment,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    args = _arguments(argv)
    library_root = args.library_root.resolve(strict=True)
    manifest = load_manifest(args.manifest, library_root)
    if manifest.fixture_manifest.stat().st_size > _MAX_FIXTURE_MANIFEST_BYTES:
        raise QualificationError(
            f"qualification index exceeds its {_MAX_FIXTURE_MANIFEST_BYTES}-byte read bound"
        )
    fixtures = FixtureRegistry.load(manifest.fixture_manifest)
    _bind_repository_inputs(manifest, library_root, fixtures)
    ledger_path = _resolve_ledger_path(args.ledger, library_root)
    if ledger_path in {
        manifest.path,
        manifest.fixture_manifest,
    }:
        raise QualificationError("evidence ledger cannot overwrite a campaign input")
    campaign_id, ledger_id = _make_campaign_id(
        manifest, fixtures, library_root, ledger_path
    )
    client = client_factory()
    ledger = EvidenceLedger(ledger_path)
    batch = _current_batch(manifest, ledger, campaign_id, args.batch)
    if batch is None:
        return {
            "schema_version": 1,
            "status": "complete",
            "accepted_recipe_count": len(_completed_keys(ledger, campaign_id)),
            "scope_recipe_count": len(manifest.authority.rows),
        }
    rows_by_key = {row.key: row for row in manifest.authority.rows}
    batch_rows = [rows_by_key[item.recipe] for item in batch.assignments]
    if batch.mode == "exclusive-dual":
        kind, _definition = _fixture_bindings(batch_rows[0], fixtures)
        if kind != "openai-service":
            raise QualificationError(
                f"{batch_rows[0].key} distributed fault acceptance requires a serving-route smoke fixture"
            )
    elif args.failure_spark is not None:
        raise QualificationError(
            "--failure-spark is only valid for exclusive-dual batches"
        )
    if args.recover_lane is not None and args.recover_lane not in {
        assignment.lane for assignment in batch.assignments
    }:
        raise QualificationError(
            f"lane {args.recover_lane} is not present in batch {batch.batch_id}"
        )
    if args.cleanup_lane is not None and args.cleanup_lane not in {
        assignment.lane for assignment in batch.assignments
    }:
        raise QualificationError(
            f"lane {args.cleanup_lane} is not present in batch {batch.batch_id}"
        )
    initial_fleet = _typed_fleet(client)
    initial_roster = sorted(_nodes(initial_fleet))
    if args.observe or args.recover_lane is not None or args.cleanup_lane is not None:
        evidence_nodes = _batch_evidence_nodes(ledger, campaign_id, batch)
        if not evidence_nodes:
            raise QualificationError(
                f"{batch.batch_id} has no durable batch evidence to observe or recover"
            )
    else:
        evidence_nodes = list(args.spark)
    if any(_NODE_ID.fullmatch(node_id) is None for node_id in evidence_nodes):
        raise QualificationError("qualification requires exact Controller Spark IDs")
    lock_nodes = _qualification_lock_nodes(initial_fleet, evidence_nodes)
    with ledger_lock(ledger_path), node_locks(lock_nodes):
        locked_fleet = _typed_fleet(client)
        _require_locked_fleet_roster(locked_fleet, initial_roster)
        ledger = EvidenceLedger._under_ledger_lock(ledger_path)
        locked_batch = _current_batch(manifest, ledger, campaign_id, args.batch)
        if locked_batch is None:
            return {
                "schema_version": 1,
                "status": "complete",
                "accepted_recipe_count": len(_completed_keys(ledger, campaign_id)),
                "scope_recipe_count": len(manifest.authority.rows),
            }
        if locked_batch.batch_id != batch.batch_id:
            raise QualificationError(
                "campaign advanced while acquiring locks; rerun for the next batch"
            )
        batch = locked_batch
        if _qualification_lock_nodes(locked_fleet, evidence_nodes) != lock_nodes:
            raise QualificationError(
                "campaign Spark identities changed while acquiring locks; rerun"
            )
        batch_rows = [rows_by_key[item.recipe] for item in batch.assignments]
        if args.observe:
            lanes, preview, batch_plan = _durable_batch_lanes(
                client=client,
                batch=batch,
                manifest=manifest,
                fixtures=fixtures,
                ledger=ledger,
                campaign_id=campaign_id,
            )
            canaries = {
                lane.row.key: _latest_batch_canary_outcome(
                    ledger,
                    campaign_id=campaign_id,
                    batch_id=batch.batch_id,
                    recipe=lane.row.key,
                    lane_id=lane.assignment.lane,
                )
                for lane in lanes
            }
            if any(value is None for value in canaries.values()):
                if not any(
                    _latest_payload(
                        ledger, campaign_id, lane.row.key, "profile.load.requested"
                    )
                    is not None
                    for lane in lanes
                ):
                    raise QualificationError(
                        f"{batch.batch_id} has no accepted batch load to observe"
                    )
                load_request = next(
                    (
                        payload
                        for lane in lanes
                        if (
                            payload := _latest_payload(
                                ledger,
                                campaign_id,
                                lane.row.key,
                                "profile.load.requested",
                            )
                        )
                        is not None
                    ),
                    None,
                )
                if load_request is None:
                    raise QualificationError(
                        f"{batch.batch_id} has no accepted batch load to observe"
                    )
                failure_node = load_request.get("failure_node_id")
                application, receipts, errors = _load_batch_and_smoke(
                    client=client,
                    batch=batch,
                    lanes=lanes,
                    fixtures=fixtures,
                    preview=preview,
                    profile_number=args.profile_number,
                    campaign_id=campaign_id,
                    campaign_digest=_string(
                        batch_plan.get("campaign_digest"), "durable batch digest"
                    ),
                    ledger=ledger,
                    options=manifest,
                    failure_node_id=(
                        failure_node if isinstance(failure_node, str) else None
                    ),
                    allow_submit=False,
                    clock=clock,
                    sleeper=sleeper,
                )
                return {
                    "schema_version": 1,
                    "mode": "observe",
                    "status": "checkpoint-required",
                    "batch_id": batch.batch_id,
                    "application_id": application.get("id"),
                    "lane_results": _batch_lane_outcomes(
                        campaign_id=campaign_id,
                        batch=batch,
                        lanes=lanes,
                        ledger=ledger,
                        receipts=receipts,
                        errors=errors,
                    ),
                    "spark_accepted": False,
                }
            if batch.mode == "exclusive-dual":
                return _observe_dual_recovery(
                    client=client,
                    batch=batch,
                    lanes=lanes,
                    manifest=manifest,
                    fixtures=fixtures,
                    profile_number=args.profile_number,
                    campaign_id=campaign_id,
                    ledger=ledger,
                )
            return _observe_batch_recovery(
                client=client,
                batch=batch,
                lanes=lanes,
                manifest=manifest,
                fixtures=fixtures,
                profile_number=args.profile_number,
                authority_id=manifest.authority.authority_id,
                ledger_id=ledger_id,
                campaign_id=campaign_id,
                ledger=ledger,
                clock=clock,
                sleeper=sleeper,
            )

        fleet = _typed_fleet(client)
        _require_locked_fleet_roster(fleet, initial_roster)
        if args.recover_lane is not None:
            if batch.mode == "exclusive-dual":
                raise QualificationError(
                    "exclusive-dual recovery advances through --observe, not --recover-lane"
                )
            return _review_or_apply_lane_recovery(
                client=client,
                batch=batch,
                lane_number=args.recover_lane,
                manifest=manifest,
                fixtures=fixtures,
                profile_number=args.profile_number,
                authority_id=manifest.authority.authority_id,
                ledger_id=ledger_id,
                campaign_id=campaign_id,
                ledger=ledger,
                apply=args.apply,
                supplied_digest=args.campaign_digest,
                args=args,
                clock=clock,
                sleeper=sleeper,
            )
        if args.cleanup_lane is not None:
            if batch.mode == "exclusive-dual":
                lanes, _batch_preview, _batch_plan = _durable_batch_lanes(
                    client=client,
                    batch=batch,
                    manifest=manifest,
                    fixtures=fixtures,
                    ledger=ledger,
                    campaign_id=campaign_id,
                )
                if len(lanes) != 1 or lanes[0].assignment.lane != args.cleanup_lane:
                    raise QualificationError(
                        "dual cleanup lane differs from its authority assignment"
                    )
                return _review_or_apply_dual_cleanup(
                    client=client,
                    batch=batch,
                    lane=lanes[0],
                    manifest=manifest,
                    fixtures=fixtures,
                    profile_number=args.profile_number,
                    authority_id=manifest.authority.authority_id,
                    ledger_id=ledger_id,
                    campaign_id=campaign_id,
                    ledger=ledger,
                    apply=args.apply,
                    supplied_digest=args.campaign_digest,
                    clock=clock,
                    sleeper=sleeper,
                )
            return _review_or_apply_lane_cleanup(
                client=client,
                batch=batch,
                lane_number=args.cleanup_lane,
                manifest=manifest,
                fixtures=fixtures,
                profile_number=args.profile_number,
                authority_id=manifest.authority.authority_id,
                ledger_id=ledger_id,
                campaign_id=campaign_id,
                ledger=ledger,
                apply=args.apply,
                supplied_digest=args.campaign_digest,
                clock=clock,
                sleeper=sleeper,
            )

        selected_nodes = _exact_batch_nodes(
            client, fleet, batch, args.spark, rows_by_key
        )
        if batch.mode == "exclusive-dual":
            assignment = batch.assignments[0]
            if args.failure_spark not in selected_nodes[assignment.recipe]:
                raise QualificationError(
                    "exclusive-dual qualification requires --failure-spark to select a reviewed lane node"
                )
        _operator_gate(args, batch_rows)
        lanes, preview, metadata = _fresh_batch_preview(
            client=client,
            manifest=manifest,
            fixtures=fixtures,
            batch=batch,
            selected_nodes=selected_nodes,
            library_root=library_root,
            profile_number=args.profile_number,
            authority_id=manifest.authority.authority_id,
            ledger_id=ledger_id,
            campaign_id=campaign_id,
            ledger=ledger,
            expected_fleet_node_ids=initial_roster,
            replace_run_ids=args.replace_run_id,
            failure_node_id=args.failure_spark,
        )
        campaign_digest = _string(metadata.get("campaign_digest"), "batch digest")
        lane_views = [
            {
                "recipe": lane.row.key,
                "lane": lane.assignment.lane,
                "node_ids": list(lane.node_ids),
                "alias": lane.alias,
                "smoke_kind": lane.smoke_kind,
                "smoke_preview": dict(lane.smoke_preview),
                "recovery_coverage_refs": [
                    dict(item) for item in lane.row.recovery_coverage_refs
                ],
            }
            for lane in lanes
        ]
        if not args.apply:
            return {
                "schema_version": 1,
                "mode": "preview",
                "status": "not-accepted",
                "batch_id": batch.batch_id,
                "batch_sequence": batch.sequence,
                "lanes": lane_views,
                "profile_number": args.profile_number,
                "campaign_digest": campaign_digest,
                "profile_preview": preview,
                "acceptance_checkpoints": {
                    lane.row.key: _checkpoint_names(lane.row) for lane in lanes
                },
                "spark_accepted": False,
            }
        if campaign_digest != args.campaign_digest:
            raise QualificationError(
                "--campaign-digest no longer matches the live exact batch preview"
            )
        failure_node_id = args.failure_spark
        application, receipts, errors = _load_batch_and_smoke(
            client=client,
            batch=batch,
            lanes=lanes,
            fixtures=fixtures,
            preview=preview,
            profile_number=args.profile_number,
            campaign_id=campaign_id,
            campaign_digest=campaign_digest,
            ledger=ledger,
            options=manifest,
            failure_node_id=failure_node_id,
            clock=clock,
            sleeper=sleeper,
        )
        outcomes = _batch_lane_outcomes(
            campaign_id=campaign_id,
            batch=batch,
            lanes=lanes,
            ledger=ledger,
            receipts=receipts,
            errors=errors,
        )
        if batch.mode == "exclusive-dual" and all(
            item.get("status") == "canary-completed" for item in outcomes
        ):
            lane = lanes[0]
            canary = receipts[lane.row.key]
            pending = {
                "batch_id": batch.batch_id,
                "lane_id": lane.assignment.lane,
                "failure_spark": failure_node_id,
                "run_id": canary.get("run_id"),
                "revision_id": canary.get("recipe_revision_id"),
                "alias": lane.alias,
                "node_ids": list(lane.node_ids),
                "node_to_rank": canary.get("node_to_rank"),
            }
            if (
                _latest_payload(ledger, campaign_id, lane.row.key, "rank-loss.pending")
                is None
            ):
                ledger.append(
                    "rank-loss.pending",
                    plan_digest=campaign_id,
                    recipe=lane.row.key,
                    payload=pending,
                )
        failed = [item for item in outcomes if item.get("status") == "canary-failed"]
        if failed:
            next_checkpoint: dict[str, object] = {
                "checkpoint": "failed-lane-cleanup",
                "lanes": [item.get("lane") for item in failed],
                "instruction": "Preserve each failed canary outcome and review exact --cleanup-lane effects before the batch can advance.",
            }
        elif batch.mode == "exclusive-dual":
            next_checkpoint = {
                "checkpoint": "distributed-rank-loss",
                "failure_spark": failure_node_id,
                "instruction": "Take only the selected rank offline through the approved operator procedure, then resume this batch with --observe.",
            }
        else:
            next_checkpoint = {
                "checkpoint": "exclusive-lane-recovery",
                "lane": 1,
                "instruction": "Review the one-lane recovery profile for this authority lane, then apply it with --recover-lane and the reviewed digest.",
            }
        return {
            "schema_version": 1,
            "mode": "apply",
            "status": "checkpoint-required",
            "batch_id": batch.batch_id,
            "application_id": application.get("id"),
            "lanes": lane_views,
            "lane_results": outcomes,
            "next": next_checkpoint,
            "spark_accepted": False,
        }


def _checkpoint_names(row: RecipeAuthorityRow) -> list[str]:
    if row.node_count == 1:
        return ["canary-serving", "offline-host-restart", "changed-boot-id"]
    return [
        "canary-serving",
        "rank-loss",
        "route-withdrawal",
        "rank-recovery",
        "recovered-serving",
        "final-offline-restart",
        "changed-boot-id-for-each-selected-spark",
    ]


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = run(argv)
    except (
        ControlClientError,
        FixtureError,
        QualificationError,
        OSError,
        ValueError,
    ) as error:
        print(
            json.dumps(
                {"schema_version": 1, "status": "blocked", "error": str(error)[:1024]},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":"), default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
