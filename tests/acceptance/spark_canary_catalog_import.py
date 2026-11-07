"""Import the synthetic canary package into a running acceptance Controller.

The Spark lifecycle harness runs this program inside the ``control-api``
container as the Controller user.  Production Controllers only read signed
recipe releases, so the canary's producer fixture cannot arrive through the
network reader.  Instead the exact index and package bytes arrive on stdin and
are applied through the same index parser, managed catalog sync, package
decoder and catalog import the release reader feeds.  The program runs in the
previous release's Controller too, so it uses only names that release ships
and builds none of the Controller's own types.  The verified archive is kept
in the Controller's digest-addressed package cache so the recorded package
handle points at durable state.

Input (stdin, JSON): ``request_key``, ``index`` (UTF-8 text) and ``packages``
(base64, one per Recipe of the index).  Output (stdout, JSON): the managed
catalog sync view.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import inspect
import json
import os
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast, get_args

from vonk_agent_protocol import CatalogSyncCode
from vonk_control.auth import TokenCodec
from vonk_control.catalog_service import CatalogService
from vonk_control.catalog_sync import (
    CatalogSyncError,
    CatalogSyncView,
    ManagedRecipeCatalogSyncService,
    SyncTrigger,
)
from vonk_control.db import build_engine, session_factory
from vonk_control.recipe_packages import RecipePackageClient, load_recipe_package
from vonk_control.settings import Settings
from vonk_control.source_bundles import DatabaseSourceBundleStore

if TYPE_CHECKING:
    from vonk_control.recipe_library_types import (
        RecipeLibraryItem,
        RecipeLibrarySnapshot,
    )

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
    """The producer's canary index, read by the installed Controller's own parser.

    This program runs inside whichever Controller the lane installed: the
    previous release and then the candidate. It never builds Controller types
    itself; the snapshot is exactly what that Controller's release reader makes
    of the same signed index, so it has that Controller's shape by construction
    (tests/test_baseline_program_surface.py keeps it that way).
    """

    def __init__(self, index: dict[str, Any], archives: dict[str, Path]) -> None:
        # The index parser is the only one the previous release ships. It reads
        # no client state, so it runs without a client, whose construction is
        # release-download wiring this program has no use for.
        parse = cast(Any, RecipePackageClient)._parse_index
        snapshot, _packages = parse(
            None,
            json.dumps(index).encode("utf-8"),
            publication_commit=str(index["source_commit"]),
        )
        # The Controller skips an entry it cannot read and still syncs the
        # rest; a canary it skipped would import nothing, so refuse loudly.
        if snapshot.problems or len(snapshot.items) != len(index["recipes"]):
            raise ValueError(
                "the installed Controller cannot read the canary index: "
                f"{snapshot.problems!r}"
            )
        self.snapshot: RecipeLibrarySnapshot = snapshot
        self._items = {item.uri: item for item in snapshot.items}
        self._archives = {
            item.uri: archives[item.package_sha256] for item in snapshot.items
        }

    def list(self) -> RecipeLibrarySnapshot:
        return self.snapshot

    def fetch(self, uri: str) -> RecipeLibraryItem:
        item = self._items.get(uri)
        if item is None:
            raise KeyError(uri)
        return load_recipe_package(
            self._archives[uri],
            package_sha256=item.package_sha256,
            publisher=item.publisher,
            slug=item.slug,
            recipe_content_sha256=item.content_sha256,
            library_commit=item.library_commit,
            source_path=item.source_path,
            display=item.display,
        )


def sync_canary_catalog(
    service: ManagedRecipeCatalogSyncService,
    *,
    request_key: str,
    snapshot: RecipeLibrarySnapshot,
) -> CatalogSyncView:
    """Use the installed Controller's observed contract, including lane predecessors.

    Only the acceptance harness bridges promoted releases; the production service
    has one request model. Signature discovery happens before any effect, never
    by catching TypeError after a possibly executed sync.
    """
    parameters = inspect.signature(service.sync).parameters
    if "request" in parameters:
        from vonk_control.catalog_sync_contract import (
            CatalogSyncTrigger,
            ManagedCatalogSyncRequest,
            reviewed_catalog_content,
        )

        return service.sync(
            ManagedCatalogSyncRequest(
                request_key=request_key,
                trigger=CatalogSyncTrigger.MANUAL,
                actor=SYNC_ACTOR,
                reviewed_content_sha256=reviewed_catalog_content(snapshot),
            )
        )
    # These calls execute only inside older promoted Controller images. Their
    # installed signature is authoritative; the immutable FixtureReader serves
    # exactly the producer snapshot even before review binding was introduced.
    # Predecessors declare their manual trigger first in the canonical Literal.
    # Consume that installed vocabulary rather than adding a second spelling.
    manual_trigger = (
        SyncTrigger.MANUAL
        if isinstance(SyncTrigger, type)
        else get_args(SyncTrigger)[0]
    )
    legacy_sync = cast(Callable[..., CatalogSyncView], service.sync)
    if "reviewed_snapshot" in parameters:
        return legacy_sync(
            request_key=request_key,
            trigger=manual_trigger,
            actor=SYNC_ACTOR,
            reviewed_snapshot=snapshot,
        )
    return legacy_sync(
        request_key=request_key,
        trigger=manual_trigger,
        actor=SYNC_ACTOR,
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
            view = sync_canary_catalog(
                sync, request_key=str(payload["request_key"]), snapshot=reader.snapshot
            )
            break
        except CatalogSyncError as error:
            if error.code != CatalogSyncCode.IN_PROGRESS or (
                time.monotonic() > deadline
            ):
                raise
            time.sleep(5)
    print(json.dumps(dataclasses.asdict(view), default=str, sort_keys=True))


if __name__ == "__main__":
    main()
