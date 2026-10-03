"""Import the synthetic canary package into a running acceptance Controller.

The Spark lifecycle harness runs this program inside the ``control-api``
container as the Controller user.  Production Controllers only read signed
recipe releases, so the canary's producer fixture cannot arrive through the
network reader.  Instead the exact index and package bytes arrive on stdin and
are applied through the same managed catalog sync, package decoder and catalog
import the release reader feeds.  The verified archive is kept in the
Controller's digest-addressed package cache so the recorded package handle
points at durable state.

Input (stdin, JSON): ``request_key``, ``index`` (UTF-8 text) and ``packages``
(base64, one per Recipe of the index).  Output (stdout, JSON): the managed
catalog sync view.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import os
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vonk_control.auth import TokenCodec
from vonk_control.catalog_service import CatalogService
from vonk_control.catalog_sync import CatalogSyncError, ManagedRecipeCatalogSyncService
from vonk_control.db import build_engine, session_factory
from vonk_control.recipe_library_types import RecipeLibraryItem, RecipeLibrarySnapshot
from vonk_control.recipe_packages import load_recipe_package
from vonk_control.settings import Settings
from vonk_control.source_bundles import DatabaseSourceBundleStore

SYNC_ACTOR = "system:spark-lifecycle-canary"
# The Controller's own automatic sync may hold the single sync slot.
SYNC_SLOT_WAIT_SECONDS = 600


def _cache_package(cache_root: Path, archive: bytes, digest: str) -> Path:
    """Store the archive where the Controller's package reader caches it."""
    target = cache_root / digest[:2] / f"{digest}.tar.gz"
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{digest}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(archive)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


class FixtureReader:
    """The snapshot of the producer's canary index: one Recipe, or siblings."""

    def __init__(self, index: dict[str, Any], archives: dict[str, Path]) -> None:
        commit = str(index["source_commit"])
        self._archives: dict[str, tuple[str, Path]] = {}
        items = []
        for entry in index["recipes"]:
            document = entry["document"]
            identity, metadata = document["identity"], document["metadata"]
            digest = str(entry["content_sha256"])
            item = RecipeLibraryItem(
                library_commit=commit,
                source_path=str(entry["source_path"]),
                publisher=str(identity["publisher"]),
                slug=str(identity["slug"]),
                title=str(metadata["title"]),
                description=str(metadata["description"]),
                tags=tuple(str(tag) for tag in metadata.get("tags", [])),
                content_sha256=digest,
                uri=f"vonk://catalog/{identity['publisher']}/{identity['slug']}@sha256:{digest}",
                document=dict(document),
            )
            self._archives[item.uri] = (
                str(entry["package"]["sha256"]),
                archives[str(entry["package"]["sha256"])],
            )
            items.append(item)
        self._items = {item.uri: item for item in items}
        self.snapshot = RecipeLibrarySnapshot(
            commit=commit,
            items=tuple(items),
            repository=str(index["repository"]),
            catalog_entities=tuple(
                dict(value["document"]) for value in index["catalog_entities"]
            ),
        )

    def list(self) -> RecipeLibrarySnapshot:
        return self.snapshot

    def fetch(self, uri: str) -> RecipeLibraryItem:
        item = self._items.get(uri)
        if item is None:
            raise KeyError(uri)
        package_sha256, archive_path = self._archives[uri]
        return load_recipe_package(
            archive_path,
            package_sha256=package_sha256,
            publisher=item.publisher,
            slug=item.slug,
            recipe_content_sha256=item.content_sha256,
            library_commit=item.library_commit,
            source_path=item.source_path,
        )


def main() -> None:
    payload = json.loads(sys.stdin.read())
    index = json.loads(payload["index"])
    archives = [base64.b64decode(value, validate=True) for value in payload["packages"]]
    expected = sorted(str(entry["package"]["sha256"]) for entry in index["recipes"])
    if sorted(hashlib.sha256(archive).hexdigest() for archive in archives) != expected:
        raise SystemExit("canary package bytes do not match the index")
    settings = Settings.from_env_and_secrets()
    archive_paths = {
        hashlib.sha256(archive).hexdigest(): _cache_package(
            settings.state_path / "recipe-library-packages",
            archive,
            hashlib.sha256(archive).hexdigest(),
        )
        for archive in archives
    }
    reader = FixtureReader(index, archive_paths)
    sessions = session_factory(build_engine(settings.database_url))

    def clock() -> datetime:
        return datetime.now(UTC)

    catalog = CatalogService(
        sessions,
        clock=clock,
        cursors=TokenCodec(settings.token_signing_key).cursor_codec(),
        source_bundles=DatabaseSourceBundleStore(sessions),
    )
    sync = ManagedRecipeCatalogSyncService(
        sessions, catalog=catalog, reader=reader, clock=clock
    )
    deadline = time.monotonic() + SYNC_SLOT_WAIT_SECONDS
    while True:
        try:
            view = sync.sync(
                request_key=str(payload["request_key"]),
                trigger="manual",
                actor=SYNC_ACTOR,
                expected_commit=reader.snapshot.commit,
            )
            break
        except CatalogSyncError as error:
            if error.code != "catalog.sync_in_progress" or (
                time.monotonic() > deadline
            ):
                raise
            time.sleep(5)
    print(json.dumps(dataclasses.asdict(view), default=str, sort_keys=True))


if __name__ == "__main__":
    main()
