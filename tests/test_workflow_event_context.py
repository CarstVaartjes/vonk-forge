"""Runner-managed parallel steps must not race on GITHUB_EVENT_PATH.

An action using @actions/github constructs Context by parsing that shared file.
The parallel runner can truncate/rewrite it while siblings or the next action
read it, even after the group reports completion. Serial runner steps avoid the
race for all actions (including composite actions and future toolkit consumers),
without guessing which dependencies import @actions/github or weakening evidence.
"""

from pathlib import Path

import pytest
import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

ROOT = Path(__file__).resolve().parents[1]


def _parallel_lines(node: Node) -> list[int]:
    if isinstance(node, MappingNode):
        return [
            key.start_mark.line + 1
            for key, _ in node.value
            if isinstance(key, ScalarNode) and key.value == "parallel"
        ] + [line for _, value in node.value for line in _parallel_lines(value)]
    if isinstance(node, SequenceNode):
        return [line for value in node.value for line in _parallel_lines(value)]
    return []


def test_workflows_do_not_race_on_the_shared_event_payload() -> None:
    problems = []
    for path in sorted((ROOT / ".github").rglob("*")):
        if path.suffix not in {".yml", ".yaml"}:
            continue
        document = yaml.compose(path.read_text())
        if document is not None:
            problems.extend(
                f"{path.relative_to(ROOT)}:{line}: runner parallel steps share "
                "GITHUB_EVENT_PATH; use serial steps or independent jobs"
                for line in _parallel_lines(document)
            )
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize(
    "source, expected",
    [
        ("steps:\n  - parallel:\n      - uses: actions/attest@pin\n", [2]),
        # Run-only groups also rewrite the event file before a later action.
        (
            (
                "steps:\n  - parallel:\n      - run: echo build\n"
                "  - uses: actions/upload-artifact@pin\n"
            ),
            [2],
        ),
        ("runs:\n  steps:\n    - parallel:\n        - run: echo build\n", [3]),
        (
            (
                "strategy:\n  max-parallel: 2\nsteps:\n"
                "  - run: |\n      echo 'parallel: is shell text'\n"
                "  - uses: actions/attest@pin\n"
            ),
            [],
        ),
    ],
)
def test_guard_catches_parallel_groups_without_banning_job_concurrency(
    source: str, expected: list[int]
) -> None:
    document = yaml.compose(source)
    assert document is not None
    assert _parallel_lines(document) == expected
