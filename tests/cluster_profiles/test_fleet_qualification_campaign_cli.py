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
from dataclasses import replace
from pathlib import Path
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
            library_root / "qualification/authorities/nl-sequential-2c118a99.json"
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

    authority = {
        "schema_version": 3,
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
    authority_path.write_text(json.dumps(authority), encoding="utf-8")


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


def test_replacement_preview_rejects_a_new_run_after_acknowledgement() -> None:
    fleet = _healthy_two_spark_fleet()
    replacement = campaign_cli._assert_fleet_exclusive(fleet, replace_run_id=RUN_ID)
    assert replacement is not None
    preview = {
        "plan_digest": "a" * 64,
        "summary": {"stops": 1, "starts": 1, "uninstalls": 0},
        "steps": [{"kind": "switch", "node_ids": [NODE_A, NODE_B]}],
    }

    with pytest.raises(QualificationError, match="does not match"):
        campaign_cli._check_replacement_preview(
            preview,
            fleet=_healthy_two_spark_fleet("new-run"),
            node_ids=[NODE_A],
            replacement=replacement,
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


def test_replacement_preview_binds_stop_membership_and_plan_digest() -> None:
    fleet = _healthy_two_spark_fleet()
    replacement = campaign_cli._assert_fleet_exclusive(fleet, replace_run_id=RUN_ID)
    assert replacement is not None
    preview = {
        "plan_digest": "a" * 64,
        "summary": {"stops": 1, "starts": 1, "uninstalls": 0},
        "steps": [{"kind": "switch", "node_ids": [NODE_A, NODE_B]}],
    }

    evidence = campaign_cli._check_replacement_preview(
        preview,
        fleet=fleet,
        node_ids=[NODE_A],
        replacement=replacement,
    )

    assert evidence == {
        **replacement,
        "acknowledged_run_id": RUN_ID,
        "switch_node_ids": [NODE_A, NODE_B],
        "stop_count": 1,
        "start_count": 1,
        "profile_plan_digest": "a" * 64,
    }

    with pytest.raises(QualificationError, match="complete acknowledged run"):
        campaign_cli._check_replacement_preview(
            {
                **preview,
                "steps": [{"kind": "switch", "node_ids": [NODE_A]}],
            },
            fleet=fleet,
            node_ids=[NODE_A],
            replacement=replacement,
        )


def test_no_loaded_run_keeps_no_interruption_behavior_and_rejects_stale_ack() -> None:
    fleet = {"nodes": [_node(NODE_A)]}
    preview = {
        "plan_digest": "a" * 64,
        "summary": {"stops": 0, "starts": 1, "uninstalls": 0},
        "steps": [{"kind": "switch", "node_ids": [NODE_A]}],
    }

    assert (
        campaign_cli._check_replacement_preview(
            preview,
            fleet=fleet,
            node_ids=[NODE_A],
            replacement=None,
        )
        is None
    )
    with pytest.raises(QualificationError, match="acknowledged run"):
        campaign_cli._assert_fleet_exclusive(fleet, replace_run_id=RUN_ID)


def test_campaign_digest_binds_replacement_evidence_and_exact_plan_digest(
    tmp_path: Path,
) -> None:
    campaign_path, _package, _fixture_raw = _catalog_inputs(tmp_path)
    manifest = campaign_cli.load_manifest(campaign_path, tmp_path)
    fixtures = FixtureRegistry.load(manifest.fixture_manifest)
    base_preview = {
        "plan_digest": "a" * 64,
        "exact_preparations": {"target_node_ids": [NODE_A]},
        "replacement_interruption": {
            "acknowledged_run_id": RUN_ID,
            "member_node_ids": [NODE_A, NODE_B],
            "stop_count": 1,
            "profile_plan_digest": "a" * 64,
        },
    }
    digest = campaign_cli._preview_digest(
        manifest=manifest,
        fixtures=fixtures,
        row=_row(),
        profile={"profile_digest": "b" * 64},
        preview=base_preview,
        node_ids=[NODE_A],
        failure_node_id=None,
        profile_number=7,
    )

    changed_evidence = {
        **base_preview,
        "replacement_interruption": {
            **base_preview["replacement_interruption"],
            "acknowledged_run_id": "different-run",
        },
    }
    assert (
        campaign_cli._preview_digest(
            manifest=manifest,
            fixtures=fixtures,
            row=_row(),
            profile={"profile_digest": "b" * 64},
            preview=changed_evidence,
            node_ids=[NODE_A],
            failure_node_id=None,
            profile_number=7,
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
        campaign_cli._preview_digest(
            manifest=manifest,
            fixtures=fixtures,
            row=_row(),
            profile={"profile_digest": "b" * 64},
            preview=changed_plan,
            node_ids=[NODE_A],
            failure_node_id=None,
            profile_number=7,
        )
        != digest
    )


def test_fresh_profile_preview_persists_replacement_evidence_in_campaign_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign_path, _package, _fixture_raw = _catalog_inputs(tmp_path)
    manifest = campaign_cli.load_manifest(campaign_path, tmp_path)
    fixtures = FixtureRegistry.load(manifest.fixture_manifest)
    fleet = _healthy_two_spark_fleet()
    ledger = EvidenceLedger(tmp_path / "evidence.jsonl")
    raw_preview: dict[str, object] = {
        "plan_digest": "a" * 64,
        "summary": {"stops": 1, "starts": 1, "uninstalls": 0},
        "steps": [{"kind": "switch", "node_ids": [NODE_A, NODE_B]}],
    }
    profile = {"profile_digest": "b" * 64}

    class PreviewClient:
        def request(self, method: str, path: str) -> dict[str, object]:
            assert method == "POST"
            assert path == "/api/profile/7/preview"
            return raw_preview

    class SmokeAdapter:
        def __init__(self, _fixtures: FixtureRegistry) -> None:
            pass

        def preview(
            self,
            _detail: dict[str, object],
            *,
            recipe_key: str,
            recipe_content_sha256: str,
        ) -> dict[str, object]:
            assert recipe_key == RECIPE_KEY
            assert recipe_content_sha256 == CONTENT_SHA
            return {
                "available": True,
                "fixture_manifest_sha256": fixtures.manifest_sha256,
            }

    monkeypatch.setattr(
        campaign_cli,
        "_validate_current_recipe",
        lambda _client, _row: ({"identity": {"recipe_id": "recipe-current"}}, {}),
    )
    monkeypatch.setattr(campaign_cli, "_typed_fleet", lambda _client: fleet)
    monkeypatch.setattr(campaign_cli, "_prepare_profile", lambda **_kwargs: profile)

    def check_preview(_raw: dict[str, object], **kwargs: object) -> dict[str, object]:
        replacement = kwargs["replacement"]
        assert isinstance(replacement, dict)
        evidence = campaign_cli._check_replacement_preview(
            raw_preview,
            fleet=fleet,
            node_ids=[NODE_A],
            replacement=replacement,
        )
        assert evidence is not None
        return {
            **raw_preview,
            "assignments": [{"recipe_revision_id": REVISION_ID}],
            "exact_preparations": {"target_node_ids": [NODE_A]},
            "replacement_interruption": evidence,
        }

    monkeypatch.setattr(campaign_cli, "_check_preview", check_preview)
    monkeypatch.setattr(campaign_cli, "ArtifactJobSmokeAdapter", SmokeAdapter)

    checked, _smoke, _result = campaign_cli._fresh_profile_preview(
        client=PreviewClient(),
        manifest=manifest,
        fixtures=fixtures,
        row=_row(),
        library_root=tmp_path,
        profile_number=7,
        authority_id="test-authority",
        ledger_id="evidence.jsonl",
        node_ids=[NODE_A],
        failure_node_id=None,
        alias="test-alias",
        kind="artifact-job",
        campaign_id=CAMPAIGN_ID,
        ledger=ledger,
        expected_fleet_node_ids=[NODE_A, NODE_B],
        replace_run_id=RUN_ID,
    )

    record = ledger.recipe_records(CAMPAIGN_ID, RECIPE_KEY)[0]
    payload = record["payload"]
    assert isinstance(payload, dict)
    interruption = payload["replacement_interruption"]
    assert interruption == checked["replacement_interruption"]
    assert isinstance(interruption, dict)
    assert interruption["acknowledged_run_id"] == RUN_ID
    assert interruption["profile_plan_digest"] == raw_preview["plan_digest"]


def test_campaign_parser_accepts_exact_replacement_ack_for_preview_and_apply() -> None:
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
            "--replace-run-id",
            RUN_ID,
        ]
    )
    assert preview.replace_run_id == RUN_ID

    apply = campaign_cli._arguments(
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
            "--replace-run-id",
            RUN_ID,
            "--apply",
            "--campaign-digest",
            "a" * 64,
        ]
    )
    assert apply.replace_run_id == RUN_ID

    with pytest.raises(SystemExit):
        campaign_cli._arguments(
            [
                "--manifest",
                "campaign.json",
                "--library-root",
                "recipes",
                "--ledger",
                "evidence.jsonl",
                "--profile-number",
                "7",
                "--observe",
                "--replace-run-id",
                RUN_ID,
            ]
        )


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


def test_repository_binding_does_not_execute_tampered_working_tree_tools(
    tmp_path: Path,
) -> None:
    campaign_path, _package, _fixture_raw = _catalog_inputs(tmp_path)
    tool_marker = tmp_path / "working-tree-validator-executed"
    contracts_marker = tmp_path / "working-tree-contracts-executed"
    tool_path = tmp_path / "tools" / "build-catalog-index"
    contracts_init = (
        tmp_path / "contracts" / "src" / "vonk_forge_contracts" / "__init__.py"
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


def test_resume_and_cleanup_require_durable_intent_before_controller_mutation(
    tmp_path: Path,
) -> None:
    client = _NoRequests()
    ledger = EvidenceLedger(tmp_path / "pending-load.jsonl")
    with pytest.raises(QualificationError, match="no durable profile load intent"):
        campaign_cli._resume_load_and_smoke(
            client=client,
            manifest=cast(campaign_cli.CampaignManifest, None),
            fixtures=cast(FixtureRegistry, None),
            row=_row(),
            campaign_id=CAMPAIGN_ID,
            ledger=ledger,
            profile_number=7,
            clock=lambda: 0.0,
            sleeper=lambda _delay: None,
        )

    empty_ledger = EvidenceLedger(tmp_path / "cleanup.jsonl")
    with pytest.raises(
        QualificationError, match="durable reviewed load and canary receipts"
    ):
        campaign_cli._profile_cleanup(
            client=client,
            profile_number=7,
            authority_id="authority",
            ledger_id="ledger",
            row=_row(),
            campaign_id=CAMPAIGN_ID,
            run_id=RUN_ID,
            alias="test-alias",
            node_ids=[NODE_A],
            ledger=empty_ledger,
            options=cast(campaign_cli.CampaignManifest, None),
            clock=lambda: 0.0,
            sleeper=lambda _delay: None,
        )
    assert client.calls == []


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


def test_cleanup_preview_stop_scope_must_match_exact_campaign_run() -> None:
    from datetime import UTC, datetime

    from cluster_profiles.generated_control.models.fleet_profile_plan_step import (
        FleetProfilePlanStep,
    )
    from cluster_profiles.generated_control.models.fleet_profile_plan_summary import (
        FleetProfilePlanSummary,
    )
    from cluster_profiles.generated_control.models.fleet_profile_scope_preview import (
        FleetProfileScopePreview,
    )

    preview = campaign_cli.FleetProfilePreview(
        allowed=True,
        assignments=[],
        generated_at=datetime(2026, 9, 24, tzinfo=UTC),
        plan_digest="a" * 64,
        profile_digest="b" * 64,
        profile_id="12345678-1234-4123-8123-123456789abc",
        profile_name="Qualification profile",
        reasons=[],
        scope=FleetProfileScopePreview(node_ids=[NODE_A], idle_node_ids=[NODE_A]),
        steps=[
            FleetProfilePlanStep(
                index=0,
                kind="switch",
                node_ids=[NODE_A],
                label="Stop workload",
            )
        ],
        summary=FleetProfilePlanSummary(
            already_correct=0,
            blockers=0,
            builds=0,
            distributions=0,
            installs=0,
            placements=0,
            starts=0,
            stops=1,
            uninstalls=0,
        ),
    ).to_dict()
    fleet = {"nodes": [_node(NODE_A, loaded=[{"run_id": RUN_ID}])]}
    profile_view = {
        "id": "12345678-1234-4123-8123-123456789abc",
        "profile_digest": "b" * 64,
        "assignments": [],
    }
    assert (
        campaign_cli._check_preview(
            preview,
            profile=profile_view,
            fleet=fleet,
            row=_row(),
            node_ids=[NODE_A],
            cleanup=True,
            owned_run_ids={RUN_ID},
        )["plan_digest"]
        == "a" * 64
    )

    wrong_scope = {
        **preview,
        "steps": [{**preview["steps"][0], "node_ids": [NODE_B]}],
    }
    with pytest.raises(QualificationError, match="exact campaign run's Sparks"):
        campaign_cli._check_preview(
            wrong_scope,
            profile=profile_view,
            fleet=fleet,
            row=_row(),
            node_ids=[NODE_A],
            cleanup=True,
            owned_run_ids={RUN_ID},
        )


def test_cleanup_completion_is_written_before_restart_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = EvidenceLedger(tmp_path / "cleanup-order.jsonl")

    def record_baseline(**kwargs: object) -> None:
        ledger.append(
            "host-restart.baseline",
            plan_digest=CAMPAIGN_ID,
            recipe=RECIPE_KEY,
            payload={"run_id": kwargs["run_id"]},
        )

    monkeypatch.setattr(campaign_cli, "_record_restart_baseline", record_baseline)
    campaign_cli._record_cleanup_then_restart_baseline(
        receipt={"application_id": "cleanup-app"},
        client=object(),
        row=_row(),
        campaign_id=CAMPAIGN_ID,
        run_id=RUN_ID,
        alias="test-alias",
        node_ids=[NODE_A],
        ledger=ledger,
    )
    assert [
        record["event"] for record in ledger.recipe_records(CAMPAIGN_ID, RECIPE_KEY)
    ] == ["profile.cleanup.completed", "host-restart.baseline"]


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


def test_rank_recovery_rejects_a_permuted_spark_rank_assignment() -> None:
    fleet = {
        "nodes": [
            _node(NODE_A, loaded=[_loaded_run_presence(rank=1)]),
            _node(NODE_B, loaded=[_loaded_run_presence(rank=0)]),
        ]
    }

    with pytest.raises(QualificationError, match="exact canary Spark/rank mapping"):
        campaign_cli._rank_recovered(
            _ExistingEndpoint(),
            fleet,
            run_id=RUN_ID,
            revision_id="revision-current",
            alias="current-service",
            node_ids=[NODE_A, NODE_B],
            node_to_rank={NODE_A: 0, NODE_B: 1},
        )


def test_offline_restart_requires_observed_downtime_and_changed_live_boot_id(
    tmp_path: Path,
) -> None:
    row = _row()
    ledger = EvidenceLedger(tmp_path / "evidence.jsonl")
    ledger.append(
        "host-restart.baseline",
        plan_digest=CAMPAIGN_ID,
        recipe=RECIPE_KEY,
        payload={
            "nodes": {NODE_A: "boot-before"},
            "route_alias": "test-alias",
            "run_id": RUN_ID,
        },
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
    reason = unchanged["reason"]
    assert isinstance(reason, str)
    assert "unchanged boot ID" in reason

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
        payload={
            "nodes": {NODE_A: "boot-before"},
            "route_alias": "test-alias",
            "run_id": RUN_ID,
        },
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

    def presence(node_id: str, rank: int, *, recovered: bool = False) -> dict[str, object]:
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
                presence(node_id, rank)
                for node_id, rank in node_to_rank.items()
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


def test_spark_acceptance_rejects_permuted_recovered_rank_mapping(
    tmp_path: Path,
) -> None:
    exact = EvidenceLedger(tmp_path / "exact-rank-evidence.jsonl")
    _append_two_node_recipe_evidence(exact)
    accepted = campaign_cli._accept_if_complete(
        row=_row(node_count=2),
        campaign_id=CAMPAIGN_ID,
        ledger=exact,
        node_ids=[NODE_A, NODE_B],
    )
    assert accepted["status"] == "spark-accepted"

    permuted = EvidenceLedger(tmp_path / "permuted-rank-evidence.jsonl")
    _append_two_node_recipe_evidence(
        permuted,
        recovered_node_to_rank={NODE_A: 1, NODE_B: 0},
    )
    with pytest.raises(QualificationError, match="exact canary Spark/rank mapping"):
        campaign_cli._accept_if_complete(
            row=_row(node_count=2),
        campaign_id=CAMPAIGN_ID,
        ledger=permuted,
        node_ids=[NODE_A, NODE_B],
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


def test_spark_acceptance_is_reachable_only_when_cleanup_precedes_restart_baseline(
    tmp_path: Path,
) -> None:
    complete = EvidenceLedger(tmp_path / "ordered-evidence.jsonl")
    _append_single_recipe_evidence(complete)
    result = campaign_cli._accept_if_complete(
        row=_row(), campaign_id=CAMPAIGN_ID, ledger=complete, node_ids=[NODE_A]
    )
    assert result["status"] == "spark-accepted"

    reordered = EvidenceLedger(tmp_path / "reordered-evidence.jsonl")
    _append_single_recipe_evidence(reordered, cleanup_before_baseline=False)
    with pytest.raises(QualificationError, match="out of sequence"):
        campaign_cli._accept_if_complete(
            row=_row(), campaign_id=CAMPAIGN_ID, ledger=reordered, node_ids=[NODE_A]
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
    campaign_cli._operator_gate(preview, row)
    with pytest.raises(QualificationError, match="accept-operator-gate"):
        campaign_cli._operator_gate(
            Namespace(apply=True, accept_operator_gate=[], accept_capacity_review=[]),
            row,
        )
    missing_capacity = Namespace(
        apply=True, accept_operator_gate=[RECIPE_KEY], accept_capacity_review=[]
    )
    with pytest.raises(QualificationError, match="accept-capacity-review"):
        campaign_cli._operator_gate(missing_capacity, row)

    accepted = Namespace(
        apply=True,
        accept_operator_gate=[RECIPE_KEY],
        accept_capacity_review=[RECIPE_KEY],
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
