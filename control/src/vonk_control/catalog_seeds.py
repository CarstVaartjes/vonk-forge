from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import TokenCodec
from .catalog_contract import catalog_content_sha256, parse_catalog_json
from .catalog_entities import CatalogConflict, CatalogEntityService
from .harnesses import BUILTIN_HARNESS_SLUGS
from .models import CatalogEntity, CatalogEntityRevision


@dataclass(frozen=True, slots=True)
class SeedResult:
    created: int
    identifiers: tuple[str, ...]


def seed_builtin_harnesses(session: Session, now: datetime) -> SeedResult:
    if now.tzinfo is None:
        raise ValueError("catalog seed timestamp must be timezone-aware")
    service = CatalogEntityService(
        session,
        clock=lambda: now,
        cursors=TokenCodec(b"\x00" * 32).cursor_codec(),
    )
    created: list[str] = []
    for slug, document in _builtin_harness_documents():
        digest = catalog_content_sha256(document)
        if _canonical_builtin_revision_is_current(session, slug, digest):
            continue
        entity = session.scalar(
            select(CatalogEntity).where(
                CatalogEntity.kind == "execution-harness",
                CatalogEntity.publisher == "vonk-forge",
                CatalogEntity.slug == slug,
            )
        )
        try:
            with session.begin_nested():
                if entity is None:
                    draft = service.create_draft(document, actor="system")
                else:
                    latest = session.scalar(
                        select(CatalogEntityRevision)
                        .where(CatalogEntityRevision.entity_id == entity.id)
                        .order_by(CatalogEntityRevision.revision_number.desc())
                        .limit(1)
                    )
                    if latest is None:
                        raise RuntimeError(
                            "built-in catalog entity has no immutable revision"
                        )
                    draft = service.revise(
                        entity.id,
                        document,
                        actor="system",
                        expected_revision=latest.revision_number,
                    )
                service.resolve(
                    draft.id,
                    actor="system",
                    expected_revision=draft.revision_number,
                )
        except CatalogConflict:
            if _canonical_builtin_revision_is_current(session, slug, digest):
                continue
            raise
        created.append(slug)
    return SeedResult(created=len(created), identifiers=tuple(created))


def _canonical_builtin_revision_is_current(
    session: Session, slug: str, digest: str
) -> bool:
    entity = session.scalar(
        select(CatalogEntity).where(
            CatalogEntity.kind == "execution-harness",
            CatalogEntity.publisher == "vonk-forge",
            CatalogEntity.slug == slug,
        )
    )
    if entity is None:
        return False
    latest = session.scalar(
        select(CatalogEntityRevision)
        .where(CatalogEntityRevision.entity_id == entity.id)
        .order_by(CatalogEntityRevision.revision_number.desc())
        .limit(1)
    )
    return (
        latest is not None
        and latest.lifecycle == "resolved"
        and latest.content_sha256 == digest
    )


def _builtin_harness_documents() -> tuple[tuple[str, dict[str, object]], ...]:
    packaged = files("vonk_control").joinpath("execution-harnesses")
    root = Path(__file__).resolve().parents[3] / "config" / "execution-harnesses"
    documents: list[tuple[str, dict[str, object]]] = []
    for slug in BUILTIN_HARNESS_SLUGS:
        resource = packaged.joinpath(f"{slug}.json")
        payload = (
            resource.read_bytes()
            if resource.is_file()
            else (root / f"{slug}.json").read_bytes()
        )
        documents.append((slug, dict(parse_catalog_json(payload))))
    return tuple(documents)
