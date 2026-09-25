from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
from argparse import Namespace
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Lock
from types import SimpleNamespace
from typing import cast
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
    library_root = Path(os.environ["VONK_RECIPE_LIBRARY_ROOT"]).resolve()
    (root / "tools").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(
        library_root / "tools/build-catalog-index", root / "tools/build-catalog-index"
    )
    contract_source = root / "contracts" / "src"
    contract_source.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        library_root / "contracts/src/vonk_forge_contracts",
        contract_source / "vonk_forge_contracts",
    )
    reviewed_authority = json.loads(
        (
            library_root / "qualification/authorities/nl-family-aware-20260924.json"
        ).read_text(encoding="utf-8")
    )
    reviewed_catalog = json.loads(
        (library_root / "catalog-index.json").read_text(encoding="utf-8")
    )
    candidates = {
        row["key"]: row
        for row in reviewed_authority["recipes"]
        if row["interface"] == "openai-service"
    }
    selected_row = candidates[
        "vonk-forge/laguna-s-2-1-nvfp4-vllm-low-memory-canary-single"
    ]
    alternate_row = candidates["vonk-forge/laguna-s-2-1-nvfp4-vllm-single"]
    recipe_entries = {
        f"{entry['document']['identity']['publisher']}/{entry['document']['identity']['slug']}": entry
        for entry in reviewed_catalog["recipes"]
    }
    selected_entry = recipe_entries[selected_row["key"]]
    alternate_entry = recipe_entries[alternate_row["key"]]
    assert selected_entry["package"]["path"] != alternate_entry["package"]["path"]

    package = (library_root / selected_entry["package"]["path"]).read_bytes()
    for entry in (selected_entry, alternate_entry):
        package_path = root / entry["package"]["path"]
        package_path.parent.mkdir(parents=True, exist_ok=True)
        package_path.write_bytes((library_root / entry["package"]["path"]).read_bytes())

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

    source_commit = reviewed_authority["catalog"]["source_commit"]
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    object_directory = subprocess.run(
        [
            "git",
            "-C",
            str(library_root),
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "objects",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (root / ".git" / "objects" / "info" / "alternates").write_text(
        f"{object_directory}\n", encoding="utf-8"
    )
    subprocess.run(
        ["git", "-C", str(root), "update-ref", "refs/heads/source", source_commit],
        check=True,
    )
    catalog_document = {
        "schema_version": reviewed_catalog["schema_version"],
        "kind": reviewed_catalog["kind"],
        "source_commit": source_commit,
        "recipes": [selected_entry, alternate_entry],
        "catalog_entities": reviewed_catalog["catalog_entities"],
    }
    catalog_raw = json.dumps(
        catalog_document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    (root / "catalog-index.json").write_bytes(catalog_raw)

    selected_row = json.loads(json.dumps(selected_row))
    selected_row["sequence"] = 1
    selected_row.setdefault("runtime_stack_sha256", "b" * 64)
    selected_row.setdefault("topology_sha256", "c" * 64)
    selected_row["recovery_coverage_refs"] = []
    recovery_coverage: list[dict[str, object]] = []
    failure_modes = (
        ["single-host-restart"]
        if selected_row["node_count"] == 1
        else ["dual-rank-loss-recovery", "dual-host-restart"]
    )
    for failure_mode in failure_modes:
        coverage_without_id: dict[str, object] = {
            "failure_mode": failure_mode,
            "representative_recipe": selected_row["key"],
            "members": [
                {
                    "recipe": selected_row["key"],
                    "recipe_content_sha256": selected_row["content_sha256"],
                    "package_sha256": selected_row["package"]["sha256"],
                    "model_content_sha256s": sorted(
                        {
                            item["content_sha256"]
                            for item in selected_row["model_license_refs"]
                        }
                    ),
                    "runtime_stack_sha256": selected_row["runtime_stack_sha256"],
                    "topology_sha256": selected_row["topology_sha256"],
                }
            ],
            "shared": False,
            "equivalence_rationale": "Dedicated exact recipe recovery fixture.",
            "invalidated_by": [
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
            ],
        }
        coverage_id = campaign_cli._contract_digest(coverage_without_id)
        recovery_coverage.append({"coverage_id": coverage_id, **coverage_without_id})
        selected_row["recovery_coverage_refs"].append(
            {
                "coverage_id": coverage_id,
                "failure_mode": failure_mode,
                "representative_recipe": selected_row["key"],
                "role": "dedicated",
            }
        )
    batch_mode = "single" if selected_row["node_count"] == 1 else "exclusive-dual"

    authority = {
        "schema_version": 4,
        "authority_id": "test-authority",
        "catalog": {
            "repository": reviewed_authority["catalog"]["repository"],
            "commit": reviewed_authority["catalog"]["commit"],
            "release_tag": reviewed_authority["catalog"]["release_tag"],
            "source_commit": source_commit,
            "catalog_index_sha256": hashlib.sha256(catalog_raw).hexdigest(),
            "qualification_index_sha256": hashlib.sha256(fixture_raw).hexdigest(),
            "recipe_count": len(catalog_document["recipes"]),
        },
        "scope": {
            "maximum_node_count": 2,
            "recipe_count": 1,
            "excluded_topology_recipe_keys": [alternate_row["key"]],
        },
        "recipes": [selected_row],
        "batches": [
            {
                "sequence": 1,
                "id": "batch-001",
                "mode": batch_mode,
                "assignments": [
                    {
                        "recipe": selected_row["key"],
                        "lane": 1,
                        "node_count": selected_row["node_count"],
                    }
                ],
            }
        ],
        "recovery_coverage": recovery_coverage,
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


def _rewrite_package(payload: bytes, mutation: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        files: dict[str, bytes] = {}
        for member in archive.getmembers():
            source = archive.extractfile(member)
            if source is not None:
                files[member.name] = source.read()
    package_manifest = json.loads(files["manifest.json"])
    if mutation == "recipe-document":
        recipe = json.loads(files["recipe.json"])
        recipe["metadata"]["title"] += " altered"
        files["recipe.json"] = json.dumps(
            recipe, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        recipe_entry = next(
            entry
            for entry in package_manifest["files"]
            if entry["path"] == "recipe.json"
        )
        recipe_entry["size"] = len(files["recipe.json"])
        recipe_entry["sha256"] = hashlib.sha256(files["recipe.json"]).hexdigest()
    elif mutation == "manifest-recipe-digest":
        package_manifest["recipe_content_sha256"] = "f" * 64
    elif mutation == "extra-member":
        files["untrusted.txt"] = b"extra package member"
        package_manifest["files"].append(
            {
                "path": "untrusted.txt",
                "size": len(files["untrusted.txt"]),
                "sha256": hashlib.sha256(files["untrusted.txt"]).hexdigest(),
            }
        )
    else:
        raise AssertionError(f"unknown package mutation: {mutation}")

    files["manifest.json"] = json.dumps(
        package_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    output = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed,
        tarfile.open(
            fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT
        ) as archive,
    ):
        for name, content in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = 0o644
            member.uid = member.gid = 0
            member.uname = member.gname = ""
            member.mtime = 0
            archive.addfile(member, io.BytesIO(content))
    return output.getvalue()


def _rebind_package_digests(root: Path, payload: bytes) -> None:
    authority_path = root / "qualification" / "authorities" / "test.json"
    authority = json.loads(authority_path.read_text(encoding="utf-8"))
    row = authority["recipes"][0]
    package = row["package"]
    package_path = root / package["path"]
    package_path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    package["sha256"] = digest
    package["expected_bytes"] = len(payload)

    catalog_path = root / "catalog-index.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    entry = next(
        item
        for item in catalog["recipes"]
        if f"{item['document']['identity']['publisher']}/{item['document']['identity']['slug']}"
        == row["key"]
    )
    entry["package"]["sha256"] = digest
    entry["package"]["expected_bytes"] = len(payload)
    catalog_raw = json.dumps(
        catalog, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    catalog_path.write_bytes(catalog_raw)
    authority["catalog"]["catalog_index_sha256"] = hashlib.sha256(
        catalog_raw
    ).hexdigest()
    _rebind_authority_coverage_identity(authority)
    authority_path.write_text(json.dumps(authority), encoding="utf-8")


def _rebind_authority_coverage_identity(authority: dict[str, object]) -> None:
    rows = authority["recipes"]
    coverage_rows = authority["recovery_coverage"]
    assert isinstance(rows, list) and isinstance(coverage_rows, list)
    rows_by_key = {str(row["key"]): row for row in rows}
    for raw_coverage in coverage_rows:
        coverage = cast(dict[str, object], raw_coverage)
        old_id = str(coverage["coverage_id"])
        members = coverage["members"]
        assert isinstance(members, list)
        for raw_member in members:
            member = cast(dict[str, object], raw_member)
            row = rows_by_key[str(member["recipe"])]
            package = cast(dict[str, object], row["package"])
            model_refs = cast(list[dict[str, object]], row["model_license_refs"])
            member.update(
                {
                    "recipe_content_sha256": row["content_sha256"],
                    "package_sha256": package["sha256"],
                    "model_content_sha256s": sorted(
                        {str(model["content_sha256"]) for model in model_refs}
                    ),
                    "runtime_stack_sha256": row["runtime_stack_sha256"],
                    "topology_sha256": row["topology_sha256"],
                }
            )
        unsigned = {
            key: value for key, value in coverage.items() if key != "coverage_id"
        }
        coverage_id = campaign_cli._contract_digest(unsigned)
        coverage["coverage_id"] = coverage_id
        for row in rows:
            references = cast(list[dict[str, object]], row["recovery_coverage_refs"])
            for reference in references:
                if reference["coverage_id"] == old_id:
                    reference["coverage_id"] = coverage_id


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
        "runtime_stack_sha256": "b" * 64,
        "topology_sha256": "c" * 64,
        "recovery_coverage_refs": [],
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
        runtime_stack_sha256="b" * 64,
        topology_sha256="c" * 64,
        recovery_coverage_refs=(),
        raw=raw,
    )


def _rollout_preparation(
    *,
    node_ids: tuple[str, ...] = (NODE_A,),
    model_state: str = "unknown",
    image_state: str = "unknown",
) -> dict[str, object]:
    model_set_digest = "e" * 64
    image_digest = "sha256:" + "f" * 64
    image_layout_digest = "a" * 64
    verified_at = "2026-09-24T10:00:00Z"

    def target(
        node_id: str,
        state: str,
        *,
        verified_sha256: str | None,
        imported_image_digest: str | None = None,
    ) -> dict[str, object]:
        ready = state == "ready"
        return {
            "node_id": node_id,
            "state": state,
            "expected_bytes": 100,
            "present_bytes": 100 if ready else 0,
            "missing_bytes": 0 if ready else 100,
            "verified_sha256": verified_sha256 if ready else None,
            "imported_image_digest": imported_image_digest if ready else None,
            "verified_at": verified_at if ready else None,
            "reason": None
            if ready
            else "The exact asset is not verified on this Spark.",
        }

    return {
        "schema_version": 2,
        "controller_ready": True,
        "targets_ready": model_state == "ready" and image_state == "ready",
        "ready": model_state == "ready" and image_state == "ready",
        "target_node_ids": sorted(node_ids),
        "model": {
            "artifact_set_sha256": model_set_digest,
            "model_content_sha256": "d" * 64,
            "recipe_revision_sha256": CONTENT_SHA,
            "artifact_count": 1,
            "artifact_set_bytes": 100,
            "dependency_model_content_sha256": [],
            "completeness": "complete" if model_state == "ready" else "incomplete",
            "controller": {
                "state": "ready",
                "expected_bytes": 100,
                "verified_bytes": 100,
                "missing_bytes": 0,
                "verified_sha256": model_set_digest,
                "verified_at": verified_at,
                "source": "nas-cache",
                "reason": None,
            },
            "targets": [
                target(
                    node_id,
                    model_state,
                    verified_sha256=model_set_digest,
                )
                for node_id in sorted(node_ids)
            ],
        },
        "runtime_image": {
            "image_digest": image_digest,
            "oci_layout_sha256": image_layout_digest,
            "image_bytes": 100,
            "architecture": "linux-arm64",
            "runtime_interface": "vonk.runtime.v1",
            "build_id": "build-1",
            "controller": {
                "state": "ready",
                "expected_bytes": 100,
                "verified_bytes": 100,
                "missing_bytes": 0,
                "verified_sha256": image_layout_digest,
                "verified_at": verified_at,
                "source": "controller-build",
                "reason": None,
            },
            "targets": [
                target(
                    node_id,
                    image_state,
                    verified_sha256=image_layout_digest,
                    imported_image_digest=image_digest,
                )
                for node_id in sorted(node_ids)
            ],
        },
        "exceptions": [],
        "reasons": [],
    }


def _node(
    node_id: str,
    *,
    online: bool = True,
    boot_id: str = "boot-before",
    loaded: Sequence[Mapping[str, object]] | None = None,
    freshness: str = "live",
) -> dict[str, object]:
    return {
        "id": node_id,
        "connection": {"online_state": "online" if online else "offline"},
        "telemetry": {"freshness": freshness, "sample": {"boot_id": boot_id}},
        "loaded": loaded or [],
    }


def _loaded_run_presence(
    *,
    run_id: str = RUN_ID,
    node_ids: tuple[str, ...] = (NODE_A, NODE_B),
    rank: int = 0,
) -> dict[str, object]:
    return {
        "alias": "current-service",
        "expected_rank_count": len(node_ids),
        "group_state": "healthy",
        "healthy": True,
        "installation_id": "installation-current",
        "member_node_ids": list(node_ids),
        "present_ranks": list(range(len(node_ids))),
        "rank": rank,
        "rank_age_seconds": 1.0,
        "rank_fresh": True,
        "rank_state": "running",
        "recipe_id": "recipe-current",
        "recipe_revision_id": "revision-current",
        "role": f"rank-{rank}",
        "route_state": "published",
        "run_id": run_id,
        "run_state": "running",
        "title": "Current service",
    }


def _healthy_two_spark_fleet(run_id: str = RUN_ID) -> dict[str, object]:
    return {
        "nodes": [
            _node(
                NODE_A,
                loaded=[_loaded_run_presence(run_id=run_id, rank=0)],
            ),
            _node(
                NODE_B,
                loaded=[_loaded_run_presence(run_id=run_id, rank=1)],
            ),
        ]
    }


def test_one_exact_acknowledged_healthy_run_is_the_only_replacement_candidate() -> None:
    fleet = _healthy_two_spark_fleet()

    with pytest.raises(QualificationError, match="--replace-run-id"):
        campaign_cli._assert_fleet_exclusive(fleet)

    evidence = campaign_cli._assert_fleet_exclusive(fleet, replace_run_id=RUN_ID)

    assert evidence == {
        "run_id": RUN_ID,
        "member_node_ids": [NODE_A, NODE_B],
        "expected_rank_count": 2,
        "present_ranks": [0, 1],
    }


@pytest.mark.parametrize("replace_run_id", ["different-run", "new-run"])
def test_replacement_acknowledgement_must_match_the_only_current_run(
    replace_run_id: str,
) -> None:
    with pytest.raises(QualificationError, match="acknowledged run"):
        campaign_cli._assert_fleet_exclusive(
            _healthy_two_spark_fleet(), replace_run_id=replace_run_id
        )


def test_replacement_rejects_an_additional_foreign_run() -> None:
    fleet = _healthy_two_spark_fleet()
    nodes = fleet["nodes"]
    assert isinstance(nodes, list)
    first = nodes[0]
    assert isinstance(first, dict)
    loaded = first["loaded"]
    assert isinstance(loaded, list)
    loaded.append(_loaded_run_presence(run_id="another-run", rank=0))

    with pytest.raises(QualificationError, match="sole current foreign run"):
        campaign_cli._assert_fleet_exclusive(fleet, replace_run_id=RUN_ID)


def test_replacement_rejects_a_run_whose_complete_membership_is_not_in_fleet() -> None:
    fleet = {
        "nodes": [
            _node(NODE_A, loaded=[_loaded_run_presence(rank=0)]),
        ]
    }

    with pytest.raises(QualificationError, match="complete run membership"):
        campaign_cli._assert_fleet_exclusive(fleet, replace_run_id=RUN_ID)


def test_no_loaded_run_rejects_stale_replacement_acknowledgement() -> None:
    fleet = {"nodes": [_node(NODE_A)]}
    with pytest.raises(QualificationError, match="acknowledged run"):
        campaign_cli._assert_fleet_exclusive(fleet, replace_run_id=RUN_ID)


def test_campaign_digest_binds_replacement_evidence_and_exact_plan_digest(
    tmp_path: Path,
) -> None:
    batch, lanes = _paired_batch_inputs()
    manifest = _paired_manifest([lane.row for lane in lanes], [batch])
    fixtures = cast(
        FixtureRegistry, SimpleNamespace(manifest_sha256=PAIRED_FIXTURE_DIGEST)
    )
    profile = {"id": PAIRED_PROFILE_ID, "profile_digest": "b" * 64}
    base_preview = {
        "plan_digest": "a" * 64,
        "exact_preparations": {"target_node_ids": [NODE_A]},
        "replacement_interruption": {
            "runs": [{"run_id": RUN_ID, "member_node_ids": [NODE_A, NODE_B]}],
            "switch_node_ids": [NODE_A, NODE_B],
            "stop_count": 1,
            "start_count": 2,
            "profile_plan_digest": "a" * 64,
        },
    }
    digest = campaign_cli._batch_preview_digest(
        manifest=manifest,
        fixtures=fixtures,
        batch=batch,
        lanes=lanes,
        profile=profile,
        preview=base_preview,
        profile_number=7,
        failure_node_id=None,
    )

    changed_evidence = {
        **base_preview,
        "replacement_interruption": {
            **base_preview["replacement_interruption"],
            "runs": [{"run_id": "different-run", "member_node_ids": [NODE_A, NODE_B]}],
        },
    }
    assert (
        campaign_cli._batch_preview_digest(
            manifest=manifest,
            fixtures=fixtures,
            batch=batch,
            lanes=lanes,
            profile=profile,
            preview=changed_evidence,
            profile_number=7,
            failure_node_id=None,
        )
        != digest
    )

    changed_plan = {
        **base_preview,
        "replacement_interruption": {
            **base_preview["replacement_interruption"],
            "profile_plan_digest": "c" * 64,
        },
    }
    assert (
        campaign_cli._batch_preview_digest(
            manifest=manifest,
            fixtures=fixtures,
            batch=batch,
            lanes=lanes,
            profile=profile,
            preview=changed_plan,
            profile_number=7,
            failure_node_id=None,
        )
        != digest
    )


def test_fresh_batch_preview_persists_replacement_evidence_in_campaign_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch, base_lanes = _paired_batch_inputs()
    lanes: list[campaign_cli.BatchLane] = []
    service_recipes: dict[str, ServiceRecipe] = {}
    for lane in base_lanes:
        model_ref = {"key": "test-model", "content_sha256": "d" * 64}
        raw_row = {**dict(lane.row.raw), "model_license_refs": [model_ref]}
        row = replace(lane.row, model_license_refs=(model_ref,), raw=raw_row)
        lanes.append(
            replace(
                lane,
                row=row,
                detail={
                    "identity": {
                        "recipe_id": f"controller-recipe-{lane.assignment.lane}"
                    }
                },
            )
        )
        service_recipes[row.key] = ServiceRecipe(
            row.key,
            row.content_sha256,
            lane.alias,
            (ServiceCase("health", "GET", "/v1/models", None, 5, 1024, ()),),
            {},
        )
    batch_lanes = tuple(lanes)
    manifest = _paired_manifest([lane.row for lane in batch_lanes], [batch])
    fixtures = FixtureRegistry(
        {},
        {},
        {},
        manifest_sha256=PAIRED_FIXTURE_DIGEST,
        service_recipes=service_recipes,
    )
    fleet = _typed_paired_fleet(batch_lanes)
    ledger = EvidenceLedger(tmp_path / "evidence.jsonl")
    raw_preview: dict[str, object] = {
        "allowed": True,
        "profile_id": PAIRED_PROFILE_ID,
        "profile_name": "paired qualification",
        "profile_digest": PAIRED_PROFILE_DIGEST,
        "profile_revision": 1,
        "profile_definition": None,
        "scope": {"node_ids": [NODE_A, NODE_B], "idle_node_ids": []},
        "plan_digest": "a" * 64,
        "summary": {
            "stops": 2,
            "starts": 2,
            "placements": 0,
            "builds": 0,
            "distributions": 0,
            "installs": 0,
            "uninstalls": 0,
        },
        "assignments": [
            {
                "assignment_id": f"assignment-{lane.assignment.lane}",
                "recipe_id": f"controller-recipe-{lane.assignment.lane}",
                "recipe_revision_id": f"recipe-revision-{lane.assignment.lane}",
                "node_ids": list(lane.node_ids),
                "desired_state": "running",
            }
            for lane in batch_lanes
        ],
        "resolved_assignments": [],
        "admission_decisions": [],
        "preparation_decisions": [],
        "effects": {
            "runs": [
                {
                    "run_id": f"run-{lane.assignment.lane}",
                    "action": "stop",
                    "alias": lane.alias,
                    "node_ids": list(lane.node_ids),
                }
                for lane in batch_lanes
            ]
        },
        "steps": [{"kind": "switch", "node_ids": [NODE_A, NODE_B]}],
        "reasons": [],
        "generated_at": "2026-09-25T00:00:00Z",
        "assessments": [],
        "preparations": [
            {
                "assignment_id": f"assignment-{lane.assignment.lane}",
                "preparation": {
                    **_rollout_preparation(node_ids=lane.node_ids),
                    "model": {
                        **cast(
                            Mapping[str, object],
                            _rollout_preparation(node_ids=lane.node_ids)["model"],
                        ),
                        "recipe_revision_sha256": lane.row.content_sha256,
                    },
                },
            }
            for lane in batch_lanes
        ],
    }
    profile = {
        "id": PAIRED_PROFILE_ID,
        "name": "paired qualification",
        "profile_digest": PAIRED_PROFILE_DIGEST,
        "revision": 1,
        "installation_policy": "keep-cached",
        "assignments": [],
    }

    class PreviewClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def request(self, method: str, path: str) -> dict[str, object]:
            self.calls.append((method, path))
            assert method == "POST"
            assert path == "/api/profile/7/preview"
            return raw_preview

    preview_client = PreviewClient()
    monkeypatch.setattr(
        campaign_cli,
        "_validate_current_recipe",
        lambda _client, row: (
            {"identity": {"recipe_id": f"controller-recipe-{row.sequence}"}},
            {},
        ),
    )
    monkeypatch.setattr(campaign_cli, "_typed_fleet", lambda _client: fleet)
    monkeypatch.setattr(
        campaign_cli, "_prepare_batch_profile", lambda **_kwargs: profile
    )
    monkeypatch.setattr(
        campaign_cli.FleetProfilePreview,
        "from_dict",
        classmethod(lambda _cls, value: SimpleNamespace(to_dict=lambda: dict(value))),
    )

    lanes, checked, _metadata = campaign_cli._fresh_batch_preview(
        client=preview_client,  # type: ignore[arg-type]
        manifest=manifest,
        fixtures=fixtures,
        library_root=tmp_path,
        batch=batch,
        selected_nodes={lane.row.key: lane.node_ids for lane in batch_lanes},
        profile_number=7,
        authority_id="test-authority",
        ledger_id="evidence.jsonl",
        campaign_id=CAMPAIGN_ID,
        ledger=ledger,
        expected_fleet_node_ids=[NODE_A, NODE_B],
        replace_run_ids=["run-1", "run-2"],
        failure_node_id=None,
    )

    assert [lane.row.key for lane in lanes] == [lane.row.key for lane in batch_lanes]
    plan_record = next(
        item for item in ledger.records if item.get("event") == "batch.plan.generated"
    )
    payload = cast(dict[str, object], plan_record["payload"])
    persisted_preview = cast(Mapping[str, object], payload["preview"])
    interruption = cast(
        Mapping[str, object], persisted_preview["replacement_interruption"]
    )
    assert interruption == checked["replacement_interruption"]
    runs = cast(list[Mapping[str, object]], interruption["runs"])
    assert [item["run_id"] for item in runs] == ["run-1", "run-2"]
    assert interruption["profile_plan_digest"] == raw_preview["plan_digest"]
    assert all(
        cast(Mapping[str, object], record["payload"])["replacement_interruption"]
        == interruption
        for record in ledger.records
        if record.get("event") == "plan.generated"
    )

    stale_fleet = _paired_fleet(batch_lanes)
    stale_nodes = cast(list[dict[str, object]], stale_fleet["nodes"])
    stale_loaded = cast(list[dict[str, object]], stale_nodes[0]["loaded"])
    stale_loaded[0]["run_id"] = "replacement-after-review"
    monkeypatch.setattr(campaign_cli, "_typed_fleet", lambda _client: stale_fleet)
    with pytest.raises(
        QualificationError, match="whole-Fleet batch replacement differs from live runs"
    ):
        campaign_cli._fresh_batch_preview(
            client=preview_client,
            manifest=manifest,
            fixtures=fixtures,
            library_root=tmp_path,
            batch=batch,
            selected_nodes={lane.row.key: lane.node_ids for lane in batch_lanes},
            profile_number=7,
            authority_id="test-authority",
            ledger_id="evidence.jsonl",
            campaign_id=CAMPAIGN_ID,
            ledger=EvidenceLedger(tmp_path / "stale-replacement.jsonl"),
            expected_fleet_node_ids=[NODE_A, NODE_B],
            replace_run_ids=["run-1", "run-2"],
            failure_node_id=None,
        )
    assert preview_client.calls == [("POST", "/api/profile/7/preview")]

    monkeypatch.setattr(campaign_cli, "_typed_fleet", lambda _client: fleet)
    raw_effects = cast(dict[str, object], raw_preview["effects"])
    raw_runs = cast(list[dict[str, object]], raw_effects["runs"])
    original_run_id = raw_runs[0]["run_id"]
    raw_runs[0]["run_id"] = "unreviewed-stop"
    rejected_ledger = EvidenceLedger(tmp_path / "changed-stop-preview.jsonl")
    with pytest.raises(
        QualificationError, match="batch replacement preview changed exact stopped runs"
    ):
        campaign_cli._fresh_batch_preview(
            client=preview_client,
            manifest=manifest,
            fixtures=fixtures,
            library_root=tmp_path,
            batch=batch,
            selected_nodes={lane.row.key: lane.node_ids for lane in batch_lanes},
            profile_number=7,
            authority_id="test-authority",
            ledger_id="evidence.jsonl",
            campaign_id=CAMPAIGN_ID,
            ledger=rejected_ledger,
            expected_fleet_node_ids=[NODE_A, NODE_B],
            replace_run_ids=["run-1", "run-2"],
            failure_node_id=None,
        )
    assert not rejected_ledger.records
    assert preview_client.calls == [
        ("POST", "/api/profile/7/preview"),
        ("POST", "/api/profile/7/preview"),
    ]
    raw_runs[0]["run_id"] = original_run_id


def test_campaign_parser_binds_batch_selection_and_explicit_recovery_consent() -> None:
    common = [
        "--manifest",
        "campaign.json",
        "--library-root",
        "recipes",
        "--ledger",
        "evidence.jsonl",
        "--profile-number",
        "7",
        "--batch",
        "batch-001",
    ]
    replacements = ["--replace-run-id", RUN_ID, "--replace-run-id", "other-run"]
    preview = campaign_cli._arguments(
        [
            *common,
            "--spark",
            NODE_A,
            "--spark",
            NODE_B,
            *replacements,
        ]
    )
    assert preview.batch == "batch-001"
    assert preview.spark == [NODE_A, NODE_B]
    assert preview.replace_run_id == [RUN_ID, "other-run"]

    apply = campaign_cli._arguments(
        [
            *common,
            "--spark",
            NODE_A,
            "--spark",
            NODE_B,
            *replacements,
            "--apply",
            "--campaign-digest",
            "a" * 64,
        ]
    )
    assert apply.replace_run_id == [RUN_ID, "other-run"]

    observe = campaign_cli._arguments([*common, "--observe"])
    assert observe.observe is True
    assert observe.spark == []

    recovery_preview = campaign_cli._arguments([*common, "--recover-lane", "2"])
    assert recovery_preview.recover_lane == 2
    assert recovery_preview.spark == []
    recovery_apply = campaign_cli._arguments(
        [
            *common,
            "--recover-lane",
            "2",
            "--apply",
            "--campaign-digest",
            "b" * 64,
        ]
    )
    assert recovery_apply.recover_lane == 2
    assert recovery_apply.apply is True
    cleanup_preview = campaign_cli._arguments([*common, "--cleanup-lane", "1"])
    assert cleanup_preview.cleanup_lane == 1
    cleanup_apply = campaign_cli._arguments(
        [
            *common,
            "--cleanup-lane",
            "1",
            "--apply",
            "--campaign-digest",
            "e" * 64,
        ]
    )
    assert cleanup_apply.cleanup_lane == 1
    assert cleanup_apply.apply is True

    for invalid in (
        [*common, "--observe", "--recover-lane", "2"],
        [*common, "--recover-lane", "2", "--cleanup-lane", "1"],
        [*common, "--recover-lane", "2", "--apply"],
        [
            "--manifest",
            "campaign.json",
            "--library-root",
            "recipes",
            "--ledger",
            "evidence.jsonl",
            "--profile-number",
            "7",
            "--recover-lane",
            "2",
        ],
        [*common, "--recover-lane", "2", "--spark", NODE_A],
    ):
        with pytest.raises(SystemExit):
            campaign_cli._arguments(invalid)


class _MissingEndpoint:
    def request(
        self, method: str, path: str, *args: object, **kwargs: object
    ) -> dict[str, object]:
        assert method == "GET"
        alias = unquote(path.rsplit("/", 1)[-1])
        assert alias == "test-alias"
        raise campaign_cli.ControlNotFound(404, "endpoint is absent", endpoint=path)


class _ExistingEndpoint:
    def request(
        self, method: str, path: str, *args: object, **kwargs: object
    ) -> dict[str, object]:
        assert method == "GET"
        assert unquote(path.rsplit("/", 1)[-1]) == "current-service"
        return {"api_base": "http://127.0.0.1:8000/v1"}


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
    assert (
        fixtures.manifest_sha256
        == manifest.authority.catalog["qualification_index_sha256"]
    )


def test_manifest_uses_canonical_default_for_omitted_operation_timeout(
    tmp_path: Path,
) -> None:
    campaign_path, _package, _fixture_raw = _catalog_inputs(tmp_path)

    manifest = campaign_cli.load_manifest(campaign_path, tmp_path)

    assert manifest.operation_timeout_seconds == 86_400


@pytest.mark.parametrize(
    "field", ["operation_timeout_seconds", "poll_interval_seconds"]
)
def test_manifest_rejects_integral_float_for_strict_integer_option(
    field: str, tmp_path: Path
) -> None:
    campaign_path, _package, _fixture_raw = _catalog_inputs(tmp_path)
    document = json.loads(campaign_path.read_text(encoding="utf-8"))
    document["options"][field] = 1.0
    campaign_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(QualificationError):
        campaign_cli.load_manifest(campaign_path, tmp_path)


def test_shared_recovery_group_requires_one_runtime_topology_identity(
    tmp_path: Path,
) -> None:
    library_root = Path(os.environ["VONK_RECIPE_LIBRARY_ROOT"]).resolve()
    authority_path = (
        library_root / "qualification/authorities/nl-family-aware-20260924.json"
    )
    authority = json.loads(authority_path.read_text(encoding="utf-8"))
    single_rows = sorted(
        (row for row in authority["recipes"] if row["node_count"] == 1),
        key=lambda row: row["key"],
    )
    members = single_rows[:2]
    assert (
        members[0]["runtime_stack_sha256"],
        members[0]["topology_sha256"],
    ) != (
        members[1]["runtime_stack_sha256"],
        members[1]["topology_sha256"],
    )
    coverage = authority["recovery_coverage"]
    old_ids: set[str] = set()
    original = next(
        item for item in coverage if item["failure_mode"] == "single-host-restart"
    )
    shared_definition = dict(original)
    shared_definition["shared"] = True
    shared_definition["representative_recipe"] = members[0]["key"]
    shared_definition["equivalence_rationale"] = (
        "Adversarial grouping with individually correct but incompatible member identities."
    )
    shared_definition["members"] = [
        {
            "recipe": row["key"],
            "recipe_content_sha256": row["content_sha256"],
            "package_sha256": row["package"]["sha256"],
            "model_content_sha256s": sorted(
                {item["content_sha256"] for item in row["model_license_refs"]}
            ),
            "runtime_stack_sha256": row["runtime_stack_sha256"],
            "topology_sha256": row["topology_sha256"],
        }
        for row in members
    ]
    for row in members:
        old_reference = next(
            reference
            for reference in row["recovery_coverage_refs"]
            if reference["failure_mode"] == "single-host-restart"
        )
        old_ids.add(old_reference["coverage_id"])
    authority["recovery_coverage"] = [
        item for item in coverage if item["coverage_id"] not in old_ids
    ]
    unsigned_definition = {
        key: value for key, value in shared_definition.items() if key != "coverage_id"
    }
    shared_definition["coverage_id"] = campaign_cli._contract_digest(
        unsigned_definition
    )
    authority["recovery_coverage"].append(shared_definition)
    for row in members:
        row["recovery_coverage_refs"] = [
            reference
            for reference in row["recovery_coverage_refs"]
            if reference["failure_mode"] != "single-host-restart"
        ] + [
            {
                "coverage_id": shared_definition["coverage_id"],
                "failure_mode": "single-host-restart",
                "representative_recipe": members[0]["key"],
                "role": "representative"
                if row["key"] == members[0]["key"]
                else "shared-member",
            }
        ]
    mutated_authority = tmp_path / authority_path.name
    mutated_authority.write_text(json.dumps(authority), encoding="utf-8")

    with pytest.raises(QualificationError):
        campaign_cli._load_authority(mutated_authority)


def test_repository_binding_rejects_a_swapped_valid_recipe_package(
    tmp_path: Path,
) -> None:
    campaign_path, _package, _fixture_raw = _catalog_inputs(tmp_path)
    authority_path = tmp_path / "qualification" / "authorities" / "test.json"
    authority = json.loads(authority_path.read_text(encoding="utf-8"))
    catalog = json.loads((tmp_path / "catalog-index.json").read_text(encoding="utf-8"))
    row = authority["recipes"][0]
    alternate_entry = next(
        entry
        for entry in catalog["recipes"]
        if f"{entry['document']['identity']['publisher']}/{entry['document']['identity']['slug']}"
        != row["key"]
    )
    row["package"] = {
        field: alternate_entry["package"][field]
        for field in ("path", "sha256", "expected_bytes", "media_type")
    }
    _rebind_authority_coverage_identity(authority)
    authority_path.write_text(json.dumps(authority), encoding="utf-8")

    manifest = campaign_cli.load_manifest(campaign_path, tmp_path)
    fixtures = FixtureRegistry.load(manifest.fixture_manifest)
    with pytest.raises(QualificationError, match="package path differs from catalog"):
        campaign_cli._bind_repository_inputs(manifest, tmp_path, fixtures)


def test_repository_binding_rejects_valid_archive_for_another_recipe(
    tmp_path: Path,
) -> None:
    campaign_path, _package, _fixture_raw = _catalog_inputs(tmp_path)
    authority_path = tmp_path / "qualification" / "authorities" / "test.json"
    authority = json.loads(authority_path.read_text(encoding="utf-8"))
    catalog = json.loads((tmp_path / "catalog-index.json").read_text(encoding="utf-8"))
    row = authority["recipes"][0]
    alternate_entry = next(
        entry
        for entry in catalog["recipes"]
        if f"{entry['document']['identity']['publisher']}/{entry['document']['identity']['slug']}"
        != row["key"]
    )
    alternate_archive = (tmp_path / alternate_entry["package"]["path"]).read_bytes()
    _rebind_package_digests(tmp_path, alternate_archive)

    manifest = campaign_cli.load_manifest(campaign_path, tmp_path)
    fixtures = FixtureRegistry.load(manifest.fixture_manifest)
    with pytest.raises(
        QualificationError, match="recipe package recipe does not match"
    ):
        campaign_cli._bind_repository_inputs(manifest, tmp_path, fixtures)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("recipe-document", "recipe package recipe does not match"),
        ("manifest-recipe-digest", "recipe package recipe digest is stale"),
        ("extra-member", "member is outside declared namespaces"),
    ],
)
def test_repository_binding_rejects_rehashed_packages_outside_recipe_closure(
    tmp_path: Path, mutation: str, message: str
) -> None:
    campaign_path, package, _fixture_raw = _catalog_inputs(tmp_path)
    _rebind_package_digests(tmp_path, _rewrite_package(package, mutation))

    manifest = campaign_cli.load_manifest(campaign_path, tmp_path)
    fixtures = FixtureRegistry.load(manifest.fixture_manifest)
    with pytest.raises(QualificationError, match=message):
        campaign_cli._bind_repository_inputs(manifest, tmp_path, fixtures)


def test_canonical_recipe_git_reads_are_bounded_and_fail_actionably(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    def timeout_run(
        command: Sequence[str], *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        observed["timeout"] = kwargs.get("timeout")
        observed["env"] = kwargs.get("env")
        raise subprocess.TimeoutExpired(
            cmd=command,
            timeout=cast(float, kwargs.get("timeout")),
        )

    monkeypatch.setattr(campaign_cli.subprocess, "run", timeout_run)
    with (
        pytest.raises(
            QualificationError,
            match="Git rev-parse timed out after 30 seconds.*local repository/object store",
        ),
        campaign_cli._canonical_recipe_package_tools(tmp_path, "a" * 40),
    ):
        pytest.fail("timed-out local Git read unexpectedly yielded tools")

    assert observed["timeout"] == 30
    environment = observed["env"]
    assert isinstance(environment, Mapping)
    assert environment["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert environment["GIT_NO_LAZY_FETCH"] == "1"


def test_repository_binding_does_not_execute_tampered_working_tree_tools(
    tmp_path: Path,
) -> None:
    campaign_path, _package, _fixture_raw = _catalog_inputs(tmp_path)
    tool_marker = tmp_path / "working-tree-validator-executed"
    contracts_marker = tmp_path / "working-tree-contracts-executed"
    replacement_tool_marker = tmp_path / "replace-ref-validator-executed"
    replacement_contracts_marker = tmp_path / "replace-ref-contracts-executed"
    tool_path = tmp_path / "tools" / "build-catalog-index"
    contracts_init = (
        tmp_path / "contracts" / "src" / "vonk_forge_contracts" / "__init__.py"
    )

    source_commit = json.loads(
        (tmp_path / "catalog-index.json").read_text(encoding="utf-8")
    )["source_commit"]

    def install_blob_replacement(relative_path: str, marker: Path) -> None:
        original_blob = subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "rev-parse",
                f"{source_commit}:{relative_path}",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        original_content = subprocess.run(
            ["git", "-C", str(tmp_path), "cat-file", "blob", original_blob],
            check=True,
            capture_output=True,
        ).stdout
        replacement_content = (
            original_content
            + b"\n__import__('pathlib').Path("
            + json.dumps(str(marker)).encode("utf-8")
            + b").write_text('executed')\n"
        )
        replacement_blob = (
            subprocess.run(
                ["git", "-C", str(tmp_path), "hash-object", "-w", "--stdin"],
                input=replacement_content,
                check=True,
                capture_output=True,
                text=False,
            )
            .stdout.decode("ascii")
            .strip()
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "replace", original_blob, replacement_blob],
            check=True,
        )

    install_blob_replacement("tools/build-catalog-index", replacement_tool_marker)
    install_blob_replacement(
        "contracts/src/vonk_forge_contracts/__init__.py", replacement_contracts_marker
    )

    tool_path.write_text(
        tool_path.read_text(encoding="utf-8")
        + "\nfrom pathlib import Path as _MarkerPath\n"
        + f"_MarkerPath({str(tool_marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    contracts_init.write_text(
        contracts_init.read_text(encoding="utf-8")
        + "\nfrom pathlib import Path as _MarkerPath\n"
        + f"_MarkerPath({str(contracts_marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )

    manifest = campaign_cli.load_manifest(campaign_path, tmp_path)
    fixtures = FixtureRegistry.load(manifest.fixture_manifest)
    campaign_cli._bind_repository_inputs(manifest, tmp_path, fixtures)

    assert not tool_marker.exists()
    assert not contracts_marker.exists()
    assert not replacement_tool_marker.exists()
    assert not replacement_contracts_marker.exists()


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
        runtime_stack_sha256=row.runtime_stack_sha256,
        topology_sha256=row.topology_sha256,
        recovery_coverage_refs=row.recovery_coverage_refs,
        raw=row.raw,
    )

    interface, binding = campaign_cli._fixture_bindings(row, registry)
    assert interface == "openai-service"
    cases = binding["cases"]
    assert isinstance(cases, list) and cases
    first_case = cases[0]
    assert isinstance(first_case, Mapping)
    assert first_case["id"] == "health"


def test_profile_application_digest_binds_preview_retry_and_request_identity() -> None:
    expected = "44870ddccd9cc6e6219681871c5113d0113f49666d36770980fcbb7b43a37d20"
    assert (
        campaign_cli._application_plan_digest("profile-plan", "qualification-request")
        == expected
    )
    assert (
        campaign_cli._application_plan_digest(
            "profile-plan",
            "qualification-request",
            retry_of_application_id="parent-application",
        )
        != expected
    )


def test_verified_controller_assets_allow_a_cold_two_spark_preview() -> None:
    node_ids = (NODE_A, NODE_B)
    row = replace(
        _row(node_count=2),
        model_license_refs=({"content_sha256": "d" * 64},),
    )
    preparation = _rollout_preparation(node_ids=node_ids)
    assert preparation["controller_ready"] is True
    assert preparation["targets_ready"] is False
    assert preparation["ready"] is False

    exact = campaign_cli._validate_preparations(
        {
            "preparations": [
                {
                    "assignment_id": "12345678-1234-4123-8123-123456789abc",
                    "preparation": preparation,
                }
            ]
        },
        row,
        [NODE_B, NODE_A],
    )

    assert exact == {
        "recipe_revision_sha256": CONTENT_SHA,
        "model_content_sha256": "d" * 64,
        "dependency_model_content_sha256": [],
        "artifact_set_sha256": "e" * 64,
        "image_digest": "sha256:" + "f" * 64,
        "architecture": "linux-arm64",
        "oci_layout_sha256": "a" * 64,
        "target_node_ids": [NODE_A, NODE_B],
    }


@pytest.mark.parametrize(
    ("asset_name", "target_state"),
    [("model", "failed"), ("runtime_image", "unsupported")],
)
def test_failed_or_unsupported_spark_assets_still_block_cold_preview(
    asset_name: str, target_state: str
) -> None:
    row = replace(
        _row(),
        model_license_refs=({"content_sha256": "d" * 64},),
    )
    preparation = _rollout_preparation()
    asset = preparation[asset_name]
    assert isinstance(asset, dict)
    targets = asset["targets"]
    assert isinstance(targets, list)
    target = targets[0]
    assert isinstance(target, dict)
    target["state"] = target_state
    target["reason"] = "The target asset failed its prior preparation."

    with pytest.raises(QualificationError, match="not eligible for exact transfer"):
        campaign_cli._validate_preparations(
            {"preparations": [{"preparation": preparation}]},
            row,
            [NODE_A],
        )


def test_ready_spark_model_must_match_the_controller_artifact_digest() -> None:
    row = replace(
        _row(),
        model_license_refs=({"content_sha256": "d" * 64},),
    )
    preparation = _rollout_preparation(model_state="ready", image_state="ready")
    model = preparation["model"]
    assert isinstance(model, dict)
    targets = model["targets"]
    assert isinstance(targets, list)
    target = targets[0]
    assert isinstance(target, dict)
    target["verified_sha256"] = "0" * 64

    with pytest.raises(QualificationError, match="mismatched model verification"):
        campaign_cli._validate_preparations(
            {"preparations": [{"preparation": preparation}]},
            row,
            [NODE_A],
        )


class _NoRequests:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, path: str, *args: object, **kwargs: object):
        self.calls.append((method, path))
        raise AssertionError("this boundary must run before a Controller request")


def test_apply_requires_campaign_digest_before_constructing_controller_client_or_reading_inputs(
    tmp_path: Path,
) -> None:
    def unexpected_client() -> _NoRequests:
        pytest.fail("apply must fail before creating a Controller client")

    with pytest.raises(SystemExit):
        campaign_cli.run(
            [
                "--manifest",
                str(tmp_path / "missing-campaign.json"),
                "--library-root",
                str(tmp_path / "missing-library"),
                "--ledger",
                str(tmp_path / "evidence.jsonl"),
                "--profile-number",
                "7",
                "--spark",
                NODE_A,
                "--apply",
            ],
            client_factory=unexpected_client,
        )


class _LoadRequestClient:
    def __init__(self, *results: dict[str, object] | Exception) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, str, object]] = []

    def request(
        self,
        method: str,
        path: str,
        payload: object = None,
        **_kwargs: object,
    ) -> dict[str, object]:
        self.calls.append((method, path, payload))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _load_application(
    *, request_key: str, plan_digest: str, profile_id: str, profile_digest: str
) -> dict[str, object]:
    return {
        "id": "22345678-1234-4234-8234-123456789abc",
        "profile_id": profile_id,
        "profile_digest": profile_digest,
        "plan_digest": campaign_cli._application_plan_digest(plan_digest, request_key),
    }


def test_submit_load_looks_up_first_and_posts_exact_reviewed_digest() -> None:
    request_key = campaign_cli._request_key(CAMPAIGN_ID, RECIPE_KEY, "load")
    plan_digest = "b" * 64
    profile_id = "12345678-1234-4123-8123-123456789abc"
    profile_digest = "c" * 64
    client = _LoadRequestClient(
        campaign_cli.ControlNotFound(404, "not found"),
        _load_application(
            request_key=request_key,
            plan_digest=plan_digest,
            profile_id=profile_id,
            profile_digest=profile_digest,
        ),
    )

    accepted = campaign_cli._submit_load(
        client,
        7,
        request_key,
        plan_digest=plan_digest,
        profile_id=profile_id,
        profile_digest=profile_digest,
    )

    assert accepted["id"] == "22345678-1234-4234-8234-123456789abc"
    assert client.calls == [
        ("GET", f"/api/profile/7/requests/{request_key}", None),
        (
            "POST",
            "/api/profile/7/load",
            {"plan_digest": plan_digest, "request_key": request_key},
        ),
    ]


def test_submit_load_adopts_existing_exact_application_without_post() -> None:
    request_key = campaign_cli._request_key(CAMPAIGN_ID, RECIPE_KEY, "load")
    plan_digest = "b" * 64
    profile_id = "12345678-1234-4123-8123-123456789abc"
    profile_digest = "c" * 64
    client = _LoadRequestClient(
        _load_application(
            request_key=request_key,
            plan_digest=plan_digest,
            profile_id=profile_id,
            profile_digest=profile_digest,
        )
    )

    accepted = campaign_cli._submit_load(
        client,
        7,
        request_key,
        plan_digest=plan_digest,
        profile_id=profile_id,
        profile_digest=profile_digest,
    )

    assert accepted["plan_digest"] == campaign_cli._application_plan_digest(
        plan_digest, request_key
    )
    assert [method for method, _path, _payload in client.calls] == ["GET"]


def test_ambiguous_load_retries_only_after_lookup_and_reuses_exact_identity() -> None:
    request_key = campaign_cli._request_key(CAMPAIGN_ID, RECIPE_KEY, "load")
    plan_digest = "b" * 64
    profile_id = "12345678-1234-4123-8123-123456789abc"
    profile_digest = "c" * 64
    payload = {"plan_digest": plan_digest, "request_key": request_key}
    client = _LoadRequestClient(
        campaign_cli.ControlNotFound(404, "not found"),
        campaign_cli.ControlTransportError("response lost"),
        campaign_cli.ControlNotFound(404, "not found"),
        _load_application(
            request_key=request_key,
            plan_digest=plan_digest,
            profile_id=profile_id,
            profile_digest=profile_digest,
        ),
    )

    accepted = campaign_cli._submit_load(
        client,
        7,
        request_key,
        plan_digest=plan_digest,
        profile_id=profile_id,
        profile_digest=profile_digest,
    )

    assert accepted["id"] == "22345678-1234-4234-8234-123456789abc"
    assert [method for method, _path, _payload in client.calls] == [
        "GET",
        "POST",
        "GET",
        "POST",
    ]
    assert client.calls[1][2] == payload
    assert client.calls[3][2] == payload


def test_ambiguous_load_does_not_replay_when_request_lookup_is_unavailable() -> None:
    request_key = campaign_cli._request_key(CAMPAIGN_ID, RECIPE_KEY, "load")
    client = _LoadRequestClient(
        campaign_cli.ControlNotFound(404, "not found"),
        campaign_cli.ControlTransportError("response lost"),
        campaign_cli.ControlUnavailable(503, "request lookup unavailable"),
    )

    with pytest.raises(campaign_cli.ControlTransportError, match="response lost"):
        campaign_cli._submit_load(
            client,
            7,
            request_key,
            plan_digest="b" * 64,
            profile_id="12345678-1234-4123-8123-123456789abc",
            profile_digest="c" * 64,
        )

    assert [method for method, _path, _payload in client.calls] == [
        "GET",
        "POST",
        "GET",
    ]


def test_submit_load_rejects_request_lookup_with_a_different_bound_plan() -> None:
    request_key = campaign_cli._request_key(CAMPAIGN_ID, RECIPE_KEY, "load")
    plan_digest = "b" * 64
    profile_id = "12345678-1234-4123-8123-123456789abc"
    profile_digest = "c" * 64
    client = _LoadRequestClient(
        _load_application(
            request_key=request_key,
            plan_digest="d" * 64,
            profile_id=profile_id,
            profile_digest=profile_digest,
        )
    )

    with pytest.raises(QualificationError, match="exact reviewed plan and request"):
        campaign_cli._submit_load(
            client,
            7,
            request_key,
            plan_digest=plan_digest,
            profile_id=profile_id,
            profile_digest=profile_digest,
        )
    assert [method for method, _path, _payload in client.calls] == ["GET"]


def _append_single_recipe_evidence(
    ledger: EvidenceLedger,
    *,
    include_offline: bool = True,
    same_boot: bool = False,
    cleanup_before_baseline: bool = True,
) -> None:
    ledger.append(
        "plan.generated",
        plan_digest=CAMPAIGN_ID,
        recipe=RECIPE_KEY,
        payload={
            "authority_row": dict(_row().raw),
            "controller_recipe_identity": {
                "content_sha256": CONTENT_SHA,
                "recipe_revision_id": REVISION_ID,
            },
            "profile_digest": "profile-digest",
            "preview": {"plan_digest": "profile-plan", "exact_preparations": {}},
            "smoke_preview": {
                "kind": "openai-service",
                "endpoint_alias": "test-alias",
                "fixture_manifest_sha256": "e" * 64,
                "cases": [{"id": "health"}],
            },
        },
    )
    request_key = campaign_cli._request_key(CAMPAIGN_ID, RECIPE_KEY, "load")
    ledger.append(
        "profile.load.requested",
        plan_digest=CAMPAIGN_ID,
        recipe=RECIPE_KEY,
        payload={"request_key": request_key, "plan_digest": "profile-plan"},
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
                "plan_digest": campaign_cli._application_plan_digest(
                    "profile-plan", request_key
                ),
            },
            "exact_preparations": {},
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
    cleanup_payload = {
        "application_id": "cleanup-application",
        "application_state": "succeeded",
        "cleanup_policy": "stop",
        "uninstalls": 0,
        "route_withdrawn": True,
        "run_absent_from_fleet": True,
        "run_id": RUN_ID,
        "alias": "test-alias",
        "node_ids": [NODE_A],
    }
    baseline_payload = {
        "nodes": {NODE_A: "boot-before"},
        "route_alias": "test-alias",
        "run_id": RUN_ID,
    }
    ordered_events = (
        (
            ("profile.cleanup.completed", cleanup_payload),
            ("host-restart.baseline", baseline_payload),
        )
        if cleanup_before_baseline
        else (
            ("host-restart.baseline", baseline_payload),
            ("profile.cleanup.completed", cleanup_payload),
        )
    )
    for event, payload in ordered_events:
        ledger.append(
            event, plan_digest=CAMPAIGN_ID, recipe=RECIPE_KEY, payload=payload
        )
    if include_offline:
        ledger.append(
            "host-restart.offline",
            plan_digest=CAMPAIGN_ID,
            recipe=RECIPE_KEY,
            payload={
                "node_id": NODE_A,
                "online_state": "offline",
                "baseline_boot_id": "boot-before",
            },
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


def _append_two_node_recipe_evidence(
    ledger: EvidenceLedger,
    *,
    recovered_node_to_rank: Mapping[str, int] | None = None,
) -> None:
    row = _row(node_count=2)
    alias = "test-alias"
    fixture_digest = "e" * 64
    node_to_rank = {NODE_A: 0, NODE_B: 1}
    recovered_node_to_rank = recovered_node_to_rank or node_to_rank
    request_key = campaign_cli._request_key(CAMPAIGN_ID, RECIPE_KEY, "load")
    preview = {"plan_digest": "profile-plan", "exact_preparations": {}}
    smoke_preview = {
        "kind": "openai-service",
        "endpoint_alias": alias,
        "fixture_manifest_sha256": fixture_digest,
        "cases": [{"case_id": "health"}],
    }
    ledger.append(
        "plan.generated",
        plan_digest=CAMPAIGN_ID,
        recipe=RECIPE_KEY,
        payload={
            "authority_row": dict(row.raw),
            "controller_recipe_identity": {
                "content_sha256": CONTENT_SHA,
                "recipe_revision_id": REVISION_ID,
            },
            "profile_digest": "profile-digest",
            "preview": preview,
            "smoke_preview": smoke_preview,
        },
    )
    ledger.append(
        "profile.load.requested",
        plan_digest=CAMPAIGN_ID,
        recipe=RECIPE_KEY,
        payload={"request_key": request_key},
    )

    def presence(
        node_id: str, rank: int, *, recovered: bool = False
    ) -> dict[str, object]:
        result = _loaded_run_presence(rank=rank)
        result.update(
            {
                "node_id": node_id,
                "alias": alias,
                "run_id": RUN_ID,
                "recipe_revision_id": REVISION_ID,
                "member_node_ids": [NODE_A, NODE_B],
            }
        )
        if recovered:
            result.update(
                {"route_state": "published", "group_state": "healthy", "healthy": True}
            )
        return result

    ledger.append(
        "canary.completed",
        plan_digest=CAMPAIGN_ID,
        recipe=RECIPE_KEY,
        payload={
            "recipe_content_sha256": CONTENT_SHA,
            "alias": alias,
            "run_id": RUN_ID,
            "recipe_revision_id": REVISION_ID,
            "node_to_rank": node_to_rank,
            "fleet_rank_presence": [
                presence(node_id, rank) for node_id, rank in node_to_rank.items()
            ],
            "application": {
                "state": "succeeded",
                "profile_digest": "profile-digest",
                "plan_digest": campaign_cli._application_plan_digest(
                    "profile-plan", request_key
                ),
            },
            "exact_preparations": {},
            "smoke": {
                "fixture_manifest_sha256": fixture_digest,
                "cases": [{"case_id": "health"}],
            },
            "review_acknowledgements": {
                "operator_acceptance": False,
                "capacity_review": False,
            },
        },
    )
    ledger.append(
        "rank-loss.observed",
        plan_digest=CAMPAIGN_ID,
        recipe=RECIPE_KEY,
        payload={
            "failure_node_id": NODE_B,
            "failure_rank": 1,
            "expected_present_ranks": [0],
            "survivors": [
                {
                    "rank": 0,
                    "group_state": "degraded",
                    "route_state": "withdrawn",
                    "healthy": False,
                    "expected_rank_count": 2,
                    "present_ranks": [0],
                }
            ],
            "failed_rank_presence": [
                {
                    "rank": 1,
                    "rank_state": "stopped",
                    "recipe_revision_id": REVISION_ID,
                    "expected_rank_count": 2,
                    "group_state": "degraded",
                    "route_state": "withdrawn",
                    "healthy": False,
                }
            ],
            "failed_node_online_state": "offline",
            "endpoint_not_found": True,
        },
    )
    ledger.append(
        "rank-recovery.smoke-completed",
        plan_digest=CAMPAIGN_ID,
        recipe=RECIPE_KEY,
        payload={
            "run_id": RUN_ID,
            "recipe_revision_id": REVISION_ID,
            "fleet_rank_presence": [
                presence(node_id, rank, recovered=True)
                for node_id, rank in recovered_node_to_rank.items()
            ],
            "smoke": {
                "endpoint_alias": alias,
                "recipe_content_sha256": CONTENT_SHA,
                "cases": [{"case_id": "health"}],
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
            "run_id": RUN_ID,
            "alias": alias,
            "node_ids": [NODE_A, NODE_B],
        },
    )
    ledger.append(
        "host-restart.baseline",
        plan_digest=CAMPAIGN_ID,
        recipe=RECIPE_KEY,
        payload={
            "nodes": {NODE_A: "boot-a-before", NODE_B: "boot-b-before"},
            "route_alias": alias,
            "run_id": RUN_ID,
        },
    )
    for node_id, baseline in (
        (NODE_A, "boot-a-before"),
        (NODE_B, "boot-b-before"),
    ):
        ledger.append(
            "host-restart.offline",
            plan_digest=CAMPAIGN_ID,
            recipe=RECIPE_KEY,
            payload={
                "node_id": node_id,
                "online_state": "offline",
                "baseline_boot_id": baseline,
            },
        )
        ledger.append(
            "host-restart.recovered",
            plan_digest=CAMPAIGN_ID,
            recipe=RECIPE_KEY,
            payload={
                "node_id": node_id,
                "online_state": "online",
                "baseline_boot_id": baseline,
                "observed_boot_id": f"{baseline}-after",
                "telemetry_freshness": "live",
            },
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
        runtime_stack_sha256="b" * 64,
        topology_sha256="c" * 64,
        recovery_coverage_refs=(),
        raw={},
    )
    preview = campaign_cli._arguments(
        [
            "--manifest",
            "campaign.json",
            "--library-root",
            "recipes",
            "--ledger",
            "evidence.jsonl",
            "--profile-number",
            "7",
            "--spark",
            NODE_A,
        ]
    )
    campaign_cli._operator_gate(preview, [row])
    with pytest.raises(QualificationError, match="accept-operator-gate"):
        campaign_cli._operator_gate(
            Namespace(apply=True, accept_operator_gate=[], accept_capacity_review=[]),
            [row],
        )
    missing_capacity = Namespace(
        apply=True, accept_operator_gate=[RECIPE_KEY], accept_capacity_review=[]
    )
    with pytest.raises(QualificationError, match="accept-capacity-review"):
        campaign_cli._operator_gate(missing_capacity, [row])

    accepted = Namespace(
        apply=True,
        accept_operator_gate=[RECIPE_KEY],
        accept_capacity_review=[RECIPE_KEY],
    )
    campaign_cli._operator_gate(accepted, [row])


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


PAIRED_PROFILE_ID = "12345678-1234-4123-8123-123456789abc"
PAIRED_PROFILE_DIGEST = "d" * 64
PAIRED_PLAN_DIGEST = "e" * 64
PAIRED_FIXTURE_DIGEST = "f" * 64


def _paired_batch_inputs(
    node_order: tuple[str, str] = (NODE_A, NODE_B),
) -> tuple[campaign_cli.CampaignBatch, tuple[campaign_cli.BatchLane, ...]]:
    base = _row()
    lanes: list[campaign_cli.BatchLane] = []
    assignments: list[campaign_cli.BatchAssignment] = []
    for lane_number, (slug, content_digest, package_digest, node_id) in enumerate(
        (
            ("first", "1" * 64, "2" * 64, node_order[0]),
            ("second", "3" * 64, "4" * 64, node_order[1]),
        ),
        start=1,
    ):
        recipe = f"vonk-forge/test-{slug}-lane"
        assignment = campaign_cli.BatchAssignment(recipe, lane_number, 1)
        raw = {
            **dict(base.raw),
            "sequence": lane_number,
            "key": recipe,
            "content_sha256": content_digest,
            "package": {"sha256": package_digest},
        }
        row = replace(
            base,
            sequence=lane_number,
            key=recipe,
            content_sha256=content_digest,
            package={"sha256": package_digest},
            raw=raw,
        )
        alias = f"paired-{lane_number}"
        lanes.append(
            campaign_cli.BatchLane(
                assignment=assignment,
                row=row,
                node_ids=(node_id,),
                alias=alias,
                smoke_kind="openai-service",
                detail={"identity": {"recipe_id": f"controller-recipe-{lane_number}"}},
                smoke_preview={
                    "available": True,
                    "endpoint_alias": alias,
                    "fixture_manifest_sha256": PAIRED_FIXTURE_DIGEST,
                    "recipe_content_sha256": content_digest,
                    "cases": [{"case_id": "health"}],
                },
            )
        )
        assignments.append(assignment)
    batch_raw: dict[str, object] = {
        "sequence": 1,
        "id": "batch-001",
        "mode": "paired-single",
        "assignments": [
            {
                "recipe": item.recipe,
                "lane": item.lane,
                "node_count": item.node_count,
            }
            for item in assignments
        ],
    }
    batch = campaign_cli.CampaignBatch(
        sequence=1,
        batch_id="batch-001",
        mode="paired-single",
        assignments=tuple(assignments),
        raw=batch_raw,
    )
    return batch, tuple(lanes)


def _paired_preview(
    lanes: Sequence[campaign_cli.BatchLane],
) -> dict[str, object]:
    return {
        "profile_id": PAIRED_PROFILE_ID,
        "profile_digest": PAIRED_PROFILE_DIGEST,
        "plan_digest": PAIRED_PLAN_DIGEST,
        "scope": {"node_ids": sorted(node for lane in lanes for node in lane.node_ids)},
        "assignments": [
            {
                "assignment_id": f"assignment-{lane.assignment.lane}",
                "node_ids": list(lane.node_ids),
                "desired_state": "running",
            }
            for lane in lanes
        ],
        "lane_revision_ids": {
            lane.row.key: f"recipe-revision-{lane.assignment.lane}" for lane in lanes
        },
        "exact_preparations": {lane.row.key: {} for lane in lanes},
    }


def _paired_manifest(
    rows: Sequence[campaign_cli.RecipeAuthorityRow],
    batches: Sequence[campaign_cli.CampaignBatch],
) -> campaign_cli.CampaignManifest:
    authority = campaign_cli.CampaignAuthority(
        authority_id="paired-test-authority",
        sha256="a" * 64,
        catalog={"recipe_count": len(rows)},
        max_node_count=2,
        excluded_topology_recipe_keys=(),
        rows=tuple(rows),
        batches=tuple(batches),
        recovery_coverage=(),
        raw={},
    )
    return campaign_cli.CampaignManifest(
        path=Path("paired-campaign.json"),
        sha256="b" * 64,
        authority=authority,
        fixture_manifest=Path("qualification-index.json"),
        cleanup="stop",
        operation_timeout_seconds=5.0,
        poll_interval_seconds=0.01,
    )


def _paired_fleet(
    lanes: Sequence[campaign_cli.BatchLane],
) -> dict[str, object]:
    nodes: list[dict[str, object]] = []
    for lane in lanes:
        lane_number = lane.assignment.lane
        node_id = lane.node_ids[0]
        nodes.append(
            _node(
                node_id,
                loaded=[
                    {
                        "alias": lane.alias,
                        "expected_rank_count": 1,
                        "group_state": "healthy",
                        "healthy": True,
                        "installation_id": f"installation-{lane_number}",
                        "member_node_ids": [node_id],
                        "present_ranks": [0],
                        "rank": 0,
                        "rank_age_seconds": 1.0,
                        "rank_fresh": True,
                        "rank_state": "running",
                        "recipe_id": f"controller-recipe-{lane_number}",
                        "recipe_revision_id": f"recipe-revision-{lane_number}",
                        "role": "rank-0",
                        "route_state": "published",
                        "run_id": f"run-{lane_number}",
                        "run_state": "running",
                        "title": f"Paired lane {lane_number}",
                    }
                ],
            )
        )
    return {"nodes": nodes}


def _typed_paired_fleet(
    lanes: Sequence[campaign_cli.BatchLane],
    *,
    active_runs: Mapping[int, str] | None = None,
    boot_ids: Mapping[str, str] | None = None,
    offline_nodes: Sequence[str] = (),
    cursor: int = 1,
) -> dict[str, object]:
    """Build the same whole-Fleet evidence from the generated public contract."""
    active = active_runs if active_runs is not None else {1: "run-1", 2: "run-2"}
    boots = (
        boot_ids
        if boot_ids is not None
        else {NODE_A: "boot-before", NODE_B: "boot-before"}
    )
    timestamp = f"2026-09-25T00:{cursor // 60:02d}:{cursor % 60:02d}Z"
    nodes: list[dict[str, object]] = []
    for node_id in (NODE_A, NODE_B):
        lane = next(item for item in lanes if item.node_ids[0] == node_id)
        lane_number = lane.assignment.lane
        run_id = active.get(lane_number)
        loaded: list[dict[str, object]] = []
        if run_id is not None:
            loaded.append(
                {
                    "alias": lane.alias,
                    "expected_rank_count": 1,
                    "group_state": "healthy",
                    "healthy": True,
                    "installation_id": f"installation-{lane_number}",
                    "member_node_ids": [node_id],
                    "present_ranks": [0],
                    "rank": 0,
                    "rank_age_seconds": 1.0,
                    "rank_fresh": True,
                    "rank_state": "running",
                    "recipe_id": f"controller-recipe-{lane_number}",
                    "recipe_revision_id": f"recipe-revision-{lane_number}",
                    "role": "rank-0",
                    "route_state": "published",
                    "run_id": run_id,
                    "run_state": "running",
                    "title": f"Paired lane {lane_number}",
                }
            )
        nodes.append(
            {
                "connection": {
                    "agent_state": "active",
                    "certificate_state": "valid",
                    "last_seen_age_seconds": 1.0,
                    "last_seen_at": timestamp,
                    "offline_reason": None,
                    "online_state": "offline" if node_id in offline_nodes else "online",
                },
                "display_name": node_id,
                "hostname": f"spark-{lane_number}",
                "id": node_id,
                "installed": [],
                "inventory": None,
                "labels": {},
                "lifecycle": "active",
                "loaded": loaded,
                "reservations": {
                    "disk_bytes": 0,
                    "gpu_memory_bytes": 0,
                    "host_memory_bytes": 0,
                    "port_count": 0,
                    "unified_memory_bytes": 0,
                },
                "telemetry": {
                    "age_seconds": 1.0,
                    "freshness": "live",
                    "sample": {
                        "boot_id": boots.get(node_id, "boot-before"),
                        "details": {},
                        "gap_samples": 0,
                        "id": f"telemetry-{node_id}-{cursor}",
                        "metrics": {
                            "capabilities": [],
                            "provenance": {
                                "collector": "qualification-test",
                                "collector_version": "1",
                            },
                            "runtimes": [],
                            "schema_version": 2,
                            "series": [],
                            "workloads": [],
                        },
                        "node_id": node_id,
                        "observed_at": timestamp,
                        "received_at": timestamp,
                    },
                },
                "warnings": [],
            }
        )
    return campaign_cli.FleetSnapshot.from_dict(
        {
            "authority_revision": "a" * 64,
            "event_cursor": cursor,
            "generated_at": timestamp,
            "nodes": nodes,
            "schema_version": 1,
        }
    ).to_dict()


def _paired_lane_finals(
    lanes: Sequence[campaign_cli.BatchLane],
) -> dict[str, Mapping[str, object]]:
    return {
        lane.row.key: {
            "run_id": f"run-{lane.assignment.lane}",
            "recipe_revision_id": f"recipe-revision-{lane.assignment.lane}",
            "ranks": [{"node_id": lane.node_ids[0], "rank": 0}],
        }
        for lane in lanes
    }


def _paired_final_application(
    lanes: Sequence[campaign_cli.BatchLane],
    *,
    wrong_revision_lane: int | None = None,
    wrong_node_lane: int | None = None,
    duplicate_run_id: bool = False,
) -> tuple[dict[str, object], dict[str, object]]:
    preview = _paired_preview(lanes)
    assignment_ids = [f"assignment-{lane.assignment.lane}" for lane in lanes]
    intended_assignments: list[dict[str, object]] = []
    children: list[dict[str, object]] = []
    for lane in lanes:
        lane_number = lane.assignment.lane
        revision_id = f"recipe-revision-{lane_number}"
        if wrong_revision_lane == lane_number:
            revision_id = "another-recipe-revision"
        intended_assignments.append(
            {
                "id": f"assignment-{lane_number}",
                "recipe_revision_id": revision_id,
                "nodes": [{"node_id": lane.node_ids[0]}],
            }
        )
        rank_node_id = lane.node_ids[0]
        if wrong_node_lane == lane_number:
            rank_node_id = NODE_A if rank_node_id == NODE_B else NODE_B
        run_id = f"run-{lane_number}"
        if duplicate_run_id and lane_number == 2:
            run_id = "run-1"
        children.append(
            {
                "kind": "run",
                "result": {
                    "run_switch": {
                        "item_index": lane_number - 1,
                        "profile_application_id": "paired-application",
                        "phase": "final_verify",
                        "run_id": run_id,
                        "final_verified": True,
                        "healthy": True,
                        "state": "running",
                        "route_state": "published",
                        "ranks": [{"node_id": rank_node_id, "rank": 0}],
                    }
                },
            }
        )
    application = {
        "id": "paired-application",
        "progress": {
            "intended_profile": {"assignments": intended_assignments},
            "step_results": {
                "profile.switch": {
                    "result": {"assignment_ids": assignment_ids, "children": children}
                }
            },
        },
    }
    return application, preview


def _paired_failed_canary_references(
    batch: campaign_cli.CampaignBatch,
    lanes: Sequence[campaign_cli.BatchLane],
    ledger: EvidenceLedger,
) -> tuple[campaign_cli.CanaryReference, ...]:
    references: list[campaign_cli.CanaryReference] = []
    for lane in lanes:
        lane_number = lane.assignment.lane
        node_id = lane.node_ids[0]
        run_id = f"run-{lane_number}"
        revision_id = f"recipe-revision-{lane_number}"
        payload = {
            "batch_id": batch.batch_id,
            "lane_id": lane_number,
            "node_ids": [node_id],
            "assigned_node_id": node_id,
            "assigned_rank": 0,
            "node_to_rank": {node_id: 0},
            "run_id": run_id,
            "recipe_revision_id": revision_id,
            "alias": lane.alias,
            "recipe_content_sha256": lane.row.content_sha256,
            "package_sha256": lane.row.package["sha256"],
            "error": "fixture smoke failed after the run was applied",
        }
        record = ledger.append(
            "canary.failed",
            plan_digest=CAMPAIGN_ID,
            recipe=lane.row.key,
            payload=payload,
        )
        references.append(
            campaign_cli.CanaryReference(
                lane_id=lane_number,
                record_sha256=str(record["record_sha256"]),
                recipe_key=lane.row.key,
                recipe_content_sha256=lane.row.content_sha256,
                package_sha256=str(lane.row.package["sha256"]),
                assigned_node_id=node_id,
                assigned_rank=0,
                run_id=run_id,
                recipe_revision_id=revision_id,
                alias=lane.alias,
                node_to_rank={node_id: 0},
                outcome_event="canary.failed",
            )
        )
    return tuple(references)


def _paired_failed_lane_target(
    batch: campaign_cli.CampaignBatch,
    lanes: Sequence[campaign_cli.BatchLane],
    references: Sequence[campaign_cli.CanaryReference],
    lane_number: int,
) -> campaign_cli.LaneRecoveryTarget:
    lane = next(item for item in lanes if item.assignment.lane == lane_number)
    own = next(item for item in references if item.lane_id == lane_number)
    partner = [item for item in references if item.lane_id != lane_number]
    return campaign_cli.LaneRecoveryTarget(
        campaign_id=CAMPAIGN_ID,
        batch_id=batch.batch_id,
        lane_id=lane_number,
        recipe_key=lane.row.key,
        recipe_content_sha256=lane.row.content_sha256,
        package_sha256=str(lane.row.package["sha256"]),
        original_run_id=str(own.run_id),
        recipe_revision_id=str(own.recipe_revision_id),
        alias=str(own.alias),
        node_id=own.assigned_node_id,
        node_to_rank=dict(own.node_to_rank or {own.assigned_node_id: 0}),
        smoke_case_ids=("health",),
        fleet_node_ids=tuple(sorted(node for item in lanes for node in item.node_ids)),
        partner_run_ids=tuple(sorted(str(item.run_id) for item in partner)),
        partner_aliases=tuple(sorted(str(item.alias) for item in partner)),
        canaries=tuple(references),
        profile_number=7,
        profile_id=PAIRED_PROFILE_ID,
        profile_digest=PAIRED_PROFILE_DIGEST,
        plan_digest=PAIRED_PLAN_DIGEST,
        allow_reactivation=True,
    )


def _canonical_profile_application_with_stops(
    run_ids: Sequence[str],
) -> dict[str, object]:
    children = [
        {
            "kind": "stop",
            "operation_id": f"operation-{index}",
            "state": "succeeded",
            "result": {
                "run_switch_operation_id": f"operation-{index}",
                "run_switch": {
                    "final_observation": {
                        "phase": "final_verify",
                        "final_verified": True,
                        "healthy": False,
                        "ranks": [
                            {
                                "node_id": f"spk_{index:032d}",
                                "rank": 0,
                                "role": "rank-0",
                                "state": "stopped",
                            }
                        ],
                        "route_state": "withdrawn",
                        "run_id": run_id,
                        "state": "stopped",
                    }
                },
            },
        }
        for index, run_id in enumerate(run_ids, start=1)
    ]
    application = campaign_cli.FleetProfileApplicationView.from_dict(
        {
            "created_at": "2026-09-25T00:00:00Z",
            "current_operation_id": None,
            "current_step": 1,
            "id": "application-1",
            "plan_digest": PAIRED_PLAN_DIGEST,
            "profile_digest": PAIRED_PROFILE_DIGEST,
            "profile_id": PAIRED_PROFILE_ID,
            "progress": {
                "switch_adapter": {
                    "actor": "admin",
                    "assignment_ids": [],
                    "assignments": [],
                    "child_id": "profile-switch-1",
                    "queue": [],
                    "request_id": "profile-request-1",
                    "scope_node_ids": [NODE_A, NODE_B],
                    "state": "succeeded",
                    "result": {"assignment_ids": [], "children": children},
                }
            },
            "request_key": "request-key",
            "result": None,
            "state": "succeeded",
            "status_reason": None,
            "total_steps": 1,
            "updated_at": "2026-09-25T00:00:01Z",
        }
    )
    return application.to_dict()


def _paired_application_identity(
    batch: campaign_cli.CampaignBatch,
) -> dict[str, object]:
    request_key = campaign_cli._request_key(CAMPAIGN_ID, batch.batch_id, "load")
    return {
        "id": "paired-application",
        "profile_id": PAIRED_PROFILE_ID,
        "profile_digest": PAIRED_PROFILE_DIGEST,
        "plan_digest": campaign_cli._application_plan_digest(
            PAIRED_PLAN_DIGEST, request_key
        ),
        "request_key": request_key,
    }


def test_generated_family_authority_is_consumable_with_complete_scope_accounting() -> (
    None
):
    library_root = Path(os.environ["VONK_RECIPE_LIBRARY_ROOT"]).resolve()
    authority_path = (
        library_root / "qualification/authorities/nl-family-aware-20260924.json"
    )
    authority = campaign_cli._load_authority(authority_path)

    assert authority.sha256 == hashlib.sha256(authority_path.read_bytes()).hexdigest()
    scope = campaign_cli._object(authority.raw["scope"], "authority scope")
    catalog = campaign_cli._object(authority.raw["catalog"], "authority catalog")
    assert len(authority.rows) == scope["recipe_count"]
    assert catalog["recipe_count"] == (
        len(authority.rows) + len(authority.excluded_topology_recipe_keys)
    )
    assignment_recipes = [
        assignment.recipe
        for batch in authority.batches
        for assignment in batch.assignments
    ]
    assert len(assignment_recipes) == len(authority.rows)
    assert set(assignment_recipes) == {row.key for row in authority.rows}
    assert len(set(assignment_recipes)) == len(assignment_recipes)
    paired_batch = next(
        batch for batch in authority.batches if batch.mode == "paired-single"
    )
    assert [item.lane for item in paired_batch.assignments] == [1, 2]
    assert [item.node_count for item in paired_batch.assignments] == [1, 1]


def test_generated_family_authority_rejects_unreferenced_recovery_definition(
    tmp_path: Path,
) -> None:
    library_root = Path(os.environ["VONK_RECIPE_LIBRARY_ROOT"]).resolve()
    authority_path = (
        library_root / "qualification/authorities/nl-family-aware-20260924.json"
    )
    authority = json.loads(authority_path.read_text(encoding="utf-8"))
    recovery_definitions = authority["recovery_coverage"]
    extra_definition = dict(recovery_definitions[0])
    extra_definition["equivalence_rationale"] = (
        "A second valid but unreferenced recovery definition."
    )
    unsigned_definition = {
        key: value for key, value in extra_definition.items() if key != "coverage_id"
    }
    extra_definition["coverage_id"] = campaign_cli._contract_digest(unsigned_definition)
    recovery_definitions.append(extra_definition)
    mutated_authority = tmp_path / authority_path.name
    mutated_authority.write_text(json.dumps(authority), encoding="utf-8")

    with pytest.raises(
        QualificationError,
        match="recovery definition includes a member with no exact row reference",
    ):
        campaign_cli._load_authority(mutated_authority)


def test_repeated_spark_ids_bind_to_ordered_disjoint_batch_lanes() -> None:
    batch, lanes = _paired_batch_inputs()
    selected = campaign_cli._exact_batch_nodes(
        object(),
        {"nodes": [_node(NODE_A), _node(NODE_B)]},
        batch,
        [NODE_B, NODE_A],
        {lane.row.key: lane.row for lane in lanes},
    )

    assert selected == {lanes[0].row.key: (NODE_B,), lanes[1].row.key: (NODE_A,)}
    with pytest.raises(QualificationError, match="exactly 2 distinct --spark IDs"):
        campaign_cli._exact_batch_nodes(
            object(),
            {"nodes": [_node(NODE_A), _node(NODE_B)]},
            batch,
            [NODE_A, NODE_A],
            {lane.row.key: lane.row for lane in lanes},
        )


def test_batch_plan_digest_changes_when_lane_spark_mapping_changes() -> None:
    batch, lanes = _paired_batch_inputs()
    manifest = _paired_manifest([lane.row for lane in lanes], [batch])
    fixtures = SimpleNamespace(manifest_sha256=PAIRED_FIXTURE_DIGEST)
    profile = {"id": PAIRED_PROFILE_ID, "profile_digest": PAIRED_PROFILE_DIGEST}
    preview = _paired_preview(lanes)
    digest = campaign_cli._batch_preview_digest(
        manifest=manifest,
        fixtures=cast(FixtureRegistry, fixtures),
        batch=batch,
        lanes=lanes,
        profile=profile,
        preview=preview,
        profile_number=7,
        failure_node_id=None,
    )
    swapped = (
        replace(lanes[0], node_ids=(NODE_B,)),
        replace(lanes[1], node_ids=(NODE_A,)),
    )
    swapped_digest = campaign_cli._batch_preview_digest(
        manifest=manifest,
        fixtures=cast(FixtureRegistry, fixtures),
        batch=batch,
        lanes=swapped,
        profile=profile,
        preview=preview,
        profile_number=7,
        failure_node_id=None,
    )

    assert digest != swapped_digest


def test_stale_batch_digest_blocks_load_for_every_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch, lanes = _paired_batch_inputs()
    fleet = _paired_fleet(lanes)
    fixtures = cast(FixtureRegistry, SimpleNamespace())
    fixture_path = tmp_path / "qualification-index.json"
    fixture_path.write_text("{}", encoding="utf-8")
    manifest = replace(
        _paired_manifest([lane.row for lane in lanes], [batch]),
        path=tmp_path / "campaign.json",
        fixture_manifest=fixture_path,
    )
    ledger_path = tmp_path / "stale-batch.jsonl"
    fresh_metadata = {"campaign_digest": "c" * 64, "profile": {}}
    load_calls: list[str] = []

    monkeypatch.setattr(campaign_cli, "load_manifest", lambda *_args: manifest)
    monkeypatch.setattr(FixtureRegistry, "load", lambda _path: fixtures)
    monkeypatch.setattr(campaign_cli, "_bind_repository_inputs", lambda *_args: None)
    monkeypatch.setattr(
        campaign_cli, "_resolve_ledger_path", lambda *_args: ledger_path
    )
    monkeypatch.setattr(
        campaign_cli, "_make_campaign_id", lambda *_args: (CAMPAIGN_ID, "ledger-id")
    )
    monkeypatch.setattr(campaign_cli, "_typed_fleet", lambda _client: fleet)
    monkeypatch.setattr(campaign_cli, "node_locks", lambda _nodes: nullcontext())
    monkeypatch.setattr(
        campaign_cli,
        "_fresh_batch_preview",
        lambda **_kwargs: (lanes, _paired_preview(lanes), fresh_metadata),
    )
    monkeypatch.setattr(campaign_cli, "_operator_gate", lambda *_args: None)

    def load_batch(**_kwargs: object) -> object:
        load_calls.append("submitted")
        pytest.fail("a stale whole-batch digest must stop before any lane load")

    monkeypatch.setattr(campaign_cli, "_load_batch_and_smoke", load_batch)

    with pytest.raises(
        QualificationError,
        match="campaign-digest no longer matches the live exact batch preview",
    ):
        campaign_cli.run(
            [
                "--manifest",
                str(manifest.path),
                "--library-root",
                str(tmp_path),
                "--ledger",
                str(ledger_path),
                "--profile-number",
                "7",
                "--batch",
                batch.batch_id,
                "--spark",
                NODE_A,
                "--spark",
                NODE_B,
                "--apply",
                "--campaign-digest",
                "d" * 64,
            ],
            client_factory=lambda: object(),
        )

    assert load_calls == []


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        ("wrong-revision", "changed a lane revision or Spark group"),
        ("wrong-node", "changed an exact lane"),
        ("duplicate-run", "duplicate run identity"),
    ],
)
def test_batch_application_rejects_wrong_recipe_or_node_receipts(
    tamper: str, message: str
) -> None:
    _batch, lanes = _paired_batch_inputs()
    application, preview = _paired_final_application(
        lanes,
        wrong_revision_lane=2 if tamper == "wrong-revision" else None,
        wrong_node_lane=2 if tamper == "wrong-node" else None,
        duplicate_run_id=tamper == "duplicate-run",
    )

    with pytest.raises(QualificationError, match=message):
        campaign_cli._lane_final_verifications(application, lanes, preview)


def test_paired_preview_refuses_competing_whole_fleet_runs_before_profile_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch, lanes = _paired_batch_inputs()
    fleet = _paired_fleet(lanes)
    fleet_nodes = cast(list[dict[str, object]], fleet["nodes"])
    foreign_load = dict(cast(list[dict[str, object]], fleet_nodes[0]["loaded"])[0])
    foreign_load.update(
        {
            "alias": "foreign-service",
            "installation_id": "foreign-installation",
            "recipe_id": "foreign-recipe",
            "recipe_revision_id": "foreign-revision",
            "run_id": "foreign-run",
            "title": "Unrelated whole-Fleet workload",
        }
    )
    fleet = {
        "nodes": [
            _node(NODE_A, loaded=[foreign_load]),
            _node(NODE_B),
        ]
    }
    manifest = _paired_manifest([lane.row for lane in lanes], [batch])
    fixtures = SimpleNamespace(
        manifest_sha256=PAIRED_FIXTURE_DIGEST,
        service_recipes={
            lane.row.key: SimpleNamespace(alias=lane.alias) for lane in lanes
        },
    )
    profile_writes: list[str] = []

    class PreviewAdapter:
        def __init__(self, _fixtures: object) -> None:
            pass

        def preview(
            self,
            _definition: object,
            alias: str,
            **_kwargs: object,
        ) -> dict[str, object]:
            return {
                "available": True,
                "endpoint_alias": alias,
                "fixture_manifest_sha256": PAIRED_FIXTURE_DIGEST,
            }

    monkeypatch.setattr(
        campaign_cli,
        "_fixture_bindings",
        lambda _row, _fixtures: ("openai-service", {}),
    )
    monkeypatch.setattr(
        campaign_cli,
        "_validate_current_recipe",
        lambda _client, row: (
            {"identity": {"recipe_id": f"recipe-{row.sequence}"}},
            {},
        ),
    )
    monkeypatch.setattr(campaign_cli, "ServiceSmokeAdapter", PreviewAdapter)
    monkeypatch.setattr(campaign_cli, "_typed_fleet", lambda _client: fleet)

    def record_profile_write(**_kwargs: object) -> dict[str, object]:
        profile_writes.append("write")
        pytest.fail("a competing whole-Fleet run must stop before profile mutation")

    monkeypatch.setattr(campaign_cli, "_prepare_batch_profile", record_profile_write)
    selected = {lane.row.key: lane.node_ids for lane in lanes}

    with pytest.raises(QualificationError, match="loaded run is present"):
        campaign_cli._fresh_batch_preview(
            client=object(),
            manifest=manifest,
            fixtures=cast(FixtureRegistry, fixtures),
            batch=batch,
            selected_nodes=selected,
            library_root=tmp_path,
            profile_number=7,
            authority_id=manifest.authority.authority_id,
            ledger_id="ledger-id",
            campaign_id=CAMPAIGN_ID,
            ledger=EvidenceLedger(tmp_path / "competing.jsonl"),
            expected_fleet_node_ids=[NODE_A, NODE_B],
            replace_run_ids=[],
            failure_node_id=None,
        )

    assert profile_writes == []


def test_next_batch_waits_for_whole_fleet_cleanup_after_a_local_lane_failure(
    tmp_path: Path,
) -> None:
    first_batch, lanes = _paired_batch_inputs()
    third_base = lanes[1].row
    third_key = "vonk-forge/next-model"
    third_raw = {**dict(third_base.raw), "sequence": 3, "key": third_key}
    third_row = replace(
        third_base,
        sequence=3,
        key=third_key,
        raw=third_raw,
    )
    second_batch = campaign_cli.CampaignBatch(
        sequence=2,
        batch_id="batch-002",
        mode="single",
        assignments=(campaign_cli.BatchAssignment(third_key, 1, 1),),
        raw={
            "sequence": 2,
            "id": "batch-002",
            "mode": "single",
            "assignments": [{"recipe": third_key, "lane": 1, "node_count": 1}],
        },
    )
    manifest = _paired_manifest(
        [lanes[0].row, lanes[1].row, third_row], [first_batch, second_batch]
    )
    ledger = EvidenceLedger(tmp_path / "batch-cleanup.jsonl")
    ledger.append(
        "profile.load.requested",
        plan_digest=CAMPAIGN_ID,
        payload={
            "batch_id": first_batch.batch_id,
            "request_key": "started-without-cleanup",
            "reason": "paired batch profile apply began",
        },
    )
    ledger.append(
        "recipe.failed",
        plan_digest=CAMPAIGN_ID,
        recipe=lanes[0].row.key,
        payload={"error": "lane-local smoke failed after retry policy was exhausted"},
    )
    ledger.append(
        "recipe.spark-accepted",
        plan_digest=CAMPAIGN_ID,
        recipe=lanes[1].row.key,
        payload={"status": "spark-accepted"},
    )

    with pytest.raises(QualificationError, match="no whole-Fleet cleanup receipt"):
        campaign_cli._current_batch(
            manifest, ledger, CAMPAIGN_ID, second_batch.batch_id
        )

    ledger.append(
        "batch.cleanup.completed",
        plan_digest=CAMPAIGN_ID,
        payload={
            "batch_id": first_batch.batch_id,
            "all_batch_runs_absent": True,
            "all_batch_routes_absent": True,
            "profile_assignments_empty": True,
        },
    )
    assert (
        campaign_cli._current_batch(
            manifest, ledger, CAMPAIGN_ID, second_batch.batch_id
        )
        == second_batch
    )


def _paired_followup_batch(
    lanes: Sequence[campaign_cli.BatchLane],
) -> tuple[campaign_cli.RecipeAuthorityRow, campaign_cli.CampaignBatch]:
    base = lanes[1].row
    key = "vonk-forge/later-representative"
    raw = {**dict(base.raw), "sequence": 3, "key": key}
    row = replace(base, sequence=3, key=key, raw=raw)
    assignment = campaign_cli.BatchAssignment(key, 1, 1)
    batch = campaign_cli.CampaignBatch(
        sequence=2,
        batch_id="batch-002",
        mode="single",
        assignments=(assignment,),
        raw={
            "sequence": 2,
            "id": "batch-002",
            "mode": "single",
            "assignments": [{"recipe": key, "lane": 1, "node_count": 1}],
        },
    )
    return row, batch


def test_fresh_ledger_can_select_later_authority_representative_batch(
    tmp_path: Path,
) -> None:
    first_batch, lanes = _paired_batch_inputs()
    later_row, later_batch = _paired_followup_batch(lanes)
    manifest = _paired_manifest(
        [lanes[0].row, lanes[1].row, later_row], [first_batch, later_batch]
    )

    selected = campaign_cli._current_batch(
        manifest,
        EvidenceLedger(tmp_path / "fresh-later-representative.jsonl"),
        CAMPAIGN_ID,
        later_batch.batch_id,
    )

    assert selected == later_batch


def test_any_started_unreleased_batch_blocks_another_batch_even_at_lower_sequence(
    tmp_path: Path,
) -> None:
    first_batch, lanes = _paired_batch_inputs()
    later_row, later_batch = _paired_followup_batch(lanes)
    manifest = _paired_manifest(
        [lanes[0].row, lanes[1].row, later_row], [first_batch, later_batch]
    )
    ledger = EvidenceLedger(tmp_path / "higher-unreleased-blocks.jsonl")
    ledger.append(
        "profile.load.requested",
        plan_digest=CAMPAIGN_ID,
        recipe=later_row.key,
        payload={"batch_id": later_batch.batch_id, "lane_id": 1},
    )

    with pytest.raises(QualificationError, match="batch-002"):
        campaign_cli._current_batch(manifest, ledger, CAMPAIGN_ID, first_batch.batch_id)


def test_paired_smoke_is_concurrent_and_replay_preserves_failure_without_second_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch, lanes = _paired_batch_inputs()
    preview = _paired_preview(lanes)
    application_identity = _paired_application_identity(batch)
    fleet = _paired_fleet(lanes)
    lane_finals = _paired_lane_finals(lanes)
    options = _paired_manifest([lane.row for lane in lanes], [batch])
    client = _PairedLoadClient(application_identity)
    ledger = EvidenceLedger(tmp_path / "paired-replay.jsonl")
    barrier = Barrier(2)
    smoke_calls: list[str] = []
    smoke_lock = Lock()
    fail_alias = lanes[1].alias
    should_fail_once = True

    class ConcurrentSmokeAdapter:
        def __init__(self, _fixtures: object) -> None:
            pass

        def run(
            self,
            _client: object,
            alias: str,
            smoke_preview: Mapping[str, object],
        ) -> dict[str, object]:
            nonlocal should_fail_once
            with smoke_lock:
                call_number = len(smoke_calls)
                smoke_calls.append(alias)
            if call_number < 2:
                barrier.wait(timeout=5)
            with smoke_lock:
                fail_now = alias == fail_alias and should_fail_once
                if fail_now:
                    should_fail_once = False
            if fail_now:
                raise QualificationError("lane-local fixture smoke failed")
            return {
                "endpoint_alias": alias,
                "fixture_manifest_sha256": smoke_preview["fixture_manifest_sha256"],
                "recipe_content_sha256": smoke_preview["recipe_content_sha256"],
                "cases": [{"case_id": "health", "status": "passed"}],
            }

    monkeypatch.setattr(campaign_cli, "_typed_fleet", lambda _client: fleet)
    monkeypatch.setattr(
        campaign_cli,
        "_lane_final_verifications",
        lambda _application, _lanes, _preview: lane_finals,
    )
    monkeypatch.setattr(
        campaign_cli,
        "_await_application",
        lambda _client, accepted, **_kwargs: {
            **application_identity,
            "id": accepted["id"],
            "state": "succeeded",
        },
    )
    monkeypatch.setattr(campaign_cli, "ServiceSmokeAdapter", ConcurrentSmokeAdapter)

    def apply_once() -> tuple[
        Mapping[str, object],
        dict[str, Mapping[str, object]],
        dict[str, str],
    ]:
        return campaign_cli._load_batch_and_smoke(
            client=client,
            batch=batch,
            lanes=lanes,
            fixtures=cast(FixtureRegistry, None),
            preview=preview,
            profile_number=7,
            campaign_id=CAMPAIGN_ID,
            campaign_digest="9" * 64,
            ledger=ledger,
            options=options,
            failure_node_id=None,
            clock=lambda: 0.0,
            sleeper=lambda _delay: None,
        )

    first_application, first_receipts, first_errors = apply_once()
    assert first_application["id"] == "paired-application"
    assert set(first_errors) == {lanes[1].row.key}
    assert set(first_receipts) == {lanes[0].row.key}
    assert set(smoke_calls[:2]) == {lane.alias for lane in lanes}
    first_lane_events = ledger.recipe_records(CAMPAIGN_ID, lanes[0].row.key)
    second_lane_events = ledger.recipe_records(CAMPAIGN_ID, lanes[1].row.key)
    completed = campaign_cli._object(
        next(
            record["payload"]
            for record in first_lane_events
            if record["event"] == "canary.completed"
        ),
        "completed canary payload",
    )
    failed = campaign_cli._object(
        next(
            record["payload"]
            for record in second_lane_events
            if record["event"] == "canary.failed"
        ),
        "failed canary payload",
    )
    assert completed["batch_id"] == failed["batch_id"] == batch.batch_id
    assert completed["lane_id"] == lanes[0].assignment.lane
    assert failed["lane_id"] == lanes[1].assignment.lane
    assert completed["run_id"] == "run-1"
    assert failed["run_id"] == "run-2"
    assert completed["recipe_revision_id"] == "recipe-revision-1"
    assert failed["recipe_revision_id"] == "recipe-revision-2"
    assert completed["alias"] == lanes[0].alias
    assert failed["alias"] == lanes[1].alias
    assert completed["node_ids"] == list(lanes[0].node_ids)
    assert failed["node_ids"] == list(lanes[1].node_ids)
    assert completed["node_to_rank"] == {lanes[0].node_ids[0]: 0}
    assert failed["node_to_rank"] == {lanes[1].node_ids[0]: 0}
    assert (
        campaign_cli._object(completed["smoke"], "completed smoke receipt")[
            "endpoint_alias"
        ]
        == lanes[0].alias
    )
    assert failed["smoke_status"] == "failed"
    assert client.load_posts == 1

    replay_application, replay_receipts, replay_errors = apply_once()
    assert replay_application["id"] == "paired-application"
    assert replay_errors == {lanes[1].row.key: "lane-local fixture smoke failed"}
    assert set(replay_receipts) == {lanes[0].row.key}
    assert len(smoke_calls) == 2
    assert client.load_posts == 1
    for lane in lanes:
        submitted = [
            record
            for record in ledger.recipe_records(CAMPAIGN_ID, lane.row.key)
            if record["event"] == "profile.load.submitted"
        ]
        assert len(submitted) == 1
        assert (
            campaign_cli._object(
                submitted[0]["payload"], "submitted batch application"
            )["application_id"]
            == "paired-application"
        )
    failed_replays = [
        record
        for record in ledger.recipe_records(CAMPAIGN_ID, lanes[1].row.key)
        if record["event"] == "canary.failed"
    ]
    assert len(failed_replays) == 1
    reopened = EvidenceLedger(tmp_path / "paired-replay.jsonl")
    assert {
        record["recipe"]
        for record in reopened.records
        if record["event"] in {"canary.completed", "canary.failed"}
    } == {lane.row.key for lane in lanes}
    assert len(reopened.records) == len(ledger.records)


def test_lane_recovery_requires_review_digest_and_observe_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch, lanes = _paired_batch_inputs()
    lane = lanes[1]
    manifest = _paired_manifest([item.row for item in lanes], [batch])
    fixtures = cast(FixtureRegistry, SimpleNamespace())
    ledger = EvidenceLedger(tmp_path / "lane-recovery-dispatch.jsonl")
    target = cast(campaign_cli.LaneRecoveryTarget, object())
    references: tuple[campaign_cli.CanaryReference, ...] = ()
    review_digest = "7" * 64
    target_digest = "8" * 64
    durable_review: dict[str, object] | None = {
        "review_digest": review_digest,
        "review": {"reviewed": True},
    }
    recover_authorizations: list[bool] = []
    observe_calls: list[str] = []

    monkeypatch.setattr(
        campaign_cli,
        "_durable_batch_lanes",
        lambda **_kwargs: (
            lanes,
            _paired_preview(lanes),
            {"campaign_digest": "9" * 64},
        ),
    )
    monkeypatch.setattr(
        campaign_cli, "_typed_fleet", lambda _client: _paired_fleet(lanes)
    )
    monkeypatch.setattr(
        campaign_cli,
        "_lane_recovery_target",
        lambda **_kwargs: (target, lane, references),
    )
    callbacks = {
        "prepare_transition": lambda _request: {},
        "transition_to_lane": lambda _request: {},
        "observe_fleet": dict,
        "verify_serving": lambda _request: {},
        "run_fixture_smoke": lambda _request: {},
    }
    monkeypatch.setattr(
        campaign_cli, "_lane_recovery_callbacks", lambda **_kwargs: callbacks
    )
    monkeypatch.setattr(
        campaign_cli,
        "_durable_lane_recovery_review",
        lambda _ledger, **kwargs: (
            durable_review if kwargs.get("recipe") == lane.row.key else None
        ),
    )
    monkeypatch.setattr(
        campaign_cli,
        "review_lane_transition",
        lambda _target, _ledger, *, prepare_transition: (
            prepare_transition({})
            or SimpleNamespace(
                target_digest=target_digest,
                request_key="transition-request",
                review_digest=review_digest,
                profile_number=7,
                profile_id="recovery-profile",
                profile_digest="a" * 64,
                plan_digest="b" * 64,
                receipt={"reviewed": True},
            )
        ),
    )

    def recover(
        _target: object,
        _ledger: EvidenceLedger,
        *,
        prepare_transition: object,
        apply_authorized: bool,
        transition_to_lane: object,
        observe_fleet: object,
        verify_serving: object,
        run_fixture_smoke: object,
    ) -> campaign_cli.LaneRecoveryProgress:
        del (
            prepare_transition,
            transition_to_lane,
            observe_fleet,
            verify_serving,
            run_fixture_smoke,
        )
        recover_authorizations.append(apply_authorized)
        return campaign_cli.LaneRecoveryProgress(
            "awaiting-host-online",
            target_digest,
            active_run_id="run-2",
            reason="wait for the exact lane to return online",
        )

    monkeypatch.setattr(campaign_cli, "recover_single_lane", recover)

    preview = campaign_cli._review_or_apply_lane_recovery(
        client=object(),
        batch=batch,
        lane_number=2,
        manifest=manifest,
        fixtures=fixtures,
        profile_number=7,
        authority_id="paired-test-authority",
        ledger_id="ledger-id",
        campaign_id=CAMPAIGN_ID,
        ledger=ledger,
        apply=False,
        supplied_digest=None,
        args=Namespace(),
        clock=lambda: 0.0,
        sleeper=lambda _delay: None,
    )
    assert preview["mode"] == "preview"
    assert preview["status"] == "not-accepted"
    assert preview["campaign_digest"] == review_digest
    assert (
        campaign_cli._object(preview["recovery"], "recovery preview")["status"]
        == "awaiting-explicit-apply"
    )
    assert recover_authorizations == []

    durable_review = None
    with pytest.raises(
        QualificationError, match="review the exact exclusive lane recovery"
    ):
        campaign_cli._review_or_apply_lane_recovery(
            client=object(),
            batch=batch,
            lane_number=2,
            manifest=manifest,
            fixtures=fixtures,
            profile_number=7,
            authority_id="paired-test-authority",
            ledger_id="ledger-id",
            campaign_id=CAMPAIGN_ID,
            ledger=ledger,
            apply=True,
            supplied_digest=review_digest,
            args=Namespace(),
            clock=lambda: 0.0,
            sleeper=lambda _delay: None,
        )
    assert recover_authorizations == []

    durable_review = {"review_digest": review_digest, "review": {"reviewed": True}}
    with pytest.raises(
        QualificationError, match="does not match the durable exclusive lane review"
    ):
        campaign_cli._review_or_apply_lane_recovery(
            client=object(),
            batch=batch,
            lane_number=2,
            manifest=manifest,
            fixtures=fixtures,
            profile_number=7,
            authority_id="paired-test-authority",
            ledger_id="ledger-id",
            campaign_id=CAMPAIGN_ID,
            ledger=ledger,
            apply=True,
            supplied_digest="0" * 64,
            args=Namespace(),
            clock=lambda: 0.0,
            sleeper=lambda _delay: None,
        )
    assert recover_authorizations == []

    applied = campaign_cli._review_or_apply_lane_recovery(
        client=object(),
        batch=batch,
        lane_number=2,
        manifest=manifest,
        fixtures=fixtures,
        profile_number=7,
        authority_id="paired-test-authority",
        ledger_id="ledger-id",
        campaign_id=CAMPAIGN_ID,
        ledger=ledger,
        apply=True,
        supplied_digest=review_digest,
        args=Namespace(),
        clock=lambda: 0.0,
        sleeper=lambda _delay: None,
    )
    assert applied["status"] == "checkpoint-required"
    assert (
        campaign_cli._object(applied["recovery"], "recovery application")["status"]
        == "awaiting-host-online"
    )
    assert recover_authorizations == [True]

    def observe_single(
        _target: object,
        _ledger: EvidenceLedger,
        *,
        observe_fleet: object,
        verify_serving: object,
        run_fixture_smoke: object,
    ) -> campaign_cli.LaneRecoveryProgress:
        del observe_fleet, verify_serving, run_fixture_smoke
        observe_calls.append("observed")
        return campaign_cli.LaneRecoveryProgress(
            "awaiting-host-online", target_digest, reason="waiting for live Fleet state"
        )

    monkeypatch.setattr(campaign_cli, "observe_single_lane", observe_single)
    observed = campaign_cli._observe_batch_recovery(
        client=object(),
        batch=batch,
        lanes=lanes,
        manifest=manifest,
        fixtures=fixtures,
        profile_number=7,
        authority_id="paired-test-authority",
        ledger_id="ledger-id",
        campaign_id=CAMPAIGN_ID,
        ledger=ledger,
        clock=lambda: 0.0,
        sleeper=lambda _delay: None,
    )
    assert observed["status"] == "checkpoint-required"
    lane_results = cast(list[Mapping[str, object]], observed["lane_results"])
    assert lane_results[-1]["status"] == "awaiting-host-online"
    assert observe_calls == ["observed"]
    assert recover_authorizations == [True]


def test_run_routes_observe_and_explicit_lane_recovery_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch, lanes = _paired_batch_inputs()
    manifest = replace(
        _paired_manifest([lane.row for lane in lanes], [batch]),
        path=tmp_path / "campaign.json",
        fixture_manifest=tmp_path / "qualification-index.json",
    )
    manifest.fixture_manifest.write_text("{}", encoding="utf-8")
    fixtures = cast(FixtureRegistry, SimpleNamespace())
    fleet = _paired_fleet(lanes)
    ledger_path = tmp_path / "run-recovery-dispatch.jsonl"
    ledger = EvidenceLedger(ledger_path)
    for lane in lanes:
        ledger.append(
            "canary.completed",
            plan_digest=CAMPAIGN_ID,
            recipe=lane.row.key,
            payload={"batch_id": batch.batch_id, "lane_id": lane.assignment.lane},
        )

    monkeypatch.setattr(campaign_cli, "load_manifest", lambda *_args: manifest)
    monkeypatch.setattr(FixtureRegistry, "load", lambda _path: fixtures)
    monkeypatch.setattr(campaign_cli, "_bind_repository_inputs", lambda *_args: None)
    monkeypatch.setattr(
        campaign_cli, "_resolve_ledger_path", lambda *_args: ledger_path
    )
    monkeypatch.setattr(
        campaign_cli, "_make_campaign_id", lambda *_args: (CAMPAIGN_ID, "ledger-id")
    )
    monkeypatch.setattr(campaign_cli, "_typed_fleet", lambda _client: fleet)
    monkeypatch.setattr(campaign_cli, "node_locks", lambda _nodes: nullcontext())
    monkeypatch.setattr(
        campaign_cli, "_batch_evidence_nodes", lambda *_args: [NODE_A, NODE_B]
    )
    monkeypatch.setattr(
        campaign_cli,
        "_durable_batch_lanes",
        lambda **_kwargs: (
            lanes,
            _paired_preview(lanes),
            {"campaign_digest": "9" * 64},
        ),
    )
    observe_calls: list[str] = []
    recovery_calls: list[dict[str, object]] = []

    def observe_batch(**_kwargs: object) -> dict[str, object]:
        observe_calls.append("observe")
        return {"mode": "observe", "status": "checkpoint-required"}

    def review_or_apply_recovery(**kwargs: object) -> dict[str, object]:
        recovery_calls.append(kwargs)
        return {
            "mode": "apply" if kwargs["apply"] else "preview",
            "status": "checkpoint-required" if kwargs["apply"] else "not-accepted",
        }

    monkeypatch.setattr(campaign_cli, "_observe_batch_recovery", observe_batch)
    monkeypatch.setattr(
        campaign_cli, "_review_or_apply_lane_recovery", review_or_apply_recovery
    )

    common = [
        "--manifest",
        str(manifest.path),
        "--library-root",
        str(tmp_path),
        "--ledger",
        str(ledger_path),
        "--profile-number",
        "7",
        "--batch",
        batch.batch_id,
    ]
    observed = campaign_cli.run([*common, "--observe"], client_factory=lambda: object())
    assert observed == {"mode": "observe", "status": "checkpoint-required"}
    assert observe_calls == ["observe"]
    assert recovery_calls == []

    preview = campaign_cli.run(
        [*common, "--recover-lane", "2"], client_factory=lambda: object()
    )
    assert preview["mode"] == "preview"
    assert recovery_calls[-1]["lane_number"] == 2
    assert recovery_calls[-1]["apply"] is False
    assert recovery_calls[-1]["supplied_digest"] is None

    applied = campaign_cli.run(
        [
            *common,
            "--recover-lane",
            "2",
            "--apply",
            "--campaign-digest",
            "7" * 64,
        ],
        client_factory=lambda: object(),
    )
    assert applied["mode"] == "apply"
    assert recovery_calls[-1]["apply"] is True
    assert recovery_calls[-1]["supplied_digest"] == "7" * 64


def test_observe_completed_lane_reports_cleanup_without_terminal_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch, lanes = _paired_batch_inputs()
    lane = lanes[1]
    manifest = _paired_manifest([item.row for item in lanes], [batch])
    fixtures = cast(FixtureRegistry, SimpleNamespace())
    ledger = EvidenceLedger(tmp_path / "observe-cleanup-barrier.jsonl")
    target = cast(campaign_cli.LaneRecoveryTarget, object())
    durable_review = {"review_digest": "7" * 64, "review": {"reviewed": True}}
    progress = campaign_cli.LaneRecoveryProgress("completed", "8" * 64)
    callbacks = {
        "prepare_transition": lambda _request: {},
        "transition_to_lane": lambda _request: {},
        "observe_fleet": dict,
        "verify_serving": lambda _request: {},
        "run_fixture_smoke": lambda _request: {},
    }

    monkeypatch.setattr(
        campaign_cli,
        "_durable_lane_recovery_review",
        lambda _ledger, **kwargs: (
            durable_review if kwargs.get("recipe") == lane.row.key else None
        ),
    )
    monkeypatch.setattr(
        campaign_cli, "_typed_fleet", lambda _client: _paired_fleet(lanes)
    )
    monkeypatch.setattr(
        campaign_cli,
        "_lane_recovery_target",
        lambda **_kwargs: (target, lane, ()),
    )
    monkeypatch.setattr(
        campaign_cli, "_lane_recovery_callbacks", lambda **_kwargs: callbacks
    )
    monkeypatch.setattr(
        campaign_cli, "observe_single_lane", lambda *_args, **_kwargs: progress
    )
    monkeypatch.setattr(
        campaign_cli,
        "recover_single_lane",
        lambda *_args, **_kwargs: pytest.fail(
            "read-only observe must not start or apply a recovery transition"
        ),
    )
    monkeypatch.setattr(
        campaign_cli,
        "_record_batch_release",
        lambda **_kwargs: pytest.fail(
            "read-only observe must not record batch release"
        ),
    )

    before_events = [record["event"] for record in ledger.records]
    result = campaign_cli._observe_batch_recovery(
        client=object(),
        batch=batch,
        lanes=lanes,
        manifest=manifest,
        fixtures=fixtures,
        profile_number=7,
        authority_id="paired-test-authority",
        ledger_id="ledger-id",
        campaign_id=CAMPAIGN_ID,
        ledger=ledger,
        clock=lambda: 0.0,
        sleeper=lambda _delay: None,
    )

    assert result["status"] == "cleanup-required"
    next_step = campaign_cli._object(result["next"], "recovery next step")
    assert next_step["checkpoint"] == "exclusive-lane-cleanup"
    assert next_step["lane"] == lane.assignment.lane
    after_records = ledger.records
    assert [record["event"] for record in after_records] == before_events
    assert not any(
        record["event"] in {"recipe.spark-accepted", "batch.cleanup.completed"}
        for record in after_records
    )


def test_lane_cleanup_requires_its_review_digest_before_recording_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch, lanes = _paired_batch_inputs()
    lane = lanes[1]
    manifest = _paired_manifest([item.row for item in lanes], [batch])
    fixtures = cast(FixtureRegistry, SimpleNamespace())
    ledger = EvidenceLedger(tmp_path / "lane-cleanup-dispatch.jsonl")
    target = cast(campaign_cli.LaneRecoveryTarget, object())
    references: tuple[campaign_cli.CanaryReference, ...] = ()
    review_digest = "6" * 64
    target_digest = "5" * 64
    durable_review: dict[str, object] | None = {
        "review_digest": review_digest,
        "review": {"reviewed": True},
    }
    cleanup_authorizations: list[bool] = []
    acceptance_calls: list[str] = []
    callbacks = {
        "prepare_cleanup": lambda _request: {},
        "cleanup_to_idle": lambda _request: {},
        "reconcile_cleanup": lambda _request: {},
    }

    monkeypatch.setattr(
        campaign_cli,
        "_durable_batch_lanes",
        lambda **_kwargs: (
            lanes,
            _paired_preview(lanes),
            {"campaign_digest": "9" * 64},
        ),
    )
    monkeypatch.setattr(
        campaign_cli, "_typed_fleet", lambda _client: _paired_fleet(lanes)
    )
    monkeypatch.setattr(
        campaign_cli,
        "_lane_recovery_target",
        lambda **_kwargs: (target, lane, references),
    )
    monkeypatch.setattr(
        campaign_cli, "_lane_cleanup_callbacks", lambda **_kwargs: callbacks
    )
    monkeypatch.setattr(
        campaign_cli,
        "_durable_lane_cleanup_review",
        lambda _ledger, **_kwargs: durable_review,
    )
    monkeypatch.setattr(
        campaign_cli,
        "review_lane_cleanup",
        lambda _target, _ledger, *, prepare_cleanup: (
            prepare_cleanup({})
            or SimpleNamespace(
                target_digest=target_digest,
                request_key="cleanup-request",
                review_digest=review_digest,
                profile_number=7,
                profile_id="cleanup-profile",
                profile_digest="a" * 64,
                plan_digest="b" * 64,
                receipt={"reviewed": True},
            )
        ),
    )

    def record_cleanup(
        _target: object,
        _ledger: EvidenceLedger,
        *,
        prepare_cleanup: object,
        apply_authorized: bool,
        cleanup_to_idle: object,
        reconcile_cleanup: object,
    ) -> campaign_cli.LaneRecoveryProgress:
        del prepare_cleanup, cleanup_to_idle, reconcile_cleanup
        cleanup_authorizations.append(apply_authorized)
        return campaign_cli.LaneRecoveryProgress(
            "cleanup-completed", target_digest, receipt_sha256="c" * 64
        )

    monkeypatch.setattr(campaign_cli, "record_lane_cleanup", record_cleanup)

    def record_acceptance(**_kwargs: object) -> dict[str, object]:
        acceptance_calls.append(lane.row.key)
        ledger.append(
            "recipe.spark-accepted",
            plan_digest=CAMPAIGN_ID,
            recipe=lane.row.key,
            payload={"status": "spark-accepted", "batch_id": batch.batch_id},
        )
        return {"status": "spark-accepted"}

    monkeypatch.setattr(campaign_cli, "_record_lane_acceptance", record_acceptance)
    monkeypatch.setattr(
        campaign_cli,
        "_record_batch_release",
        lambda **_kwargs: pytest.fail(
            "the partner lane is incomplete; cleanup must not release the batch"
        ),
    )

    preview = campaign_cli._review_or_apply_lane_cleanup(
        client=object(),
        batch=batch,
        lane_number=2,
        manifest=manifest,
        fixtures=fixtures,
        profile_number=7,
        authority_id="paired-test-authority",
        ledger_id="ledger-id",
        campaign_id=CAMPAIGN_ID,
        ledger=ledger,
        apply=False,
        supplied_digest=None,
        clock=lambda: 0.0,
        sleeper=lambda _delay: None,
    )
    assert preview["mode"] == "preview"
    assert preview["status"] == "not-accepted"
    assert preview["campaign_digest"] == review_digest
    assert cleanup_authorizations == []
    assert acceptance_calls == []

    durable_review = None
    with pytest.raises(QualificationError, match="review exact lane cleanup"):
        campaign_cli._review_or_apply_lane_cleanup(
            client=object(),
            batch=batch,
            lane_number=2,
            manifest=manifest,
            fixtures=fixtures,
            profile_number=7,
            authority_id="paired-test-authority",
            ledger_id="ledger-id",
            campaign_id=CAMPAIGN_ID,
            ledger=ledger,
            apply=True,
            supplied_digest=review_digest,
            clock=lambda: 0.0,
            sleeper=lambda _delay: None,
        )
    assert cleanup_authorizations == []
    assert acceptance_calls == []

    durable_review = {"review_digest": review_digest, "review": {"reviewed": True}}
    with pytest.raises(
        QualificationError, match="does not match the durable cleanup review"
    ):
        campaign_cli._review_or_apply_lane_cleanup(
            client=object(),
            batch=batch,
            lane_number=2,
            manifest=manifest,
            fixtures=fixtures,
            profile_number=7,
            authority_id="paired-test-authority",
            ledger_id="ledger-id",
            campaign_id=CAMPAIGN_ID,
            ledger=ledger,
            apply=True,
            supplied_digest="0" * 64,
            clock=lambda: 0.0,
            sleeper=lambda _delay: None,
        )
    assert cleanup_authorizations == []
    assert acceptance_calls == []

    applied = campaign_cli._review_or_apply_lane_cleanup(
        client=object(),
        batch=batch,
        lane_number=2,
        manifest=manifest,
        fixtures=fixtures,
        profile_number=7,
        authority_id="paired-test-authority",
        ledger_id="ledger-id",
        campaign_id=CAMPAIGN_ID,
        ledger=ledger,
        apply=True,
        supplied_digest=review_digest,
        clock=lambda: 0.0,
        sleeper=lambda _delay: None,
    )
    assert applied["status"] == "cleanup-completed"
    assert (
        campaign_cli._object(applied["cleanup"], "cleanup progress")["status"]
        == "cleanup-completed"
    )
    assert cleanup_authorizations == [True]
    assert acceptance_calls == [lane.row.key]
    assert (
        sum(record["event"] == "recipe.spark-accepted" for record in ledger.records)
        == 1
    )
    assert not any(
        record["event"] == "batch.cleanup.completed" for record in ledger.records
    )


def test_controller_application_returns_both_stop_receipts_from_switch_adapter() -> (
    None
):
    application = _canonical_profile_application_with_stops(("run-1", "run-2"))

    receipts = campaign_cli._profile_stop_receipts(application)

    assert [item["run_id"] for item in receipts] == ["run-1", "run-2"]
    assert all(item["operation_state"] == "succeeded" for item in receipts)
    assert all(
        campaign_cli._object(item["final_observation"], "stop observation")[
            "final_verified"
        ]
        is True
        for item in receipts
    )


def test_failed_lane_cleanup_review_binds_exact_terminal_canary_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch, lanes = _paired_batch_inputs()
    lane = lanes[0]
    ledger = EvidenceLedger(tmp_path / "failed-lane-cleanup-review.jsonl")
    references = _paired_failed_canary_references(batch, lanes, ledger)
    target = _paired_failed_lane_target(batch, lanes, references, lane.assignment.lane)
    manifest = _paired_manifest([item.row for item in lanes], [batch])
    profile = {
        "number": 7,
        "id": PAIRED_PROFILE_ID,
        "profile_digest": PAIRED_PROFILE_DIGEST,
        "installation_policy": "keep-cached",
        "labels": {
            "qualification-authority": "paired-test-authority",
            "qualification-ledger": "ledger-id",
        },
        "assignments": [],
    }
    preview = {
        "allowed": True,
        "profile_id": PAIRED_PROFILE_ID,
        "profile_digest": PAIRED_PROFILE_DIGEST,
        "plan_digest": PAIRED_PLAN_DIGEST,
        "scope": {"node_ids": [NODE_A, NODE_B], "idle_node_ids": [NODE_A, NODE_B]},
        "summary": {
            "stops": 2,
            "starts": 0,
            "placements": 0,
            "builds": 0,
            "distributions": 0,
            "installs": 0,
            "uninstalls": 0,
        },
        "effects": {
            "runs": [
                {
                    "run_id": f"run-{item.assignment.lane}",
                    "action": "stop",
                    "alias": item.alias,
                    "node_ids": list(item.node_ids),
                }
                for item in lanes
            ]
        },
        "steps": [{"kind": "switch", "node_ids": [NODE_A, NODE_B]}],
    }
    monkeypatch.setattr(
        campaign_cli,
        "_typed_fleet",
        lambda _client: _typed_paired_fleet(lanes),
    )
    monkeypatch.setattr(campaign_cli, "_profile_view", lambda _client, _number: profile)
    monkeypatch.setattr(
        campaign_cli.FleetProfilePreview,
        "from_dict",
        classmethod(lambda _cls, value: SimpleNamespace(to_dict=lambda: dict(value))),
    )
    client = SimpleNamespace(
        request=lambda method, path, *_args, **_kwargs: (
            preview
            if method == "POST" and path == "/api/profile/7/preview"
            else pytest.fail(f"unexpected Controller request: {method} {path}")
        )
    )
    callbacks = campaign_cli._lane_cleanup_callbacks(
        client=client,
        target=target,
        lane=lane,
        lanes=lanes,
        references=references,
        profile_number=7,
        authority_id="paired-test-authority",
        ledger_id="ledger-id",
        campaign_id=CAMPAIGN_ID,
        ledger=ledger,
        options=manifest,
        clock=lambda: 0.0,
        sleeper=lambda _delay: None,
    )

    review = campaign_cli.review_lane_cleanup(
        target, ledger, prepare_cleanup=callbacks["prepare_cleanup"]
    )
    own = next(item for item in references if item.lane_id == lane.assignment.lane)
    assert review.receipt["cleanup_mode"] == "failed-canary-lane"
    assert review.receipt["terminal_event"] == "canary.failed"
    assert review.receipt["terminal_record_sha256"] == own.record_sha256
    assert review.receipt["active_run_id"] == own.run_id
    assert review.receipt["stop_run_ids"] == ["run-1", "run-2"]
    assert review.receipt["stop_aliases"] == ["paired-1", "paired-2"]


def test_paired_lifecycle_releases_each_lane_with_typed_controller_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch, base_lanes = _paired_batch_inputs()
    lanes: list[campaign_cli.BatchLane] = []
    coverage_definitions: list[dict[str, object]] = []
    service_recipes: dict[str, ServiceRecipe] = {}
    for lane in base_lanes:
        model_ref = {"key": "test-model", "content_sha256": "d" * 64}
        raw_row = {
            **dict(lane.row.raw),
            "model_license_refs": [model_ref],
        }
        coverage_body: dict[str, object] = {
            "failure_mode": "single-host-restart",
            "representative_recipe": lane.row.key,
            "members": [
                {
                    "recipe": lane.row.key,
                    "recipe_content_sha256": lane.row.content_sha256,
                    "package_sha256": lane.row.package["sha256"],
                    "model_content_sha256s": [model_ref["content_sha256"]],
                    "runtime_stack_sha256": lane.row.runtime_stack_sha256,
                    "topology_sha256": lane.row.topology_sha256,
                }
            ],
            "shared": False,
            "equivalence_rationale": "Dedicated paired lifecycle recovery evidence.",
            "invalidated_by": [
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
            ],
        }
        coverage_id = campaign_cli._contract_digest(coverage_body)
        coverage_ref = {
            "coverage_id": coverage_id,
            "failure_mode": "single-host-restart",
            "representative_recipe": lane.row.key,
            "role": "dedicated",
        }
        raw_row["recovery_coverage_refs"] = [coverage_ref]
        coverage_definitions.append({"coverage_id": coverage_id, **coverage_body})
        row = replace(
            lane.row,
            model_license_refs=(model_ref,),
            recovery_coverage_refs=(coverage_ref,),
            raw=raw_row,
        )
        service_case = ServiceCase("health", "GET", "/v1/models", None, 5, 1024, ())
        service_recipes[row.key] = ServiceRecipe(
            row.key, row.content_sha256, lane.alias, (service_case,), {}
        )
        smoke_preview = {
            "kind": "openai-service",
            "available": True,
            "recipe": row.key,
            "recipe_content_sha256": row.content_sha256,
            "alias": lane.alias,
            "endpoint_alias": lane.alias,
            "fixture_manifest_sha256": PAIRED_FIXTURE_DIGEST,
            "cases": [
                {
                    "id": "health",
                    "method": "GET",
                    "path": "/v1/models",
                    "body": None,
                    "timeout_seconds": 5,
                    "max_response_bytes": 1024,
                    "assertions": [],
                }
            ],
        }
        lanes.append(
            replace(
                lane,
                row=row,
                detail={
                    "identity": {
                        "recipe_id": f"controller-recipe-{lane.assignment.lane}",
                        "recipe_revision_id": f"recipe-revision-{lane.assignment.lane}",
                    }
                },
                smoke_preview=smoke_preview,
            )
        )
    paired_lanes = tuple(lanes)
    fixtures = FixtureRegistry(
        {},
        {},
        {},
        manifest_sha256=PAIRED_FIXTURE_DIGEST,
        service_recipes=service_recipes,
    )
    followup_base = paired_lanes[1].row
    followup_key = "vonk-forge/followup-model"
    followup_row = replace(
        followup_base,
        sequence=3,
        key=followup_key,
        raw={**dict(followup_base.raw), "sequence": 3, "key": followup_key},
    )
    followup = campaign_cli.CampaignBatch(
        sequence=2,
        batch_id="batch-002",
        mode="single",
        assignments=(campaign_cli.BatchAssignment(followup_key, 1, 1),),
        raw={
            "sequence": 2,
            "id": "batch-002",
            "mode": "single",
            "assignments": [{"recipe": followup_key, "lane": 1, "node_count": 1}],
        },
    )
    manifest = _paired_manifest(
        [item.row for item in paired_lanes] + [followup_row], [batch, followup]
    )
    manifest = replace(
        manifest,
        authority=replace(
            manifest.authority,
            recovery_coverage=tuple(coverage_definitions),
        ),
    )
    ledger = EvidenceLedger(tmp_path / "paired-real-helper-lifecycle.jsonl")
    batch_preview = _paired_preview(paired_lanes)
    lane_inputs = [
        {
            "recipe": lane.row.key,
            "lane": lane.assignment.lane,
            "node_ids": list(lane.node_ids),
            "alias": lane.alias,
            "smoke_kind": lane.smoke_kind,
            "smoke_preview": dict(lane.smoke_preview),
        }
        for lane in paired_lanes
    ]
    ledger.append(
        "batch.plan.generated",
        plan_digest=CAMPAIGN_ID,
        payload={
            "batch_id": batch.batch_id,
            "batch": dict(batch.raw),
            "lane_inputs": lane_inputs,
            "preview": batch_preview,
            "profile_id": PAIRED_PROFILE_ID,
            "profile_digest": PAIRED_PROFILE_DIGEST,
        },
    )
    for lane in paired_lanes:
        lane_number = lane.assignment.lane
        node_id = lane.node_ids[0]
        ledger.append(
            "canary.completed",
            plan_digest=CAMPAIGN_ID,
            recipe=lane.row.key,
            payload={
                "batch_id": batch.batch_id,
                "lane_id": lane_number,
                "node_ids": [node_id],
                "assigned_node_id": node_id,
                "assigned_rank": 0,
                "node_to_rank": {node_id: 0},
                "run_id": f"run-{lane_number}",
                "recipe_revision_id": f"recipe-revision-{lane_number}",
                "alias": lane.alias,
                "recipe_content_sha256": lane.row.content_sha256,
                "package_sha256": lane.row.package["sha256"],
                "exact_preparations": {
                    "recipe_revision_sha256": lane.row.content_sha256,
                    "model_content_sha256": "d" * 64,
                    "dependency_model_content_sha256": [],
                    "image_digest": "sha256:" + "f" * 64,
                    "architecture": "linux-arm64",
                    "target_node_ids": sorted(lane.node_ids),
                },
                "smoke": {
                    "endpoint_alias": lane.alias,
                    "recipe_content_sha256": lane.row.content_sha256,
                    "cases": [{"case_id": "health", "passed": True}],
                },
            },
        )

    profile: dict[str, object] = {
        "number": 7,
        "id": PAIRED_PROFILE_ID,
        "name": "paired qualification",
        "profile_digest": PAIRED_PROFILE_DIGEST,
        "revision": 1,
        "installation_policy": "keep-cached",
        "labels": {
            "qualification-authority": manifest.authority.authority_id,
            "qualification-ledger": "ledger-id",
        },
        "assignments": [
            {
                "recipe_selector": lane.row.key,
                "spark_ids": list(lane.node_ids),
                "selector": lane.alias,
            }
            for lane in paired_lanes
        ],
    }

    class TypedController:
        def __init__(self) -> None:
            self.active_runs: dict[int, str] = {1: "run-1", 2: "run-2"}
            self.run_identity: dict[str, tuple[int, str]] = {
                f"run-{lane.assignment.lane}": (
                    lane.assignment.lane,
                    lane.node_ids[0],
                )
                for lane in paired_lanes
            }
            self.boot_ids: dict[str, str] = {
                NODE_A: "boot-before",
                NODE_B: "boot-before",
            }
            self.offline_nodes: set[str] = set()
            self.cursor = 100
            self.application_number = 0
            self.applications: dict[str, dict[str, object]] = {}
            self.requests: dict[str, dict[str, object]] = {}
            self.fleet_reads: list[dict[str, object]] = []
            self.load_posts = 0

        def snapshot(self) -> dict[str, object]:
            if self.fleet_reads:
                return self.fleet_reads.pop(0)
            result = _typed_paired_fleet(
                paired_lanes,
                active_runs=self.active_runs,
                boot_ids=self.boot_ids,
                offline_nodes=sorted(self.offline_nodes),
                cursor=self.cursor,
            )
            self.cursor += 1
            return result

        def _assignment(self) -> tuple[campaign_cli.BatchLane, str] | None:
            assignments = cast(list[dict[str, object]], profile["assignments"])
            if not assignments:
                return None
            raw = assignments[0]
            lane = next(
                item for item in paired_lanes if item.row.key == raw["recipe_selector"]
            )
            return lane, str(raw["selector"])

        def _profile_preview(self) -> dict[str, object]:
            current = self.snapshot()
            roster = sorted(campaign_cli._nodes(current))
            assignment = self._assignment()
            runs = dict(self.active_runs)
            stop_ids: list[str] = []
            if assignment is None:
                stop_ids = sorted(runs.values())
            else:
                lane, _alias = assignment
                active = runs.get(lane.assignment.lane)
                stop_ids = sorted(
                    run_id for run_id in runs.values() if run_id != active
                )
            stop_effects = []
            for run_id in stop_ids:
                lane_number, node_id = self.run_identity[run_id]
                lane = next(
                    item for item in paired_lanes if item.assignment.lane == lane_number
                )
                stop_effects.append(
                    {
                        "run_id": run_id,
                        "action": "stop",
                        "alias": lane.alias,
                        "node_ids": [node_id],
                    }
                )
            if assignment is None:
                starts = 0
                assignments: list[dict[str, object]] = []
                preparations: list[dict[str, object]] = []
                idle = roster
                switch_nodes = sorted(
                    node_id
                    for run_id in stop_ids
                    for _lane_number, node_id in [self.run_identity[run_id]]
                )
                plan_key = "cleanup"
            else:
                lane, alias = assignment
                starts = int(lane.assignment.lane not in runs)
                idle = sorted(set(roster) - set(lane.node_ids))
                assignment_id = f"assignment-{lane.assignment.lane}"
                preparation = _rollout_preparation(node_ids=lane.node_ids)
                cast(dict[str, object], preparation["model"])[
                    "recipe_revision_sha256"
                ] = lane.row.content_sha256
                assignments = [
                    {
                        "assignment_id": assignment_id,
                        "assignment_name": alias,
                        "recipe_id": f"controller-recipe-{lane.assignment.lane}",
                        "recipe_revision_id": f"recipe-revision-{lane.assignment.lane}",
                        "node_ids": list(lane.node_ids),
                        "desired_state": "running",
                    }
                ]
                preparations = [
                    {
                        "assignment_id": assignment_id,
                        "preparation": preparation,
                    }
                ]
                switch_nodes = sorted(
                    set(lane.node_ids)
                    | {
                        node_id
                        for run_id in stop_ids
                        for _lane_number, node_id in [self.run_identity[run_id]]
                    }
                )
                plan_key = lane.row.key
            return {
                "allowed": True,
                "profile_id": profile["id"],
                "profile_digest": profile["profile_digest"],
                "plan_digest": hashlib.sha256(
                    f"{profile['revision']}:{plan_key}:{','.join(stop_ids)}".encode()
                ).hexdigest(),
                "scope": {"node_ids": roster, "idle_node_ids": idle},
                "summary": {
                    "stops": len(stop_ids),
                    "starts": starts,
                    "placements": 0,
                    "builds": 0,
                    "distributions": 0,
                    "installs": 0,
                    "uninstalls": 0,
                },
                "effects": {"runs": stop_effects},
                "assignments": assignments,
                "preparations": preparations,
                "steps": (
                    [{"kind": "switch", "node_ids": switch_nodes}]
                    if switch_nodes
                    else []
                ),
            }

        def _child(
            self, run_id: str, *, stopped: bool, index: int
        ) -> dict[str, object]:
            _lane_number, node_id = self.run_identity[run_id]
            final = {
                "phase": "final_verify",
                "final_verified": True,
                "healthy": not stopped,
                "state": "stopped" if stopped else "running",
                "route_state": "withdrawn" if stopped else "published",
                "run_id": run_id,
                "ranks": [
                    {
                        "node_id": node_id,
                        "rank": 0,
                        "role": "rank-0",
                        "state": "stopped" if stopped else "running",
                        "fresh": True,
                    }
                ],
            }
            return {
                "kind": "stop" if stopped else "run",
                "operation_id": f"child-{index}-{run_id}",
                "state": "succeeded",
                "result": {
                    "run_switch_operation_id": f"child-{index}-{run_id}",
                    "run_switch": {
                        "item_index": index,
                        "profile_application_id": f"application-{self.application_number}",
                        "final_observation": final,
                    },
                },
            }

        def _application(
            self,
            *,
            app_id: str,
            request_key: str,
            plan_digest: str,
            assignments: list[dict[str, object]],
            children: list[dict[str, object]],
        ) -> dict[str, object]:
            assignment_ids = [str(item["id"]) for item in assignments]
            progress = {
                "operation_kind": "fleet-profile.apply",
                "completed_steps": 1,
                "total_steps": 1,
                "intended_profile": {
                    "assignments": assignments,
                    "installation_policy": "keep-cached",
                    "profile_digest": profile["profile_digest"],
                    "reviewed_application_id": app_id,
                    "reviewed_plan_digest": plan_digest,
                    "scope": {"node_ids": [NODE_A, NODE_B]},
                },
                "step_results": {
                    "profile.switch": {
                        "kind": "switch",
                        "operation_id": f"step-{app_id}",
                        "result": {
                            "assignment_ids": assignment_ids,
                            "children": children,
                        },
                    }
                },
                "switch_adapter": {
                    "actor": "qualification-test",
                    "assignment_ids": assignment_ids,
                    "assignments": assignments,
                    "child_id": f"adapter-{app_id}",
                    "queue": [
                        {"id": item["operation_id"], "kind": item["kind"]}
                        for item in children
                    ],
                    "request_id": request_key,
                    "scope_node_ids": [NODE_A, NODE_B],
                    "state": "succeeded",
                    "result": {"assignment_ids": assignment_ids, "children": children},
                },
            }
            return {
                "created_at": "2026-09-25T00:00:00Z",
                "current_operation_id": None,
                "current_step": 1,
                "id": app_id,
                "plan_digest": campaign_cli._application_plan_digest(
                    plan_digest, request_key
                ),
                "profile_digest": profile["profile_digest"],
                "profile_id": profile["id"],
                "progress": progress,
                "request_key": request_key,
                "result": None,
                "state": "succeeded",
                "status_reason": None,
                "total_steps": 1,
                "updated_at": "2026-09-25T00:00:00Z",
            }

        def request(
            self,
            method: str,
            path: str,
            payload: object = None,
            **_kwargs: object,
        ) -> dict[str, object]:
            if method == "GET" and path == "/api/fleet":
                return self.snapshot()
            if method == "GET" and path.startswith("/api/fleet/"):
                node_id = unquote(path.rsplit("/", 1)[-1])
                snapshot = _typed_paired_fleet(
                    paired_lanes,
                    active_runs=self.active_runs,
                    boot_ids=self.boot_ids,
                    cursor=self.cursor,
                )
                node = next(
                    item
                    for item in cast(list[dict[str, object]], snapshot["nodes"])
                    if item["id"] == node_id
                )
                node["provenance"] = {
                    "schema_version": 2,
                    "generated_at": "2026-09-25T00:00:00Z",
                    "platform": [
                        {
                            "boundary": "controller_deployment",
                            "state": "observed",
                            "image_digest": "sha256:" + "a" * 64,
                            "evidence": {
                                "source": "qualification-test",
                                "freshness": "current",
                                "age_seconds": 0,
                                "observed_at": "2026-09-25T00:00:00Z",
                            },
                        }
                    ],
                    "recipe_library": {
                        "state": "unknown",
                        "evidence": {"source": "qualification-test"},
                    },
                    "agents": [
                        {
                            "node_id": node_id,
                            "display_name": node_id,
                            "state": "installed",
                            "connectivity": "recent",
                            "build_digest": "sha256:" + "b" * 64,
                            "binary_sha256": "c" * 64,
                            "package_sha256": next(
                                lane.row.package["sha256"]
                                for lane in paired_lanes
                                if lane.node_ids[0] == node_id
                            ),
                            "evidence": {
                                "source": "qualification-test",
                                "freshness": "current",
                                "age_seconds": 0,
                                "observed_at": "2026-09-25T00:00:00Z",
                            },
                            "package_evidence": {
                                "source": "qualification-test",
                                "freshness": "current",
                                "age_seconds": 0,
                                "observed_at": "2026-09-25T00:00:00Z",
                            },
                        }
                    ],
                    "workloads": [],
                    "invalid_operation_evidence": [],
                    "invalid_operation_evidence_omitted_count": 0,
                }
                return node
            if method == "GET" and path.startswith("/api/endpoints/"):
                alias = unquote(path.rsplit("/", 1)[-1])
                if any(
                    next(
                        item.alias
                        for item in paired_lanes
                        if item.assignment.lane == lane_number
                    )
                    == alias
                    for lane_number in self.active_runs
                ):
                    return {"api_base": "https://qualification.invalid/v1"}
                raise campaign_cli.ControlNotFound(404, "endpoint absent")
            if method == "POST" and path == "/api/profile/7/preview":
                return self._profile_preview()
            if method == "GET" and path.startswith("/api/profile/7/requests/"):
                request_key = unquote(path.rsplit("/", 1)[-1])
                if request_key not in self.requests:
                    raise campaign_cli.ControlNotFound(404, "request absent")
                return dict(self.requests[request_key])
            if method == "GET" and path.startswith("/api/profile/applications/"):
                return dict(self.applications[path.rsplit("/", 1)[-1]])
            if method == "POST" and path == "/api/profile/7/load":
                self.load_posts += 1
                body = cast(dict[str, object], payload)
                request_key = str(body["request_key"])
                base_plan_digest = str(body["plan_digest"])
                assignment = self._assignment()
                before_runs = dict(self.active_runs)
                stop_run_ids: list[str]
                if assignment is None:
                    stop_run_ids = sorted(before_runs.values())
                    assignments: list[dict[str, object]] = []
                    self.active_runs = {}
                else:
                    lane, alias = assignment
                    stop_run_ids = sorted(
                        run_id
                        for lane_number, run_id in before_runs.items()
                        if lane_number != lane.assignment.lane
                    )
                    previous_active = before_runs.get(lane.assignment.lane)
                    active_run = (
                        previous_active or f"reactivated-run-{lane.assignment.lane}"
                    )
                    self.run_identity[active_run] = (
                        lane.assignment.lane,
                        lane.node_ids[0],
                    )
                    self.active_runs = {lane.assignment.lane: active_run}
                    assignments = [
                        {
                            "id": f"assignment-{lane.assignment.lane}",
                            "desired_state": "running",
                            "nodes": [
                                {
                                    "node_id": lane.node_ids[0],
                                    "rank": 0,
                                    "role": "rank-0",
                                }
                            ],
                            "recipe_id": f"controller-recipe-{lane.assignment.lane}",
                            "recipe_revision_id": f"recipe-revision-{lane.assignment.lane}",
                            "recipe_title": lane.row.key,
                            "topology_name": "single",
                            "alias": alias,
                        }
                    ]
                self.application_number += 1
                app_id = f"application-{self.application_number}"
                children = [
                    self._child(run_id, stopped=True, index=index)
                    for index, run_id in enumerate(stop_run_ids)
                ]
                if assignment is not None:
                    lane, _alias = assignment
                    if lane.assignment.lane not in before_runs:
                        run_id = self.active_runs[lane.assignment.lane]
                        children.append(
                            self._child(run_id, stopped=False, index=len(children))
                        )
                application = self._application(
                    app_id=app_id,
                    request_key=request_key,
                    plan_digest=base_plan_digest,
                    assignments=assignments,
                    children=children,
                )
                self.applications[app_id] = application
                accepted = {
                    key: application[key]
                    for key in ("id", "profile_id", "profile_digest", "request_key")
                }
                accepted["plan_digest"] = application["plan_digest"]
                self.requests[request_key] = dict(accepted)
                if assignment is not None:
                    lane, _alias = assignment
                    live_snapshot = _typed_paired_fleet(
                        paired_lanes,
                        active_runs=self.active_runs,
                        boot_ids=self.boot_ids,
                        cursor=self.cursor,
                    )
                    self.cursor += 1
                    offline_snapshot = _typed_paired_fleet(
                        paired_lanes,
                        active_runs=self.active_runs,
                        boot_ids=self.boot_ids,
                        offline_nodes=[lane.node_ids[0]],
                        cursor=self.cursor,
                    )
                    self.cursor += 1
                    self.fleet_reads.extend(
                        [live_snapshot, live_snapshot, offline_snapshot]
                    )
                return accepted
            raise AssertionError(f"unexpected Controller request: {method} {path}")

    controller = TypedController()
    monkeypatch.setattr(
        campaign_cli,
        "_durable_batch_lanes",
        lambda **_kwargs: (
            list(paired_lanes),
            batch_preview,
            {"preview": batch_preview},
        ),
    )

    def save_profile(
        _client: object,
        current: Mapping[str, object],
        *,
        assignments: Sequence[Mapping[str, object]],
        authority_id: str,
        ledger_id: str,
    ) -> dict[str, object]:
        assert authority_id == manifest.authority.authority_id
        assert ledger_id == "ledger-id"
        profile["assignments"] = [
            {
                "recipe_selector": item["recipe_selector"],
                "spark_ids": list(cast(Sequence[str], item["spark_ids"])),
                "selector": item["assignment_name"],
            }
            for item in assignments
        ]
        profile["revision"] = int(cast(int, current["revision"])) + 1
        profile["profile_digest"] = hashlib.sha256(
            f"profile-revision-{profile['revision']}:".encode()
            + json.dumps(profile["assignments"], sort_keys=True).encode()
        ).hexdigest()
        return profile

    monkeypatch.setattr(campaign_cli, "_profile_view", lambda _client, _number: profile)
    monkeypatch.setattr(campaign_cli, "_save_profile", save_profile)
    monkeypatch.setattr(
        campaign_cli.FleetProfilePreview,
        "from_dict",
        classmethod(lambda _cls, value: SimpleNamespace(to_dict=lambda: dict(value))),
    )
    smoke_calls: list[str] = []

    def run_service_smoke(
        _adapter: object, _client: object, alias: str, _preview: Mapping[str, object]
    ) -> Mapping[str, object]:
        smoke_calls.append(alias)
        return {
            "endpoint_alias": alias,
            "recipe_content_sha256": next(
                lane.row.content_sha256 for lane in paired_lanes if lane.alias == alias
            ),
            "cases": [{"case_id": "health", "http_status": 200}],
        }

    monkeypatch.setattr(campaign_cli.ServiceSmokeAdapter, "run", run_service_smoke)

    recovery_preview = campaign_cli._review_or_apply_lane_recovery(
        client=controller,
        batch=batch,
        lane_number=1,
        manifest=manifest,
        fixtures=fixtures,
        profile_number=7,
        authority_id=manifest.authority.authority_id,
        ledger_id="ledger-id",
        campaign_id=CAMPAIGN_ID,
        ledger=ledger,
        apply=False,
        supplied_digest=None,
        args=Namespace(),
        clock=lambda: 0.0,
        sleeper=lambda _delay: None,
    )
    assert recovery_preview["status"] == "not-accepted"
    assert recovery_preview["spark_accepted"] is False
    recovery_apply = campaign_cli._review_or_apply_lane_recovery(
        client=controller,
        batch=batch,
        lane_number=1,
        manifest=manifest,
        fixtures=fixtures,
        profile_number=7,
        authority_id=manifest.authority.authority_id,
        ledger_id="ledger-id",
        campaign_id=CAMPAIGN_ID,
        ledger=ledger,
        apply=True,
        supplied_digest=str(recovery_preview["campaign_digest"]),
        args=Namespace(),
        clock=lambda: 0.0,
        sleeper=lambda _delay: None,
    )
    assert (
        campaign_cli._object(recovery_apply["recovery"], "lane recovery progress")[
            "status"
        ]
        == "awaiting-host-online"
    )
    transition_payload = campaign_cli._object(
        next(
            record["payload"]
            for record in ledger.records
            if record["event"] == "lane_recovery.transitioned"
        ),
        "transition payload",
    )
    transition_receipt = campaign_cli._object(
        transition_payload["receipt"], "transition receipt"
    )
    stop_receipts = cast(
        list[Mapping[str, object]], transition_receipt["partner_stop_receipts"]
    )
    assert [item["run_id"] for item in stop_receipts] == ["run-2"]
    controller.boot_ids[NODE_A] = "boot-after-lane-1"
    recovered = campaign_cli._observe_batch_recovery(
        client=controller,
        batch=batch,
        lanes=paired_lanes,
        manifest=manifest,
        fixtures=fixtures,
        profile_number=7,
        authority_id=manifest.authority.authority_id,
        ledger_id="ledger-id",
        campaign_id=CAMPAIGN_ID,
        ledger=ledger,
        clock=lambda: 0.0,
        sleeper=lambda _delay: None,
    )
    assert recovered["status"] == "cleanup-required"
    assert campaign_cli._object(recovered["next"], "recovery next step")["lane"] == 1
    controller.fleet_reads.clear()
    controller.offline_nodes.clear()

    cleanup_preview = campaign_cli._review_or_apply_lane_cleanup(
        client=controller,
        batch=batch,
        lane_number=1,
        manifest=manifest,
        fixtures=fixtures,
        profile_number=7,
        authority_id=manifest.authority.authority_id,
        ledger_id="ledger-id",
        campaign_id=CAMPAIGN_ID,
        ledger=ledger,
        apply=False,
        supplied_digest=None,
        clock=lambda: 0.0,
        sleeper=lambda _delay: None,
    )
    assert cleanup_preview["status"] == "not-accepted"
    posts_before_cleanup = controller.load_posts
    cleanup_applied = campaign_cli._review_or_apply_lane_cleanup(
        client=controller,
        batch=batch,
        lane_number=1,
        manifest=manifest,
        fixtures=fixtures,
        profile_number=7,
        authority_id=manifest.authority.authority_id,
        ledger_id="ledger-id",
        campaign_id=CAMPAIGN_ID,
        ledger=ledger,
        apply=True,
        supplied_digest=str(cleanup_preview["campaign_digest"]),
        clock=lambda: 0.0,
        sleeper=lambda _delay: None,
    )
    assert (
        campaign_cli._object(cleanup_applied["acceptance"], "lane acceptance")["status"]
        == "spark-accepted"
    )
    assert controller.load_posts == posts_before_cleanup + 1
    first_cleanup_application = controller.applications["application-2"]
    assert [
        item["run_id"]
        for item in campaign_cli._profile_stop_receipts(first_cleanup_application)
    ] == ["run-1"]
    replayed_cleanup = campaign_cli._review_or_apply_lane_cleanup(
        client=controller,
        batch=batch,
        lane_number=1,
        manifest=manifest,
        fixtures=fixtures,
        profile_number=7,
        authority_id=manifest.authority.authority_id,
        ledger_id="ledger-id",
        campaign_id=CAMPAIGN_ID,
        ledger=ledger,
        apply=True,
        supplied_digest=str(cleanup_preview["campaign_digest"]),
        clock=lambda: 0.0,
        sleeper=lambda _delay: None,
    )
    assert (
        campaign_cli._object(
            replayed_cleanup["acceptance"], "replayed lane acceptance"
        )["status"]
        == "spark-accepted"
    )
    assert controller.load_posts == posts_before_cleanup + 1
    assert any(
        record["event"] == "lane_recovery.cleanup.reconciled"
        and record["recipe"] == paired_lanes[0].row.key
        for record in ledger.records
    )

    second_recovery_preview = campaign_cli._review_or_apply_lane_recovery(
        client=controller,
        batch=batch,
        lane_number=2,
        manifest=manifest,
        fixtures=fixtures,
        profile_number=7,
        authority_id=manifest.authority.authority_id,
        ledger_id="ledger-id",
        campaign_id=CAMPAIGN_ID,
        ledger=ledger,
        apply=False,
        supplied_digest=None,
        args=Namespace(),
        clock=lambda: 0.0,
        sleeper=lambda _delay: None,
    )
    assert second_recovery_preview["status"] == "not-accepted"
    second_review = campaign_cli._object(
        second_recovery_preview["review"], "second lane recovery review"
    )
    assert second_review["stop_run_ids"] == []
    assert [
        proof["run_id"]
        for proof in cast(
            list[Mapping[str, object]], second_review["released_partner_proofs"]
        )
    ] == ["run-1"]
    second_recovery_apply = campaign_cli._review_or_apply_lane_recovery(
        client=controller,
        batch=batch,
        lane_number=2,
        manifest=manifest,
        fixtures=fixtures,
        profile_number=7,
        authority_id=manifest.authority.authority_id,
        ledger_id="ledger-id",
        campaign_id=CAMPAIGN_ID,
        ledger=ledger,
        apply=True,
        supplied_digest=str(second_recovery_preview["campaign_digest"]),
        args=Namespace(),
        clock=lambda: 0.0,
        sleeper=lambda _delay: None,
    )
    assert (
        campaign_cli._object(
            second_recovery_apply["recovery"], "second lane recovery progress"
        )["active_run_id"]
        == "reactivated-run-2"
    )
    assert any(
        record["event"] == "lane_recovery.reactivation_smoke.completed"
        for record in ledger.records
    )
    controller.boot_ids[NODE_B] = "boot-after-lane-2"
    second_observation = campaign_cli._observe_batch_recovery(
        client=controller,
        batch=batch,
        lanes=paired_lanes,
        manifest=manifest,
        fixtures=fixtures,
        profile_number=7,
        authority_id=manifest.authority.authority_id,
        ledger_id="ledger-id",
        campaign_id=CAMPAIGN_ID,
        ledger=ledger,
        clock=lambda: 0.0,
        sleeper=lambda _delay: None,
    )
    assert second_observation["status"] == "cleanup-required"
    assert (
        campaign_cli._object(second_observation["next"], "second recovery next step")[
            "lane"
        ]
        == 2
    )

    second_cleanup_preview = campaign_cli._review_or_apply_lane_cleanup(
        client=controller,
        batch=batch,
        lane_number=2,
        manifest=manifest,
        fixtures=fixtures,
        profile_number=7,
        authority_id=manifest.authority.authority_id,
        ledger_id="ledger-id",
        campaign_id=CAMPAIGN_ID,
        ledger=ledger,
        apply=False,
        supplied_digest=None,
        clock=lambda: 0.0,
        sleeper=lambda _delay: None,
    )
    second_cleanup_applied = campaign_cli._review_or_apply_lane_cleanup(
        client=controller,
        batch=batch,
        lane_number=2,
        manifest=manifest,
        fixtures=fixtures,
        profile_number=7,
        authority_id=manifest.authority.authority_id,
        ledger_id="ledger-id",
        campaign_id=CAMPAIGN_ID,
        ledger=ledger,
        apply=True,
        supplied_digest=str(second_cleanup_preview["campaign_digest"]),
        clock=lambda: 0.0,
        sleeper=lambda _delay: None,
    )
    assert second_cleanup_applied["status"] == "lane-terminal"
    assert second_cleanup_applied["spark_accepted"] is True
    assert [
        lane.row.key
        for lane in paired_lanes
        if campaign_cli._latest_payload(
            ledger, CAMPAIGN_ID, lane.row.key, "recipe.spark-accepted"
        )
        is not None
    ] == [lane.row.key for lane in paired_lanes]
    assert controller.load_posts == 4
    assert campaign_cli._batch_release_recorded(ledger, CAMPAIGN_ID, batch) is True
    assert campaign_cli._current_batch(manifest, ledger, CAMPAIGN_ID, None) == followup


class _PairedLoadClient:
    def __init__(self, application: Mapping[str, object]) -> None:
        self.application = dict(application)
        self.load_posts = 0
        self.committed = False
        self.calls: list[tuple[str, str, object]] = []

    def request(
        self,
        method: str,
        path: str,
        payload: object = None,
        **_kwargs: object,
    ) -> dict[str, object]:
        self.calls.append((method, path, payload))
        if method == "GET" and "/requests/" in path:
            if self.committed:
                return dict(self.application)
            raise campaign_cli.ControlNotFound(404, "request has not been accepted")
        if method == "POST" and path == "/api/profile/7/load":
            self.load_posts += 1
            self.committed = True
            raise campaign_cli.ControlTransportError(
                "accepted request response was lost"
            )
        raise AssertionError(f"unexpected Controller request: {method} {path}")


def _typed_failed_dual_fleet(
    lane: campaign_cli.BatchLane, *, active: bool, cursor: int
) -> dict[str, object]:
    rank_lanes = [replace(lane, node_ids=(node_id,)) for node_id in (NODE_A, NODE_B)]
    active_runs = cast(
        Mapping[int, str], {lane.assignment.lane: "dual-run" if active else None}
    )
    snapshot = _typed_paired_fleet(rank_lanes, active_runs=active_runs, cursor=cursor)
    nodes = cast(list[dict[str, object]], snapshot["nodes"])
    for rank, node_id in enumerate((NODE_A, NODE_B)):
        node = next(item for item in nodes if item["id"] == node_id)
        loaded = cast(list[dict[str, object]], node["loaded"])
        if active:
            assert len(loaded) == 1
            loaded[0].update(
                {
                    "run_id": "dual-run",
                    "recipe_revision_id": "dual-revision",
                    "expected_rank_count": 2,
                    "member_node_ids": [NODE_A, NODE_B],
                    "present_ranks": [0, 1],
                    "rank": rank,
                    "role": f"rank-{rank}",
                }
            )
    return campaign_cli.FleetSnapshot.from_dict(snapshot).to_dict()


def test_failed_exclusive_dual_canary_requires_exact_cleanup_before_failure_and_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _row(node_count=2)
    recipe = "vonk-forge/failed-dual-cleanup"
    row = replace(
        base,
        key=recipe,
        content_sha256=CONTENT_SHA,
        package={"sha256": "2" * 64},
        raw={
            **dict(base.raw),
            "key": recipe,
            "content_sha256": CONTENT_SHA,
            "package": {"sha256": "2" * 64},
            "node_count": 2,
        },
    )
    assignment = campaign_cli.BatchAssignment(recipe, lane=1, node_count=2)
    batch = campaign_cli.CampaignBatch(
        sequence=1,
        batch_id="batch-dual-001",
        mode="exclusive-dual",
        assignments=(assignment,),
        raw={
            "sequence": 1,
            "id": "batch-dual-001",
            "mode": "exclusive-dual",
            "assignments": [{"recipe": recipe, "lane": 1, "node_count": 2}],
        },
    )
    lane = campaign_cli.BatchLane(
        assignment=assignment,
        row=row,
        node_ids=(NODE_A, NODE_B),
        alias="failed-dual-alias",
        smoke_kind="openai-service",
        detail={"identity": {"recipe_revision_id": "dual-revision"}},
        smoke_preview={"available": True, "cases": [{"id": "health"}]},
    )
    manifest = replace(
        _paired_manifest([row], [batch]),
        operation_timeout_seconds=5,
        poll_interval_seconds=0.01,
    )
    ledger = EvidenceLedger(tmp_path / "failed-exclusive-dual-cleanup.jsonl")
    source_request_key = "00000000-0000-4000-8000-000000000001"
    source_application_id = "dual-canary-application"
    updated_at = "2026-09-25T00:00:00+00:00"
    profile: dict[str, object] = {
        "number": 7,
        "id": PAIRED_PROFILE_ID,
        "name": "dedicated dual qualification",
        "profile_digest": PAIRED_PROFILE_DIGEST,
        "revision": 1,
        "installation_policy": "keep-cached",
        "labels": {
            "qualification-authority": manifest.authority.authority_id,
            "qualification-ledger": "dual-test-ledger",
        },
        "assignments": [
            {
                "recipe_selector": recipe,
                "spark_ids": [NODE_A, NODE_B],
                "selector": lane.alias,
            }
        ],
    }

    def app_child(*, stopped: bool, application_id: str) -> dict[str, object]:
        final = {
            "phase": "final_verify",
            "final_verified": True,
            "healthy": not stopped,
            "state": "stopped" if stopped else "running",
            "route_state": "withdrawn" if stopped else "published",
            "run_id": "dual-run",
            "ranks": [
                {
                    "node_id": node_id,
                    "rank": rank,
                    "role": f"rank-{rank}",
                    "state": "stopped" if stopped else "running",
                    "fresh": True,
                }
                for rank, node_id in enumerate((NODE_A, NODE_B))
            ],
        }
        return {
            "kind": "stop" if stopped else "run",
            "operation_id": f"child-{application_id}",
            "state": "succeeded",
            "result": {
                "run_switch_operation_id": f"child-{application_id}",
                "run_switch": {
                    "item_index": 0,
                    "profile_application_id": application_id,
                    "final_observation": final,
                },
            },
        }

    def app_view(
        *,
        app_id: str,
        request_key: str,
        reconciliation_digest: str,
        profile_digest: str,
        child: Mapping[str, object],
    ) -> dict[str, object]:
        children = [dict(child)]
        assignments: list[dict[str, object]] = []
        progress = {
            "operation_kind": "fleet-profile.apply",
            "completed_steps": 1,
            "total_steps": 1,
            "intended_profile": {
                "assignments": assignments,
                "installation_policy": "keep-cached",
                "profile_digest": profile_digest,
                "reviewed_application_id": app_id,
                "reviewed_plan_digest": reconciliation_digest,
                "scope": {"node_ids": [NODE_A, NODE_B]},
            },
            "step_results": {
                "profile.switch": {
                    "kind": "switch",
                    "operation_id": f"step-{app_id}",
                    "result": {"assignment_ids": [], "children": children},
                }
            },
            "switch_adapter": {
                "actor": "qualification-test",
                "assignment_ids": [],
                "assignments": assignments,
                "child_id": f"adapter-{app_id}",
                "queue": [{"id": child["operation_id"], "kind": child["kind"]}],
                "request_id": request_key,
                "scope_node_ids": [NODE_A, NODE_B],
                "state": "succeeded",
                "result": {"assignment_ids": [], "children": children},
            },
        }
        return {
            "created_at": updated_at,
            "current_operation_id": None,
            "current_step": 1,
            "id": app_id,
            "plan_digest": campaign_cli._application_plan_digest(
                reconciliation_digest, request_key
            ),
            "profile_digest": profile_digest,
            "profile_id": PAIRED_PROFILE_ID,
            "progress": progress,
            "request_key": request_key,
            "result": None,
            "state": "succeeded",
            "status_reason": None,
            "total_steps": 1,
            "updated_at": updated_at,
        }

    source_application = app_view(
        app_id=source_application_id,
        request_key=source_request_key,
        reconciliation_digest=PAIRED_PLAN_DIGEST,
        profile_digest=PAIRED_PROFILE_DIGEST,
        child=app_child(stopped=False, application_id=source_application_id),
    )
    ledger.append(
        "batch.plan.generated",
        plan_digest=CAMPAIGN_ID,
        payload={
            "batch_id": batch.batch_id,
            "batch": dict(batch.raw),
            "profile_number": 7,
            "profile_id": PAIRED_PROFILE_ID,
            "profile_digest": PAIRED_PROFILE_DIGEST,
            "failure_node_id": NODE_A,
            "preview": {
                "plan_digest": PAIRED_PLAN_DIGEST,
                "profile_id": PAIRED_PROFILE_ID,
                "profile_digest": PAIRED_PROFILE_DIGEST,
            },
        },
    )
    ledger.append(
        "profile.load.requested",
        plan_digest=CAMPAIGN_ID,
        recipe=recipe,
        payload={
            "batch_id": batch.batch_id,
            "lane_id": assignment.lane,
            "profile_id": PAIRED_PROFILE_ID,
            "profile_digest": PAIRED_PROFILE_DIGEST,
            "plan_digest": PAIRED_PLAN_DIGEST,
            "failure_node_id": NODE_A,
            "request_key": source_request_key,
        },
    )
    ledger.append(
        "canary.failed",
        plan_digest=CAMPAIGN_ID,
        recipe=recipe,
        payload={
            "batch_id": batch.batch_id,
            "lane_id": assignment.lane,
            "recipe_content_sha256": row.content_sha256,
            "package_sha256": row.package["sha256"],
            "alias": lane.alias,
            "smoke_kind": lane.smoke_kind,
            "smoke_status": "failed",
            "error": "fixture health check failed after both ranks were serving",
            "node_ids": [NODE_A, NODE_B],
            "node_to_rank": {NODE_A: 0, NODE_B: 1},
            "assigned_node_id": NODE_A,
            "assigned_rank": 0,
            "run_id": "dual-run",
            "recipe_revision_id": "dual-revision",
            "application_id": source_application_id,
            "application_request_key": source_request_key,
            "application": source_application,
            "fleet_rank_presence": [
                {
                    "node_id": node_id,
                    "rank": rank,
                    "run_id": "dual-run",
                    "alias": lane.alias,
                    "recipe_revision_id": "dual-revision",
                    "expected_rank_count": 2,
                    "run_state": "running",
                    "route_state": "published",
                    "healthy": True,
                    "rank_state": "running",
                    "rank_fresh": True,
                    "group_state": "healthy",
                    "present_ranks": [0, 1],
                    "member_node_ids": [NODE_A, NODE_B],
                }
                for rank, node_id in enumerate((NODE_A, NODE_B))
            ],
        },
    )

    cleanup_plan_digest = "9" * 64
    cleanup_profile_digest = "8" * 64
    cleanup_preview = {
        "allowed": True,
        "profile_id": PAIRED_PROFILE_ID,
        "profile_digest": cleanup_profile_digest,
        "plan_digest": cleanup_plan_digest,
        "scope": {"node_ids": [NODE_A, NODE_B], "idle_node_ids": [NODE_A, NODE_B]},
        "summary": {
            "stops": 1,
            "starts": 0,
            "placements": 0,
            "builds": 0,
            "distributions": 0,
            "installs": 0,
            "uninstalls": 0,
        },
        "effects": {
            "runs": [
                {
                    "run_id": "dual-run",
                    "action": "stop",
                    "alias": lane.alias,
                    "node_ids": [NODE_A, NODE_B],
                }
            ]
        },
        "steps": [{"kind": "switch", "node_ids": [NODE_A, NODE_B]}],
    }

    class TypedController:
        def __init__(self) -> None:
            self.cursor = 100
            self.applications = {source_application_id: source_application}
            self.requests = {source_request_key: source_application}
            self.cleanup_post_count = 0

        def request(
            self,
            method: str,
            path: str,
            payload: object = None,
            **_kwargs: object,
        ) -> dict[str, object]:
            if method == "GET" and path == "/api/fleet":
                result = _typed_failed_dual_fleet(
                    lane, active=self.cleanup_post_count == 0, cursor=self.cursor
                )
                self.cursor += 1
                return result
            if method == "GET" and path.startswith("/api/fleet/"):
                node_id = unquote(path.rsplit("/", 1)[-1])
                snapshot = _typed_failed_dual_fleet(
                    lane, active=self.cleanup_post_count == 0, cursor=self.cursor
                )
                node = next(
                    item
                    for item in cast(list[dict[str, object]], snapshot["nodes"])
                    if item["id"] == node_id
                )
                node["provenance"] = {
                    "schema_version": 2,
                    "generated_at": updated_at,
                    "platform": [],
                    "recipe_library": {
                        "state": "unknown",
                        "evidence": {"source": "qualification-test"},
                    },
                    "agents": [],
                    "workloads": [],
                    "invalid_operation_evidence": [],
                    "invalid_operation_evidence_omitted_count": 0,
                }
                return node
            if method == "GET" and path.startswith("/api/profile/7/requests/"):
                key = unquote(path.rsplit("/", 1)[-1])
                if key not in self.requests:
                    raise campaign_cli.ControlNotFound(404, "request not found")
                return self.requests[key]
            if method == "GET" and path.startswith("/api/profile/applications/"):
                app_id = unquote(path.rsplit("/", 1)[-1])
                if app_id not in self.applications:
                    raise campaign_cli.ControlNotFound(404, "application not found")
                return self.applications[app_id]
            if method == "GET" and path == f"/api/endpoints/{lane.alias}":
                raise campaign_cli.ControlNotFound(404, "endpoint not found")
            if method == "POST" and path == "/api/profile/7/preview":
                return cleanup_preview
            if method == "POST" and path == "/api/profile/7/load":
                body = cast(Mapping[str, object], payload)
                key = str(body["request_key"])
                assert key not in self.requests
                self.cleanup_post_count += 1
                application = app_view(
                    app_id="cleanup-app",
                    request_key=key,
                    reconciliation_digest=cleanup_plan_digest,
                    profile_digest=cleanup_profile_digest,
                    child=app_child(stopped=True, application_id="cleanup-app"),
                )
                self.applications["cleanup-app"] = application
                self.requests[key] = application
                return application
            raise AssertionError(f"unexpected Controller request: {method} {path}")

    controller = TypedController()

    def save_profile(
        _client: object,
        current: Mapping[str, object],
        *,
        assignments: Sequence[Mapping[str, object]],
        authority_id: str,
        ledger_id: str,
    ) -> dict[str, object]:
        assert authority_id == manifest.authority.authority_id
        assert ledger_id == "dual-test-ledger"
        profile["assignments"] = [dict(item) for item in assignments]
        profile["revision"] = int(cast(int, current["revision"])) + 1
        profile["profile_digest"] = cleanup_profile_digest
        return profile

    monkeypatch.setattr(campaign_cli, "_profile_view", lambda _client, _number: profile)
    monkeypatch.setattr(campaign_cli, "_save_profile", save_profile)
    monkeypatch.setattr(
        campaign_cli.FleetProfilePreview,
        "from_dict",
        classmethod(lambda _cls, value: SimpleNamespace(to_dict=lambda: dict(value))),
    )

    preview = campaign_cli._review_or_apply_failed_dual_cleanup(
        client=controller,
        batch=batch,
        lane=lane,
        manifest=manifest,
        profile_number=7,
        authority_id=manifest.authority.authority_id,
        ledger_id="dual-test-ledger",
        campaign_id=CAMPAIGN_ID,
        ledger=ledger,
        apply=False,
        supplied_digest=None,
        clock=lambda: 0.0,
        sleeper=lambda _delay: None,
    )
    assert preview["status"] == "not-accepted"
    assert preview["spark_accepted"] is False
    assert controller.cleanup_post_count == 0
    assert not any(record["event"] == "recipe.failed" for record in ledger.records)
    assert not any(
        record["event"] == "batch.cleanup.completed" for record in ledger.records
    )

    applied = campaign_cli._review_or_apply_failed_dual_cleanup(
        client=controller,
        batch=batch,
        lane=lane,
        manifest=manifest,
        profile_number=7,
        authority_id=manifest.authority.authority_id,
        ledger_id="dual-test-ledger",
        campaign_id=CAMPAIGN_ID,
        ledger=ledger,
        apply=True,
        supplied_digest=str(preview["campaign_digest"]),
        clock=lambda: 0.0,
        sleeper=lambda _delay: None,
    )
    assert applied["status"] == "lane-terminal"
    assert (
        campaign_cli._object(applied["lane_outcome"], "failed lane outcome")["status"]
        == "recipe.failed"
    )
    assert (
        campaign_cli._object(applied["batch_release"], "batch release")["batch_id"]
        == batch.batch_id
    )
    assert controller.cleanup_post_count == 1
    failed_cleanup = next(
        record
        for record in ledger.records
        if record["event"] == "dual_recovery.failed_cleanup.completed"
    )
    cleanup_payload = cast(Mapping[str, object], failed_cleanup["payload"])
    receipt = cast(Mapping[str, object], cleanup_payload["receipt"])
    assert receipt["terminal_event"] == "canary.failed"
    assert receipt["cleanup_mode"] == "failed-dual-lane"
    stop_receipts = cast(list[Mapping[str, object]], receipt["stop_receipts"])
    assert len(stop_receipts) == 1
    assert stop_receipts[0]["run_id"] == "dual-run"
    final = cast(Mapping[str, object], stop_receipts[0]["final_observation"])
    assert final["route_state"] == "withdrawn"
    assert [
        item["state"] for item in cast(list[Mapping[str, object]], final["ranks"])
    ] == ["stopped", "stopped"]
    assert campaign_cli._batch_release_recorded(ledger, CAMPAIGN_ID, batch)
    assert not any(
        record["event"]
        in {
            "recipe.spark-accepted",
            "rank-loss.observed",
            "rank-recovery.smoke-completed",
        }
        for record in ledger.records
    )

    replay = campaign_cli._review_or_apply_failed_dual_cleanup(
        client=controller,
        batch=batch,
        lane=lane,
        manifest=manifest,
        profile_number=7,
        authority_id=manifest.authority.authority_id,
        ledger_id="dual-test-ledger",
        campaign_id=CAMPAIGN_ID,
        ledger=ledger,
        apply=True,
        supplied_digest=str(preview["campaign_digest"]),
        clock=lambda: 0.0,
        sleeper=lambda _delay: None,
    )
    assert replay["status"] == "lane-terminal"
    assert (
        campaign_cli._object(replay["batch_release"], "replayed batch release")[
            "batch_id"
        ]
        == batch.batch_id
    )
    assert controller.cleanup_post_count == 1
    assert (
        sum(
            record["event"] == "dual_recovery.failed_cleanup.reconciled"
            for record in ledger.records
        )
        == 1
    )
