from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from vonk_control.recipe_packages import RecipePackageClient


ROOT = Path("/private/tmp/vonk-forge-recipes-release-integration")


def test_production_reader_pins_raw_index_and_package_to_resolved_commit(tmp_path: Path) -> None:
    if not (ROOT / "catalog-index.json").is_file():
        pytest.skip("published recipe fixture is unavailable")
    index = (ROOT / "catalog-index.json").read_bytes()
    rows = json.loads(index)["recipes"]
    row = next(
        row for row in rows if row["document"]["identity"]["slug"] == "deepseek-v4-flash-0731-mia-dual"
    )
    package_path = ROOT / row["package"]["path"]
    publication = "2" * 40
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": publication})
        if request.url.path.endswith("/catalog-index.json"):
            return httpx.Response(200, headers={"content-type": "text/plain"}, content=index)
        if request.url.path.endswith(package_path.name):
            return httpx.Response(
                200,
                headers={"content-type": "application/octet-stream"},
                content=package_path.read_bytes(),
            )
        return httpx.Response(404)

    client = RecipePackageClient(
        None,
        api_url="http://127.0.0.1",
        cache_root=tmp_path / "packages",
        transport=httpx.MockTransport(handler),
    )
    snapshot = client.list()
    item = client.fetch(
        next(
            entry.uri
            for entry in snapshot.items
            if entry.slug == "deepseek-v4-flash-0731-mia-dual"
        )
    )
    assert requests[0].endswith("/repos/CarstVaartjes/vonk-forge-recipes/commits/main")
    assert requests[1] == (
        "https://raw.githubusercontent.com/CarstVaartjes/vonk-forge-recipes/"
        f"{publication}/catalog-index.json"
    )
    assert requests[2] == (
        "https://raw.githubusercontent.com/CarstVaartjes/vonk-forge-recipes/"
        f"{publication}/{row['package']['path']}"
    )
    assert item.package_handle is not None
    assert item.package_handle.publication_commit == publication
    assert item.package_handle.package_size == row["package"]["expected_bytes"]
    assert item.package_handle.package_sha256 == row["package"]["sha256"]
    assert item.package_handle.archive_path.is_file()
    assert item.package_handle.closure_path.is_dir()


def test_publication_network_smoke_at_published_commit(tmp_path: Path) -> None:
    """Opt-in smoke for the real GitHub API/raw publication boundary."""
    if os.environ.get("VONK_RUN_RECIPE_NETWORK_SMOKE") != "1":
        pytest.skip("set VONK_RUN_RECIPE_NETWORK_SMOKE=1 for the public publication smoke")
    client = RecipePackageClient(
        None,
        publication_commit="2001c6502bfdc66141dd7224bfde5d77734e9959",
        cache_root=tmp_path / "packages",
    )
    try:
        snapshot = client.list()
        assert snapshot.repository == "CarstVaartjes/vonk-forge-recipes"
        assert len(snapshot.items) == 84
        item = client.fetch(
            next(
                entry.uri
                for entry in snapshot.items
                if entry.slug == "deepseek-v4-flash-0731-mia-dual"
            )
        )
        assert item.package_handle is not None
        assert item.package_handle.publication_commit == (
            "2001c6502bfdc66141dd7224bfde5d77734e9959"
        )
        assert item.package_handle.package_sha256 == (
            "eb408d2559ab16b7aa3697eb4cf66495eb22cd6da31cec496f540ac76898a581"
        )
        assert item.package_handle.package_size == 72879
        assert item.package_handle.archive_path.is_file()
        assert item.package_handle.closure_path.is_dir()
    finally:
        client.close()
