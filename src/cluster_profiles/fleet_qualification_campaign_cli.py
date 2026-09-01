"""Coordinate two exact, disjoint, node-pinned qualification lanes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from .control_client import ControlClient, ControlClientError
from .fleet_qualification import (
    ArtifactJobSmokeAdapter,
    EvidenceLedger,
    QualificationError,
    QualificationRunner,
    RunnerOptions,
    ServiceSmokeAdapter,
    build_plan,
    load_policy,
)
from .qualification_fixtures import FixtureError, FixtureRegistry
from .qualification_locking import ledger_lock, node_locks

_LANE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")
_RECIPE_KEY = re.compile(r"[^/\s]+/[^/\s]+\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_MANIFEST_BYTES = 1024 * 1024
_AUTHORITY_FILES = {
    "nl-single-spark-e6a8e750": "nl-single-spark-e6a8e750.json",
}


@dataclass(frozen=True)
class CampaignLane:
    name: str
    node_id: str
    recipes: tuple[str, ...]
    ledger: Path
    plan_output: Path


@dataclass(frozen=True)
class CampaignAuthority:
    authority_id: str
    authority_sha256: str
    repository: str
    commit: str
    catalog_index_sha256: str
    catalog_recipe_count: int
    jurisdiction: str
    actionable_recipe_keys: tuple[str, ...]
    capacity_blocked_recipe_keys: tuple[str, ...] = ()
    legal_blocked_recipe_keys: tuple[str, ...] = ()
    dual_spark_recipe_keys: tuple[str, ...] = ()
    unsupported_topology_recipe_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class CampaignManifest:
    path: Path
    manifest_sha256: str
    authority: CampaignAuthority
    lanes: tuple[CampaignLane, CampaignLane]
    jurisdiction: str | None
    cleanup: str
    operation_timeout_seconds: float
    poll_interval_seconds: float
    policy: Path | None
    fixture_manifest: Path | None


@dataclass(frozen=True)
class PreparedLane:
    lane: CampaignLane
    options: RunnerOptions
    client: Any
    ledger: EvidenceLedger
    plan: dict[str, object]


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise QualificationError(f"campaign manifest has duplicate key: {key}")
        result[key] = value
    return result


def _object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise QualificationError(f"campaign manifest {label} must be an object")
    return value


def _exact_keys(
    value: Mapping[str, object], *, required: set[str], optional: set[str], label: str
) -> None:
    missing = sorted(required - set(value))
    if missing:
        raise QualificationError(f"campaign manifest {label} lacks {missing[0]}")
    unknown = sorted(set(value) - required - optional)
    if unknown:
        raise QualificationError(
            f"campaign manifest {label} has unknown field {unknown[0]}"
        )


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise QualificationError(f"campaign manifest {label} must be a string")
    return value


def _bounded_number(
    value: object, label: str, default: float, minimum: float, maximum: float
) -> float:
    if value is None:
        return default
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise QualificationError(
            f"campaign manifest options.{label} must be between "
            f"{minimum:g} and {maximum:g}"
        )
    return float(value)


def _recipe_keys(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise QualificationError(f"campaign manifest {label} must be a non-empty list")
    result: list[str] = []
    for raw in value:
        key = _string(raw, f"{label} recipe")
        if _RECIPE_KEY.fullmatch(key) is None:
            raise QualificationError(f"campaign manifest recipe key is invalid: {key}")
        result.append(key)
    duplicates = sorted(key for key in set(result) if result.count(key) > 1)
    if duplicates:
        raise QualificationError(
            f"campaign manifest {label} repeats recipe {duplicates[0]}"
        )
    return tuple(result)


def _manifest_path(root: Path, value: object, label: str) -> Path:
    supplied = Path(_string(value, label)).expanduser()
    return (supplied if supplied.is_absolute() else root / supplied).resolve(
        strict=False
    )


def _load_authority(authority_id: str) -> CampaignAuthority:
    filename = _AUTHORITY_FILES.get(authority_id)
    if filename is None:
        raise QualificationError(
            f"campaign qualification authority is not reviewed: {authority_id}"
        )
    raw = (
        resources.files("cluster_profiles")
        .joinpath("qualification_authorities", filename)
        .read_bytes()
    )
    try:
        document = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QualificationError(
            f"checked-in qualification authority is invalid: {authority_id}"
        ) from error
    root = _object(document, "qualification authority")
    schema_version = root.get("schema_version")
    if schema_version != 2 or isinstance(schema_version, bool):
        raise QualificationError(
            f"checked-in qualification authority identity is invalid: {authority_id}"
        )
    category_fields = {
        "capacity_blocked_recipe_keys",
        "legal_blocked_recipe_keys",
        "dual_spark_recipe_keys",
        "unsupported_topology_recipe_keys",
    }
    _exact_keys(
        root,
        required={
            "schema_version",
            "authority_id",
            "catalog",
            "jurisdiction",
            "reviewed_disposition",
            "actionable_recipe_keys",
        }
        | category_fields,
        optional=set(),
        label="qualification authority",
    )
    if root["authority_id"] != authority_id:
        raise QualificationError(
            f"checked-in qualification authority identity is invalid: {authority_id}"
        )
    catalog = _object(root["catalog"], "qualification authority catalog")
    _exact_keys(
        catalog,
        required={
            "repository",
            "commit",
            "catalog_index_sha256",
            "recipe_count",
        },
        optional=set(),
        label="qualification authority catalog",
    )
    repository = _string(catalog["repository"], "authority catalog repository")
    commit = _string(catalog["commit"], "authority catalog commit")
    catalog_index_sha256 = _string(
        catalog["catalog_index_sha256"], "authority catalog index digest"
    )
    recipe_count = catalog["recipe_count"]
    if (
        re.fullmatch(r"[0-9a-f]{40}", commit) is None
        or _SHA256.fullmatch(catalog_index_sha256) is None
        or not isinstance(recipe_count, int)
        or isinstance(recipe_count, bool)
        or recipe_count < 1
    ):
        raise QualificationError(
            f"checked-in qualification authority catalog is invalid: {authority_id}"
        )
    jurisdiction = _string(root["jurisdiction"], "authority jurisdiction")
    if (
        len(jurisdiction) != 2
        or not jurisdiction.isascii()
        or not jurisdiction.isalpha()
        or jurisdiction != jurisdiction.upper()
    ):
        raise QualificationError(
            f"checked-in qualification authority jurisdiction is invalid: {authority_id}"
        )
    recipe_keys = _recipe_keys(
        root["actionable_recipe_keys"], "authority actionable_recipe_keys"
    )
    if recipe_keys != tuple(sorted(recipe_keys)):
        raise QualificationError(
            f"checked-in qualification authority recipe keys are not sorted: {authority_id}"
        )
    disposition = _object(
        root["reviewed_disposition"], "qualification authority disposition"
    )
    disposition_fields = {
        "actionable_single_spark_count",
        "capacity_blocked_single_spark_count",
        "dual_spark_count",
        "legal_blocked_single_spark_count",
        "unsupported_topology_count",
    }
    _exact_keys(
        disposition,
        required=disposition_fields,
        optional=set(),
        label="qualification authority disposition",
    )
    if any(
        not isinstance(disposition[field], int)
        or isinstance(disposition[field], bool)
        or disposition[field] < 0
        for field in disposition_fields
    ):
        raise QualificationError(
            f"checked-in qualification authority counts are invalid: {authority_id}"
        )
    category_keys: dict[str, tuple[str, ...]] = {}
    for field in sorted(category_fields):
        values = _recipe_keys(root[field], f"authority {field}")
        if values != tuple(sorted(values)):
            raise QualificationError(
                "checked-in qualification authority category keys are not "
                f"sorted: {authority_id} {field}"
            )
        category_keys[field] = values
    categories = {"actionable_recipe_keys": recipe_keys, **category_keys}
    assigned = [key for values in categories.values() for key in values]
    duplicates = sorted(key for key in set(assigned) if assigned.count(key) > 1)
    if duplicates:
        raise QualificationError(
            "checked-in qualification authority classifies recipe more than "
            f"once: {authority_id} {duplicates[0]}"
        )
    expected_counts = {
        "actionable_recipe_keys": "actionable_single_spark_count",
        "capacity_blocked_recipe_keys": "capacity_blocked_single_spark_count",
        "legal_blocked_recipe_keys": "legal_blocked_single_spark_count",
        "dual_spark_recipe_keys": "dual_spark_count",
        "unsupported_topology_recipe_keys": "unsupported_topology_count",
    }
    counts_match = all(
        len(categories[key]) == disposition[count_field]
        for key, count_field in expected_counts.items()
    )
    closure_matches = len(assigned) == recipe_count
    if not counts_match or not closure_matches:
        raise QualificationError(
            f"checked-in qualification authority closure is invalid: {authority_id}"
        )
    return CampaignAuthority(
        authority_id=authority_id,
        authority_sha256=_digest(document),
        repository=repository,
        commit=commit,
        catalog_index_sha256=catalog_index_sha256,
        catalog_recipe_count=recipe_count,
        jurisdiction=jurisdiction,
        actionable_recipe_keys=recipe_keys,
        capacity_blocked_recipe_keys=category_keys.get(
            "capacity_blocked_recipe_keys", ()
        ),
        legal_blocked_recipe_keys=category_keys.get("legal_blocked_recipe_keys", ()),
        dual_spark_recipe_keys=category_keys.get("dual_spark_recipe_keys", ()),
        unsupported_topology_recipe_keys=category_keys.get(
            "unsupported_topology_recipe_keys", ()
        ),
    )


def load_manifest(path: Path) -> CampaignManifest:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise QualificationError(f"campaign manifest cannot be read: {path}") from error
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise QualificationError("campaign manifest exceeds 1 MiB")
    try:
        document = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except UnicodeDecodeError as error:
        raise QualificationError("campaign manifest must be UTF-8 JSON") from error
    except json.JSONDecodeError as error:
        raise QualificationError("campaign manifest is invalid JSON") from error
    root = _object(document, "root")
    _exact_keys(
        root,
        required={"schema_version", "qualification_authority", "lanes"},
        optional={"options", "policy", "fixture_manifest"},
        label="root",
    )
    if (
        not isinstance(root["schema_version"], int)
        or isinstance(root["schema_version"], bool)
        or root["schema_version"] != 1
    ):
        raise QualificationError("campaign manifest schema_version must be 1")

    authority = _load_authority(
        _string(root["qualification_authority"], "qualification_authority")
    )
    intended = authority.actionable_recipe_keys
    raw_lanes = root["lanes"]
    if not isinstance(raw_lanes, list) or len(raw_lanes) != 2:
        raise QualificationError("campaign manifest must contain exactly two lanes")
    manifest_root = path.resolve().parent
    lanes: list[CampaignLane] = []
    for index, raw_lane in enumerate(raw_lanes):
        lane = _object(raw_lane, f"lanes[{index}]")
        _exact_keys(
            lane,
            required={"name", "node_id", "recipes", "ledger", "plan_output"},
            optional=set(),
            label=f"lanes[{index}]",
        )
        name = _string(lane["name"], f"lanes[{index}].name")
        if _LANE_NAME.fullmatch(name) is None:
            raise QualificationError(f"campaign manifest lane name is invalid: {name}")
        node_id = _string(lane["node_id"], f"lanes[{index}].node_id")
        if _NODE_ID.fullmatch(node_id) is None:
            raise QualificationError(
                f"campaign manifest controller node ID is invalid: {node_id}"
            )
        lanes.append(
            CampaignLane(
                name=name,
                node_id=node_id,
                recipes=_recipe_keys(lane["recipes"], f"lane {name}"),
                ledger=_manifest_path(
                    manifest_root, lane["ledger"], f"lane {name} ledger"
                ),
                plan_output=_manifest_path(
                    manifest_root,
                    lane["plan_output"],
                    f"lane {name} plan_output",
                ),
            )
        )

    names = [lane.name for lane in lanes]
    if len(set(names)) != 2:
        raise QualificationError("campaign manifest lane names must be distinct")
    node_ids = [lane.node_id for lane in lanes]
    if len(set(node_ids)) != 2:
        raise QualificationError("campaign manifest node IDs must be distinct")
    outputs = [lane.ledger for lane in lanes] + [lane.plan_output for lane in lanes]
    if len(set(outputs)) != 4:
        raise QualificationError(
            "campaign manifest ledgers and plan outputs must all be unique"
        )

    assigned = [key for lane in lanes for key in lane.recipes]
    repeated = sorted(key for key in set(assigned) if assigned.count(key) > 1)
    if repeated:
        raise QualificationError(
            f"campaign manifest assigns recipe more than once: {repeated[0]}"
        )
    missing = sorted(set(intended) - set(assigned))
    unexpected = sorted(set(assigned) - set(intended))
    if missing or unexpected:
        raise QualificationError(
            "campaign manifest does not exactly partition reviewed authority "
            f"{authority.authority_id}; missing={missing}, unexpected={unexpected}"
        )

    options = _object(root.get("options", {}), "options")
    _exact_keys(
        options,
        required=set(),
        optional={
            "jurisdiction",
            "cleanup",
            "operation_timeout_seconds",
            "poll_interval_seconds",
        },
        label="options",
    )
    jurisdiction_value = options.get("jurisdiction")
    if jurisdiction_value is not None:
        jurisdiction = _string(jurisdiction_value, "options.jurisdiction")
        if (
            len(jurisdiction) != 2
            or not jurisdiction.isascii()
            or not jurisdiction.isalpha()
        ):
            raise QualificationError(
                "campaign manifest options.jurisdiction must be ISO alpha-2"
            )
        jurisdiction = jurisdiction.upper()
    else:
        jurisdiction = None
    if jurisdiction != authority.jurisdiction:
        raise QualificationError(
            "campaign jurisdiction does not match its reviewed qualification authority"
        )
    cleanup = options.get("cleanup", "stop")
    if cleanup not in {"none", "stop", "uninstall"}:
        raise QualificationError(
            "campaign manifest options.cleanup must be none, stop, or uninstall"
        )

    policy = (
        _manifest_path(manifest_root, root["policy"], "policy")
        if "policy" in root
        else None
    )
    fixture_manifest = (
        _manifest_path(manifest_root, root["fixture_manifest"], "fixture_manifest")
        if "fixture_manifest" in root
        else None
    )
    protected_inputs = {
        value
        for value in (path.resolve(), policy, fixture_manifest)
        if value is not None
    }
    conflicts = sorted((set(outputs) & protected_inputs), key=str)
    if conflicts:
        raise QualificationError(
            f"campaign output would overwrite an input file: {conflicts[0]}"
        )
    return CampaignManifest(
        path=path.resolve(),
        manifest_sha256=_digest(document),
        authority=authority,
        lanes=(lanes[0], lanes[1]),
        jurisdiction=jurisdiction,
        cleanup=str(cleanup),
        operation_timeout_seconds=_bounded_number(
            options.get("operation_timeout_seconds"),
            "operation_timeout_seconds",
            7_200,
            1,
            86_400,
        ),
        poll_interval_seconds=_bounded_number(
            options.get("poll_interval_seconds"),
            "poll_interval_seconds",
            5,
            0.1,
            60,
        ),
        policy=policy,
        fixture_manifest=fixture_manifest,
    )


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Preview or apply one exact two-Spark qualification manifest as two "
            "disjoint, concurrently executable single-Spark lanes."
        )
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--apply", action="store_true", help="Apply both freshly regenerated plans"
    )
    parser.add_argument(
        "--campaign-digest",
        help="Required with --apply; copied from the reviewed two-lane preview",
    )
    args = parser.parse_args(argv)
    if args.apply and not args.campaign_digest:
        parser.error("--apply requires --campaign-digest from a fresh preview")
    if not args.apply and args.campaign_digest:
        parser.error("--campaign-digest is only valid with --apply")
    if args.campaign_digest and _SHA256.fullmatch(args.campaign_digest) is None:
        parser.error("--campaign-digest must be a lowercase SHA-256 digest")
    return args


def _prepare_lanes(
    manifest: CampaignManifest, client_factory: Callable[[], Any]
) -> tuple[PreparedLane, PreparedLane]:
    policy = load_policy(manifest.policy)
    fixtures = FixtureRegistry.packaged(manifest.fixture_manifest)
    prepared: list[PreparedLane] = []
    for lane in manifest.lanes:
        options = RunnerOptions(
            jurisdiction=manifest.jurisdiction,
            cleanup=manifest.cleanup,
            operation_timeout_seconds=manifest.operation_timeout_seconds,
            poll_interval_seconds=manifest.poll_interval_seconds,
            selected_recipes=frozenset(lane.recipes),
            allowed_node_ids=frozenset({lane.node_id}),
        )
        client = client_factory()
        ledger = EvidenceLedger(lane.ledger)
        plan = build_plan(client, options, policy, fixtures)
        catalog = _object(plan.get("catalog"), f"lane {lane.name} plan catalog")
        if (
            catalog.get("repository") != manifest.authority.repository
            or catalog.get("commit") != manifest.authority.commit
        ):
            raise QualificationError(
                f"lane {lane.name} public catalog drifted from reviewed authority "
                f"{manifest.authority.authority_id}"
            )
        planned_keys = {
            str(item.get("key"))
            for item in plan.get("recipes", [])
            if isinstance(item, Mapping)
        }
        if planned_keys != set(lane.recipes):
            raise QualificationError(
                f"lane {lane.name} plan does not match its exact recipe partition"
            )
        prepared.append(
            PreparedLane(
                lane=lane,
                options=options,
                client=client,
                ledger=ledger,
                plan=plan,
            )
        )
    return prepared[0], prepared[1]


def _campaign_digest(
    manifest: CampaignManifest, prepared: Sequence[PreparedLane]
) -> str:
    return _digest(
        {
            "schema_version": 1,
            "manifest_sha256": manifest.manifest_sha256,
            "authority_id": manifest.authority.authority_id,
            "authority_sha256": manifest.authority.authority_sha256,
            "lanes": [
                {
                    "name": item.lane.name,
                    "node_id": item.lane.node_id,
                    "recipes": list(item.lane.recipes),
                    "plan_digest": item.plan.get("plan_digest"),
                }
                for item in prepared
            ],
        }
    )


def _write_private_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        encoded = json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise QualificationError(f"campaign plan output is not private: {path}")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _lane_summary(item: PreparedLane) -> dict[str, object]:
    return {
        "name": item.lane.name,
        "node_id": item.lane.node_id,
        "recipe_count": len(item.lane.recipes),
        "plan_digest": item.plan.get("plan_digest"),
        "ledger": str(item.lane.ledger),
        "plan_output": str(item.lane.plan_output),
    }


def _apply_lane(item: PreparedLane, fixtures: FixtureRegistry) -> dict[str, object]:
    plan_digest = str(item.plan["plan_digest"])
    result = QualificationRunner(
        item.client,
        item.ledger,
        item.options,
        artifact_smoke=ArtifactJobSmokeAdapter(fixtures),
        service_smoke=ServiceSmokeAdapter(fixtures),
    ).apply(item.plan, plan_digest)
    return {"name": item.lane.name, "node_id": item.lane.node_id, **result}


def run(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[], Any] = ControlClient.from_environment,
) -> dict[str, object]:
    args = _arguments(argv)
    manifest = load_manifest(args.manifest)
    node_ids = [lane.node_id for lane in manifest.lanes]
    ledger_paths = sorted((lane.ledger for lane in manifest.lanes), key=str)
    with node_locks(node_ids), ExitStack() as stack:
        for ledger_path in ledger_paths:
            stack.enter_context(ledger_lock(ledger_path))
        prepared = _prepare_lanes(manifest, client_factory)
        campaign_digest = _campaign_digest(manifest, prepared)
        if not args.apply:
            for item in prepared:
                plan_output = {
                    "mode": "preview",
                    "campaign_digest": campaign_digest,
                    "campaign_manifest_sha256": manifest.manifest_sha256,
                    "qualification_authority": manifest.authority.authority_id,
                    "qualification_authority_sha256": manifest.authority.authority_sha256,
                    "lane": item.lane.name,
                    **item.plan,
                }
                _write_private_json(item.lane.plan_output, plan_output)
                item.ledger.append(
                    "plan.generated",
                    plan_digest=str(item.plan["plan_digest"]),
                    payload={
                        "plan": item.plan,
                        "campaign_digest": campaign_digest,
                        "campaign_manifest_sha256": manifest.manifest_sha256,
                        "qualification_authority": manifest.authority.authority_id,
                        "qualification_authority_sha256": manifest.authority.authority_sha256,
                        "lane": item.lane.name,
                    },
                )
            return {
                "schema_version": 1,
                "mode": "preview",
                "campaign_digest": campaign_digest,
                "campaign_manifest_sha256": manifest.manifest_sha256,
                "qualification_authority": manifest.authority.authority_id,
                "qualification_authority_sha256": manifest.authority.authority_sha256,
                "lanes": [_lane_summary(item) for item in prepared],
            }

        if campaign_digest != args.campaign_digest:
            raise QualificationError(
                "--campaign-digest does not match the current two-lane plans"
            )
        fixtures = FixtureRegistry.packaged(manifest.fixture_manifest)
        results: dict[str, dict[str, object]] = {}
        failures: list[tuple[str, Exception]] = []
        with ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="vonk-qualification-lane"
        ) as executor:
            futures = {
                executor.submit(_apply_lane, item, fixtures): item.lane.name
                for item in prepared
            }
            for future in as_completed(futures):
                lane_name = futures[future]
                try:
                    results[lane_name] = future.result()
                except Exception as error:  # noqa: BLE001 - await both lanes
                    failures.append((lane_name, error))
        if failures:
            details = "; ".join(
                f"{name}: {str(error)[:512]}" for name, error in sorted(failures)
            )
            raise QualificationError(
                f"qualification campaign lane failure(s): {details}"
            )
        return {
            "schema_version": 1,
            "mode": "apply",
            "campaign_digest": campaign_digest,
            "campaign_manifest_sha256": manifest.manifest_sha256,
            "qualification_authority": manifest.authority.authority_id,
            "qualification_authority_sha256": manifest.authority.authority_sha256,
            "lanes": [results[lane.name] for lane in manifest.lanes],
        }


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = run(argv)
    except (ControlClientError, FixtureError, QualificationError, OSError) as error:
        print(json.dumps({"error": str(error)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
