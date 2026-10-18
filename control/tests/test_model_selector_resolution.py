"""Name selectors follow accepted heads; exact content and conflicts remain visible."""

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import Disposition, LifecycleState, ModelCacheCode
from vonk_control.api import create_app
from vonk_control.auth import Actor, TokenCodec
from vonk_control.catalog_entities import CatalogEntityService
from vonk_control.failure_classification import disposition
from vonk_control.library_projection import LibraryProjection
from vonk_control.model_cache import ModelCacheService
from vonk_control.model_cache.errors import ModelCacheError
from vonk_control.models import Base, CatalogDocumentRevision

from .test_model_cache import _artifact, _canonical_model, _download

NOW = datetime(2026, 10, 18, tzinfo=UTC)


@pytest.fixture
def world(tmp_path: Path, postgres_engine: Engine):
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    catalog = CatalogEntityService(sessions, clock=lambda: NOW)
    cache = ModelCacheService(
        sessions, tmp_path / "cache", reserve_bytes=0, fixture_sources=True
    )
    tokens = TokenCodec(b"m" * 32)
    app = create_app(
        jobs=Mock(),
        tokens=tokens,
        now=lambda: 10,
        model_cache=cache,
        library_projection=LibraryProjection(sessions, cursors=tokens.cursor_codec()),
    )
    with TestClient(app) as client:
        token = tokens.issue(Actor("test", "administrator"), ttl_seconds=1000, now=0)
        client.headers["Authorization"] = f"Bearer {token}"
        yield catalog, cache, sessions, client


def _activate(catalog: CatalogEntityService, *, publisher: str, weight: str):
    model = _canonical_model(
        publisher=publisher,
        slug="same-label",
        file_id="weights",
        file_digest=hashlib.sha256(weight.encode() * 3).hexdigest(),
    )
    row = catalog.create_draft(model.model_dump(mode="json"), actor="test")
    return catalog.resolve(row.id, actor="test")


def test_refresh_keeps_both_cached_contents_but_names_only_accepted_head(
    world, tmp_path: Path
):
    """Reject the old implementation that treats retained active revisions as heads."""
    catalog, cache, sessions, client = world
    old = _activate(catalog, publisher="test", weight="a")
    updated = _canonical_model(
        publisher="test",
        slug="same-label",
        file_id="weights",
        file_digest=hashlib.sha256(b"bbb").hexdigest(),
    )
    new = catalog.revise(old.document_id, updated.model_dump(mode="json"), actor="test")
    # An unaccepted refresh must not replace the name's meaning.
    assert cache._resolve_model_selector("test/same-label") == old.content_digest
    catalog.resolve(new.id, actor="test")
    for row, data in ((old, b"aaa"), (new, b"bbb")):
        downloaded = _download(
            cache,
            [
                _artifact(
                    tmp_path,
                    data,
                    path="weights.safetensors",
                    model_content_sha256=row.content_digest,
                )
            ],
            model_content_sha256=row.content_digest,
            request_key=str(uuid.uuid4()),
        )
        assert downloaded.state == LifecycleState.SUCCEEDED
    with sessions() as session:
        retained = list(session.scalars(select(CatalogDocumentRevision)))
        assert len(retained) == 2
    # Resolve names, the document UUID and exact historical identities consistently.
    for selector, digest in (
        ("test/same-label", new.content_digest),
        ("same-label", new.content_digest),
        (old.document_id, new.content_digest),
        (old.id, old.content_digest),
        (old.content_digest, old.content_digest),
        (new.content_digest, new.content_digest),
    ):
        assert cache._resolve_model_selector(selector) == digest
        response = client.get(f"/api/model/{selector}")
        assert response.status_code == 200, response.text
        assert response.json()["identity"]["content_sha256"] == digest
        review = client.get(f"/api/model/{selector}/remove-review")
        assert review.status_code == 200, review.text


def test_ambiguous_names_fail_once_with_same_409_and_candidate_digests(world):
    """A cross-publisher name conflict must not retry or crash validating hex choices."""
    catalog, cache, _sessions, client = world
    rows = [
        _activate(catalog, publisher=publisher, weight=weight)
        for publisher, weight in (("first", "a"), ("second", "b"))
    ]
    expected = sorted(row.content_digest for row in rows)
    with pytest.raises(ModelCacheError) as caught:
        cache._resolve_model_selector("same-label")
    assert disposition(caught.value) is Disposition.FAIL
    assert caught.value.code == ModelCacheCode.SELECTOR_AMBIGUOUS
    responses = [
        client.get(f"/api/model/same-label{suffix}")
        for suffix in ("", "/remove-review")
    ]
    for response in responses:
        assert response.status_code == 409, response.text
        assert (
            response.headers["x-vonk-error-code"] == ModelCacheCode.SELECTOR_AMBIGUOUS
        )
        assert response.json()["candidates"] == expected
        assert "retry-after" not in response.headers
    assert responses[0].json() == responses[1].json()
    for digest in expected:
        assert client.get(f"/api/model/{digest}").status_code == 200


def test_invalid_dependency_pin_is_not_observed_again(world):
    """Malformed immutable input fails before another observation attempt."""
    _catalog, cache, sessions, _client = world
    with sessions() as session, pytest.raises(ModelCacheError) as caught:
        cache._collect_model_definitions(session, "invalid", {})
    assert caught.value.code == ModelCacheCode.MODEL_PIN_INVALID
    assert disposition(caught.value) is Disposition.FAIL


def test_changed_removal_scope_requires_a_new_plan_and_can_then_be_accepted(world):
    """A known scope mismatch ends; repeating observations cannot repair its plan."""
    from dataclasses import replace

    from vonk_agent_protocol import ArtifactLifecycleCode

    catalog, cache, sessions, _client = world
    row = _activate(catalog, publisher="test", weight="a")
    with sessions() as session:
        scope = cache._model_removal_scope_for_sets(session, ())
    stale = replace(scope, selected_objects=("a" * 64,))
    key = str(uuid.uuid4())
    with sessions.begin() as session, pytest.raises(ModelCacheError) as caught:
        cache._accept_model_removal(
            session,
            actor="test",
            request_key=key,
            selector="test/same-label",
            model_content_sha256=row.content_digest,
            selected_sets=(),
            expected_scope=stale,
            defer_scope=False,
        )
    assert caught.value.code == ArtifactLifecycleCode.REFERENCE_IDENTITY_MISMATCH
    assert disposition(caught.value) is Disposition.FAIL
    # Replanning against current membership accepts through the normal owner.
    with sessions.begin() as session:
        accepted = cache._accept_model_removal(
            session,
            actor="test",
            request_key=key,
            selector="test/same-label",
            model_content_sha256=row.content_digest,
            selected_sets=(),
            expected_scope=scope,
            defer_scope=False,
        )
        assert accepted.id


@pytest.mark.parametrize(
    "action,options,suffix",
    [("detail", (), ""), ("remove", ("--review",), "/remove-review")],
)
def test_model_cli_passes_exact_digest_to_the_real_api(
    world, action, options, suffix, capsys
):
    """A digest reaches the Controller verbatim and its typed response reaches CLI output."""
    from cluster_profiles import cli

    catalog, _cache, _sessions, api = world
    row = _activate(catalog, publisher="test", weight="a")
    digest = row.content_digest

    class Client:
        request_timeout_seconds = 15.0

        def __init__(self):
            self.calls: list[tuple[str, str]] = []

        def request(self, method, path, **kwargs):
            self.calls.append((method, path))
            response = api.request(method, path)
            response.raise_for_status()
            return response.json()

    client = Client()
    assert (
        cli.main(("model", action, digest, *options, "--json"), control_client=client)
        == 0
    )
    assert client.calls == [("GET", f"/api/model/{digest}{suffix}")]
    assert digest in capsys.readouterr().out


def test_model_detail_uses_cache_selector_validation(world):
    """The shared decision must reject invalid names with the canonical cache error."""
    _catalog, cache, sessions, _client = world
    projection = LibraryProjection(
        sessions, cursors=TokenCodec(b"v" * 32).cursor_codec()
    )
    for resolve in (cache._resolve_model_selector, projection.model_detail):
        with pytest.raises(ModelCacheError) as caught:
            resolve(" ")
        assert caught.value.code == ModelCacheCode.SELECTOR_INVALID
        assert disposition(caught.value) is Disposition.FAIL
