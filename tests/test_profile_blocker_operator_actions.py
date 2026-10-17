"""Only consent blockers may ask the operator to repeat a Fleet profile load."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from vonk_agent_protocol import ProfileReasonCode
from vonk_control.fleet_profiles import contracts

ROOT = Path(__file__).resolve().parents[1]
CONSENT_EDGES = {
    ProfileReasonCode.REVIEW_STALE: (
        "The caller's reviewed digest or fleet membership changed, or execution would "
        "stop or remove an unreviewed target. A new review authorizes those effects."
    ),
}
_REPEAT_ACTION = re.compile(r"\b(?:review|load)\b[^.;]*\bagain\b", re.IGNORECASE)


def operator_action_violations(source: str) -> list[tuple[int, str | None]]:
    """Inspect message literals, including split strings and f-strings.

    Resolve exception codes from the current contract rather than allowlisting
    message text or file positions. Unclassified messages fail closed here.
    """
    tree = ast.parse(source)
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }
    failures: list[tuple[int, str | None]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            message = " ".join(
                part.value
                for part in node.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if isinstance(parents.get(node), ast.JoinedStr):
                continue
            message = node.value
        else:
            continue
        if isinstance(parents.get(node), ast.Expr):
            continue  # Docstrings are not operator-facing messages.
        if not _REPEAT_ACTION.search(message):
            continue
        owner: ast.AST = node
        while owner in parents and not isinstance(owner, ast.Call):
            owner = parents[owner]
        code: str | None = None
        if isinstance(owner, ast.Call):
            if isinstance(owner.func, ast.Name):
                code = getattr(getattr(contracts, owner.func.id, None), "code", None)
            code_expressions = [
                keyword.value for keyword in owner.keywords if keyword.arg == "code"
            ]
            if (
                isinstance(owner.func, ast.Name)
                and owner.func.id == "make_blocker"
                and owner.args
            ):
                code_expressions.append(owner.args[0])
            for value in code_expressions:
                if (
                    isinstance(value, ast.Attribute)
                    and isinstance(value.value, ast.Name)
                    and value.value.id == ProfileReasonCode.__name__
                ):
                    code = ProfileReasonCode[value.attr]
        if code not in CONSENT_EDGES:
            failures.append((node.lineno, code))
    return failures


def test_fleet_profile_repeat_actions_require_an_allowlisted_consent_code() -> None:
    """Catches new manual-retry instructions on recoverable profile blockers."""
    failures = {
        str(path.relative_to(ROOT)): violations
        for path in (ROOT / "control/src/vonk_control/fleet_profiles").rglob("*.py")
        if (violations := operator_action_violations(path.read_text()))
    }
    assert not failures, failures


@pytest.mark.parametrize(
    "source",
    [
        'raise FleetProfileStalePlanConflict("Effects changed; review again")',
        'raise FleetProfileAdmissionEffectBusy(f"Busy {holder}; load the profile again")',
        'raise FleetProfileAdmissionBusy(f"load {profile_id} again")',
        'make_blocker(ProfileReasonCode.ADMISSION_BUSY, "review again")',
        'return "load the current " "profile again"',
        'FleetProfileReason(code=ProfileReasonCode.REQUEST_INVALID, detail="review again")',
    ],
)
def test_guard_rejects_manual_retries_even_when_the_message_moves(source: str) -> None:
    assert operator_action_violations(source)


@pytest.mark.parametrize(
    "source",
    [
        'raise FleetProfileReviewStale("Unreviewed stop; review and load again")',
        'FleetProfileReason(code=ProfileReasonCode.REVIEW_STALE, detail="review again")',
        'make_blocker(ProfileReasonCode.REVIEW_STALE, "review again")',
        'raise FleetProfileAdmissionEffectBusy("Busy; the Controller will retry automatically")',
    ],
)
def test_guard_allows_consent_edges_and_automatic_waits(source: str) -> None:
    assert not operator_action_violations(source)
