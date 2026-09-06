from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import UTC, datetime
from importlib.resources import files
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_control import availability_production
from vonk_control.availability_production import (
    RecipeImageAvailabilityScheduler,
    build_recipe_image_availability,
)
from vonk_control.models import Base, CatalogDocumentRevision, RuntimeImageReceipt
from vonk_control.runtime_image_preparation import PulledImageEvidence
from vonk_forge_contracts import RecipeDefinition, content_sha256


class _Claim:
    pass


class _Service:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.claimed = 0

    def claim_pending(self, *, limit: int, owner_id: str):
        del owner_id
        if self.claimed:
            return ()
        self.claimed += 1
        return (_Claim(),)[:limit]

    def run_claim(self, claim: _Claim) -> None:
        del claim
        self.started.set()
        self.release.wait(5)


def test_scheduler_submits_durable_claim_without_waiting_for_image_io() -> None:
    service = _Service()
    scheduler = RecipeImageAvailabilityScheduler(service, max_workers=1)
    started = time.monotonic()
    assert scheduler.tick() == 1
    elapsed = time.monotonic() - started
    assert elapsed < 0.5
    assert service.started.wait(1)
    service.release.set()
    scheduler.close()
    assert scheduler.executor._shutdown is True


def test_scheduler_close_is_idempotent_and_stops_new_claims() -> None:
    service = _Service()
    scheduler = RecipeImageAvailabilityScheduler(service, max_workers=1)
    scheduler.close()
    scheduler.close()
    assert scheduler.tick() == 0


def test_production_factory_separates_api_service_and_worker_scheduler(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'availability.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)

    class Settings:
        agent_artifact_root = tmp_path / "api-artifacts"

    kwargs = dict(
        sessions=sessions,
        settings=Settings(),
        managed_catalog_sync=None,
        recipe_builds=object(),
        recipe_operations=object(),
        clock=lambda: datetime.now(UTC),
    )
    api = build_recipe_image_availability(**kwargs)
    assert api.scheduler is None
    assert api.storage.root == tmp_path / "api-artifacts" / "oci-archives"
    api.close()

    worker = build_recipe_image_availability(
        **kwargs,
        artifact_root=tmp_path / "worker-artifacts",
        with_scheduler=True,
    )
    assert worker.scheduler is not None
    scheduler = worker.scheduler
    worker.close()
    assert scheduler.executor._shutdown is True


def test_production_factory_claim_compiles_and_persists_sql_receipt(
    tmp_path, monkeypatch
) -> None:
    recipe = RecipeDefinition.model_validate(
        json.loads(files("vonk_forge_contracts").joinpath("examples", "recipe-image.json").read_text())
    )
    model = json.loads(
        files("vonk_forge_contracts").joinpath("examples", "model-definition.json").read_text()
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'authority.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(
            CatalogDocumentRevision(
                id="model-revision",
                document_id="model-document",
                kind="model",
                publisher="vonk-forge",
                slug="synthetic-tiny-fp16",
                revision_number=1,
                schema_version=2,
                state="active",
                document=model,
                content_digest="7b5431cb5c3f062afa8cc3e7013610cd1fa52fad35c53b5dd0f57482649c4202",
                artifact_key="b" * 64,
                projected={},
                created_by="test",
                created_at=now,
            )
        )
        session.add(
            CatalogDocumentRevision(
                id="recipe-revision",
                document_id="recipe-document",
                kind="recipe",
                publisher=recipe.identity.publisher,
                slug=recipe.identity.slug,
                revision_number=1,
                schema_version=2,
                state="active",
                document=recipe.model_dump(mode="json"),
                content_digest=content_sha256(recipe),
                artifact_key="c" * 64,
                execution_key="a" * 64,
                projected={},
                created_by="test",
                created_at=now,
            )
        )

    class Transport:
        def pull_and_export(self, reference, destination, **_kwargs):
            archive = b"production availability archive"
            destination.write_bytes(archive)
            return PulledImageEvidence(
                manifest_digest="sha256:" + "e" * 64,
                requested_manifest_digest="sha256:" + "d" * 64,
                config_id="sha256:" + "c" * 64,
                local_reference=reference,
                architecture="linux/arm64",
                runtime_interface="v1",
                archive_sha256=hashlib.sha256(archive).hexdigest(),
                archive_bytes=len(archive),
            )

    monkeypatch.setattr(availability_production, "SkopeoOCIImageTransport", Transport)

    class Settings:
        agent_artifact_root = tmp_path / "artifacts"

    production = build_recipe_image_availability(
        sessions,
        settings=Settings(),
        managed_catalog_sync=None,
        recipe_builds=object(),
        recipe_operations=object(),
        clock=lambda: now,
    )
    queued = production.service.start(
        "recipe-revision", actor="operator", request_id="r" * 36
    )
    claim = production.service.claim_pending(owner_id="worker-a")[0]
    production.service.run_claim(claim)
    assert production.service.get(queued.id).state == "succeeded"
    with sessions() as session:
        receipt = session.scalar(select(RuntimeImageReceipt))
        assert receipt is not None
        assert receipt.recipe_revision_id == "recipe-revision"
        assert receipt.state == "verified"
    production.close()


def test_source_build_without_builder_queues_provisional_parent(tmp_path, monkeypatch) -> None:
    recipe = RecipeDefinition.model_validate(
        json.loads(files("vonk_forge_contracts").joinpath("examples", "recipe-source-build.json").read_text())
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'saturated.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(CatalogDocumentRevision(
            id="saturated-revision", document_id="saturated-document", kind="recipe",
            publisher=recipe.identity.publisher, slug=recipe.identity.slug,
            revision_number=1, schema_version=2, state="active",
            document=recipe.model_dump(mode="json"), content_digest=content_sha256(recipe),
            artifact_key="c" * 64, execution_key="a" * 64, projected={},
            created_by="test", created_at=now,
        ))

    class Builds:
        def resolve(self, _revision_id: str):
            return SimpleNamespace(
                cached=False, input_intent_sha256="a" * 64,
                build_input_sha256=None, build_id=None,
            )

        def plan(self, *_args, **_kwargs):
            raise AssertionError("builder planning must remain dispatch-time")

    monkeypatch.setattr(
        availability_production,
        "resolve_recipe_entities",
        lambda _session, _document: {},
    )
    monkeypatch.setattr(
        availability_production,
        "_compile_consistent_runtime",
        lambda *_args, **_kwargs: {
            "input_intent_sha256": "a" * 64,
            "interface": "vonk.runtime.v1",
            "architecture": "linux/arm64",
            "image": "sha256:" + "d" * 64,
        },
    )

    class Settings:
        agent_artifact_root = tmp_path / "artifacts"

    production = build_recipe_image_availability(
        sessions, settings=Settings(), managed_catalog_sync=None,
        recipe_builds=Builds(), recipe_operations=object(), clock=lambda: now,
    )
    queued = production.service.start(
        "saturated-revision", actor="operator", request_id="s" * 36,
    )
    assert queued.state == "queued"
    assert queued.build_input_sha256 is None
    with sessions() as session:
        row = session.get(CatalogDocumentRevision, "saturated-revision")
        assert row is not None
    production.close()
