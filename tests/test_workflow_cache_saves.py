"""Only pushes to main may write the shared uv and Rust caches.

Pull requests restore main's caches but never save their own: each PR-scoped
uv cache is ~900 MB, and a handful of them evicts main's warmed caches from
the repository quota. Every setup-uv and rust-cache step therefore declares
its save condition explicitly (the main ref, or "false").
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MAIN_ONLY = "${{ github.ref == 'refs/heads/main' }}"
SAVE_OPTION = {"astral-sh/setup-uv": "save-cache", "Swatinem/rust-cache": "save-if"}


def _steps(document: dict) -> list[dict]:
    if "runs" in document:
        return document["runs"].get("steps", [])
    return [
        s for job in document.get("jobs", {}).values() for s in job.get("steps", [])
    ]


def test_cache_saving_steps_save_only_on_main() -> None:
    problems = []
    paths = [*(ROOT / ".github/workflows").glob("*.yml")]
    paths += [*(ROOT / ".github/actions").glob("*/action.yml")]
    for path in sorted(paths):
        for step in _steps(yaml.safe_load(path.read_text())):
            action = step.get("uses", "").split("@")[0]
            option = SAVE_OPTION.get(action)
            if option is None:
                continue
            value = step.get("with", {}).get(option)
            if value not in (MAIN_ONLY, "false", False):
                problems.append(f"{path.relative_to(ROOT)}: {action} needs {option}")
    assert problems == []
