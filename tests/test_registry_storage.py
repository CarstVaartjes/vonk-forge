"""A lowering in one module must never rewrite another module's allowance."""

import multiprocessing
import os
from pathlib import Path

import pytest

from control.tests import registry_storage
from control.tests.registry_storage import read_registry, write_registry


def test_module_lowering_is_local_and_empty_modules_are_deleted(tmp_path: Path) -> None:
    registry = tmp_path / "allowlist"
    original = {
        "schema": 1,
        "debt": [
            {"path": "src/a.py", "count": 2},
            {"path": "src/b.py", "count": 3},
        ],
        "content_identities": {"src/a.py": {"f": "aaa"}, "src/b.py": {"g": "bbb"}},
    }
    write_registry(registry, original)
    peer = registry / "src/b.py.json"
    global_file = registry / "_global.json"
    peer_before = (peer.read_bytes(), peer.stat().st_mtime_ns)
    global_before = (global_file.read_bytes(), global_file.stat().st_mtime_ns)
    lowered = {
        **original,
        "debt": [{"path": "src/b.py", "count": 3}],
        "content_identities": {"src/b.py": {"g": "bbb"}},
    }
    write_registry(registry, lowered)
    assert not (registry / "src/a.py.json").exists()
    assert (peer.read_bytes(), peer.stat().st_mtime_ns) == peer_before
    assert (global_file.read_bytes(), global_file.stat().st_mtime_ns) == global_before
    assert read_registry(registry) == lowered


def test_family_sites_and_scope_are_local_and_totals_are_derived(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "blockers"
    document = {
        "schema": 1,
        "scope": {"audited_paths": ["src/a.py"], "guard_paths": ["src/b.py"]},
        "debt_ceiling": {"total": 2, "unaudited": 3, "note": "reviewed"},
        "max_debt": 0,
        "categorized_raises": {"ceiling": 1, "grandfathered": {"src/b.py": 1}},
        "operator_waits": [],
        "fail_closed": [
            {
                "family": "example",
                "category": "bookkeeping-debt",
                "reason": "reviewed",
                "sites": [
                    ["src/a.py", "E", "f", "x", 2],
                    ["src/b.py", "E", "g", "x", 3],
                ],
            }
        ],
    }
    write_registry(registry, document)
    assert read_registry(registry) == document
    global_before = (registry / "_global.json").read_bytes()
    peer = registry / "src/b.py.json"
    peer_before = (peer.read_bytes(), peer.stat().st_mtime_ns)
    document["fail_closed"][0]["sites"][0][4] = 1
    document["debt_ceiling"]["total"] = 1
    write_registry(registry, document)
    assert read_registry(registry) == document
    assert (registry / "_global.json").read_bytes() == global_before
    assert (peer.read_bytes(), peer.stat().st_mtime_ns) == peer_before


def test_rewriting_identical_inventory_preserves_all_files(tmp_path: Path) -> None:
    registry = tmp_path / "baseline"
    write_registry(registry, {"schema": 1, "files": {"src/b.py": 4, "src/a.py": 2}})
    before = {
        p: (p.read_bytes(), p.stat().st_mtime_ns) for p in registry.rglob("*.json")
    }
    write_registry(registry, read_registry(registry))
    assert {
        p: (p.read_bytes(), p.stat().st_mtime_ns) for p in registry.rglob("*.json")
    } == before


def test_entry_order_does_not_rewrite_shards(tmp_path: Path) -> None:
    registry = tmp_path / "inventory"
    entries = [
        {"path": "src/a.py", "function": "z", "count": 2},
        {"path": "src/a.py", "function": "a", "count": 1},
        {"path": "src/b.py", "function": "b", "count": 3},
    ]
    write_registry(registry, {"schema": 1, "debt": entries})
    before = {
        p: (p.read_bytes(), p.stat().st_mtime_ns) for p in registry.rglob("*.json")
    }
    write_registry(registry, {"debt": list(reversed(entries)), "schema": 1})
    assert {
        p: (p.read_bytes(), p.stat().st_mtime_ns) for p in registry.rglob("*.json")
    } == before


@pytest.mark.parametrize("fault", ["missing", "corrupt", "conflict", "derived"])
def test_incomplete_observation_ends_and_an_explicit_replacement_repairs_it(
    tmp_path, fault
):
    registry = tmp_path / "inventory"
    original = {"schema": 1, "files": {"src/a.py": 1, "src/b.py": 2}}
    write_registry(registry, original)
    shard = registry / "src/a.py.json"
    if fault == "missing":
        shard.unlink()
    elif fault == "corrupt":
        shard.write_text("{")
    else:
        text = (
            '{"schema":2,"files":{"src/a.py":1}}'
            if fault == "conflict"
            else '{"fail_closed":null}'
        )
        shard.write_text(text)
        # A coherent fingerprint alone cannot bless conflicting or malformed
        # contents: exercise assembly after completeness validation as well.
        fragments = {
            p.relative_to(registry).as_posix(): p.read_text()
            for p in registry.rglob("*.json")
        }
        (registry / ".manifest").write_text(
            registry_storage._manifest(fragments).model_dump_json()
        )
    calls = []
    observe = registry_storage._read_once

    def read(path):
        calls.append(path)
        return observe(path)

    from unittest.mock import patch

    with patch.object(registry_storage, "_read_once", read):
        with pytest.raises(RuntimeError):
            read_registry(registry)
        assert len(calls) == 2
    write_registry(registry, original)
    assert read_registry(registry) == original


def test_disappearance_during_read_reobserves_a_complete_view(tmp_path, monkeypatch):
    registry = tmp_path / "inventory"
    original = {"schema": 1, "files": {"src/a.py": 1}}
    write_registry(registry, original)
    shard = registry / "src/a.py.json"
    saved = shard.read_bytes()
    read = Path.read_text
    calls = []

    def disappearing(path, *args, **kwargs):
        if path == shard:
            calls.append(path)
            if len(calls) == 1:
                shard.unlink()
                try:
                    return read(path, *args, **kwargs)
                finally:
                    shard.write_bytes(saved)
        return read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", disappearing)
    assert read_registry(registry) == original
    assert len(calls) == 2
    assert read_registry(registry) == original


def test_stale_module_lowering_preserves_newer_peer_update_and_insertion(tmp_path):
    registry = tmp_path / "inventory"
    write_registry(registry, {"schema": 1, "files": {"src/a.py": 2, "src/b.py": 3}})
    a = read_registry(registry)
    b = read_registry(registry)
    b["files"]["src/b.py"] = 4
    b["files"]["src/c.py"] = 5
    write_registry(registry, b)
    a["files"]["src/a.py"] = 1
    write_registry(registry, a)
    assert read_registry(registry)["files"] == {
        "src/a.py": 1,
        "src/b.py": 4,
        "src/c.py": 5,
    }


def test_superseded_same_module_write_ends_then_fresh_write_is_admitted(tmp_path):
    registry = tmp_path / "inventory"
    write_registry(registry, {"schema": 1, "files": {"src/a.py": 3}})
    a, b = read_registry(registry), read_registry(registry)
    b["files"]["src/a.py"] = 2
    write_registry(registry, b)
    a["files"]["src/a.py"] = 1
    with pytest.raises(RuntimeError):
        write_registry(registry, a)
    fresh = read_registry(registry)
    assert fresh["files"]["src/a.py"] == 2
    fresh["files"]["src/a.py"] = 1
    write_registry(registry, fresh)
    assert read_registry(registry)["files"]["src/a.py"] == 1


def _die_during_publication(registry):
    real = registry_storage._atomic_write

    def die(target, data):
        real(target, data)
        if target.name == "a.py.json":
            os._exit(19)

    registry_storage._atomic_write = die
    current = read_registry(registry)
    current["files"]["src/a.py"] = 1
    current["files"]["src/b.py"] = 1
    write_registry(registry, current)


def test_process_death_preserves_complete_view_and_next_writer_reconciles(tmp_path):
    registry = tmp_path / "inventory"
    original = {"schema": 1, "files": {"src/a.py": 3, "src/b.py": 3}}
    write_registry(registry, original)
    process = multiprocessing.get_context("spawn").Process(
        target=_die_during_publication, args=(registry,)
    )
    process.start()
    try:
        process.join(timeout=15)
        assert process.exitcode == 19
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=15)
    assert read_registry(registry) == original
    fresh = read_registry(registry)
    fresh["files"]["src/c.py"] = 2
    write_registry(registry, fresh)
    assert read_registry(registry)["files"] == {
        "src/a.py": 1,
        "src/b.py": 1,
        "src/c.py": 2,
    }
    assert not (registry / ".publication").exists()


def test_storage_failure_ends_without_exposing_partial_state_and_fresh_write_repairs(
    tmp_path, monkeypatch
):
    registry = tmp_path / "inventory"
    original = {"schema": 1, "files": {"src/a.py": 3, "src/b.py": 3}}
    write_registry(registry, original)
    current = read_registry(registry)
    current["files"]["src/a.py"] = 1
    current["files"]["src/b.py"] = 1
    real = registry_storage._atomic_write
    calls = []

    def failed(target, data):
        if target.name == "b.py.json":
            calls.append(target)
            raise OSError("storage unavailable")
        real(target, data)

    monkeypatch.setattr(registry_storage, "_atomic_write", failed)
    with pytest.raises(RuntimeError):
        write_registry(registry, current)
    assert len(calls) == 2
    assert read_registry(registry) == original
    monkeypatch.setattr(registry_storage, "_atomic_write", real)
    write_registry(registry, read_registry(registry))
    assert read_registry(registry)["files"] == {"src/a.py": 1, "src/b.py": 1}
