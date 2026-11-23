"""The model objects live operations keep on a Spark: one definition for the
retention request sent to the agent and for the Controller's storage accounting.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet

from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from vonk_forge_contracts import ModelDefinition

from ..catalog.revision_contract import read_catalog_document
from ..model_cache_contract import CacheManifest
from ..models import CatalogDocumentRevision, ModelCacheSet
from ..strict_json import read_stored_model


def protected_model_objects(session: Session, references: AbstractSet[str]) -> set[str]:
    """Raw digests, files of cache sets named by a reference, and files of the
    named model definitions with their transitive dependencies.

    Raises ValueError when a required definition is unavailable.
    """
    objects = set(references)
    for document in session.scalars(
        select(ModelCacheSet.manifest).where(
            or_(
                ModelCacheSet.artifact_set_sha256.in_(references),
                ModelCacheSet.model_content_sha256.in_(references),
            )
        )
    ):
        manifest = read_stored_model(CacheManifest, document, from_json=True)
        objects.update(artifact.sha256 for artifact in manifest.artifacts)
    # A reusable cache set keeps its first requester's provenance. A pinned
    # editorial successor can name the same files under a different model ID;
    # the accepted model definitions own those file identities, not that index.
    pending = set(references)
    visited: set[str] = set()
    required_dependencies: set[str] = set()
    # This walk ends with the finite set of persisted definitions; cycles
    # cannot revisit a content identity. It never waits or retries a missing row.
    while pending:
        batch, pending = pending - visited, set()
        unresolved = required_dependencies & batch
        for revision in session.scalars(
            select(CatalogDocumentRevision)
            .where(
                CatalogDocumentRevision.kind == "model",
                CatalogDocumentRevision.content_digest.in_(batch),
            )
            .execution_options(yield_per=1)
        ):
            digest = revision.content_digest
            if digest is None:
                raise ValueError("model reference has no content identity")
            unresolved.discard(digest)
            visited.add(digest)
            # The row's digest was verified at ingress; it is read, never re-hashed.
            definition = read_catalog_document(revision)
            assert isinstance(definition, ModelDefinition)  # kind "model" above
            objects.update(file.sha256 for file in definition.files)
            dependencies = {item.content_sha256 for item in definition.dependencies}
            required_dependencies.update(dependencies)
            pending.update(dependencies - visited)
        if unresolved:
            raise ValueError("model dependency definition is unavailable")
    return objects
