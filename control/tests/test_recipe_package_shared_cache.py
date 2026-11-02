"""The API and the worker each run a recipe package reader on one cache root.

Both observe the library on the same cadence, so neither may consume, remove
or truncate a file the other is still about to commit.
"""

from __future__ import annotations

import errno
import os
import time
from pathlib import Path

import httpx2
import pytest
from vonk_agent_protocol import RecipePackageCode
from vonk_control import recipe_packages
from vonk_control.recipe_packages import RecipePackageClient, RecipePackageError

from .test_recipe_package_transport import (
    RELEASES,
    _canonical_package_fixture,
    _client,
    _release_for,
    signed_releases,
)

__all__ = ["signed_releases"]  # the fixture is used by name


def _scratch_files(cache: Path) -> list[str]:
    return sorted(
        path.name
        for path in cache.iterdir()
        if path.is_file() and path.name != "snapshot.json"
    )


def test_two_readers_ingesting_one_release_both_commit_it(
    tmp_path: Path, signed_releases: list[bytes]
) -> None:
    index, _, package = _canonical_package_fixture()
    release = _release_for(index, package)
    cache = tmp_path / "packages"
    api, worker = _client(release, cache), _client(release, cache)

    api_snapshot, worker_snapshot = api.list(), worker.list()
    api.prepare(api_snapshot)
    worker.prepare(worker_snapshot)
    # The next observation is just as settled in both processes.
    worker.prepare(worker.list())

    assert (cache / "snapshot.json").is_file()
    assert _scratch_files(cache) == []
    api.close()
    worker.close()


def test_an_offline_reader_leaves_the_other_readers_generation_alone(
    tmp_path: Path, signed_releases: list[bytes]
) -> None:
    index, _, package = _canonical_package_fixture()
    release = _release_for(index, package)
    cache = tmp_path / "packages"

    def releases_unavailable(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == RELEASES:
            return httpx2.Response(503)
        return release.handler(request)

    api = _client(release, cache)
    worker = RecipePackageClient(
        api_url="http://127.0.0.1",
        cache_root=cache,
        transport=httpx2.MockTransport(releases_unavailable),
    )
    snapshot = api.list()
    with pytest.raises(RecipePackageError):
        worker.list()  # falls back to the persisted cache, which is empty
    api.prepare(snapshot)

    assert (cache / "snapshot.json").is_file()
    api.close()
    worker.close()


def test_a_failed_commit_names_its_cause_and_heals_on_the_next_prepare(
    tmp_path: Path, signed_releases: list[bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    index, _, package = _canonical_package_fixture()
    release = _release_for(index, package)
    cache = tmp_path / "packages"
    client = _client(release, cache)
    snapshot_path = (cache / "snapshot.json").resolve()
    replace = os.replace
    failures = [OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))]

    def disk_full_once(source: object, target: object) -> None:
        if failures and Path(str(target)).resolve() == snapshot_path:
            raise failures.pop()
        replace(source, target)  # type: ignore[arg-type]

    monkeypatch.setattr(recipe_packages.os, "replace", disk_full_once)
    snapshot = client.list()
    with pytest.raises(RecipePackageError) as refused:
        client.prepare(snapshot)
    assert refused.value.code == RecipePackageCode.CACHE_UNAVAILABLE
    assert os.strerror(errno.ENOSPC) in refused.value.detail
    assert not snapshot_path.exists()

    client.prepare(client.list())
    assert snapshot_path.is_file()
    assert _scratch_files(cache) == []
    client.close()


def test_a_killed_writers_old_private_files_are_removed_at_start(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "packages"
    (cache / "ab").mkdir(parents=True)
    old = [
        cache / ".recipe-library.tar.k1ll3d.part",
        cache / ".snapshot.k1ll3d.tmp",
        cache / "ab" / ".abcdef.k1ll3d.tmp",
    ]
    live = [cache / ".snapshot.writing.tmp", cache / "snapshot.json"]
    two_days_ago = time.time() - 2 * 24 * 60 * 60
    for path in old + live:
        path.write_bytes(b"x")
    for path in old + [live[1]]:
        os.utime(path, (two_days_ago, two_days_ago))

    RecipePackageClient(api_url="http://127.0.0.1", cache_root=cache).close()

    assert [path for path in old if path.exists()] == []
    assert all(path.exists() for path in live)
