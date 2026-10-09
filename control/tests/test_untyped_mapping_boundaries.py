"""Untyped mapping syntax is detected; typed annotations remain valid."""

from __future__ import annotations

from textwrap import dedent

import pytest

from .untyped_mapping_boundaries import (
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


def test_extensionless_python_tool_is_not_invisible(tmp_path, monkeypatch):
    from . import untyped_mapping_boundaries as scanner

    monkeypatch.setattr(scanner, "REPO_ROOT", tmp_path)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    tool = scripts / "check-sample"
    tool.write_text(
        "#!/usr/bin/env -S uv run python\ndef f(x: dict[str, object]): pass\n"
    )
    assert scanner.scan_sites([scripts])[0].path == "scripts/check-sample"
    tool.write_text("#!/usr/bin/env -S uv run python\ndef f(x: dict[str, str]): pass\n")
    assert scanner.scan_sites([scripts]) == []


def test_generated_extension_dicts_do_not_hide_authored_cli_debt(tmp_path, monkeypatch):
    from . import untyped_mapping_boundaries as scanner

    monkeypatch.setattr(scanner, "REPO_ROOT", tmp_path)
    root = tmp_path / "src" / "cluster_profiles"
    generated = root / "generated_control"
    generated.mkdir(parents=True)
    annotation = "def f(data: dict[str, object]): pass\n"
    (generated / "generated.py").write_text(annotation)
    (root / "authored.py").write_text(annotation)
    assert [site.path for site in scanner.scan_sites([root])] == [
        "src/cluster_profiles/authored.py"
    ]
