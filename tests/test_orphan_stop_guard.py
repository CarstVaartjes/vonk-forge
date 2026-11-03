"""A running container no claim owns is stopped, never skipped forever.

The agent's observation of a run the Controller names unowned must reach the
orphan stop; a branch that skips because "the container is running" leaves the
container holding GPU memory with no owner. Native end-to-end tests exercise
the stop; this scan keeps the old skip from coming back.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OBSERVATIONS = ROOT / "rust/crates/vonk-agent/src/executor/observations.rs"

# The skip that kept a running container forever, on any unowned path.
RUNNING_SKIP = re.compile(r'skip\(\s*"the container is running"\s*\)')


def unowned_running_skips(source: str) -> list[int]:
    return [
        number
        for number, line in enumerate(source.splitlines(), 1)
        if RUNNING_SKIP.search(line)
    ]


def test_no_observation_path_skips_a_running_container_it_could_stop() -> None:
    assert unowned_running_skips(OBSERVATIONS.read_text(encoding="utf-8")) == []


def test_the_scan_finds_the_skip_it_guards_against() -> None:
    # Catches a scan that no longer matches the form the skip had.
    old = (
        "RecipeRunDisposition::Unowned => {\n"
        '    Ok(_) => return Err(skip("the container is running")),\n'
    )
    assert unowned_running_skips(old) == [2]
