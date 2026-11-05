"""A download or repair operation is created only by ``admit_transfer``.

Two live transfers of one model set split bandwidth and block each other on
per-object writer locks (2026-11-05). ``admit_transfer`` takes the model-set
gate and attaches to the live transfer; a second creation site would skip that.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "control/src"

#: file (relative to control/src) -> why it may call ``new_operation`` directly
EXEMPT = {
    "vonk_control/model_cache/transfer_admission.py": "the one admission helper",
    "vonk_control/model_cache/removal_acceptance.py": (
        "a removal is not a transfer; it has its own fence"
    ),
    "vonk_control/lifecycle/model_cache.py": "defines the adapter constructor",
    "vonk_control/lifecycle/run_switch.py": "defines the run-switch job constructor",
    "vonk_control/run_switch_operations/reservation.py": (
        "creates a run-switch job row, not a model transfer"
    ),
    "vonk_control/run_switch_operations/cancellation_retry.py": (
        "creates a run-switch job row, not a model transfer"
    ),
}


def test_only_the_admission_helper_creates_transfer_operations() -> None:
    offenders = sorted(
        path.relative_to(SOURCES).as_posix()
        for path in SOURCES.rglob("*.py")
        if path.relative_to(SOURCES).as_posix() not in EXEMPT
        and "new_operation(" in path.read_text()
    )
    assert not offenders, f"create transfers through admit_transfer: {offenders}"
    assert all(reason.strip() for reason in EXEMPT.values())
    assert all((SOURCES / name).is_file() for name in EXEMPT), "stale exemption"
