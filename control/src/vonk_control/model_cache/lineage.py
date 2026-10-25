"""Which cached sets belong to a model: the Library's attribution, shared.

The Library shows a model's cached bytes by its publisher/slug lineage: a set
belongs to every model whose catalog content it is bound to, through its first
requester or a verified binding published later. A refreshed model therefore
keeps the bytes it was first downloaded under. Removal reads this same rule,
so it removes the sets the listing shows, never a narrower exact-digest view.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import NamedTuple

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


class ModelSets(NamedTuple):
    """The cached sets bound to a model, split by who else they serve."""

    #: Bound to this model's contents and to no other catalog model: removal
    #: removes them.
    owned: tuple[str, ...]
    #: Also bound to another catalog model: removal retains them, and says so.
    shared: tuple[str, ...]


def model_sets(session: Session, contents: Collection[str]) -> ModelSets:
    """The cached sets bound to any of ``contents``, in digest order.

    A set's binding is its first requester's content or a verified binding in
    its stored manifest; a manifest that does not read binds only through its
    first requester. A binding the catalog does not know (a collected
    revision) belongs to nobody else, so it never makes a set shared.
    """

    if not contents:
        return ModelSets((), ())
    wanted = set(contents)
    bound: dict[str, frozenset[str]] = {}
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
                ModelCacheSet.model_content_sha256.in_(tuple(wanted)),
                verifies_model(session, wanted),
            )
        )
    ):
        manifest = read_row_column(row, "manifest")
        verified = (
            manifest.verified_models if isinstance(manifest, CacheManifest) else ()
        )
        bindings = set_bindings(row.model_content_sha256, verified)
        if bindings & wanted:
            bound[row.artifact_set_sha256] = bindings
    others = {content for bindings in bound.values() for content in bindings} - wanted
    catalogued = (
        set(
            session.scalars(
                select(CatalogDocumentRevision.content_digest).where(
                    CatalogDocumentRevision.kind == RunSwitchReasonScopeName.MODEL,
                    CatalogDocumentRevision.content_digest.in_(tuple(others)),
                )
            )
        )
        if others
        else set()
    )
    return ModelSets(
        owned=tuple(sorted(d for d, b in bound.items() if not b & catalogued)),
        shared=tuple(sorted(d for d, b in bound.items() if b & catalogued)),
    )


def first_requested_sets(session: Session, digest: str) -> tuple[str, ...]:
    """The sets first downloaded under exactly ``digest``: retention's unit.

    Retention measures, protects and evicts per first requester; it removes
    exactly what it measured, never the model's wider Library attribution.
    """

    return tuple(
        session.scalars(
            select(ModelCacheSet.artifact_set_sha256)
            .where(ModelCacheSet.model_content_sha256 == digest)
            .order_by(ModelCacheSet.artifact_set_sha256)
        )
    )
