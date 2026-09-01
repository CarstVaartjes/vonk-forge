from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from cluster_profiles import fleet_qualification_campaign_cli as campaign_cli
from cluster_profiles.fleet_qualification import EvidenceLedger, QualificationError
from cluster_profiles.qualification_locking import node_locks

NODE_A = "spk_" + "1" * 32
NODE_B = "spk_" + "2" * 32
PACKAGED_AUTHORITY_LOADER = campaign_cli._load_authority
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _reviewed_test_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    authority = campaign_cli.CampaignAuthority(
        authority_id="test-nl-single",
        authority_sha256="d" * 64,
        repository="test/recipes",
        commit="b" * 40,
        catalog_index_sha256="e" * 64,
        catalog_recipe_count=3,
        jurisdiction="NL",
        actionable_recipe_keys=("vonk/a", "vonk/b", "vonk/c"),
    )

    def load(authority_id: str) -> campaign_cli.CampaignAuthority:
        if authority_id != authority.authority_id:
            raise QualificationError("qualification authority is not reviewed")
        return authority

    monkeypatch.setattr(campaign_cli, "_load_authority", load)


class _Client:
    def __init__(self, *, catalog_commit: str = "b" * 40) -> None:
        self.catalog_commit = catalog_commit

    def request(
        self,
        method: str,
        path: str,
        payload: object = None,
        *,
        extra_headers: object = None,
        query: object = None,
    ) -> dict[str, object]:
        del payload, extra_headers, query
        if (method, path) == ("GET", "/api/v1/fleet"):
            return {
                "authority_revision": "a" * 40,
                "event_cursor": 1,
                "nodes": [
                    {
                        "id": node_id,
                        "connection": {"online_state": "online"},
                        "inventory": {
                            "host_memory_free_bytes": 120_000_000_000,
                            "disk_free_bytes": 500_000_000_000,
                        },
                    }
                    for node_id in (NODE_A, NODE_B)
                ],
            }
        if (method, path) == ("GET", "/api/v1/catalog/public-recipes"):
            return {
                "repository": "test/recipes",
                "commit": self.catalog_commit,
                "recipes": [_recipe("a"), _recipe("b"), _recipe("c")],
            }
        raise AssertionError((method, path))


def _recipe(slug: str) -> dict[str, object]:
    return {
        "publisher": "vonk",
        "slug": slug,
        "uri": f"vonk+github://test/recipes/{slug}.json?ref={'b' * 40}&sha256={'c' * 64}",
        "content_sha256": "c" * 64,
        "release_version": "1.0.0",
        "node_count": 1,
        "topology_roles": [
            {
                "name": "entrypoint",
                "count": 1,
                "endpoint_owner": True,
                "disk": {
                    "image_bytes": 1,
                    "artifact_bytes": 1,
                    "staging_bytes": 1,
                    "cache_bytes": 1,
                    "rollback_bytes": 0,
                    "safety_margin_bytes": 1,
                },
            }
        ],
        "expected_download_bytes": 10,
        "artifact_count": 0,
        "artifact_identities": [],
        "temporary_build_bytes_per_node": 0,
        "maximum_installed_bytes_per_node": 20,
        "maximum_runtime_memory_bytes_per_node": 30,
        "execution_readiness": "executable",
        "execution_readiness_detail": "complete",
        "local": {"status": "not-imported"},
    }


def _document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "qualification_authority": "test-nl-single",
        "options": {
            "jurisdiction": "NL",
            "cleanup": "stop",
            "operation_timeout_seconds": 60,
            "poll_interval_seconds": 0.1,
        },
        "lanes": [
            {
                "name": "first",
                "node_id": NODE_A,
                "recipes": ["vonk/a"],
                "ledger": "evidence/first.jsonl",
                "plan_output": "plans/first.json",
            },
            {
                "name": "second",
                "node_id": NODE_B,
                "recipes": ["vonk/b", "vonk/c"],
                "ledger": "evidence/second.jsonl",
                "plan_output": "plans/second.json",
            },
        ],
    }


def _write_manifest(tmp_path: Path, document: object | None = None) -> Path:
    path = tmp_path / "campaign.json"
    path.write_text(json.dumps(document or _document()), encoding="utf-8")
    return path


def test_manifest_requires_an_exact_disjoint_complete_partition(tmp_path: Path) -> None:
    overlapping = _document()
    overlapping["lanes"][1]["recipes"] = ["vonk/a"]  # type: ignore[index]
    path = _write_manifest(tmp_path, overlapping)
    with pytest.raises(QualificationError, match="assigns recipe more than once"):
        campaign_cli.load_manifest(path)

    missing = _document()
    missing["lanes"][1]["recipes"] = ["vonk/b"]  # type: ignore[index]
    path = _write_manifest(tmp_path, missing)
    with pytest.raises(
        QualificationError, match=r"missing=\['vonk/c'\], unexpected=\[\]"
    ):
        campaign_cli.load_manifest(path)

    substituted = _document()
    substituted["lanes"][1]["recipes"] = ["vonk/b", "vonk/d"]  # type: ignore[index]
    path = _write_manifest(tmp_path, substituted)
    with pytest.raises(
        QualificationError,
        match=r"missing=\['vonk/c'\], unexpected=\['vonk/d'\]",
    ):
        campaign_cli.load_manifest(path)

    duplicate_output = _document()
    duplicate_output["lanes"][1]["plan_output"] = (  # type: ignore[index]
        "evidence/first.jsonl"
    )
    path = _write_manifest(tmp_path, duplicate_output)
    with pytest.raises(QualificationError, match="must all be unique"):
        campaign_cli.load_manifest(path)

    overwrite_manifest = _document()
    overwrite_manifest["lanes"][0]["plan_output"] = "campaign.json"  # type: ignore[index]
    path = _write_manifest(tmp_path, overwrite_manifest)
    with pytest.raises(QualificationError, match="overwrite an input file"):
        campaign_cli.load_manifest(path)


def test_manifest_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    path = tmp_path / "campaign.json"
    path.write_text(
        '{"schema_version":1,"schema_version":1,"qualification_authority":"test-nl-single","lanes":[]}',
        encoding="utf-8",
    )
    with pytest.raises(QualificationError, match="duplicate key: schema_version"):
        campaign_cli.load_manifest(path)


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


def test_node_lock_environment_roots_must_be_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del tmp_path
    monkeypatch.setenv("VONK_QUALIFICATION_LOCK_DIR", "relative-locks")
    with (
        pytest.raises(QualificationError, match="must be an absolute path"),
        node_locks([NODE_A]),
    ):
        pass

    monkeypatch.delenv("VONK_QUALIFICATION_LOCK_DIR")
    monkeypatch.setenv("XDG_STATE_HOME", "relative-state")
    with (
        pytest.raises(QualificationError, match="must be an absolute path"),
        node_locks([NODE_A]),
    ):
        pass


def test_node_lock_is_shared_across_processes_with_different_working_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_cwd = tmp_path / "first"
    second_cwd = tmp_path / "second"
    first_cwd.mkdir()
    second_cwd.mkdir()
    monkeypatch.delenv("VONK_QUALIFICATION_LOCK_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    environment = os.environ.copy()
    source_root = Path(campaign_cli.__file__).resolve().parents[1]
    environment["PYTHONPATH"] = str(source_root)
    monkeypatch.chdir(first_cwd)
    script = f"""
from cluster_profiles.fleet_qualification import QualificationError
from cluster_profiles.qualification_locking import node_locks
try:
    with node_locks([{NODE_A!r}]):
        raise SystemExit(3)
except QualificationError as error:
    if "owns controller node" not in str(error):
        raise
"""
    with node_locks([NODE_A]):
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=second_cwd,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
    assert completed.returncode == 0, completed.stderr


def test_packaged_authority_binds_reviewed_e996_catalog_closure() -> None:
    authority = PACKAGED_AUTHORITY_LOADER("nl-single-spark-e996f025")
    assert authority.repository == "CarstVaartjes/vonk-forge-recipes"
    assert authority.commit == "e996f025b9f402e352376e56964e02b3cd392fcc"
    assert (
        authority.catalog_index_sha256
        == "a02d80639af37f518a9399f0a7a3d035c9269fe8a20b3ed0f89feb1fdd06294c"
    )
    assert authority.catalog_recipe_count == 69
    assert authority.jurisdiction == "NL"
    assert len(authority.actionable_recipe_keys) == 59
    assert (
        "vonk-forge/hunyuan3d-omni-pytorch-single"
        not in authority.actionable_recipe_keys
    )


def test_packaged_authority_binds_reviewed_02ae_catalog_closure() -> None:
    authority = PACKAGED_AUTHORITY_LOADER("nl-single-spark-02ae8bb5")
    assert authority.repository == "CarstVaartjes/vonk-forge-recipes"
    assert authority.commit == "02ae8bb5065919e263183f59637f4d8954a7334a"
    assert (
        authority.catalog_index_sha256
        == "165be2692acafa1fe51345d83dbdd3b3d07ba308463a031a98e9bc563e0da5c5"
    )
    assert authority.catalog_recipe_count == 70
    assert authority.jurisdiction == "NL"
    assert len(authority.actionable_recipe_keys) == 59
    assert "vonk-forge/qwen-image-edit-2511-fp8mixed-comfyui-single" in (
        authority.actionable_recipe_keys
    )
    assert {
        "vonk-forge/hunyuan3d-omni-pytorch-single",
        "vonk-forge/hunyuanocr-1-5-vllm-dflash-single",
    }.isdisjoint(authority.actionable_recipe_keys)


def test_packaged_authority_binds_current_745a_catalog_closure() -> None:
    authority = PACKAGED_AUTHORITY_LOADER("nl-single-spark-745a42b5")

    assert authority.repository == "CarstVaartjes/vonk-forge-recipes"
    assert authority.commit == "745a42b5daa3ac8010483421c45235e32e866672"
    assert (
        authority.catalog_index_sha256
        == "e864b644e374c76f594bcc4a394348844d4e5aa8d7dc78142f7d596b1fc2b55e"
    )
    assert authority.catalog_recipe_count == 76
    assert authority.jurisdiction == "NL"
    assert [
        len(authority.actionable_recipe_keys),
        len(authority.capacity_blocked_recipe_keys),
        len(authority.legal_blocked_recipe_keys),
        len(authority.dual_spark_recipe_keys),
        len(authority.unsupported_topology_recipe_keys),
    ] == [49, 8, 9, 6, 4]
    categories = (
        authority.actionable_recipe_keys,
        authority.capacity_blocked_recipe_keys,
        authority.legal_blocked_recipe_keys,
        authority.dual_spark_recipe_keys,
        authority.unsupported_topology_recipe_keys,
    )
    classified = [recipe for category in categories for recipe in category]
    assert len(classified) == len(set(classified)) == 76
    assert {
        "vonk-forge/hunyuanocr-1-5-vllm-dflash-single",
        "vonk-forge/minimax-h3-diffusers-single",
        "vonk-forge/minimax-h3-fl2va-diffusers-single",
    }.issubset(authority.legal_blocked_recipe_keys)
    assert {
        "vonk-forge/gemma-4-26b-a4b-vllm028-single",
        "vonk-forge/lfm2-5-vl-3b-vllm028-single",
    }.issubset(authority.actionable_recipe_keys)


def test_packaged_authority_binds_current_224c_catalog_closure() -> None:
    authority = PACKAGED_AUTHORITY_LOADER("nl-single-spark-224c4cc7")

    assert authority.authority_sha256 == (
        "439514622665e34b376a657f65c20c556afaf0fb60f2d0704680fe8b1ee9c47b"
    )
    assert authority.repository == "CarstVaartjes/vonk-forge-recipes"
    assert authority.commit == "224c4cc72a9aab2cb1ed9c0f439036451ea22b1b"
    assert (
        authority.catalog_index_sha256
        == "068921e7e7a104bff144a2c6e99ea042a3ce4f0fcb4b356ae5802d04dcb850f1"
    )
    assert authority.catalog_recipe_count == 83
    assert authority.jurisdiction == "NL"
    assert [
        len(authority.actionable_recipe_keys),
        len(authority.capacity_blocked_recipe_keys),
        len(authority.legal_blocked_recipe_keys),
        len(authority.dual_spark_recipe_keys),
        len(authority.unsupported_topology_recipe_keys),
    ] == [57, 5, 9, 8, 4]
    categories = (
        authority.actionable_recipe_keys,
        authority.capacity_blocked_recipe_keys,
        authority.legal_blocked_recipe_keys,
        authority.dual_spark_recipe_keys,
        authority.unsupported_topology_recipe_keys,
    )
    classified = [recipe for category in categories for recipe in category]
    assert len(classified) == len(set(classified)) == 83
    assert (
        "vonk-forge/glm-5-3-flash-nvfp4-ablit-l15-43-dflash2-vllm-dual"
        in authority.dual_spark_recipe_keys
    )


def test_checked_in_224c_physical_campaign_preserves_single_spark_partition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(campaign_cli, "_load_authority", PACKAGED_AUTHORITY_LOADER)
    current = campaign_cli.load_manifest(
        REPOSITORY_ROOT / "config/qualification/nl-single-spark-224c4cc7.json"
    )
    historical = campaign_cli.load_manifest(
        REPOSITORY_ROOT / "config/qualification/nl-single-spark-9a6c7516.json"
    )

    assert current.manifest_sha256 == (
        "fb04211029a15eaab57ba00516b2d6dd0353bb8ef38d9f43a7bde27568e7af0f"
    )
    assert current.cleanup == "stop"
    assert current.jurisdiction == "NL"
    assert [
        (lane.name, lane.node_id, len(lane.recipes)) for lane in current.lanes
    ] == [
        ("spark-3542", "spk_2818d189042b4c77aefa7796f4befd23", 29),
        ("spark-2297", "spk_9a86fdbab116442ab6707bf4181a3c1c", 28),
    ]
    assert [lane.recipes for lane in current.lanes] == [
        lane.recipes for lane in historical.lanes
    ]
    assigned = [recipe for lane in current.lanes for recipe in lane.recipes]
    assert len(assigned) == len(set(assigned)) == 57
    assert set(assigned) == set(current.authority.actionable_recipe_keys)
    state_root = (
        REPOSITORY_ROOT / ".state/qualification/nl-single-spark-224c4cc7"
    ).resolve()
    for lane in current.lanes:
        assert lane.ledger.is_relative_to(state_root)
        assert lane.plan_output.is_relative_to(state_root)


def test_packaged_authority_binds_historical_9a6c_catalog_closure() -> None:
    authority = PACKAGED_AUTHORITY_LOADER("nl-single-spark-9a6c7516")

    assert authority.authority_sha256 == (
        "e6609a4dcac0525ff2fc7dab84b3484c0a7f07233d0c05b6a74aedada7c77408"
    )
    assert authority.repository == "CarstVaartjes/vonk-forge-recipes"
    assert authority.commit == "9a6c75167dbe6b66fd211cc5e37aaecdae175d00"
    assert (
        authority.catalog_index_sha256
        == "0697cc15026fd9789d00ba6289b9895e13ff1d68001c175c37981ecaa93f91b8"
    )
    assert authority.catalog_recipe_count == 82
    assert authority.jurisdiction == "NL"
    assert [
        len(authority.actionable_recipe_keys),
        len(authority.capacity_blocked_recipe_keys),
        len(authority.legal_blocked_recipe_keys),
        len(authority.dual_spark_recipe_keys),
        len(authority.unsupported_topology_recipe_keys),
    ] == [57, 5, 9, 7, 4]
    categories = (
        authority.actionable_recipe_keys,
        authority.capacity_blocked_recipe_keys,
        authority.legal_blocked_recipe_keys,
        authority.dual_spark_recipe_keys,
        authority.unsupported_topology_recipe_keys,
    )
    classified = [recipe for category in categories for recipe in category]
    assert len(classified) == len(set(classified)) == 82
    assert {
        "vonk-forge/deepseek-v4-flash-vision-exp-mia-dual",
        "vonk-forge/glm-5-3-flash-nvfp4-vllm-dual",
    }.issubset(authority.dual_spark_recipe_keys)
    assert {
        "vonk-forge/lfm2-5-vl-3b-vllm-single",
        "vonk-forge/lfm2-5-vl-3b-vllm028-single",
        "vonk-forge/ltx-2-19b-dev-bf16-diffusers-single",
    }.issubset(authority.actionable_recipe_keys)


def test_checked_in_9a6c_historical_campaign_preserves_reviewed_partition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(campaign_cli, "_load_authority", PACKAGED_AUTHORITY_LOADER)
    current = campaign_cli.load_manifest(
        REPOSITORY_ROOT / "config/qualification/nl-single-spark-9a6c7516.json"
    )
    historical = campaign_cli.load_manifest(
        REPOSITORY_ROOT / "config/qualification/nl-single-spark-f8d43aac.json"
    )

    assert current.manifest_sha256 == (
        "ef4b919216d98849ebb453e8fbe74711010d675d8111e4d214083d32a3a8c684"
    )
    assert current.cleanup == "stop"
    assert current.jurisdiction == "NL"
    assert [
        (lane.name, lane.node_id, len(lane.recipes)) for lane in current.lanes
    ] == [
        ("spark-3542", "spk_2818d189042b4c77aefa7796f4befd23", 29),
        ("spark-2297", "spk_9a86fdbab116442ab6707bf4181a3c1c", 28),
    ]
    assert [lane.recipes for lane in current.lanes] == [
        lane.recipes for lane in historical.lanes
    ]
    assigned = [recipe for lane in current.lanes for recipe in lane.recipes]
    assert len(assigned) == len(set(assigned)) == 57
    assert set(assigned) == set(current.authority.actionable_recipe_keys)
    state_root = (
        REPOSITORY_ROOT / ".state/qualification/nl-single-spark-9a6c7516"
    ).resolve()
    for lane in current.lanes:
        assert lane.ledger.is_relative_to(state_root)
        assert lane.plan_output.is_relative_to(state_root)


def test_packaged_authority_binds_historical_f8d43_catalog_closure() -> None:
    authority = PACKAGED_AUTHORITY_LOADER("nl-single-spark-f8d43aac")

    assert authority.authority_sha256 == (
        "8b2b362a83175146f49c936415b3238f232e827aafff6212897df8361cfc8efd"
    )
    assert authority.repository == "CarstVaartjes/vonk-forge-recipes"
    assert authority.commit == "f8d43aacbaa16c016697be684bf688ef3b81932a"
    assert (
        authority.catalog_index_sha256
        == "88386d53d22bbd2ce5cac7676f8dcf8aeabb565c1b8e6d17afab796311ec71ca"
    )
    assert authority.catalog_recipe_count == 82
    assert authority.jurisdiction == "NL"
    assert [
        len(authority.actionable_recipe_keys),
        len(authority.capacity_blocked_recipe_keys),
        len(authority.legal_blocked_recipe_keys),
        len(authority.dual_spark_recipe_keys),
        len(authority.unsupported_topology_recipe_keys),
    ] == [57, 5, 9, 7, 4]
    categories = (
        authority.actionable_recipe_keys,
        authority.capacity_blocked_recipe_keys,
        authority.legal_blocked_recipe_keys,
        authority.dual_spark_recipe_keys,
        authority.unsupported_topology_recipe_keys,
    )
    classified = [recipe for category in categories for recipe in category]
    assert len(classified) == len(set(classified)) == 82
    assert {
        "vonk-forge/hunyuanocr-1-5-vllm-dflash-single",
        "vonk-forge/minimax-h3-diffusers-single",
        "vonk-forge/minimax-h3-fl2va-diffusers-single",
    }.issubset(authority.legal_blocked_recipe_keys)
    assert {
        "vonk-forge/deepseek-v4-flash-0731-sparkinfer-single",
        "vonk-forge/laguna-s-2-1-nvfp4-vllm-single",
        "vonk-forge/ltx-2-5-22b-distilled-bf16-diffusers-single",
    }.issubset(authority.capacity_blocked_recipe_keys)
    assert {
        "vonk-forge/deepseek-v4-flash-0731-sparkinfer-target-only-canary-single",
        "vonk-forge/laguna-s-2-1-nvfp4-vllm-low-memory-canary-single",
        "vonk-forge/ltx-2-5-22b-distilled-fp8-cast-diffusers-single",
        "vonk-forge/nemotron-3-5-lightning-dspark-lowmem-canary-single",
        "vonk-forge/wan-dancer-14b-disk-offload-pytorch-single",
    }.issubset(authority.actionable_recipe_keys)


def test_checked_in_f8d43_physical_campaign_is_complete_and_dependency_ordered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(campaign_cli, "_load_authority", PACKAGED_AUTHORITY_LOADER)
    manifest = campaign_cli.load_manifest(
        REPOSITORY_ROOT / "config/qualification/nl-single-spark-f8d43aac.json"
    )

    assert manifest.manifest_sha256 == (
        "f6dbe17d8b399ccb0df78ce6193a6cc18e1401d2e7fb2f0efe3dcb672183826e"
    )
    assert manifest.cleanup == "stop"
    assert manifest.jurisdiction == "NL"
    assert [
        (lane.name, lane.node_id, len(lane.recipes)) for lane in manifest.lanes
    ] == [
        ("spark-3542", "spk_2818d189042b4c77aefa7796f4befd23", 29),
        ("spark-2297", "spk_9a86fdbab116442ab6707bf4181a3c1c", 28),
    ]
    assigned = [recipe for lane in manifest.lanes for recipe in lane.recipes]
    assert len(assigned) == len(set(assigned)) == 57
    assert set(assigned) == set(manifest.authority.actionable_recipe_keys)
    assert set(assigned).isdisjoint(manifest.authority.legal_blocked_recipe_keys)
    assert set(assigned).isdisjoint(manifest.authority.capacity_blocked_recipe_keys)

    lanes = {lane.name: list(lane.recipes) for lane in manifest.lanes}
    first = lanes["spark-3542"]
    second = lanes["spark-2297"]
    for earlier, later in (
        (
            "vonk-forge/mova-360p-diffusers-single",
            "vonk-forge/mova-720p-diffusers-single",
        ),
        (
            "vonk-forge/step1x-3d-geometry-pytorch-single",
            "vonk-forge/step1x-3d-label-geometry-pytorch-single",
        ),
        (
            "vonk-forge/step1x-3d-label-geometry-pytorch-single",
            "vonk-forge/step1x-3d-texture-pytorch-single",
        ),
        (
            "vonk-forge/qwen3-8-27b-fp8-vllm-single",
            "vonk-forge/qwen3-8-27b-vllm-single",
        ),
        (
            "vonk-forge/wan-2-2-ti2v-5b-comfyui-single",
            "vonk-forge/wan-2-2-i2v-14b-comfyui-single",
        ),
    ):
        assert first.index(earlier) < first.index(later)
    for earlier, later in (
        (
            "vonk-forge/deepseek-v4-flash-0731-ds4-single",
            "vonk-forge/deepseek-v4-flash-0731-ds4-dspark-latency-single",
        ),
        (
            "vonk-forge/flux-2-klein-4b-nvfp4-comfyui-single",
            "vonk-forge/flux-2-klein-4b-comfyui-single",
        ),
        (
            "vonk-forge/qwen-image-2512-fp8-lightning-comfyui-single",
            "vonk-forge/qwen-image-2512-comfyui-single",
        ),
        (
            "vonk-forge/qwen-image-edit-2511-fp8mixed-comfyui-single",
            "vonk-forge/qwen-image-edit-2511-comfyui-single",
        ),
        (
            "vonk-forge/ltx-2-19b-distilled-fp8-diffusers-single",
            "vonk-forge/ltx-2-19b-distilled-diffusers-single",
        ),
    ):
        assert second.index(earlier) < second.index(later)

    state_root = (
        REPOSITORY_ROOT / ".state/qualification/nl-single-spark-f8d43aac"
    ).resolve()
    for lane in manifest.lanes:
        assert lane.ledger.is_relative_to(state_root)
        assert lane.plan_output.is_relative_to(state_root)


def test_checked_in_745a_physical_campaign_is_the_exact_reviewed_partition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(campaign_cli, "_load_authority", PACKAGED_AUTHORITY_LOADER)
    manifest = campaign_cli.load_manifest(
        REPOSITORY_ROOT / "config/qualification/nl-single-spark-745a42b5.json"
    )

    assert manifest.manifest_sha256 == (
        "c13b5881fc561aae64f305abb7cef21d193b75b4cedbfc58e4511b3f6f2dc386"
    )
    assert manifest.cleanup == "stop"
    assert manifest.jurisdiction == "NL"
    assert [
        (lane.name, lane.node_id, len(lane.recipes)) for lane in manifest.lanes
    ] == [
        ("spark-3542", "spk_2818d189042b4c77aefa7796f4befd23", 25),
        ("spark-2297", "spk_9a86fdbab116442ab6707bf4181a3c1c", 24),
    ]
    assigned = [recipe for lane in manifest.lanes for recipe in lane.recipes]
    assert len(assigned) == len(set(assigned)) == 49
    assert set(assigned) == set(manifest.authority.actionable_recipe_keys)
    assert set(assigned).isdisjoint(manifest.authority.legal_blocked_recipe_keys)
    assert set(assigned).isdisjoint(manifest.authority.capacity_blocked_recipe_keys)
    state_root = (
        REPOSITORY_ROOT / ".state/qualification/nl-single-spark-745a42b5"
    ).resolve()
    for lane in manifest.lanes:
        assert lane.ledger.is_relative_to(state_root)
        assert lane.plan_output.is_relative_to(state_root)


def test_checked_in_02ae_physical_campaign_is_the_exact_reviewed_partition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(campaign_cli, "_load_authority", PACKAGED_AUTHORITY_LOADER)
    manifest = campaign_cli.load_manifest(
        REPOSITORY_ROOT / "config/qualification/nl-single-spark-02ae8bb5.json"
    )

    assert manifest.manifest_sha256 == (
        "7cbf48df404bd1bd656579c0a8823189abdfb04703e09808f0549874ca7e1939"
    )
    assert manifest.cleanup == "stop"
    assert manifest.jurisdiction == "NL"
    assert [
        (lane.name, lane.node_id, len(lane.recipes)) for lane in manifest.lanes
    ] == [
        ("spark-3542", "spk_2818d189042b4c77aefa7796f4befd23", 29),
        ("spark-2297", "spk_9a86fdbab116442ab6707bf4181a3c1c", 30),
    ]
    assigned = [recipe for lane in manifest.lanes for recipe in lane.recipes]
    assert len(assigned) == len(set(assigned)) == 59
    assert set(assigned) == set(manifest.authority.actionable_recipe_keys)
    state_root = (
        REPOSITORY_ROOT / ".state/qualification/nl-single-spark-02ae8bb5"
    ).resolve()
    for lane in manifest.lanes:
        assert lane.ledger.is_relative_to(state_root)
        assert lane.plan_output.is_relative_to(state_root)


def test_02ae_physical_runbook_matches_capacity_and_residency_contract() -> None:
    runbook = (
        REPOSITORY_ROOT / "docs/runbooks/physical-qualification-02ae8bb5.md"
    ).read_text(encoding="utf-8")
    normalized = " ".join(runbook.split())

    assert "Preview does not create `capacity.plan.created`" in runbook
    assert "before its first install" in normalized
    assert "`automatic_eviction` remains false" in runbook
    assert ".payload.complete" not in runbook
    assert runbook.count(".payload.installation_inventory_complete") >= 2
    assert "($records | last | .payload.blocked) == 0" in runbook
    assert "unique | length) == 59" in runbook


def test_every_packaged_authority_is_explicitly_mapped() -> None:
    authority_directory = (
        Path(campaign_cli.__file__).resolve().parent / "qualification_authorities"
    )
    assert set(campaign_cli._AUTHORITY_FILES.values()) == {
        path.name for path in authority_directory.glob("*.json")
    }


def test_preview_rejects_catalog_commit_drift_before_writing_plans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = _write_manifest(tmp_path)
    monkeypatch.setenv("VONK_QUALIFICATION_LOCK_DIR", str(tmp_path / "locks"))

    with pytest.raises(QualificationError, match="public catalog drifted"):
        campaign_cli.run(
            ["--manifest", str(manifest_path)],
            client_factory=lambda: _Client(catalog_commit="c" * 40),
        )
    assert not (tmp_path / "plans" / "first.json").exists()


def test_preview_writes_both_private_plans_and_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = _write_manifest(tmp_path)
    monkeypatch.setenv("VONK_QUALIFICATION_LOCK_DIR", str(tmp_path / "locks"))

    result = campaign_cli.run(
        ["--manifest", str(manifest_path)], client_factory=_Client
    )

    assert result["mode"] == "preview"
    assert len(str(result["campaign_digest"])) == 64
    assert {lane["recipe_count"] for lane in result["lanes"]} == {1, 2}  # type: ignore[index]
    assert result["qualification_authority"] == "test-nl-single"
    for lane_name in ("first", "second"):
        plan_path = tmp_path / "plans" / f"{lane_name}.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        assert plan["campaign_digest"] == result["campaign_digest"]
        assert plan["lane"] == lane_name
        assert stat.S_IMODE(plan_path.stat().st_mode) == 0o600
        ledger = EvidenceLedger(tmp_path / "evidence" / f"{lane_name}.jsonl")
        assert ledger.records[-1]["event"] == "plan.generated"
        assert (
            ledger.records[-1]["payload"]["campaign_digest"]
            == result["campaign_digest"]
        )


def test_apply_checks_global_digest_before_starting_either_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = _write_manifest(tmp_path)
    monkeypatch.setenv("VONK_QUALIFICATION_LOCK_DIR", str(tmp_path / "locks"))
    started: list[str] = []

    class _Runner:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            started.append("constructed")

        def apply(
            self, plan: dict[str, object], expected_digest: str
        ) -> dict[str, object]:
            assert plan["plan_digest"] == expected_digest
            return {"plan_digest": expected_digest, "succeeded": 1}

    monkeypatch.setattr(campaign_cli, "QualificationRunner", _Runner)
    with pytest.raises(QualificationError, match="does not match"):
        campaign_cli.run(
            [
                "--manifest",
                str(manifest_path),
                "--campaign-digest",
                "0" * 64,
                "--apply",
            ],
            client_factory=_Client,
        )
    assert started == []


def test_apply_passes_each_exact_plan_digest_and_runs_lanes_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = _write_manifest(tmp_path)
    monkeypatch.setenv("VONK_QUALIFICATION_LOCK_DIR", str(tmp_path / "locks"))
    preview = campaign_cli.run(
        ["--manifest", str(manifest_path)], client_factory=_Client
    )
    barrier = threading.Barrier(2, timeout=5)
    observed: list[str] = []

    class _Runner:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        def apply(
            self, plan: dict[str, object], expected_digest: str
        ) -> dict[str, object]:
            assert plan["plan_digest"] == expected_digest
            observed.append(expected_digest)
            barrier.wait()
            return {"plan_digest": expected_digest, "succeeded": 1}

    monkeypatch.setattr(campaign_cli, "QualificationRunner", _Runner)
    applied = campaign_cli.run(
        [
            "--manifest",
            str(manifest_path),
            "--campaign-digest",
            str(preview["campaign_digest"]),
            "--apply",
        ],
        client_factory=_Client,
    )

    assert applied["mode"] == "apply"
    assert applied["campaign_digest"] == preview["campaign_digest"]
    assert len(observed) == 2
    assert set(observed) == {
        lane["plan_digest"]
        for lane in preview["lanes"]  # type: ignore[index]
    }
