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
from vonk_agent_protocol import LifecycleState
from vonk_control.auth import Actor
from vonk_control.model_cache import (
    ArtifactSetManifest,
    ArtifactSpec,
    ModelCacheConflict,
    ModelCacheResolutionError,
    ModelCacheService,
)
from vonk_control.model_cache_api import (
    install_model_operator_routes,
)
from vonk_control.model_cache_contract import (
    ModelCacheDownloadResult,
)
from vonk_control.models import (
    AgentNode,
    ArtifactLifecycleGate,
    CatalogDocument,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    ModelCacheOperation,
    ModelCacheSet,
    ModelCacheSetArtifact,
    RecipeBuild,
)
from vonk_control.run_switch_operations import DatabaseRunSwitchArtifactInspector
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, document_sha256
from vonk_forge_contracts.model import ModelReference

from . import model_cache_support as _support
from .display_fixtures import reword, reworded
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

cache = _support.cache


def _canonical_recipe(model_digest: str) -> dict[str, object]:
    document = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "recipe-source-build.json")
        .read_text(encoding="utf-8")
    )
    document["models"][0]["model"]["content_sha256"] = model_digest
    return document


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
        from cluster_profiles.control_client import ControlClient

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


def test_resolve_latest_cached_returns_the_head_even_when_an_older_revision_is_cached(
    cache, monkeypatch: pytest.MonkeyPatch
):
    service, sessions = cache
    model = _canonical_model(
        publisher="vonk-forge",
        slug="resolver-model",
        file_id="weights",
        file_digest="1" * 64,
    )
    model_digest = document_sha256(model.model_dump(mode="json"))
    newer_model = _canonical_model(
        publisher="vonk-forge",
        slug="resolver-model",
        file_id="weights",
        file_digest="2" * 64,
    )
    newer_model_digest = document_sha256(newer_model.model_dump(mode="json"))
    old_document = _canonical_recipe(model_digest)
    old_document["identity"] = {"publisher": "vonk-forge", "slug": "resolver-recipe"}
    old_recipe = RecipeDefinition.model_validate(old_document)
    new_document = json.loads(json.dumps(old_document))
    new_document["models"][0]["model"]["content_sha256"] = newer_model_digest
    reword(new_document, " newer")
    new_recipe = RecipeDefinition.model_validate(new_document)
    model_root = CatalogDocument(
        id="00000000-0000-0000-0000-000000000101",
        kind="model",
        publisher="vonk-forge",
        slug="resolver-model",
        title="Resolver model",
        created_by="test",
        created_at=NOW,
        updated_at=NOW,
    )
    recipe_root = CatalogDocument(
        id="00000000-0000-0000-0000-000000000102",
        kind="recipe",
        publisher="vonk-forge",
        slug="resolver-recipe",
        title="Resolver recipe",
        created_by="test",
        created_at=NOW,
        updated_at=NOW,
    )
    old_digest = document_sha256(old_recipe.model_dump(mode="json"))
    new_digest = document_sha256(new_recipe.model_dump(mode="json"))
    old_revision = CatalogDocumentRevision(
        id="00000000-0000-0000-0000-000000000103",
        document_id=recipe_root.id,
        kind="recipe",
        publisher=recipe_root.publisher,
        slug=recipe_root.slug,
        revision_number=1,
        schema_version=2,
        state="active",
        document=old_recipe.model_dump(mode="json"),
        content_digest=old_digest,
        execution_key="a" * 64,
        projected={},
        created_by="test",
        created_at=NOW,
    )
    new_revision = CatalogDocumentRevision(
        id="00000000-0000-0000-0000-000000000104",
        document_id=recipe_root.id,
        kind="recipe",
        publisher=recipe_root.publisher,
        slug=recipe_root.slug,
        revision_number=2,
        schema_version=2,
        state="active",
        document=new_recipe.model_dump(mode="json"),
        content_digest=new_digest,
        execution_key="b" * 64,
        projected={},
        created_by="test",
        created_at=NOW,
    )
    model_revision = CatalogDocumentRevision(
        id="00000000-0000-0000-0000-000000000105",
        document_id=model_root.id,
        kind="model",
        publisher=model_root.publisher,
        slug=model_root.slug,
        revision_number=1,
        schema_version=2,
        state="failed",
        document=model.model_dump(mode="json"),
        content_digest=model_digest,
        projected={},
        created_by="test",
        created_at=NOW,
    )
    newer_model_revision = CatalogDocumentRevision(
        id="00000000-0000-0000-0000-000000000109",
        document_id=model_root.id,
        kind="model",
        publisher=model_root.publisher,
        slug=model_root.slug,
        revision_number=2,
        schema_version=2,
        state="active",
        document=newer_model.model_dump(mode="json"),
        content_digest=newer_model_digest,
        projected={},
        created_by="test",
        created_at=NOW,
    )
    build = RecipeBuild(
        id="00000000-0000-0000-0000-000000000106",
        recipe_revision_id=old_revision.id,
        builder_node_id="resolver-builder",
        source_bundle_sha256="2" * 64,
        build_input_sha256="3" * 64,
        state="succeeded",
        policy_report={},
        plan={},
        image_digest="sha256:" + "6" * 64,
        oci_layout_sha256="8" * 64,
        image_bytes=17,
        error=None,
        created_at=NOW,
        updated_at=NOW,
    )
    with sessions.begin() as session:
        session.add_all(
            [
                model_root,
                recipe_root,
                model_revision,
                newer_model_revision,
                old_revision,
                new_revision,
                AgentNode(node_id="resolver-builder", state="active"),
                build,
            ]
        )

    manifest = ArtifactSetManifest(
        model_content_sha256=model_digest,
        recipe_revision_sha256=None,
        model_content_digests=(model_digest,),
        artifacts=(
            ArtifactSpec(
                key="artifact-111111111111-weights",
                artifact_id="weights",
                path="weights.safetensors",
                kind="huggingface.file",
                repository="https://huggingface.co/vonk-forge/resolver-model",
                source=(
                    "https://huggingface.co/vonk-forge/resolver-model/resolve/"
                    + "0" * 40
                    + "/weights.safetensors"
                ),
                revision="0" * 40,
                sha256="1" * 64,
                expected_bytes=3,
                roles=("weights",),
                model_content_sha256=model_digest,
            ),
        ),
        model_definition_ref=ModelReference(
            publisher="vonk-forge",
            slug="resolver-model",
            content_sha256=model_digest,
        ),
    )
    artifact = manifest.artifacts[0]
    model_cache = ModelCacheSet(
        artifact_set_sha256=manifest.digest,
        schema_version=2,
        model_content_sha256=model_digest,
        recipe_revision_sha256=None,
        manifest=manifest.document(),
        expected_bytes=3,
        verified_bytes=3,
        state="cached",
        protected=False,
        protected_reasons=[],
        created_at=NOW,
        updated_at=NOW,
        verified_at=NOW,
        last_accessed_at=NOW,
        last_error=None,
    )
    model_object = service.root / "objects" / artifact.sha256[:2] / artifact.sha256
    model_object.parent.mkdir(parents=True)
    model_object.write_bytes(b"one")
    image_object = service.root.parent / "runtime-images" / build.oci_layout_sha256
    assert build.image_bytes is not None
    image_object.write_bytes(b"x" * build.image_bytes)
    with sessions.begin() as session:
        session.add(model_cache)

    # The catalog head is current: an older cached revision never stands in for
    # it. The head comes back with its missing cache reported.
    resolved = service.resolve_latest_cached(
        recipe_identity="vonk-forge/resolver-recipe", model_variant="fp16"
    )
    assert resolved.recipe.recipe_revision_id == new_revision.id
    assert resolved.recipe.update_available is False
    assert resolved.recipe.cached is False
    assert "recipe-not-cached" in resolved.blockers

    # An exact older revision is told apart from the head.
    older = service.resolve_latest_cached(
        recipe_identity="vonk-forge/resolver-recipe",
        model_variant="fp16",
        exact_revision_id=old_revision.id,
    )
    assert older.recipe.recipe_revision_id == old_revision.id
    assert older.recipe.update_available is True
    assert older.recipe.cached is True
    assert older.model.content_sha256 == model_digest

    # An explicitly selected exact revision is never silently replaced by an
    # older cached one: the newer head comes back with its missing cache
    # reported instead.
    exact = service.resolve_latest_cached(
        recipe_identity="vonk-forge/resolver-recipe",
        model_variant="fp16",
        exact_revision_id=new_revision.id,
    )
    assert exact.recipe.recipe_revision_id == new_revision.id
    assert exact.recipe.cached is False
    assert exact.recipe.cache_state == "missing"
    assert exact.recipe.content_sha256 == new_digest
    assert "recipe-not-cached" in exact.blockers

    real_open = os.open

    def deny_model_object(path, *args, **kwargs):
        if Path(path) == model_object:
            raise PermissionError("model object is inaccessible")
        return real_open(path, *args, **kwargs)

    def deny_runtime_archive(_digest: str, _size: int) -> bool:
        raise PermissionError("runtime archive is inaccessible")

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "open", deny_model_object)
        observed = service.resolve_latest_cached(
            recipe_identity="vonk-forge/resolver-recipe",
            model_variant="fp16",
            exact_revision_id=old_revision.id,
        )
        assert not (observed.model.cached and observed.recipe.cached)
    restored = service.resolve_latest_cached(
        recipe_identity="vonk-forge/resolver-recipe",
        model_variant="fp16",
        exact_revision_id=old_revision.id,
    )
    assert not restored.blockers
    with monkeypatch.context() as scoped:
        scoped.setattr(
            service,
            "_runtime_archive_available",
            deny_runtime_archive,
        )
        observed = service.resolve_latest_cached(
            recipe_identity="vonk-forge/resolver-recipe",
            model_variant="fp16",
            exact_revision_id=old_revision.id,
        )
        assert not (observed.model.cached and observed.recipe.cached)

    model_object.unlink()
    missing_model_bytes = service.resolve_latest_cached(
        recipe_identity="vonk-forge/resolver-recipe",
        model_variant="fp16",
        exact_revision_id=old_revision.id,
    )
    assert missing_model_bytes.recipe.cached is True
    assert missing_model_bytes.model.cached is False
    assert "model-not-cached" in missing_model_bytes.blockers
    model_object.write_bytes(b"one")

    image_object.unlink()
    missing_image_bytes = service.resolve_latest_cached(
        recipe_identity="vonk-forge/resolver-recipe",
        model_variant="fp16",
        exact_revision_id=old_revision.id,
    )
    assert missing_image_bytes.recipe.recipe_revision_id == old_revision.id
    assert missing_image_bytes.recipe.cached is False
    assert "recipe-not-cached" in missing_image_bytes.blockers
    image_object.write_bytes(b"x" * build.image_bytes)

    with sessions.begin() as session:
        session.delete(model_cache)
    image_only = service.resolve_latest_cached(
        recipe_identity="vonk-forge/resolver-recipe",
        model_variant="fp16",
        exact_revision_id=old_revision.id,
    )
    assert image_only.recipe.cached is True
    assert image_only.model.cached is False
    assert image_only.resources.additional_disk_bytes is None

    with sessions.begin() as session:
        session.delete(build)
    missing = service.resolve_latest_cached(
        recipe_identity="vonk-forge/resolver-recipe",
        model_variant="fp16",
        exact_revision_id=old_revision.id,
    )
    assert missing.recipe.recipe_revision_id == old_revision.id
    assert missing.recipe.expected_bytes is None
    assert missing.model.expected_bytes is None
    assert missing.resources.additional_disk_bytes is None


@pytest.mark.parametrize("shared_object", [False, True])
@pytest.mark.parametrize("partial_checkpoint", [False, True])
def test_canonical_dependency_closure_reaches_run_switch(
    cache,
    shared_object: bool,
    partial_checkpoint: bool,
) -> None:
    service, sessions = cache
    companion = _canonical_model(
        publisher="vonk-forge",
        slug="companion",
        file_id="encoder",
        file_digest=("3" if shared_object else "2") * 64,
    )
    companion_document = companion.model_dump(mode="json")
    companion_document["files"].append(
        {
            "id": "empty-config",
            "path": "config/empty.txt",
            "sha256": hashlib.sha256(b"").hexdigest(),
            "size_bytes": 0,
            "roles": ["config"],
        }
    )
    companion = ModelDefinition.model_validate(companion_document)
    companion_digest = document_sha256(companion.model_dump(mode="json"))
    primary = _canonical_model(
        publisher="vonk-forge",
        slug="primary",
        file_id="weights",
        file_digest="3" * 64,
        dependencies=[
            {
                "kind": "model",
                "publisher": "vonk-forge",
                "slug": "companion",
                "content_sha256": companion_digest,
            }
        ],
    )
    primary_digest = document_sha256(primary.model_dump(mode="json"))
    recipe_document = _canonical_recipe(primary_digest)
    models = recipe_document["models"]
    assert isinstance(models, list) and models
    selection = models[0]
    assert isinstance(selection, dict)
    model_reference = selection["model"]
    assert isinstance(model_reference, dict)
    model_reference["slug"] = "primary"
    recipe = RecipeDefinition.model_validate(recipe_document)
    recipe_digest = document_sha256(recipe.model_dump(mode="json"))
    with sessions.begin() as session:
        for index, (definition, digest, slug) in enumerate(
            (
                (primary, primary_digest, "primary"),
                (companion, companion_digest, "companion"),
                (recipe, recipe_digest, recipe.identity.slug),
            ),
            start=41,
        ):
            kind = definition.kind
            root_id = f"00000000-0000-0000-0000-0000000000{index:02d}"
            session.add(
                CatalogDocument(
                    id=root_id,
                    kind=kind,
                    publisher="vonk-forge",
                    slug=slug,
                    title=slug,
                    created_by="test",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            session.add(
                CatalogDocumentRevision(
                    id=f"00000000-0000-0000-0000-0000000000{index + 10:02d}",
                    document_id=root_id,
                    kind=kind,
                    publisher="vonk-forge",
                    slug=slug,
                    revision_number=1,
                    schema_version=2,
                    state="active",
                    document=definition.model_dump(mode="json"),
                    content_digest=digest,
                    projected={},
                    created_by="test",
                    created_at=NOW,
                )
            )

    manifest = service.resolve_artifact_set(model_content_sha256=primary_digest)

    assert manifest.model_content_sha256 == primary_digest
    assert manifest.model_content_digests == tuple(
        sorted((primary_digest, companion_digest))
    )
    assert {item.model_content_sha256 for item in manifest.artifacts} == {
        primary_digest,
        companion_digest,
    }

    inspector = DatabaseRunSwitchArtifactInspector(service)
    if partial_checkpoint:
        for spec in manifest.artifacts:
            if spec.expected_bytes:
                partial = service._partial_path(manifest.digest, spec.sha256)
                partial.parent.mkdir(parents=True, exist_ok=True)
                partial.write_bytes(b"x")
    with sessions() as session:
        inspection = inspector.inspect(
            session,
            model_content_sha256=primary_digest,
            recipe_revision_id="00000000-0000-0000-0000-000000000053",
            node_ids=("spk_" + "9" * 32,),
            now=NOW,
        )
    # Real ModelDefinition -> cache service -> Run/Switch, including companion
    # identity, empty support files and one-copy accounting of shared bytes.
    assert inspection.dependency_model_content_sha256 == (companion_digest,)
    assert inspection.artifact_set_sha256 == manifest.digest
    assert inspection.artifact_set_bytes == (3 if shared_object else 6)
    assert inspection.required_bytes == inspection.artifact_set_bytes
    assert inspection.missing_nas_bytes == inspection.artifact_set_bytes


def test_download_persists_real_primary_and_auxiliary_bytes_and_deduplicates(
    cache, tmp_path: Path
) -> None:
    service, sessions = cache
    model_a = "a" * 64
    primary = _artifact(tmp_path, b"primary model bytes", model_content_sha256=model_a)
    auxiliary = _artifact(
        tmp_path,
        b"tokenizer auxiliary bytes",
        artifact_id="tokenizer",
        path="tokenizer.json",
        model_content_sha256=model_a,
    )

    first = _download(
        service,
        [primary, auxiliary],
        model_content_sha256=model_a,
        request_key="00000000-0000-4000-8000-000000000001",
    )
    assert first.state == "succeeded"
    assert isinstance(first.result, ModelCacheDownloadResult)
    assert first.result.artifact_set_sha256 == first.artifact_set_sha256
    entry = service.get_entry(first.artifact_set_sha256 or "")
    assert entry["coverage"] == "complete"
    assert entry["expected_bytes"] == len(
        b"primary model bytestokenizer auxiliary bytes"
    )
    assert entry["verified_bytes"] == entry["expected_bytes"]
    assert {item["path"] for item in entry["artifacts"]} == {
        "weights.bin",
        "tokenizer.json",
    }
    preparation = service.preparation_evidence(first.artifact_set_sha256 or "")
    assert preparation["artifact_set_sha256"] == first.artifact_set_sha256
    assert preparation["artifact_set_bytes"] == entry["expected_bytes"]
    assert preparation["controller"]["state"] == "ready"
    assert preparation["controller"]["verified_sha256"] == first.artifact_set_sha256
    assert preparation["targets"] == []

    descriptors = service.resolve_verified_artifact_set(first.artifact_set_sha256 or "")
    assert {item["sha256"] for item in descriptors} == {
        primary["sha256"],
        auxiliary["sha256"],
    }
    assert {item["file_id"] for item in descriptors} == {"weights", "tokenizer"}
    assert {item["model_content_sha256"] for item in descriptors} == {model_a}
    assert (
        service.read_verified_artifact(
            first.artifact_set_sha256 or "",
            str(primary["sha256"]),
            "weights.bin",
            offset=8,
            maximum_bytes=6,
        )
        == b"model "
    )

    model_b = "b" * 64
    primary_b = dict(primary, model_content_sha256=model_b)
    auxiliary_b = dict(auxiliary, model_content_sha256=model_b)
    second = _download(
        service,
        [primary_b, auxiliary_b],
        model_content_sha256=model_b,
        request_key="00000000-0000-4000-8000-000000000002",
    )
    assert second.state == "succeeded"
    with sessions() as session:
        # SQL owns membership: two objects, one membership row per set.
        assert (
            session.scalar(select(func.count()).select_from(ModelCacheSetArtifact)) == 4
        )
    assert service.storage_summary().unique_used_bytes == len(
        b"primary model bytes"
    ) + len(b"tokenizer auxiliary bytes")


def test_upstream_revision_downloads_only_new_files_and_reuses_the_rest(
    cache, tmp_path: Path
) -> None:
    """A model updated upstream shares most files with the cached revision.

    Unchanged objects are reused by digest without fetching or re-hashing
    them; only the changed and added files are downloaded, and the new set
    is complete and describable for the compiler.
    """

    service, _sessions = cache
    model = "a" * 64
    shard = _artifact(tmp_path, b"unchanged shard bytes", model_content_sha256=model)
    config = _artifact(
        tmp_path,
        b"old config",
        artifact_id="config",
        path="config.json",
        model_content_sha256=model,
    )
    first = _download(
        service,
        [shard, config],
        model_content_sha256=model,
        request_key="00000000-0000-4000-8000-000000000031",
    )
    assert first.state == "succeeded"
    # The unchanged shard cannot be fetched again: its source is gone.
    Path(str(shard["source"]).removeprefix("file://")).unlink()

    updated = "b" * 64
    (tmp_path / "update").mkdir()
    new_config = _artifact(
        tmp_path / "update",
        b"new config!",
        artifact_id="config",
        path="config.json",
        model_content_sha256=updated,
    )
    added = _artifact(
        tmp_path / "update",
        b"added tokenizer",
        artifact_id="tokenizer",
        path="tokenizer.json",
        model_content_sha256=updated,
    )
    files = [dict(shard, model_content_sha256=updated), new_config, added]
    preview = service.download_preview(model_content_sha256=updated, artifacts=files)
    assert preview["artifact_set_sha256"] != first.artifact_set_sha256
    assert preview["new_bytes"] == len(b"new config!") + len(b"added tokenizer")
    second = _download(
        service,
        files,
        model_content_sha256=updated,
        request_key="00000000-0000-4000-8000-000000000032",
    )
    assert second.state == "succeeded", second.last_error
    entry = service.get_entry(second.artifact_set_sha256 or "")
    assert entry["coverage"] == "complete"
    descriptors = service.resolve_verified_artifact_set(
        second.artifact_set_sha256 or ""
    )
    assert {
        (item["model_content_sha256"], item["file_id"]) for item in descriptors
    } == {
        (updated, "weights"),
        (updated, "config"),
        (updated, "tokenizer"),
    }


@pytest.mark.parametrize(
    "invalid_result",
    [
        {"schema_version": 2, "artifact_set_sha256": "a" * 64},
        {"schema_version": 2, "removed_entries": [], "reclaimed_bytes": 0},
        "not a result document",
    ],
)
@pytest.mark.usefixtures("damaged_json_rows")
def test_cache_operation_reads_rebuild_malformed_or_wrong_kind_results(
    cache,
    tmp_path: Path,
    invalid_result: object,
) -> None:
    """A damaged result is re-derived from the operation's own evidence."""

    service, sessions = cache
    operation = _download(
        service,
        [_artifact(tmp_path, b"cached payload")],
        model_content_sha256="a" * 64,
        request_key="00000000-0000-4000-8000-000000000019",
    )
    assert isinstance(operation.result, ModelCacheDownloadResult)
    with sessions.begin() as session:
        row = session.get(ModelCacheOperation, operation.id)
        row.payload = {**row.payload, "result": invalid_result}
    rebuilt = service.get_operation(operation.id)
    assert rebuilt.state == "succeeded"
    assert isinstance(rebuilt.result, ModelCacheDownloadResult)
    assert rebuilt.result.artifact_set_sha256 == operation.artifact_set_sha256


def test_one_set_with_shared_digest_counts_one_physical_payload(
    cache, tmp_path: Path
) -> None:
    service, _sessions = cache
    model = "9" * 64
    primary = _artifact(tmp_path, b"shared payload", model_content_sha256=model)
    alias = dict(
        primary,
        id="weights-alias",
        path="weights-alias.bin",
        roles=["auxiliary"],
    )
    operation = _download(
        service,
        [primary, alias],
        model_content_sha256=model,
        request_key="00000000-0000-4000-8000-000000000015",
    )
    assert operation.state == "succeeded"
    assert operation.progress["downloaded_bytes"] == len(b"shared payload")
    entry = service.get_entry(operation.artifact_set_sha256 or "")
    assert entry["coverage"] == "complete"
    assert entry["expected_bytes"] == len(b"shared payload")
    assert entry["unique_bytes"] == len(b"shared payload")
    assert service.storage_summary().unique_used_bytes == len(b"shared payload")
