from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.models import (
    Base,
    CatalogDocument,
    CatalogDocumentHead,
    CatalogDocumentRevision,
)
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
    source = (
        Path(__file__).parent / "fixtures/workload_run/minimal-vllm.yaml"
    ).read_text()
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
        assert session.scalar(select(CatalogDocument)) is None
        assert session.scalar(select(CatalogDocumentRevision)) is None
        assert session.scalar(select(CatalogDocumentHead)) is None


def test_apply_rejects_noncanonical_draft_without_legacy_persistence(
    tmp_path: Path,
) -> None:
    client, headers, sessions = setup(tmp_path)
    source = (
        Path(__file__).parent / "fixtures/workload_run/minimal-vllm.yaml"
    ).read_text()
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
    first = client.post(
        "/api/v1/catalog/imports/workload_run", headers=headers, json=body
    )
    second = client.post(
        "/api/v1/catalog/imports/workload_run", headers=headers, json=body
    )

    assert first.status_code == second.status_code == 409
    assert first.json()["code"] == second.json()["code"] == "catalog.document_invalid"
    with sessions() as session:
        documents = session.scalars(select(CatalogDocument)).all()
        revisions = session.scalars(select(CatalogDocumentRevision)).all()
        heads = session.scalars(select(CatalogDocumentHead)).all()
        assert documents == revisions == heads == []
    assert "local_recipes" not in Base.metadata.tables
    assert "recipe_imports" not in Base.metadata.tables


def test_apply_rejects_stale_preview_and_operator(tmp_path: Path) -> None:
    client, headers, _sessions = setup(tmp_path)
    source = (
        Path(__file__).parent / "fixtures/workload_run/minimal-vllm.yaml"
    ).read_text()
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
