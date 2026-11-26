"""A pin refresh verifies the release at ingress, then leaves every pin file agreeing."""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import shutil
import sys
import tomllib
from collections.abc import Iterable
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LOCK = "control/packaging/public-contracts.lock"
ACCEPTANCE = "tests/acceptance/recipe-library-revision.txt"
VECTOR = "control/tests/fixtures/future-canary-index.json"
FILES = (
    LOCK,
    ACCEPTANCE,
    "control/pyproject.toml",
    "control/Dockerfile",
    "scripts/ci/resolve-publication-producer",
)


def _tool():
    loader = importlib.machinery.SourceFileLoader(
        "refresh_public_contracts", str(ROOT / "scripts/dev/refresh-public-contracts")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


tool = _tool()


class FakeRecipes:
    """A release in a directory; ``attested`` holds the bytes publish.yml signed."""

    def __init__(self, release: Path, members: dict[str, bytes], attested: set[bytes]):
        self.release, self.members, self.attested = release, members, attested

    def latest_version(self) -> str:
        return "9.9.9"

    def download(self, version: str, asset: str, directory: Path) -> Path:
        shutil.copy2(self.release / asset, directory / asset)
        return directory / asset

    def library_members(self, version: str, names: Iterable[str]) -> dict[str, bytes]:
        return {name: self.members[name] for name in names if name in self.members}

    def verify(self, path: Path, bundle: Path | None = None) -> None:
        if path.read_bytes() not in self.attested:
            raise tool.RefreshError(f"{path.name} is not attested")

    def main_head(self) -> str:
        return "2" * 40


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    for relative in FILES:
        (repository / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, repository / relative)
    old = tomllib.loads((ROOT / LOCK).read_text())["wheel"]
    (repository / old).parent.mkdir(parents=True)
    (repository / old).write_bytes(b"old")
    (repository / VECTOR).parent.mkdir(parents=True, exist_ok=True)
    return repository


def _recipes(
    tmp_path: Path, *, attested_wheel: bool = True, vector: bytes = b"{}"
) -> FakeRecipes:
    release = tmp_path / "release"
    release.mkdir()
    wheel = tool.wheel_name("9.9.9")
    (release / wheel).write_bytes(b"new wheel")
    sums = f"{hashlib.sha256(b'{}').hexdigest()}  future-canary-index.json\n".encode()
    members = {
        "SHA256SUMS": sums,
        "SHA256SUMS.sigstore.json": b"bundle",
        "future-canary-index.json": vector,
    }
    attested = {sums} | ({b"new wheel"} if attested_wheel else set())
    return FakeRecipes(release, members, attested)


def _snapshot(repository: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(repository)): path.read_bytes()
        for path in sorted(repository.rglob("*"))
        if path.is_file()
    }


def _refresh(repository: Path, recipes: FakeRecipes, **choices: object) -> list[str]:
    ran: list[str] = []
    tool.refresh(
        repository,
        recipes,
        version=choices.get("version"),
        acceptance=choices.get("acceptance"),
        acceptance_latest=bool(choices.get("acceptance_latest")),
        relock=lambda root: ran.append("relock"),
        regenerate=lambda root: ran.append("regenerate"),
    )
    return ran


@pytest.mark.parametrize(
    "recipes",
    [
        lambda tmp: _recipes(tmp, attested_wheel=False),
        lambda tmp: _recipes(tmp, vector=b"{ tampered }"),
    ],
    ids=["wheel-not-attested", "vector-not-in-sums"],
)
def test_an_unverified_release_asset_writes_nothing(tmp_path: Path, recipes) -> None:
    repository = _repository(tmp_path)
    before = _snapshot(repository)

    with pytest.raises(tool.RefreshError):
        _refresh(repository, recipes(tmp_path), acceptance_latest=True)

    assert _snapshot(repository) == before


def test_a_verified_release_rewrites_the_lock_and_every_spelling(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    old = tomllib.loads((ROOT / LOCK).read_text())["wheel"]

    ran = _refresh(repository, _recipes(tmp_path))

    new = tool.wheel_name("9.9.9")
    assert tomllib.loads((repository / LOCK).read_text()) == {
        "package": "vonk-forge-public-contracts==9.9.9",
        "release": "https://github.com/CarstVaartjes/vonk-forge-recipes/releases/tag/v9.9.9",
        "wheel": "inventory/wheels/" + new,
        "sha256": hashlib.sha256(b"new wheel").hexdigest(),
    }
    assert [p.name for p in (repository / "inventory/wheels").iterdir()] == [new]
    for relative in FILES[2:]:
        text = (repository / relative).read_text()
        assert new in text
        assert Path(old).name not in text
    assert (repository / VECTOR).read_bytes() == b"{}"
    assert (repository / ACCEPTANCE).read_bytes() == (ROOT / ACCEPTANCE).read_bytes()
    assert ran == ["relock", "regenerate"]


def test_the_acceptance_pin_moves_on_its_own_input(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    _refresh(repository, _recipes(tmp_path), version="9.9.9", acceptance="3" * 40)
    assert (repository / ACCEPTANCE).read_text() == "3" * 40 + "\n"

    shutil.rmtree(tmp_path / "release")
    _refresh(repository, _recipes(tmp_path), acceptance_latest=True)
    assert (repository / ACCEPTANCE).read_text() == "2" * 40 + "\n"


def test_a_release_without_a_future_vector_keeps_the_fixture(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / VECTOR).write_bytes(b"kept")
    recipes = _recipes(tmp_path)
    recipes.members["SHA256SUMS"] = b""
    recipes.members.pop("future-canary-index.json")
    recipes.attested.add(b"")

    _refresh(repository, recipes)

    assert (repository / VECTOR).read_bytes() == b"kept"


def test_the_vector_moves_only_with_the_version(tmp_path: Path) -> None:
    """The tar is republished in place; an unchanged pin keeps the committed vector."""

    repository = _repository(tmp_path)
    _refresh(repository, _recipes(tmp_path))
    shutil.rmtree(tmp_path / "release")
    republished = _recipes(tmp_path)
    republished.members["future-canary-index.json"] = b"republished"

    _refresh(repository, republished)

    assert (repository / VECTOR).read_bytes() == b"{}"


def test_a_failed_gh_command_is_a_refresh_error() -> None:
    with pytest.raises(tool.RefreshError):
        tool._output("false")
