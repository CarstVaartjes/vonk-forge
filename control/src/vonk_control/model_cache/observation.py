"""Durable set observations, independent of a profile request's deadline.

Receipts remain the storage owner. This disposable index checkpoints one object
at a time; slow storage and process death cannot reset the completed prefix.
"""

from __future__ import annotations

import fcntl
import os
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError
from sqlalchemy import or_, select, update
from vonk_agent_protocol import WaitReason, canonical_message

from ..categorized_errors import BookkeepingUnknown
from ..model_cache_contract import (
    ModelCacheObservedObject,
    ModelCacheSetState,
    ModelCacheStorageObservation,
)
from ..models import ModelCacheSet
from ..profile_read_observation import remaining_storage_read_seconds
from .artifacts import ArtifactSetManifest, _unique_artifacts
from .constants import _LOGGER, _MAX_MANIFEST_BYTES, SCHEMA_VERSION
from .errors import ModelCacheError
from .source_helpers import _fsync_directory

if TYPE_CHECKING:
    from .service import ModelCacheService

_REFRESH_SECONDS = 30
_RECHECK_SECONDS = 5


class StorageObserver:
    def __init__(self, cache: ModelCacheService) -> None:
        self._cache = cache
        self._pool = ThreadPoolExecutor(
            max_workers=cache._max_parallel_downloads,
            thread_name_prefix="cache-observer",
        )
        self._lock = threading.Lock()
        self._pending: dict[str, Future[None]] = {}
        self._collected: dict[str, threading.Event] = {}
        self._snapshots: dict[str, ModelCacheStorageObservation] = {}
        self._discovery: Future[None] | None = None
        self._next_discovery: datetime | None = None

    def close(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=True)

    def _path(self, digest: str) -> Path:
        return self._cache.root / "observations" / f"{digest}.json"

    def _read(
        self, manifest: ArtifactSetManifest
    ) -> ModelCacheStorageObservation | None:
        try:
            # One bounded manifest plus its per-object projection. Corruption
            # cannot turn a disposable index into an unbounded allocation.
            maximum_bytes = 3 * _MAX_MANIFEST_BYTES
            with self._path(manifest.digest).open("rb") as handle:
                raw = handle.read(maximum_bytes + 1)
            if len(raw) > maximum_bytes:
                return None
            result = ModelCacheStorageObservation.model_validate_json(raw)
        except (FileNotFoundError, ValidationError):
            return None  # derived, damaged indexes are rebuilt from receipts
        try:
            if (
                ArtifactSetManifest.from_contract(result.manifest).digest
                != manifest.digest
            ):
                return None
        except ModelCacheError:
            return None  # invalid derived identity is rebuilt, never trusted
        specs = _unique_artifacts(manifest.artifacts)
        if any(
            digest not in specs
            or entry.expected_bytes != specs[digest].expected_bytes
            or entry.baseline_bytes > entry.expected_bytes
            for digest, entry in result.objects.items()
        ):
            return None
        if result.completed_at is not None and (
            result.objects.keys() != specs.keys() or result.available_bytes is None
        ):
            return None
        return result

    def _write(
        self, manifest: ArtifactSetManifest, value: ModelCacheStorageObservation
    ) -> None:
        path = self._path(manifest.digest)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("wb") as handle:
                handle.write(canonical_message(value))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            _fsync_directory(path.parent)
            self._publish(manifest, value)
        finally:
            temporary.unlink(missing_ok=True)

    def _publish(
        self, manifest: ArtifactSetManifest, value: ModelCacheStorageObservation
    ) -> None:
        with self._lock:
            self._snapshots[manifest.digest] = value.model_copy(deep=True)
            collected = self._collected.get(manifest.digest)
            if collected is not None:
                collected.set()

    def read(self, manifest: ArtifactSetManifest) -> ModelCacheStorageObservation:
        """Requests use memory; even checkpoint reads on slow NAS run in the observer."""
        with self._lock:
            value = self._snapshots.get(manifest.digest)
        now = self._cache._clock()
        if value is None or now >= value.next_check_at:
            collected = self.schedule(manifest)
            if value is None and collected is not None:
                # Collect an already published checkpoint on the first API
                # read. This joins only the first publication, never the scan.
                # A timed-out collection continues in the same background task.
                collected.wait(remaining_storage_read_seconds())
                with self._lock:
                    value = self._snapshots.get(manifest.digest)
        # Age starts background refresh; it does not erase a measured fact.
        # Retain completed members while refreshing, so any polling interval
        # can collect the result instead of repeatedly landing in an expiry gap.
        if (
            value is not None
            and value.completed_at is not None
            and value.available_bytes is not None
            and value.objects.keys() == _unique_artifacts(manifest.artifacts).keys()
            and value.detail is None
        ):
            return value
        completed = (
            0
            if value is None
            else sum(
                entry.observed_at >= value.started_at
                for entry in value.objects.values()
            )
        )
        total = len(_unique_artifacts(manifest.artifacts))
        next_attempt = (
            now + timedelta(seconds=_RECHECK_SECONDS)
            if value is None
            else max(now + timedelta(seconds=_RECHECK_SECONDS), value.next_check_at)
        )
        detail = (
            ""
            if value is None or value.detail is None
            else f" Last storage error: {value.detail}."
        )
        started = (
            "waiting for the worker"
            if value is None
            else f"started {value.started_at.isoformat()}"
        )
        raise BookkeepingUnknown(
            f"Cache observation {manifest.digest}: {completed}/{total} objects checked ({started}); "
            f"Controller cache observer resumes durable checkpoints; next check {next_attempt.isoformat()}. "
            f"Load proceeds after observation completes; missing objects need download.{detail}",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )

    def schedule(self, manifest: ArtifactSetManifest) -> threading.Event | None:
        with self._lock:
            self._pending = {
                key: task for key, task in self._pending.items() if not task.done()
            }
            self._collected = {
                key: value
                for key, value in self._collected.items()
                if key in self._pending
            }
            if (
                manifest.digest not in self._pending
                and not self._cache._closed.is_set()
            ):
                collected = threading.Event()
                self._collected[manifest.digest] = collected

                def observe() -> None:
                    try:
                        self._observe(manifest)
                    finally:
                        collected.set()

                self._pending[manifest.digest] = self._pool.submit(observe)
            return self._collected.get(manifest.digest)

    def tick(self) -> None:
        """Discover sets without blocking the Controller loop or request workers."""
        with self._lock:
            now = self._cache._clock()
            if (self._next_discovery is None or now >= self._next_discovery) and (
                self._discovery is None or self._discovery.done()
            ):
                self._next_discovery = now + timedelta(seconds=_REFRESH_SECONDS)
                self._discovery = self._pool.submit(self._discover)

    def _discover(self) -> None:
        try:
            with self._cache._session() as session:
                manifests = [
                    manifest
                    for row in session.scalars(select(ModelCacheSet))
                    if (manifest := self._cache._stored_manifest(row)) is not None
                ]
            for manifest in manifests:
                self.schedule(manifest)
        except Exception:  # noqa: BLE001 -- next worker tick retries discovery
            _LOGGER.exception("cache observation discovery failed")

    def _observe(self, manifest: ArtifactSetManifest) -> None:
        root = self._cache.root / "observations"
        try:
            with (root / f"{manifest.digest}.lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    # Atomic checkpoints are readable while another process
                    # observes. API progress must follow the worker's prefix.
                    value = self._read(manifest)
                    if value is not None:
                        self._publish(manifest, value)
                    return
                value = self._read(manifest)
                if value is not None:
                    self._publish(manifest, value)
                now = self._cache._clock()
                if (
                    value is not None
                    and value.completed_at is not None
                    and value.completed_at >= value.started_at
                    and now < value.next_check_at
                ):
                    return
                if value is None:
                    value = ModelCacheStorageObservation(
                        schema_version=SCHEMA_VERSION,
                        manifest=manifest.contract(),
                        started_at=now,
                        next_check_at=now + timedelta(seconds=_RECHECK_SECONDS),
                    )
                    self._write(manifest, value)
                elif (
                    value.completed_at is not None
                    and value.completed_at >= value.started_at
                ):
                    value = value.model_copy(
                        update={
                            "started_at": now,
                            "next_check_at": now + timedelta(seconds=_RECHECK_SECONDS),
                            "detail": None,
                        }
                    )
                    self._write(manifest, value)
                if now < value.next_check_at and value.detail is not None:
                    return
                try:
                    for digest, spec in _unique_artifacts(manifest.artifacts).items():
                        if self._cache._closed.is_set():
                            return
                        previous = value.objects.get(digest)
                        if (
                            previous is not None
                            and previous.observed_at >= value.started_at
                        ):
                            continue
                        available = (
                            self._cache._stored_object(digest, spec.expected_bytes)
                            is not None
                        )
                        baseline = (
                            spec.expected_bytes
                            if available
                            else self._cache._partial_bytes(manifest.digest, spec)
                        )
                        value.objects[digest] = ModelCacheObservedObject(
                            expected_bytes=spec.expected_bytes,
                            available=available,
                            baseline_bytes=baseline,
                            observed_at=self._cache._clock(),
                        )
                        value.next_check_at = self._cache._clock() + timedelta(
                            seconds=_RECHECK_SECONDS
                        )
                        value.detail = None
                        self._write(manifest, value)
                    value.available_bytes = self._cache.free_bytes()
                    value.detail = None
                    value.completed_at = self._cache._clock()
                    value.next_check_at = value.completed_at + timedelta(
                        seconds=_REFRESH_SECONDS
                    )
                    self._write(manifest, value)
                except OSError as error:
                    # Preserve evidence and the last checkpoint's observation
                    # cadence. Discovery keeps checking; errors do not create
                    # a second retry scheduler or extend a work deadline.
                    value.detail = f"{type(error).__name__}: {error}"
                    self._write(manifest, value)
                    return
            # SQL is only a disposable library projection. No transaction spans
            # storage I/O, and a newer publication/transfer keeps its own state.
            verified_bytes = sum(
                entry.expected_bytes
                for entry in value.objects.values()
                if entry.available
            )
            state = (
                ModelCacheSetState.CACHED
                if all(entry.available for entry in value.objects.values())
                else ModelCacheSetState.NEEDS_REPAIR
            )
            with self._cache._session(write=True) as session:
                session.execute(
                    update(ModelCacheSet)
                    .where(
                        ModelCacheSet.artifact_set_sha256 == manifest.digest,
                        ModelCacheSet.state.in_(
                            [
                                ModelCacheSetState.CACHED,
                                ModelCacheSetState.INCOMPLETE,
                                ModelCacheSetState.NEEDS_REPAIR,
                            ]
                        ),
                        ModelCacheSet.updated_at <= value.started_at,
                        or_(
                            ModelCacheSet.state != state,
                            ModelCacheSet.verified_bytes != verified_bytes,
                        ),
                    )
                    .values(
                        state=state,
                        verified_bytes=verified_bytes,
                        updated_at=value.completed_at,
                    )
                )

        except OSError as error:
            # A NAS outage may prevent reading/writing the diagnostic itself.
            # Keep that cause visible in memory without discarding the durable
            # prefix on storage. Ordinary discovery resumes it after recovery.
            with self._lock:
                previous = self._snapshots.get(manifest.digest)
            now = self._cache._clock()
            value = ModelCacheStorageObservation(
                schema_version=SCHEMA_VERSION,
                manifest=manifest.contract(),
                objects={} if previous is None else previous.objects,
                started_at=now if previous is None else previous.started_at,
                completed_at=None if previous is None else previous.completed_at,
                available_bytes=None if previous is None else previous.available_bytes,
                next_check_at=now + timedelta(seconds=_RECHECK_SECONDS),
                detail=f"{type(error).__name__}: {error}",
            )
            self._publish(manifest, value)
            _LOGGER.exception(
                "cache observation %s storage unavailable", manifest.digest
            )
        except Exception:  # noqa: BLE001 -- preserve unexpected failures in server evidence
            _LOGGER.exception("cache observation %s failed", manifest.digest)
