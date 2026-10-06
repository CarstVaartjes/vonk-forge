"""The allowlist merge tool merges sites and ceilings as deltas."""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[2] / "tools" / "merge-blocker-allowlist"


def _tool():
    loader = importlib.machinery.SourceFileLoader("merge_blocker_allowlist", str(TOOL))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _document(sites: dict[str, list[list[object]]], total: int) -> dict:
    return {
        "schema": 1,
        "scope": {},
        "max_debt": 1,
        "debt_ceiling": {"total": total, "unaudited": total, "note": "n"},
        "categorized_raises": {"note": "n", "ceiling": total, "grandfathered": {}},
        "operator_waits": [],
        "fail_closed": [
            {"family": name, "category": "bookkeeping-debt", "reason": "r", "sites": s}
            for name, s in sites.items()
        ],
    }


def test_sites_and_ceilings_merge_as_deltas() -> None:
    base = _document({"f": [["a.py", "E", "g", "x.y", 2]]}, 10)
    ours = _document({"f": [["a.py", "E", "g", "x.y", 1]]}, 9)
    theirs = _document(
        {"f": [["a.py", "E", "g", "x.y", 2], ["b.py", "E", "h", "x.z", 3]]}, 13
    )
    merged = _tool().merge(base, ours, theirs)
    assert merged["fail_closed"][0]["sites"] == [
        ["a.py", "E", "g", "x.y", 1],
        ["b.py", "E", "h", "x.z", 3],
    ]
    assert merged["debt_ceiling"]["total"] == 12


def test_a_site_removed_on_both_sides_stays_removed() -> None:
    base = _document({"f": [["a.py", "E", "g", "x.y", 1]]}, 1)
    empty = _document({}, 0)
    assert _tool().merge(base, empty, empty)["fail_closed"] == []


def test_both_sides_changing_a_section_is_refused() -> None:
    base = _document({}, 1)
    ours, theirs = _document({}, 1), _document({}, 1)
    ours["max_debt"], theirs["max_debt"] = 2, 3
    with pytest.raises(ValueError):
        _tool().merge(base, ours, theirs)
