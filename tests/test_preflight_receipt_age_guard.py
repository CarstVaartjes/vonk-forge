"""A runtime preflight receipt is valid by identity, never by wall-clock age.

A receipt answers the exact requirements it was probed for
(``requirements_sha256``) on the exact host it was probed on (the agent's host
fingerprint: boot id, agent build, runtime and storage policy, refreshed on
every claim and inventory report). Until 2026-11-12 a 300 s window
(``PREFLIGHT_MAX_AGE_SECONDS``) was compared with the receipt's age and
re-checked at every later phase and at install admission, so every switch whose
phases outlasted five minutes ran a second probe round on an unchanged host
(15-130 s per load).

This guard fails when code that reads preflight receipts orders a receipt's
``received_at`` or ``observed_at`` against anything (an age or freshness check)
or reintroduces a preflight age constant. Selection by newest (``max(...,
key=...)``) and presence checks (``is None``) remain allowed.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / "control/src/vonk_control"

#: Names whose presence marks a function as one that reads preflight receipts.
RECEIPT_MARKERS = frozenset(
    {
        "PreflightInventoryItem",
        "LifecyclePreflightCheckpoint",
        "RuntimePreflightResult",
        "admission_blockers",
        "latest_result",
        "record_preflight",
        "preflights",
    }
)
RECEIPT_TIMES = frozenset({"received_at", "observed_at"})
ORDERING = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)
AGE_CONSTANT = re.compile(r"PREFLIGHT\w*_(?:AGE|TTL|WINDOW|FRESHNESS)(?:_|$)")


def _names(node: ast.AST) -> set[str]:
    found: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            found.add(child.id)
        elif isinstance(child, ast.Attribute):
            found.add(child.attr)
    return found


def age_comparisons(tree: ast.AST) -> list[int]:
    """Lines where a receipt-reading function orders a receipt time."""

    lines: list[int] = []
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not _names(function) & RECEIPT_MARKERS:
            continue
        for compare in ast.walk(function):
            if not isinstance(compare, ast.Compare):
                continue
            if not any(isinstance(op, ORDERING) for op in compare.ops):
                continue
            if _names(compare) & RECEIPT_TIMES:
                lines.append(compare.lineno)
    for node in ast.walk(tree):
        name = (
            node.id
            if isinstance(node, ast.Name)
            else node.attr
            if isinstance(node, ast.Attribute)
            else None
        )
        if name is not None and AGE_CONSTANT.search(name):
            lines.append(node.lineno)
    return sorted(set(lines))


FLAGGED = (
    "def valid(receipt: PreflightInventoryItem, now, maximum_age):\n    return now - receipt.received_at.timestamp() > maximum_age",
    "def keep(items, now):\n    return [i for i in items.preflights if i.received_at >= now - 300]",
    "def fresh(receipt: PreflightInventoryItem, now):\n    return receipt.result.observed_at < now",
    "PREFLIGHT_MAX_AGE_SECONDS = 300",
    "PREFLIGHT_TTL = 300",
)

ALLOWED = (
    "def valid(receipt: PreflightInventoryItem, digest, fingerprint):\n    return receipt.result.fingerprint == fingerprint",
    "def newest(items: list[PreflightInventoryItem]):\n    return max(items, key=lambda item: item.result.observed_at)",
    "def present(receipt: PreflightInventoryItem):\n    return receipt.received_at is not None",
    # Post-stop inventory is not a preflight receipt.
    "def after_stop(snapshot, stopped_at):\n    return snapshot.received_at < stopped_at",
    "_INSTALL_PREFLIGHT_REFRESH_REASON = 'refresh'",
)


@pytest.mark.parametrize("source", FLAGGED)
def test_a_wall_clock_age_check_on_a_receipt_is_found(source: str) -> None:
    assert age_comparisons(ast.parse(source))


@pytest.mark.parametrize("source", ALLOWED)
def test_identity_checks_selection_and_inventory_times_are_allowed(
    source: str,
) -> None:
    assert not age_comparisons(ast.parse(source))


def test_no_preflight_receipt_is_aged_by_the_wall_clock() -> None:
    found = [
        f"{path.relative_to(ROOT).as_posix()}:{line}"
        for path in sorted(CONTROL.rglob("*.py"))
        for line in age_comparisons(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert not found, (
        "A preflight receipt stays valid while its requirements digest and the "
        "host fingerprint are unchanged; do not compare its time with now:\n"
        + "\n".join(found)
    )
