"""Which cached sets belong to a model: the Library's attribution, shared.

The Library shows a model's cached bytes by its publisher/slug lineage: a set
belongs to every model whose catalog content it is bound to, through its first
requester or a verified binding published later. A refreshed model therefore
keeps the bytes it was first downloaded under. Removal reads this same rule,
so it removes the sets the listing shows, never a narrower exact-digest view.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable

from sqlalchemy import (
    Exists,
    case,
    func,
    literal,
    literal_column,
    or_,
    select,
)
from sqlalchemy.orm import Session, load_only
from vonk_agent_protocol.agent_words import RunSwitchReasonScopeName
from vonk_forge_contracts.model import ModelReference

from ..model_cache_contract import CacheManifest
from ..models import CatalogDocumentRevision, ModelCacheSet
from ..stored_json import read_row_column


def set_bindings(
    model_content_sha256: str | None, verified_models: Iterable[ModelReference]
) -> frozenset[str]:
    """Every model content a set serves: its first requester and verified bindings."""

    bound = {reference.content_sha256 for reference in verified_models}
    if model_content_sha256 is not None:
        bound.add(model_content_sha256)
    return frozenset(bound)


def verifies_model(session: Session, digests: Collection[str]) -> Exists:
    """Whether a set's manifest lists one of ``digests`` among its verified models.

    Each set's reference list is expanded once and matched by equality, never
    joined against every requested digest, so the cost grows with the sets plus
    the requested digests. A prefilter only: :func:`set_bindings` over the
    shared manifest reader decides exact membership, and a damaged list never
    fails the read.
    """

    verified = ModelCacheSet.manifest["verified_models"]
    if session.get_bind().dialect.name == "postgresql":
        array = case(
            (func.json_typeof(verified) == "array", verified),
            else_=literal_column("'[]'::json"),
        )
        reference = func.json_array_elements(array).table_valued("value")
        content = reference.c.value.op("->>")("content_sha256")
    else:
        reference = func.json_each(
            ModelCacheSet.manifest, "$.verified_models"
        ).table_valued("value", "type")
        content = case(
            (
                reference.c.type == "object",
                func.json_extract(reference.c.value, "$.content_sha256"),
            )
        )
    return (
        select(literal(1))
        .select_from(reference)
        .where(content.in_(tuple(digests)))
        .exists()
    )


def lineage_contents(session: Session, digest: str) -> frozenset[str]:
    """Every catalog content of the model's publisher/slug lineage, itself included."""

    lineage = session.execute(
        select(CatalogDocumentRevision.publisher, CatalogDocumentRevision.slug)
        .where(
            CatalogDocumentRevision.kind == RunSwitchReasonScopeName.MODEL,
            CatalogDocumentRevision.content_digest == digest,
        )
        .limit(1)
    ).first()
    if lineage is None:
        return frozenset({digest})
    publisher, slug = lineage
    return frozenset(
        {
            digest,
            *session.scalars(
                select(CatalogDocumentRevision.content_digest).where(
                    CatalogDocumentRevision.kind == RunSwitchReasonScopeName.MODEL,
                    CatalogDocumentRevision.publisher == publisher,
                    CatalogDocumentRevision.slug == slug,
                )
            ),
        }
    )


def model_sets(session: Session, contents: Collection[str]) -> tuple[str, ...]:
    """The cached sets bound to any of ``contents``, in digest order.

    A set's binding is its first requester's content or a verified binding in
    its stored manifest; a manifest that does not read binds only through its
    first requester.
    """

    if not contents:
        return ()
    found: set[str] = set()
    for row in session.scalars(
        select(ModelCacheSet)
        .options(
            load_only(
                ModelCacheSet.artifact_set_sha256,
                ModelCacheSet.model_content_sha256,
                ModelCacheSet.manifest,
            )
        )
        .where(
            or_(
                ModelCacheSet.model_content_sha256.in_(tuple(contents)),
                verifies_model(session, contents),
            )
        )
    ):
        manifest = read_row_column(row, "manifest")
        verified = (
            manifest.verified_models if isinstance(manifest, CacheManifest) else ()
        )
        if set_bindings(row.model_content_sha256, verified) & set(contents):
            found.add(row.artifact_set_sha256)
    return tuple(sorted(found))
