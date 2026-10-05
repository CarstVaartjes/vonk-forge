"""The vocabulary-literal ratchet fails on each wrong shape, and the repository holds it."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
from vonk_agent_protocol import (
    FailureCode,
    InvalidRequestReason,
    LifecycleState,
    SecurityRefusalReason,
    WaitReason,
)

from . import vocabulary_literals as scan

REL = "control/src/vonk_control/example.py"


def _module(tmp_path: Path, source: str, relative: str = REL) -> Path:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _python(
    tmp_path: Path, source: str, relative: str = REL
) -> Counter[tuple[str, str]]:
    return scan.scan_python([_module(tmp_path, source, relative)], root=tmp_path)


def test_the_scanned_words_come_from_the_contract() -> None:
    assert {reason.value for reason in WaitReason} <= scan.DISTINCTIVE_WORDS
    assert {code.value for code in FailureCode} <= scan.DISTINCTIVE_WORDS
    assert {
        reason.value for reason in SecurityRefusalReason if "." in reason.value
    } <= (scan.DISTINCTIVE_WORDS)
    assert {LifecycleState.NEEDS_OPERATOR.value, scan.LEGACY_WAIT_STATE} <= (
        scan.DISTINCTIVE_WORDS
    )
    assert {word for word in scan.STORED_STATE_WORDS} == {
        "queued",
        "running",
        "succeeded",
        "failed",
        "cancelled",
    }
    # Plain prose that is also a vocabulary word is deliberately not scanned.
    for word in ("unknown", "none", "stop", "retry", "conflict", "malformed"):
        assert scan.tier_of(word) is None
    assert InvalidRequestReason.NOT_FOUND.value in scan.DISTINCTIVE_WORDS


def test_a_hand_spelled_word_is_found_in_code_not_in_a_docstring(
    tmp_path: Path,
) -> None:
    counts = _python(
        tmp_path,
        '''
"""A module that mentions waiting-for-operator in prose."""

def wait() -> str:
    """Parks as "waiting-for-operator"."""
    state = "waiting-for-operator"
    code = "operation_cancelled"
    return state if code else "running"
''',
    )

    assert counts == Counter({("distinctive", REL): 2, ("stored_state", REL): 1})


def test_prose_words_and_unrelated_strings_are_not_literals(tmp_path: Path) -> None:
    counts = _python(
        tmp_path,
        'WORDS = ("unknown", "none", "stop", "retry", "waiting", "needs operator")\n',
    )

    assert not counts


def test_the_contract_and_the_legacy_adapter_may_spell_the_words(
    tmp_path: Path,
) -> None:
    for relative in sorted(scan.ALLOWED_FILES):
        assert not _python(tmp_path, 'X = "waiting-for-operator"\n', relative)


def test_generated_python_is_not_scanned(tmp_path: Path) -> None:
    relative = "src/cluster_profiles/generated_control/models/state.py"

    assert not _python(tmp_path, 'X = "needs-operator"\n', relative)


def test_a_new_literal_a_raised_count_and_a_fallen_count_each_fail(
    tmp_path: Path,
) -> None:
    baseline = {"distinctive": {REL: 1}, "stored_state": {}, "legacy_state": {}}

    held = _python(tmp_path, 'X = "waiting-for-operator"\n')
    assert scan.problems(held, baseline) == []

    raised = _python(tmp_path, 'X = "waiting-for-operator"\nY = "needs-operator"\n')
    assert "use the contract enum" in scan.problems(raised, baseline)[0]

    unlisted = scan.problems(
        held, {"distinctive": {}, "stored_state": {}, "legacy_state": {}}
    )
    assert "use the contract enum" in unlisted[0]

    fallen = _python(tmp_path, "X = 1\n")
    assert "lower" in scan.problems(fallen, baseline)[0]


def test_lowering_the_baseline_never_raises_a_count(tmp_path: Path) -> None:
    baseline = {
        "distinctive": {REL: 3},
        "stored_state": {REL: 1},
        "legacy_state": {},
    }
    counts = _python(tmp_path, 'X = "waiting-for-operator"\nY = "needs-operator"\n')
    counts[("stored_state", REL)] = 5

    lowered = scan.lowered_baseline(counts, baseline)

    assert lowered == {
        "distinctive": {REL: 2},
        "stored_state": {REL: 1},
        "legacy_state": {},
    }


def test_typescript_sources_may_not_spell_the_words(tmp_path: Path) -> None:
    source = _module(
        tmp_path,
        "if (state === \"waiting-for-operator\" || kind === 'needs-operator') {}\n",
        "control/web/src/pages/example.tsx",
    )
    generated = _module(
        tmp_path,
        'export const X = "waiting-for-operator";\n',
        "control/web/src/api/vocabulary.generated.ts",
    )
    fixture = _module(
        tmp_path,
        'const state = "waiting-for-operator";\n',
        "control/web/src/pages/example.test.tsx",
    )

    counts = scan.scan_typescript([source, generated, fixture], root=tmp_path)

    assert counts == Counter({"control/web/src/pages/example.tsx": 2})


def test_the_repository_holds_the_python_ratchet() -> None:
    assert scan.problems(scan.scan_python(), scan.load_baseline()) == []


def test_the_web_app_spells_no_vocabulary_word_by_hand() -> None:
    assert scan.scan_typescript() == Counter()


def test_every_baselined_file_exists() -> None:
    baseline = json.loads(scan.BASELINE_PATH.read_text(encoding="utf-8"))
    for tier in scan.TIERS:
        for relative in baseline[tier]:
            assert (scan.REPO_ROOT / relative).is_file(), relative


@pytest.mark.parametrize("tier", scan.TIERS)
def test_the_baseline_is_sorted_and_positive(tier: str) -> None:
    baseline = scan.load_baseline()[tier]
    assert list(baseline) == sorted(baseline)
    assert all(count > 0 for count in baseline.values())


@pytest.mark.parametrize(
    "source",
    [
        'if row.state == "expired":\n    pass\n',
        'ACTIVE_STATES = ("queued", "waiting")\n',
        'x = Job(state="cancelling")\n',
        'ok = attempt.state in {"expired", State.FAILED}\n',
        'm = {"partial": State.BACKOFF}\n',
    ],
)
def test_a_retired_state_spelling_is_found_where_a_statement_names_a_state(
    tmp_path: Path, source: str
) -> None:
    assert _python(tmp_path, source)[("legacy_state", REL)] == 1


@pytest.mark.parametrize(
    "source",
    [
        'activity = "waiting"\n',
        'phase = "partial"\n',
        'label = "cancelling"  # a progress word, not a stored state\n',
        'def f(state):\n    """expired"""\n    return 1\n',
        "def f(row):\n    row.state = 1\n    return 'waiting'\n",
    ],
)
def test_the_same_words_elsewhere_are_prose(tmp_path: Path, source: str) -> None:
    assert ("legacy_state", REL) not in _python(tmp_path, source)


def test_only_the_alias_table_may_spell_the_retired_words(tmp_path: Path) -> None:
    contract = "agent_protocol/src/vonk_agent_protocol/lifecycle_vocabulary.py"
    assert not _python(tmp_path, 'STORED_STATE = "waiting"\n', contract)
    assert _python(tmp_path, 'STORED_STATE = "waiting"\n')


def test_the_non_lifecycle_floors_match_the_repository() -> None:
    assert scan.floor_problems(scan.scan_python()) == []
