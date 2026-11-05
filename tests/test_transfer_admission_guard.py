"""A download or repair operation is created only by ``admit_transfer``.

Two live transfers of one model set split bandwidth and block each other on
per-object writer locks (2026-11-05). ``admit_transfer`` takes the model-set
gate and attaches to the live transfer; a second creation site would skip that.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "control/src/vonk_control/model_cache"

#: file -> why it may call ``ModelCacheAdapter.new_operation`` directly
EXEMPT = {
    "transfer_admission.py": "the one admission helper",
    "removal_acceptance.py": "a removal is not a transfer; it has its own fence",
}


def test_only_the_admission_helper_creates_transfer_operations() -> None:
    offenders = sorted(
        path.name
        for path in CACHE.glob("*.py")
        if path.name not in EXEMPT
        and "ModelCacheAdapter.new_operation(" in path.read_text()
    )
    assert not offenders, f"create transfers through admit_transfer: {offenders}"
