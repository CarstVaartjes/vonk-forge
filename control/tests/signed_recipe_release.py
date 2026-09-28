"""An in-process signed recipe release for Controller package tests.

The Controller reads recipe packages only from signed GitHub releases.  Tests
serve an index and its packages as such a release through an httpx mock
transport; the ``signed_recipe_releases`` fixture replaces Sigstore with a
verifier that accepts only this module's test signature, so everything after
the signature (digests, commit binding, caching, decoding) runs unchanged.
The real verifier is covered by ``test_recipe_release.py``.

Use it per module::

    from tests.signed_recipe_release import SignedRecipeRelease, signed_recipe_releases

    pytestmark = pytest.mark.usefixtures("signed_recipe_releases")
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping
from copy import deepcopy
from pathlib import Path, PurePosixPath

import httpx2
import pytest
from vonk_control import recipe_packages
from vonk_control.recipe_packages import PACKAGE_REPOSITORY, RecipePackageClient
from vonk_control.recipe_release import (
    RELEASE_BUNDLE,
    RELEASE_CHECKSUMS,
    RELEASE_INDEX,
    RecipeReleaseError,
)

RELEASE_TAG = "v2.0.0"
_SIGNATURE_KIND = "vonk-forge-test-recipe-release-signature"
_RELEASES = f"/repos/{PACKAGE_REPOSITORY}/releases"
_DOWNLOAD = f"/{PACKAGE_REPOSITORY}/releases/download/{RELEASE_TAG}/"


def _verify(checksums: bytes, bundle: bytes) -> str:
    del checksums
    try:
        signature = json.loads(bundle)
    except ValueError:
        signature = None
    if not isinstance(signature, dict) or signature.get("kind") != _SIGNATURE_KIND:
        raise RecipeReleaseError("recipe_release.signature_invalid", "unsigned")
    return str(signature["commit"])


@pytest.fixture
def signed_recipe_releases(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Accept only releases signed by :class:`SignedRecipeRelease`."""
    monkeypatch.setattr(recipe_packages, "verify_release_checksums", _verify)
    yield


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


class SignedRecipeRelease:
    """Serve one index and its packages as the latest signed recipe release.

    ``publish`` replaces the release contents, so a test can advance or
    tamper with the library between reads.  Package entries are served as
    the release asset named by the basename of their index path; the index
    is rewritten to the release layout ``packages/<asset>``.
    """

    def __init__(
        self,
        index: Mapping[str, object],
        packages: Mapping[str, bytes] | Callable[[str], bytes],
    ) -> None:
        self.requests: list[str] = []
        self.offline = False
        self.publish(index, packages)

    def publish(
        self,
        index: Mapping[str, object],
        packages: Mapping[str, bytes] | Callable[[str], bytes],
        *,
        signed: bool = True,
    ) -> None:
        document = deepcopy(dict(index))
        assets: dict[str, bytes] = {}
        recipes = document.get("recipes")
        assert isinstance(recipes, list)
        for row in recipes:
            package = row["package"]
            location = str(package["path"])
            name = PurePosixPath(location).name
            assets[name] = (
                packages(location) if callable(packages) else packages[location]
            )
            package["path"] = f"packages/{name}"
        assets[RELEASE_INDEX] = _canonical(document)
        self.index = document
        self.assets = assets
        self.checksums = "".join(
            f"{hashlib.sha256(payload).hexdigest()}  {name}\n"
            for name, payload in sorted(assets.items())
        ).encode()
        self.signature = (
            json.dumps(
                {"kind": _SIGNATURE_KIND, "commit": document["source_commit"]}
            ).encode()
            if signed
            else b'{"forged": true}'
        )

    @classmethod
    def from_library(
        cls, index: Mapping[str, object], root: Path
    ) -> SignedRecipeRelease:
        """Serve packages from a built recipe library checkout."""
        return cls(index, lambda location: (root / location).read_bytes())

    @property
    def package_downloads(self) -> list[str]:
        return [
            name
            for name in self.requests
            if name not in {RELEASE_CHECKSUMS, RELEASE_BUNDLE, RELEASE_INDEX}
            and not name.startswith("/")
        ]

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        self.requests.append(path.removeprefix(_DOWNLOAD))
        if self.offline:
            raise httpx2.ConnectError("offline", request=request)
        served = {
            RELEASE_CHECKSUMS: self.checksums,
            RELEASE_BUNDLE: self.signature,
            **self.assets,
        }
        release = {
            "tag_name": RELEASE_TAG,
            "draft": False,
            "assets": [{"name": name, "state": "uploaded"} for name in sorted(served)],
        }
        if path == _RELEASES:
            return httpx2.Response(200, json=[release])
        if path == f"{_RELEASES}/tags/{RELEASE_TAG}":
            return httpx2.Response(200, json=release)
        if path.startswith(_DOWNLOAD) and path[len(_DOWNLOAD) :] in served:
            return httpx2.Response(
                200,
                headers={"content-type": "application/octet-stream"},
                content=served[path[len(_DOWNLOAD) :]],
            )
        return httpx2.Response(404)

    def client(self, cache_root: Path) -> RecipePackageClient:
        return RecipePackageClient(
            cache_root=cache_root, transport=httpx2.MockTransport(self.handler)
        )
