"""Prove the untyped-mapping ratchet fails on an increase and passes a typed change."""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import pytest

from .untyped_mapping_boundaries import (
    Site,
    evaluate_gate,
    load_allowlist,
    scan_sites,
    scan_source,
)

PATH = "control/src/vonk_control/sample.py"


def _scanned(source: str) -> list[str]:
    return [site.annotation for site in scan_source(dedent(source), path=PATH)]


@pytest.mark.parametrize(
    "annotation",
    [
        "Mapping[str, object]",
        "Mapping[str, Any]",
        "dict[str, object]",
        "dict[str, Any]",
        "MutableMapping[str, object]",
        "typing.Dict[str, typing.Any]",
        "dict[str, object | None]",
        "dict[str, JsonValue]",
        "Mapping[str, list[Any]]",
        "Mapping[str, Optional[object]]",
    ],
)
def test_an_untyped_mapping_is_a_site(annotation: str) -> None:
    assert _scanned(f"def f(value: {annotation}) -> None: ...\n") == [
        annotation if "typing" not in annotation else "typing.Dict[str, typing.Any]"
    ]


def test_nested_and_assigned_annotations_are_sites() -> None:
    sites = _scanned(
        """
        class A:
            items: list[dict[str, object]]
        def f() -> dict[str, dict[str, object]]:
            local: Mapping[str, Any] = {}
        """
    )
    # A nested untyped mapping is reported once, by its outermost annotation.
    assert sorted(sites) == [
        "Mapping[str, Any]",
        "dict[str, dict[str, object]]",
        "dict[str, object]",
    ]


@pytest.mark.parametrize(
    "annotation",
    ["dict[str, str]", "Mapping[str, int]", "dict[str, Model]", "Mapping[int, object]"],
)
def test_a_typed_mapping_is_not_a_site(annotation: str) -> None:
    assert _scanned(f"def f(value: {annotation}) -> None: ...\n") == []


def _site(annotation: str = "dict[str, object]", line: int = 3) -> Site:
    return Site(path=PATH, function="f", annotation=annotation, line=line)


def test_gate_rejects_an_increase_and_a_stale_count() -> None:
    allowlist = {"permanent": [], "debt": [{"path": PATH, "count": 1}]}
    assert evaluate_gate([_site()], allowlist) == []
    assert evaluate_gate([_site(), _site(line=9)], allowlist)
    assert evaluate_gate([], allowlist)
    assert evaluate_gate([_site()], {"permanent": [], "debt": []})


def test_permanent_entries_are_keyed_on_function_and_annotation() -> None:
    entry = {
        "path": PATH,
        "function": "f",
        "annotation": "dict[str, object]",
        "count": 1,
        "reason": "external passthrough",
    }
    allowlist = {"permanent": [entry], "debt": []}
    assert evaluate_gate([_site(line=40)], allowlist) == []
    assert evaluate_gate([_site(), _site(line=9)], allowlist)
    assert evaluate_gate([], allowlist)


def test_loader_requires_a_reason(tmp_path: Path) -> None:
    entry = {"path": PATH, "function": "f", "annotation": "x", "count": 1}
    path = tmp_path / "a.json"
    path.write_text(
        json.dumps({"schema": 1, "permanent": [entry], "debt": []}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="written reason"):
        load_allowlist(path)


def test_untyped_mapping_annotations_only_go_down() -> None:
    """The gate itself, over ``control/src`` and ``agent_protocol/src``."""

    assert evaluate_gate(scan_sites(), load_allowlist()) == []
