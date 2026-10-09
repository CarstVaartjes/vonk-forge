"""Service."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import httpx2
from sqlalchemy.orm import Session, sessionmaker

from ..categorized_errors import InvalidValue
from ..lifecycle import Reconciler
from ..lifecycle.model_cache import ModelCacheAdapter, ModelCacheStore
from ..model_cache_streams import StreamGovernor
from ..storage_demands import StorageDemands
from .availability import AvailabilityMixin
from .cancellation import CancellationMixin
from .catalog import CatalogMixin
from .checkpoints import CheckpointsMixin
from .constants import (
    _DEFAULT_MAX_DOWNLOAD_STREAMS,
    _DEFAULT_MAX_PARALLEL_DOWNLOADS,
    _MAX_PARALLEL_DOWNLOADS,
    _UPSTREAM_CHECK_WORKERS,
)
from .core import CoreMixin
from .download import DownloadMixin
from .download_admission import DownloadAdmissionMixin
from .failure import FailureMixin
from .github import GithubMixin
from .http import HttpMixin
from .inventory import InventoryMixin
from .operations import OperationsMixin
from .persistence import _read_operation_payload, _store_operation_payload
from .removal_acceptance import RemovalAcceptanceMixin
from .removal_execution import RemovalExecutionMixin
from .removal_reconcile import RemovalReconcileMixin
from .removal_review import RemovalReviewMixin
from .removal_scope import RemovalScopeMixin
from .repair import RepairMixin
from .resolution import ResolutionMixin
from .retry import RetryMixin
from .scheduler import SchedulerMixin
from .split_transfer import SplitTransferMixin
from .storage import StorageMixin
from .transfer import TransferMixin
from .updates import UpdatesMixin
from .views import _BackgroundTransfer


class ModelCacheService(
    CoreMixin,
    ResolutionMixin,
    RemovalReviewMixin,
    RemovalScopeMixin,
    RemovalAcceptanceMixin,
    RemovalReconcileMixin,
    RemovalExecutionMixin,
    CatalogMixin,
    DownloadAdmissionMixin,
    TransferMixin,
    SplitTransferMixin,
    DownloadMixin,
    GithubMixin,
    HttpMixin,
    CheckpointsMixin,
    FailureMixin,
    RetryMixin,
    CancellationMixin,
    OperationsMixin,
    SchedulerMixin,
    RepairMixin,
    AvailabilityMixin,
    InventoryMixin,
    UpdatesMixin,
    StorageMixin,
):
    """Resolve, download, verify, repair and remove NAS model artifacts."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        root: Path,
        *,
        reserve_bytes: int = 10 * 1024**3,
        max_parallel_downloads: int = _DEFAULT_MAX_PARALLEL_DOWNLOADS,
        max_download_streams: int = _DEFAULT_MAX_DOWNLOAD_STREAMS,
        clock: Callable[[], datetime] | None = None,
        http_client: httpx2.Client | None = None,
        fixture_sources: bool = False,
        trusted_source_hosts: Sequence[str] = ("huggingface.co",),
        huggingface_token_path: Path | None = None,
        runtime_archive_available: Callable[[str, int], bool] | None = None,
    ) -> None:
        if not isinstance(root, Path):
            root = Path(root)
        if not root.is_absolute() or any(
            part in {"", ".", ".."} for part in root.parts
        ):
            raise InvalidValue("model cache root must be an absolute normalized path")
        if root.is_symlink():
            raise InvalidValue("model cache root must not be a symlink")
        if (
            not isinstance(reserve_bytes, int)
            or isinstance(reserve_bytes, bool)
            or reserve_bytes < 0
        ):
            raise InvalidValue("model cache reserve must be a non-negative integer")
        if (
            not isinstance(max_parallel_downloads, int)
            or isinstance(max_parallel_downloads, bool)
            or not 1 <= max_parallel_downloads <= _MAX_PARALLEL_DOWNLOADS
        ):
            raise InvalidValue(
                "model cache parallel downloads must be between 1 and 32"
            )
        if (
            not isinstance(max_download_streams, int)
            or isinstance(max_download_streams, bool)
            or not 1 <= max_download_streams <= _MAX_PARALLEL_DOWNLOADS
        ):
            raise InvalidValue("model cache download streams must be between 1 and 32")
        root.mkdir(parents=True, exist_ok=True, mode=0o750)
        for child in ("objects", "partials", "quarantine", "manifests", "locks"):
            directory = root / child
            if directory.is_symlink():
                raise InvalidValue(
                    "model cache storage directory must not be a symlink"
                )
            directory.mkdir(mode=0o750, exist_ok=True)
        self._sessions = sessions
        self._root = root
        self._reserve_bytes = reserve_bytes
        self._storage_demands: StorageDemands | None = None
        self._max_parallel_downloads = max_parallel_downloads
        self._streams = StreamGovernor(max_download_streams)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._removal_gate_after: tuple[str, str] | None = None
        self._removal_request_after: str | None = None
        self._http = http_client
        # Local file and caller-supplied HTTP sources are useful for isolated
        # fixture tests, but are never enabled by the production constructor.
        # Production manifests are resolved from trusted catalog rows only.
        self._fixture_sources = fixture_sources
        self._trusted_source_hosts = frozenset(
            host.strip().lower().rstrip(".")
            for host in trusted_source_hosts
            if isinstance(host, str) and host.strip()
        )
        self._huggingface_token_path = (
            Path(huggingface_token_path) if huggingface_token_path is not None else None
        )
        self._runtime_archive_available = runtime_archive_available
        self._lock = threading.RLock()
        self._closed = threading.Event()
        self._claim_owner = uuid.uuid4().hex
        self._executor = ThreadPoolExecutor(
            max_workers=max_parallel_downloads,
            thread_name_prefix="vonk-model-cache",
        )
        self._upstream_executor = ThreadPoolExecutor(
            max_workers=_UPSTREAM_CHECK_WORKERS, thread_name_prefix="vonk-model-updates"
        )
        self._upstream_slots = threading.BoundedSemaphore(_UPSTREAM_CHECK_WORKERS)
        self._background_operations: dict[str, _BackgroundTransfer] = {}
        self._active_digests: set[str] = set()
        self._hf_cooldown_until: datetime | None = None
        self._observed_credential_fingerprint: str | None = None
        self._progress_checkpoint_at: dict[str, datetime] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._range_reserved_bytes = 0
        self._reverified_at: dict[str, datetime] = {}
        # The lifecycle core decides every recovery outcome of an operation; this
        # adapter is the only writer of its stored ``state`` (see the module
        # docstring of ``lifecycle/model_cache.py``).
        self._lifecycle = ModelCacheAdapter(
            self._session,
            clock=lambda: self._clock(),
            effects=self,
            read_payload=_read_operation_payload,
            store_payload=lambda operation, payload: _store_operation_payload(
                operation, operation.kind, payload
            ),
            on_end=self._project_end,
        )
        self._reconciler = Reconciler(
            ModelCacheStore(self._session, self._lifecycle, self),
            {kind: self._lifecycle for kind in ("download", "repair", "remove")},
            clock=lambda: self._clock(),
            enabled=True,
        )
