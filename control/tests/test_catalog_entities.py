from __future__ import annotations

import base64
import copy
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, delete, event
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from vonk_control.auth import TokenCodec
from vonk_control.catalog_contract import catalog_content_sha256
from vonk_control.catalog_entities import CatalogEntityService
from vonk_control.catalog_service import CatalogConflict, CatalogValidationError
from vonk_control.models import Base, CatalogEntity, CatalogEntityRevision


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


def runtime_distribution(
    harness_digest: str,
    *,
    slug: str = "python-312-cuda",
    harness_slug: str = "vllm-openai",
) -> dict[str, object]:
    return {
        **_base("runtime-distribution", slug, slug.replace("-", " ").title()),
        "implements_harness": {
            "kind": "execution-harness",
            "publisher": "vonk-forge",
            "slug": harness_slug,
            "content_sha256": harness_digest,
        },
        "platform": "linux/arm64",
        "sha256": "d" * 64,
    }


def execution_harness(
    slug: str = "vllm-openai", *, source_sha256: str = "b" * 64
) -> dict[str, object]:
    return {
        **_base("execution-harness", slug, slug.replace("-", " ").title()),
        "runtime_interface": "vonk.runtime.v1",
        "adapters": ["openai"],
        "source_bundle": {
            "sha256": source_sha256,
            "expected_bytes": 2048,
            "media_type": "application/vnd.vonk-forge.source-bundle.v1+tar",
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
        session,
        clock=lambda: datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
        cursors=TokenCodec(b"c" * 32).cursor_codec(),
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
    harness = _resolve(service, execution_harness())
    distribution = _resolve(service, runtime_distribution(harness.content_sha256))
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
    _resolve(service, execution_harness())

    values, next_cursor = service.list_entities(kind="model-group")

    assert [value.id for value in values] == [group.id]
    assert next_cursor is None
    assert values[0].content_sha256 == catalog_content_sha256(group.document)


def test_list_entities_pages_more_than_one_hundred_with_an_opaque_cursor(
    service: CatalogEntityService,
) -> None:
    for index in range(101):
        document = model_group()
        slug = f"catalog-page-{index:03d}"
        document["identity"]["slug"] = slug
        document["metadata"]["title"] = f"Catalog Page {index:03d}"
        document["family"] = slug
        service.create_draft(document, actor="admin")

    first, cursor = service.list_entities(kind="model-group", limit=100)
    second, final_cursor = service.list_entities(
        kind="model-group", limit=100, cursor=cursor
    )

    assert len(first) == 100
    assert cursor is not None and cursor.startswith("v1.")
    assert all(value.entity_id not in cursor for value in first)
    encoded_body = cursor.split(".")[1]
    decoded_body = base64.urlsafe_b64decode(
        encoded_body + "=" * (-len(encoded_body) % 4)
    )
    assert decoded_body != first[-1].entity_id.encode("ascii")
    assert len(second) == 1
    assert final_cursor is None
    assert {value.entity_id for value in first}.isdisjoint(
        value.entity_id for value in second
    )


def test_entity_cursor_is_bound_to_filters_and_rejects_tampering(
    service: CatalogEntityService,
) -> None:
    for index in range(3):
        document = model_group()
        document["identity"]["slug"] = f"cursor-context-{index}"
        document["metadata"]["title"] = f"Cursor Context {index}"
        document["family"] = f"cursor-context-{index}"
        service.create_draft(document, actor="admin")
    _first, cursor = service.list_entities(kind="model-group", limit=1)
    assert cursor is not None

    for invalid in (
        cursor[:-1] + ("A" if cursor[-1] != "A" else "B"),
        cursor,
    ):
        with pytest.raises(CatalogValidationError) as caught:
            service.list_entities(
                kind="model-group",
                publisher="vonk-forge" if invalid == cursor else None,
                limit=1,
                cursor=invalid,
            )
        assert caught.value.code == "catalog.cursor"


def test_entity_pagination_uses_immutable_boundary_when_rows_are_revised(
    session: Session, service: CatalogEntityService
) -> None:
    for index in range(4):
        document = model_group()
        document["identity"]["slug"] = f"cursor-mutation-{index}"
        document["metadata"]["title"] = f"Cursor Mutation {index}"
        document["family"] = f"cursor-mutation-{index}"
        service.create_draft(document, actor="admin")
    first, cursor = service.list_entities(kind="model-group", limit=2)
    assert cursor is not None
    boundary = session.get(CatalogEntity, first[-1].entity_id)
    assert boundary is not None
    boundary.updated_at = datetime(2027, 1, 1, tzinfo=UTC)
    session.flush()

    second, final_cursor = service.list_entities(
        kind="model-group", limit=2, cursor=cursor
    )

    assert final_cursor is None
    assert len({value.entity_id for value in (*first, *second)}) == 4


def test_harness_resolution_keeps_source_bundle_separate_from_catalog_entities(
    service: CatalogEntityService,
) -> None:
    resolved = _resolve(service, execution_harness(source_sha256="a" * 64))

    assert resolved.document["source_bundle"] == {
        "sha256": "a" * 64,
        "expected_bytes": 2048,
        "media_type": "application/vnd.vonk-forge.source-bundle.v1+tar",
    }


def test_distribution_resolution_requires_its_exact_implemented_harness(
    service: CatalogEntityService,
) -> None:
    document = runtime_distribution("f" * 64)
    draft = service.create_draft(document, actor="admin")

    with pytest.raises(CatalogConflict, match="exact execution-harness"):
        service.resolve(draft.id, actor="admin")


def test_entity_parent_cannot_be_deleted_while_revisions_exist(
    session: Session, service: CatalogEntityService
) -> None:
    resolved = _resolve(service, model_group())

    session.delete(resolved.entity)
    with pytest.raises(ValueError, match="revisions cannot be deleted"):
        session.commit()


def test_database_foreign_key_restricts_bulk_parent_deletion() -> None:
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _foreign_keys(dbapi_connection, _record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        local_service = CatalogEntityService(
            session,
            clock=lambda: datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
            cursors=TokenCodec(b"d" * 32).cursor_codec(),
        )
        draft = local_service.create_draft(model_group(), actor="admin")
        session.commit()

        with pytest.raises(IntegrityError):
            session.execute(
                delete(CatalogEntity).where(CatalogEntity.id == draft.entity_id)
            )


def test_resolve_locks_parent_and_checks_expected_revision_in_service(
    service: CatalogEntityService,
) -> None:
    draft = service.create_draft(model_group(), actor="admin")
    changed = copy.deepcopy(draft.document)
    changed["metadata"]["title"] = "Synthetic Updated"
    service.revise(
        draft.entity_id,
        changed,
        actor="admin",
        expected_revision=draft.revision_number,
    )

    with pytest.raises(CatalogConflict) as caught:
        service.resolve(
            draft.entity_id,
            actor="admin",
            expected_revision=draft.revision_number,
        )

    assert caught.value.code == "catalog.stale_entity_revision"
    statement = service._entity_statement(draft.entity_id, for_update=True)
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert compiled.endswith("FOR UPDATE")


def test_resolution_integrity_race_has_a_stable_conflict_code(
    service: CatalogEntityService,
) -> None:
    draft = service.create_draft(model_group(), actor="admin")

    def _raise_integrity_error(_mapper, _connection, target) -> None:
        if target.lifecycle == "resolved":
            raise IntegrityError("INSERT", {}, RuntimeError("simulated race"))

    event.listen(CatalogEntityRevision, "before_insert", _raise_integrity_error)
    try:
        with pytest.raises(CatalogConflict) as caught:
            service.resolve(draft.entity_id, actor="admin", expected_revision=1)
    finally:
        event.remove(CatalogEntityRevision, "before_insert", _raise_integrity_error)

    assert caught.value.code == "catalog.entity_resolution_conflict"


def test_entity_cursor_and_limit_validation_have_stable_codes(
    service: CatalogEntityService,
) -> None:
    signed_malformed = service._cursors.encode(
        resource="catalog-entities",
        order="created-at-desc/id-desc/v1",
        context={"kind": None, "publisher": None},
        boundary=["not-a-date", "not-an-id"],
    )
    for invalid_cursor in ("v1.not-a-valid-cursor", "v1._", signed_malformed):
        with pytest.raises(CatalogValidationError) as cursor_error:
            service.list_entities(cursor=invalid_cursor)
        assert cursor_error.value.code == "catalog.cursor"
    with pytest.raises(CatalogValidationError) as limit_error:
        service.list_entities(limit=101)

    assert limit_error.value.code == "catalog.limit"
