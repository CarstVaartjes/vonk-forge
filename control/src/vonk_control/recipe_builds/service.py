"""Recipe builds: service concerns."""

from __future__ import annotations

import time
from collections.abc import Callable

from sqlalchemy.orm import Session, sessionmaker

from ..inventory_repository import InventoryRepository
from ..models import RecipeBuild
from ..recipe_build_receipts import PreparedBuildLookup
from ..source_bundles import SourceBundleStoreProtocol
from ..storage_demands import StorageDemands
from .common import (
    SourceBundleRederiver,
    _valid_succeeded_receipt,
    rederive_source_bundle_from_closure,
)
from .persistence import (
    _reserve_in_session,
    persist_plan_in_session,
    record_success,
    reserve_in_session,
)
from .planning import (
    _admit_spark_build,
    _prepare_plan_once,
    _usable_prebuilt,
    plan,
    prepare_plan,
    reusable_build_id,
)
from .source import (
    _check_source_once,
    _heal_source_bundle,
    _resolve_once,
    _verified_bundle,
    check_source,
    resolve,
)


class RecipeBuildService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        bundles: SourceBundleStoreProtocol,
        inventory_max_age: int = 300,
        build_archive_available: Callable[[str, int], bool] | None = None,
        prepared_builds: PreparedBuildLookup | None = None,
        source_rederiver: SourceBundleRederiver = rederive_source_bundle_from_closure,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._sessions = sessions
        self._sleep = sleep
        self._bundles = bundles
        self._source_rederiver = source_rederiver
        self._inventory = InventoryRepository(sessions)
        self._inventory_max_age = inventory_max_age
        self._build_archive_available = build_archive_available
        self._prepared_builds = prepared_builds
        self._storage_demands: StorageDemands | None = None

    def bind_storage_demands(self, demands: StorageDemands) -> None:
        """Attach the register a build refused for lack of disk asks space in."""

        self._storage_demands = demands

    def _stored_archive_present(self, archive_sha256: str, image_bytes: int) -> bool:
        """Cheap presence/type/length check usable inside a short transaction."""

        if self._build_archive_available is None:
            return True
        return self._build_archive_available(archive_sha256, image_bytes)

    def _succeeded_build_available(self, build: RecipeBuild) -> bool:
        if not _valid_succeeded_receipt(build):
            return False
        assert build.oci_layout_sha256 is not None
        assert build.image_bytes is not None
        return self._stored_archive_present(build.oci_layout_sha256, build.image_bytes)

    _verified_bundle = _verified_bundle
    _heal_source_bundle = _heal_source_bundle
    check_source = check_source
    _check_source_once = _check_source_once
    resolve = resolve
    _resolve_once = _resolve_once
    _admit_spark_build = _admit_spark_build
    _usable_prebuilt = _usable_prebuilt
    prepare_plan = prepare_plan
    _prepare_plan_once = _prepare_plan_once
    plan = plan
    reusable_build_id = reusable_build_id
    persist_plan_in_session = persist_plan_in_session
    record_success = record_success
    reserve_in_session = reserve_in_session
    _reserve_in_session = _reserve_in_session
