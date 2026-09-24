"""Sequential, profile-based physical qualification of exact recipe releases.

The campaign is deliberately one recipe at a time.  Every workload change is
an ordinary whole-fleet profile preview/load; physical fault and restart steps
are observed from durable Controller Fleet evidence and remain operator-owned.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
import time
import urllib.parse
import uuid
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

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
from .generated_control.models.fleet_profile_application_view import (
    FleetProfileApplicationView,
)
from .generated_control.models.fleet_profile_preview import FleetProfilePreview
from .generated_control.models.fleet_profile_view import FleetProfileView
from .generated_control.models.fleet_snapshot import FleetSnapshot
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
    raw: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class CampaignAuthority:
    authority_id: str
    sha256: str
    catalog: Mapping[str, object]
    max_node_count: int
    excluded_topology_recipe_keys: tuple[str, ...]
    rows: tuple[RecipeAuthorityRow, ...]
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


def _bounded_number(
    value: object, label: str, default: float, minimum: float, maximum: float
) -> float:
    if value is None:
        return default
    if (
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise QualificationError(f"{label} must be between {minimum:g} and {maximum:g}")
    return float(value)


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
    root = _object(value, "qualification authority")
    _exact_keys(
        root,
        required={"schema_version", "authority_id", "catalog", "scope", "recipes"},
        label="qualification authority",
    )
    if type(root["schema_version"]) is not int or root["schema_version"] != 3:
        raise QualificationError("qualification authority schema_version must be 3")
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
    catalog_count = _integer(catalog["recipe_count"], "catalog recipe_count", 1, 1000)

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
    recipe_count = _integer(scope["recipe_count"], "scope recipe_count", 1, 1000)
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
        _exact_keys(
            row,
            required={
                "sequence",
                "key",
                "content_sha256",
                "node_count",
                "interface",
                "recipe_version",
                "package",
                "disposition",
                "operator_acceptance_required",
                "model_license_refs",
                "qualification_inputs",
                "smoke_cases",
                "review_gates",
            },
            optional={"disposition_reason"},
            label=f"authority recipe {position}",
        )
        sequence = _integer(row["sequence"], f"recipe {position} sequence", 1, 1000)
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
                dict(row),
            )
        )
    if len(rows) != recipe_count or catalog_count - len(excluded) != recipe_count:
        raise QualificationError("authority scope does not close over its catalog")
    if seen & set(excluded):
        raise QualificationError("excluded topology recipes appear in the test scope")
    return CampaignAuthority(
        authority_id,
        hashlib.sha256(raw).hexdigest(),
        dict(catalog),
        maximum,
        excluded,
        tuple(rows),
        dict(root),
    )


def load_manifest(path: Path, library_root: Path) -> CampaignManifest:
    value, raw = _strict_read(path, "campaign manifest")
    root = _object(value, "campaign manifest")
    _exact_keys(
        root,
        required={"schema_version", "qualification_authority", "fixture_manifest"},
        optional={"options"},
        label="campaign manifest",
    )
    if type(root["schema_version"]) is not int or root["schema_version"] != 2:
        raise QualificationError("campaign manifest schema_version must be 2")
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
    _exact_keys(
        options,
        required=set(),
        optional={
            "cleanup",
            "operation_timeout_seconds",
            "poll_interval_seconds",
        },
        label="campaign options",
    )
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
        _bounded_number(
            options.get("operation_timeout_seconds"),
            "operation_timeout_seconds",
            7_200,
            1,
            86_400,
        ),
        _bounded_number(
            options.get("poll_interval_seconds"), "poll_interval_seconds", 5, 0.1, 60
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


def _canonical_recipe_package_tools(
    library_root: Path,
) -> tuple[
    Callable[[bytes, dict[str, object], dict[str, dict[str, object]]], None],
    Any,
    Any,
    Callable[[Any], str],
    str,
]:
    import runpy

    validator_path = _relative_path(
        library_root,
        "tools/build-catalog-index",
        "canonical recipe package validator",
    )
    contracts_init = _relative_path(
        library_root,
        "contracts/src/vonk_forge_contracts/__init__.py",
        "canonical recipe contracts",
    )
    try:
        tool_namespace = runpy.run_path(str(validator_path))
        contract_source = str(contracts_init.parent.parent)
        sys.path[:] = [entry for entry in sys.path if entry != contract_source]
        sys.path.insert(0, contract_source)
        for module_name in tuple(sys.modules):
            if module_name == "vonk_forge_contracts" or module_name.startswith(
                "vonk_forge_contracts."
            ):
                del sys.modules[module_name]
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
            "loaded recipe contracts do not belong to the reviewed recipe repository"
        )
    candidate_validator = tool_namespace.get("validate_recipe_archive")
    if not callable(candidate_validator):
        raise QualificationError("canonical recipe package validator is unavailable")
    validator = cast(
        Callable[[bytes, dict[str, object], dict[str, dict[str, object]]], None],
        candidate_validator,
    )
    package_media_type = _string(
        tool_namespace.get("PACKAGE_MEDIA_TYPE"),
        "canonical recipe package media type",
    )
    return (
        validator,
        RecipeDefinition,
        ModelDefinition,
        content_sha256,
        package_media_type,
    )


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
    source_commit = catalog_document.get("source_commit")
    if source_commit != manifest.authority.catalog["source_commit"]:
        raise QualificationError("catalog source commit differs from its authority")

    recipe_entries = _catalog_recipe_entries(catalog_document)
    catalog_models = _catalog_model_entries(catalog_document)
    (
        validate_recipe_archive,
        recipe_type,
        model_type,
        content_sha256,
        expected_package_media_type,
    ) = _canonical_recipe_package_tools(root)
    for row in manifest.authority.rows:
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


def _online(node: Mapping[str, object]) -> bool:
    connection = node.get("connection")
    return (
        isinstance(connection, Mapping) and connection.get("online_state") == "online"
    )


def _offline(node: Mapping[str, object]) -> bool:
    connection = node.get("connection")
    return (
        isinstance(connection, Mapping) and connection.get("online_state") == "offline"
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


def _check_replacement_preview(
    preview: Mapping[str, object],
    *,
    fleet: Mapping[str, object],
    node_ids: Sequence[str],
    replacement: Mapping[str, object] | None,
) -> dict[str, object] | None:
    summary = _object(preview.get("summary"), "profile preview summary")
    if replacement is None:
        _assert_fleet_exclusive(fleet)
        if summary.get("stops") != 0:
            raise QualificationError(
                "profile preview contains an unacknowledged workload stop"
            )
        return None

    run_id = _string(replacement.get("run_id"), "acknowledged run ID")
    observed = _assert_fleet_exclusive(fleet, replace_run_id=run_id)
    if observed != dict(replacement):
        raise QualificationError(
            "the acknowledged run or its complete Fleet membership changed during preview"
        )
    if summary.get("stops") != 1 or summary.get("starts") != 1:
        raise QualificationError(
            "whole-fleet preview does not contain exactly one run stop and one replacement start"
        )
    if summary.get("uninstalls") != 0:
        raise QualificationError(
            "workload replacement preview must retain cached assets"
        )
    raw_steps = preview.get("steps")
    if not isinstance(raw_steps, list) or len(raw_steps) != 1:
        raise QualificationError(
            "whole-fleet replacement preview must contain one aggregate switch step"
        )
    step = _object(raw_steps[0], "profile replacement switch step")
    if step.get("kind") != "switch":
        raise QualificationError(
            "whole-fleet replacement preview has an unsupported step"
        )
    step_nodes = _string_array(step.get("node_ids"), "replacement switch node IDs")
    fleet_nodes = set(_nodes(fleet))
    members = _string_array(
        replacement.get("member_node_ids"), "acknowledged run member node IDs"
    )
    if (
        not set(step_nodes) <= fleet_nodes
        or not set(members) <= set(step_nodes)
        or not set(node_ids) <= set(step_nodes)
    ):
        raise QualificationError(
            "replacement switch step does not cover the complete acknowledged run "
            "and requested profile assignment inside the current Fleet"
        )
    plan_digest = _string(preview.get("plan_digest"), "profile plan digest")
    return {
        **dict(replacement),
        "acknowledged_run_id": run_id,
        "switch_node_ids": list(step_nodes),
        "stop_count": 1,
        "start_count": 1,
        "profile_plan_digest": plan_digest,
    }


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
    image_layout = image.get("oci_layout_sha256")
    image_bytes = image.get("image_bytes")
    if (
        not isinstance(image_digest, str)
        or _OCI_DIGEST.fullmatch(image_digest) is None
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
        "oci_layout_sha256": image_layout,
        "target_node_ids": sorted(node_ids),
    }


def _check_preview(
    raw_preview: Mapping[str, object],
    *,
    profile: Mapping[str, object],
    fleet: Mapping[str, object],
    row: RecipeAuthorityRow,
    node_ids: Sequence[str],
    cleanup: bool = False,
    owned_run_ids: AbstractSet[str] = frozenset(),
    replacement: Mapping[str, object] | None = None,
) -> dict[str, object]:
    try:
        preview = FleetProfilePreview.from_dict(raw_preview).to_dict()
    except (KeyError, TypeError, ValueError) as error:
        raise QualificationError("Controller profile preview is invalid") from error
    if preview.get("allowed") is not True:
        reasons = preview.get("reasons")
        raise QualificationError(f"whole-fleet profile preview is blocked: {reasons}")
    expected_scope = sorted(_nodes(fleet))
    scope = _object(preview.get("scope"), "profile preview scope")
    if scope.get("node_ids") != expected_scope:
        raise QualificationError(
            "profile preview does not bind the complete current Fleet"
        )
    if cleanup:
        if scope.get("idle_node_ids") != expected_scope:
            raise QualificationError(
                "cleanup preview does not leave the complete Fleet idle"
            )
        summary = _object(preview.get("summary"), "cleanup preview summary")
        if summary.get("uninstalls") != 0:
            raise QualificationError(
                "cleanup would uninstall assets instead of keeping cache"
            )
        steps = preview.get("steps")
        if not isinstance(steps, list):
            raise QualificationError("cleanup preview steps are invalid")
        _assert_fleet_exclusive(fleet, owned_run_ids=owned_run_ids)
        loaded_runs = _all_loaded_runs(fleet)
        if loaded_runs != owned_run_ids or len(owned_run_ids) > 1:
            raise QualificationError(
                "cleanup preview cannot bind its whole-Fleet effects to one exact campaign run"
            )
        step_summary = (
            summary.get("already_correct"),
            summary.get("placements"),
            summary.get("builds"),
            summary.get("distributions"),
            summary.get("installs"),
            summary.get("starts"),
        )
        if any(value != 0 for value in step_summary):
            raise QualificationError(
                "cleanup preview contains effects beyond stopping the campaign run"
            )
        loaded_nodes: set[str] = set()
        for node_id, node in _nodes(fleet).items():
            loaded = node.get("loaded")
            if not isinstance(loaded, list):
                raise QualificationError(
                    f"Fleet loaded-run list is invalid for {node_id}"
                )
            if any(
                _object(raw_presence, "Fleet run presence").get("run_id")
                in owned_run_ids
                for raw_presence in loaded
            ):
                loaded_nodes.add(node_id)
        if owned_run_ids:
            if summary.get("stops") != len(owned_run_ids) or not steps:
                raise QualificationError(
                    "cleanup preview does not stop the exact campaign run"
                )
            stepped_nodes: set[str] = set()
            for raw_step in steps:
                step = _object(raw_step, "cleanup plan step")
                step_nodes = step.get("node_ids")
                if (
                    step.get("kind") != "switch"
                    or not isinstance(step_nodes, list)
                    or not step_nodes
                    or any(not isinstance(node_id, str) for node_id in step_nodes)
                    or stepped_nodes.intersection(step_nodes)
                ):
                    raise QualificationError(
                        "cleanup plan step does not identify the campaign run's exact Sparks"
                    )
                stepped_nodes.update(step_nodes)
            if stepped_nodes != loaded_nodes:
                raise QualificationError(
                    "cleanup plan stop scope differs from the exact campaign run's Sparks"
                )
        elif summary.get("stops") != 0 or steps:
            raise QualificationError(
                "cleanup preview contains an unowned whole-Fleet stop effect"
            )
        if _profile_assignments_equal(profile, []):
            return preview
        raise QualificationError("cleanup preview profile is not empty")
    idle = scope.get("idle_node_ids")
    expected_idle = sorted(set(expected_scope) - set(node_ids))
    if idle != expected_idle:
        raise QualificationError(
            "profile preview does not leave all unassigned Sparks idle"
        )
    if preview.get("profile_id") != profile.get("id") or preview.get(
        "profile_digest"
    ) != profile.get("profile_digest"):
        raise QualificationError("profile preview is bound to another saved profile")
    summary = _object(preview.get("summary"), "profile preview summary")
    replacement_interruption = _check_replacement_preview(
        preview,
        fleet=fleet,
        node_ids=node_ids,
        replacement=replacement,
    )
    if summary.get("uninstalls") != 0:
        raise QualificationError("profile preview would uninstall cached assets")
    assignments = preview.get("assignments")
    if not isinstance(assignments, list) or len(assignments) != 1:
        raise QualificationError(
            "profile preview must contain one exact recipe assignment"
        )
    assignment = _object(assignments[0], "profile assignment preview")
    if (
        assignment.get("node_ids") != sorted(node_ids)
        or assignment.get("desired_state") != "running"
    ):
        raise QualificationError(
            "profile preview changed the exact Spark/desired-state binding"
        )
    preparations = _validate_preparations(preview, row, node_ids)
    checked = {**preview, "exact_preparations": preparations}
    if replacement_interruption is not None:
        checked["replacement_interruption"] = replacement_interruption
    return checked


def _run_id_from_application(
    application: Mapping[str, object], node_count: int
) -> tuple[str, Mapping[str, object]]:
    progress = _object(application.get("progress"), "profile application progress")
    step_results = progress.get("step_results")
    if not isinstance(step_results, Mapping):
        raise QualificationError("profile application lacks durable switch receipts")
    final_verifications: list[Mapping[str, object]] = []

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            if value.get("phase") == "final_verify" and isinstance(
                value.get("run_id"), str
            ):
                final_verifications.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(step_results)
    distinct = {str(item["run_id"]) for item in final_verifications}
    if len(distinct) != 1 or len(final_verifications) != 1:
        raise QualificationError(
            "profile application lacks one exact run final-verification receipt"
        )
    final = final_verifications[0]
    ranks = final.get("ranks")
    if (
        final.get("final_verified") is not True
        or final.get("healthy") is not True
        or final.get("state") != "running"
        or final.get("route_state") != "published"
        or not isinstance(ranks, list)
        or len(ranks) != node_count
    ):
        raise QualificationError("recipe run has no complete healthy serving receipt")
    ranks_by_node: dict[str, int] = {}
    for raw_rank in ranks:
        item = _object(raw_rank, "rank receipt")
        node_id = item.get("node_id")
        rank = item.get("rank")
        if (
            not isinstance(node_id, str)
            or not node_id
            or node_id in ranks_by_node
            or type(rank) is not int
            or item.get("state") != "running"
            or item.get("fresh") is not True
        ):
            raise QualificationError("recipe run rank receipts are incomplete or stale")
        ranks_by_node[node_id] = rank
    if len(ranks_by_node) != node_count or set(ranks_by_node.values()) != set(
        range(node_count)
    ):
        raise QualificationError("recipe run rank receipts are not contiguous")
    return str(final["run_id"]), final


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
        rows.append(dict(presence))
    if observed_nodes != set(node_ids):
        raise QualificationError("Fleet run presence node set differs from assignment")
    return rows


def _assert_no_campaign_run(
    client: Any, fleet: Mapping[str, object], run_id: str, alias: str
) -> None:
    if _run_presences(fleet, run_id):
        raise QualificationError("profile cleanup did not stop the exact campaign run")
    if _endpoint_exists(client, alias):
        raise QualificationError("profile cleanup did not withdraw the serving route")


def _latest_payload(
    ledger: EvidenceLedger, campaign_id: str, key: str, event: str
) -> Mapping[str, object] | None:
    for record in reversed(ledger.recipe_records(campaign_id, key)):
        if record.get("event") == event:
            return _object(record.get("payload"), f"{event} payload")
    return None


def _record_restart_baseline(
    *,
    client: Any,
    row: RecipeAuthorityRow,
    campaign_id: str,
    run_id: str,
    alias: str,
    node_ids: Sequence[str],
    ledger: EvidenceLedger,
) -> Mapping[str, object]:
    existing = _latest_payload(ledger, campaign_id, row.key, "host-restart.baseline")
    if existing is not None:
        nodes = existing.get("nodes")
        if isinstance(nodes, Mapping) and set(nodes) == set(node_ids):
            return existing
        raise QualificationError(
            "existing restart baseline is bound to another Spark group"
        )
    fleet = _typed_fleet(client)
    _assert_no_campaign_run(client, fleet, run_id, alias)
    if _all_loaded_runs(fleet):
        raise QualificationError(
            "restart baseline requires an otherwise idle whole Fleet"
        )
    baselines: dict[str, str] = {}
    for node_id in node_ids:
        node = _nodes(fleet).get(node_id)
        if node is None or not _online(node):
            raise QualificationError("selected Spark must be online after safe cleanup")
        boot_id = _boot_id(node)
        if boot_id is None:
            raise QualificationError(
                f"{node_id} lacks live serialized Fleet boot-ID telemetry"
            )
        baselines[node_id] = boot_id
    baseline = {"nodes": baselines, "route_alias": alias, "run_id": run_id}
    ledger.append(
        "host-restart.baseline",
        plan_digest=campaign_id,
        recipe=row.key,
        payload=baseline,
    )
    return baseline


def _record_cleanup_then_restart_baseline(
    *,
    receipt: Mapping[str, object],
    client: Any,
    row: RecipeAuthorityRow,
    campaign_id: str,
    run_id: str,
    alias: str,
    node_ids: Sequence[str],
    ledger: EvidenceLedger,
) -> None:
    ledger.append(
        "profile.cleanup.completed",
        plan_digest=campaign_id,
        recipe=row.key,
        payload=dict(receipt),
    )
    _record_restart_baseline(
        client=client,
        row=row,
        campaign_id=campaign_id,
        run_id=run_id,
        alias=alias,
        node_ids=node_ids,
        ledger=ledger,
    )


def _profile_cleanup(
    *,
    client: Any,
    profile_number: int,
    authority_id: str,
    ledger_id: str,
    row: RecipeAuthorityRow,
    campaign_id: str,
    run_id: str,
    alias: str,
    node_ids: Sequence[str],
    ledger: EvidenceLedger,
    options: CampaignManifest,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> Mapping[str, object]:
    existing_cleanup = _latest_payload(
        ledger, campaign_id, row.key, "profile.cleanup.completed"
    )
    if existing_cleanup is not None:
        if (
            existing_cleanup.get("run_id") != run_id
            or existing_cleanup.get("alias") != alias
            or existing_cleanup.get("node_ids") != sorted(node_ids)
            or existing_cleanup.get("application_state") != "succeeded"
            or existing_cleanup.get("cleanup_policy") != "stop"
            or existing_cleanup.get("uninstalls") != 0
            or existing_cleanup.get("route_withdrawn") is not True
            or existing_cleanup.get("run_absent_from_fleet") is not True
        ):
            raise QualificationError(
                "existing cleanup receipt is not bound to this exact canary"
            )
        _record_restart_baseline(
            client=client,
            row=row,
            campaign_id=campaign_id,
            run_id=run_id,
            alias=alias,
            node_ids=node_ids,
            ledger=ledger,
        )
        return existing_cleanup

    load_intent = _latest_payload(
        ledger, campaign_id, row.key, "profile.load.requested"
    )
    load_submitted = _latest_payload(
        ledger, campaign_id, row.key, "profile.load.submitted"
    )
    canary = _latest_payload(ledger, campaign_id, row.key, "canary.completed")
    if load_intent is None or load_submitted is None or canary is None:
        raise QualificationError(
            "cleanup requires the durable reviewed load and canary receipts"
        )
    load_request_key = load_intent.get("request_key")
    load_plan_digest = load_intent.get("plan_digest")
    load_profile_digest = load_intent.get("profile_digest")
    load_preview = _object(load_intent.get("preview"), "durable load preview")
    load_application = _object(canary.get("application"), "canary application receipt")
    load_profile_id = load_preview.get("profile_id")
    if (
        load_request_key != _request_key(campaign_id, row.key, "load")
        or not isinstance(load_plan_digest, str)
        or _SHA256.fullmatch(load_plan_digest) is None
        or not isinstance(load_profile_digest, str)
        or _SHA256.fullmatch(load_profile_digest) is None
        or not isinstance(load_profile_id, str)
        or not load_profile_id
        or load_preview.get("plan_digest") != load_plan_digest
        or load_preview.get("profile_digest") != load_profile_digest
        or not isinstance(load_application.get("id"), str)
        or not load_application.get("id")
        or load_submitted.get("request_key") != load_request_key
        or load_submitted.get("application_id") != load_application.get("id")
        or load_submitted.get("plan_digest") != load_plan_digest
        or load_submitted.get("profile_digest") != load_profile_digest
        or load_application.get("profile_id") != load_profile_id
        or load_application.get("profile_digest") != load_profile_digest
        or load_application.get("plan_digest")
        != _application_plan_digest(load_plan_digest, str(load_request_key))
        or load_application.get("state") != "succeeded"
        or canary.get("run_id") != run_id
        or canary.get("alias") != alias
        or set(_ordered_rank_nodes(canary.get("node_to_rank"), row.node_count))
        != set(node_ids)
    ):
        raise QualificationError(
            "canary receipt differs from the exact durable profile load"
        )
    if (
        load_intent.get("node_ids") != sorted(node_ids)
        or load_intent.get("alias") != alias
        or load_intent.get("operator_gate_accepted")
        is not row.operator_acceptance_required
        or load_intent.get("capacity_review_accepted")
        is not any(gate.get("kind") == "capacity-review" for gate in row.review_gates)
    ):
        raise QualificationError(
            "durable profile load intent does not authorize this cleanup"
        )

    cleanup_request = _latest_payload(
        ledger, campaign_id, row.key, "profile.cleanup.requested"
    )
    submitted = _latest_payload(
        ledger, campaign_id, row.key, "profile.cleanup.submitted"
    )
    if submitted is not None and cleanup_request is None:
        raise QualificationError("cleanup submission lacks its durable reviewed intent")

    profile: dict[str, object] | None = None
    fleet: dict[str, object] | None = None
    accepted: dict[str, object] | None = None
    request_key = _request_key(campaign_id, row.key, "cleanup")
    if cleanup_request is not None:
        if (
            cleanup_request.get("request_key") != request_key
            or cleanup_request.get("run_id") != run_id
            or cleanup_request.get("alias") != alias
            or cleanup_request.get("node_ids") != sorted(node_ids)
            or cleanup_request.get("source_request_key") != load_request_key
            or cleanup_request.get("source_plan_digest") != load_plan_digest
        ):
            raise QualificationError(
                "durable cleanup intent differs from the exact canary"
            )
        plan_digest = cleanup_request.get("plan_digest")
        profile_id = cleanup_request.get("profile_id")
        profile_digest = cleanup_request.get("profile_digest")
        preview = _object(cleanup_request.get("preview"), "durable cleanup preview")
        if (
            not isinstance(plan_digest, str)
            or _SHA256.fullmatch(plan_digest) is None
            or preview.get("plan_digest") != plan_digest
            or preview.get("profile_id") != profile_id
            or preview.get("profile_digest") != profile_digest
            or profile_id != load_profile_id
            or not isinstance(profile_id, str)
            or not profile_id
            or not isinstance(profile_digest, str)
            or _SHA256.fullmatch(profile_digest) is None
        ):
            raise QualificationError(
                "durable cleanup intent lacks its exact reviewed preview"
            )
        accepted = _lookup_load_request(
            client,
            profile_number,
            request_key,
            plan_digest=plan_digest,
            profile_id=str(profile_id),
            profile_digest=profile_digest,
        )
        if submitted is not None:
            if (
                submitted.get("request_key") != request_key
                or submitted.get("application_id") is None
                or submitted.get("plan_digest") != plan_digest
                or submitted.get("profile_digest") != profile_digest
                or submitted.get("profile_id") != profile_id
            ):
                raise QualificationError(
                    "cleanup submission differs from its durable intent"
                )
            submitted_id = submitted.get("application_id")
            if accepted is not None and accepted.get("id") != submitted_id:
                raise QualificationError(
                    "cleanup request lookup differs from the submitted application"
                )
            accepted = {"id": submitted_id}
        elif accepted is None:
            profile = _profile_view(client, profile_number)
            _assert_profile_owner(profile, authority_id, ledger_id)
            if (
                profile.get("id") != profile_id
                or profile.get("profile_digest") != profile_digest
                or not _profile_assignments_equal(profile, [])
            ):
                raise QualificationError(
                    "dedicated profile changed after cleanup preview; refusing stale request replay"
                )
            fleet = _typed_fleet(client)
            _assert_fleet_exclusive(fleet, owned_run_ids={run_id})
            if _run_presences(fleet, run_id):
                _check_serving_fleet(
                    fleet,
                    run_id=run_id,
                    revision_id=_string(
                        canary.get("recipe_revision_id"), "canary revision ID"
                    ),
                    alias=alias,
                    node_ids=node_ids,
                    expected_run_state="running",
                    expected_route_state="published",
                    expected_health=True,
                )
            elif _endpoint_exists(client, alias):
                raise QualificationError(
                    "campaign route remains published without its exact Fleet run"
                )
            checked_preview = _check_preview(
                preview,
                profile=profile,
                fleet=fleet,
                row=row,
                node_ids=node_ids,
                cleanup=True,
                owned_run_ids={run_id} if _run_presences(fleet, run_id) else set(),
            )
            if checked_preview.get("plan_digest") != plan_digest:
                raise QualificationError(
                    "cleanup preview changed before its exact retry"
                )
            accepted = _submit_load(
                client,
                profile_number,
                request_key,
                plan_digest=plan_digest,
                profile_id=str(profile_id),
                profile_digest=profile_digest,
            )
    else:
        fleet = _typed_fleet(client)
        _assert_fleet_exclusive(fleet, owned_run_ids={run_id})
        run_presences = _run_presences(fleet, run_id)
        if run_presences:
            _check_serving_fleet(
                fleet,
                run_id=run_id,
                revision_id=_string(
                    canary.get("recipe_revision_id"), "canary revision ID"
                ),
                alias=alias,
                node_ids=node_ids,
                expected_run_state="running",
                expected_route_state="published",
                expected_health=True,
            )
        elif _endpoint_exists(client, alias):
            raise QualificationError(
                "campaign route remains published without its exact Fleet run"
            )
        profile = _profile_view(client, profile_number)
        _assert_profile_owner(profile, authority_id, ledger_id)
        assignments = profile.get("assignments")
        if not isinstance(assignments, list):
            raise QualificationError("dedicated profile assignment list is invalid")
        if assignments and not _profile_assignments_equal(
            profile, [_profile_assignment(row, node_ids, alias)]
        ):
            raise QualificationError(
                "dedicated profile contains a non-campaign assignment"
            )
        if assignments:
            profile = _save_profile(
                client,
                profile,
                assignments=[],
                authority_id=authority_id,
                ledger_id=ledger_id,
            )
        # Re-read all fleet state after the profile save; the subsequent
        # preview and plan digest must describe the complete current scope.
        fleet = _typed_fleet(client)
        _assert_fleet_exclusive(fleet, owned_run_ids={run_id})
        run_presences = _run_presences(fleet, run_id)
        if run_presences:
            _check_serving_fleet(
                fleet,
                run_id=run_id,
                revision_id=_string(
                    canary.get("recipe_revision_id"), "canary revision ID"
                ),
                alias=alias,
                node_ids=node_ids,
                expected_run_state="running",
                expected_route_state="published",
                expected_health=True,
            )
        elif _endpoint_exists(client, alias):
            raise QualificationError(
                "campaign route remains published without its exact Fleet run"
            )
        raw_preview = client.request("POST", f"/api/profile/{profile_number}/preview")
        preview = _check_preview(
            raw_preview,
            profile=profile,
            fleet=fleet,
            row=row,
            node_ids=node_ids,
            cleanup=True,
            owned_run_ids={run_id} if run_presences else set(),
        )
        summary = _object(preview.get("summary"), "cleanup preview summary")
        if summary.get("uninstalls") != 0:
            raise QualificationError("cleanup preview would uninstall cached assets")
        profile_id = _string(preview.get("profile_id"), "cleanup profile ID")
        profile_digest = _string(
            preview.get("profile_digest"), "cleanup profile digest"
        )
        plan_digest = _string(
            preview.get("plan_digest"), "reviewed cleanup plan digest"
        )
        cleanup_request = {
            "request_key": request_key,
            "run_id": run_id,
            "alias": alias,
            "node_ids": sorted(node_ids),
            "profile_id": profile_id,
            "profile_digest": profile_digest,
            "plan_digest": plan_digest,
            "source_request_key": load_request_key,
            "source_plan_digest": load_plan_digest,
            "preview": preview,
        }
        ledger.append(
            "profile.cleanup.requested",
            plan_digest=campaign_id,
            recipe=row.key,
            payload=cleanup_request,
        )
        accepted = _submit_load(
            client,
            profile_number,
            request_key,
            plan_digest=plan_digest,
            profile_id=profile_id,
            profile_digest=profile_digest,
        )

    if accepted is None:
        raise QualificationError(
            "cleanup request has no durable Controller application"
        )
    application_id = accepted.get("id")
    if not isinstance(application_id, str) or not application_id:
        raise QualificationError("cleanup load did not return a durable application")
    if submitted is None:
        assert cleanup_request is not None
        ledger.append(
            "profile.cleanup.submitted",
            plan_digest=campaign_id,
            recipe=row.key,
            payload={
                "request_key": request_key,
                "application_id": application_id,
                "profile_id": cleanup_request.get("profile_id"),
                "profile_digest": cleanup_request.get("profile_digest"),
                "plan_digest": cleanup_request.get("plan_digest"),
            },
        )
    application = _await_application(
        client,
        accepted,
        ledger=ledger,
        campaign_id=campaign_id,
        key=row.key,
        timeout=options.operation_timeout_seconds,
        interval=options.poll_interval_seconds,
        clock=clock,
        sleeper=sleeper,
    )
    request_plan_digest = _string(
        cleanup_request.get("plan_digest") if cleanup_request else None,
        "durable cleanup plan digest",
    )
    expected_profile_id = _string(
        cleanup_request.get("profile_id") if cleanup_request else None,
        "durable cleanup profile ID",
    )
    expected_profile_digest = _string(
        cleanup_request.get("profile_digest") if cleanup_request else None,
        "durable cleanup profile digest",
    )
    if (
        application.get("profile_id") != expected_profile_id
        or application.get("profile_digest") != expected_profile_digest
        or application.get("plan_digest")
        != _application_plan_digest(request_plan_digest, request_key)
    ):
        raise QualificationError(
            "cleanup application differs from its reviewed request identity"
        )
    fleet_after = _typed_fleet(client)
    _assert_no_campaign_run(client, fleet_after, run_id, alias)
    if _all_loaded_runs(fleet_after):
        raise QualificationError(
            "cleanup left another run active across the whole Fleet"
        )
    receipt = {
        "application_id": application.get("id"),
        "application_state": application.get("state"),
        "request_key": request_key,
        "profile_id": expected_profile_id,
        "preview_plan_digest": request_plan_digest,
        "profile_digest": expected_profile_digest,
        "cleanup_policy": "stop",
        "uninstalls": 0,
        "route_withdrawn": True,
        "run_absent_from_fleet": True,
        "run_id": run_id,
        "alias": alias,
        "node_ids": sorted(node_ids),
    }
    _record_cleanup_then_restart_baseline(
        receipt=receipt,
        client=client,
        row=row,
        campaign_id=campaign_id,
        run_id=run_id,
        alias=alias,
        node_ids=node_ids,
        ledger=ledger,
    )
    return receipt


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


def _current_row(
    manifest: CampaignManifest,
    ledger: EvidenceLedger,
    campaign_id: str,
    requested: str | None,
) -> RecipeAuthorityRow | None:
    complete = _completed_keys(ledger, campaign_id)
    next_row = next(
        (row for row in manifest.authority.rows if row.key not in complete), None
    )
    if requested is None:
        return next_row
    row = next(
        (item for item in manifest.authority.rows if item.key == requested), None
    )
    if row is None:
        raise QualificationError(
            f"recipe is not in the reviewed authority: {requested}"
        )
    if next_row is None or row.key != next_row.key:
        raise QualificationError(
            "recipes must run in the authority's sequence; "
            f"next recipe is {next_row.key if next_row else 'none'}"
        )
    return row


def _evidence_lock_nodes(
    ledger: EvidenceLedger, campaign_id: str, row: RecipeAuthorityRow
) -> list[str]:
    canary = _latest_payload(ledger, campaign_id, row.key, "canary.completed")
    if canary is not None:
        ranks = canary.get("node_to_rank")
        result = _ordered_rank_nodes(ranks, row.node_count)
    else:
        request = _latest_payload(
            ledger, campaign_id, row.key, "profile.load.requested"
        )
        if request is None:
            return []
        raw_nodes = request.get("node_ids")
        if not isinstance(raw_nodes, list) or any(
            not isinstance(item, str) for item in raw_nodes
        ):
            raise QualificationError("profile load intent has invalid Spark identities")
        result = list(raw_nodes)
    if (
        len(result) != row.node_count
        or len(set(result)) != row.node_count
        or any(_NODE_ID.fullmatch(node_id) is None for node_id in result)
    ):
        raise QualificationError("durable evidence has invalid Spark lock identities")
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


def _operator_gate(args: argparse.Namespace, row: RecipeAuthorityRow) -> None:
    acknowledgments = set(args.accept_operator_gate)
    if acknowledgments - {row.key}:
        raise QualificationError(
            "operator acceptance must name only the exact current recipe key"
        )
    if row.operator_acceptance_required and row.key not in acknowledgments:
        raise QualificationError(f"{row.key} requires --accept-operator-gate {row.key}")
    if not row.operator_acceptance_required and acknowledgments:
        raise QualificationError(
            f"{row.key} does not declare an operator-acceptance gate"
        )
    capacity_acknowledgments = set(args.accept_capacity_review)
    if capacity_acknowledgments - {row.key}:
        raise QualificationError(
            "capacity review must name only the exact current recipe key"
        )
    capacity_required = any(
        gate.get("kind") == "capacity-review" for gate in row.review_gates
    )
    if capacity_required and row.key not in capacity_acknowledgments:
        raise QualificationError(
            f"{row.key} requires --accept-capacity-review {row.key} after operator capacity review"
        )
    if not capacity_required and capacity_acknowledgments:
        raise QualificationError(f"{row.key} does not declare a capacity-review gate")


def _prepare_profile(
    *,
    client: Any,
    number: int,
    authority_id: str,
    ledger_id: str,
    row: RecipeAuthorityRow,
    node_ids: Sequence[str],
    alias: str,
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
    target = _profile_assignment(row, node_ids, alias)
    if current:
        if not _profile_assignments_equal(profile, [target]):
            raise QualificationError(
                "dedicated profile contains another assignment; clear it through the runner first"
            )
    else:
        profile = _save_profile(
            client,
            profile,
            assignments=[target],
            authority_id=authority_id,
            ledger_id=ledger_id,
        )
    return profile


def _preview_digest(
    *,
    manifest: CampaignManifest,
    fixtures: FixtureRegistry,
    row: RecipeAuthorityRow,
    profile: Mapping[str, object],
    preview: Mapping[str, object],
    node_ids: Sequence[str],
    failure_node_id: str | None,
    profile_number: int,
) -> str:
    return _digest(
        {
            "schema_version": 1,
            "campaign_manifest_sha256": manifest.sha256,
            "authority_sha256": manifest.authority.sha256,
            "fixture_manifest_sha256": fixtures.manifest_sha256,
            "authority_sequence": row.sequence,
            "recipe_key": row.key,
            "recipe_content_sha256": row.content_sha256,
            "node_ids": sorted(node_ids),
            "failure_node_id": failure_node_id,
            "profile_number": profile_number,
            "profile_digest": profile.get("profile_digest"),
            "profile_plan_digest": preview.get("plan_digest"),
            "exact_preparations": preview.get("exact_preparations"),
            "replacement_interruption": preview.get("replacement_interruption"),
        }
    )


def _fresh_profile_preview(
    *,
    client: Any,
    manifest: CampaignManifest,
    fixtures: FixtureRegistry,
    row: RecipeAuthorityRow,
    library_root: Path,
    profile_number: int,
    authority_id: str,
    ledger_id: str,
    node_ids: Sequence[str],
    failure_node_id: str | None,
    alias: str,
    kind: str,
    campaign_id: str,
    ledger: EvidenceLedger,
    expected_fleet_node_ids: Sequence[str],
    replace_run_id: str | None = None,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    detail, definition = _validate_current_recipe(client, row)
    fleet = _typed_fleet(client)
    _require_locked_fleet_roster(fleet, expected_fleet_node_ids)
    if len(_nodes(fleet)) < row.node_count:
        raise QualificationError(f"{row.key} does not fit the current complete Fleet")
    if any(not _online(node) for node in _nodes(fleet).values()):
        raise QualificationError(
            "whole-fleet profile execution requires every enrolled Spark online"
        )
    replacement = _assert_fleet_exclusive(fleet, replace_run_id=replace_run_id)
    profile = _prepare_profile(
        client=client,
        number=profile_number,
        authority_id=authority_id,
        ledger_id=ledger_id,
        row=row,
        node_ids=node_ids,
        alias=alias,
    )
    fleet = _typed_fleet(client)
    _require_locked_fleet_roster(fleet, expected_fleet_node_ids)
    if len(_nodes(fleet)) < row.node_count:
        raise QualificationError(f"{row.key} does not fit the current complete Fleet")
    confirmed_replacement = _assert_fleet_exclusive(
        fleet, replace_run_id=replace_run_id
    )
    if confirmed_replacement != replacement:
        raise QualificationError(
            "the acknowledged run or its complete Fleet membership changed while "
            "preparing the profile preview"
        )
    if any(not _online(node) for node in _nodes(fleet).values()):
        raise QualificationError(
            "whole-fleet profile execution requires every enrolled Spark online"
        )
    raw_preview = client.request("POST", f"/api/profile/{profile_number}/preview")
    preview_fleet = _typed_fleet(client)
    _require_locked_fleet_roster(preview_fleet, expected_fleet_node_ids)
    if len(_nodes(preview_fleet)) < row.node_count:
        raise QualificationError(f"{row.key} does not fit the current complete Fleet")
    if any(not _online(node) for node in _nodes(preview_fleet).values()):
        raise QualificationError(
            "whole-fleet profile execution requires every enrolled Spark online"
        )
    preview_replacement = _assert_fleet_exclusive(
        preview_fleet, replace_run_id=replace_run_id
    )
    if preview_replacement != replacement:
        raise QualificationError(
            "the acknowledged run or its complete Fleet membership changed while "
            "the Controller generated the profile preview"
        )
    fleet = preview_fleet
    checked = _check_preview(
        raw_preview,
        profile=profile,
        fleet=fleet,
        row=row,
        node_ids=node_ids,
        replacement=replacement,
    )
    assignments = checked.get("assignments")
    assert isinstance(assignments, list) and len(assignments) == 1
    assignment = _object(assignments[0], "profile assignment preview")
    revision_id = assignment.get("recipe_revision_id")
    if not isinstance(revision_id, str) or not revision_id:
        raise QualificationError("profile preview lacks exact recipe revision identity")
    # Bind the revision identifier into the validation performed above.
    checked["_exact_recipe_revision_id"] = revision_id
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
    if smoke_preview.get("available") is not True:
        raise QualificationError(f"{row.key} has no executable reviewed smoke fixture")
    if smoke_preview.get("fixture_manifest_sha256") != fixtures.manifest_sha256:
        raise QualificationError(f"{row.key} smoke fixture manifest changed")
    campaign_digest = _preview_digest(
        manifest=manifest,
        fixtures=fixtures,
        row=row,
        profile=profile,
        preview=checked,
        node_ids=node_ids,
        failure_node_id=failure_node_id,
        profile_number=profile_number,
    )
    identity = detail.get("identity")
    ledger.append(
        "plan.generated",
        plan_digest=campaign_id,
        recipe=row.key,
        payload={
            "campaign_digest": campaign_digest,
            "sequence": row.sequence,
            "authority_row": dict(row.raw),
            "controller_recipe_identity": {
                "recipe_id": identity.get("recipe_id")
                if isinstance(identity, Mapping)
                else None,
                "recipe_revision_id": revision_id,
                "content_sha256": row.content_sha256,
                "release_version": row.recipe_version,
            },
            "profile_number": profile_number,
            "profile_digest": profile.get("profile_digest"),
            "preview": checked,
            "replacement_interruption": checked.get("replacement_interruption"),
            "smoke_preview": smoke_preview,
        },
    )
    return (
        checked,
        smoke_preview,
        {"campaign_digest": campaign_digest, "profile": profile},
    )


def _load_and_smoke(
    *,
    client: Any,
    row: RecipeAuthorityRow,
    fixtures: FixtureRegistry,
    node_ids: Sequence[str],
    alias: str,
    kind: str,
    detail: Mapping[str, object],
    smoke_preview: Mapping[str, object],
    preview: Mapping[str, object],
    profile_number: int,
    campaign_id: str,
    ledger: EvidenceLedger,
    options: CampaignManifest,
    failure_node_id: str | None,
    operator_gate_accepted: bool,
    capacity_review_accepted: bool,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> tuple[str, Mapping[str, object], Mapping[str, object]]:
    existing = _latest_payload(ledger, campaign_id, row.key, "profile.load.submitted")
    request = _latest_payload(ledger, campaign_id, row.key, "profile.load.requested")
    if existing is not None and request is None:
        raise QualificationError(
            "profile application receipt lacks its durable load intent"
        )
    if existing is None:
        request_key = _request_key(campaign_id, row.key, "load")
        if request is None:
            request = {
                "request_key": request_key,
                "profile_digest": preview.get("profile_digest"),
                "plan_digest": preview.get("plan_digest"),
                "campaign_digest": (
                    _latest_payload(ledger, campaign_id, row.key, "plan.generated")
                    or {}
                ).get("campaign_digest"),
                "alias": alias,
                "smoke_kind": kind,
                "node_ids": sorted(node_ids),
                "failure_node_id": failure_node_id,
                "operator_gate_accepted": operator_gate_accepted,
                "capacity_review_accepted": capacity_review_accepted,
                "preview": dict(preview),
                "smoke_preview": dict(smoke_preview),
            }
            ledger.append(
                "profile.load.requested",
                plan_digest=campaign_id,
                recipe=row.key,
                payload=request,
            )
        if request is None:
            raise QualificationError("profile load intent was not durably recorded")
        if (
            request.get("request_key") != request_key
            or request.get("profile_digest") != preview.get("profile_digest")
            or request.get("plan_digest") != preview.get("plan_digest")
            or request.get("alias") != alias
            or request.get("smoke_kind") != kind
            or request.get("node_ids") != sorted(node_ids)
            or request.get("failure_node_id") != failure_node_id
            or request.get("operator_gate_accepted") is not operator_gate_accepted
            or request.get("capacity_review_accepted") is not capacity_review_accepted
            or request.get("preview") != dict(preview)
            or request.get("smoke_preview") != dict(smoke_preview)
        ):
            raise QualificationError(
                "durable load intent differs from this exact preview"
            )
        accepted = _submit_load(
            client,
            profile_number,
            request_key,
            plan_digest=_string(
                preview.get("plan_digest"), "reviewed profile plan digest"
            ),
            profile_id=_string(preview.get("profile_id"), "reviewed profile ID"),
            profile_digest=_string(
                preview.get("profile_digest"), "reviewed profile digest"
            ),
        )
        application_id = accepted.get("id")
        if not isinstance(application_id, str) or not application_id:
            raise QualificationError(
                "profile load did not return a durable application"
            )
        ledger.append(
            "profile.load.submitted",
            plan_digest=campaign_id,
            recipe=row.key,
            payload={
                "request_key": request_key,
                "application_id": application_id,
                "profile_digest": preview.get("profile_digest"),
                "plan_digest": preview.get("plan_digest"),
            },
        )
    else:
        if request is None:
            raise QualificationError(
                "profile application receipt lacks its durable load intent"
            )
        if (
            existing.get("request_key") != request.get("request_key")
            or existing.get("profile_digest") != request.get("profile_digest")
            or existing.get("plan_digest") != request.get("plan_digest")
        ):
            raise QualificationError(
                "profile application receipt differs from its durable intent"
            )
        application_id = existing.get("application_id")
        if not isinstance(application_id, str):
            raise QualificationError("ledger profile application identity is invalid")
        accepted = {"id": application_id}
    application = _await_application(
        client,
        accepted,
        ledger=ledger,
        campaign_id=campaign_id,
        key=row.key,
        timeout=options.operation_timeout_seconds,
        interval=options.poll_interval_seconds,
        clock=clock,
        sleeper=sleeper,
    )
    request = _latest_payload(ledger, campaign_id, row.key, "profile.load.requested")
    if request is None:
        raise QualificationError(
            "profile application receipt lacks its durable load intent"
        )
    request_key = _string(request.get("request_key"), "profile load request key")
    preview_plan_digest = _string(
        preview.get("plan_digest"), "reviewed profile plan digest"
    )
    if (
        application.get("profile_id") != preview.get("profile_id")
        or application.get("profile_digest") != preview.get("profile_digest")
        or application.get("plan_digest")
        != _application_plan_digest(preview_plan_digest, request_key)
    ):
        raise QualificationError("accepted profile application changed profile digest")
    request_key = request.get("request_key")
    preview_digest = preview.get("plan_digest")
    if not isinstance(request_key, str) or not isinstance(preview_digest, str):
        raise QualificationError(
            "accepted profile application lacks its execution identity"
        )
    if application.get("plan_digest") != _application_plan_digest(
        preview_digest, request_key
    ):
        raise QualificationError(
            "accepted profile application digest differs from the reviewed reconciliation and request"
        )
    run_id, final = _run_id_from_application(application, row.node_count)
    raw_ranks = final.get("ranks")
    if not isinstance(raw_ranks, list):
        raise QualificationError("profile application has invalid final rank receipts")
    node_to_rank: dict[str, int] = {}
    for value in raw_ranks:
        item = _object(value, "rank receipt")
        node_id = _string(item.get("node_id"), "rank receipt Spark ID")
        rank = item.get("rank")
        if type(rank) is not int or node_id in node_to_rank:
            raise QualificationError("profile application rank receipt is invalid")
        node_to_rank[node_id] = rank
    if set(node_to_rank) != set(node_ids):
        raise QualificationError(
            "run rank receipt does not match selected Spark identities"
        )
    fleet = _typed_fleet(client)
    presences = _check_serving_fleet(
        fleet,
        run_id=run_id,
        revision_id=str(preview["_exact_recipe_revision_id"]),
        alias=alias,
        node_ids=node_ids,
        expected_run_state="running",
        expected_route_state="published",
        expected_health=True,
    )
    if kind == "artifact-job":
        adapter = ArtifactJobSmokeAdapter(fixtures)
        smoke = adapter.run(
            client,
            run_id,
            smoke_preview,
            ledger=ledger,
            plan_digest=campaign_id,
            recipe_key=row.key,
            timeout_seconds=options.operation_timeout_seconds,
            poll_interval_seconds=options.poll_interval_seconds,
            clock=clock,
            sleeper=sleeper,
        )
    else:
        adapter = ServiceSmokeAdapter(fixtures)
        smoke = adapter.run(client, alias, smoke_preview)
    receipt = {
        "application": application,
        "run_id": run_id,
        "alias": alias,
        "smoke_kind": kind,
        "review_acknowledgements": {
            "operator_acceptance": operator_gate_accepted,
            "capacity_review": capacity_review_accepted,
        },
        "recipe_revision_id": preview.get("_exact_recipe_revision_id"),
        "recipe_content_sha256": row.content_sha256,
        "node_to_rank": node_to_rank,
        "fleet_rank_presence": presences,
        "exact_preparations": preview.get("exact_preparations"),
        "smoke": dict(smoke),
    }
    ledger.append(
        "canary.completed",
        plan_digest=campaign_id,
        recipe=row.key,
        payload=receipt,
    )
    return run_id, final, receipt


def _resume_load_and_smoke(
    *,
    client: Any,
    manifest: CampaignManifest,
    fixtures: FixtureRegistry,
    row: RecipeAuthorityRow,
    campaign_id: str,
    ledger: EvidenceLedger,
    profile_number: int,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> Mapping[str, object]:
    if _latest_payload(ledger, campaign_id, row.key, "canary.completed") is not None:
        raise QualificationError(
            "successful canary already exists; resume physical checkpoints"
        )
    request = _latest_payload(ledger, campaign_id, row.key, "profile.load.requested")
    if request is None:
        raise QualificationError(
            f"{row.key} has no durable profile load intent to resume"
        )
    application_observation = _latest_payload(
        ledger, campaign_id, row.key, "profile.application.observed"
    )
    if application_observation is not None and application_observation.get("state") in {
        "failed",
        "cancelled",
        "waiting-for-operator",
    }:
        raise QualificationError(
            "the durable profile application is terminal and its request key cannot be replayed; "
            "reconcile partial workload state before authorizing a fresh profile and ledger"
        )
    plan = _latest_payload(ledger, campaign_id, row.key, "plan.generated")
    if plan is None:
        raise QualificationError("durable load intent has no reviewed profile preview")
    request_key = request.get("request_key")
    plan_digest = request.get("plan_digest")
    profile_digest = request.get("profile_digest")
    preview = _object(request.get("preview"), "durable profile preview")
    smoke_preview = _object(request.get("smoke_preview"), "durable smoke preview")
    profile_id = preview.get("profile_id")
    if (
        request_key != _request_key(campaign_id, row.key, "load")
        or not isinstance(plan_digest, str)
        or _SHA256.fullmatch(plan_digest) is None
        or preview.get("plan_digest") != plan_digest
        or preview.get("profile_digest") != profile_digest
        or plan.get("profile_digest") != profile_digest
        or plan.get("preview") != dict(preview)
        or plan.get("smoke_preview") != dict(smoke_preview)
        or request.get("campaign_digest") != plan.get("campaign_digest")
        or not isinstance(profile_id, str)
    ):
        raise QualificationError(
            "durable load intent differs from its reviewed preview"
        )
    campaign_digest = plan.get("campaign_digest")
    node_ids = _string_array(request.get("node_ids"), "load-intent Spark IDs")
    if (
        len(node_ids) != row.node_count
        or len(set(node_ids)) != row.node_count
        or node_ids != sorted(node_ids)
        or any(_NODE_ID.fullmatch(node_id) is None for node_id in node_ids)
    ):
        raise QualificationError("durable load intent Spark identities are invalid")
    failure_node_id = request.get("failure_node_id")
    if row.node_count == 2:
        if not isinstance(failure_node_id, str) or failure_node_id not in node_ids:
            raise QualificationError(
                "durable distributed load intent lacks its failure Spark"
            )
    elif failure_node_id is not None:
        raise QualificationError("single-Spark load intent has a failure Spark")
    if campaign_digest != _preview_digest(
        manifest=manifest,
        fixtures=fixtures,
        row=row,
        profile={"profile_digest": profile_digest},
        preview=preview,
        node_ids=node_ids,
        failure_node_id=(failure_node_id if isinstance(failure_node_id, str) else None),
        profile_number=profile_number,
    ):
        raise QualificationError(
            "saved campaign digest no longer closes over its exact preview"
        )
    kind, _definition = _fixture_bindings(row, fixtures)
    if request.get("smoke_kind") != kind:
        raise QualificationError("durable load intent changed its smoke fixture kind")
    alias = _string(request.get("alias"), "load-intent route alias")
    if _ALIAS.fullmatch(alias) is None:
        raise QualificationError("durable load intent route alias is invalid")
    capacity_review_required = any(
        gate.get("kind") == "capacity-review" for gate in row.review_gates
    )
    if (
        request.get("operator_gate_accepted") is not row.operator_acceptance_required
        or request.get("capacity_review_accepted") is not capacity_review_required
    ):
        raise QualificationError(
            "durable load intent lacks exact recipe review acknowledgements"
        )

    existing_application = _lookup_load_request(
        client,
        profile_number,
        str(request_key),
        plan_digest=plan_digest,
        profile_id=profile_id,
        profile_digest=_string(profile_digest, "durable profile digest"),
    )
    if existing_application is None:
        profile = _profile_view(client, profile_number)
        _assert_profile_owner(
            profile, manifest.authority.authority_id, _ledger_identity(ledger.path)
        )
        if (
            profile.get("id") != profile_id
            or profile.get("profile_digest") != profile_digest
            or not _profile_assignments_equal(
                profile,
                [_profile_assignment(row, node_ids, alias)],
            )
        ):
            raise QualificationError(
                "dedicated profile changed before the durable load could be resumed"
            )

    detail, _definition = _validate_current_recipe(client, row)
    _run_id, _final, receipt = _load_and_smoke(
        client=client,
        row=row,
        fixtures=fixtures,
        node_ids=node_ids,
        alias=alias,
        kind=kind,
        detail=detail,
        smoke_preview=smoke_preview,
        preview=preview,
        profile_number=profile_number,
        campaign_id=campaign_id,
        ledger=ledger,
        options=manifest,
        failure_node_id=(failure_node_id if isinstance(failure_node_id, str) else None),
        operator_gate_accepted=row.operator_acceptance_required,
        capacity_review_accepted=capacity_review_required,
        clock=clock,
        sleeper=sleeper,
    )
    if (
        row.node_count == 2
        and _latest_payload(ledger, campaign_id, row.key, "rank-loss.pending") is None
    ):
        ranks = receipt.get("node_to_rank")
        if not isinstance(ranks, Mapping):
            raise QualificationError(
                "resumed canary lacks exact rank-to-Spark bindings"
            )
        ledger.append(
            "rank-loss.pending",
            plan_digest=campaign_id,
            recipe=row.key,
            payload={
                "failure_spark": failure_node_id,
                "run_id": receipt.get("run_id"),
                "revision_id": receipt.get("recipe_revision_id"),
                "alias": alias,
                "node_ids": node_ids,
                "node_to_rank": dict(ranks),
            },
        )
    return receipt


def _rank_lost(
    client: Any,
    fleet: Mapping[str, object],
    *,
    run_id: str,
    revision_id: str,
    alias: str,
    node_ids: Sequence[str],
    node_to_rank: Mapping[str, int],
    failure_node_id: str,
) -> tuple[bool, Mapping[str, object]]:
    presences = _run_presences(fleet, run_id)
    if not presences:
        return False, {}
    if any(node_id not in node_ids for node_id, _presence in presences):
        return False, {}
    if len({node_id for node_id, _presence in presences}) != len(presences):
        return False, {}
    # A physical rank loss is not inferred from a partial profile application:
    # Fleet must show exactly one unavailable/lost rank, a degraded group, and
    # withdrawn route evidence on every surviving rank.
    missing = set(node_ids) - {node_id for node_id, _ in presences}
    failed_state = {
        node_id
        for node_id, presence in presences
        if presence.get("rank_state") in {"lost", "failed", "stopped"}
    }
    failed_nodes = missing | failed_state
    if failed_nodes != {failure_node_id}:
        return False, {}
    if set(node_to_rank) != set(node_ids) or failure_node_id not in node_to_rank:
        raise QualificationError(
            "rank-loss evidence lacks exact rank-to-Spark bindings"
        )
    failure_rank = node_to_rank[failure_node_id]
    expected_present_ranks = set(range(len(node_ids))) - {failure_rank}
    failure_node = _nodes(fleet).get(failure_node_id)
    if failure_node is None:
        return False, {}
    failure_node_offline = _offline(failure_node)
    survivors = [(node, item) for node, item in presences if node != failure_node_id]
    if len(survivors) != len(node_ids) - 1:
        return False, {}
    full_rank_set = set(range(len(node_ids)))
    for node_id, presence in survivors:
        present_ranks = presence.get("present_ranks")
        if (
            presence.get("recipe_revision_id") != revision_id
            or presence.get("rank") != node_to_rank[node_id]
            or presence.get("expected_rank_count") != len(node_ids)
            or presence.get("group_state") != "degraded"
            or presence.get("healthy") is not False
            or presence.get("route_state") != "withdrawn"
            or not isinstance(present_ranks, list)
        ):
            return False, {}
        present_rank_set = set(present_ranks)
        if present_rank_set != expected_present_ranks and not (
            present_rank_set == full_rank_set
            and (failure_node_id in failed_state or failure_node_offline)
        ):
            return False, {}
    failed_presence = [
        item for node_id, item in presences if node_id == failure_node_id
    ]
    if failed_presence and any(
        item.get("rank") != failure_rank
        or item.get("recipe_revision_id") != revision_id
        or item.get("expected_rank_count") != len(node_ids)
        or item.get("group_state") != "degraded"
        or item.get("route_state") != "withdrawn"
        or item.get("healthy") is not False
        or item.get("rank_state") not in {"lost", "failed", "stopped"}
        for item in failed_presence
    ):
        return False, {}
    if not failed_presence and not failure_node_offline:
        return False, {}
    node = failure_node
    if not failure_node_offline and failure_node_id not in failed_state:
        return False, {}
    connection = node.get("connection")
    try:
        client.request("GET", f"/api/endpoints/{urllib.parse.quote(alias, safe='')}")
    except ControlNotFound:
        return True, {
            "failure_node_id": failure_node_id,
            "failure_rank": failure_rank,
            "expected_present_ranks": sorted(expected_present_ranks),
            "survivors": [dict(item) for _, item in survivors],
            "failed_rank_presence": [dict(item) for item in failed_presence],
            "failed_node_online_state": (
                connection.get("online_state")
                if isinstance(connection, Mapping)
                else None
            ),
            "endpoint_not_found": True,
        }
    return False, {}


def _rank_recovered(
    client: Any,
    fleet: Mapping[str, object],
    *,
    run_id: str,
    revision_id: str,
    alias: str,
    node_ids: Sequence[str],
) -> list[dict[str, object]] | None:
    nodes = _nodes(fleet)
    if any(node_id not in nodes or not _online(nodes[node_id]) for node_id in node_ids):
        return None
    try:
        presences = _check_serving_fleet(
            fleet,
            run_id=run_id,
            revision_id=revision_id,
            alias=alias,
            node_ids=node_ids,
            expected_run_state="running",
            expected_route_state="published",
            expected_health=True,
        )
    except QualificationError:
        return None
    try:
        endpoint = client.request(
            "GET", f"/api/endpoints/{urllib.parse.quote(alias, safe='')}"
        )
    except ControlNotFound:
        return None
    if not isinstance(endpoint.get("api_base"), str):
        return None
    return presences


def _restart_observation(
    *,
    client: Any,
    fleet: Mapping[str, object],
    row: RecipeAuthorityRow,
    campaign_id: str,
    run_id: str,
    alias: str,
    node_ids: Sequence[str],
    ledger: EvidenceLedger,
) -> dict[str, object]:
    baseline_record = next(
        (
            record
            for record in reversed(ledger.recipe_records(campaign_id, row.key))
            if record.get("event") == "host-restart.baseline"
        ),
        None,
    )
    if baseline_record is None:
        raise QualificationError(
            "physical restart baseline is absent from durable evidence"
        )
    baseline = _object(baseline_record.get("payload"), "restart baseline")
    raw_baselines = _object(baseline.get("nodes"), "restart boot IDs")
    if set(raw_baselines) != set(node_ids) or any(
        not isinstance(value, str) or not value for value in raw_baselines.values()
    ):
        raise QualificationError(
            "physical restart baseline does not match the exact Spark group"
        )
    if _run_presences(fleet, run_id) or _endpoint_exists(client, alias):
        raise QualificationError(
            "offline restart checkpoint requires cleanup and route withdrawal"
        )
    if _all_loaded_runs(fleet):
        raise QualificationError(
            "offline restart checkpoint requires every whole-Fleet workload to be stopped"
        )
    observed = [
        record
        for record in ledger.recipe_records(campaign_id, row.key)
        if record.get("event") in {"host-restart.offline", "host-restart.recovered"}
    ]
    recovered = {
        str(_object(record.get("payload"), "restart evidence").get("node_id"))
        for record in observed
        if record.get("event") == "host-restart.recovered"
    }
    current_node = next((node for node in node_ids if node not in recovered), None)
    if current_node is None:
        nodes = _nodes(fleet)
        recovery_records = {
            str(
                _object(record.get("payload"), "restart evidence").get("node_id")
            ): _object(record.get("payload"), "restart evidence")
            for record in observed
            if record.get("event") == "host-restart.recovered"
        }
        for node_id in node_ids:
            node = nodes.get(node_id)
            recovery = recovery_records.get(node_id)
            boot_id = _boot_id(node) if node is not None else None
            if (
                node is None
                or not _online(node)
                or boot_id is None
                or recovery is None
                or boot_id == raw_baselines[node_id]
            ):
                return {
                    "complete": False,
                    "checkpoint": "host-online",
                    "node_id": node_id,
                    "reason": "all selected Sparks must remain online with fresh changed boot IDs",
                }
            if recovery.get("observed_boot_id") != boot_id:
                ledger.append(
                    "host-restart.recovered",
                    plan_digest=campaign_id,
                    recipe=row.key,
                    payload={
                        "node_id": node_id,
                        "baseline_boot_id": raw_baselines[node_id],
                        "observed_boot_id": boot_id,
                        "online_state": "online",
                        "telemetry_freshness": "live",
                    },
                )
        return {"complete": True, "recovered_nodes": sorted(recovered)}
    offline_event = next(
        (
            record
            for record in reversed(observed)
            if record.get("event") == "host-restart.offline"
            and _object(record.get("payload"), "restart evidence").get("node_id")
            == current_node
        ),
        None,
    )
    nodes = _nodes(fleet)
    node = nodes.get(current_node)
    if node is None:
        return {
            "complete": False,
            "checkpoint": "host-offline",
            "node_id": current_node,
            "reason": "Spark is absent from current Fleet authority",
        }
    if offline_event is None:
        other_selected = set(node_ids) - {current_node}
        if not all(
            other in nodes and _online(nodes[other]) for other in other_selected
        ):
            return {
                "complete": False,
                "checkpoint": "host-offline",
                "node_id": current_node,
                "reason": "restart checkpoints must be sequential; another selected Spark is offline",
            }
        if not _offline(node):
            if not _online(node):
                return {
                    "complete": False,
                    "checkpoint": "host-offline",
                    "node_id": current_node,
                    "reason": "Fleet has not confirmed this Spark is offline",
                }
            current_boot = _boot_id(node, require_live=False)
            if current_boot != raw_baselines[current_node]:
                return {
                    "complete": False,
                    "checkpoint": "host-offline",
                    "node_id": current_node,
                    "reason": "boot ID changed without an observed offline state; repeat while the runner can observe downtime",
                }
            return {
                "complete": False,
                "checkpoint": "host-offline",
                "node_id": current_node,
                "reason": "take this Spark offline, then rerun with --observe",
            }
        ledger.append(
            "host-restart.offline",
            plan_digest=campaign_id,
            recipe=row.key,
            payload={
                "node_id": current_node,
                "baseline_boot_id": raw_baselines[current_node],
                "online_state": "offline",
                "fleet_authority_revision": fleet.get("authority_revision"),
                "fleet_event_cursor": fleet.get("event_cursor"),
            },
        )
        return {
            "complete": False,
            "checkpoint": "host-online",
            "node_id": current_node,
            "reason": "offline state observed; restart or power on this Spark, then rerun with --observe",
        }
    if not _online(node):
        return {
            "complete": False,
            "checkpoint": "host-online",
            "node_id": current_node,
            "reason": "Spark remains offline",
        }
    boot_id = _boot_id(node)
    if boot_id is None:
        return {
            "complete": False,
            "checkpoint": "host-online",
            "node_id": current_node,
            "reason": "waiting for live serialized Fleet telemetry with a boot ID",
        }
    if boot_id == raw_baselines[current_node]:
        return {
            "complete": False,
            "checkpoint": "host-online",
            "node_id": current_node,
            "reason": "host returned with unchanged boot ID; restart is not proven",
        }
    if any(not _online(nodes[item]) for item in set(node_ids) - {current_node}):
        return {
            "complete": False,
            "checkpoint": "host-online",
            "node_id": current_node,
            "reason": "another selected Spark is offline; sequential restart gate failed",
        }
    telemetry = node.get("telemetry")
    ledger.append(
        "host-restart.recovered",
        plan_digest=campaign_id,
        recipe=row.key,
        payload={
            "node_id": current_node,
            "baseline_boot_id": raw_baselines[current_node],
            "observed_boot_id": boot_id,
            "online_state": "online",
            "telemetry_freshness": (
                telemetry.get("freshness") if isinstance(telemetry, Mapping) else None
            ),
        },
    )
    return {
        "complete": len(recovered | {current_node}) == len(node_ids),
        "checkpoint": None
        if len(recovered | {current_node}) == len(node_ids)
        else "host-offline",
        "next_node_id": next(
            (item for item in node_ids if item not in recovered | {current_node}), None
        ),
        "recovered_nodes": sorted(recovered | {current_node}),
    }


def _accept_if_complete(
    *,
    row: RecipeAuthorityRow,
    campaign_id: str,
    ledger: EvidenceLedger,
    node_ids: Sequence[str],
) -> dict[str, object]:
    records = ledger.recipe_records(campaign_id, row.key)
    canary = _latest_payload(ledger, campaign_id, row.key, "canary.completed")
    cleanup = _latest_payload(ledger, campaign_id, row.key, "profile.cleanup.completed")
    baseline = _latest_payload(ledger, campaign_id, row.key, "host-restart.baseline")
    if canary is None or cleanup is None or baseline is None:
        raise QualificationError(
            "spark-accepted gate is missing required physical evidence"
        )
    ranks = canary.get("node_to_rank")
    if (
        canary.get("recipe_content_sha256") != row.content_sha256
        or canary.get("alias") is None
        or not isinstance(canary.get("run_id"), str)
        or not isinstance(ranks, Mapping)
        or set(ranks) != set(node_ids)
        or any(type(value) is not int for value in ranks.values())
        or {int(value) for value in ranks.values()} != set(range(row.node_count))
    ):
        raise QualificationError(
            "canary evidence does not bind the exact recipe and Spark ranks"
        )
    plan = _latest_payload(ledger, campaign_id, row.key, "plan.generated")
    if plan is None:
        raise QualificationError("canary evidence has no durable reviewed plan")
    authority_row = _object(plan.get("authority_row"), "reviewed authority row")
    controller_identity = _object(
        plan.get("controller_recipe_identity"), "Controller recipe identity"
    )
    application = _object(canary.get("application"), "profile application receipt")
    smoke = _object(canary.get("smoke"), "fixture smoke receipt")
    smoke_preview = _object(plan.get("smoke_preview"), "reviewed smoke preview")
    load_request = _latest_payload(
        ledger, campaign_id, row.key, "profile.load.requested"
    )
    request_key = load_request.get("request_key") if load_request is not None else None
    reconciliation_digest = _object(
        plan.get("preview"), "reviewed profile preview"
    ).get("plan_digest")
    expected_alias = (
        smoke_preview.get("endpoint_alias")
        if smoke_preview.get("kind") == "openai-service"
        else f"q{row.sequence}"
    )
    if (
        dict(authority_row) != dict(row.raw)
        or canary.get("alias") != expected_alias
        or controller_identity.get("content_sha256") != row.content_sha256
        or controller_identity.get("recipe_revision_id")
        != canary.get("recipe_revision_id")
        or application.get("state") != "succeeded"
        or application.get("profile_digest") != plan.get("profile_digest")
        or not isinstance(request_key, str)
        or not isinstance(reconciliation_digest, str)
        or application.get("plan_digest")
        != _application_plan_digest(reconciliation_digest, request_key)
        or smoke.get("fixture_manifest_sha256")
        != smoke_preview.get("fixture_manifest_sha256")
        or canary.get("exact_preparations")
        != _object(plan.get("preview"), "reviewed profile preview").get(
            "exact_preparations"
        )
    ):
        raise QualificationError(
            "canary execution differs from its exact reviewed plan or fixtures"
        )
    smoke_cases = smoke.get("cases")
    if isinstance(smoke_cases, list):
        observed_case_ids = [
            _object(item, "smoke case receipt").get("case_id") for item in smoke_cases
        ]
    else:
        observed_case_ids = [smoke.get("case_id")]
    if observed_case_ids != list(row.smoke_cases):
        raise QualificationError(
            "smoke receipt does not cover the exact ordered reviewed cases"
        )
    acknowledgements = _object(
        canary.get("review_acknowledgements"), "canary review acknowledgements"
    )
    capacity_review_required = any(
        gate.get("kind") == "capacity-review" for gate in row.review_gates
    )
    if (
        acknowledgements.get("operator_acceptance")
        is not row.operator_acceptance_required
        or acknowledgements.get("capacity_review") is not capacity_review_required
    ):
        raise QualificationError(
            "canary lacks the exact recipe review acknowledgements"
        )
    if (
        cleanup.get("application_state") != "succeeded"
        or not isinstance(cleanup.get("application_id"), str)
        or not cleanup.get("application_id")
        or cleanup.get("cleanup_policy") != "stop"
        or cleanup.get("uninstalls") != 0
        or cleanup.get("route_withdrawn") is not True
        or cleanup.get("run_absent_from_fleet") is not True
    ):
        raise QualificationError("profile cleanup evidence is incomplete or unsafe")
    baseline_nodes = _object(baseline.get("nodes"), "restart baseline nodes")
    if (
        set(baseline_nodes) != set(node_ids)
        or baseline.get("run_id") != canary.get("run_id")
        or baseline.get("route_alias") != canary.get("alias")
        or any(
            not isinstance(value, str) or not value for value in baseline_nodes.values()
        )
    ):
        raise QualificationError(
            "restart baseline does not bind the exact canary run and Sparks"
        )
    recovered_by_node: dict[str, tuple[int, Mapping[str, object]]] = {}
    offline_by_node: dict[str, tuple[int, Mapping[str, object]]] = {}
    event_positions: dict[str, int] = {}
    for index, record in enumerate(records):
        event = record.get("event")
        payload = _object(record.get("payload"), "qualification event")
        if event in {
            "canary.completed",
            "rank-loss.observed",
            "rank-recovery.smoke-completed",
            "profile.cleanup.completed",
            "host-restart.baseline",
        }:
            event_positions[str(event)] = index
        if event == "host-restart.offline":
            offline_by_node[str(payload.get("node_id"))] = (index, payload)
        elif event == "host-restart.recovered":
            recovered_by_node[str(payload.get("node_id"))] = (index, payload)
    if not (
        event_positions.get("canary.completed", -1)
        < event_positions.get("profile.cleanup.completed", -1)
        < event_positions.get("host-restart.baseline", -1)
    ):
        raise QualificationError("cleanup and restart evidence are out of sequence")
    if set(recovered_by_node) != set(node_ids):
        raise QualificationError(
            "offline host restart evidence does not cover every selected Spark"
        )
    if set(offline_by_node) != set(node_ids):
        raise QualificationError(
            "offline observations do not match the exact selected Spark group"
        )
    baseline_position = event_positions["host-restart.baseline"]
    restart_events = sorted(
        [
            (index, "offline", node_id)
            for node_id, (index, _payload) in offline_by_node.items()
        ]
        + [
            (index, "recovered", node_id)
            for node_id, (index, _payload) in recovered_by_node.items()
        ]
    )
    offline_node: str | None = None
    for index, event, node_id in restart_events:
        if index <= baseline_position:
            raise QualificationError(
                "physical restart checkpoint preceded its clean baseline"
            )
        if event == "offline":
            if offline_node is not None:
                raise QualificationError("selected Spark restarts were not sequential")
            offline_node = node_id
        elif offline_node != node_id:
            raise QualificationError(
                "restart recovery does not follow that Spark's offline event"
            )
        else:
            offline_node = None
    if offline_node is not None:
        raise QualificationError("last physical restart has no observed recovery")
    for node_id in node_ids:
        offline_entry = offline_by_node.get(node_id)
        recovered_index, recovered = recovered_by_node[node_id]
        if offline_entry is None:
            raise QualificationError(f"{node_id} lacks a recorded offline observation")
        offline_index, offline = offline_entry
        if (
            offline_index >= recovered_index
            or offline.get("online_state") != "offline"
            or offline.get("baseline_boot_id") != baseline_nodes[node_id]
            or recovered.get("online_state") != "online"
            or recovered.get("baseline_boot_id") != baseline_nodes[node_id]
            or not isinstance(recovered.get("observed_boot_id"), str)
            or not recovered.get("observed_boot_id")
            or recovered.get("observed_boot_id") == baseline_nodes[node_id]
            or recovered.get("telemetry_freshness") != "live"
        ):
            raise QualificationError(
                f"{node_id} restart evidence lacks an offline/changed live boot ID"
            )
    rank_loss = _latest_payload(ledger, campaign_id, row.key, "rank-loss.observed")
    recovery = _latest_payload(
        ledger, campaign_id, row.key, "rank-recovery.smoke-completed"
    )
    if row.node_count == 2:
        if rank_loss is None or recovery is None:
            raise QualificationError(
                "distributed spark-accepted gate lacks rank-loss recovery evidence"
            )
        failure_node = rank_loss.get("failure_node_id")
        failure_rank = (
            ranks.get(failure_node) if isinstance(failure_node, str) else None
        )
        expected_survivor_ranks = set(range(row.node_count)) - {failure_rank}
        survivors = rank_loss.get("survivors")
        failed_presence = rank_loss.get("failed_rank_presence")
        recovered_presences = recovery.get("fleet_rank_presence")
        if not isinstance(survivors, list) or not isinstance(failed_presence, list):
            raise QualificationError(
                "rank-loss evidence lacks per-rank route-withdrawal receipts"
            )
        survivor_ranks: set[int] = set()
        survivor_evidence_valid = True
        for raw_presence in survivors:
            presence = _object(raw_presence, "rank-loss survivor")
            rank = presence.get("rank")
            if type(rank) is not int:
                survivor_evidence_valid = False
                break
            survivor_ranks.add(rank)
            observed_present = presence.get("present_ranks")
            if (
                presence.get("group_state") != "degraded"
                or presence.get("route_state") != "withdrawn"
                or presence.get("healthy") is not False
                or presence.get("expected_rank_count") != row.node_count
                or not isinstance(observed_present, list)
                or set(observed_present)
                not in (
                    expected_survivor_ranks,
                    set(range(row.node_count)),
                )
            ):
                survivor_evidence_valid = False
                break
        failed_state_proven = bool(failed_presence) and all(
            _object(item, "failed rank presence").get("rank") == failure_rank
            and _object(item, "failed rank presence").get("rank_state")
            in {"lost", "failed", "stopped"}
            and _object(item, "failed rank presence").get("recipe_revision_id")
            == canary.get("recipe_revision_id")
            and _object(item, "failed rank presence").get("expected_rank_count")
            == row.node_count
            and _object(item, "failed rank presence").get("group_state") == "degraded"
            and _object(item, "failed rank presence").get("route_state") == "withdrawn"
            and _object(item, "failed rank presence").get("healthy") is False
            for item in failed_presence
        )
        recovery_smoke = recovery.get("smoke")
        if (
            not isinstance(failure_node, str)
            or failure_node not in ranks
            or type(failure_rank) is not int
            or rank_loss.get("failure_rank") != failure_rank
            or rank_loss.get("expected_present_ranks")
            != sorted(expected_survivor_ranks)
            or survivor_ranks != expected_survivor_ranks
            or not survivor_evidence_valid
            or not (
                rank_loss.get("failed_node_online_state") == "offline"
                or failed_state_proven
            )
            or rank_loss.get("endpoint_not_found") is not True
            or recovery.get("run_id") != canary.get("run_id")
            or recovery.get("recipe_revision_id") != canary.get("recipe_revision_id")
            or not isinstance(recovered_presences, list)
            or len(recovered_presences) != row.node_count
            or {
                _object(item, "recovered rank presence").get("rank")
                for item in recovered_presences
            }
            != set(range(row.node_count))
            or any(
                _object(item, "recovered rank presence").get("route_state")
                != "published"
                or _object(item, "recovered rank presence").get("healthy") is not True
                or _object(item, "recovered rank presence").get("group_state")
                != "healthy"
                or _object(item, "recovered rank presence").get("recipe_revision_id")
                != canary.get("recipe_revision_id")
                or _object(item, "recovered rank presence").get("member_node_ids")
                != sorted(node_ids)
                for item in recovered_presences
            )
            or not isinstance(recovery_smoke, Mapping)
            or recovery_smoke.get("endpoint_alias") != canary.get("alias")
            or recovery_smoke.get("recipe_content_sha256") != row.content_sha256
            or not isinstance(recovery_smoke.get("cases"), list)
            or not recovery_smoke.get("cases")
            or [
                _object(item, "recovered smoke case").get("case_id")
                for item in recovery_smoke["cases"]
            ]
            != list(row.smoke_cases)
        ):
            raise QualificationError(
                "distributed failure/recovery evidence changed exact rank identity"
            )
        if not (
            event_positions.get("canary.completed", -1)
            < event_positions.get("rank-loss.observed", -1)
            < event_positions.get("rank-recovery.smoke-completed", -1)
            < event_positions.get("profile.cleanup.completed", -1)
        ):
            raise QualificationError(
                "distributed fault and cleanup evidence are out of sequence"
            )
    payload = {
        "sequence": row.sequence,
        "recipe_content_sha256": row.content_sha256,
        "node_count": row.node_count,
        "node_ids": list(node_ids),
        "canary": dict(canary),
        "rank_loss": dict(rank_loss) if rank_loss is not None else None,
        "rank_recovery": dict(recovery) if recovery is not None else None,
        "cleanup": dict(cleanup),
        "offline_restarts": [
            dict(_object(record.get("payload"), "restart evidence"))
            for record in ledger.recipe_records(campaign_id, row.key)
            if record.get("event") == "host-restart.recovered"
        ],
        "acceptance_gate": (
            "distributed-rank-loss-recovery-and-final-offline-restart"
            if row.node_count == 2
            else "canary-and-offline-restart"
        ),
    }
    ledger.append(
        "recipe.spark-accepted",
        plan_digest=campaign_id,
        recipe=row.key,
        payload=payload,
    )
    return {"status": "spark-accepted", **payload}


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Qualify one exact recipe at a time through a dedicated whole-fleet "
            "profile; physical rank-loss and restart checkpoints are operator-run."
        )
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--library-root", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--profile-number", type=int, required=True)
    parser.add_argument("--recipe")
    parser.add_argument("--spark", action="append", default=[])
    parser.add_argument("--failure-spark")
    parser.add_argument("--accept-operator-gate", action="append", default=[])
    parser.add_argument("--accept-capacity-review", action="append", default=[])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--observe", action="store_true")
    parser.add_argument(
        "--campaign-digest",
        help="Required with --apply; copy from the fresh preview for this exact recipe and Spark group",
    )
    parser.add_argument(
        "--replace-run-id",
        help=(
            "Explicitly acknowledge replacement of this exact currently loaded "
            "run when the whole-Fleet profile preview includes its stop"
        ),
    )
    args = parser.parse_args(argv)
    if type(args.profile_number) is not int or args.profile_number < 1:
        parser.error("--profile-number must be a positive existing profile number")
    if args.campaign_digest and not args.apply:
        parser.error("--campaign-digest is only valid with --apply")
    if args.apply and not args.campaign_digest:
        parser.error("--apply requires --campaign-digest from a fresh preview")
    if not args.apply and (args.accept_operator_gate or args.accept_capacity_review):
        parser.error("review acknowledgements are only accepted with --apply")
    if args.observe and (
        args.spark
        or args.failure_spark
        or args.accept_operator_gate
        or args.accept_capacity_review
        or args.replace_run_id
    ):
        parser.error(
            "--observe resumes durable evidence; it takes no Spark, replacement, "
            "or review-gate flags"
        )
    if not args.observe and not args.spark:
        parser.error("preview and apply require exact --spark IDs")
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
    row = _current_row(manifest, ledger, campaign_id, args.recipe)
    if row is None:
        return {
            "schema_version": 1,
            "status": "complete",
            "accepted_recipe_count": len(_completed_keys(ledger, campaign_id)),
            "scope_recipe_count": len(manifest.authority.rows),
        }
    kind, smoke_definition = _fixture_bindings(row, fixtures)
    del smoke_definition
    if row.node_count == 2 and kind != "openai-service":
        raise QualificationError(
            f"{row.key} distributed fault acceptance requires a serving-route smoke fixture"
        )
    evidence_nodes = (
        list(args.spark)
        if not args.observe
        else _evidence_lock_nodes(ledger, campaign_id, row)
    )
    if args.observe and not evidence_nodes:
        raise QualificationError(
            f"{row.key} has no durable physical/load evidence to observe"
        )
    if any(_NODE_ID.fullmatch(node_id) is None for node_id in evidence_nodes):
        raise QualificationError("qualification requires exact Controller Spark IDs")
    initial_fleet = _typed_fleet(client)
    initial_roster = sorted(_nodes(initial_fleet))
    lock_nodes = _qualification_lock_nodes(initial_fleet, evidence_nodes)
    with ledger_lock(ledger_path), node_locks(lock_nodes):
        locked_fleet = _typed_fleet(client)
        _require_locked_fleet_roster(locked_fleet, initial_roster)
        # The lock may be acquired after another invocation completed. Reload
        # under lock before deriving sequence or appending, so stale in-memory
        # sequence numbers cannot corrupt the hash-chained ledger.
        ledger = EvidenceLedger._under_ledger_lock(ledger_path)
        locked_row = _current_row(manifest, ledger, campaign_id, args.recipe)
        if locked_row is None:
            return {
                "schema_version": 1,
                "status": "complete",
                "accepted_recipe_count": len(_completed_keys(ledger, campaign_id)),
                "scope_recipe_count": len(manifest.authority.rows),
            }
        if locked_row.key != row.key:
            raise QualificationError(
                "campaign advanced while acquiring locks; rerun for the next recipe"
            )
        row = locked_row
        locked_nodes = (
            list(args.spark)
            if not args.observe
            else _evidence_lock_nodes(ledger, campaign_id, row)
        )
        if sorted(locked_nodes) != sorted(evidence_nodes):
            raise QualificationError(
                "campaign evidence changed while acquiring locks; rerun"
            )
        if _qualification_lock_nodes(locked_fleet, locked_nodes) != lock_nodes:
            raise QualificationError(
                "campaign Spark identities changed while acquiring locks; rerun"
            )
        if args.observe:
            return _observe(
                client=client,
                row=row,
                manifest=manifest,
                campaign_id=campaign_id,
                ledger=ledger,
                fixtures=fixtures,
                profile_number=args.profile_number,
                library_root=library_root,
                clock=clock,
                sleeper=sleeper,
            )
        _operator_gate(args, row)
        fleet = _typed_fleet(client)
        _require_locked_fleet_roster(fleet, initial_roster)
        node_ids = _exact_nodes(client, fleet, row, args.spark)
        if row.node_count == 2:
            if args.failure_spark not in node_ids or args.failure_spark is None:
                raise QualificationError(
                    "dual-Spark qualification requires --failure-spark to select the exact rank-loss target"
                )
        elif args.failure_spark is not None:
            raise QualificationError(
                "--failure-spark is only valid for dual-Spark recipes"
            )
        alias = (
            str(fixtures.service_recipes[row.key].alias)
            if kind == "openai-service"
            else f"q{row.sequence}"
        )
        if _ALIAS.fullmatch(alias) is None:
            raise QualificationError(
                f"{row.key} smoke alias is invalid for a profile assignment"
            )
        if args.apply:
            canary = _latest_payload(ledger, campaign_id, row.key, "canary.completed")
            if canary is not None:
                canary_nodes = _ordered_rank_nodes(
                    canary.get("node_to_rank"), row.node_count
                )
                pending = _latest_payload(
                    ledger, campaign_id, row.key, "rank-loss.pending"
                )
                if set(canary_nodes) != set(node_ids) or (
                    row.node_count == 2
                    and (
                        pending is None
                        or pending.get("failure_spark") != args.failure_spark
                    )
                ):
                    raise QualificationError(
                        "physical checkpoints are bound to another Spark group/rank; resume with --observe"
                    )
                return {
                    "schema_version": 1,
                    "mode": "apply",
                    "status": "checkpoint-required",
                    "recipe": row.key,
                    "next": _next_checkpoint(ledger, campaign_id, row),
                    "spark_accepted": False,
                }
            intent = _latest_payload(
                ledger, campaign_id, row.key, "profile.load.requested"
            )
            if intent is not None:
                if (
                    intent.get("node_ids") != node_ids
                    or intent.get("failure_node_id") != args.failure_spark
                ):
                    raise QualificationError(
                        "a different durable profile load intent exists; resume it with --observe"
                    )
                return {
                    "schema_version": 1,
                    "mode": "apply",
                    "status": "checkpoint-required",
                    "recipe": row.key,
                    "next": {
                        "checkpoint": "resume-profile-load",
                        "instruction": "Run with --observe to replay and reconcile the same durable load request.",
                    },
                    "spark_accepted": False,
                }
        if not args.apply:
            preview, smoke_preview, metadata = _fresh_profile_preview(
                client=client,
                manifest=manifest,
                fixtures=fixtures,
                row=row,
                library_root=library_root,
                profile_number=args.profile_number,
                authority_id=manifest.authority.authority_id,
                ledger_id=ledger_id,
                node_ids=node_ids,
                failure_node_id=args.failure_spark,
                alias=alias,
                kind=kind,
                campaign_id=campaign_id,
                ledger=ledger,
                expected_fleet_node_ids=initial_roster,
                replace_run_id=args.replace_run_id,
            )
            return {
                "schema_version": 1,
                "mode": "preview",
                "status": "not-accepted",
                "sequence": row.sequence,
                "recipe": row.key,
                "recipe_content_sha256": row.content_sha256,
                "node_ids": node_ids,
                "failure_spark": args.failure_spark,
                "profile_number": args.profile_number,
                "campaign_digest": metadata["campaign_digest"],
                "profile_preview": preview,
                "smoke_preview": smoke_preview,
                "acceptance_checkpoints": _checkpoint_names(row),
                "review_gates": [dict(gate) for gate in row.review_gates],
                "spark_accepted": False,
            }
        profile = _profile_view(client, args.profile_number)
        _assert_profile_owner(profile, manifest.authority.authority_id, ledger_id)
        preview, smoke_preview, metadata = _fresh_profile_preview(
            client=client,
            manifest=manifest,
            fixtures=fixtures,
            row=row,
            library_root=library_root,
            profile_number=args.profile_number,
            authority_id=manifest.authority.authority_id,
            ledger_id=ledger_id,
            node_ids=node_ids,
            failure_node_id=args.failure_spark,
            alias=alias,
            kind=kind,
            campaign_id=campaign_id,
            ledger=ledger,
            expected_fleet_node_ids=initial_roster,
            replace_run_id=args.replace_run_id,
        )
        if metadata["campaign_digest"] != args.campaign_digest:
            raise QualificationError(
                "--campaign-digest no longer matches the live exact profile preview"
            )
        detail, _definition = _validate_current_recipe(client, row)
        profile = metadata["profile"]
        assert isinstance(profile, Mapping)
        run_id, _final, canary = _load_and_smoke(
            client=client,
            row=row,
            node_ids=node_ids,
            fixtures=fixtures,
            alias=alias,
            kind=kind,
            detail=detail,
            smoke_preview=smoke_preview,
            preview=preview,
            profile_number=args.profile_number,
            campaign_id=campaign_id,
            ledger=ledger,
            options=manifest,
            failure_node_id=args.failure_spark,
            operator_gate_accepted=row.operator_acceptance_required,
            capacity_review_accepted=any(
                gate.get("kind") == "capacity-review" for gate in row.review_gates
            ),
            clock=clock,
            sleeper=sleeper,
        )
        if row.node_count == 1:
            _profile_cleanup(
                client=client,
                profile_number=args.profile_number,
                authority_id=manifest.authority.authority_id,
                ledger_id=ledger_id,
                row=row,
                campaign_id=campaign_id,
                run_id=run_id,
                alias=alias,
                node_ids=node_ids,
                ledger=ledger,
                options=manifest,
                clock=clock,
                sleeper=sleeper,
            )
            return {
                "schema_version": 1,
                "mode": "apply",
                "status": "checkpoint-required",
                "recipe": row.key,
                "canary": dict(canary),
                "next": _next_checkpoint(ledger, campaign_id, row),
                "spark_accepted": False,
            }
        pending = {
            "failure_spark": args.failure_spark,
            "run_id": run_id,
            "revision_id": canary.get("recipe_revision_id"),
            "alias": alias,
            "node_ids": node_ids,
            "node_to_rank": canary.get("node_to_rank"),
        }
        ledger.append(
            "rank-loss.pending",
            plan_digest=campaign_id,
            recipe=row.key,
            payload=pending,
        )
        return {
            "schema_version": 1,
            "mode": "apply",
            "status": "checkpoint-required",
            "recipe": row.key,
            "canary": dict(canary),
            "next": {
                "checkpoint": "distributed-rank-loss",
                "failure_spark": args.failure_spark,
                "instruction": "Use the approved operator procedure to take only this rank offline; then rerun this command with --observe.",
            },
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


def _next_checkpoint(
    ledger: EvidenceLedger, campaign_id: str, row: RecipeAuthorityRow
) -> dict[str, object]:
    if (
        _latest_payload(ledger, campaign_id, row.key, "recipe.spark-accepted")
        is not None
    ):
        return {"checkpoint": "complete"}
    if _latest_payload(ledger, campaign_id, row.key, "canary.completed") is None:
        return {"checkpoint": "profile-load-and-fixture-canary"}
    if row.node_count == 2:
        if _latest_payload(ledger, campaign_id, row.key, "rank-loss.observed") is None:
            pending = (
                _latest_payload(ledger, campaign_id, row.key, "rank-loss.pending") or {}
            )
            return {
                "checkpoint": "distributed-rank-loss",
                "failure_spark": pending.get("failure_spark"),
                "instruction": "Take only the selected rank offline through the approved operator procedure, then run --observe.",
            }
        if (
            _latest_payload(
                ledger, campaign_id, row.key, "rank-recovery.smoke-completed"
            )
            is None
        ):
            return {
                "checkpoint": "distributed-rank-recovery",
                "instruction": "Restore the failed rank through the approved operator procedure, then run --observe.",
            }
    if (
        _latest_payload(ledger, campaign_id, row.key, "profile.cleanup.completed")
        is None
    ):
        return {
            "checkpoint": "safe-cleanup",
            "instruction": "Run --observe to finish profile cleanup.",
        }
    next_node = _next_restart_node(ledger, campaign_id, row)
    if next_node is not None:
        return {
            "checkpoint": "offline-host-restart",
            "node_id": next_node,
            "instruction": "Take only this Spark offline, run --observe while it is offline, then restart it and run --observe again.",
        }
    return {"checkpoint": "spark-accepted"}


def _next_restart_node(
    ledger: EvidenceLedger, campaign_id: str, row: RecipeAuthorityRow
) -> str | None:
    canary = _latest_payload(ledger, campaign_id, row.key, "canary.completed")
    if canary is None:
        return None
    ranks = canary.get("node_to_rank")
    if not isinstance(ranks, Mapping):
        return None
    nodes = [
        node_id
        for node_id, _rank in sorted(
            ((str(node), int(rank)) for node, rank in ranks.items()),
            key=lambda item: item[1],
        )
    ]
    recovered = {
        str(_object(record.get("payload"), "restart event").get("node_id"))
        for record in ledger.recipe_records(campaign_id, row.key)
        if record.get("event") == "host-restart.recovered"
    }
    return next((node_id for node_id in nodes if node_id not in recovered), None)


def _observe(
    *,
    client: Any,
    row: RecipeAuthorityRow,
    manifest: CampaignManifest,
    campaign_id: str,
    ledger: EvidenceLedger,
    fixtures: FixtureRegistry,
    profile_number: int,
    library_root: Path,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    del library_root
    canary = _latest_payload(ledger, campaign_id, row.key, "canary.completed")
    if canary is None:
        canary = _resume_load_and_smoke(
            client=client,
            manifest=manifest,
            fixtures=fixtures,
            row=row,
            campaign_id=campaign_id,
            ledger=ledger,
            profile_number=profile_number,
            clock=clock,
            sleeper=sleeper,
        )
    node_to_rank = canary.get("node_to_rank")
    node_ids = _ordered_rank_nodes(node_to_rank, row.node_count)
    run_id = _string(canary.get("run_id"), "canary run ID")
    alias = _string(canary.get("alias"), "canary route alias")
    if (
        _latest_payload(ledger, campaign_id, row.key, "recipe.spark-accepted")
        is not None
    ):
        return {"schema_version": 1, "status": "spark-accepted", "recipe": row.key}

    if row.node_count == 2:
        pending = _latest_payload(ledger, campaign_id, row.key, "rank-loss.pending")
        if pending is None:
            request = _latest_payload(
                ledger, campaign_id, row.key, "profile.load.requested"
            )
            failure_node = (
                request.get("failure_node_id") if request is not None else None
            )
            if not isinstance(failure_node, str) or failure_node not in node_ids:
                raise QualificationError(
                    "distributed canary lacks its exact rank-loss target"
                )
            ranks = canary.get("node_to_rank")
            if not isinstance(ranks, Mapping) or set(ranks) != set(node_ids):
                raise QualificationError(
                    "distributed canary lacks exact rank-to-Spark bindings"
                )
            pending = {
                "failure_spark": failure_node,
                "run_id": run_id,
                "revision_id": canary.get("recipe_revision_id"),
                "alias": alias,
                "node_ids": node_ids,
                "node_to_rank": dict(ranks),
            }
            ledger.append(
                "rank-loss.pending",
                plan_digest=campaign_id,
                recipe=row.key,
                payload=pending,
            )
        if pending is None:
            raise QualificationError(
                "distributed canary lacks its rank-loss target receipt"
            )
        failure_node = _string(pending.get("failure_spark"), "failure Spark")
        revision_id = _string(pending.get("revision_id"), "recipe revision ID")
        node_to_rank = pending.get("node_to_rank")
        if not isinstance(node_to_rank, Mapping):
            raise QualificationError(
                "rank-loss request lacks exact rank-to-Spark identities"
            )
        if _latest_payload(ledger, campaign_id, row.key, "rank-loss.observed") is None:
            fleet = _typed_fleet(client)
            observed, proof = _rank_lost(
                client,
                fleet,
                run_id=run_id,
                revision_id=revision_id,
                alias=alias,
                node_ids=node_ids,
                node_to_rank=node_to_rank,
                failure_node_id=failure_node,
            )
            if not observed:
                return {
                    "schema_version": 1,
                    "status": "checkpoint-required",
                    "recipe": row.key,
                    "next": _next_checkpoint(ledger, campaign_id, row),
                    "spark_accepted": False,
                }
            ledger.append(
                "rank-loss.observed",
                plan_digest=campaign_id,
                recipe=row.key,
                payload=proof,
            )
            return {
                "schema_version": 1,
                "status": "checkpoint-observed",
                "recipe": row.key,
                "observed": "rank-loss-and-route-withdrawal",
                "next": _next_checkpoint(ledger, campaign_id, row),
                "spark_accepted": False,
            }
        if (
            _latest_payload(
                ledger, campaign_id, row.key, "rank-recovery.smoke-completed"
            )
            is None
        ):
            fleet = _typed_fleet(client)
            presences = _rank_recovered(
                client,
                fleet,
                run_id=run_id,
                revision_id=revision_id,
                alias=alias,
                node_ids=node_ids,
            )
            if presences is None:
                return {
                    "schema_version": 1,
                    "status": "checkpoint-required",
                    "recipe": row.key,
                    "next": _next_checkpoint(ledger, campaign_id, row),
                    "spark_accepted": False,
                }
            smoke_preview = ServiceSmokeAdapter(fixtures).preview(
                {},
                alias,
                recipe_key=row.key,
                recipe_content_sha256=row.content_sha256,
            )
            if smoke_preview.get("available") is not True:
                raise QualificationError(
                    "distributed recovery smoke fixture is unavailable"
                )
            smoke = ServiceSmokeAdapter(fixtures).run(client, alias, smoke_preview)
            payload = {
                "fleet_rank_presence": presences,
                "smoke": dict(smoke),
                "run_id": run_id,
                "recipe_revision_id": revision_id,
            }
            ledger.append(
                "rank-recovery.smoke-completed",
                plan_digest=campaign_id,
                recipe=row.key,
                payload=payload,
            )
            _profile_cleanup(
                client=client,
                profile_number=profile_number,
                authority_id=manifest.authority.authority_id,
                ledger_id=_ledger_identity(ledger.path),
                row=row,
                campaign_id=campaign_id,
                run_id=run_id,
                alias=alias,
                node_ids=node_ids,
                ledger=ledger,
                options=manifest,
                clock=clock,
                sleeper=sleeper,
            )
            return {
                "schema_version": 1,
                "status": "checkpoint-observed",
                "recipe": row.key,
                "observed": "rank-recovery-and-recovered-serving",
                "next": _next_checkpoint(ledger, campaign_id, row),
                "spark_accepted": False,
            }
    if (
        _latest_payload(ledger, campaign_id, row.key, "profile.cleanup.completed")
        is None
    ):
        _profile_cleanup(
            client=client,
            profile_number=profile_number,
            authority_id=manifest.authority.authority_id,
            ledger_id=_ledger_identity(ledger.path),
            row=row,
            campaign_id=campaign_id,
            run_id=run_id,
            alias=alias,
            node_ids=node_ids,
            ledger=ledger,
            options=manifest,
            clock=clock,
            sleeper=sleeper,
        )
    fleet = _typed_fleet(client)
    observation = _restart_observation(
        client=client,
        fleet=fleet,
        row=row,
        campaign_id=campaign_id,
        run_id=run_id,
        alias=alias,
        node_ids=node_ids,
        ledger=ledger,
    )
    if observation.get("complete") is True:
        accepted = _accept_if_complete(
            row=row, campaign_id=campaign_id, ledger=ledger, node_ids=node_ids
        )
        return {"schema_version": 1, **accepted}
    return {
        "schema_version": 1,
        "status": "checkpoint-required",
        "recipe": row.key,
        "next": (
            {
                "checkpoint": observation.get("checkpoint"),
                "node_id": observation.get("node_id"),
                "instruction": observation.get("reason"),
            }
            if observation.get("complete") is not True
            else _next_checkpoint(ledger, campaign_id, row)
        ),
        "observation": observation,
        "spark_accepted": False,
    }


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
