from __future__ import annotations

import json
from pathlib import Path

import httpx

from vonk_control.recipe_packages import RecipePackageClient


ROOT = Path("/private/tmp/vonk-forge-recipes-contract-conversion-final")


def test_production_reader_pins_raw_index_and_package_to_resolved_commit(tmp_path: Path) -> None:
    if not (ROOT / "catalog-index.json").is_file():
        return
    index = (ROOT / "catalog-index.json").read_bytes()
    row = json.loads(index)["recipes"][0]
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
    item = client.fetch(client.list().items[0].uri)
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
