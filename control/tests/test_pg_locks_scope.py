"""Every pg_locks read is scoped to this database.

pg_locks and pg_stat_activity list every database in the cluster, while an
advisory key only means something inside one database; an unscoped read names
another database's holder (CI shard 4 of #1908).
"""

from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "vonk_control"


def _statements(text: str) -> list[str]:
    """Each adjacent run of string literals that mentions pg_locks."""
    lines = text.splitlines()
    found = []
    for index, line in enumerate(lines):
        if "pg_locks" in line:
            found.append(" ".join(lines[max(index - 2, 0) : index + 12]))
    return found


def test_every_pg_locks_read_names_the_current_database() -> None:
    unscoped = [
        f"{path.relative_to(SOURCE)}: {statement[:120]}"
        for path in sorted(SOURCE.rglob("*.py"))
        for statement in _statements(path.read_text(encoding="utf-8"))
        if "current_database()" not in statement
    ]
    assert not unscoped, "pg_locks read without current_database(): " + "; ".join(
        unscoped
    )
