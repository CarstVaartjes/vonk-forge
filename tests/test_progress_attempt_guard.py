"""Monotonic agent measurements must never compare different attempts."""

from pathlib import Path

from control.tests.progress_attempt_boundaries import scan_source

ROOT = Path(__file__).resolve().parents[1]


def test_agent_progress_validators_have_an_attempt_key_boundary():
    violations = []
    for directory in ("control/src", "agent_protocol/src"):
        for path in (ROOT / directory).rglob("*.py"):
            for line in scan_source(path.read_text()):
                violations.append(f"{path.relative_to(ROOT)}:{line}")
    assert not violations, (
        "monotonic progress validation without attempt key: " + ", ".join(violations)
    )


def test_guard_catches_counter_item_activity_and_checkpoint_refusals():
    for counter in (
        "completed_bytes",
        "completed_items",
        "activity_sequence",
        "sequence",
    ):
        body = f'    if current.{counter} < previous.{counter}:\n        raise ValueError("backwards")\n'
        assert scan_source("def ingress(previous, current):\n" + body)
        assert not scan_source(
            "def ingress(previous, current):\n    if not same_progress_attempt(previous, current):\n        return current\n"
            + body
        )
    # A schema bound protects actual resource/contract validity.
    assert not scan_source(
        'def bound(current):\n    if current.completed_bytes > current.total_bytes:\n        raise ValueError("total")\n'
    )
