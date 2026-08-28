from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Self

import pytest

from cluster_profiles.fleet_qualification import (
    Blocker,
    EvidenceLedger,
    OperationMonitor,
    QualificationError,
    QualificationRunner,
    RunnerOptions,
    ServiceSmokeAdapter,
    build_plan,
    legal_blockers,
    load_policy,
)
from cluster_profiles.qualification_fixtures import FixtureRegistry


class _Client:
    def __init__(self, responses: dict[tuple[str, str], Any]) -> None:
        self.responses = {
            key: list(value) if isinstance(value, list) else value
            for key, value in responses.items()
        }
        self.calls: list[tuple[str, str, object, object]] = []

    def request(
        self,
        method: str,
        path: str,
        payload: object = None,
        *,
        extra_headers: object = None,
        query: object = None,
    ) -> dict[str, object]:
        self.calls.append((method, path, payload, query))
        value = self.responses[(method, path)]
        if isinstance(value, list):
            value = value.pop(0)
        return dict(value)


def _fleet(count: int = 2) -> dict[str, object]:
    return {
        "authority_revision": "a" * 40,
        "event_cursor": 7,
        "nodes": [
            {
                "id": f"spk_{index:032x}",
                "connection": {"online_state": "online"},
                "inventory": {
                    "host_memory_free_bytes": 120_000_000_000,
                    "disk_free_bytes": 500_000_000_000,
                },
            }
            for index in range(1, count + 1)
        ],
    }


def _recipe(slug: str, *, nodes: int = 1, local: object = None) -> dict[str, object]:
    return {
        "publisher": "vonk",
        "slug": slug,
        "uri": "vonk+github://CarstVaartjes/vonk-forge-recipes/recipes/"
        + slug
        + ".json?ref="
        + "b" * 40
        + "&sha256="
        + "c" * 64,
        "content_sha256": "c" * 64,
        "release_version": "1.0.0",
        "node_count": nodes,
        "expected_download_bytes": 10,
        "maximum_installed_bytes_per_node": 20,
        "maximum_runtime_memory_bytes_per_node": 30,
        "execution_readiness": "executable",
        "execution_readiness_detail": "complete",
        "local": local or {"status": "not-imported"},
    }


def _role_disk(
    *,
    image: int = 0,
    artifacts: int = 0,
    staging: int = 0,
    cache: int = 0,
    rollback: int = 0,
    safety: int = 0,
) -> dict[str, dict[str, int]]:
    return {
        "solo": {
            "image_bytes": image,
            "artifact_bytes": artifacts,
            "staging_bytes": staging,
            "cache_bytes": cache,
            "rollback_bytes": rollback,
            "safety_margin_bytes": safety,
        }
    }


def _campaign_plan(
    recipes: list[dict[str, object]], options: RunnerOptions
) -> dict[str, object]:
    fixture_sha = FixtureRegistry.packaged().manifest_sha256
    intent_recipes = []
    for item in recipes:
        blockers = item.get("blockers", [])
        intent_recipes.append(
            {
                "key": item.get("key"),
                "uri": item.get("uri"),
                "content_sha256": item.get("content_sha256"),
                "release_version": item.get("release_version"),
                "node_count": item.get("node_count"),
                "immutable_blockers": [
                    blocker
                    for blocker in blockers
                    if blocker.get("code")
                    not in {
                        "topology.insufficient_online_nodes",
                        "resource.memory_exceeds_fleet",
                    }
                ],
            }
        )
    intent_recipes.sort(key=lambda item: str(item["key"]))
    intent = {
        "schema_version": 1,
        "catalog": {"repository": "test", "commit": "c" * 40},
        "controller_authority": {
            "authority_revision": "a" * 40,
            "node_ids": [],
        },
        "recipes": intent_recipes,
        "operator_policy": {},
        "options": {
            "jurisdiction": options.jurisdiction,
            "cleanup": options.cleanup,
            "selected_recipes": sorted(options.selected_recipes),
            "fixture_manifest_sha256": fixture_sha,
        },
    }
    digest = hashlib.sha256(
        json.dumps(intent, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "plan_digest": digest,
        "campaign_intent": intent,
        "catalog": intent["catalog"],
        "fleet": {"online_node_ids": []},
        "recipes": recipes,
    }


def test_ledger_is_hash_chained_durable_and_resumable(tmp_path: Path) -> None:
    path = tmp_path / "evidence.jsonl"
    ledger = EvidenceLedger(path)
    first = ledger.append(
        "plan.generated", plan_digest="d" * 64, payload={"catalog": "exact"}
    )
    second = ledger.append(
        "recipe.succeeded",
        plan_digest="d" * 64,
        recipe="vonk/tiny",
        payload={"revision": "r1"},
    )

    loaded = EvidenceLedger(path)

    assert first["previous_sha256"] == "0" * 64
    assert second["previous_sha256"] == first["record_sha256"]
    assert loaded.completed_recipes("d" * 64) == {"vonk/tiny"}
    assert path.stat().st_mode & 0o777 == 0o600


def test_ledger_rejects_tampering_and_partial_records(tmp_path: Path) -> None:
    path = tmp_path / "evidence.jsonl"
    EvidenceLedger(path).append("plan.generated", plan_digest="d" * 64)
    original = path.read_text(encoding="utf-8")
    path.write_text(
        original.replace("plan.generated", "plan.changed"), encoding="utf-8"
    )
    os.chmod(path, 0o600)
    with pytest.raises(QualificationError, match="integrity"):
        EvidenceLedger(path)

    path.write_text(original.rstrip("\n"), encoding="utf-8")
    os.chmod(path, 0o600)
    with pytest.raises(QualificationError, match="partial"):
        EvidenceLedger(path)

    path.write_text('{"sequence":1,"sequence":1}\n', encoding="utf-8")
    os.chmod(path, 0o600)
    with pytest.raises(QualificationError, match="invalid"):
        EvidenceLedger(path)


def test_plan_previews_import_and_classifies_wide_and_policy_blockers() -> None:
    supported = _recipe("tiny")
    wide = _recipe("eight", nodes=8)
    denied = _recipe("denied")
    client = _Client(
        {
            ("GET", "/api/v1/fleet"): _fleet(),
            ("GET", "/api/v1/catalog/public-recipes"): {
                "repository": "CarstVaartjes/vonk-forge-recipes",
                "commit": "b" * 40,
                "recipes": [supported, wide, denied],
            },
            ("POST", "/api/v1/catalog/imports/public/preview"): {
                **supported,
                "source": "recipe_library",
            },
        }
    )

    plan = build_plan(
        client,
        RunnerOptions(jurisdiction="NL"),
        {
            "vonk/denied": Blocker(
                "license", "operator.policy_block", "Non-commercial dependency"
            )
        },
    )

    recipes = {item["key"]: item for item in plan["recipes"]}
    assert recipes["vonk/tiny"]["planned_actions"][0] == "import"
    assert RunnerOptions().cleanup == "stop"
    assert "warm-redeploy-smoke" in recipes["vonk/tiny"]["planned_actions"]
    assert "retain-installation" in recipes["vonk/tiny"]["planned_actions"]
    assert recipes["vonk/eight"]["blockers"][0]["classification"] == "topology"
    assert recipes["vonk/denied"]["blockers"][0]["classification"] == "license"
    assert [call[:2] for call in client.calls].count(
        ("POST", "/api/v1/catalog/imports/public/preview")
    ) == 1
    assert all(
        call[:2] != ("POST", "/api/v1/catalog/imports/public") for call in client.calls
    )


def test_intent_digest_ignores_observed_fleet_drift_but_evidence_snapshot_changes() -> (
    None
):
    recipe = _recipe("tiny")

    def planned(generated_at: str, age: float, memory: int) -> dict[str, object]:
        fleet = _fleet(1)
        fleet["generated_at"] = generated_at
        node = fleet["nodes"][0]
        node["inventory"]["age_seconds"] = age
        node["inventory"]["host_memory_free_bytes"] = memory
        client = _Client(
            {
                ("GET", "/api/v1/fleet"): fleet,
                ("GET", "/api/v1/catalog/public-recipes"): {
                    "repository": "CarstVaartjes/vonk-forge-recipes",
                    "commit": "b" * 40,
                    "recipes": [recipe],
                },
                ("POST", "/api/v1/catalog/imports/public/preview"): recipe,
            }
        )
        return build_plan(client, RunnerOptions(jurisdiction="NL"), {})

    first = planned("2026-08-28T10:00:00Z", 1, 120_000_000_000)
    second = planned("2026-08-28T10:00:05Z", 6, 120_000_000_000)
    changed = planned("2026-08-28T10:00:05Z", 6, 119_000_000_000)

    assert first["plan_digest"] == second["plan_digest"]
    assert changed["plan_digest"] == first["plan_digest"]
    assert changed["fleet"]["snapshot_sha256"] != first["fleet"]["snapshot_sha256"]


def test_intent_digest_binds_catalog_authority_and_fixture_manifest() -> None:
    recipe = _recipe("tiny")

    def planned(*, commit: str, authority: str, fixture_sha: str) -> dict[str, object]:
        fleet = _fleet(1)
        fleet["authority_revision"] = authority
        client = _Client(
            {
                ("GET", "/api/v1/fleet"): fleet,
                ("GET", "/api/v1/catalog/public-recipes"): {
                    "repository": "CarstVaartjes/vonk-forge-recipes",
                    "commit": commit,
                    "recipes": [recipe],
                },
                ("POST", "/api/v1/catalog/imports/public/preview"): recipe,
            }
        )
        fixtures = FixtureRegistry({}, {}, {}, manifest_sha256=fixture_sha)
        return build_plan(client, RunnerOptions(jurisdiction="NL"), {}, fixtures)

    baseline = planned(commit="b" * 40, authority="a" * 40, fixture_sha="f" * 64)
    assert (
        planned(commit="c" * 40, authority="a" * 40, fixture_sha="f" * 64)[
            "plan_digest"
        ]
        != baseline["plan_digest"]
    )
    assert (
        planned(commit="b" * 40, authority="c" * 40, fixture_sha="f" * 64)[
            "plan_digest"
        ]
        != baseline["plan_digest"]
    )
    assert (
        planned(commit="b" * 40, authority="a" * 40, fixture_sha="e" * 64)[
            "plan_digest"
        ]
        != baseline["plan_digest"]
    )


@pytest.mark.parametrize(
    ("lane", "expected_code"),
    [
        ("artifact", "fixture.recipe_digest_mismatch"),
        ("service", "service_fixture.recipe_digest_mismatch"),
    ],
)
def test_plan_blocks_fixture_digest_drift_before_import(
    lane: str, expected_code: str
) -> None:
    fixtures = FixtureRegistry.packaged()
    key = (
        next(iter(fixtures.recipes))
        if lane == "artifact"
        else next(iter(fixtures.service_recipes))
    )
    publisher, slug = key.split("/", 1)
    recipe = _recipe(slug)
    recipe["publisher"] = publisher
    recipe["content_sha256"] = "0" * 64
    client = _Client(
        {
            ("GET", "/api/v1/fleet"): _fleet(1),
            ("GET", "/api/v1/catalog/public-recipes"): {
                "repository": "CarstVaartjes/vonk-forge-recipes",
                "commit": "b" * 40,
                "recipes": [recipe],
            },
        }
    )

    plan = build_plan(client, RunnerOptions(jurisdiction="NL"), {}, fixtures)

    assert plan["recipes"][0]["blockers"][0]["code"] == expected_code
    assert all("imports/public" not in call[1] for call in client.calls)


def test_apply_rejects_actionable_plan_tampering_and_option_drift(
    tmp_path: Path,
) -> None:
    options = RunnerOptions(jurisdiction="NL")
    item = {
        "key": "vonk/tiny",
        "uri": _recipe("tiny")["uri"],
        "content_sha256": "c" * 64,
        "release_version": "1.0.0",
        "node_count": 1,
        "blockers": [],
    }
    plan = _campaign_plan([item], options)
    tampered = json.loads(json.dumps(plan))
    tampered["recipes"][0]["content_sha256"] = "e" * 64

    with pytest.raises(QualificationError, match="actionable recipe rows"):
        QualificationRunner(
            _Client({}), EvidenceLedger(tmp_path / "tamper.jsonl"), options
        ).apply(tampered, str(plan["plan_digest"]))
    with pytest.raises(QualificationError, match="runner options"):
        QualificationRunner(
            _Client({}),
            EvidenceLedger(tmp_path / "options.jsonl"),
            RunnerOptions(jurisdiction="US"),
        ).apply(plan, str(plan["plan_digest"]))


def test_transient_blocker_is_retried_under_same_campaign_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = RunnerOptions()
    blocked_item = {
        "key": "vonk/tiny",
        "uri": _recipe("tiny")["uri"],
        "content_sha256": "c" * 64,
        "release_version": "1.0.0",
        "node_count": 1,
        "blockers": [
            {
                "classification": "topology",
                "code": "topology.insufficient_online_nodes",
                "detail": "one Spark is temporarily offline",
            }
        ],
    }
    first = _campaign_plan([blocked_item], options)
    eligible_item = {**blocked_item, "blockers": []}
    second = _campaign_plan([eligible_item], options)
    assert first["plan_digest"] == second["plan_digest"]
    client = _Client(
        {
            ("GET", "/api/v1/library"): {
                "models": [],
                "unlinked_recipes": [],
                "next_cursor": None,
            },
            ("GET", "/api/v1/fleet"): _fleet(1),
        }
    )
    ledger = EvidenceLedger(tmp_path / "evidence.jsonl")
    runner = QualificationRunner(client, ledger, options)
    runner.apply(first, str(first["plan_digest"]))
    executed: list[str] = []
    monkeypatch.setattr(runner, "_prepare_capacity_campaign", lambda *_args: None)
    monkeypatch.setattr(
        runner,
        "_apply_recipe",
        lambda _digest, item: (
            executed.append(str(item["key"])),
            ledger.append(
                "recipe.succeeded",
                plan_digest=str(second["plan_digest"]),
                recipe=str(item["key"]),
            ),
        ),
    )
    runner.apply(second, str(second["plan_digest"]))

    assert executed == ["vonk/tiny"]


def test_storage_capacity_is_previewed_and_never_auto_evicts(tmp_path: Path) -> None:
    node_id = "spk_" + "1" * 32
    fleet = _fleet(1)
    fleet["nodes"][0]["id"] = node_id
    client = _Client({("GET", "/api/v1/fleet"): fleet})
    ledger = EvidenceLedger(tmp_path / "evidence.jsonl")
    runner = QualificationRunner(client, ledger, RunnerOptions())

    runner._prove_storage_capacity(
        "d" * 64,
        "vonk/tiny",
        {
            "maximum_installed_bytes_per_node": 20,
            "expected_download_bytes": 10,
            "temporary_build_bytes_per_node": 5,
        },
        [node_id],
    )

    plan = ledger.records[-1]["payload"]["plan"]
    assert plan["required_bytes_per_node"] == 25
    assert plan["automatic_eviction"] is False
    assert plan["disposition"] == "fits"


def test_global_capacity_backtracking_finds_order_independent_balanced_plan(
    tmp_path: Path,
) -> None:
    node_ids = ["spk_" + "1" * 32, "spk_" + "2" * 32]
    fleet = _fleet(2)
    for index, node in enumerate(fleet["nodes"]):
        node["id"] = node_ids[index]
        node["inventory"]["disk_free_bytes"] = 10_000_000_010
        node["reservations"] = {"disk_bytes": 0}
    sizes = {"a": 3, "b": 3, "c": 3, "d": 3, "e": 5}
    temporary = {"a": 0, "b": 1, "c": 2, "d": 3, "e": 0}
    responses: dict[tuple[str, str], object] = {("GET", "/api/v1/fleet"): fleet}
    items = []
    for index, (slug, size) in enumerate(sizes.items(), start=1):
        recipe_id = f"00000000-0000-4000-8000-{index:012d}"
        revision_id = f"10000000-0000-4000-8000-{index:012d}"
        items.append(
            {
                "key": f"vonk/{slug}",
                "content_sha256": f"{index:064x}",
                "node_count": 1,
                "blockers": [],
                "local_recipe_id": recipe_id,
                "local_revision_id": revision_id,
                "maximum_installed_bytes_per_node": size,
                "expected_download_bytes": size,
                "temporary_build_bytes_per_node": temporary[slug],
                "disk_requirements_by_role": _role_disk(image=size),
                "artifact_identities": [],
            }
        )
        responses[("GET", f"/api/v1/library/recipes/{recipe_id}")] = {
            "selected_revision": {
                "id": revision_id,
                "content_sha256": f"{index:064x}",
            },
            "visual_recipe": {},
            "operational_state": {"installations": []},
            "placement": [
                {
                    "recommendations": [
                        {
                            "eligible": True,
                            "node_ids": [node_id],
                            "nodes": [{"node_id": node_id, "role": "solo"}],
                            "installation_ids": [],
                        }
                        for node_id in node_ids
                    ]
                }
            ],
        }
    runner = QualificationRunner(
        _Client(responses),
        EvidenceLedger(tmp_path / "capacity.jsonl"),
        RunnerOptions(),
    )

    runner._prepare_capacity_campaign("d" * 64, list(reversed(items)), set())

    assert runner._capacity_execution_order == [
        "vonk/d",
        "vonk/c",
        "vonk/b",
        "vonk/e",
        "vonk/a",
    ]
    d_assignment = runner._capacity_assignments["vonk/d"]
    assert set(d_assignment["planned_available_before_by_node"]) == set(
        d_assignment["node_ids"]
    )
    assert all(
        value == 10_000_000_000
        for value in d_assignment["safety_floor_bytes_by_node"].values()
    )
    assert all(value == 0 for value in d_assignment["staging_bytes_by_node"].values())
    loads = {node_id: 0 for node_id in node_ids}
    for assignment in runner._capacity_assignments.values():
        for node_id, value in assignment["persistent_bytes_by_node"].items():
            loads[node_id] += value
    assert sorted(loads.values()) == [8, 9]
    assert all(value <= 10 for value in loads.values())

    resumed = QualificationRunner(_Client(responses), runner.ledger, RunnerOptions())
    resumed._prepare_capacity_campaign("d" * 64, items, {"vonk/a"})
    assert resumed._capacity_assignments == runner._capacity_assignments
    assert (
        sum(row["event"] == "capacity.plan.created" for row in runner.ledger.records)
        == 1
    )

    drifted_fleet = json.loads(json.dumps(fleet))
    for node in drifted_fleet["nodes"]:
        node["inventory"]["disk_free_bytes"] += 1
    drifted_responses = dict(responses)
    drifted_responses[("GET", "/api/v1/fleet")] = drifted_fleet
    drifted = QualificationRunner(
        _Client(drifted_responses), runner.ledger, RunnerOptions()
    )
    drifted._prepare_capacity_campaign("d" * 64, items, set())
    assert any(
        row["event"] == "capacity.plan.invalidated" for row in runner.ledger.records
    )
    assert (
        sum(row["event"] == "capacity.plan.created" for row in runner.ledger.records)
        == 2
    )

    full_fleet = json.loads(json.dumps(fleet))
    for node in full_fleet["nodes"]:
        node["inventory"]["disk_free_bytes"] = 0
    resident_detail = json.loads(
        json.dumps(
            responses[
                ("GET", "/api/v1/library/recipes/00000000-0000-4000-8000-000000000001")
            ]
        )
    )
    assigned_nodes = resumed._capacity_assignments["vonk/a"]["node_ids"]
    for candidate in resident_detail["placement"][0]["recommendations"]:
        if candidate["node_ids"] == assigned_nodes:
            candidate["installation_ids"] = ["installation-a"]
    resident_runner = QualificationRunner(
        _Client({("GET", "/api/v1/fleet"): full_fleet}),
        EvidenceLedger(tmp_path / "resident-capacity.jsonl"),
        RunnerOptions(),
    )
    resident_assignments = json.loads(json.dumps(resumed._capacity_assignments))
    resident_assignment = resident_assignments["vonk/a"]
    resident_assignment["installation_ids"] = ["installation-a"]
    resident_assignment["preexisting_installation"] = True
    resident_assignment["candidate_signature"]["installation_ids"] = ["installation-a"]
    resident_runner._capacity_assignments = resident_assignments
    selected = resident_runner._select_placement(
        "d" * 64, "vonk/a", resident_detail, items[0]
    )
    assert selected["installation_ids"] == ["installation-a"]
    checked = next(
        row
        for row in resident_runner.ledger.records
        if row["event"] == "capacity.checked"
    )
    assert all(
        node["peak_required_bytes"] == 0 and node["fits"] is True
        for node in checked["payload"]["nodes"].values()
    )

    drift_fleet = json.loads(json.dumps(fleet))
    assignment = resumed._capacity_assignments["vonk/a"]
    planned_before = assignment["planned_available_before_by_node"]
    for node in drift_fleet["nodes"]:
        if node["id"] in planned_before:
            node["inventory"]["disk_free_bytes"] = planned_before[node["id"]] - 1
    drift_detail = json.loads(
        json.dumps(
            responses[
                (
                    "GET",
                    "/api/v1/library/recipes/00000000-0000-4000-8000-000000000001",
                )
            ]
        )
    )
    runtime_drift = QualificationRunner(
        _Client({("GET", "/api/v1/fleet"): drift_fleet}),
        EvidenceLedger(tmp_path / "runtime-capacity-drift.jsonl"),
        RunnerOptions(),
    )
    runtime_drift._capacity_assignments = resumed._capacity_assignments
    with pytest.raises(QualificationError, match="assignment drifted"):
        runtime_drift._select_placement("d" * 64, "vonk/a", drift_detail, items[0])
    drift_check = next(
        row
        for row in runtime_drift.ledger.records
        if row["event"] == "capacity.checked"
    )
    assert any(
        node["baseline_preserved"] is False and node["fits"] is False
        for node in drift_check["payload"]["nodes"].values()
    )

    constrained = json.loads(json.dumps(fleet))
    for node in constrained["nodes"]:
        node["inventory"]["disk_free_bytes"] = 10_000_000_002
    blocked_responses = dict(responses)
    blocked_responses[("GET", "/api/v1/fleet")] = constrained
    blocked_ledger = EvidenceLedger(tmp_path / "blocked-capacity.jsonl")
    blocked = QualificationRunner(
        _Client(blocked_responses), blocked_ledger, RunnerOptions()
    )
    blocked._prepare_capacity_campaign("e" * 64, items[:1], set())
    assert blocked._preflight_blocked == {"vonk/a"}
    capacity_block = next(
        row for row in blocked_ledger.records if row["event"] == "capacity.blocked"
    )
    alternatives = capacity_block["payload"]["candidate_alternatives"]["vonk/a"]
    assert alternatives
    assert all(
        max(candidate["shortfall_bytes_by_node"].values()) == 1
        for candidate in alternatives
    )
    assert any(
        row["event"] == "recipe.blocked" and row["recipe"] == "vonk/a"
        for row in blocked_ledger.records
    )


def test_apply_honors_persisted_capacity_execution_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = RunnerOptions()
    original_order = ["low-temp", "high-temp"]
    items = [
        {
            "key": f"vonk/{slug}",
            "uri": _recipe(slug)["uri"],
            "content_sha256": f"{index:064x}",
            "release_version": "1.0.0",
            "node_count": 1,
            "blockers": [],
        }
        for index, slug in enumerate(original_order, start=1)
    ]
    plan = _campaign_plan(items, options)
    client = _Client(
        {
            ("GET", "/api/v1/library"): {
                "models": [],
                "unlinked_recipes": [],
                "next_cursor": None,
            },
            ("GET", "/api/v1/fleet"): _fleet(1),
        }
    )
    ledger = EvidenceLedger(tmp_path / "ordered-apply.jsonl")
    runner = QualificationRunner(client, ledger, options)
    executed: list[str] = []

    def prepare(*_args: object) -> None:
        runner._capacity_execution_order = ["vonk/high-temp", "vonk/low-temp"]

    def apply_recipe(digest: str, item: Mapping[str, object]) -> None:
        key = str(item["key"])
        executed.append(key)
        ledger.append("recipe.succeeded", plan_digest=digest, recipe=key)

    monkeypatch.setattr(runner, "_prepare_capacity_campaign", prepare)
    monkeypatch.setattr(runner, "_apply_recipe", apply_recipe)

    runner.apply(plan, str(plan["plan_digest"]))

    assert executed == ["vonk/high-temp", "vonk/low-temp"]


def test_capacity_plan_binds_staging_build_and_controller_safety_floor(
    tmp_path: Path,
) -> None:
    node_id = "spk_" + "1" * 32
    fleet = _fleet(1)
    fleet["nodes"][0]["id"] = node_id
    fleet["nodes"][0]["inventory"]["disk_free_bytes"] = 13_000_000_100
    recipe_id = "00000000-0000-4000-8000-000000000101"
    revision_id = "10000000-0000-4000-8000-000000000101"
    item = {
        "key": "vonk/staging",
        "content_sha256": "1" * 64,
        "node_count": 1,
        "blockers": [],
        "local_recipe_id": recipe_id,
        "local_revision_id": revision_id,
        "maximum_installed_bytes_per_node": 40,
        "expected_download_bytes": 20,
        "temporary_build_bytes_per_node": 7,
        "disk_requirements_by_role": _role_disk(
            image=10,
            artifacts=20,
            staging=30,
            cache=5,
            rollback=5,
            safety=12_000_000_000,
        ),
        "artifact_identities": [
            {
                "identity_sha256": "a" * 64,
                "download_bytes": 20,
                "installed_bytes": 20,
                "roles": ["solo"],
            }
        ],
    }
    detail = {
        "selected_revision": {
            "id": revision_id,
            "content_sha256": "1" * 64,
        },
        "visual_recipe": {},
        "operational_state": {"installations": []},
        "placement": [
            {
                "recommendations": [
                    {
                        "eligible": True,
                        "node_ids": [node_id],
                        "nodes": [{"node_id": node_id, "role": "solo"}],
                        "installation_ids": [],
                    }
                ]
            }
        ],
    }
    runner = QualificationRunner(
        _Client(
            {
                ("GET", "/api/v1/fleet"): fleet,
                ("GET", f"/api/v1/library/recipes/{recipe_id}"): detail,
            }
        ),
        EvidenceLedger(tmp_path / "staging-capacity.jsonl"),
        RunnerOptions(),
    )

    runner._prepare_capacity_campaign("d" * 64, [item], set())

    assignment = runner._capacity_assignments["vonk/staging"]
    assert assignment["persistent_bytes_by_node"] == {node_id: 40}
    assert assignment["staging_bytes_by_node"] == {node_id: 30}
    assert assignment["safety_floor_bytes_by_node"] == {node_id: 12_000_000_000}
    assert assignment["peak_bytes_by_node"] == {node_id: 12_000_000_077}


def test_capacity_search_explores_recipe_order_for_transient_peaks(
    tmp_path: Path,
) -> None:
    node_id = "spk_" + "1" * 32
    fleet = _fleet(1)
    fleet["nodes"][0]["id"] = node_id
    fleet["nodes"][0]["inventory"]["disk_free_bytes"] = 10_000_000_150
    responses: dict[tuple[str, str], object] = {("GET", "/api/v1/fleet"): fleet}
    items = []
    for index, (slug, persistent, temporary) in enumerate(
        (("a", 100, 0), ("b", 1, 100)), start=1
    ):
        recipe_id = f"00000000-0000-4000-8000-{index:012d}"
        revision_id = f"10000000-0000-4000-8000-{index:012d}"
        items.append(
            {
                "key": f"vonk/{slug}",
                "content_sha256": f"{index:064x}",
                "node_count": 1,
                "blockers": [],
                "local_recipe_id": recipe_id,
                "local_revision_id": revision_id,
                "maximum_installed_bytes_per_node": persistent,
                "expected_download_bytes": persistent,
                "temporary_build_bytes_per_node": temporary,
                "disk_requirements_by_role": _role_disk(image=persistent),
                "artifact_identities": [],
            }
        )
        responses[("GET", f"/api/v1/library/recipes/{recipe_id}")] = {
            "selected_revision": {
                "id": revision_id,
                "content_sha256": f"{index:064x}",
            },
            "visual_recipe": {},
            "operational_state": {"installations": []},
            "placement": [
                {
                    "recommendations": [
                        {
                            "eligible": True,
                            "node_ids": [node_id],
                            "nodes": [{"node_id": node_id, "role": "solo", "rank": 0}],
                            "installation_ids": [],
                        }
                    ]
                }
            ],
        }
    runner = QualificationRunner(
        _Client(responses),
        EvidenceLedger(tmp_path / "order-search.jsonl"),
        RunnerOptions(),
    )

    runner._prepare_capacity_campaign("d" * 64, items, set())

    assert runner._capacity_execution_order == ["vonk/b", "vonk/a"]
    assert set(runner._capacity_assignments) == {"vonk/a", "vonk/b"}


def test_dual_capacity_charges_build_temp_to_rank_zero_builder(
    tmp_path: Path,
) -> None:
    high_node = "spk_" + "1" * 32
    builder_node = "spk_" + "f" * 32
    fleet = _fleet(2)
    fleet["nodes"][0]["id"] = high_node
    fleet["nodes"][0]["inventory"]["disk_free_bytes"] = 10_000_000_200
    fleet["nodes"][1]["id"] = builder_node
    fleet["nodes"][1]["inventory"]["disk_free_bytes"] = 10_000_000_050
    recipe_id = "00000000-0000-4000-8000-000000000201"
    revision_id = "10000000-0000-4000-8000-000000000201"
    item = {
        "key": "vonk/dual-builder",
        "content_sha256": "2" * 64,
        "node_count": 2,
        "blockers": [],
        "local_recipe_id": recipe_id,
        "local_revision_id": revision_id,
        "maximum_installed_bytes_per_node": 1,
        "expected_download_bytes": 1,
        "temporary_build_bytes_per_node": 100,
        "disk_requirements_by_role": {
            "entrypoint": _role_disk()["solo"],
            "worker": _role_disk()["solo"],
        },
        "artifact_identities": [],
    }
    detail = {
        "selected_revision": {
            "id": revision_id,
            "content_sha256": "2" * 64,
        },
        "visual_recipe": {},
        "operational_state": {"installations": []},
        "placement": [
            {
                "recommendations": [
                    {
                        "eligible": True,
                        "node_ids": [builder_node, high_node],
                        "nodes": [
                            {
                                "node_id": builder_node,
                                "role": "entrypoint",
                                "rank": 0,
                            },
                            {"node_id": high_node, "role": "worker", "rank": 1},
                        ],
                        "installation_ids": [],
                    }
                ]
            }
        ],
    }
    runner = QualificationRunner(
        _Client(
            {
                ("GET", "/api/v1/fleet"): fleet,
                ("GET", f"/api/v1/library/recipes/{recipe_id}"): detail,
            }
        ),
        EvidenceLedger(tmp_path / "dual-builder.jsonl"),
        RunnerOptions(),
    )

    runner._prepare_capacity_campaign("d" * 64, [item], set())

    assert runner._preflight_blocked == {"vonk/dual-builder"}
    assert runner._capacity_assignments == {}


def test_failed_capacity_provider_blocks_dependent_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = RunnerOptions()
    items = [
        {
            "key": f"vonk/{slug}",
            "uri": _recipe(slug)["uri"],
            "content_sha256": f"{index:064x}",
            "release_version": "1.0.0",
            "node_count": 1,
            "blockers": [],
        }
        for index, slug in enumerate(("provider", "consumer"), start=1)
    ]
    plan = _campaign_plan(items, options)
    client = _Client(
        {
            ("GET", "/api/v1/library"): {
                "models": [],
                "unlinked_recipes": [],
                "next_cursor": None,
            },
            ("GET", "/api/v1/fleet"): _fleet(1),
        }
    )
    ledger = EvidenceLedger(tmp_path / "provider-block.jsonl")
    runner = QualificationRunner(client, ledger, options)
    invoked: list[str] = []
    provider = "vonk/provider"
    consumer = "vonk/consumer"
    node_id = "spk_" + f"{1:032x}"

    def prepare(*_args: object) -> None:
        runner._capacity_execution_order = [provider, consumer]
        runner._capacity_assignments = {
            provider: {
                "preexisting_installation": False,
                "artifact_provider_recipes_by_node": {node_id: {}},
            },
            consumer: {
                "preexisting_installation": False,
                "artifact_provider_recipes_by_node": {node_id: {"a" * 64: provider}},
            },
        }

    def apply_recipe(digest: str, item: Mapping[str, object]) -> None:
        key = str(item["key"])
        invoked.append(key)
        if key == provider:
            ledger.append(
                "recipe.blocked",
                plan_digest=digest,
                recipe=key,
                payload={
                    "blockers": [
                        {
                            "classification": "resource",
                            "code": "resource.capacity_drift",
                        }
                    ]
                },
            )
            raise QualificationError("provider drifted")

    monkeypatch.setattr(runner, "_prepare_capacity_campaign", prepare)
    monkeypatch.setattr(runner, "_apply_recipe", apply_recipe)

    result = runner.apply(plan, str(plan["plan_digest"]))

    assert invoked == [provider]
    assert result["blocked"] == 2
    consumer_block = next(
        row
        for row in ledger.records
        if row.get("event") == "recipe.blocked" and row.get("recipe") == consumer
    )
    assert (
        consumer_block["payload"]["blockers"][0]["code"]
        == "resource.capacity_provider_unavailable"
    )


def test_capacity_resume_replans_when_dedup_provider_becomes_ineligible(
    tmp_path: Path,
) -> None:
    node_id = "spk_" + "1" * 32
    fleet = _fleet(1)
    fleet["nodes"][0]["id"] = node_id
    fleet["nodes"][0]["inventory"]["disk_free_bytes"] = 10_000_000_100
    artifact = {
        "identity_sha256": "a" * 64,
        "download_bytes": 10,
        "installed_bytes": 20,
        "roles": ["solo"],
    }
    items: list[dict[str, object]] = []
    responses: dict[tuple[str, str], object] = {("GET", "/api/v1/fleet"): fleet}
    for index, slug in enumerate(("a-provider", "z-consumer"), start=1):
        recipe_id = f"00000000-0000-4000-8000-{index:012d}"
        revision_id = f"10000000-0000-4000-8000-{index:012d}"
        item = {
            "key": f"vonk/{slug}",
            "content_sha256": f"{index:064x}",
            "node_count": 1,
            "blockers": [],
            "local_recipe_id": recipe_id,
            "local_revision_id": revision_id,
            "maximum_installed_bytes_per_node": 20,
            "expected_download_bytes": 10,
            "temporary_build_bytes_per_node": 0,
            "disk_requirements_by_role": _role_disk(artifacts=20),
            "artifact_identities": [dict(artifact)],
        }
        items.append(item)
        responses[("GET", f"/api/v1/library/recipes/{recipe_id}")] = {
            "selected_revision": {
                "id": revision_id,
                "content_sha256": f"{index:064x}",
            },
            "visual_recipe": {},
            "operational_state": {"installations": []},
            "placement": [
                {
                    "recommendations": [
                        {
                            "eligible": True,
                            "node_ids": [node_id],
                            "nodes": [{"node_id": node_id, "role": "solo"}],
                            "installation_ids": [],
                        }
                    ]
                }
            ],
        }
    ledger = EvidenceLedger(tmp_path / "provider-resume.jsonl")
    initial = QualificationRunner(_Client(responses), ledger, RunnerOptions())

    initial._prepare_capacity_campaign("d" * 64, items, set())

    consumer_assignment = initial._capacity_assignments["vonk/z-consumer"]
    assert consumer_assignment["persistent_bytes_by_node"] == {node_id: 0}
    assert consumer_assignment["artifact_provider_recipes_by_node"] == {
        node_id: {"a" * 64: "vonk/a-provider"}
    }

    changed = dict(responses)
    provider_path = (
        "GET",
        "/api/v1/library/recipes/00000000-0000-4000-8000-000000000001",
    )
    provider_detail = json.loads(json.dumps(changed[provider_path]))
    provider_detail["placement"][0]["recommendations"][0]["eligible"] = False
    changed[provider_path] = provider_detail
    resumed = QualificationRunner(_Client(changed), ledger, RunnerOptions())

    resumed._prepare_capacity_campaign("d" * 64, items, set())

    assert resumed._preflight_blocked == {"vonk/a-provider"}
    assert resumed._capacity_assignments["vonk/z-consumer"][
        "persistent_bytes_by_node"
    ] == {node_id: 20}
    assert resumed._capacity_assignments["vonk/z-consumer"][
        "artifact_provider_recipes_by_node"
    ] == {node_id: {}}
    assert any(row["event"] == "capacity.plan.invalidated" for row in ledger.records)


def test_runtime_ineligible_planned_placement_is_structured_resource_blocker(
    tmp_path: Path,
) -> None:
    planned_node = "spk_" + "1" * 32
    alternate_node = "spk_" + "2" * 32
    ledger = EvidenceLedger(tmp_path / "runtime-ineligible.jsonl")
    runner = QualificationRunner(_Client({}), ledger, RunnerOptions())
    runner._capacity_assignments = {
        "vonk/drifted": {
            "node_ids": [planned_node],
            "builder_node_id": planned_node,
            "installation_ids": [],
            "candidate_signature": {
                "node_ids": [planned_node],
                "builder_node_id": planned_node,
                "installation_ids": [],
                "role_by_node": {planned_node: "solo"},
                "rank_by_node": {planned_node: 0},
            },
            "planned_available_before_by_node": {planned_node: 100},
            "persistent_bytes_by_node": {planned_node: 1},
            "peak_bytes_by_node": {planned_node: 1},
            "artifact_provider_recipes_by_node": {planned_node: {}},
        }
    }
    detail = {
        "placement": [
            {
                "recommendations": [
                    {
                        "eligible": True,
                        "node_ids": [alternate_node],
                        "nodes": [{"node_id": alternate_node, "role": "solo"}],
                    }
                ]
            }
        ]
    }

    with pytest.raises(QualificationError, match="no longer eligible"):
        runner._select_placement(
            "d" * 64,
            "vonk/drifted",
            detail,
            {"maximum_installed_bytes_per_node": 1},
        )

    blocker = ledger.records[-1]["payload"]["blockers"][0]
    assert blocker["classification"] == "resource"
    assert blocker["code"] == "resource.planned_placement_ineligible"
    assert blocker["planned_node_ids"] == [planned_node]


def test_apply_continues_after_recipe_failure_but_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = RunnerOptions()
    items = [
        {
            "key": f"vonk/{slug}",
            "uri": _recipe(slug)["uri"],
            "content_sha256": f"{index:064x}",
            "release_version": "1.0.0",
            "node_count": 1,
            "blockers": [],
        }
        for index, slug in enumerate(("broken", "healthy"), start=1)
    ]
    plan = _campaign_plan(items, options)
    ledger = EvidenceLedger(tmp_path / "continue-failure.jsonl")
    runner = QualificationRunner(
        _Client(
            {
                ("GET", "/api/v1/library"): {
                    "models": [],
                    "unlinked_recipes": [],
                    "next_cursor": None,
                },
                ("GET", "/api/v1/fleet"): _fleet(1),
            }
        ),
        ledger,
        options,
    )
    invoked: list[str] = []

    def prepare(*_args: object) -> None:
        runner._capacity_execution_order = ["vonk/broken", "vonk/healthy"]

    def apply_recipe(digest: str, item: Mapping[str, object]) -> None:
        key = str(item["key"])
        invoked.append(key)
        if key == "vonk/broken":
            raise RuntimeError("synthetic adapter failure")
        ledger.append("recipe.succeeded", plan_digest=digest, recipe=key)

    monkeypatch.setattr(runner, "_prepare_capacity_campaign", prepare)
    monkeypatch.setattr(runner, "_apply_recipe", apply_recipe)

    with pytest.raises(QualificationError, match="completed with 1 failed"):
        runner.apply(plan, str(plan["plan_digest"]))

    assert invoked == ["vonk/broken", "vonk/healthy"]
    terminal = next(
        row for row in ledger.records if row["event"] == "run.completed-with-failures"
    )
    assert terminal["payload"]["failed"] == 1
    assert terminal["payload"]["succeeded"] == 1
    assert any(row["event"] == "run.residency-inventoried" for row in ledger.records)


def test_apply_isolates_import_preparation_failure_and_runs_healthy_recipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = RunnerOptions()
    node_id = "spk_" + "1" * 32
    healthy_recipe_id = "00000000-0000-4000-8000-000000000301"
    healthy_revision_id = "10000000-0000-4000-8000-000000000301"
    items = []
    for index, slug in enumerate(("broken-import", "healthy"), start=1):
        items.append(
            {
                "key": f"vonk/{slug}",
                "uri": _recipe(slug)["uri"],
                "content_sha256": f"{index:064x}",
                "release_version": "1.0.0",
                "node_count": 1,
                "blockers": [],
                "maximum_installed_bytes_per_node": 1,
                "expected_download_bytes": 1,
                "temporary_build_bytes_per_node": 0,
                "disk_requirements_by_role": _role_disk(image=1),
                "artifact_identities": [],
            }
        )
    plan = _campaign_plan(items, options)
    fleet = _fleet(1)
    fleet["nodes"][0]["id"] = node_id
    detail = {
        "selected_revision": {
            "id": healthy_revision_id,
            "content_sha256": f"{2:064x}",
        },
        "visual_recipe": {},
        "operational_state": {"installations": []},
        "placement": [
            {
                "recommendations": [
                    {
                        "eligible": True,
                        "node_ids": [node_id],
                        "nodes": [{"node_id": node_id, "role": "solo", "rank": 0}],
                        "installation_ids": [],
                    }
                ]
            }
        ],
    }
    ledger = EvidenceLedger(tmp_path / "prepare-isolation.jsonl")
    runner = QualificationRunner(
        _Client(
            {
                ("GET", f"/api/v1/library/recipes/{healthy_recipe_id}"): detail,
                ("GET", "/api/v1/fleet"): fleet,
                ("GET", "/api/v1/library"): {
                    "models": [],
                    "unlinked_recipes": [],
                    "next_cursor": None,
                },
            }
        ),
        ledger,
        options,
    )
    invoked: list[str] = []

    def ensure_import(
        _digest_value: str, item: Mapping[str, object]
    ) -> tuple[str, str]:
        if item["key"] == "vonk/broken-import":
            raise RuntimeError("catalog import unavailable")
        return healthy_recipe_id, healthy_revision_id

    def apply_recipe(digest: str, item: Mapping[str, object]) -> None:
        invoked.append(str(item["key"]))
        ledger.append(
            "recipe.succeeded",
            plan_digest=digest,
            recipe=str(item["key"]),
        )

    monkeypatch.setattr(runner, "_ensure_import", ensure_import)
    monkeypatch.setattr(runner, "_apply_recipe", apply_recipe)

    with pytest.raises(QualificationError, match="completed with 1 failed"):
        runner.apply(plan, str(plan["plan_digest"]))

    assert invoked == ["vonk/healthy"]
    failed = next(
        row
        for row in ledger.records
        if row["event"] == "recipe.failed" and row.get("recipe") == "vonk/broken-import"
    )
    assert failed["payload"]["phase"] == "capacity-preparation"


def test_plan_classifies_definite_memory_blocker_without_importing() -> None:
    recipe = _recipe("too-large")
    recipe["maximum_runtime_memory_bytes_per_node"] = 130_000_000_000
    client = _Client(
        {
            ("GET", "/api/v1/fleet"): _fleet(),
            ("GET", "/api/v1/catalog/public-recipes"): {
                "repository": "CarstVaartjes/vonk-forge-recipes",
                "commit": "b" * 40,
                "recipes": [recipe],
            },
        }
    )

    plan = build_plan(client, RunnerOptions(jurisdiction="NL"), {})

    assert plan["recipes"][0]["blockers"][0]["code"] == "resource.memory_exceeds_fleet"
    assert all("imports/public" not in call[1] for call in client.calls)


def test_restricted_license_fails_closed_and_understands_eu_membership() -> None:
    recipe = {
        "model_license": {
            "territorial_restrictions": {
                "denied_jurisdictions": ["EU", "GB", "KR"],
                "notice": "Not licensed in the denied territories.",
            }
        }
    }

    assert legal_blockers(recipe, None)[0].code == "license.jurisdiction_required"
    assert legal_blockers(recipe, "NL")[0].code == "license.territory_denied"
    assert legal_blockers(recipe, "US") == []


def test_plan_reads_current_library_detail_for_revision_and_license() -> None:
    recipe = _recipe(
        "restricted",
        local={
            "status": "current",
            "recipe_id": "00000000-0000-4000-8000-000000000001",
            "revision_number": 2,
            "content_sha256": "c" * 64,
        },
    )
    client = _Client(
        {
            ("GET", "/api/v1/fleet"): _fleet(),
            ("GET", "/api/v1/catalog/public-recipes"): {
                "repository": "CarstVaartjes/vonk-forge-recipes",
                "commit": "b" * 40,
                "recipes": [recipe],
            },
            (
                "GET",
                "/api/v1/library/recipes/00000000-0000-4000-8000-000000000001",
            ): {
                "selected_revision": {
                    "id": "00000000-0000-4000-8000-000000000002",
                    "content_sha256": "c" * 64,
                },
                "visual_recipe": {
                    "model_license": {
                        "territorial_restrictions": {
                            "denied_jurisdictions": ["EU"],
                            "notice": "Excluded territory",
                        }
                    }
                },
            },
        }
    )

    plan = build_plan(client, RunnerOptions(jurisdiction="NL"), {})
    item = plan["recipes"][0]

    assert item["local_revision_id"] == "00000000-0000-4000-8000-000000000002"
    assert item["blockers"][0]["code"] == "license.territory_denied"
    assert all(
        call[:2] != ("POST", "/api/v1/catalog/imports/public") for call in client.calls
    )


def test_policy_is_additive_and_strict(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "blocked_recipes": {
                    "vonk/tiny": {
                        "classification": "manual",
                        "detail": "Hold for review",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert load_policy(path)["vonk/tiny"].detail == "Hold for review"

    path.write_text(
        '{"schema_version":1,"blocked_recipes":{"bad":{"classification":"manual","detail":"x"}}}',
        encoding="utf-8",
    )
    with pytest.raises(QualificationError, match="publisher/slug"):
        load_policy(path)

    path.write_text(
        '{"schema_version":1,"schema_version":1,"blocked_recipes":{}}',
        encoding="utf-8",
    )
    with pytest.raises(QualificationError, match="unreadable"):
        load_policy(path)


def test_operation_monitor_resumes_submitted_operation_without_resubmitting(
    tmp_path: Path,
) -> None:
    ledger = EvidenceLedger(tmp_path / "evidence.jsonl")
    digest = "d" * 64
    ledger.append(
        "operation.submitted",
        plan_digest=digest,
        recipe="vonk/tiny",
        payload={"step": "install", "operation": {"id": "operation-1"}},
    )
    client = _Client(
        {
            ("GET", "/api/v1/recipes/operations/operation-1"): {
                "id": "operation-1",
                "state": "succeeded",
                "owner_id": "installation-1",
            }
        }
    )
    options = RunnerOptions(poll_interval_seconds=0.1)
    runner = QualificationRunner(
        client,
        ledger,
        options,
        monitor=OperationMonitor(client, ledger, options),
    )

    result = runner._operation(
        digest, "vonk/tiny", "install", "/must-not-be-called", {"unsafe": True}
    )

    assert result["owner_id"] == "installation-1"
    assert client.calls == [
        ("GET", "/api/v1/recipes/operations/operation-1", None, None)
    ]


class _Response:
    def __init__(self, value: object) -> None:
        self.raw = json.dumps(value).encode()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self.raw[:limit]


class _FailingServiceSmoke:
    fixtures = FixtureRegistry.packaged()

    def preview(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {"available": True, "kind": "openai-service"}

    def run(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("original smoke failure")


class _PreviewFailingServiceSmoke(_FailingServiceSmoke):
    def preview(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("preview smoke failure")


class _CleanupFailureClient(_Client):
    def request(
        self,
        method: str,
        path: str,
        payload: object = None,
        *,
        extra_headers: object = None,
        query: object = None,
    ) -> dict[str, object]:
        if path == "/api/v1/recipes/stop-plans/preview":
            self.calls.append((method, path, payload, query))
            raise RuntimeError("cleanup failure")
        return super().request(
            method,
            path,
            payload,
            extra_headers=extra_headers,
            query=query,
        )


def test_service_smoke_uses_published_https_route_and_records_digest() -> None:
    requests: list[object] = []

    def opener(request: object, *, timeout: float) -> _Response:
        requests.append((request, timeout))
        return _Response({"choices": [{"message": {"content": "OK"}}]})

    client = _Client(
        {
            ("GET", "/api/v1/endpoints/qual-tiny"): {
                "alias": "qual-tiny",
                "api_base": "https://models.example.test/v1",
                "node_id": "spk_" + "1" * 32,
                "plan_digest": "e" * 64,
            }
        }
    )
    adapter = ServiceSmokeAdapter(opener=opener)
    preview = {
        "alias": "qual-tiny",
        "recipe_content_sha256": "a" * 64,
        "fixture_manifest_sha256": "b" * 64,
        "cases": [
            {
                "id": "bounded-chat",
                "method": "POST",
                "path": "/chat/completions",
                "body": {
                    "model": "qual-tiny",
                    "messages": [{"role": "user", "content": "Reply OK."}],
                },
                "timeout_seconds": 60,
                "max_response_bytes": 1024,
                "assertions": [
                    {
                        "kind": "path.equals",
                        "path": "choices.0.message.content",
                        "value": "OK",
                    }
                ],
            }
        ],
    }

    result = adapter.run(client, "qual-tiny", preview)

    request, timeout = requests[0]
    assert request.full_url == "https://models.example.test/v1/chat/completions"
    assert json.loads(request.data)["model"] == "qual-tiny"
    assert timeout == 60
    assert len(result["cases"][0]["response_sha256"]) == 64


def test_apply_records_static_blockers_without_mutation(tmp_path: Path) -> None:
    options = RunnerOptions()
    plan = _campaign_plan(
        [
            {
                "key": "vonk/wide",
                "blockers": [
                    {
                        "classification": "topology",
                        "code": "topology.unsupported_fleet_width",
                        "detail": "requires eight Sparks",
                    }
                ],
            }
        ],
        options,
    )
    ledger = EvidenceLedger(tmp_path / "evidence.jsonl")
    client = _Client(
        {
            ("GET", "/api/v1/library"): {
                "models": [],
                "unlinked_recipes": [],
                "next_cursor": None,
            },
            ("GET", "/api/v1/fleet"): _fleet(0),
        }
    )

    result = QualificationRunner(client, ledger, options).apply(
        plan, str(plan["plan_digest"])
    )

    assert result["blocked"] == 1
    assert all(call[0] == "GET" for call in client.calls)
    assert ledger.completed_recipes(str(plan["plan_digest"])) == set()


def test_apply_blocks_after_import_when_resolved_model_license_denies_jurisdiction(
    tmp_path: Path,
) -> None:
    options = RunnerOptions(jurisdiction="NL")
    plan = _campaign_plan(
        [
            {
                "key": "vonk/restricted",
                "uri": _recipe("restricted")["uri"],
                "content_sha256": "c" * 64,
                "local_recipe_id": None,
                "local_revision_id": None,
                "blockers": [],
                "node_count": 1,
                "release_version": "1.0.0",
            }
        ],
        options,
    )
    digest = str(plan["plan_digest"])
    client = _Client(
        {
            ("POST", "/api/v1/catalog/imports/public"): {
                "recipe_id": "00000000-0000-4000-8000-000000000001",
                "id": "00000000-0000-4000-8000-000000000002",
                "content_sha256": "c" * 64,
            },
            (
                "GET",
                "/api/v1/library/recipes/00000000-0000-4000-8000-000000000001",
            ): {
                "selected_revision": {
                    "id": "00000000-0000-4000-8000-000000000002",
                    "content_sha256": "c" * 64,
                },
                "visual_recipe": {
                    "model_license": {
                        "territorial_restrictions": {
                            "denied_jurisdictions": ["EU"],
                            "notice": "Excluded territory",
                        }
                    }
                },
            },
            ("GET", "/api/v1/library"): {
                "models": [],
                "unlinked_recipes": [],
                "next_cursor": None,
            },
            ("GET", "/api/v1/fleet"): _fleet(1),
        }
    )
    ledger = EvidenceLedger(tmp_path / "evidence.jsonl")

    result = QualificationRunner(client, ledger, options).apply(plan, digest)

    assert result["blocked"] == 1
    assert result["succeeded"] == 0
    blocked = [row for row in ledger.records if row["event"] == "recipe.blocked"]
    assert blocked[0]["payload"]["blockers"][0]["code"] == "license.territory_denied"


def test_resume_never_uninstalls_a_preexisting_installation(tmp_path: Path) -> None:
    digest = "d" * 64
    key = "vonk/tiny"
    ledger = EvidenceLedger(tmp_path / "evidence.jsonl")
    for step, owner in (
        ("image-distribution", "build-1"),
        ("run", "run-1"),
        ("stop", "run-1"),
        ("warm-redeploy", "run-2"),
        ("warm-redeploy-stop", "run-2"),
    ):
        ledger.append(
            "operation.completed",
            plan_digest=digest,
            recipe=key,
            payload={
                "step": step,
                "operation": {
                    "id": f"operation-{step}",
                    "state": "succeeded",
                    "owner_id": owner,
                },
            },
        )
    ledger.append(
        "step.completed",
        plan_digest=digest,
        recipe=key,
        payload={"step": "smoke", "result": {"response_sha256": "a" * 64}},
    )
    ledger.append(
        "step.completed",
        plan_digest=digest,
        recipe=key,
        payload={
            "step": "warm-redeploy-smoke",
            "result": {"response_sha256": "b" * 64},
        },
    )
    detail = {
        "selected_revision": {
            "id": "revision-1",
            "content_sha256": "c" * 64,
        },
        "visual_recipe": {"interfaces": [{"adapter": "openai-chat"}]},
        "operational_state": {
            "mappings": [{"mapping_id": "mapping-1", "generation": 3}]
        },
        "placement": [
            {
                "recommendations": [
                    {
                        "eligible": True,
                        "node_ids": ["spk_" + "1" * 32],
                        "mapping_id": "mapping-1",
                        "recipe_build_id": "build-1",
                        "installation_ids": ["installation-preexisting"],
                    }
                ]
            }
        ],
    }
    client = _Client(
        {
            ("GET", "/api/v1/library/recipes/recipe-1"): detail,
            ("POST", "/api/v1/recipes/image-distribution-plans/preview"): {
                "plan_digest": "f" * 64
            },
        }
    )
    runner = QualificationRunner(client, ledger, RunnerOptions(cleanup="uninstall"))

    runner._apply_recipe(
        digest,
        {
            "key": key,
            "content_sha256": "c" * 64,
            "node_count": 1,
            "local_recipe_id": "recipe-1",
            "local_revision_id": "revision-1",
        },
    )

    assert all("uninstall" not in call[1] for call in client.calls)
    skipped = [row for row in ledger.records if row["event"] == "cleanup.skipped"]
    assert skipped[-1]["payload"]["installation_id"] == "installation-preexisting"


def test_final_residency_is_per_revision_and_includes_stale_retained_installs(
    tmp_path: Path,
) -> None:
    digest = "d" * 64
    recipe_id = "00000000-0000-4000-8000-000000000001"
    selected_revision = "00000000-0000-4000-8000-000000000002"
    stale_revision = "00000000-0000-4000-8000-000000000003"
    node_id = "spk_" + "1" * 32
    installations = [
        {
            "installation_id": "00000000-0000-4000-8000-000000000010",
            "recipe_revision_id": selected_revision,
            "state": "installed",
            "node_ids": [node_id],
        },
        {
            "installation_id": "00000000-0000-4000-8000-000000000011",
            "recipe_revision_id": stale_revision,
            "state": "installed",
            "node_ids": [node_id],
        },
    ]
    detail = {
        "selected_revision": {
            "id": selected_revision,
            "content_sha256": "c" * 64,
        },
        "operational_state": {"installations": installations},
    }
    summary = {
        "recipe_id": recipe_id,
        "slug": "tiny",
        "selected_revision": {"id": selected_revision},
        "installations": installations,
        "installation_total_count": 2,
        "installations_truncated": False,
    }
    fleet = _fleet(1)
    fleet["nodes"][0]["id"] = node_id
    client = _Client(
        {
            ("GET", f"/api/v1/library/recipes/{recipe_id}"): detail,
            ("GET", "/api/v1/library"): {
                "models": [],
                "unlinked_recipes": [summary],
                "next_cursor": None,
            },
            ("GET", "/api/v1/fleet"): fleet,
        }
    )
    ledger = EvidenceLedger(tmp_path / "residency.jsonl")
    ledger.append(
        "recipe.succeeded",
        plan_digest=digest,
        recipe="vonk/tiny",
        payload={
            "recipe_id": recipe_id,
            "recipe_revision_id": selected_revision,
            "installation_id": installations[0]["installation_id"],
        },
    )
    ledger.append(
        "step.completed",
        plan_digest=digest,
        recipe="vonk/tiny",
        payload={"step": "warm-redeploy-smoke", "result": {}},
    )
    inventory = QualificationRunner(
        client, ledger, RunnerOptions()
    )._residency_inventory(
        digest,
        {
            "recipes": [
                {
                    "key": "vonk/tiny",
                    "content_sha256": "c" * 64,
                    "local_recipe_id": recipe_id,
                    "local_revision_id": selected_revision,
                }
            ]
        },
    )

    dispositions = {
        item["recipe_revision_id"]: item["deployability"]
        for item in inventory["installations"]
    }
    assert dispositions[selected_revision] == "deployable-retained"
    assert dispositions[stale_revision] == "stale-revision-retained"
    assert inventory["installation_inventory_complete"] is True


def test_warm_smoke_failure_always_attempts_release_and_preserves_primary_error(
    tmp_path: Path,
) -> None:
    digest = "d" * 64
    key = "vonk/tiny"
    ledger = EvidenceLedger(tmp_path / "cleanup.jsonl")
    for step, owner in (
        ("image-distribution", "build-1"),
        ("run", "run-1"),
        ("stop", "run-1"),
        ("warm-redeploy", "run-2"),
    ):
        ledger.append(
            "operation.completed",
            plan_digest=digest,
            recipe=key,
            payload={
                "step": step,
                "operation": {
                    "id": f"operation-{step}",
                    "state": "succeeded",
                    "owner_id": owner,
                },
            },
        )
    ledger.append(
        "step.completed",
        plan_digest=digest,
        recipe=key,
        payload={"step": "smoke", "result": {}},
    )
    detail = {
        "selected_revision": {
            "id": "revision-1",
            "content_sha256": "c" * 64,
        },
        "visual_recipe": {"interfaces": [{"adapter": "openai-chat"}]},
        "operational_state": {
            "mappings": [{"mapping_id": "mapping-1", "generation": 3}]
        },
        "placement": [
            {
                "recommendations": [
                    {
                        "eligible": True,
                        "node_ids": ["spk_" + "1" * 32],
                        "mapping_id": "mapping-1",
                        "recipe_build_id": "build-1",
                        "installation_ids": ["installation-preexisting"],
                    }
                ]
            }
        ],
    }
    client = _CleanupFailureClient(
        {
            ("GET", "/api/v1/library/recipes/recipe-1"): detail,
            ("POST", "/api/v1/recipes/image-distribution-plans/preview"): {
                "plan_digest": "f" * 64
            },
        }
    )
    runner = QualificationRunner(
        client,
        ledger,
        RunnerOptions(cleanup="stop"),
        service_smoke=_FailingServiceSmoke(),
    )

    with pytest.raises(RuntimeError, match="original smoke failure"):
        runner._apply_recipe(
            digest,
            {
                "key": key,
                "content_sha256": "c" * 64,
                "node_count": 1,
                "local_recipe_id": "recipe-1",
                "local_revision_id": "revision-1",
            },
        )

    failures = [
        row for row in ledger.records if row["event"] == "cleanup.release-failed"
    ]
    assert failures[-1]["payload"]["error"] == "cleanup failure"
    assert failures[-1]["payload"]["original_error"] == "original smoke failure"
    assert any(call[1] == "/api/v1/recipes/stop-plans/preview" for call in client.calls)


@pytest.mark.parametrize(
    ("service_smoke", "error_message"),
    [
        (_FailingServiceSmoke(), "original smoke failure"),
        (_PreviewFailingServiceSmoke(), "preview smoke failure"),
    ],
)
def test_initial_smoke_failure_always_attempts_release_and_preserves_primary_error(
    tmp_path: Path, service_smoke: object, error_message: str
) -> None:
    digest = "d" * 64
    key = "vonk/tiny"
    ledger = EvidenceLedger(tmp_path / "initial-cleanup.jsonl")
    for step, owner in (("image-distribution", "build-1"), ("run", "run-1")):
        ledger.append(
            "operation.completed",
            plan_digest=digest,
            recipe=key,
            payload={
                "step": step,
                "operation": {
                    "id": f"operation-{step}",
                    "state": "succeeded",
                    "owner_id": owner,
                },
            },
        )
    detail = {
        "selected_revision": {
            "id": "revision-1",
            "content_sha256": "c" * 64,
        },
        "visual_recipe": {"interfaces": [{"adapter": "openai-chat"}]},
        "operational_state": {
            "mappings": [{"mapping_id": "mapping-1", "generation": 3}]
        },
        "placement": [
            {
                "recommendations": [
                    {
                        "eligible": True,
                        "node_ids": ["spk_" + "1" * 32],
                        "mapping_id": "mapping-1",
                        "recipe_build_id": "build-1",
                        "installation_ids": ["installation-preexisting"],
                    }
                ]
            }
        ],
    }
    client = _CleanupFailureClient(
        {
            ("GET", "/api/v1/library/recipes/recipe-1"): detail,
            ("POST", "/api/v1/recipes/image-distribution-plans/preview"): {
                "plan_digest": "f" * 64
            },
        }
    )
    runner = QualificationRunner(
        client,
        ledger,
        RunnerOptions(cleanup="stop"),
        service_smoke=service_smoke,
    )

    with pytest.raises(RuntimeError, match=error_message):
        runner._apply_recipe(
            digest,
            {
                "key": key,
                "content_sha256": "c" * 64,
                "node_count": 1,
                "local_recipe_id": "recipe-1",
                "local_revision_id": "revision-1",
            },
        )

    failure = next(
        row
        for row in reversed(ledger.records)
        if row["event"] == "cleanup.release-failed"
    )
    assert failure["payload"]["step"] == "stop"
    assert failure["payload"]["original_error"] == error_message
