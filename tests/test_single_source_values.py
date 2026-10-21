"""Wrong implementations: handwritten/stale selections and unverified projections."""

from pathlib import Path

import pytest

from tools.single_source_values import violations

ROOT = Path(__file__).resolve().parents[1]


def test_registered_values_have_one_owner() -> None:
    assert violations(ROOT) == []


@pytest.mark.parametrize("suffix", (".py", ".yml", "Dockerfile", ".md", ""))
def test_guard_rejects_copied_policy_in_executable_surfaces(
    tmp_path: Path, suffix: str
) -> None:
    (tmp_path / "scripts").mkdir()
    registry = "scripts/single-source-values.toml"
    (tmp_path / registry).write_text((ROOT / registry).read_text())
    owner = "scripts/image_policy.py"
    (tmp_path / owner).write_text(
        'FLOATING_TAGS = frozenset({"latest", "main", "edge"})\n'
    )
    consumer = f"consumer{suffix}"
    source = 'tags = {"latest", "main", "edge"}\n'
    if suffix == ".md":
        source = f"```python\n{source}```\n"
    (tmp_path / consumer).write_text(source)
    errors = violations(tmp_path, [registry, owner, consumer])
    assert len(errors) == 1
    assert "images.floating_tags" in errors[0]
    assert f"{consumer}:" in errors[0]
    assert f"owner {owner}:FLOATING_TAGS" in errors[0]


def test_missing_owner_and_duplicate_registration_fail(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    registry = "scripts/single-source-values.toml"
    text = (ROOT / registry).read_text()
    (tmp_path / registry).write_text(text + text)
    with pytest.raises(ValueError, match="duplicate"):
        violations(tmp_path, [registry])
    (tmp_path / registry).write_text(text)
    (tmp_path / "scripts/image_policy.py").write_text("unrelated = 32\n")
    with pytest.raises(ValueError, match="owner selector"):
        violations(tmp_path, [registry])


def test_mirror_matrix_follows_changed_lock(tmp_path: Path) -> None:
    # Wrong implementation: the workflow mirrors a stale, independently pinned Tailscale.
    import json
    import subprocess
    import sys

    import yaml

    lock_path = tmp_path / "deploy/compose/images.lock.json"
    lock_path.parent.mkdir(parents=True)
    lock = json.loads((ROOT / "deploy/compose/images.lock.json").read_text())
    changed = "example.invalid/tail:changed@sha256:" + "a" * 64
    lock["images"]["tailscale"] = changed
    lock_path.write_text(json.dumps(lock))
    output = subprocess.check_output(
        [sys.executable, str(ROOT / "scripts/ci-image-mirror-inputs"), str(tmp_path)],
        text=True,
    )
    images = json.loads(output)
    assert changed in images
    assert set(images) == {
        ref
        for section in ("images", "build_bases", "ci_tools")
        for ref in lock[section].values()
    }
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci-image-mirror.yml").read_text())[
        "jobs"
    ]
    assert jobs["mirror"]["needs"] == "inputs"
    assert (
        "fromJSON(needs.inputs.outputs.images)"
        in jobs["mirror"]["strategy"]["matrix"]["image"]
    )
    assert "scripts/ci-image-mirror-inputs" in jobs["inputs"]["steps"][-1]["run"]
