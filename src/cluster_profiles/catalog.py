"""Content-addressed catalog for declarative GPU node workload profiles."""

from __future__ import annotations

import hashlib
import json
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from functools import lru_cache
from itertools import pairwise
from pathlib import Path
from types import MappingProxyType
from typing import Any

from jsonschema import ValidationError, validators

from .contracts import (
    ClusterProfile,
    WorkloadDefinition,
    load_cluster_profile,
    load_workload,
)
from .workload_packages import (
    LegacyWorkloadReader,
    PackageFamily,
    WorkloadDeployment,
    WorkloadPackageError,
)


class CatalogError(ValueError):
    """Raised when a catalog cannot establish its content-addressed evidence."""


def _normal_form(value: Any) -> Any:
    if is_dataclass(value):
        result = {}
        for field in fields(value):
            item = getattr(value, field.name)
            if field.metadata.get("omit_if_none") and item is None:
                continue
            result[field.name] = _normal_form(item)
        return result
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Mapping):
        return {str(key): _normal_form(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_normal_form(item) for item in value]
    return value


def fingerprint(value: Any) -> str:
    """Return the SHA-256 of the canonical JSON representation of *value*."""
    payload = json.dumps(
        _normal_form(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, ValueError) as error:
        raise CatalogError(f"cannot load evidence index {path}: {error}") from error
    if not isinstance(data, dict):
        raise CatalogError(f"evidence index {path} must be a JSON object")
    return data


def _schema(name: str) -> dict[str, Any]:
    from importlib import resources

    with resources.files("cluster_profiles").joinpath("schemas", name).open(
        encoding="utf-8"
    ) as source:
        return json.load(source)


@lru_cache(maxsize=None)
def _validator(name: str) -> Any:
    schema = _schema(name)
    validator_class = validators.validator_for(schema)
    validator_class.check_schema(schema)
    return validator_class(schema)


def validate_evidence_indexes(root: Path) -> None:
    """Validate the checked-in maturity and accepted-profile indexes."""
    for name, schema_name in (
        ("model-definitions.json", "model-definitions.schema.json"),
        ("accepted-cluster-profiles.json", "accepted-cluster-profiles.schema.json"),
    ):
        try:
            _validator(schema_name).validate(
                _load_json(root / "inventory/reports" / name)
            )
        except ValidationError as error:
            location = ".".join(str(part) for part in error.absolute_path)
            prefix = f"{location}: " if location else ""
            raise CatalogError(f"invalid {name}: {prefix}{error.message}") from error


def _load_locks(path: Path) -> dict[str, str]:
    try:
        with path.open("rb") as source:
            raw = tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise CatalogError(f"cannot load definition locks {path}: {error}") from error
    locks = raw.get("definitions")
    if not isinstance(locks, dict) or not locks:
        raise CatalogError("definition locks must contain a [definitions] table")
    if set(raw) != {"definitions"} or any(
        not isinstance(identifier, str) or not isinstance(value, str)
        for identifier, value in locks.items()
    ):
        raise CatalogError("definition locks must contain only definition fingerprints")
    return dict(locks)


def _load_selectors(path: Path, profiles: Mapping[str, ClusterProfile]) -> dict[str, str]:
    try:
        with path.open("rb") as source:
            raw = tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise CatalogError(f"cannot load profile selectors {path}: {error}") from error
    selectors = raw.get("selectors")
    if set(raw) != {"selectors"} or not isinstance(selectors, dict) or not selectors:
        raise CatalogError("profile selectors must contain a [selectors] table")
    result = dict(selectors)
    if any(not isinstance(name, str) or not isinstance(target, str) for name, target in result.items()):
        raise CatalogError("profile selectors must map names to profile IDs")
    missing = sorted(set(result.values()) - set(profiles))
    if missing:
        raise CatalogError(f"selector target is missing: {', '.join(missing)}")
    return result


def _load_definitions(root: Path) -> dict[str, WorkloadDefinition]:
    result: dict[str, WorkloadDefinition] = {}
    for path in sorted((root / "config/workloads").glob("*.toml")):
        definition = load_workload(path)
        if definition.id in result:
            raise CatalogError(f"duplicate workload definition ID: {definition.id}")
        result[definition.id] = definition
    if not result:
        raise CatalogError("catalog has no workload definitions")
    return result


def _load_profiles(root: Path) -> dict[str, ClusterProfile]:
    result: dict[str, ClusterProfile] = {}
    for path in sorted((root / "config/cluster-profiles").glob("*.toml")):
        profile = load_cluster_profile(path)
        if profile.id in result:
            raise CatalogError(f"duplicate cluster profile ID: {profile.id}")
        result[profile.id] = profile
    if not result:
        raise CatalogError("catalog has no cluster profiles")
    return result


def _load_package_families(root: Path) -> dict[str, PackageFamily]:
    directory = root / "config/package-families"
    result: dict[str, PackageFamily] = {}
    if not directory.is_dir():
        return result
    for path in sorted(directory.glob("*.toml")):
        try:
            with path.open("rb") as source:
                family = PackageFamily.load(tomllib.load(source))
        except (OSError, tomllib.TOMLDecodeError, WorkloadPackageError) as error:
            raise CatalogError(f"cannot load package family {path}: {error}") from error
        if family.family_id in result:
            raise CatalogError(f"duplicate package family ID: {family.family_id}")
        if path.name != f"{family.family_id}.toml":
            raise CatalogError(f"package family path does not match ID: {path.name}")
        result[family.family_id] = family
    return result


def _load_workload_deployments(
    root: Path, families: Mapping[str, PackageFamily]
) -> dict[str, WorkloadDeployment]:
    directory = root / "config/workload-deployments"
    result: dict[str, WorkloadDeployment] = {}
    if not directory.is_dir():
        return result
    for path in sorted(directory.glob("*.toml")):
        try:
            with path.open("rb") as source:
                deployment = WorkloadDeployment.load(tomllib.load(source))
        except (OSError, tomllib.TOMLDecodeError, WorkloadPackageError) as error:
            raise CatalogError(f"cannot load workload deployment {path}: {error}") from error
        if deployment.deployment_id in result:
            raise CatalogError(f"duplicate workload deployment ID: {deployment.deployment_id}")
        if path.name != f"{deployment.deployment_id}.toml":
            raise CatalogError(f"workload deployment path does not match ID: {path.name}")
        if deployment.family_id not in families:
            raise CatalogError(f"workload deployment family is missing: {deployment.family_id}")
        result[deployment.deployment_id] = deployment
    return result


def _project_legacy_deployments(
    definitions: Mapping[str, WorkloadDefinition],
    deployments: Mapping[str, WorkloadDeployment],
) -> dict[str, WorkloadDeployment]:
    result: dict[str, WorkloadDeployment] = {}
    for deployment in deployments.values():
        legacy_id = deployment.routing["alias"]
        definition = definitions.get(legacy_id)
        if definition is None:
            continue
        if legacy_id in result:
            raise CatalogError(f"duplicate legacy workload deployment: {legacy_id}")
        try:
            result[legacy_id] = LegacyWorkloadReader.read(definition, deployment)
        except (TypeError, WorkloadPackageError) as error:
            raise CatalogError(f"invalid legacy workload projection: {legacy_id}") from error
    return result


_LEGAL_MATURITY_TRANSITIONS = {
    "planned": "prepared",
    "prepared": "verified",
    "verified": ("accepted", "rejected"),
}


def _audit_timestamp(value: str, context: str) -> datetime:
    try:
        timestamp = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise CatalogError(f"{context} has an invalid timestamp") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise CatalogError(f"{context} timestamp must include a UTC offset")
    return timestamp


def _validate_evidence_refs(root: Path, references: list[str], context: str) -> None:
    for reference in references:
        path = Path(reference)
        if path.is_absolute() or ".." in path.parts:
            raise CatalogError(
                f"{context} evidence reference must be repository-relative: {reference}"
            )
        candidate = (root / path).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            raise CatalogError(
                f"{context} evidence reference does not exist: {reference}"
            )


def _repository_file(root: Path, reference: Path | str, context: str) -> Path:
    path = Path(reference)
    if path.is_absolute() or ".." in path.parts:
        raise CatalogError(f"{context} must be repository-relative: {reference}")
    candidate = (root / path).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise CatalogError(f"{context} does not exist: {reference}")
    return candidate


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_runtime_release(root: Path, definition: WorkloadDefinition) -> None:
    release = definition.runtime_release
    if release is None:
        return
    manifest_path = _repository_file(root, release.manifest, "runtime release manifest")
    if _file_sha256(manifest_path) != release.sha256:
        raise CatalogError(f"runtime release manifest digest does not match definition: {definition.id}")
    try:
        manifest = _load_json(manifest_path)
    except CatalogError as error:
        raise CatalogError(f"invalid runtime release manifest: {definition.id}") from error
    files = manifest.get("files")
    if set(manifest) != {"files"} or not isinstance(files, dict) or not files:
        raise CatalogError(f"invalid runtime release manifest: {definition.id}")
    for reference, expected in files.items():
        if not isinstance(reference, str) or not isinstance(expected, str):
            raise CatalogError(f"invalid runtime release manifest: {definition.id}")
        artifact = _repository_file(root, reference, "runtime release artifact")
        if _file_sha256(artifact) != expected:
            raise CatalogError(f"runtime release artifact digest does not match manifest: {reference}")


def _validate_model_owned_storage_paths(
    definitions: Mapping[str, WorkloadDefinition],
) -> None:
    """Reject cache/scratch/output roots shared by distinct model definitions."""
    owners: dict[Path, tuple[str, str]] = {}
    for definition in definitions.values():
        for field in ("cache", "scratch", "output"):
            path = Path(getattr(definition.paths, field)).expanduser()
            owner = (definition.id, field)
            previous = owners.get(path)
            if previous is not None and previous[0] != definition.id:
                raise CatalogError(
                    "model-owned storage path collision: "
                    f"{path} ({previous[0]}.{previous[1]} and {definition.id}.{field})"
                )
            owners[path] = owner


def _stage_report_path(
    identifier: str, stage: str, *, correction_position: int | None = None
) -> str:
    suffix = (
        f"{stage}-correction-{correction_position}"
        if correction_position is not None
        else stage
    )
    return f"inventory/reports/model-definitions/{identifier}-{suffix}.json"


def _validate_stage_evidence(
    root: Path,
    *,
    definition: WorkloadDefinition,
    definition_sha256: str,
    transition: Mapping[str, Any],
    predecessor: str | None,
    history_position: int,
    correction: bool,
) -> str:
    stage = transition["state"]
    expected_path = _stage_report_path(
        definition.id,
        stage,
        correction_position=history_position if correction else None,
    )
    if transition["evidence_refs"] != [expected_path]:
        raise CatalogError(f"{stage} maturity evidence must name its canonical report: {definition.id}")
    try:
        report = _load_json(_repository_file(root, expected_path, f"{stage} maturity evidence"))
        _validator("model-definition-evidence.schema.json").validate(report)
    except (CatalogError, ValidationError) as error:
        raise CatalogError(f"invalid {stage} maturity evidence: {definition.id}") from error
    if report["stage"] != stage or report["definition_id"] != definition.id:
        raise CatalogError(f"maturity evidence identity does not match definition: {definition.id}")
    if report["definition_sha256"] != definition_sha256:
        raise CatalogError(f"maturity evidence fingerprint does not match definition: {definition.id}")
    if report["runtime_manifest_sha256"] != (
        definition.runtime_release.sha256 if definition.runtime_release else None
    ):
        raise CatalogError(f"runtime release pin does not match definition: {definition.id}")
    if report["source"] != {"repository": definition.source.repository, "commit": definition.source.commit}:
        raise CatalogError(f"source pin does not match definition: {definition.id}")
    if report["checkpoint"] != {
        "repository": definition.checkpoint.repository,
        "revision": definition.checkpoint.revision,
        "manifest_sha256": definition.checkpoint.manifest_sha256,
    }:
        raise CatalogError(f"checkpoint pin does not match definition: {definition.id}")
    if report["image"] != {"reference": definition.image.reference}:
        raise CatalogError(f"image pin does not match definition: {definition.id}")
    report_nodes = report["nodes"]
    reported_node_ids = {node["node"] for node in report_nodes}
    if (
        len(reported_node_ids) != len(report_nodes)
        or reported_node_ids != set(definition.nodes)
    ):
        raise CatalogError(
            f"maturity evidence nodes must exactly match definition nodes: "
            f"{definition.id}"
        )
    _audit_timestamp(report["recorded_at"], f"{stage} maturity evidence")
    if report["predecessor"] != predecessor:
        raise CatalogError(f"maturity evidence predecessor does not name the immediately prior valid report: {definition.id}")
    return expected_path


def _validate_maturity_history(root: Path, record: Mapping[str, Any]) -> None:
    identifier = record["id"]
    history = record["history"]
    states = [transition["state"] for transition in history]
    if states[0] != "planned":
        raise CatalogError(f"maturity history must begin at planned: {identifier}")

    previous_timestamp: datetime | None = None
    for position, transition in enumerate(history):
        context = f"maturity history for {identifier} at transition {position}"
        timestamp = _audit_timestamp(transition["timestamp"], context)
        if previous_timestamp is not None and timestamp <= previous_timestamp:
            raise CatalogError(f"maturity history timestamps must increase: {identifier}")
        previous_timestamp = timestamp
        _validate_evidence_refs(root, transition["evidence_refs"], context)

    for position, (previous, current) in enumerate(pairwise(states), start=1):
        transition = history[position]
        has_correction = (
            "correction_of" in transition or "correction_reason" in transition
        )
        if previous == "rejected":
            if current != "verified":
                raise CatalogError(
                    f"rejected maturity may only return to verified: {identifier}"
                )
            if not has_correction:
                raise CatalogError(
                    "rejected to verified requires correction_of and "
                    f"correction_reason: {identifier}"
                )
            rejected_position = position - 1
            if transition["correction_of"] != rejected_position:
                raise CatalogError(
                    f"correction_of must reference transition {rejected_position}: "
                    f"{identifier}"
                )
            continue
        if has_correction:
            raise CatalogError(
                f"correction metadata may only follow rejected: {identifier}"
            )
        expected = _LEGAL_MATURITY_TRANSITIONS.get(previous, ())
        if isinstance(expected, str):
            allowed = (expected,)
        else:
            allowed = expected
        if current not in allowed:
            raise CatalogError(
                f"illegal maturity transition for {identifier}: {previous} -> {current}"
            )

    if record["maturity"] != states[-1]:
        raise CatalogError(f"current maturity does not match history: {identifier}")


def _maturity_records(
    root: Path,
    index: Mapping[str, Any],
    fingerprints: Mapping[str, str],
    definitions: Mapping[str, WorkloadDefinition],
) -> dict[str, str]:
    records = index["definitions"]
    maturity: dict[str, str] = {}
    for record in records:
        identifier = record["id"]
        if identifier in maturity:
            raise CatalogError(f"duplicate maturity record: {identifier}")
        maturity[identifier] = record["maturity"]
        if fingerprints.get(identifier) != record["sha256"]:
            raise CatalogError(f"maturity fingerprint does not match definition: {identifier}")
        _validate_maturity_history(root, record)
        definition = definitions.get(identifier)
        if definition is None:
            continue
        previous_report: str | None = None
        for position, transition in enumerate(record["history"]):
            if transition["state"] not in {"prepared", "verified", "accepted"}:
                continue
            previous_report = _validate_stage_evidence(
                root,
                definition=definition,
                definition_sha256=fingerprints[identifier],
                transition=transition,
                predecessor=previous_report,
                history_position=position,
                correction=(
                    transition["state"] == "verified"
                    and position > 0
                    and record["history"][position - 1]["state"] == "rejected"
                ),
            )
    missing = sorted(set(fingerprints) - set(maturity))
    extra = sorted(set(maturity) - set(fingerprints))
    if missing or extra:
        detail = ", ".join([*(f"missing {item}" for item in missing), *(f"unknown {item}" for item in extra)])
        raise CatalogError(f"maturity records do not match definitions: {detail}")
    return maturity


@dataclass
class Catalog:
    """Immutable profile data plus checked content-addressed evidence indexes."""

    definitions: Mapping[str, WorkloadDefinition]
    profiles: Mapping[str, ClusterProfile]
    selectors: Mapping[str, str]
    definition_fingerprints: Mapping[str, str]
    profile_fingerprints: Mapping[str, str]
    maturity: dict[str, str]
    maturity_fingerprints: Mapping[str, str]
    accepted_profiles: Mapping[str, tuple[str, ...]]
    package_families: Mapping[str, PackageFamily]
    workload_deployments: Mapping[str, WorkloadDeployment]
    legacy_workload_deployments: Mapping[str, WorkloadDeployment]

    @classmethod
    def load(cls, root: Path) -> Catalog:
        root = root.resolve()
        definitions = _load_definitions(root)
        package_families = _load_package_families(root)
        workload_deployments = _load_workload_deployments(root, package_families)
        legacy_workload_deployments = _project_legacy_deployments(
            definitions, workload_deployments
        )
        _validate_model_owned_storage_paths(definitions)
        profiles = _load_profiles(root)
        selectors = _load_selectors(root / "config/profile-selectors.toml", profiles)
        definition_fingerprints = {key: fingerprint(value) for key, value in definitions.items()}
        locks = _load_locks(root / "locks/model-definitions.toml")
        missing_locks = sorted(set(definitions) - set(locks))
        extra_locks = sorted(set(locks) - set(definitions))
        if missing_locks or extra_locks:
            detail = ", ".join([*(f"missing {item}" for item in missing_locks), *(f"unknown {item}" for item in extra_locks)])
            raise CatalogError(f"definition locks do not match catalog: {detail}")
        for identifier, expected in definition_fingerprints.items():
            if locks[identifier] != expected:
                raise CatalogError(f"lock fingerprint does not match definition: {identifier}")

        validate_evidence_indexes(root)
        maturity_index = _load_json(root / "inventory/reports/model-definitions.json")
        for definition in definitions.values():
            _validate_runtime_release(root, definition)
        maturity = _maturity_records(
            root, maturity_index, definition_fingerprints, definitions
        )
        maturity_fingerprints = {
            record["id"]: record["sha256"] for record in maturity_index["definitions"]
        }
        accepted_index = _load_json(root / "inventory/reports/accepted-cluster-profiles.json")
        accepted_profiles: dict[str, tuple[str, ...]] = {}
        for record in accepted_index["profiles"]:
            profile_hash = record["profile_sha256"]
            if profile_hash in accepted_profiles:
                raise CatalogError(f"duplicate accepted profile fingerprint: {profile_hash}")
            hashes = tuple(record["definition_sha256"])
            if tuple(sorted(hashes)) != hashes or len(set(hashes)) != len(hashes):
                raise CatalogError("accepted definition hashes must be sorted and unique")
            context = f"accepted profile {profile_hash}"
            _audit_timestamp(record["accepted_at"], context)
            _validate_evidence_refs(root, record["evidence_refs"], context)
            accepted_profiles[profile_hash] = hashes

        return cls(
            definitions=MappingProxyType(definitions),
            profiles=MappingProxyType(profiles),
            selectors=MappingProxyType(selectors),
            definition_fingerprints=MappingProxyType(definition_fingerprints),
            profile_fingerprints=MappingProxyType(
                {key: fingerprint(value) for key, value in profiles.items()}
            ),
            maturity=maturity,
            maturity_fingerprints=MappingProxyType(maturity_fingerprints),
            accepted_profiles=MappingProxyType(accepted_profiles),
            package_families=MappingProxyType(package_families),
            workload_deployments=MappingProxyType(workload_deployments),
            legacy_workload_deployments=MappingProxyType(legacy_workload_deployments),
        )

    def resolve_profile(self, selector: str) -> ClusterProfile:
        try:
            return self.profiles[self.selectors[selector]]
        except KeyError as error:
            raise CatalogError(f"unknown profile selector: {selector}") from error
