"""Model cache: removal review manifest."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState
from vonk_control.auth import Actor
from vonk_control.model_cache.api import (
    install_model_operator_routes,
)
from vonk_control.model_cache.artifacts import ArtifactSetManifest
from vonk_control.model_cache.errors import (
    ModelCacheConflict,
    ModelCacheResolutionError,
)
from vonk_control.model_cache.service import ModelCacheService
from vonk_control.models.cache import (
    ArtifactLifecycleGate,
    ModelCacheOperation,
    ModelCacheSet,
)
from vonk_control.models.catalog import (
    CatalogDocument,
    CatalogDocumentHead,
    CatalogDocumentRevision,
)
from vonk_forge_contracts import ModelDefinition, document_sha256

from .display_fixtures import reworded
from .model_cache_support import (
    NOW,
    _admit_fresh_download,
    _artifact,
    _canonical_model,
    _download,
    _first_manifest_artifact,
    _manifest_document,
    _shared_connection_sessions,
    _watch_services_on,
)
from .non_blocking import assert_ended_without_blocking


def test_a_restarted_service_that_ticks_on_the_shared_connection_fails_the_net(
    tmp_path: Path,
):
    """The shape of the flake: a second service on the fixture's sessions ticks."""

    sessions = _shared_connection_sessions()
    with (
        pytest.raises(AssertionError, match="use threaded_cache"),
        _watch_services_on(sessions),
    ):
        ModelCacheService(
            sessions, tmp_path / "first", free_fraction=0, fixture_sources=True
        )
        restarted = ModelCacheService(
            sessions, tmp_path / "first", free_fraction=0, fixture_sources=True
        )
        restarted.tick()


def test_a_service_that_starts_no_background_work_passes_the_net(tmp_path: Path):
    sessions = _shared_connection_sessions()
    with _watch_services_on(sessions):
        ModelCacheService(
            sessions, tmp_path / "quiet", free_fraction=0, fixture_sources=True
        )


def test_model_removal_review_binds_exact_assets_without_mutation(cache, tmp_path):
    service, sessions = cache
    data = b"reviewed model cache bytes"
    artifact = _artifact(tmp_path, data)
    downloaded = _download(
        service,
        [artifact],
        model_content_sha256="a" * 64,
        request_key="00000000-0000-4000-8000-000000001062",
    )
    assert downloaded.state == "succeeded"
    assert downloaded.artifact_set_sha256 is not None
    object_digest = str(artifact["sha256"])

    with sessions() as session:
        before = (
            session.scalar(select(func.count()).select_from(ModelCacheOperation)),
            session.scalar(select(func.count()).select_from(ModelCacheSet)),
            session.scalar(select(func.count()).select_from(ArtifactLifecycleGate)),
            tuple(
                session.scalars(
                    select(ArtifactLifecycleGate.removal_owner_id).order_by(
                        ArtifactLifecycleGate.artifact_kind,
                        ArtifactLifecycleGate.artifact_sha256,
                    )
                )
            ),
        )

    review = service.review_model_removal("a" * 64)

    with sessions() as session:
        after = (
            session.scalar(select(func.count()).select_from(ModelCacheOperation)),
            session.scalar(select(func.count()).select_from(ModelCacheSet)),
            session.scalar(select(func.count()).select_from(ArtifactLifecycleGate)),
            tuple(
                session.scalars(
                    select(ArtifactLifecycleGate.removal_owner_id).order_by(
                        ArtifactLifecycleGate.artifact_kind,
                        ArtifactLifecycleGate.artifact_sha256,
                    )
                )
            ),
        )

    assert after == before
    assert service._object_path(object_digest).read_bytes() == data
    assert review.resource_kind == "model"
    assert review.target_identity == "a" * 64
    assert review.blockers == []
    assert {
        (item.kind, item.sha256, item.expected_bytes, item.disposition)
        for item in review.assets
    } == {
        (
            "model-set",
            downloaded.artifact_set_sha256,
            len(data),
            "remove",
        ),
        ("model-object", object_digest, len(data), "remove"),
    }
    assert all(item.availability == "verified" for item in review.assets)


def test_model_removal_review_counts_resumable_partial_set_bytes(cache, tmp_path):
    service, _sessions = cache
    data = bytes(range(256)) * 12_000
    artifact = _artifact(tmp_path, data, model_content_sha256="b" * 64)
    operation = _download(
        service,
        [artifact],
        model_content_sha256="b" * 64,
        request_key="00000000-0000-4000-8000-000000001067",
        interrupt_after_bytes=1_100_000,
    )
    assert operation.state == LifecycleState.BACKOFF
    assert operation.artifact_set_sha256 is not None
    object_digest = str(artifact["sha256"])
    partial = service._partial_path(operation.artifact_set_sha256, object_digest)
    observed_bytes = partial.stat().st_size
    assert 0 < observed_bytes < len(data)

    review = service.review_model_removal("b" * 64)
    set_asset = next(item for item in review.assets if item.kind == "model-set")
    object_asset = next(
        item
        for item in review.assets
        if item.kind == "model-object" and item.sha256 == object_digest
    )

    assert set_asset.availability == "partial"
    assert set_asset.expected_bytes == len(data)
    assert set_asset.available_bytes == observed_bytes
    assert object_asset.availability == "missing"
    assert object_asset.available_bytes == 0


@pytest.mark.parametrize("unavailable", ["unreadable", "nonregular"])
def test_model_removal_review_preserves_unknown_storage_observation(
    cache, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unavailable: str
):
    service, _sessions = cache
    data = b"observed model cache bytes"
    artifact = _artifact(tmp_path, data)
    downloaded = _download(
        service,
        [artifact],
        model_content_sha256="a" * 64,
        request_key="00000000-0000-4000-8000-000000001068",
    )
    assert downloaded.state == "succeeded"
    object_digest = str(artifact["sha256"])
    object_path = service._object_path(object_digest)
    if unavailable == "nonregular":
        object_path.unlink()
        object_path.mkdir()
    else:
        original_lstat = Path.lstat
        original_open = os.open

        def unreadable_open(file, *args, **kwargs):
            if file == object_path:
                raise PermissionError("fixture denies object open")
            return original_open(file, *args, **kwargs)

        monkeypatch.setattr(os, "open", unreadable_open)

        def unreadable_lstat(path: Path):
            if path == object_path:
                raise PermissionError("fixture denies object metadata")
            return original_lstat(path)

        monkeypatch.setattr(Path, "lstat", unreadable_lstat)

    review = service.review_model_removal("a" * 64)
    set_asset = next(item for item in review.assets if item.kind == "model-set")
    object_asset = next(
        item
        for item in review.assets
        if item.kind == "model-object" and item.sha256 == object_digest
    )

    assert set_asset.availability == "unknown"
    assert set_asset.available_bytes is None
    assert object_asset.availability == "unknown"
    assert object_asset.available_bytes is None


def test_model_removal_applies_to_current_storage_after_an_old_review(
    cache, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, sessions = cache
    now = NOW
    monkeypatch.setattr(service, "_clock", lambda: now)
    data = b"review must not accept missing bytes"
    artifact = _artifact(tmp_path, data)
    downloaded = _download(
        service,
        [artifact],
        model_content_sha256="a" * 64,
        request_key="00000000-0000-4000-8000-000000001063",
    )
    assert downloaded.state == "succeeded"
    assert downloaded.artifact_set_sha256 is not None
    digest = str(artifact["sha256"])
    reviewed = service.review_model_removal("a" * 64)
    assert all(item.availability == "verified" for item in reviewed.assets)

    service._object_path(digest).unlink()
    # The earlier review is advisory: removal applies to the current state.
    accepted = service.remove_model_selector(
        "a" * 64,
        actor="operator",
        request_key="00000000-0000-4000-8000-000000001064",
    )
    for _ in range(10):
        observed = service.get_operation(accepted.id)
        if observed.state == LifecycleState.SUCCEEDED:
            break
        with sessions() as session:
            stored = session.get(ModelCacheOperation, accepted.id)
            assert stored is not None
            if stored.next_action_at is not None:
                now = stored.next_action_at.replace(tzinfo=UTC)
        service.advance_removals(limit=10)
    assert service.get_operation(accepted.id).state == LifecycleState.SUCCEEDED
    with sessions() as session:
        assert session.get(ModelCacheSet, downloaded.artifact_set_sha256) is None
        assert all(
            row.removal_owner_id is None
            for row in session.scalars(select(ArtifactLifecycleGate))
        )

    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        accepted,
        end=lambda receipt: service.get_operation(receipt.id),
        fresh=lambda _world: _admit_fresh_download(service, "a" * 64, artifact),
    )


def test_model_removal_review_reports_exact_live_deletion_owner(
    cache, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    service, sessions = cache
    now = NOW
    monkeypatch.setattr(service, "_clock", lambda: now)
    data = b"active deletion owner bytes"
    artifact = _artifact(tmp_path, data)
    _download(
        service,
        [artifact],
        model_content_sha256="a" * 64,
        request_key="00000000-0000-4000-8000-000000001065",
    )
    accepted = service.remove_model_selector(
        "a" * 64,
        actor="operator",
        request_key="00000000-0000-4000-8000-000000001066",
    )

    observe_scope = service._observe_model_removal_scope
    scope_attempts = 0

    def observe_after_miss(operation_id: str) -> bool:
        nonlocal scope_attempts
        scope_attempts += 1
        # Exercise the bounded observation miss that can occur under CI load.
        return scope_attempts > 1 and observe_scope(operation_id)

    monkeypatch.setattr(service, "_observe_model_removal_scope", observe_after_miss)

    def advance_when_due() -> None:
        nonlocal now
        with sessions() as session:
            stored = session.get(ModelCacheOperation, accepted.id)
            assert stored is not None
            if stored.next_action_at is not None:
                now = max(now, stored.next_action_at.replace(tzinfo=UTC))
        service.advance_removals(limit=1)

    # Scope observation may defer; follow its durable clock, rather than assuming
    # that one worker pass commits the deletion gates.
    for _attempt in range(10):
        advance_when_due()
        blocked = service.review_model_removal("a" * 64)
        if any(item.owner_id == accepted.id for item in blocked.active_work):
            break

    assert any(item.owner_id == accepted.id for item in blocked.active_work)
    assert scope_attempts > 1

    def finish_removal(receipt):
        for _attempt in range(10):
            observed = service.get_operation(receipt.id)
            if observed.state == LifecycleState.SUCCEEDED:
                return observed
            advance_when_due()
        return service.get_operation(receipt.id)

    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        accepted,
        end=finish_removal,
        fresh=lambda _world: _admit_fresh_download(service, "a" * 64, artifact),
    )


def test_cache_manifest_requires_exact_canonical_field_sets(tmp_path: Path) -> None:
    document = _manifest_document(tmp_path)

    with pytest.raises(ModelCacheResolutionError):
        ArtifactSetManifest.from_document({**document, "unexpected": True})
    missing = dict(document)
    missing.pop("model_content_digests")
    with pytest.raises(ModelCacheResolutionError):
        ArtifactSetManifest.from_document(missing)
    restored = ArtifactSetManifest.from_document(document)
    assert restored.document() == document


@pytest.mark.parametrize("schema_version", [True, 2.0])
def test_cache_manifest_requires_native_schema_version_type(
    tmp_path: Path, schema_version: object
) -> None:
    document = _manifest_document(tmp_path)
    document["schema_version"] = schema_version

    with pytest.raises(ModelCacheResolutionError):
        ArtifactSetManifest.from_document(document)


@pytest.mark.parametrize(
    ("field", "value"),
    [("download_bytes", 1.0), ("download_bytes", True), ("roles", ("model",))],
)
def test_cache_manifest_rejects_coercible_artifact_types(
    tmp_path: Path, field: str, value: object
) -> None:
    document = _manifest_document(tmp_path)
    artifact = _first_manifest_artifact(document)
    artifact[field] = value
    document["artifacts"] = [artifact]

    with pytest.raises(ModelCacheResolutionError):
        ArtifactSetManifest.from_document(document)


@pytest.mark.parametrize("mutation", ["extra", "missing"])
def test_cache_manifest_artifact_dto_requires_exact_fields(
    tmp_path: Path, mutation: str
) -> None:
    document = _manifest_document(tmp_path)
    artifact = _first_manifest_artifact(document)
    if mutation == "extra":
        artifact["unexpected"] = True
    else:
        artifact.pop("roles")
    document["artifacts"] = [artifact]

    with pytest.raises(ModelCacheResolutionError):
        ArtifactSetManifest.from_document(document)


def test_operator_download_replays_original_before_catalog_or_storage_readmission(
    cache, tmp_path: Path, capsys
) -> None:
    _, sessions = cache
    data = b"abc"
    model = _canonical_model(
        publisher="vonk-forge",
        slug="request-recovery",
        file_id="weights",
        file_digest=hashlib.sha256(data).hexdigest(),
    )
    digest = document_sha256(model.model_dump(mode="json"))
    selector = f"{model.identity.publisher}/{model.identity.slug}"
    with sessions.begin() as session:
        document = CatalogDocument(
            kind="model",
            publisher=model.identity.publisher,
            slug=model.identity.slug,
            title="Request recovery model",
            created_by="operator",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(document)
        session.flush()
        revision = CatalogDocumentRevision(
            document_id=document.id,
            kind="model",
            publisher=model.identity.publisher,
            slug=model.identity.slug,
            revision_number=1,
            schema_version=2,
            state="active",
            document=model.model_dump(mode="json"),
            content_digest=digest,
            projected={},
            created_by="operator",
            created_at=NOW,
        )
        session.add(revision)
        session.flush()
        document_id = document.id
        session.add(
            CatalogDocumentHead(
                kind=revision.kind,
                publisher=revision.publisher,
                slug=revision.slug,
                active_revision_id=revision.id,
                generation=1,
            )
        )
    with httpx2.Client(
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(200, content=data)
        )
    ) as client:
        service = ModelCacheService(
            sessions, tmp_path / "operator-cache", free_fraction=0, http_client=client
        )
        key = "00000000-0000-4000-8000-000000000097"
        accepted = service.download_model_selector(
            selector, actor="operator", request_key=key, force=True
        )
        service.run_pending()
        completed = service.get_operation(accepted.id)
        assert completed.state == "succeeded"
        # The actual transfer changes the current remaining-byte preview. The
        # original public request must still recover its accepted operation.
        recovered = service.download_model_selector(
            selector, actor="operator", request_key=key, force=True
        )
        assert recovered.id == completed.id
        assert recovered.progress == completed.progress
    with sessions.begin() as session:
        newer = reworded(model, "New revision")
        session.add(
            CatalogDocumentRevision(
                document_id=document_id,
                kind="model",
                publisher=model.identity.publisher,
                slug=model.identity.slug,
                revision_number=2,
                schema_version=2,
                state="active",
                document=newer.model_dump(mode="json"),
                content_digest=document_sha256(newer.model_dump(mode="json")),
                projected={},
                created_by="operator",
                created_at=NOW,
            )
        )
    restarted = ModelCacheService(sessions, service.root, free_fraction=1.0)
    assert (
        restarted.download_model_selector(
            selector, actor="operator", request_key=key, force=True
        ).id
        == accepted.id
    )
    actor = Actor("operator", "administrator")
    app = FastAPI()
    install_model_operator_routes(
        app, actor_dependency=Depends(lambda: actor), service=restarted
    )
    with TestClient(app) as api:
        observed = api.get(f"/api/model/requests/{key}")
        assert observed.status_code == 200, observed.text
        assert observed.json()["operation_id"] == accepted.id

        from email.message import Message
        from io import BytesIO
        from urllib.error import URLError
        from urllib.parse import quote

        from cluster_profiles.cli import main
        from cluster_profiles.control_client.transfers import ControlClient

        token = tmp_path / "recovery-token"
        token.write_text("fixture-token")
        token.chmod(0o600)
        paths = []

        class OpenedResponse(BytesIO):
            def __init__(self, response):
                super().__init__(response.content)
                self.status = response.status_code
                self.headers = Message()
                for name, value in response.headers.items():
                    self.headers[name] = value

            def __exit__(self, *_args: object) -> None:
                self.close()

        def opener(request, timeout):
            paths.append((request.get_method(), request.selector))
            response = api.request(
                request.get_method(),
                request.selector,
                content=request.data,
                headers=dict(request.header_items()),
            )
            if request.get_method() == "POST" and response.status_code == 202:
                raise URLError(ConnectionResetError())
            return OpenedResponse(response)

        control = ControlClient("https://forge.example.test", token, opener=opener)
        assert (
            main(
                ("model", "download", selector, "--request-key", key, "--json"),
                control_client=control,
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out) == observed.json()
        assert paths == [
            ("POST", f"/api/model/{quote(selector, safe='')}/download"),
            ("GET", f"/api/model/requests/{key}"),
        ]
        for unused in (False, True):
            assert (
                api.post(
                    f"/api/model/{digest}/download",
                    json={"request_key": key, "with_model": unused},
                ).status_code
                == 422
            )
        actor = Actor("other-operator", "administrator")
        hidden = api.get(f"/api/model/requests/{key}")
        missing = api.get("/api/model/requests/00000000-0000-4000-8000-000000000098")
        assert hidden.status_code == missing.status_code == 404
        assert hidden.json() == missing.json()
        assert api.get(f"/api/model/operations/{accepted.id}").status_code == 200
        assert (
            api.post(
                f"/api/model/{digest}/download", json={"request_key": key}
            ).status_code
            == 409
        )
    for actor, requested in [
        ("other-operator", selector),
        ("operator", "absent-model"),
    ]:
        with pytest.raises(ModelCacheConflict):
            restarted.download_model_selector(
                requested, actor=actor, request_key=key, force=True
            )
    with sessions() as session:
        assert (
            session.scalar(select(func.count()).select_from(ModelCacheOperation)) == 1
        )
    # A second operator request while a download is live keeps its own receipt,
    # readable by its own key, instead of attaching to another key's operation.
    actor = Actor("operator", "administrator")
    app = FastAPI()
    install_model_operator_routes(
        app, actor_dependency=Depends(lambda: actor), service=restarted
    )
    with TestClient(app) as api:
        receipts = []
        for suffix in ("a1", "a2"):
            other = f"00000000-0000-4000-8000-0000000000{suffix}"
            posted = api.post(
                f"/api/model/{digest}/download", json={"request_key": other}
            )
            assert posted.status_code == 202, posted.text
            observed = api.get(f"/api/model/requests/{other}")
            assert observed.status_code == 200, observed.text
            receipts.append(observed.json()["operation_id"])
        assert receipts[0] != receipts[1]


def test_canonical_catalog_revision_resolves_immutable_model_files(cache) -> None:
    service, sessions = cache
    document = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text(encoding="utf-8")
    )
    document["identity"]["publisher"] = "vonk-forge"
    document["identity"]["slug"] = "canonical-model"
    document["identity"]["model"]["publisher"] = "vonk-forge"
    document["identity"]["model"]["slug"] = "canonical-model"
    document["source"]["repository"] = (
        "https://huggingface.co/vonk-forge/canonical-model"
    )
    document["source"]["revision"] = "0" * 40
    document["files"] = [
        {
            "id": "weights",
            "path": "weights.bin",
            "sha256": "1" * 64,
            "size_bytes": 3,
            "roles": ["weights"],
        }
    ]
    document = ModelDefinition.model_validate(document).model_dump(mode="json")
    digest = document_sha256(document)
    with sessions.begin() as session:
        root = CatalogDocument(
            id="00000000-0000-0000-0000-000000000031",
            kind="model",
            publisher="vonk-forge",
            slug="canonical-model",
            title="Canonical model",
            created_by="test",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(root)
        session.flush()
        session.add(
            CatalogDocumentRevision(
                id="00000000-0000-0000-0000-000000000032",
                document_id=root.id,
                kind="model",
                publisher=root.publisher,
                slug=root.slug,
                revision_number=1,
                schema_version=2,
                state="active",
                document=document,
                content_digest=digest,
                projected={},
                created_by="test",
                created_at=NOW,
            )
        )

    manifest = service.resolve_artifact_set(model_content_sha256=digest)
    assert manifest.model_content_sha256 == digest
    assert manifest.model_definition_ref is not None
    assert manifest.model_definition_ref.model_dump(mode="json") == {
        "kind": "model",
        "publisher": "vonk-forge",
        "slug": "canonical-model",
        "content_sha256": digest,
    }
    assert [
        (item.path, item.expected_bytes, item.roles) for item in manifest.artifacts
    ] == [("weights.bin", 3, ("weights",))]
