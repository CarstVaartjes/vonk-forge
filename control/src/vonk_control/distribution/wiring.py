"""Distribution: wiring."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from .filesystem import RecipeBuildObjectSource
from .model_cache import ModelCacheObjectSource
from .service import DistributionService
from .sources import CompositeObjectSource
from .types import ObjectSource


def build_distribution_service(
    model_source: ObjectSource,
    oci_source: ObjectSource,
    sessions: sessionmaker[Session],
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> DistributionService:
    """Production construction hook used by Controller startup wiring."""
    return DistributionService(
        CompositeObjectSource(model_source, oci_source),
        clock=clock,
        sessions=sessions,
    )


def build_distribution_service_from_components(
    model_cache: object,
    sessions: sessionmaker[Session],
    artifact_root: Path,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> DistributionService:
    """Build the production source pair from Controller startup components."""
    return build_distribution_service(
        ModelCacheObjectSource.from_service(model_cache),
        RecipeBuildObjectSource(sessions, artifact_root),
        sessions,
        clock=clock,
    )
