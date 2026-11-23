"""The artifact specs one model content identity derives, built once.

A model document is immutable content and its content digest is the digest of
exactly the stored document, so the digest is the content identity. The specs
derived from it depend on nothing else but whether the service accepts fixture
sources (``fixture_sources`` changes which providers are trusted), so they are
kept per ``(digest, fixture_sources)``. Entries are tuples of frozen specs;
nothing a caller holds can change them, and no parse object is bound or
retained. Failures are never remembered. The bound counts retained specs, like
the document cache bounds retained bytes.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from threading import Lock

from ..models.catalog import CatalogDocumentRevision
from .artifacts import ArtifactSpec

_MAX_RETAINED_SPECS = 100_000
_cache: OrderedDict[tuple[str, bool], tuple[ArtifactSpec, ...]] = OrderedDict()
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
    key = (row.content_digest, fixture_sources)
    with _lock:
        cached = _cache.get(key)
        if cached is not None:
            _cache.move_to_end(key)
            return cached
    specs = build(row)
    with _lock:
        previous = _cache.pop(key, None)
        if previous is not None:
            _retained -= len(previous)
        if len(specs) <= _MAX_RETAINED_SPECS:
            while _retained + len(specs) > _MAX_RETAINED_SPECS:
                _retained -= len(_cache.popitem(last=False)[1])
            _cache[key] = specs
            _retained += len(specs)
    return specs
