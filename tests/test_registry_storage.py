"""A lowering in one module must never rewrite another module's allowance."""

from pathlib import Path

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
