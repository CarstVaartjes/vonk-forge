"""The artifact specs one model content identity derives, built once.

A model document is immutable content, and ``catalog_revision_contract`` already
shares one parse per ``(kind, content digest)``. The specs derived from that
parse depend on nothing else but whether the service accepts fixture sources
(``fixture_sources`` changes which providers are trusted), so they are kept per
``(digest, fixture_sources)`` and bound to the exact shared parse they came
from: a hit needs the same parse object, so a replaced or evicted document can
never serve specs derived from other content. Entries are tuples of frozen
specs; nothing a caller holds can change them. Failures are never remembered.
The bound counts retained specs, like the document cache bounds retained bytes.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock

from vonk_forge_contracts import ModelDefinition, RecipeDefinition

from ..catalog_revision_contract import shared_catalog_document
from ..models import CatalogDocumentRevision
from .artifacts import ArtifactSpec

_MAX_RETAINED_SPECS = 100_000


@dataclass(frozen=True)
class _Entry:
    parsed: ModelDefinition | RecipeDefinition
    specs: tuple[ArtifactSpec, ...]


_cache: OrderedDict[tuple[str, bool], _Entry] = OrderedDict()
_retained = 0
_lock = Lock()


def model_artifact_specs(
    row: CatalogDocumentRevision,
    *,
    fixture_sources: bool,
    build: Callable[[CatalogDocumentRevision], tuple[ArtifactSpec, ...]],
) -> tuple[ArtifactSpec, ...]:
    """The specs of ``row``'s exact model content; ``build`` runs only on a miss."""

    global _retained
    parsed = shared_catalog_document(row.kind, row.content_digest, row.document)
    key = (row.content_digest, fixture_sources)
    with _lock:
        entry = _cache.get(key)
        if entry is not None and entry.parsed is parsed:
            _cache.move_to_end(key)
            return entry.specs
    specs = build(row)
    with _lock:
        previous = _cache.pop(key, None)
        if previous is not None:
            _retained -= len(previous.specs)
        if len(specs) <= _MAX_RETAINED_SPECS:
            while _retained + len(specs) > _MAX_RETAINED_SPECS:
                _retained -= len(_cache.popitem(last=False)[1].specs)
            _cache[key] = _Entry(parsed, specs)
            _retained += len(specs)
    return specs
