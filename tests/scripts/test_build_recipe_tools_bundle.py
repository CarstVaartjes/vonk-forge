from __future__ import annotations

import io
import os
import tarfile
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_LOADER = SourceFileLoader(
    "build_recipe_tools_bundle", str(ROOT / "scripts/recipes/build-recipe-tools-bundle")
)
_SPEC = spec_from_loader(_LOADER.name, _LOADER)
assert _SPEC is not None
assert _SPEC.loader is not None
bundle = module_from_spec(_SPEC)
_SPEC.loader.exec_module(bundle)

FEATURES = b'{"serving_kinds": ["openai.chat"]}\n'


def _tree(root: Path, *, reverse: bool, mtime: int) -> Path:
    """Every bundled path as a small file or directory, written in either order."""
    paths = list(bundle.PATHS)
    for relative in reversed(paths) if reverse else paths:
        path = root / relative
        if path.name in {"src", "migrations"}:
            files = [path / "b.py", path / "a" / "c.py", path / "__pycache__/x.pyc"]
        else:
            files = [path]
        for file in reversed(files) if reverse else files:
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(f"{file.relative_to(root)}\n")
            os.utime(file, (mtime, mtime))
    (root / "scripts/recipes/validate-recipe-checkout").chmod(0o750)
    return root


def test_two_trees_with_the_same_files_give_the_same_normalised_bytes(
    tmp_path: Path,
) -> None:
    first = bundle.build(_tree(tmp_path / "one", reverse=False, mtime=1), FEATURES)
    second = bundle.build(
        _tree(tmp_path / "two", reverse=True, mtime=2_000_000_000), FEATURES
    )
    assert first == second
    with tarfile.open(fileobj=io.BytesIO(first), mode="r:gz") as archive:
        members = archive.getmembers()
        assert archive.extractfile("platform-features.json").read() == FEATURES  # pyright: ignore[reportOptionalMemberAccess]  # a regular member
    names = [member.name for member in members]
    assert names[1:] == sorted(names[1:])
    assert not [
        name for name in names if "__pycache__" in name or name.endswith(".pyc")
    ]
    assert {(m.mtime, m.uid, m.gid, m.uname, m.gname) for m in members} == {
        (0, 0, 0, "", "")
    }
    modes = {member.name: member.mode for member in members}
    assert modes["scripts/recipes/validate-recipe-checkout"] == 0o755
    assert modes["uv.lock"] == 0o644
    assert first[4:8] == b"\0\0\0\0"  # the gzip header carries no mtime


def test_a_missing_bundled_path_refuses_to_build(tmp_path: Path) -> None:
    root = _tree(tmp_path, reverse=False, mtime=1)
    (root / "uv.lock").unlink()
    with pytest.raises(bundle.BundleError, match="uv.lock"):
        bundle.build(root, FEATURES)
