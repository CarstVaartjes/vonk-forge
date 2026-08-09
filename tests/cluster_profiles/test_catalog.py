from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from cluster_profiles import catalog as catalog_module
from cluster_profiles import contracts as contracts_module
from cluster_profiles.catalog import (
    Catalog,
    CatalogError,
    fingerprint,
    validate_evidence_indexes,
)
from cluster_profiles.contracts import ProfileValidationError, load_workload

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def catalog_root(tmp_path: Path) -> Path:
    for relative in (
        "config/workloads",
        "config/cluster-profiles",
        "config/profile-selectors.toml",
        "adapters/deepseek/mia-vllm",
        "adapters/deepseek/ds4",
        "adapters/creative/triposg",
        "adapters/creative/qwen3-vl-8b-single",
        "adapters/creative/nemotron-nano-omni-single",
        "locks/model-definitions.toml",
        "inventory/reports/model-definitions.json",
        "inventory/reports/accepted-cluster-profiles.json",
        "docs/superpowers/specs/2026-08-02-multi-runtime-model-profiles-design.md",
    ):
        source = REPOSITORY_ROOT / relative
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
    maturity_path = tmp_path / "inventory/reports/model-definitions.json"
    maturity = json.loads(maturity_path.read_text(encoding="utf-8"))
    for definition in maturity["definitions"]:
        definition["maturity"] = "planned"
        definition["history"] = [definition["history"][0]]
    maturity_path.write_text(json.dumps(maturity, indent=2) + "\n", encoding="utf-8")
    return tmp_path


def test_default_selector_resolves_to_canonical_home(catalog_root: Path) -> None:
    catalog = Catalog.load(catalog_root)

    assert catalog.resolve_profile("default").id == "agent-full-dual"


def test_catalog_load_checks_each_packaged_schema_once_and_rejects_invalid_workload(
    catalog_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated catalog loads must reuse checked schemas without relaxing payload validation."""
    for module in (catalog_module, contracts_module):
        validator = getattr(module, "_validator", None)
        if validator is not None:
            validator.cache_clear()

    names: dict[int, str] = {}
    catalog_schema = catalog_module._schema
    contract_schema = contracts_module._load_schema

    def load_catalog_schema(name: str) -> dict:
        schema = catalog_schema(name)
        names[id(schema)] = name
        return schema

    def load_contract_schema(name: str) -> dict:
        schema = contract_schema(name)
        names[id(schema)] = name
        return schema

    checked: Counter[str] = Counter()
    check_schema = Draft202012Validator.check_schema

    def count_checked_schema(cls, schema: dict, *args: object, **kwargs: object) -> None:
        checked[names[id(schema)]] += 1
        check_schema(schema, *args, **kwargs)

    monkeypatch.setattr(catalog_module, "_schema", load_catalog_schema)
    monkeypatch.setattr(contracts_module, "_load_schema", load_contract_schema)
    monkeypatch.setattr(
        Draft202012Validator, "check_schema", classmethod(count_checked_schema)
    )

    Catalog.load(REPOSITORY_ROOT)
    Catalog.load(REPOSITORY_ROOT)

    assert checked == Counter(
        {
            "model-definitions.schema.json": 1,
            "accepted-cluster-profiles.schema.json": 1,
            "model-definition-evidence.schema.json": 1,
            "workload.schema.json": 1,
            "cluster-profile.schema.json": 1,
        }
    )

    workload = catalog_root / "config/workloads/deepseek-agent-dual.toml"
    workload.write_text(
        workload.read_text(encoding="utf-8") + "\nunexpected = true\n",
        encoding="utf-8",
    )
    with pytest.raises(ProfileValidationError, match="unexpected"):
        load_workload(workload)


def test_each_model_definition_has_its_own_adapter_command_path() -> None:
    catalog = Catalog.load(REPOSITORY_ROOT)
    prepare_paths = {
        definition.id: definition.commands.prepare[0]
        for definition in catalog.definitions.values()
    }

    assert len(set(prepare_paths.values())) == len(prepare_paths)
    assert all("node-model-adapter" not in path for path in prepare_paths.values())


def test_ds4_single_definition_is_locked_planned_and_node1_only(
    catalog_root: Path,
) -> None:
    catalog = Catalog.load(catalog_root)
    definition = catalog.definitions["deepseek-agent-single"]
    profile = catalog.profiles["agent-single"]

    assert catalog.definition_fingerprints[definition.id]
    assert catalog.maturity[definition.id] == "planned"
    assert definition.topology == "single"
    assert definition.placement_class == "single-exclusive"
    assert definition.co_location == "exclusive"
    assert definition.nodes == ("node1",)
    assert definition.start_order == ("node1",)
    assert definition.stop_order == ("node1",)
    assert profile.placements == {
        "node1": ("deepseek-agent-single",),
        "node2": (),
    }
    assert profile.endpoints == {"deepseek": "deepseek-agent-single"}
    assert catalog.resolve_profile("default").id == "agent-full-dual"
    assert catalog.resolve_profile("agent").id == "agent-full-dual"


def test_definition_change_invalidates_lock(catalog_root: Path) -> None:
    workload = catalog_root / "config/workloads/deepseek-agent-dual.toml"
    workload.write_text(
        workload.read_text(encoding="utf-8").replace(
            "minimum_free_memory_bytes = 120000000000",
            "minimum_free_memory_bytes = 120000000001",
        ),
        encoding="utf-8",
    )

    with pytest.raises(CatalogError, match="lock fingerprint"):
        Catalog.load(catalog_root)


def test_model_owned_storage_paths_must_not_collide(catalog_root: Path) -> None:
    second = catalog_root / "config/workloads/qwen3-vl-8b-single.toml"
    second.write_text(
        second.read_text(encoding="utf-8").replace(
            "/srv/models/runtime-cache/qwen3-vl-8b-single",
            "/srv/models/runtime-cache/tokenrig-single",
        ),
        encoding="utf-8",
    )
    _refresh_definition_fingerprint(catalog_root)
    maturity = _read_report(catalog_root, "model-definitions.json")
    changed = load_workload(second)
    next(record for record in maturity["definitions"] if record["id"] == changed.id)["sha256"] = fingerprint(changed)
    _write_report(catalog_root, "model-definitions.json", maturity)

    with pytest.raises(CatalogError, match="model-owned storage path collision"):
        Catalog.load(catalog_root)


def test_toml_comments_do_not_change_definition_fingerprint(catalog_root: Path) -> None:
    catalog = Catalog.load(catalog_root)
    fingerprint = catalog.definition_fingerprints["deepseek-agent-dual"]
    workload = catalog_root / "config/workloads/deepseek-agent-dual.toml"
    workload.write_text(
        "# A comment is not part of a declarative definition.\n"
        + workload.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    reloaded = Catalog.load(catalog_root)

    assert reloaded.definition_fingerprints["deepseek-agent-dual"] == fingerprint


def test_changed_runtime_release_artifact_invalidates_catalog(
    catalog_root: Path,
) -> None:
    _enable_runtime_release(catalog_root)
    payload = catalog_root / "adapters/example/adapter.sh"
    payload.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")

    with pytest.raises(CatalogError, match="runtime release artifact digest"):
        Catalog.load(catalog_root)


def test_runtime_release_manifest_digest_is_verified(catalog_root: Path) -> None:
    _enable_runtime_release(catalog_root)
    manifest = catalog_root / "adapters/example/runtime-manifest.json"
    manifest.write_text('{"files": {}}\n', encoding="utf-8")

    with pytest.raises(CatalogError, match="runtime release manifest digest"):
        Catalog.load(catalog_root)


def test_evidence_indexes_satisfy_packaged_schemas(catalog_root: Path) -> None:
    validate_evidence_indexes(catalog_root)


def test_evidence_indexes_are_json_objects(catalog_root: Path) -> None:
    """The fixture itself remains legible before the catalog validates it."""
    for name in ("model-definitions.json", "accepted-cluster-profiles.json"):
        with (catalog_root / "inventory/reports" / name).open(encoding="utf-8") as file:
            assert isinstance(json.load(file), dict)


def _read_report(catalog_root: Path, name: str) -> dict:
    with (catalog_root / "inventory/reports" / name).open(encoding="utf-8") as source:
        return json.load(source)


def _write_report(catalog_root: Path, name: str, report: dict) -> None:
    (catalog_root / "inventory/reports" / name).write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _refresh_definition_fingerprint(catalog_root: Path) -> str:
    definition = load_workload(catalog_root / "config/workloads/deepseek-agent-dual.toml")
    value = fingerprint(definition)
    fingerprints = {
        load_workload(path).id: fingerprint(load_workload(path))
        for path in sorted((catalog_root / "config/workloads").glob("*.toml"))
    }
    (catalog_root / "locks/model-definitions.toml").write_text(
        "[definitions]\n"
        + "".join(
            f'{identifier} = "{digest}"\n'
            for identifier, digest in sorted(fingerprints.items())
        ),
        encoding="utf-8",
    )
    index = _read_report(catalog_root, "model-definitions.json")
    index["definitions"][0]["sha256"] = value
    _write_report(catalog_root, "model-definitions.json", index)
    return value


def _enable_runtime_release(catalog_root: Path) -> str:
    release = catalog_root / "adapters/example"
    release.mkdir(parents=True)
    payload = release / "adapter.sh"
    payload.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    manifest = release / "runtime-manifest.json"
    manifest.write_text(
        json.dumps(
            {"files": {"adapters/example/adapter.sh": _sha256(payload)}}, indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    workload = catalog_root / "config/workloads/deepseek-agent-dual.toml"
    old_release = '''[runtime_release]
manifest = "adapters/deepseek/mia-vllm/runtime-manifest.json"
sha256 = "11fa4d36945ed6530daf29f8b4342feaab90ad9cd47fa505cfd9858a358ebf37"'''
    new_release = f'''[runtime_release]
manifest = "adapters/example/runtime-manifest.json"
sha256 = "{_sha256(manifest)}"'''
    workload.write_text(
        workload.read_text(encoding="utf-8").replace(old_release, new_release),
        encoding="utf-8",
    )
    return _refresh_definition_fingerprint(catalog_root)


def _stage_report(
    catalog_root: Path,
    *,
    stage: str,
    predecessor: str | None,
    fingerprint_value: str,
    correction_position: int | None = None,
    workload_name: str = "deepseek-agent-dual",
) -> str:
    definition = load_workload(catalog_root / f"config/workloads/{workload_name}.toml")
    suffix = (
        f"{stage}-correction-{correction_position}"
        if correction_position is not None
        else stage
    )
    path = f"inventory/reports/model-definitions/{definition.id}-{suffix}.json"
    report = {
        "stage": stage,
        "definition_id": definition.id,
        "definition_sha256": fingerprint_value,
        "runtime_manifest_sha256": (
            definition.runtime_release.sha256 if definition.runtime_release else None
        ),
        "source": {"repository": definition.source.repository, "commit": definition.source.commit},
        "checkpoint": {
            "repository": definition.checkpoint.repository,
            "revision": definition.checkpoint.revision,
            "manifest_sha256": definition.checkpoint.manifest_sha256,
        },
        "image": {"reference": definition.image.reference},
        "recorded_at": "2026-08-02T08:00:00Z",
        "nodes": [
            {"node": node, "boot_id": str(position + 1) * 32}
            for position, node in enumerate(definition.nodes)
        ],
        "predecessor": predecessor,
        "gates": {
            "prepared": {"artifacts": True, "node_manifests": True},
            "verified": (
                {
                    "offline": True, "release": True, "image": True,
                    "architecture": True, "manifest": True, "mmap": True,
                    "api_identity": True,
                }
                if definition.topology == "single"
                else {
                    "offline": True, "release": True, "image": True,
                    "architecture": True, "fabric": True, "role": True,
                    "compose": True,
                }
            ),
            "accepted": {
                "quality": True, "lifecycle": True, "capacity": True,
                "performance": True, "thermal": True, "release": True, "reboot": True,
            },
        }[stage],
    }
    destination = catalog_root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return path


def _add_single_definition(catalog_root: Path) -> tuple[str, str, str]:
    """Add an independently evidenced, GPU node 1-only definition to the fixture."""
    source = (catalog_root / "config/workloads/deepseek-agent-dual.toml").read_text(
        encoding="utf-8"
    )
    (catalog_root / "config/workloads/single.toml").write_text(
        source.replace('id = "deepseek-agent-dual"', 'id = "single"')
        .replace('topology = "distributed"', 'topology = "single"')
        .replace('placement_class = "dual-exclusive"', 'placement_class = "single-exclusive"')
        .replace('nodes = ["node1", "node2"]', 'nodes = ["node1"]')
        .replace('start_order = ["node2", "node1"]', 'start_order = ["node1"]')
        .replace('stop_order = ["node1", "node2"]', 'stop_order = ["node1"]')
        .replace('/srv/models/snapshots/deepseek-v4-flash-0731', '/srv/models/snapshots/single')
        .replace('/srv/models/runtime-cache/deepseek-agent-dual', '/srv/models/runtime-cache/single')
        .replace('/srv/models/outputs/deepseek-agent-dual', '/srv/models/outputs/single'),
        encoding="utf-8",
    )
    definition = load_workload(catalog_root / "config/workloads/single.toml")
    fingerprint_value = fingerprint(definition)
    locks = catalog_root / "locks/model-definitions.toml"
    locks.write_text(
        locks.read_text(encoding="utf-8") + f'single = "{fingerprint_value}"\n',
        encoding="utf-8",
    )
    prepared = _stage_report(
        catalog_root,
        stage="prepared",
        predecessor=None,
        fingerprint_value=fingerprint_value,
        workload_name="single",
    )
    verified = _stage_report(
        catalog_root,
        stage="verified",
        predecessor=prepared,
        fingerprint_value=fingerprint_value,
        workload_name="single",
    )
    index = _read_report(catalog_root, "model-definitions.json")
    index["definitions"].append(
        {
            "id": "single",
            "sha256": fingerprint_value,
            "maturity": "verified",
            "history": [
                _transition("planned", "2026-08-02T08:00:00Z"),
                {
                    **_transition("prepared", "2026-08-02T08:01:00Z"),
                    "evidence_refs": [prepared],
                },
                {
                    **_transition("verified", "2026-08-02T08:02:00Z"),
                    "evidence_refs": [verified],
                },
            ],
        }
    )
    _write_report(catalog_root, "model-definitions.json", index)
    return fingerprint_value, prepared, verified


def test_single_node_evidence_chain_loads_for_its_declared_node(
    catalog_root: Path,
) -> None:
    _add_single_definition(catalog_root)

    catalog = Catalog.load(catalog_root)

    assert catalog.maturity["single"] == "verified"


@pytest.mark.parametrize(
    ("nodes", "error"),
    (
        ([{"node": "node2", "boot_id": "2" * 32}], "maturity evidence nodes"),
        (
            [
                {"node": "node1", "boot_id": "1" * 32},
                {"node": "node2", "boot_id": "2" * 32},
            ], "maturity evidence nodes"),
        (
            [
                {"node": "node1", "boot_id": "1" * 32},
                {"node": "node1", "boot_id": "2" * 32},
            ], "maturity evidence nodes"),
    ),
)
def test_single_node_evidence_rejects_missing_extra_or_duplicate_nodes(
    catalog_root: Path, nodes: list[dict[str, str]], error: str
) -> None:
    _, prepared, _ = _add_single_definition(catalog_root)
    report_path = catalog_root / prepared
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["nodes"] = nodes
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(CatalogError, match=error):
        Catalog.load(catalog_root)


def test_single_node_report_is_rejected_for_the_dual_definition(
    catalog_root: Path,
) -> None:
    catalog = Catalog.load(catalog_root)
    fingerprint_value = catalog.definition_fingerprints["deepseek-agent-dual"]
    _advance_to(catalog_root, "prepared", fingerprint_value)
    report_path = "inventory/reports/model-definitions/deepseek-agent-dual-prepared.json"
    destination = catalog_root / report_path
    report = json.loads(destination.read_text(encoding="utf-8"))
    report["nodes"] = [{"node": "node1", "boot_id": "1" * 32}]
    destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(CatalogError, match="maturity evidence nodes"):
        Catalog.load(catalog_root)


def _advance_to(catalog_root: Path, stage: str, fingerprint_value: str) -> None:
    index = _read_report(catalog_root, "model-definitions.json")
    history = index["definitions"][0]["history"]
    predecessor = None
    if stage in {"verified", "accepted"}:
        previous = "prepared" if stage == "verified" else "verified"
        predecessor = f"inventory/reports/model-definitions/deepseek-agent-dual-{previous}.json"
    evidence_path = _stage_report(
        catalog_root, stage=stage, predecessor=predecessor, fingerprint_value=fingerprint_value
    )
    history.append(
        {
            "state": stage,
            "timestamp": f"2026-08-02T15:0{len(history)}:00Z",
            "evidence_refs": [evidence_path],
            "rejection_reason": None,
        }
    )
    index["definitions"][0]["maturity"] = stage
    _write_report(catalog_root, "model-definitions.json", index)


def _transition(
    state: str,
    timestamp: str,
    *,
    rejection_reason: str | None = None,
    correction_of: int | None = None,
    correction_reason: str | None = None,
) -> dict:
    transition = {
        "state": state,
        "timestamp": timestamp,
        "evidence_refs": [
            "docs/superpowers/specs/2026-08-02-multi-runtime-model-profiles-design.md"
        ],
        "rejection_reason": rejection_reason,
    }
    if correction_of is not None:
        transition["correction_of"] = correction_of
    if correction_reason is not None:
        transition["correction_reason"] = correction_reason
    return transition


def _profile_workload_ids(profile) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                identifier
                for identifiers in profile.placements.values()
                for identifier in identifiers
            }
        )
    )


def test_definition_evidence_requires_transition_history(catalog_root: Path) -> None:
    """Dropping auditable history from a maturity record must fail closed."""
    report = _read_report(catalog_root, "model-definitions.json")
    report["definitions"][0].pop("history", None)
    _write_report(catalog_root, "model-definitions.json", report)

    with pytest.raises(CatalogError, match="history.*required"):
        Catalog.load(catalog_root)


def test_definition_evidence_rejects_illegal_maturity_progression(
    catalog_root: Path,
) -> None:
    """Skipping preparation and verification must not produce accepted evidence."""
    report = _read_report(catalog_root, "model-definitions.json")
    report["definitions"][0]["maturity"] = "accepted"
    report["definitions"][0]["history"] = [
        _transition("planned", "2026-08-02T08:00:00Z"),
        _transition("accepted", "2026-08-02T08:01:00Z"),
    ]
    _write_report(catalog_root, "model-definitions.json", report)

    with pytest.raises(CatalogError, match="illegal maturity transition.*planned.*accepted"):
        Catalog.load(catalog_root)


def test_definition_current_maturity_must_match_history(catalog_root: Path) -> None:
    """A stale current-state field must not disagree with its audit trail."""
    report = _read_report(catalog_root, "model-definitions.json")
    report["definitions"][0]["maturity"] = "prepared"
    report["definitions"][0]["history"] = [
        _transition("planned", "2026-08-02T08:00:00Z")
    ]
    _write_report(catalog_root, "model-definitions.json", report)

    with pytest.raises(CatalogError, match="current maturity does not match history"):
        Catalog.load(catalog_root)


@pytest.mark.parametrize(
    ("state", "rejection_reason"),
    (("rejected", None), ("verified", "runtime output regressed")),
)
def test_rejection_reason_is_present_only_for_rejected_transitions(
    catalog_root: Path,
    state: str,
    rejection_reason: str | None,
) -> None:
    """A missing or misplaced rejection reason must invalidate evidence."""
    report = _read_report(catalog_root, "model-definitions.json")
    report["definitions"][0]["maturity"] = state
    report["definitions"][0]["history"] = [
        _transition(
            state,
            "2026-08-02T08:00:00Z",
            rejection_reason=rejection_reason,
        )
    ]
    _write_report(catalog_root, "model-definitions.json", report)

    with pytest.raises(CatalogError, match="rejection_reason"):
        Catalog.load(catalog_root)


def test_rejected_definition_requires_audited_correction_metadata(
    catalog_root: Path,
) -> None:
    """A bare rejected-to-verified transition must remain fail-closed."""
    report = _read_report(catalog_root, "model-definitions.json")
    report["definitions"][0]["maturity"] = "verified"
    report["definitions"][0]["history"] = [
        _transition("planned", "2026-08-02T08:00:00Z"),
        _transition("prepared", "2026-08-02T08:01:00Z"),
        _transition("verified", "2026-08-02T08:02:00Z"),
        _transition(
            "rejected",
            "2026-08-02T08:03:00Z",
            rejection_reason="runtime output regressed",
        ),
        _transition("verified", "2026-08-02T08:04:00Z"),
    ]
    _write_report(catalog_root, "model-definitions.json", report)

    with pytest.raises(
        CatalogError,
        match="rejected to verified requires correction_of and correction_reason",
    ):
        Catalog.load(catalog_root)


def test_rejected_definition_accepts_a_positioned_canonical_correction_report(
    catalog_root: Path,
) -> None:
    """A correction carries fresh evidence without reusing the verified report."""
    catalog = Catalog.load(catalog_root)
    fingerprint_value = catalog.definition_fingerprints["deepseek-agent-dual"]
    prepared = _stage_report(
        catalog_root,
        stage="prepared",
        predecessor=None,
        fingerprint_value=fingerprint_value,
    )
    verified = _stage_report(
        catalog_root,
        stage="verified",
        predecessor=prepared,
        fingerprint_value=fingerprint_value,
    )
    correction = _stage_report(
        catalog_root,
        stage="verified",
        predecessor=verified,
        fingerprint_value=fingerprint_value,
        correction_position=4,
    )
    report = _read_report(catalog_root, "model-definitions.json")
    report["definitions"][0]["maturity"] = "verified"
    report["definitions"][0]["history"] = [
        _transition("planned", "2026-08-02T08:00:00Z"),
        {
            **_transition("prepared", "2026-08-02T08:01:00Z"),
            "evidence_refs": [prepared],
        },
        {
            **_transition("verified", "2026-08-02T08:02:00Z"),
            "evidence_refs": [verified],
        },
        _transition(
            "rejected",
            "2026-08-02T08:03:00Z",
            rejection_reason="runtime output regressed",
        ),
        {
            **_transition(
                "verified",
                "2026-08-02T08:04:00Z",
                correction_of=3,
                correction_reason="audit proved the regression fixture was corrupt",
            ),
            "evidence_refs": [correction],
        },
    ]
    _write_report(catalog_root, "model-definitions.json", report)

    loaded = Catalog.load(catalog_root)

    assert loaded.maturity["deepseek-agent-dual"] == "verified"


def test_rejected_definition_correction_rejects_reused_verified_report(
    catalog_root: Path,
) -> None:
    catalog = Catalog.load(catalog_root)
    fingerprint_value = catalog.definition_fingerprints["deepseek-agent-dual"]
    prepared = _stage_report(
        catalog_root, stage="prepared", predecessor=None, fingerprint_value=fingerprint_value
    )
    verified = _stage_report(
        catalog_root, stage="verified", predecessor=prepared, fingerprint_value=fingerprint_value
    )
    report = _read_report(catalog_root, "model-definitions.json")
    report["definitions"][0]["maturity"] = "verified"
    report["definitions"][0]["history"] = [
        _transition("planned", "2026-08-02T08:00:00Z"),
        {**_transition("prepared", "2026-08-02T08:01:00Z"), "evidence_refs": [prepared]},
        {**_transition("verified", "2026-08-02T08:02:00Z"), "evidence_refs": [verified]},
        _transition("rejected", "2026-08-02T08:03:00Z", rejection_reason="runtime output regressed"),
        {
            **_transition("verified", "2026-08-02T08:04:00Z", correction_of=3, correction_reason="fixture"),
                "evidence_refs": [
                    "docs/superpowers/specs/2026-08-02-multi-runtime-model-profiles-design.md"
                ],
        },
    ]
    _write_report(catalog_root, "model-definitions.json", report)

    with pytest.raises(CatalogError, match="verified maturity evidence must name its canonical report"):
        Catalog.load(catalog_root)


def test_rejected_correction_must_reference_immediately_prior_transition(
    catalog_root: Path,
) -> None:
    """A correction cannot point at an older non-rejection audit event."""
    report = _read_report(catalog_root, "model-definitions.json")
    report["definitions"][0]["maturity"] = "verified"
    report["definitions"][0]["history"] = [
        _transition("planned", "2026-08-02T08:00:00Z"),
        _transition("prepared", "2026-08-02T08:01:00Z"),
        _transition("verified", "2026-08-02T08:02:00Z"),
        _transition(
            "rejected",
            "2026-08-02T08:03:00Z",
            rejection_reason="runtime output regressed",
        ),
        _transition(
            "verified",
            "2026-08-02T08:04:00Z",
            correction_of=2,
            correction_reason="audit proved the regression fixture was corrupt",
        ),
    ]
    _write_report(catalog_root, "model-definitions.json", report)

    with pytest.raises(CatalogError, match="must reference transition 3"):
        Catalog.load(catalog_root)


def test_correction_metadata_is_forbidden_on_normal_progression(
    catalog_root: Path,
) -> None:
    """Removing the prior-rejection check must make this regression fail."""
    report = _read_report(catalog_root, "model-definitions.json")
    report["definitions"][0]["maturity"] = "verified"
    report["definitions"][0]["history"] = [
        _transition("planned", "2026-08-02T08:00:00Z"),
        _transition("prepared", "2026-08-02T08:01:00Z"),
        _transition(
            "verified",
            "2026-08-02T08:02:00Z",
            correction_of=1,
            correction_reason="not actually correcting a rejection",
        ),
    ]
    _write_report(catalog_root, "model-definitions.json", report)

    with pytest.raises(CatalogError, match="correction metadata may only follow rejected"):
        Catalog.load(catalog_root)


def test_maturity_history_timestamps_must_increase(catalog_root: Path) -> None:
    """Reordered audit entries must not be accepted as a valid history."""
    report = _read_report(catalog_root, "model-definitions.json")
    report["definitions"][0]["maturity"] = "prepared"
    report["definitions"][0]["history"] = [
        _transition("planned", "2026-08-02T08:01:00Z"),
        _transition("prepared", "2026-08-02T08:00:00Z"),
    ]
    _write_report(catalog_root, "model-definitions.json", report)

    with pytest.raises(CatalogError, match="timestamps must increase"):
        Catalog.load(catalog_root)


def test_maturity_history_evidence_reference_must_exist(catalog_root: Path) -> None:
    """A plausible-looking but absent evidence path must not be auditable."""
    report = _read_report(catalog_root, "model-definitions.json")
    report["definitions"][0]["history"][0]["evidence_refs"] = [
        "docs/audits/not-checked-in.md"
    ]
    _write_report(catalog_root, "model-definitions.json", report)

    with pytest.raises(CatalogError, match="evidence reference does not exist"):
        Catalog.load(catalog_root)


def test_prepared_transition_requires_schema_valid_evidence_report(
    catalog_root: Path,
) -> None:
    Catalog.load(catalog_root)
    index = _read_report(catalog_root, "model-definitions.json")
    path = "inventory/reports/model-definitions/deepseek-agent-dual-prepared.json"
    destination = catalog_root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text('{"stage": "prepared"}\n', encoding="utf-8")
    index["definitions"][0]["maturity"] = "prepared"
    index["definitions"][0]["history"].append(
        {
            "state": "prepared",
            "timestamp": "2026-08-02T15:01:00Z",
            "evidence_refs": [path],
            "rejection_reason": None,
        }
    )
    _write_report(catalog_root, "model-definitions.json", index)

    with pytest.raises(CatalogError, match="invalid prepared maturity evidence"):
        Catalog.load(catalog_root)


def test_prepared_transition_rejects_an_arbitrary_existing_evidence_reference(
    catalog_root: Path,
) -> None:
    index = _read_report(catalog_root, "model-definitions.json")
    index["definitions"][0]["maturity"] = "prepared"
    index["definitions"][0]["history"].append(
        {
            "state": "prepared",
            "timestamp": "2026-08-02T15:01:00Z",
            "evidence_refs": [
                "docs/superpowers/specs/2026-08-02-multi-runtime-model-profiles-design.md"
            ],
            "rejection_reason": None,
        }
    )
    _write_report(catalog_root, "model-definitions.json", index)

    with pytest.raises(CatalogError, match="prepared maturity evidence must name its canonical report"):
        Catalog.load(catalog_root)


def test_maturity_evidence_pins_must_match_current_definition(
    catalog_root: Path,
) -> None:
    catalog = Catalog.load(catalog_root)
    fingerprint_value = catalog.definition_fingerprints["deepseek-agent-dual"]
    _advance_to(catalog_root, "prepared", fingerprint_value)
    path = catalog_root / "inventory/reports/model-definitions/deepseek-agent-dual-prepared.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    report["image"]["reference"] = "example.invalid/image@sha256:" + "a" * 64
    path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(CatalogError, match="image pin does not match definition"):
        Catalog.load(catalog_root)


def test_verified_evidence_requires_immediately_prior_report(
    catalog_root: Path,
) -> None:
    catalog = Catalog.load(catalog_root)
    fingerprint_value = catalog.definition_fingerprints["deepseek-agent-dual"]
    _advance_to(catalog_root, "prepared", fingerprint_value)
    _advance_to(catalog_root, "verified", fingerprint_value)
    path = catalog_root / "inventory/reports/model-definitions/deepseek-agent-dual-verified.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    report["predecessor"] = "inventory/reports/model-definitions/not-the-prepared-report.json"
    path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(CatalogError, match="predecessor does not name the immediately prior"):
        Catalog.load(catalog_root)


def test_accepted_profile_evidence_requires_audit_metadata(catalog_root: Path) -> None:
    """An otherwise exact profile hash must not be accepted without provenance."""
    catalog = Catalog.load(catalog_root)
    profile = catalog.resolve_profile("default")
    report = {
        "profiles": [
            {
                "profile_sha256": catalog.profile_fingerprints[profile.id],
                "definition_sha256": sorted(
                    catalog.definition_fingerprints[identifier]
                    for identifier in _profile_workload_ids(profile)
                ),
            }
        ]
    }
    _write_report(catalog_root, "accepted-cluster-profiles.json", report)

    with pytest.raises(CatalogError, match="accepted_at.*required"):
        Catalog.load(catalog_root)


def test_accepted_profile_evidence_preserves_public_hash_mapping(
    catalog_root: Path,
) -> None:
    """Audit metadata must not change the accepted_profiles public mapping."""
    catalog = Catalog.load(catalog_root)
    profile = catalog.resolve_profile("default")
    profile_hash = catalog.profile_fingerprints[profile.id]
    definition_hashes = tuple(
        sorted(
            catalog.definition_fingerprints[identifier]
            for identifier in _profile_workload_ids(profile)
        )
    )
    report = {
        "profiles": [
            {
                "profile_sha256": profile_hash,
                "definition_sha256": list(definition_hashes),
                "accepted_at": "2026-08-02T08:00:00Z",
                "evidence_refs": [
                    "docs/superpowers/specs/2026-08-02-multi-runtime-model-profiles-design.md"
                ],
            }
        ]
    }
    _write_report(catalog_root, "accepted-cluster-profiles.json", report)

    loaded = Catalog.load(catalog_root)

    assert loaded.accepted_profiles == {profile_hash: definition_hashes}


@pytest.mark.parametrize(
    ("field", "value", "error"),
    (
        ("accepted_at", "not-a-timestamp", "invalid timestamp"),
        ("evidence_refs", ["docs/audits/not-checked-in.md"], "evidence reference does not exist"),
    ),
)
def test_accepted_profile_audit_metadata_is_semantically_validated(
    catalog_root: Path,
    field: str,
    value: str | list[str],
    error: str,
) -> None:
    """Removing timestamp or evidence-path validation must fail this check."""
    catalog = Catalog.load(catalog_root)
    profile = catalog.resolve_profile("default")
    report = {
        "profiles": [
            {
                "profile_sha256": catalog.profile_fingerprints[profile.id],
                "definition_sha256": sorted(
                    catalog.definition_fingerprints[identifier]
                    for identifier in _profile_workload_ids(profile)
                ),
                "accepted_at": "2026-08-02T08:00:00Z",
                "evidence_refs": [
                    "docs/superpowers/specs/2026-08-02-multi-runtime-model-profiles-design.md"
                ],
            }
        ]
    }
    report["profiles"][0][field] = value
    _write_report(catalog_root, "accepted-cluster-profiles.json", report)

    with pytest.raises(CatalogError, match=error):
        Catalog.load(catalog_root)
