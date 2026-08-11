from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.model_resolution import ModelFile, SnapshotEnvelope
from vonk_control.models import Base, LocalRecipe, RecipeImport
from vonk_control.registry_resolution import ManifestEnvelope
from vonk_control.source_bundles import SourceBundleStore
from vonk_control.workload_run_workflow import WorkloadRunWorkflow


class Jobs:
    def get(self, job_id):
        raise KeyError(job_id)

    def list(self, *, limit=100):
        return []

    def list_page(self, **kwargs):
        return [], None, 0


def setup(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'db.sqlite'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    clock = lambda: datetime(2026, 8, 7, tzinfo=UTC)
    workflow = WorkloadRunWorkflow(
        sessions, clock=clock, bundles=SourceBundleStore(tmp_path / "bundles")
    )
    codec = TokenCodec(b"s" * 32)
    audits = MemoryAuditStore()
    app = create_app(
        jobs=Jobs(),
        tokens=codec,
        audits=audits,
        fleet=lambda: {"nodes": []},
        now=lambda: 10,
        workload_run=workflow,
    )
    token = codec.issue(Actor("admin", "administrator"), ttl_seconds=100, now=0)
    return TestClient(app), {"Authorization": f"Bearer {token}"}, sessions


def test_preview_does_not_persist_recipe(tmp_path: Path) -> None:
    client, headers, sessions = setup(tmp_path)
    source = (Path(__file__).parent / "fixtures/workload_run/minimal-vllm.yaml").read_text()
    response = client.post(
        "/api/v1/catalog/imports/workload_run/preview",
        headers=headers,
        json={"source_yaml": source},
    )

    assert response.status_code == 200
    assert response.json()["runnable"] is False
    assert (
        response.json()["source_bundle"]["sha256"]
        == response.json()["draft_document"]["build"]["context"]["sha256"]
    )
    assert response.json()["source_bundle"]["files"][0]["path"] == "Dockerfile"
    assert any(
        item["disposition"] == "overlay_required" for item in response.json()["report"]
    )
    with sessions() as session:
        assert session.scalar(select(LocalRecipe)) is None


def test_apply_is_idempotent_and_persists_only_redacted_source(tmp_path: Path) -> None:
    client, headers, sessions = setup(tmp_path)
    source = (Path(__file__).parent / "fixtures/workload_run/minimal-vllm.yaml").read_text()
    preview = client.post(
        "/api/v1/catalog/imports/workload_run/preview",
        headers=headers,
        json={"source_yaml": source},
    ).json()
    body = {
        "source_yaml": source,
        "source_sha256": preview["source_sha256"],
        "report_digest": preview["report_digest"],
    }
    first = client.post("/api/v1/catalog/imports/workload_run", headers=headers, json=body)
    second = client.post("/api/v1/catalog/imports/workload_run", headers=headers, json=body)

    assert first.status_code == second.status_code == 201
    assert first.json()["recipe_id"] == second.json()["recipe_id"]
    with sessions() as session:
        assert len(session.scalars(select(LocalRecipe)).all()) == 1
        stored = session.scalar(select(RecipeImport))
        assert stored is not None and "command" in stored.redacted_source


def test_apply_rejects_stale_preview_and_operator(tmp_path: Path) -> None:
    client, headers, _sessions = setup(tmp_path)
    source = (Path(__file__).parent / "fixtures/workload_run/minimal-vllm.yaml").read_text()
    stale = client.post(
        "/api/v1/catalog/imports/workload_run",
        headers=headers,
        json={
            "source_yaml": source,
            "source_sha256": "a" * 64,
            "report_digest": "b" * 64,
        },
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "workload_run.stale_preview"


def test_workflow_resolves_with_verified_metadata_and_overlays(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'resolve.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    workflow = WorkloadRunWorkflow(
        sessions,
        clock=lambda: datetime(2026, 8, 7, tzinfo=UTC),
        bundles=SourceBundleStore(tmp_path / "bundles"),
        registry=_Registry(),
        models=_Models(),
    )
    raw = (Path(__file__).parent / "fixtures/workload_run/minimal-vllm.yaml").read_bytes()
    preview = workflow.preview(raw)
    applied = workflow.apply(
        raw,
        source_sha256=preview.source_sha256,
        report_digest=preview.report_digest,
        actor="admin",
    )

    resolved = workflow.resolve(
        applied.recipe_id,
        expected_revision=1,
        overlays={
            "build_resources": {
                "download_bytes": 50,
                "temporary_bytes": 50,
                "memory_bytes": 100,
                "timeout_seconds": 600,
            },
            "artifact_sizes": {
                "weights": {"download_bytes": 100, "installed_bytes": 150}
            },
            "profile_resources": {
                "solo": {
                    "entrypoint": {
                        "disk": {
                            "image_bytes": 50,
                            "artifact_bytes": 150,
                            "staging_bytes": 50,
                            "cache_bytes": 10,
                            "rollback_bytes": 50,
                            "safety_margin_bytes": 20,
                        },
                        "memory": {
                            "kind": "unified",
                            "startup_peak_bytes": 225,
                            "steady_state_bytes": 200,
                            "runtime_growth_bytes": 25,
                            "system_reserve_bytes": 25,
                        },
                    }
                }
            },
            "security_acknowledged": True,
        },
        actor="admin",
    )
    assert resolved.revision_number == 2
    assert len(resolved.content_sha256) == 64


class _Registry:
    def resolve(self, host):
        return ("93.184.216.34",)

    def manifest(self, host, repository, reference, *, maximum_bytes):
        document = {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "layers": [{"digest": "sha256:" + "b" * 64, "size": 50}],
        }
        body = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        return ManifestEnvelope(
            body,
            "sha256:" + hashlib.sha256(body).hexdigest(),
            document["mediaType"],
            "linux/arm64",
            (),
        )


class _Models:
    def snapshot(self, repository, revision, *, maximum_files):
        return SnapshotEnvelope(
            repository, revision, (ModelFile("model.safetensors", 100),)
        )
