"""Refuse new oversized sources, growth, and stale size exemptions.

Hermetic: no git. The allowlist is the reviewed ceiling; any raise is a visible
diff of that file. After a split or reduction, record the smaller exact count
(remove entries at 1500 or below).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = "tools/file-size-allowlist.json"
LIMIT = 1500
FOCUSED_PACKAGES = (
    "control/src/vonk_control/operation_api/",
    "control/src/vonk_control/artifact_jobs/",
    "control/src/vonk_control/models/",
)
FOCUSED_LIMIT = 1000
SOURCE_ROOTS = ("control/src", "src", "scripts", "control/web/src")
# These outputs belong to the repository's client/wire generators.
GENERATED_PREFIXES = ("src/cluster_profiles/generated_control/",)
GENERATED_FILES = {
    "src/cluster_profiles/schemas/control-openapi.json",
    "src/cluster_profiles/schemas/cli-update-contract.schema.json",
    "src/cluster_profiles/schemas/qualification-authority-v5.schema.json",
    "src/cluster_profiles/schemas/qualification-campaign-manifest-v2.schema.json",
    "rust/crates/vonk-agent-protocol/src/generated.rs",
    "control/web/src/api/generated.d.ts",
    "control/web/src/api/runtime.generated.d.ts",
    "control/web/src/api/runtime.generated.js",
    "control/web/src/api/vocabulary.generated.ts",
}


def source_counts(root: Path) -> dict[str, int]:
    roots = [root / path for path in SOURCE_ROOTS]
    roots.extend((root / "rust/crates").glob("*/src"))
    counts = {}
    for directory in roots:
        for path in directory.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(root).as_posix()
            if (
                relative in GENERATED_FILES
                or relative.startswith(GENERATED_PREFIXES)
                or "__pycache__" in path.parts
                or "node_modules" in path.parts
            ):
                continue
            data = path.read_bytes()
            counts[relative] = data.count(b"\n") + int(
                bool(data) and not data.endswith(b"\n")
            )
    return counts


def evaluate(
    counts: dict[str, int], listed: dict[str, int], previous: dict[str, int]
) -> list[str]:
    problems = []
    for path, ceiling in listed.items():
        if type(ceiling) is not int or ceiling <= LIMIT:
            problems.append(f"{path}: exemption must exceed {LIMIT}; remove it")
            continue
        if ceiling > previous.get(path, LIMIT):
            problems.append(f"{path}: ceiling increased or new oversized file listed")
        actual = counts.get(path, 0)
        if actual != ceiling:
            problems.append(
                f"{path}: listed {ceiling}, actual {actual}; lower on shrink"
            )
    for path, count in counts.items():
        if path.startswith(FOCUSED_PACKAGES) and count > FOCUSED_LIMIT:
            problems.append(f"{path}: focused module exceeds {FOCUSED_LIMIT} lines")
        if count > LIMIT and path not in listed:
            problems.append(
                f"{path}: unlisted source has {count} lines (limit {LIMIT})"
            )
    return problems


def test_source_file_sizes_only_fall() -> None:
    listed: dict[str, int] = json.loads((ROOT / ALLOWLIST).read_text())["files"]
    # Hermetic: actual sizes against the reviewed allowlist only. Raising a
    # listed ceiling is a visible, reviewed diff of the allowlist file.
    assert not (problems := evaluate(source_counts(ROOT), listed, dict(listed))), (
        "\n".join(problems)
    )


@pytest.mark.parametrize(
    ("actual", "listed", "previous", "fails"),
    [
        (1500, {}, {}, False),
        (1501, {}, {}, True),
        (1501, {"file.py": 1501}, {}, True),  # new file cannot buy an exemption
        (1601, {"file.py": 1601}, {"file.py": 1600}, True),
        (1601, {"file.py": 1600}, {"file.py": 1600}, True),
        (1599, {"file.py": 1600}, {"file.py": 1600}, True),  # stale after shrink
        (1599, {"file.py": 1599}, {"file.py": 1600}, False),
        (1500, {"file.py": 1500}, {"file.py": 1600}, True),
        (1500, {}, {"file.py": 1600}, False),
        (0, {"file.py": 1600}, {"file.py": 1600}, True),  # deleted exemption
    ],
)
def test_ratchet_rejects_growth_new_exemptions_and_stale_counts(
    actual: int, listed: dict[str, int], previous: dict[str, int], fails: bool
) -> None:
    assert bool(evaluate({"file.py": actual}, listed, previous)) is fails


def test_scan_covers_authored_sources_and_excludes_generator_outputs(
    tmp_path: Path,
) -> None:
    authored = [
        "control/src/new.py",
        "src/new.py",
        "scripts/new-tool",
        "scripts/dev.Dockerfile",
        "src/new.sql",
        "rust/crates/new-crate/src/main.rs",
        "control/web/src/new.tsx",
        "control/web/src/new.css",
    ]
    for relative in [
        *authored,
        *GENERATED_FILES,
        "src/cluster_profiles/generated_control/a.py",
    ]:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/usr/bin/env python\n" + "line\n" * LIMIT)
    assert source_counts(tmp_path) == dict.fromkeys(authored, LIMIT + 1)


@pytest.mark.parametrize("package", FOCUSED_PACKAGES)
def test_focused_packages_cannot_regrow_monoliths(package: str) -> None:
    path = package + "service.py"
    assert evaluate({path: FOCUSED_LIMIT}, {}, {}) == []
    assert evaluate({path: FOCUSED_LIMIT + 1}, {}, {})
