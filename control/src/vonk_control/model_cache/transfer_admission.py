"""The one place a download or repair operation is admitted."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from ..artifact_lifecycle import ArtifactIdentity, lock_reference_gates
from ..lifecycle.model_cache import ModelCacheAdapter
from ..models import ModelCacheOperation
from .views import CacheOperationView

if TYPE_CHECKING:
    from .service import ModelCacheService


def admit_transfer(
    cache: ModelCacheService,
    session: Session,
    *,
    artifact_set_sha256: str,
    now: datetime,
    force: bool,
    attach: bool = True,
    excluding: str | None = None,
    prepare: Callable[[], object] | None = None,
    **fields: object,
) -> CacheOperationView | ModelCacheOperation:
    """Return the live transfer of this set, or a new operation row to add.

    Under the model-set gate, a live download or repair of the same set with the
    same refresh intent is returned so the caller attaches to it: one live
    transfer per set, whatever the request key.  ``attach=False`` is for a
    caller whose receipt is read by its own key.  ``prepare`` runs under the
    gate once nothing is shared, before the new row is built.
    """
    lock_reference_gates(
        session, (ArtifactIdentity("model-set", artifact_set_sha256),), now=now
    )
    if attach:
        shared = cache.reusable_operation(
            artifact_set_sha256=artifact_set_sha256,
            include_succeeded=False,
            excluding=excluding,
            force_refresh=force,
        )
        if shared is not None:
            return shared
    if prepare is not None:
        prepare()
    return ModelCacheAdapter.new_operation(
        artifact_set_sha256=artifact_set_sha256,
        created_at=now,
        updated_at=now,
        **fields,  # type: ignore[arg-type]
    )


def refreshes(operation: ModelCacheOperation, payload: object) -> bool:
    """Whether the operation re-downloads the whole set (a repair always does)."""
    return operation.kind == "repair" or bool(getattr(payload, "force_refresh", False))
