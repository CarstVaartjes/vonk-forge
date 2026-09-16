"""Adversarial and boundary archive cases for the package consumer.

`RecipePackageClient._decode_package` is the only shipped code that opens a recipe
archive somebody else produced: the Controller downloads a package published by the
recipe library and must fail closed on a tampered or malformed tar. The producer's
own `validate_recipe_archive` never meets an adversary -- its only caller validates
the archive it assembled a line earlier -- so the adversarial cases belong here, in
front of the copy that actually parses untrusted bytes.

Every case serves bytes whose declared `package.sha256` matches what is sent, so the
transport digest check passes and any rejection has to come from the archive
validation itself. The closing group covers the resource limits that stop a published
archive from exhausting the Controller.
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import io
import json
import tarfile
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from vonk_control import recipe_packages
from vonk_control.recipe_packages import (
    PACKAGE_MEDIA_TYPE,
    RecipePackageClient,
    RecipePackageError,
)

from tests.recipe_library_source import recipe_library_root

Member = tuple[tarfile.TarInfo, bytes | None]


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


@pytest.fixture(scope="module")
def published_package() -> tuple[dict[str, Any], dict[str, Any], bytes]:
    """One real published index row and its archive, for tampering with."""
    root = recipe_library_root()
    index: dict[str, Any] = json.loads(
        (root / "catalog-index.json").read_text(encoding="utf-8")
    )
    row: dict[str, Any] = index["recipes"][0]
    package = (root / row["package"]["path"]).read_bytes()
    return index, row, package


def _members(package: bytes) -> list[Member]:
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:*") as archive:
        members: list[Member] = []
        for member in archive.getmembers():
            stream = archive.extractfile(member)
            members.append(
                (copy.deepcopy(member), stream.read() if stream is not None else None)
            )
        return members


def _repack(members: list[Member]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for member, body in members:
            archive.addfile(member, io.BytesIO(body) if body is not None else None)
    return gzip.compress(stream.getvalue(), compresslevel=9, mtime=0)


def _rename(members: list[Member], index: int, name: str) -> list[Member]:
    members[index][0].name = name
    return members


def _rewrite_manifest(
    members: list[Member], mutate: Callable[[dict[str, Any]], None]
) -> list[Member]:
    """Apply `mutate` to manifest.json and repair the member's declared size."""
    result: list[Member] = []
    for member, body in members:
        if member.name == "manifest.json":
            assert body is not None
            manifest: dict[str, Any] = json.loads(body)
            mutate(manifest)
            encoded = _canonical(manifest)
            member = copy.deepcopy(member)
            member.size = len(encoded)
            result.append((member, encoded))
        else:
            result.append((member, body))
    return result


def _declared(members: list[Member], name: str) -> dict[str, Any]:
    """The manifest entry for `name`, read from the members as they stand."""
    body = next(body for member, body in members if member.name == "manifest.json")
    assert body is not None
    manifest: dict[str, Any] = json.loads(body)
    return next(entry for entry in manifest["files"] if entry["path"] == name)


def _add_member(members: list[Member], name: str, body: bytes) -> list[Member]:
    member = tarfile.TarInfo(name)
    member.size = len(body)
    member.mode = 0o644
    return [*members, (member, body)]


def _member(name: str, body: bytes) -> Member:
    member = tarfile.TarInfo(name)
    member.size = len(body)
    member.mode = 0o644
    return (member, body)


def _reject(
    tmp_path: Any,
    published_package: tuple[dict[str, Any], dict[str, Any], bytes],
    package: bytes,
    *,
    code: str,
) -> RecipePackageError:
    """Serve `package` as the published archive and return the refusal."""
    index, row, _published = published_package
    served = copy.deepcopy(index)
    served_row = copy.deepcopy(row)
    served_row["package"]["sha256"] = hashlib.sha256(package).hexdigest()
    served_row["package"]["expected_bytes"] = len(package)
    served["recipes"] = [served_row]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("index.json"):
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=_canonical(served),
            )
        return httpx.Response(
            200, headers={"content-type": PACKAGE_MEDIA_TYPE}, content=package
        )

    client = RecipePackageClient(
        "http://127.0.0.1",
        cache_root=tmp_path / "packages",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(RecipePackageError) as caught:
            client.prepare(client.list())
    finally:
        client.close()
    assert caught.value.code == code, caught.value.detail
    return caught.value


# --- member paths -----------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    (
        "../recipe.json",
        "nested/../../recipe.json",
        "/recipe.json",
        "recipes\\recipe.json",
        "recipes//recipe.json",
        "./recipe.json",
        ".",
    ),
)
def test_rejects_member_paths_that_escape_the_archive_root(
    tmp_path: Any,
    published_package: tuple[dict[str, Any], dict[str, Any], bytes],
    name: str,
) -> None:
    _index, _row, package = published_package
    members = _rename(_members(package), 0, name)
    _reject(
        tmp_path,
        published_package,
        _repack(members),
        code="recipe_package.extract_invalid",
    )


# --- member types -----------------------------------------------------------------


@pytest.mark.parametrize(
    "member_type",
    (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE),
)
def test_rejects_non_regular_members(
    tmp_path: Any,
    published_package: tuple[dict[str, Any], dict[str, Any], bytes],
    member_type: bytes,
) -> None:
    _index, _row, package = published_package
    members = _members(package)
    member, _body = members[0]
    member.type = member_type
    member.linkname = "recipe.json"
    member.size = 0
    members[0] = (member, None)
    _reject(
        tmp_path,
        published_package,
        _repack(members),
        code="recipe_package.extract_invalid",
    )


def test_rejects_duplicate_member_names(
    tmp_path: Any,
    published_package: tuple[dict[str, Any], dict[str, Any], bytes],
) -> None:
    _index, _row, package = published_package
    members = _members(package)
    members.append((copy.deepcopy(members[0][0]), members[0][1]))
    _reject(
        tmp_path,
        published_package,
        _repack(members),
        code="recipe_package.extract_invalid",
    )


# --- manifest inventory -----------------------------------------------------------


def test_rejects_a_member_the_manifest_does_not_declare(
    tmp_path: Any,
    published_package: tuple[dict[str, Any], dict[str, Any], bytes],
) -> None:
    _index, _row, package = published_package
    members = _add_member(_members(package), "undeclared.txt", b"extra")
    _reject(
        tmp_path,
        published_package,
        _repack(members),
        code="recipe_package.package_invalid",
    )


def test_rejects_a_declared_entry_missing_from_the_archive(
    tmp_path: Any,
    published_package: tuple[dict[str, Any], dict[str, Any], bytes],
) -> None:
    _index, _row, package = published_package
    members = [item for item in _members(package) if item[0].name != "recipe.json"]
    _reject(
        tmp_path,
        published_package,
        _repack(members),
        code="recipe_package.package_invalid",
    )


def test_rejects_a_stale_per_file_digest(
    tmp_path: Any,
    published_package: tuple[dict[str, Any], dict[str, Any], bytes],
) -> None:
    _index, _row, package = published_package
    members = _members(package)
    target = "recipe.json"
    original = _declared(members, target)["sha256"]

    def mutate(manifest: dict[str, Any]) -> None:
        for entry in manifest["files"]:
            if entry["path"] == target:
                entry["sha256"] = "0" * 64

    assert original != "0" * 64
    _reject(
        tmp_path,
        published_package,
        _repack(_rewrite_manifest(members, mutate)),
        code="recipe_package.package_invalid",
    )


def test_rejects_a_wrong_per_file_size(
    tmp_path: Any,
    published_package: tuple[dict[str, Any], dict[str, Any], bytes],
) -> None:
    _index, _row, package = published_package
    members = _members(package)
    target = "recipe.json"

    def mutate(manifest: dict[str, Any]) -> None:
        for entry in manifest["files"]:
            if entry["path"] == target:
                entry["size"] = int(entry["size"]) + 1

    _reject(
        tmp_path,
        published_package,
        _repack(_rewrite_manifest(members, mutate)),
        code="recipe_package.package_invalid",
    )


def test_rejects_a_manifest_that_declares_a_path_twice(
    tmp_path: Any,
    published_package: tuple[dict[str, Any], dict[str, Any], bytes],
) -> None:
    """A duplicate manifest entry keeps the path set equal but grows the list."""
    _index, _row, package = published_package
    members = _members(package)

    def mutate(manifest: dict[str, Any]) -> None:
        entry = next(
            item for item in manifest["files"] if item["path"] == "recipe.json"
        )
        manifest["files"].append(dict(entry))

    _reject(
        tmp_path,
        published_package,
        _repack(_rewrite_manifest(members, mutate)),
        code="recipe_package.package_invalid",
    )


# --- entrypoint -------------------------------------------------------------------


def test_rejects_a_second_recipe_json_entrypoint(
    tmp_path: Any,
    published_package: tuple[dict[str, Any], dict[str, Any], bytes],
) -> None:
    _index, _row, package = published_package
    body = next(
        body for member, body in _members(package) if member.name == "recipe.json"
    )
    assert body is not None
    members = _add_member(_members(package), "nested/recipe.json", body)

    def mutate(manifest: dict[str, Any]) -> None:
        manifest["files"].append(
            {
                "path": "nested/recipe.json",
                "size": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        )

    _reject(
        tmp_path,
        published_package,
        _repack(_rewrite_manifest(members, mutate)),
        code="recipe_package.package_invalid",
    )


def test_rejects_an_archive_without_a_recipe_json_entrypoint(
    tmp_path: Any,
    published_package: tuple[dict[str, Any], dict[str, Any], bytes],
) -> None:
    _index, _row, package = published_package
    members = [item for item in _members(package) if item[0].name != "recipe.json"]

    def mutate(manifest: dict[str, Any]) -> None:
        manifest["files"] = [
            entry for entry in manifest["files"] if entry["path"] != "recipe.json"
        ]

    _reject(
        tmp_path,
        published_package,
        _repack(_rewrite_manifest(members, mutate)),
        code="recipe_package.package_invalid",
    )


# --- resource limits --------------------------------------------------------------


def test_rejects_more_members_than_the_shipped_file_count_limit(
    tmp_path: Any,
    published_package: tuple[dict[str, Any], dict[str, Any], bytes],
) -> None:
    """The first rejected count is MAX_PACKAGE_FILES + 1.

    Empty members are enough to reach this limit, so it runs against the shipped
    constant rather than a scaled copy.
    """
    members = [
        _member(f"member-{index:05d}", b"")
        for index in range(recipe_packages.MAX_PACKAGE_FILES + 1)
    ]
    _reject(
        tmp_path,
        published_package,
        _repack(members),
        code="recipe_package.extract_invalid",
    )


def test_member_size_limit_is_exclusive(
    tmp_path: Any,
    published_package: tuple[dict[str, Any], dict[str, Any], bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member at the limit is read; one byte more is refused.

    The limit is scaled down here. Materialising the shipped 128 MiB, only to
    prove a `>` comparison, would cost the suite a 128 MiB archive and would
    still leave the accepted side untestable. Scaling exercises both sides of the
    shipped comparison; the constant's own value is not asserted.
    """
    monkeypatch.setattr(recipe_packages, "MAX_PACKAGE_FILE_BYTES", 64)
    _reject(
        tmp_path,
        published_package,
        _repack([_member("payload.bin", b"x" * 64)]),
        code="recipe_package.package_invalid",
    )
    _reject(
        tmp_path,
        published_package,
        _repack([_member("payload.bin", b"x" * 65)]),
        code="recipe_package.extract_invalid",
    )


def test_total_size_limit_is_exclusive(
    tmp_path: Any,
    published_package: tuple[dict[str, Any], dict[str, Any], bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Members summing to the limit are read; one byte more is refused.

    Scaled for the same reason as the per-member limit: the shipped 256 MiB is
    not worth materialising in a unit test.
    """
    monkeypatch.setattr(recipe_packages, "MAX_PACKAGE_FILE_BYTES", 1024)
    monkeypatch.setattr(recipe_packages, "MAX_PACKAGE_TOTAL_BYTES", 20)
    _reject(
        tmp_path,
        published_package,
        _repack([_member("a.bin", b"x" * 10), _member("b.bin", b"x" * 10)]),
        code="recipe_package.package_invalid",
    )
    _reject(
        tmp_path,
        published_package,
        _repack([_member("a.bin", b"x" * 11), _member("b.bin", b"x" * 11)]),
        code="recipe_package.extract_invalid",
    )
