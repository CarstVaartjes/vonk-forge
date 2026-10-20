"""Core row bounds must continue, even when the first page remains protected."""

from pathlib import Path

import pytest

from control.tests import collection_continuation as guard
from control.tests.added_line_guards import check_source
from control.tests.parsed_sources import parse_file
from control.tests.principle_guards import rust_test_lines

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "path,source",
    [
        ("control/src/profiles.py", "def profiles():\n    return query.limit(128)\n"),
        (
            "control/src/profiles.py",
            "def listing(profiles):\n    return profiles[:128]\n",
        ),
        (
            "control/src/profiles.py",
            'def list_profiles():\n    return execute("SELECT * FROM profiles LIMIT 128")\n',
        ),
        ("rust/agent.rs", 'fn containers() { args.push("--last"); }'),
        (
            "rust/agent.rs",
            "fn installations() { names.take(MAX_REPORTED_INSTALLATIONS); }",
        ),
        ("rust/agent.rs", "fn run_scopes() { scopes.take(4096); }"),
    ],
)
def test_new_core_prefix_without_continuation_fails(path, source):
    # Comments, including a promise of future continuation, cannot silence it.
    source = source + (
        "\n# next_cursor\n" if path.endswith(".py") else "\n// next_cursor\n"
    )
    lines = set(range(1, len(source.splitlines()) + 1))
    assert guard.scan_source(path, source)
    assert check_source(path, source, lines, modes=("collections",))
    assert not check_source(path, source, set(), modes=("collections",))


@pytest.mark.parametrize(
    "path,source",
    [
        (
            "control/src/profiles.py",
            "def profiles():\n    page = query.limit(128)\n    return page, next_after(page)\n",
        ),
        (
            "rust/agent.rs",
            'fn containers(before: Cursor) { args.push("--last"); args.push(before); }',
        ),
        (
            "rust/agent.rs",
            "fn rotating_installations() { names.take(MAX_REPORTED_INSTALLATIONS); save(cursor); }",
        ),
        ("rust/agent.rs", "fn read_profile() { file.take(4096).read_to_end(bytes); }"),
    ],
)
def test_explicit_continuation_and_byte_reads_are_allowed(path, source):
    assert not guard.scan_source(path, source)


def test_core_collection_producers_continue_in_the_whole_tree():
    hits = []
    for directory in ("control/src", "src", "rust/crates"):
        for path in (ROOT / directory).rglob("*"):
            relative = str(path.relative_to(ROOT))
            if path.suffix not in {".py", ".rs"} or "tests" in path.parts:
                continue
            if path.suffix == ".py":
                parsed = parse_file(path)
                lines = guard.scan_source(relative, parsed.source, parsed.tree)
            else:
                source = path.read_text()
                lines = set(guard.scan_rust(source)) - rust_test_lines(source)
            hits.extend(f"{relative}:{line}" for line in lines)
    assert not hits, "Core limits require cursor, next page or rotation:\n" + "\n".join(
        hits
    )
