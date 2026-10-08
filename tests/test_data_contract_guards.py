"""Every data class has one definition: the Pydantic contract model.

The scans live in ``tools/contract_scans.py``; each runs against a reviewed
allowlist that only shrinks.  These tests catch an implementation that adds a
hand-written copy (a serde struct, a TypeScript shape, a second Pydantic model)
or that lets an allowlist rot.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from control.tests.registry_storage import read_registry
from tools import contract_scans as scans

ROOT = scans.ROOT


def _entries(path: Path) -> list[dict[str, str]]:
    document = read_registry(path)
    return document["entries"]


def _assert_allowlist(found: list[tuple[str, str]], path: Path) -> None:
    entries = _entries(path)
    assert all(entry["reason"].strip() for entry in entries), (
        "every entry needs a reason"
    )
    listed = {(entry["file"], entry["type"]) for entry in entries}
    assert len(listed) == len(entries), "duplicate allowlist entry"
    unlisted = sorted(set(found) - listed)
    stale = sorted(listed - set(found))
    assert not unlisted, f"not in {path.name}; generate from the contract: {unlisted}"
    assert not stale, f"stale entries in {path.name}; delete them: {stale}"


def test_rust_serde_types_are_generated_or_allowlisted_with_a_reason() -> None:
    _assert_allowlist(scans.rust_serde_types(), scans.RUST_ALLOWLIST)


def test_typescript_shapes_are_generated_or_allowlisted_with_a_reason() -> None:
    _assert_allowlist(scans.typescript_shapes(), scans.TS_ALLOWLIST)


def test_python_models_live_in_the_registry_once() -> None:
    assert scans.python_registry_problems() == []


def test_the_inventory_document_names_every_registered_module_and_exception() -> None:
    document = (ROOT / "docs/data-contracts.md").read_text(encoding="utf-8")
    registry = read_registry(scans.PY_REGISTRY)
    missing = [
        item
        for item in [
            *(entry["module"] for entry in registry["modules"]),
            *(
                f"`{entry['file']}` | `{entry['type']}`"
                for path in (scans.RUST_ALLOWLIST, scans.TS_ALLOWLIST)
                for entry in _entries(path)
            ),
        ]
        if item not in document
    ]
    assert not missing, f"docs/data-contracts.md does not list: {missing}"


# -- the scanners catch what they claim to --------------------------------------


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


def test_rust_scan_finds_multiline_derives_and_manual_impls_but_not_generated(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "rust/crates/a/src/lib.rs",
        """
        #[derive(
            Debug,
            serde::Serialize,
        )]
        pub struct Wire { a: u8 }
        #[derive(Debug)]
        struct Plain { a: u8 }
        impl<'de> serde::Deserialize<'de> for Manual {}
        """,
    )
    _write(
        tmp_path,
        "rust/crates/a/src/generated.rs",
        "#[derive(serde::Serialize)]\npub struct Gen {}\n",
    )
    assert scans.rust_serde_types(tmp_path) == [
        ("rust/crates/a/src/lib.rs", "Manual"),
        ("rust/crates/a/src/lib.rs", "Wire"),
    ]


def test_typescript_scan_finds_type_literals_and_interfaces_but_not_aliases(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "control/web/src/api/x.ts",
        """
        export type Token = {expiresAt: string};
        export interface Api<T> extends Base {
          call(): T;
        }
        export type Alias = components["schemas"]["Alias"];
        type Union = {a: 1} | {b: 2};
        """,
    )
    _write(tmp_path, "control/web/src/api/generated.d.ts", "export type G = {a: 1};\n")
    _write(tmp_path, "control/web/src/api/x.test.ts", "type T = {a: 1};\n")
    assert scans.typescript_shapes(tmp_path) == [
        ("control/web/src/api/x.ts", "Api"),
        ("control/web/src/api/x.ts", "Token"),
        ("control/web/src/api/x.ts", "Union"),
    ]


def test_python_registry_rejects_unregistered_and_duplicate_models(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "agent_protocol/src/p/a.py",
        """
        from pydantic import BaseModel
        class Thing(BaseModel):
            name: str
        """,
    )
    _write(
        tmp_path,
        "control/src/c/elsewhere.py",
        """
        from p.a import Thing
        class Copy(Thing):
            pass
        class Other(BaseModel):
            name: str
        """,
    )
    registry = {"modules": [{"module": "agent_protocol/src/p/a.py"}]}
    problems = scans.python_registry_problems(tmp_path, registry)
    assert any("elsewhere.py: model Copy" in item for item in problems)
    assert any("elsewhere.py: model Other" in item for item in problems)

    registry["modules"].append({"module": "control/src/c/elsewhere.py"})
    problems = scans.python_registry_problems(tmp_path, registry)
    assert any(
        "duplicate model definitions" in item and "Other" in item for item in problems
    )
