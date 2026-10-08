"""Hermetic producer/store/build-consumer regressions for damaged source evidence."""

from __future__ import annotations

import io
from dataclasses import replace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import (
    SecurityRefusalError,
    UnknownOutcomeError,
    canonical_message,
)
from vonk_agent_protocol.source_bundles import SourceBundleManifest
from vonk_control.catalog_revision_contract import RecipeRevisionProjection
from vonk_control.models import Base, RecipeSourceBundle
from vonk_control.recipe_builds import RecipeBuildService, RecipeBuildUnknown
from vonk_control.source_bundles import (
    DatabaseSourceBundleStore,
    SourceBundleStore,
    generate_source_bundle,
)
from vonk_control.source_policy import enforce_build_source_policy

from .test_distributed_lifecycle import _topology


def _service(store, archive):
    # Source consumption does not need SQL; a regression opening a session here
    # fails because this factory deliberately has no database binding.
    return RecipeBuildService(
        sessionmaker(), bundles=store, source_rederiver=lambda *_: archive
    )


def _projected():
    return RecipeRevisionProjection(
        title="test",
        description="",
        tags=[],
        runtime_engine="test",
        topology=_topology(),
    )


@pytest.mark.parametrize("damage", [b"truncated", b"header-only", None])
def test_damaged_bundle_is_rederived_for_build_and_fresh_request(tmp_path, damage):
    bundle = generate_source_bundle({"Dockerfile": b"FROM scratch\n"})
    store = SourceBundleStore(tmp_path)
    stored = store.put(bundle.sha256, io.BytesIO(bundle.archive))
    if damage is None:
        stored.path.unlink()
    else:
        stored.path.write_bytes(
            bundle.archive[:600] if damage == b"header-only" else damage
        )
    with pytest.raises(UnknownOutcomeError):
        store.get(bundle.sha256)
    service = _service(store, bundle.archive)
    build = {"dockerfile": "Dockerfile", "context": {"path": "."}}
    for _ in range(2):
        loaded = service._verified_bundle(_projected(), build, bundle.sha256)
        assert loaded.files["Dockerfile"] == b"FROM scratch\n"
        assert loaded.sha256 == bundle.sha256


def test_damage_after_reingress_is_unknown_and_does_not_poison_fresh_build(tmp_path):
    bundle = generate_source_bundle({"Dockerfile": b"FROM scratch\n"})
    store = SourceBundleStore(tmp_path)
    store.put(bundle.sha256, io.BytesIO(bundle.archive))
    real_get = store.get
    store.get = lambda sha256: replace(real_get(sha256), files={})
    service = _service(store, bundle.archive)
    build = {"dockerfile": "Dockerfile", "context": {"path": "."}}
    with pytest.raises(RecipeBuildUnknown):
        service._verified_bundle(_projected(), build, bundle.sha256)
    store.get = real_get
    assert service._verified_bundle(_projected(), build, bundle.sha256).files


def test_wrong_rederived_identity_is_security_refusal_and_fresh_build_recovers(
    tmp_path,
):
    bundle = generate_source_bundle({"Dockerfile": b"FROM scratch\n"})
    wrong = generate_source_bundle({"Dockerfile": b"FROM wrong\n"})
    store = SourceBundleStore(tmp_path)
    service = _service(store, wrong.archive)
    build = {"dockerfile": "Dockerfile", "context": {"path": "."}}
    with pytest.raises(SecurityRefusalError):
        service._verified_bundle(_projected(), build, bundle.sha256)
    service = _service(store, bundle.archive)
    assert service._verified_bundle(_projected(), build, bundle.sha256).files


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize("missing", [True, False])
def test_bundle_receipt_damage_cannot_hide_verified_archive_or_poison_ingress(missing):
    # SQLite exercises only receipt row presence, not locks/concurrency.
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    store = DatabaseSourceBundleStore(sessions)
    bundle = generate_source_bundle({"Dockerfile": b"FROM scratch\n"})
    store.put(bundle.sha256, io.BytesIO(bundle.archive))
    with sessions.begin() as session:
        row = session.get(RecipeSourceBundle, bundle.sha256)
        assert row is not None
        if missing:
            session.delete(row)
        else:
            row.manifest = {"damaged": True}
    assert store.get(bundle.sha256).manifest == bundle.manifest
    for _ in range(2):
        store.put(bundle.sha256, io.BytesIO(bundle.archive))
        assert store.get(bundle.sha256).manifest == bundle.manifest
        with sessions() as session:
            row = session.get(RecipeSourceBundle, bundle.sha256)
            assert row is not None
            assert (
                SourceBundleManifest.model_validate_json(
                    canonical_message(row.manifest)
                )
                == bundle.manifest
            )
    engine.dispose()


def test_request_retries_are_bounded_and_fresh_source_check_proceeds(
    tmp_path, monkeypatch
):
    dockerfile = b"FROM ghcr.io/example/runtime@sha256:" + b"a" * 64 + b"\nUSER 10001\n"
    bundle = generate_source_bundle({"Dockerfile": dockerfile})
    store = SourceBundleStore(tmp_path)
    stored = store.put(bundle.sha256, io.BytesIO(bundle.archive))
    stored.path.write_bytes(b"partial")
    service = _service(store, None)
    delays = []
    attempts = []
    service._sleep = delays.append
    build = {
        "dockerfile": "Dockerfile",
        "context": {"path": ".", "sha256": bundle.sha256},
    }

    def check(revision_id):
        attempts.append(revision_id)
        loaded = service._verified_bundle(_projected(), build, bundle.sha256)
        return enforce_build_source_policy({"build": build}, loaded)

    monkeypatch.setattr(service, "_check_source_once", check)
    with pytest.raises(UnknownOutcomeError):
        service.check_source("same-revision")
    assert attempts == ["same-revision"] * 3
    assert delays == [0.1, 0.2]
    service._source_rederiver = lambda *_: bundle.archive
    assert service.check_source("same-revision").passed
    assert store.get(bundle.sha256).sha256 == bundle.sha256
