"""Import the synthetic canary package into a running acceptance Controller.

The Spark lifecycle harness runs this program inside the ``control-api``
container as the Controller user.  Production Controllers only read signed
recipe releases, so the canary's producer fixture cannot arrive through the
network reader.  Instead the exact index and package bytes arrive on stdin and
are applied through the same managed catalog sync, package decoder and catalog
import the release reader feeds.  The verified archive is kept in the
Controller's digest-addressed package cache so the recorded package handle
points at durable state.

Input (stdin, JSON): ``request_key``, ``index`` (UTF-8 text) and ``package``
(base64).  Output (stdout, JSON): the managed catalog sync view.
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
    """The one-recipe snapshot of the producer's canary index."""

    def __init__(self, index: dict[str, Any], archive_path: Path) -> None:
        recipes = index["recipes"]
        if len(recipes) != 1:
            raise ValueError("canary index must contain exactly one recipe")
        entry = recipes[0]
        document = entry["document"]
        identity, metadata = document["identity"], document["metadata"]
        commit = str(index["source_commit"])
        digest = str(entry["content_sha256"])
        self._package_sha256 = str(entry["package"]["sha256"])
        self._archive_path = archive_path
        self._item = RecipeLibraryItem(
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
        self.snapshot = RecipeLibrarySnapshot(
            commit=commit,
            items=(self._item,),
            repository=str(index["repository"]),
            catalog_entities=tuple(
                dict(value["document"]) for value in index["catalog_entities"]
            ),
        )

    def list(self) -> RecipeLibrarySnapshot:
        return self.snapshot

    def fetch(self, uri: str) -> RecipeLibraryItem:
        if uri != self._item.uri:
            raise KeyError(uri)
        return load_recipe_package(
            self._archive_path,
            package_sha256=self._package_sha256,
            publisher=self._item.publisher,
            slug=self._item.slug,
            recipe_content_sha256=self._item.content_sha256,
            library_commit=self._item.library_commit,
            source_path=self._item.source_path,
        )


def main() -> None:
    payload = json.loads(sys.stdin.read())
    index = json.loads(payload["index"])
    archive = base64.b64decode(payload["package"], validate=True)
    package_sha256 = str(index["recipes"][0]["package"]["sha256"])
    if hashlib.sha256(archive).hexdigest() != package_sha256:
        raise SystemExit("canary package bytes do not match the index")
    settings = Settings.from_env_and_secrets()
    archive_path = _cache_package(
        settings.state_path / "recipe-library-packages", archive, package_sha256
    )
    reader = FixtureReader(index, archive_path)
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
