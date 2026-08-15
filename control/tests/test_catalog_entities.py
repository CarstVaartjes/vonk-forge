from __future__ import annotations

import copy
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from vonk_control.catalog_contract import catalog_content_sha256
from vonk_control.catalog_entities import CatalogEntityService
from vonk_control.catalog_service import CatalogConflict, CatalogValidationError
from vonk_control.models import Base


def _base(kind: str, slug: str, title: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": kind,
        "identity": {"publisher": "vonk-forge", "slug": slug},
        "metadata": {
            "title": title,
            "description": f"{title} contract fixture.",
            "tags": ["synthetic"],
        },
    }


def model_group() -> dict[str, object]:
    return {**_base("model-group", "synthetic", "Synthetic"), "family": "synthetic"}


def model(group_digest: str) -> dict[str, object]:
    return {
        **_base("model", "synthetic-tiny", "Synthetic Tiny"),
        "model_group": {
            "kind": "model-group",
            "publisher": "vonk-forge",
            "slug": "synthetic",
            "content_sha256": group_digest,
        },
        "architecture": "synthetic",
    }


def model_version(model_digest: str) -> dict[str, object]:
    return {
        **_base("model-version", "synthetic-tiny-fp16", "Synthetic Tiny FP16"),
        "model": {
            "kind": "model",
            "publisher": "vonk-forge",
            "slug": "synthetic-tiny",
            "content_sha256": model_digest,
        },
        "version": "1.0.0",
        "license": {"spdx": "Apache-2.0", "attribution": []},
        "artifacts": [
            {
                "kind": "huggingface.snapshot",
                "repository": "vonk-forge/synthetic-tiny",
                "revision": "0123456789abcdef0123456789abcdef01234567",
                "sha256": "c" * 64,
                "installed_bytes": 1024,
            }
        ],
    }


def runtime_distribution(slug: str = "python-312-cuda") -> dict[str, object]:
    return {
        **_base("runtime-distribution", slug, slug.replace("-", " ").title()),
        "platform": "linux/arm64",
        "sha256": "d" * 64,
    }


def execution_harness(distribution_digest: str) -> dict[str, object]:
    return {
        **_base("execution-harness", "vllm-openai", "vLLM OpenAI"),
        "runtime_interface": "vonk.runtime.v1",
        "adapters": ["openai"],
        "source_bundle": {
            "kind": "runtime-distribution",
            "publisher": "vonk-forge",
            "slug": "python-312-cuda",
            "content_sha256": distribution_digest,
        },
    }


def patch_bundle(distribution_slug: str, distribution_digest: str) -> dict[str, object]:
    return {
        **_base("patch-bundle", "vllm-fix", "vLLM Fix"),
        "applies_to": {
            "kind": "runtime-distribution",
            "publisher": "vonk-forge",
            "slug": distribution_slug,
            "content_sha256": distribution_digest,
        },
        "sha256": "e" * 64,
    }


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as value:
        yield value


@pytest.fixture
def service(session: Session) -> CatalogEntityService:
    return CatalogEntityService(
        session, clock=lambda: datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    )


def _resolve(service: CatalogEntityService, document: dict[str, object]):
    draft = service.create_draft(document, actor="admin")
    return service.resolve(draft.id, actor="admin")


def _resolved_model_version(service: CatalogEntityService):
    group = _resolve(service, model_group())
    model_revision = _resolve(service, model(group.content_sha256))
    return _resolve(service, model_version(model_revision.content_sha256))


def test_resolved_entity_revision_is_immutable(
    session: Session, service: CatalogEntityService
) -> None:
    resolved = _resolved_model_version(service)

    resolved.document["metadata"]["title"] = "changed"

    with pytest.raises(ValueError, match="immutable"):
        session.commit()


def test_exact_lookup_never_falls_back_to_a_newer_digest(
    service: CatalogEntityService,
) -> None:
    first = _resolve(service, model_group())
    changed = copy.deepcopy(first.document)
    changed["metadata"]["title"] = "Synthetic Updated"
    draft = service.revise(first.entity_id, changed, actor="admin")
    second = service.resolve(draft.id, actor="admin")

    assert (
        service.lookup_exact(
            "model-group", "vonk-forge", "synthetic", first.content_sha256
        ).id
        == first.id
    )
    assert (
        service.lookup_exact(
            "model-group", "vonk-forge", "synthetic", second.content_sha256
        ).id
        == second.id
    )
    with pytest.raises(CatalogConflict, match="exact model-group"):
        service.lookup_exact("model-group", "vonk-forge", "synthetic", "f" * 64)


def test_resolving_the_same_draft_is_idempotent(
    service: CatalogEntityService,
) -> None:
    draft = service.create_draft(model_group(), actor="admin")

    resolved = service.resolve(draft.id, actor="admin")
    repeated = service.resolve(draft.id, actor="admin")

    assert repeated.id == resolved.id
    assert repeated.content_sha256 == resolved.content_sha256


def test_model_version_resolution_requires_exact_model_and_group_lineage(
    service: CatalogEntityService,
) -> None:
    group = _resolve(service, model_group())
    model_revision = _resolve(service, model(group.content_sha256))
    document = model_version("f" * 64)
    draft = service.create_draft(document, actor="admin")

    with pytest.raises(CatalogConflict, match="exact model"):
        service.resolve(draft.id, actor="admin")

    assert model_revision.lifecycle == "resolved"


def test_patch_resolution_requires_its_exact_distribution(
    service: CatalogEntityService,
) -> None:
    distribution = _resolve(service, runtime_distribution())
    document = patch_bundle("different-distribution", distribution.content_sha256)
    draft = service.create_draft(document, actor="admin")

    with pytest.raises(CatalogConflict, match="exact runtime-distribution"):
        service.resolve(draft.id, actor="admin")


def test_entity_documents_reject_secret_values_without_reflecting_them(
    service: CatalogEntityService,
) -> None:
    document = model_group()
    document["metadata"]["source_gating_token"] = "never-reflect-me"

    with pytest.raises(CatalogValidationError) as caught:
        service.create_draft(document, actor="admin")

    assert caught.value.code == "catalog.sensitive_field"
    assert "never-reflect-me" not in str(caught.value)


def test_list_entities_returns_latest_revision_and_can_filter_kind(
    service: CatalogEntityService,
) -> None:
    group = _resolve(service, model_group())
    _resolve(service, runtime_distribution())

    values = service.list_entities(kind="model-group")

    assert [value.id for value in values] == [group.id]
    assert values[0].content_sha256 == catalog_content_sha256(group.document)
