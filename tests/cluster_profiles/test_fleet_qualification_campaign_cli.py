from __future__ import annotations

import hashlib
import json
from argparse import Namespace
from pathlib import Path
from urllib.parse import unquote

import pytest

from cluster_profiles import fleet_qualification_campaign_cli as campaign_cli
from cluster_profiles.fleet_qualification import EvidenceLedger, QualificationError
from cluster_profiles.qualification_fixtures import (
    FixtureRegistry,
    ServiceCase,
    ServiceRecipe,
)
from cluster_profiles.qualification_locking import node_locks

NODE_A = "spk_" + "1" * 32
NODE_B = "spk_" + "2" * 32
RUN_ID = "qualification-run"
REVISION_ID = "recipe-revision"
CAMPAIGN_ID = "a" * 64
RECIPE_KEY = "vonk-forge/test-model"
CONTENT_SHA = "c" * 64


def _catalog_inputs(root: Path) -> tuple[Path, bytes, bytes]:
    package = b"reviewed recipe package"
    package_path = root / "packages" / "test-model.tar.gz"
    package_path.parent.mkdir(parents=True, exist_ok=True)
    package_path.write_bytes(package)

    index = {
        "schema_version": 2,
        "fixtures": {},
        "recipes": {},
        "special_fixtures": {},
        "service_case_templates": {},
        "service_recipes": {},
    }
    fixture_raw = json.dumps(index, sort_keys=True).encode()
    fixture_path = root / "qualification" / "qualification-index.json"
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    fixture_path.write_bytes(fixture_raw)

    source_commit = "1" * 40
    catalog_raw = json.dumps({"source_commit": source_commit}, sort_keys=True).encode()
    (root / "catalog-index.json").write_bytes(catalog_raw)

    authority = {
        "schema_version": 3,
        "authority_id": "test-authority",
        "catalog": {
            "repository": "test/recipes",
            "commit": "2" * 40,
            "release_tag": "v1.0.0",
            "source_commit": source_commit,
            "catalog_index_sha256": hashlib.sha256(catalog_raw).hexdigest(),
            "qualification_index_sha256": hashlib.sha256(fixture_raw).hexdigest(),
            "recipe_count": 1,
        },
        "scope": {
            "maximum_node_count": 2,
            "recipe_count": 1,
            "excluded_topology_recipe_keys": [],
        },
        "recipes": [
            {
                "sequence": 1,
                "key": RECIPE_KEY,
                "recipe_version": "1.0.0",
                "content_sha256": CONTENT_SHA,
                "package": {
                    "path": "packages/test-model.tar.gz",
                    "sha256": hashlib.sha256(package).hexdigest(),
                    "expected_bytes": len(package),
                    "media_type": "application/vnd.vonk-forge.recipe-package.v2+tar+gzip",
                },
                "node_count": 1,
                "interface": "openai-service",
                "disposition": "actionable",
                "operator_acceptance_required": False,
                "model_license_refs": [
                    {
                        "key": "test/model",
                        "content_sha256": "d" * 64,
                        "spdx": "Apache-2.0",
                        "url": "https://example.invalid/license",
                        "attribution": [],
                        "operator_acceptance_required": False,
                    }
                ],
                "qualification_inputs": [],
                "smoke_cases": ["health"],
                "review_gates": [],
            }
        ],
    }
    authority_path = root / "qualification" / "authorities" / "test.json"
    authority_path.parent.mkdir(parents=True, exist_ok=True)
    authority_path.write_text(json.dumps(authority), encoding="utf-8")

    campaign_path = root / "qualification" / "campaigns" / "test.json"
    campaign_path.parent.mkdir(parents=True, exist_ok=True)
    campaign_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "qualification_authority": "../authorities/test.json",
                "fixture_manifest": "../qualification-index.json",
                "options": {"cleanup": "stop"},
            }
        ),
        encoding="utf-8",
    )
    return campaign_path, package, fixture_raw


def _row(
    *, node_count: int = 1, review_gates: tuple[dict[str, object], ...] = ()
) -> campaign_cli.RecipeAuthorityRow:
    raw = {
        "sequence": 1,
        "key": RECIPE_KEY,
        "content_sha256": CONTENT_SHA,
        "node_count": node_count,
        "interface": "openai-service",
        "recipe_version": "1.0.0",
        "package": {},
        "disposition": "actionable",
        "operator_acceptance_required": False,
        "model_license_refs": [],
        "qualification_inputs": [],
        "smoke_cases": ["health"],
        "review_gates": [dict(gate) for gate in review_gates],
    }
    return campaign_cli.RecipeAuthorityRow(
        sequence=1,
        key=RECIPE_KEY,
        content_sha256=CONTENT_SHA,
        node_count=node_count,
        interface="openai-service",
        recipe_version="1.0.0",
        package={},
        disposition="actionable",
        review_gates=review_gates,
        operator_acceptance_required=False,
        model_license_refs=(),
        qualification_inputs=(),
        smoke_cases=("health",),
        raw=raw,
    )


def _node(
    node_id: str,
    *,
    online: bool = True,
    boot_id: str = "boot-before",
    loaded: list[dict[str, object]] | None = None,
    freshness: str = "live",
) -> dict[str, object]:
    return {
        "id": node_id,
        "connection": {"online_state": "online" if online else "offline"},
        "telemetry": {"freshness": freshness, "sample": {"boot_id": boot_id}},
        "loaded": loaded or [],
    }


class _MissingEndpoint:
    def request(self, method: str, path: str, *args: object, **kwargs: object) -> dict[str, object]:
        assert method == "GET"
        alias = unquote(path.rsplit("/", 1)[-1])
        assert alias == "test-alias"
        raise campaign_cli.ControlNotFound(404, "endpoint is absent", endpoint=path)


def test_manifest_loads_confined_parent_references_and_binds_all_local_inputs(
    tmp_path: Path,
) -> None:
    campaign_path, _package, _fixture_raw = _catalog_inputs(tmp_path)

    manifest = campaign_cli.load_manifest(campaign_path, tmp_path)
    fixtures = FixtureRegistry.load(manifest.fixture_manifest)
    campaign_cli._bind_repository_inputs(manifest, tmp_path, fixtures)

    assert manifest.authority.authority_id == "test-authority"
    assert len(manifest.authority.rows) == 1
    assert manifest.authority.rows[0].interface == "openai-service"
    assert fixtures.manifest_sha256 == manifest.authority.catalog["qualification_index_sha256"]


def test_manifest_rejects_parent_references_that_escape_or_follow_symlinks(
    tmp_path: Path,
) -> None:
    campaign_path, _package, _fixture_raw = _catalog_inputs(tmp_path)
    campaign_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "qualification_authority": "../../../outside.json",
                "fixture_manifest": "../qualification-index.json",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(QualificationError, match="escapes the recipe repository"):
        campaign_cli.load_manifest(campaign_path, tmp_path)

    campaign_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "qualification_authority": "../authorities/test.json",
                "fixture_manifest": "../qualification-index.json",
            }
        ),
        encoding="utf-8",
    )
    fixture_path = tmp_path / "qualification" / "qualification-index.json"
    fixture_path.unlink()
    outside = tmp_path.parent / f"{tmp_path.name}-outside.json"
    outside.write_text("{}", encoding="utf-8")
    fixture_path.symlink_to(outside)
    with pytest.raises(QualificationError, match="symbolic link"):
        campaign_cli.load_manifest(campaign_path, tmp_path)


def test_exact_fixture_cases_are_derived_from_the_reviewed_registry() -> None:
    row = _row()
    registry = FixtureRegistry(
        fixtures={},
        recipes={},
        special={},
        manifest_sha256="e" * 64,
        service_cases={},
        service_recipes={
            RECIPE_KEY: ServiceRecipe(
                key=RECIPE_KEY,
                content_sha256=CONTENT_SHA,
                alias="test-alias",
                cases=(
                    ServiceCase(
                        case_id="health",
                        method="GET",
                        path="/health",
                        body=None,
                        timeout_seconds=1,
                        max_response_bytes=128,
                        assertions=(),
                    ),
                ),
                higher_tiers={},
            )
        },
    )
    # A fixture-free request has no asset references; the authority records the
    # actual case order, not an invented input ID.
    row = campaign_cli.RecipeAuthorityRow(
        sequence=row.sequence,
        key=row.key,
        content_sha256=row.content_sha256,
        node_count=row.node_count,
        interface=row.interface,
        recipe_version=row.recipe_version,
        package=row.package,
        disposition=row.disposition,
        review_gates=row.review_gates,
        operator_acceptance_required=row.operator_acceptance_required,
        model_license_refs=row.model_license_refs,
        qualification_inputs=(),
        smoke_cases=("health",),
        raw=row.raw,
    )

    assert campaign_cli._fixture_bindings(row, registry)[0] == "openai-service"
    assert campaign_cli._fixture_bindings(row, registry)[1]["cases"][0]["id"] == "health"


def test_rank_loss_requires_exact_failed_rank_and_withdrawn_route() -> None:
    survivor = {
        "run_id": RUN_ID,
        "recipe_revision_id": REVISION_ID,
        "alias": "test-alias",
        "rank": 0,
        "expected_rank_count": 2,
        "present_ranks": [0],
        "group_state": "degraded",
        "route_state": "withdrawn",
        "healthy": False,
    }
    fleet = {
        "nodes": [
            _node(NODE_A, loaded=[survivor]),
            _node(NODE_B, online=False),
        ]
    }
    observed, proof = campaign_cli._rank_lost(
        _MissingEndpoint(),
        fleet,
        run_id=RUN_ID,
        revision_id=REVISION_ID,
        alias="test-alias",
        node_ids=[NODE_A, NODE_B],
        node_to_rank={NODE_A: 0, NODE_B: 1},
        failure_node_id=NODE_B,
    )
    assert observed is True
    assert proof["failure_rank"] == 1
    assert proof["endpoint_not_found"] is True

    fleet["nodes"][0]["loaded"][0]["route_state"] = "published"  # type: ignore[index]
    observed, _proof = campaign_cli._rank_lost(
        _MissingEndpoint(),
        fleet,
        run_id=RUN_ID,
        revision_id=REVISION_ID,
        alias="test-alias",
        node_ids=[NODE_A, NODE_B],
        node_to_rank={NODE_A: 0, NODE_B: 1},
        failure_node_id=NODE_B,
    )
    assert observed is False


def test_offline_restart_requires_observed_downtime_and_changed_live_boot_id(
    tmp_path: Path,
) -> None:
    row = _row()
    ledger = EvidenceLedger(tmp_path / "evidence.jsonl")
    ledger.append(
        "host-restart.baseline",
        plan_digest=CAMPAIGN_ID,
        recipe=RECIPE_KEY,
        payload={"nodes": {NODE_A: "boot-before"}, "route_alias": "test-alias", "run_id": RUN_ID},
    )
    common = {
        "row": row,
        "campaign_id": CAMPAIGN_ID,
        "run_id": RUN_ID,
        "alias": "test-alias",
        "node_ids": [NODE_A],
        "ledger": ledger,
        "client": _MissingEndpoint(),
    }

    offline = campaign_cli._restart_observation(
        **common,
        fleet={"nodes": [_node(NODE_A, online=False, freshness="stale")]},
    )
    assert offline["checkpoint"] == "host-online"

    unchanged = campaign_cli._restart_observation(
        **common,
        fleet={"nodes": [_node(NODE_A, boot_id="boot-before")]},
    )
    assert unchanged["complete"] is False
    assert "unchanged boot ID" in unchanged["reason"]

    restarted = campaign_cli._restart_observation(
        **common,
        fleet={"nodes": [_node(NODE_A, boot_id="boot-after")]},
    )
    assert restarted["complete"] is True
    recovered = campaign_cli._latest_payload(
        ledger, CAMPAIGN_ID, RECIPE_KEY, "host-restart.recovered"
    )
    assert recovered is not None
    assert recovered["observed_boot_id"] == "boot-after"


def test_offline_restart_refuses_an_unrelated_whole_fleet_workload(
    tmp_path: Path,
) -> None:
    ledger = EvidenceLedger(tmp_path / "busy-evidence.jsonl")
    ledger.append(
        "host-restart.baseline",
        plan_digest=CAMPAIGN_ID,
        recipe=RECIPE_KEY,
        payload={"nodes": {NODE_A: "boot-before"}, "route_alias": "test-alias", "run_id": RUN_ID},
    )
    foreign = {"run_id": "other-run"}

    with pytest.raises(QualificationError, match="every whole-Fleet workload"):
        campaign_cli._restart_observation(
            client=_MissingEndpoint(),
            fleet={"nodes": [_node(NODE_A, loaded=[foreign])]},
            row=_row(),
            campaign_id=CAMPAIGN_ID,
            run_id=RUN_ID,
            alias="test-alias",
            node_ids=[NODE_A],
            ledger=ledger,
        )


def _append_single_recipe_evidence(
    ledger: EvidenceLedger, *, include_offline: bool = True, same_boot: bool = False
) -> None:
    ledger.append(
        "plan.generated",
        plan_digest=CAMPAIGN_ID,
        recipe=RECIPE_KEY,
        payload={
            "authority_row": {
                "sequence": 1,
                "key": RECIPE_KEY,
                "content_sha256": CONTENT_SHA,
                "node_count": 1,
                "interface": "openai-service",
                "recipe_version": "1.0.0",
                "package": {},
                "disposition": "actionable",
                "operator_acceptance_required": False,
                "model_license_refs": [],
                "qualification_inputs": [],
                "smoke_cases": ["health"],
                "review_gates": [],
            },
            "controller_recipe_identity": {
                "content_sha256": CONTENT_SHA,
                "recipe_revision_id": REVISION_ID,
            },
            "profile_digest": "profile-digest",
            "preview": {"plan_digest": "profile-plan"},
            "smoke_preview": {
                "kind": "openai-service",
                "endpoint_alias": "test-alias",
                "fixture_manifest_sha256": "e" * 64,
                "cases": [{"id": "health"}],
            },
        },
    )
    ledger.append(
        "canary.completed",
        plan_digest=CAMPAIGN_ID,
        recipe=RECIPE_KEY,
        payload={
            "recipe_content_sha256": CONTENT_SHA,
            "alias": "test-alias",
            "run_id": RUN_ID,
            "recipe_revision_id": REVISION_ID,
            "node_to_rank": {NODE_A: 0},
            "application": {
                "state": "succeeded",
                "profile_digest": "profile-digest",
                "plan_digest": "profile-plan",
            },
            "smoke": {
                "fixture_manifest_sha256": "e" * 64,
                "case_id": "health",
            },
            "review_acknowledgements": {
                "operator_acceptance": False,
                "capacity_review": False,
            },
        },
    )
    ledger.append(
        "profile.cleanup.completed",
        plan_digest=CAMPAIGN_ID,
        recipe=RECIPE_KEY,
        payload={
            "application_id": "cleanup-application",
            "application_state": "succeeded",
            "cleanup_policy": "stop",
            "uninstalls": 0,
            "route_withdrawn": True,
            "run_absent_from_fleet": True,
        },
    )
    ledger.append(
        "host-restart.baseline",
        plan_digest=CAMPAIGN_ID,
        recipe=RECIPE_KEY,
        payload={"nodes": {NODE_A: "boot-before"}, "route_alias": "test-alias", "run_id": RUN_ID},
    )
    if include_offline:
        ledger.append(
            "host-restart.offline",
            plan_digest=CAMPAIGN_ID,
            recipe=RECIPE_KEY,
            payload={"node_id": NODE_A, "online_state": "offline", "baseline_boot_id": "boot-before"},
        )
    ledger.append(
        "host-restart.recovered",
        plan_digest=CAMPAIGN_ID,
        recipe=RECIPE_KEY,
        payload={
            "node_id": NODE_A,
            "online_state": "online",
            "baseline_boot_id": "boot-before",
            "observed_boot_id": "boot-before" if same_boot else "boot-after",
            "telemetry_freshness": "live",
        },
    )


def test_spark_accepted_requires_the_offline_event_and_changed_boot_id(
    tmp_path: Path,
) -> None:
    row = _row()
    ledger = EvidenceLedger(tmp_path / "complete-evidence.jsonl")
    _append_single_recipe_evidence(ledger, include_offline=False)
    with pytest.raises(QualificationError, match="offline observation"):
        campaign_cli._accept_if_complete(
            row=row,
            campaign_id=CAMPAIGN_ID,
            ledger=ledger,
            node_ids=[NODE_A],
        )

    other = EvidenceLedger(tmp_path / "unchanged-boot-evidence.jsonl")
    _append_single_recipe_evidence(other, same_boot=True)
    with pytest.raises(QualificationError, match="changed live boot ID"):
        campaign_cli._accept_if_complete(
            row=row,
            campaign_id=CAMPAIGN_ID,
            ledger=other,
            node_ids=[NODE_A],
        )


def test_review_gates_must_be_acknowledged_for_the_exact_recipe() -> None:
    row = campaign_cli.RecipeAuthorityRow(
        sequence=1,
        key=RECIPE_KEY,
        content_sha256=CONTENT_SHA,
        node_count=1,
        interface="openai-service",
        recipe_version="1.0.0",
        package={},
        disposition="operator-acceptance-required",
        review_gates=(
            {"kind": "operator-acceptance-required", "reason": "review license"},
            {"kind": "capacity-review", "reason": "review capacity"},
        ),
        operator_acceptance_required=True,
        model_license_refs=(),
        qualification_inputs=(),
        smoke_cases=("health",),
        raw={},
    )
    missing_capacity = Namespace(
        accept_operator_gate=[RECIPE_KEY], accept_capacity_review=[]
    )
    with pytest.raises(QualificationError, match="accept-capacity-review"):
        campaign_cli._operator_gate(missing_capacity, row)

    accepted = Namespace(
        accept_operator_gate=[RECIPE_KEY], accept_capacity_review=[RECIPE_KEY]
    )
    campaign_cli._operator_gate(accepted, row)


def test_profile_ledger_label_fits_the_current_contract(tmp_path: Path) -> None:
    identity = campaign_cli._ledger_identity(tmp_path / "evidence.jsonl")
    assert len(identity) == 63


def test_node_lock_is_shared_across_independent_ledgers(tmp_path: Path) -> None:
    lock_directory = tmp_path / "node-locks"
    with node_locks([NODE_A], lock_directory=lock_directory):
        with (
            pytest.raises(QualificationError, match="owns controller node"),
            node_locks([NODE_A], lock_directory=lock_directory),
        ):
            pass
        with node_locks([NODE_B], lock_directory=lock_directory):
            pass


def test_profile_campaign_lock_covers_the_whole_fleet() -> None:
    fleet = {"nodes": [_node(NODE_A), _node(NODE_B)]}

    assert campaign_cli._qualification_lock_nodes(fleet, [NODE_A]) == [NODE_A, NODE_B]
    with pytest.raises(QualificationError, match="absent from current Fleet"):
        campaign_cli._qualification_lock_nodes(fleet, [NODE_A, "spk_" + "3" * 32])


def test_profile_campaign_lock_rejects_roster_change() -> None:
    fleet = {"nodes": [_node(NODE_A), _node(NODE_B), _node("spk_" + "3" * 32)]}

    with pytest.raises(QualificationError, match="Fleet membership changed"):
        campaign_cli._require_locked_fleet_roster(fleet, [NODE_A, NODE_B])
