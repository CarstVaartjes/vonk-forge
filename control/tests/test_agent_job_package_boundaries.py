"""Keep the queue split reviewable and prevent raw persisted-document readers."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from .parsed_sources import parsed_tree

pytestmark = pytest.mark.usefixtures("parsed_repository")
PACKAGE = "control/src/vonk_control/agent_jobs"
ROWS = frozenset(
    {
        "operation",
        "job",
        "parent",
        "old_parent",
        "source",
        "source_parent",
        "stop_parent",
        "child",
        "old",
        "current_parent",
        "current",
        "attempt",
        "parent_hint",
    }
)


def raw_reads(source: str) -> list[str]:
    return [
        ast.unparse(node)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute)
        and isinstance(node.ctx, ast.Load)
        and isinstance(node.value, ast.Name)
        and node.value.id in ROWS
        and node.attr in {"payload", "result", "targets", "progress"}
    ]


def test_raw_row_reader_guard_catches_the_retired_pattern():
    assert raw_reads('intent = parent.payload.get("workload_intent_ordinal")')
    assert raw_reads('reason = attempt.result["reason"]')
    assert not raw_reads("message.result.reason")
    assert not raw_reads('column_field(parent, "payload", "workload_intent_ordinal")')


def test_agent_job_package_has_no_raw_row_document_readers():
    for path, parsed in parsed_tree(Path(PACKAGE)):
        assert raw_reads(parsed.source) == [], path


def test_agent_job_modules_stay_within_the_requested_split_boundary():
    for path, parsed in parsed_tree(Path(PACKAGE)):
        assert len(parsed.source.splitlines()) < 1000, path
