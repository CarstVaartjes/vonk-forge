"""Prove the coordination scanner catches each boundary and allows each allowed shape.

A gate that cannot fail on the wrong implementation is ceremony, so every rule
this module checks is exercised twice: once against a fixture that violates it
and again against the closest allowed shape. The fixtures are the wrong and
right implementations of the same function, which is what the gate has to
separate.
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent, indent

import pytest

from .coordination_boundaries import (
    BASELINE_PATH,
    BLOCKING_ARTIFACT_LOCK,
    NESTED_ARTIFACT_LOCK,
    SQL_TRANSACTION_SPANS_ARTIFACT_LOCK,
    SQL_TRANSACTION_SPANS_EXTERNAL_WORK,
    Site,
    evaluate_coordination_gate,
    load_baseline,
    render_baseline,
    scan_coordination_sites,
    scan_source,
)


def _scanned(source: str) -> list[tuple[str, int, str]]:
    return [
        (site.kind, site.line, site.detail)
        for site in scan_source(dedent(source), path="control/src/sample.py")
    ]


def _wrap(body: str) -> str:
    """Put an 8-space-indented body inside ``Service.run``."""

    return (
        "import fcntl\n"
        "import os\n"
        "import httpx\n"
        "import subprocess\n"
        "import time\n"
        "\n"
        "\n"
        "class Store:\n"
        "    def load(self, digest):\n"
        "        return open(digest, 'rb')\n"
        "\n"
        "\n"
        "class Service:\n"
        "    artifact_lock = None\n"
        "\n"
        "    def _session(self, *, write=False):\n"
        "        return self.sessions()\n"
        "\n"
        "    def run(self):\n" + indent(dedent(body), "        ")
    )


def test_external_io_inside_a_read_transaction_is_a_site() -> None:
    assert _scanned(
        _wrap(
            """\
                with self._session() as session:
                    opened = open('model.bin', 'rb')
                    session.add(opened)
            """
        )
    ) == [(SQL_TRANSACTION_SPANS_EXTERNAL_WORK, 21, "external call open")]


def test_external_io_after_the_transaction_closes_is_allowed() -> None:
    assert (
        _scanned(
            _wrap(
                """\
                    with self._session() as session:
                        row = session.get('thing')
                    opened = open('model.bin', 'rb')
                """
            )
        )
        == []
    )


def test_http_process_and_sleep_inside_a_transaction_are_sites() -> None:
    kinds = _scanned(
        _wrap(
            """\
                with self.sessions.begin() as session:
                    session.add(1)
                    httpx.get('https://example.invalid/manifest')
                    subprocess.run(['skopeo', 'copy'])
                    time.sleep(5)
            """
        )
    )
    assert [detail for _kind, _line, detail in kinds] == [
        "external call httpx.get",
        "external call subprocess.run",
        "external call time.sleep",
    ]
    assert {kind for kind, _line, _detail in kinds} == {
        SQL_TRANSACTION_SPANS_EXTERNAL_WORK
    }


def test_in_memory_hashing_and_validation_inside_a_transaction_are_allowed() -> None:
    assert (
        _scanned(
            _wrap(
                """\
                    import hashlib
                    from vonk_forge_contracts import content_sha256

                    with self._session() as session:
                        row = session.get('thing')
                        digest = hashlib.sha256(b'bytes').hexdigest()
                        canonical = content_sha256(row)
                        session.add((digest, canonical))
                """
            )
        )
        == []
    )


def test_artifact_lock_inside_a_transaction_is_a_site() -> None:
    assert _scanned(
        _wrap(
            """\
                with self._session() as session:
                    session.add(1)
                    with self.artifact_lock:
                        pass
            """
        )
    ) == [
        (
            SQL_TRANSACTION_SPANS_ARTIFACT_LOCK,
            22,
            "artifact lock self.artifact_lock",
        )
    ]


def test_artifact_lock_acquired_after_commit_is_allowed() -> None:
    assert (
        _scanned(
            _wrap(
                """\
                    with self._session() as session:
                        session.add(1)
                    with self.artifact_lock:
                        pass
                """
            )
        )
        == []
    )


def test_nonblocking_flock_outside_a_transaction_is_allowed() -> None:
    assert (
        _scanned(
            _wrap(
                """\
                    descriptor = os.open('object', os.O_RDONLY)
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                """
            )
        )
        == []
    )


def test_blocking_flock_is_a_site() -> None:
    assert _scanned(
        _wrap(
            """\
                descriptor = os.open('object', os.O_RDONLY)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            """
        )
    ) == [(BLOCKING_ARTIFACT_LOCK, 21, "blocking artifact lock fcntl.flock")]


def test_a_lock_and_a_session_in_one_with_block_are_allowed() -> None:
    """Acquired together, the lock is outside the transaction it validates."""

    assert (
        _scanned(
            _wrap(
                """\
                    with self.artifact_lock, self._session(write=True) as session:
                        session.add(1)
                """
            )
        )
        == []
    )


def test_a_lock_acquired_inside_a_transaction_body_is_a_site() -> None:
    """The forbidden direction: SQL is already open when the lock is taken."""

    assert _scanned(
        _wrap(
            """\
                with self._session() as session:
                    session.add(1)
                    with self.artifact_lock:
                        pass
            """
        )
    ) == [
        (
            SQL_TRANSACTION_SPANS_ARTIFACT_LOCK,
            22,
            "artifact lock self.artifact_lock",
        )
    ]


def test_nested_lock_blocks_are_a_site() -> None:
    assert _scanned(
        _wrap(
            """\
                with self.artifact_lock:
                    with self.partial_lock:
                        pass
            """
        )
    ) == [
        (
            NESTED_ARTIFACT_LOCK,
            21,
            "artifact locks held together: self.artifact_lock, self.partial_lock",
        )
    ]


def test_two_locks_in_sequence_are_allowed() -> None:
    assert (
        _scanned(
            _wrap(
                """\
                    with self.artifact_lock:
                        pass
                    with self.artifact_lock:
                        pass
                """
            )
        )
        == []
    )


def test_helper_mediated_storage_work_inside_a_transaction_is_a_site() -> None:
    assert _scanned(
        _wrap(
            """\
                with self._session() as session:
                    session.add(1)
                    self._managed_cached_objects(manifest)
            """
        )
    ) == [
        (
            SQL_TRANSACTION_SPANS_EXTERNAL_WORK,
            22,
            "external call self._managed_cached_objects",
        )
    ]


def test_scan_source_fails_on_the_previous_admission_implementation() -> None:
    """The exact wrong implementation this gate was written to catch."""

    wrong = _wrap(
        """\
            with self._session() as session:
                rows = session.scalars(select_one())
                for row in rows:
                    descriptor = os.open(row.sha256, os.O_RDONLY)
                    cached.add(os.fstat(descriptor))
        """
    )
    fixed = _wrap(
        """\
            with self._session() as session:
                rows = session.scalars(select_one())
                receipts = list(rows)
            for sha256 in receipts:
                descriptor = os.open(sha256, os.O_RDONLY)
                cached.add(os.fstat(descriptor))
        """
    )
    assert [kind for kind, _line, _detail in _scanned(wrong)] == [
        SQL_TRANSACTION_SPANS_EXTERNAL_WORK,
        SQL_TRANSACTION_SPANS_EXTERNAL_WORK,
    ]
    assert _scanned(fixed) == []


def test_the_repository_source_is_clean_or_exactly_baselined() -> None:
    """The gate itself: no new site, and no stale baseline entry."""

    sites = scan_coordination_sites()
    messages = evaluate_coordination_gate(sites, load_baseline(BASELINE_PATH))
    assert messages == []


def test_gate_rejects_a_new_site_and_a_stale_entry() -> None:
    reviewed = Site(
        path="control/src/sample.py",
        line=10,
        kind=SQL_TRANSACTION_SPANS_EXTERNAL_WORK,
        function="run",
        detail="external call open",
    )
    baseline = [{**reviewed.identity, "reason": "reviewed"}]
    assert evaluate_coordination_gate([reviewed], baseline) == []
    fresh = Site(
        path="control/src/sample.py",
        line=11,
        kind=SQL_TRANSACTION_SPANS_EXTERNAL_WORK,
        function="run",
        detail="external call os.stat",
    )
    # The replacement is at a new line, so the old entry is stale and the new
    # site is unreviewed: a same-function swap cannot keep the baseline green.
    messages = evaluate_coordination_gate([fresh], baseline)
    assert len(messages) == 2
    assert "new coordination violation" in messages[0]
    assert "no longer occurs" in messages[1]


def test_baseline_loader_rejects_an_unexplained_or_unknown_entry(
    tmp_path: Path,
) -> None:
    def write(document: object) -> Path:
        path = tmp_path / "baseline.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    entry = {
        "path": "control/src/sample.py",
        "line": 10,
        "kind": SQL_TRANSACTION_SPANS_EXTERNAL_WORK,
        "function": "run",
        "detail": "external call open",
    }
    with pytest.raises(ValueError, match="written reason"):
        load_baseline(write({"schema": 1, "sites": [entry]}))
    with pytest.raises(ValueError, match="unknown site kind"):
        load_baseline(
            write({"schema": 1, "sites": [{**entry, "kind": "made-up", "reason": "x"}]})
        )
    assert load_baseline(write({"schema": 1, "sites": [{**entry, "reason": "x"}]}))


def test_rendered_baseline_round_trips() -> None:
    site = Site(
        path="control/src/sample.py",
        line=10,
        kind=SQL_TRANSACTION_SPANS_EXTERNAL_WORK,
        function="run",
        detail="external call open",
    )
    document = json.loads(
        render_baseline([site], {SQL_TRANSACTION_SPANS_EXTERNAL_WORK: "reviewed"})
    )
    assert document["schema"] == 1
    assert document["sites"] == [site.identity | {"reason": "reviewed"}]
