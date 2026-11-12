"""A pin refresh leaves the files `scripts/verify-supply-chain` checks agreeing."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import shutil
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOCK = "control/packaging/public-contracts.lock"
FILES = (
    LOCK,
    "control/pyproject.toml",
    "tests/acceptance/recipe-library-revision.txt",
    "control/Dockerfile",
    "scripts/resolve-publication-producer",
    ".github/workflows/profile-effect-consumer-proof.yml",
    "control/tests/test_profile_effect_consumer_contract_postgres.py",
)


def _tool():
    loader = importlib.machinery.SourceFileLoader(
        "refresh_public_contracts", str(ROOT / "scripts/refresh-public-contracts")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def test_a_new_pin_replaces_the_wheel_and_every_spelling_of_it(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    for relative in FILES:
        (repository / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, repository / relative)
    old = tomllib.loads((ROOT / LOCK).read_text())["wheel"]
    (repository / old).parent.mkdir(parents=True)
    (repository / old).write_bytes(b"old")
    new = tmp_path / "vonk_forge_public_contracts-9.9.9-py3-none-any.whl"
    new.write_bytes(b"new")
    commit = "1" * 40

    _tool().apply_pin(repository, commit, new, relock=lambda root: None)

    lock = tomllib.loads((repository / LOCK).read_text())
    assert lock["revision"] == commit
    assert lock["package"] == "vonk-forge-public-contracts==9.9.9"
    assert lock["wheel"] == "inventory/wheels/" + new.name
    assert [p.name for p in (repository / "inventory/wheels").iterdir()] == [new.name]
    revision = repository / "tests/acceptance/recipe-library-revision.txt"
    assert revision.read_text() == commit + "\n"
    assert f'rev = "{commit}"' in (repository / "control/pyproject.toml").read_text()
    old_revision = tomllib.loads((ROOT / LOCK).read_text())["revision"]
    for relative in (
        ".github/workflows/profile-effect-consumer-proof.yml",
        "control/tests/test_profile_effect_consumer_contract_postgres.py",
    ):
        text = (repository / relative).read_text()
        assert commit in text and old_revision not in text
    for relative in ("control/Dockerfile", "scripts/resolve-publication-producer"):
        text = (repository / relative).read_text()
        assert new.name in text and Path(old).name not in text
