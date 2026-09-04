from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from vonk_control.recipe_packages import PACKAGE_MEDIA_TYPE, RecipePackageClient


@pytest.mark.skipif(
    not Path("/private/tmp/vonk-recipe-package-fixture").is_dir(),
    reason="cross-repository publisher fixture was not generated",
)
def test_publisher_fixture_imports_all_84_and_reuses_persistent_packages(tmp_path: Path) -> None:
    fixture = Path("/private/tmp/vonk-recipe-package-fixture")
    index_path = Path("/private/tmp/vonk-forge-recipes-packages/catalog-index.json")
    if not index_path.is_file():
        pytest.skip("recipe publisher checkout is not available")
    descriptor = json.loads(index_path.read_text(encoding="utf-8"))
    rows = descriptor["recipes"]
    assert len(rows) == 84
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("index.json"):
            return httpx.Response(200, headers={"content-type": "application/json"}, content=index_path.read_bytes())
        return httpx.Response(200, headers={"content-type": PACKAGE_MEDIA_TYPE}, content=(fixture / Path(request.url.path).name).read_bytes())

    cache = tmp_path / "packages"
    client = RecipePackageClient("http://127.0.0.1", cache_root=cache, transport=httpx.MockTransport(handler))
    snapshot = client.list()
    client.prepare(snapshot)
    assert len(snapshot.items) == 84
    assert len([path for path in calls if path.endswith(".tar.gz")]) == 84
    client.close()

    calls.clear()
    restarted = RecipePackageClient("http://127.0.0.1", cache_root=cache, transport=httpx.MockTransport(handler))
    restarted.prepare(restarted.list())
    assert calls == ["/v1/recipe-library/index.json"]
    restarted.close()
