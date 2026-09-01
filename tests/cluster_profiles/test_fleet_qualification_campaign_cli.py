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


def test_packaged_authority_binds_only_current_e6a8_catalog_closure() -> None:
    authority = PACKAGED_AUTHORITY_LOADER("nl-single-spark-e6a8e750")

    assert authority.authority_sha256 == (
        "94b308b39c10d5e853fdfd8a6c4c61394dc6e44c229788716839368790b3cbd2"
    )
    assert authority.repository == "CarstVaartjes/vonk-forge-recipes"
    assert authority.commit == "e6a8e75029ad85216b22e2d5e41d26a5689fcf6b"
    assert (
        authority.catalog_index_sha256
        == "24a57e7d89e7a07708fe960c400a85546ae9de2d45da6b879061109bb967d352"
    )
    assert authority.catalog_recipe_count == 84
    assert authority.jurisdiction == "NL"
    assert [
        len(authority.actionable_recipe_keys),
        len(authority.capacity_blocked_recipe_keys),
        len(authority.legal_blocked_recipe_keys),
        len(authority.dual_spark_recipe_keys),
        len(authority.unsupported_topology_recipe_keys),
    ] == [58, 5, 9, 8, 4]


def test_packaged_authority_rejects_schema_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority_root = tmp_path / "qualification_authorities"
    authority_root.mkdir()
    document = json.loads(
        (
            REPOSITORY_ROOT
            / "src/cluster_profiles/qualification_authorities/nl-single-spark-e6a8e750.json"
        ).read_text(encoding="utf-8")
    )
    document["schema_version"] = 1
    (authority_root / "nl-single-spark-e6a8e750.json").write_text(
        json.dumps(document), encoding="utf-8"
    )
    monkeypatch.setattr(campaign_cli.resources, "files", lambda _package: tmp_path)

    with pytest.raises(QualificationError, match="authority identity is invalid"):
        PACKAGED_AUTHORITY_LOADER("nl-single-spark-e6a8e750")


def test_checked_in_e6a8_campaign_is_the_exact_current_partition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(campaign_cli, "_load_authority", PACKAGED_AUTHORITY_LOADER)
    manifest = campaign_cli.load_manifest(
        REPOSITORY_ROOT / "config/qualification/nl-single-spark-e6a8e750.json"
    )

    assert manifest.manifest_sha256 == (
        "951522a6ca644931c6c35d279e88fd30d8aa822b6d5ae313bc1eec46eb3d1ca5"
    )
    assert manifest.cleanup == "stop"
    assert manifest.jurisdiction == "NL"
    assert [
        (lane.name, lane.node_id, len(lane.recipes)) for lane in manifest.lanes
    ] == [
        ("spark-3542", "spk_2818d189042b4c77aefa7796f4befd23", 29),
        ("spark-2297", "spk_9a86fdbab116442ab6707bf4181a3c1c", 29),
    ]
    assigned = [recipe for lane in manifest.lanes for recipe in lane.recipes]
    assert len(assigned) == len(set(assigned)) == 58
    assert set(assigned) == set(manifest.authority.actionable_recipe_keys)
    state_root = (
        REPOSITORY_ROOT / ".state/qualification/nl-single-spark-e6a8e750"
    ).resolve()
    for lane in manifest.lanes:
        assert lane.ledger.is_relative_to(state_root)
        assert lane.plan_output.is_relative_to(state_root)


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
