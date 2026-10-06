"""Prove the blocker allowlist scanners flag each wrong shape and pass the gate.

A gate that cannot fail on the wrong implementation is ceremony: every rule runs
against the shape that dead-ended a load in production (a state assigned
``waiting-for-operator`` with no action, a bookkeeping raise) and against the
closest shape that only *reads* the state.
"""

from __future__ import annotations

import ast
import copy
import json
from textwrap import dedent

import pytest

from .blocker_boundaries import (
    ALLOWLIST_PATH,
    CONTROL_SOURCE_ROOT,
    CONTROL_STATE_ARGUMENT,
    CONTROL_STATE_ASSIGNMENT,
    REPO_ROOT,
    RUST_RESULT,
    RaiseSite,
    UncategorizedRaise,
    WaitSite,
    audited_paths,
    categorized_classes,
    dump_document,
    evaluate_guard_gate,
    evaluate_raise_gate,
    evaluate_wait_gate,
    exception_classes,
    failure_code,
    guard_paths,
    load_allowlist,
    parsed_modules,
    scan_guard_raises,
    scan_guard_source,
    scan_python_waits,
    scan_raise_source,
    scan_raises,
    scan_rust_waits,
    scan_waits,
    write_counts,
)

#: The repository parse is shared setup, not the first test's own time.
pytestmark = pytest.mark.usefixtures("parsed_repository")

PATH = "control/src/vonk_control/sample.py"
AUDITED_PATH = "control/src/vonk_control/audited_sample.py"


@pytest.fixture(scope="module", autouse=True)
def _parsed_control_sources() -> None:
    """Parse control/src once for the module: the scans share it (a shared fixture
    is outside the per-test budget, and the parse is the whole cost)."""

    parsed_modules(CONTROL_SOURCE_ROOT)


def _waits(source: str) -> list[tuple[str, str]]:
    return [
        (site.kind, site.function)
        for site in scan_python_waits(dedent(source), path=PATH)
    ]


@pytest.mark.parametrize(
    "statement",
    [
        'operation.state = "waiting-for-operator"',
        'state["state"] = "waiting-for-operator"',
        'state = "waiting-for-operator"',
        'row.state = "waiting-for-operator" if safe else "failed"',
    ],
)
def test_a_state_assigned_waiting_for_operator_is_a_site(statement: str) -> None:
    sites = _waits(f"def park(operation, state, row, safe):\n    {statement}\n")
    assert sites == [(CONTROL_STATE_ASSIGNMENT, "park")]


def test_state_arguments_are_sites() -> None:
    sites = _waits(
        """
        class Service:
            def a(self, fence):
                self._finish(fence, "waiting-for-operator", None)

            def b(self, session, row):
                self._set_application_state(session, row, "waiting-for-operator")

            def c(self):
                return make(state="waiting-for-operator")
        """
    )
    assert sites == [
        (CONTROL_STATE_ARGUMENT, "Service.a"),
        (CONTROL_STATE_ARGUMENT, "Service.b"),
        (CONTROL_STATE_ARGUMENT, "Service.c"),
    ]


@pytest.mark.parametrize(
    "source",
    [
        'def f(job):\n    return job.state == "waiting-for-operator"\n',
        'def f(job):\n    return job.state in {"queued", "waiting-for-operator"}\n',
        'def f(model):\n    return model.state.in_(("waiting-for-operator",))\n',
        'LABELS = {"waiting-for-operator": ("waiting for operator", "warning")}\n',
        'from typing import Literal\nState = Literal["queued", "waiting-for-operator"]\n',
        'def f(job):\n    """A waiting-for-operator order is decided elsewhere."""\n',
    ],
)
def test_reading_the_state_is_not_a_site(source: str) -> None:
    assert _waits(source) == []


def test_rust_results_are_keyed_by_the_enclosing_function() -> None:
    source = dedent(
        """
        impl Executor for RecipeExecutor {
            fn execute(&self) -> ExecutionResult {
                return unconfirmed(WaitReason::StopUnconfirmed, "stop remains unconfirmed");
            }
        }
        fn other() -> ExecutionResult {
            ExecutionResult::unknown(WaitReason::StopUnconfirmed, "x")
        }
        fn unconfirmed(wait: WaitReason, reason: &'static str) -> ExecutionResult {
            ExecutionResult::unknown(wait, reason)
        }
        // unconfirmed(WaitReason::StopUnconfirmed, "a comment is not a site")
        """
    )
    sites = scan_rust_waits(source, path="rust/crates/vonk-agent/src/executor.rs")
    assert [(site.kind, site.function) for site in sites] == [
        (RUST_RESULT, "RecipeExecutor::execute"),
        (RUST_RESULT, "other"),
    ]


def _entry(
    function: str = "park", sites: int = 1, verdict: str = "SELF-HEAL"
) -> dict[str, object]:
    return {
        "path": PATH,
        "function": function,
        "kind": CONTROL_STATE_ASSIGNMENT,
        "operation_kinds": ["recipe.stop"],
        "verdict": verdict,
        "advertised_action": "automatic retry",
        "reason": "Written reason for the entry.",
        "sites": sites,
    }


def _site(function: str = "park") -> WaitSite:
    return WaitSite(PATH, function, CONTROL_STATE_ASSIGNMENT, 1)


def _document(
    waits: list[dict[str, object]], debt: int | None = None
) -> dict[str, object]:
    debt = (
        sum(1 for entry in waits if entry["verdict"] != "KEEP")
        if debt is None
        else debt
    )
    return {"operator_waits": waits, "max_debt": debt}


def test_the_wait_gate_fails_on_new_stale_moved_and_rising_debt() -> None:
    assert evaluate_wait_gate([_site()], _document([_entry()])) == []

    new = evaluate_wait_gate([_site(), _site("other")], _document([_entry()]))
    assert any("outside the blocker allowlist" in message for message in new)

    stale = evaluate_wait_gate([], _document([_entry()]))
    assert any("no longer occurs" in message for message in stale)

    more = evaluate_wait_gate([_site(), _site()], _document([_entry()]))
    assert any("rose from 1 to 2" in message for message in more)

    fewer = evaluate_wait_gate([_site()], _document([_entry(sites=2)]))
    assert any("fell from 2 to 1" in message for message in fewer)

    debt = evaluate_wait_gate([_site()], _document([_entry()], debt=0))
    assert any("above max_debt" in message for message in debt)
    slack = evaluate_wait_gate([_site()], _document([_entry()], debt=3))
    assert any("lower max_debt" in message for message in slack)


def _family(
    sites: list[list[object]], category: str = "bookkeeping-debt"
) -> dict[str, object]:
    return {
        "family": "x.y",
        "category": category,
        "reason": "Written reason.",
        "sites": sites,
    }


def _raise_document(
    families: list[dict[str, object]], total: int, unaudited: int = 0
) -> dict[str, object]:
    return {
        "fail_closed": families,
        "debt_ceiling": {"total": total, "unaudited": unaudited},
        "scope": {"audited_paths": [AUDITED_PATH]},
    }


def _raise(function: str = "go", code: str = "x.bad", path: str = PATH) -> RaiseSite:
    return RaiseSite(path, "SampleConflict", function, code, 1)


def _listed(
    function: str = "go", code: str = "x.bad", count: int = 1, path: str = PATH
) -> list[object]:
    return [path, "SampleConflict", function, code, count]


def test_the_raise_gate_fails_on_new_stale_moved_and_rising_debt() -> None:
    document = _raise_document([_family([_listed()])], 0, unaudited=1)
    assert evaluate_raise_gate([_raise()], document) == []

    new = evaluate_raise_gate([_raise(), _raise("other")], document)
    assert any("outside the blocker allowlist" in message for message in new)

    stale = evaluate_raise_gate([], document)
    assert any("no longer occurs" in message for message in stale)

    more = evaluate_raise_gate([_raise(), _raise()], document)
    assert any("rose from 1 to 2" in message for message in more)

    fewer = evaluate_raise_gate(
        [_raise()], _raise_document([_family([_listed(count=2)])], 0, unaudited=2)
    )
    assert any("fell from 2 to 1" in message for message in fewer)

    over = evaluate_raise_gate(
        [_raise()], _raise_document([_family([_listed()])], 0, unaudited=0)
    )
    assert any("above the ceiling" in message for message in over)
    under = evaluate_raise_gate(
        [_raise()], _raise_document([_family([_listed()])], 0, unaudited=4)
    )
    assert any("lower debt_ceiling.unaudited" in message for message in under)

    reviewed = evaluate_raise_gate(
        [_raise()], _raise_document([_family([_listed()], "input-validation")], 0)
    )
    assert reviewed == []  # a reviewed category carries no debt


def test_debt_of_audited_and_other_modules_has_separate_ceilings() -> None:
    audited = _listed(path=AUDITED_PATH)
    document = _raise_document([_family([audited, _listed()])], total=1, unaudited=1)
    sites = [_raise(path=AUDITED_PATH), _raise()]
    assert evaluate_raise_gate(sites, document) == []

    # A new debt site elsewhere cannot hide behind the audited modules' ceiling.
    spill = _raise_document([_family([audited, _listed()])], total=2, unaudited=0)
    messages = evaluate_raise_gate(sites, spill)
    assert any(
        "other modules" in message and "above" in message for message in messages
    )
    assert any(
        "audited modules" in message and "lower" in message for message in messages
    )


def test_a_site_in_two_families_is_refused() -> None:
    document = _raise_document([_family([_listed()]), _family([_listed()])], 2)
    with pytest.raises(ValueError, match="two families"):
        evaluate_raise_gate([_raise()], document)


def test_raise_codes_come_from_the_leading_dotted_token() -> None:
    def code(source: str) -> str:
        call = ast.parse(source, mode="eval").body
        assert isinstance(call, ast.Call)
        return failure_code(call)

    assert code('Conflict("run-switch.plan_blocked: the plan is stale")') == (
        "run-switch.plan_blocked"
    )
    assert code('Conflict(f"model_cache.digest_mismatch: {name} differs")') == (
        "model_cache.digest_mismatch"
    )
    assert code('Conflict("recipe installation is already complete")') == (
        "recipe-installation-is-already-complete"
    )
    assert code("Conflict(message)") == "message"


def test_raises_of_local_error_classes_are_sites_and_builtins_are_not() -> None:
    source = dedent(
        """
        class SampleConflict(Exception): ...

        def go(x):
            if x:
                raise SampleConflict("sample.bad: nope")
            raise ValueError("programming error")
        """
    )
    classes = exception_classes([ast.parse(source)])
    assert "SampleConflict" in classes and "ValueError" not in classes
    sites = scan_raise_source(source, path=PATH, classes=classes)
    assert [(site.exception_class, site.function, site.code) for site in sites] == [
        ("SampleConflict", "go", "sample.bad")
    ]


def test_exception_classes_follow_the_bases_not_only_the_name() -> None:
    source = dedent(
        """
        class Denied(RuntimeError): ...
        class Pending(Denied): ...
        class SampleConflict(Exception): ...
        class _Kept(Exception): ...
        class _Private(ValueError): ...
        class Helper: ...
        """
    )
    classes = exception_classes([ast.parse(source)])
    assert {"Denied", "Pending", "SampleConflict", "_Private"} <= classes
    assert "_Kept" not in classes and "Helper" not in classes


def test_the_scan_covers_every_module_of_control_src() -> None:
    document = load_allowlist()
    audited = audited_paths(document)
    paths = {site.path for site in scan_raises()}
    # An audited module may have no raise left at all, so it is checked against
    # the modules of control/src, not against the raises the scan still finds.
    modules = {
        module.relative_to(REPO_ROOT).as_posix()
        for module in CONTROL_SOURCE_ROOT.rglob("*.py")
    }
    assert audited <= modules
    assert paths - audited, "raises outside the audited modules are scanned too"


def test_the_repository_holds_at_its_reviewed_blockers() -> None:
    document = load_allowlist()
    waits = scan_waits()
    raises = scan_raises()
    assert evaluate_wait_gate(waits, document) == []
    assert evaluate_raise_gate(raises, document) == []
    assert evaluate_guard_gate(scan_guard_raises(guard_paths(document)), document) == []


GUARD_SOURCE = dedent(
    """
    class Refused(SecurityRefusalError, RuntimeError): ...
    class Narrower(Refused): ...
    class Bad(InvalidRequestError): ...
    class Wait(UnknownOutcomeError): ...
    class Plain(RuntimeError): ...

    def go(x):
        if x == 1:
            raise Refused("categorized")
        if x == 2:
            raise Narrower("a subclass of a categorized type")
        if x == 3:
            raise Bad("categorized")
        if x == 4:
            raise Wait("categorized")
        if x == 5:
            raise RuntimeError("bare")
        if x == 6:
            raise ValueError("bare")
        if x == 7:
            raise Plain("a local class outside the categories")
        if x == 8:
            raise NotImplementedError
        try:
            go(1)
        except Refused as error:
            raise error
        except Exception:
            raise
        raise _factory("a raise through a factory proves nothing")
    """
)


def test_the_guard_flags_a_raise_outside_the_three_categories() -> None:
    categorized = categorized_classes([ast.parse(GUARD_SOURCE)])
    assert {"Refused", "Narrower", "Bad", "Wait"} <= categorized
    assert "Plain" not in categorized
    sites = scan_guard_source(GUARD_SOURCE, path=PATH, categorized=categorized)
    assert [site.exception_class for site in sites] == [
        "RuntimeError",
        "ValueError",
        "Plain",
        "_factory",
    ]
    assert {site.function for site in sites} == {"go"}


def test_the_guard_lets_a_categorized_raise_in_a_lifecycle_module_through() -> None:
    categorized = categorized_classes([ast.parse(GUARD_SOURCE)])
    clean = dedent(
        """
        def go():
            raise Refused("security edge")
        """
    )
    assert scan_guard_source(clean, path=PATH, categorized=categorized) == []


def _uncategorized(path: str = PATH, count: int = 1) -> list[UncategorizedRaise]:
    return [
        UncategorizedRaise(path, "RuntimeError", "go", line) for line in range(count)
    ]


def _guard_document(grandfathered: dict[str, int], ceiling: int) -> dict[str, object]:
    return {"categorized_raises": {"grandfathered": grandfathered, "ceiling": ceiling}}


def test_the_guard_gate_fails_on_new_rising_stale_and_a_rising_ceiling() -> None:
    document = _guard_document({PATH: 1}, 1)
    assert evaluate_guard_gate(_uncategorized(), document) == []

    new = evaluate_guard_gate(_uncategorized(AUDITED_PATH), document)
    assert any("outside the three error categories" in message for message in new)

    more = evaluate_guard_gate(_uncategorized(count=2), document)
    assert any("rose from 1 to 2" in message for message in more)

    fewer = evaluate_guard_gate([], document)
    assert any("none left" in message for message in fewer)
    fell = evaluate_guard_gate(_uncategorized(), _guard_document({PATH: 2}, 2))
    assert any("fell from 2 to 1" in message for message in fell)

    above = evaluate_guard_gate(_uncategorized(), _guard_document({PATH: 1}, 0))
    assert any("above the ceiling" in message for message in above)
    below = evaluate_guard_gate(_uncategorized(), _guard_document({PATH: 1}, 5))
    assert any("lower categorized_raises.ceiling" in message for message in below)


def test_the_lifecycle_core_is_scanned_and_its_debt_is_small() -> None:
    document = load_allowlist()
    assert any(entry.endswith("/lifecycle/") for entry in guard_paths(document))
    recorded = document["categorized_raises"]["grandfathered"]  # type: ignore[index]
    lifecycle = sum(count for path, count in recorded.items() if "/lifecycle/" in path)
    assert lifecycle <= 4, "the lifecycle core only falls from here"


def test_the_allowlist_keeps_a_few_real_waits() -> None:
    document = load_allowlist()
    keep = [e for e in document["operator_waits"] if e["verdict"] == "KEEP"]  # type: ignore[attr-defined]
    assert keep, "a genuinely irreversible wait is the reason the state exists"


def test_a_keep_entry_without_an_effect_or_action_is_refused(tmp_path) -> None:  # type: ignore[no-untyped-def]
    document = load_allowlist()
    for field, value in (
        ("irreversible_effect", "none"),
        ("advertised_action", "someone should look"),
    ):
        broken = copy.deepcopy(document)
        entry = next(e for e in broken["operator_waits"] if e["verdict"] == "KEEP")  # type: ignore[attr-defined]
        entry[field] = value
        target = tmp_path / "broken.json"
        target.write_text(json.dumps(broken), encoding="utf-8")
        with pytest.raises(ValueError, match="KEEP needs"):
            load_allowlist(target)


def test_a_category_or_verdict_outside_the_vocabulary_is_refused(tmp_path) -> None:  # type: ignore[no-untyped-def]
    document = load_allowlist()
    bad_category = copy.deepcopy(document)
    bad_category["fail_closed"][0]["category"] = "whatever"  # type: ignore[index]
    bad_verdict = copy.deepcopy(document)
    bad_verdict["operator_waits"][0]["verdict"] = "LATER"  # type: ignore[index]
    for broken, message in ((bad_category, "category"), (bad_verdict, "verdict")):
        target = tmp_path / "broken.json"
        target.write_text(json.dumps(broken), encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            load_allowlist(target)


def test_write_counts_lowers_and_never_adds() -> None:
    document = load_allowlist()
    waits = scan_waits()
    raises = scan_raises()
    guard = scan_guard_raises(guard_paths(document))
    assert write_counts(document, waits, raises, guard) == document
    fewer = write_counts(document, waits[:-3], raises[:-5], guard[:-7])
    sites = [UncategorizedRaise("p.py", "X", "f", line) for line in range(10)]
    grandfathered = {
        **document,
        "categorized_raises": {
            **document["categorized_raises"],  # type: ignore[dict-item]
            "grandfathered": {"p.py": 10},
            "ceiling": 10,
        },
    }
    lowered = write_counts(grandfathered, waits, raises, sites[:-7])
    assert lowered["categorized_raises"]["ceiling"] == 3  # type: ignore[index]
    assert fewer["max_debt"] <= document["max_debt"]  # type: ignore[operator]
    assert fewer["debt_ceiling"]["total"] <= document["debt_ceiling"]["total"]  # type: ignore[index, operator]
    assert evaluate_wait_gate(waits[:-3], fewer) == []
    assert evaluate_raise_gate(raises[:-5], fewer) == []


def test_the_committed_file_is_what_the_writer_produces() -> None:
    text = ALLOWLIST_PATH.read_text(encoding="utf-8")
    assert dump_document(json.loads(text)) == text


def test_existing_error_types_that_joined_a_category_keep_their_handlers() -> None:
    from vonk_agent_protocol import (
        InvalidRequestError,
        SecurityRefusalError,
        UnknownOutcomeError,
    )
    from vonk_control.admission_locking import AdmissionLockBusy
    from vonk_control.auth import AuthError, CursorError
    from vonk_control.jobs import StaleAttempt
    from vonk_control.passwords import PasswordPolicyError
    from vonk_control.request_fault import RequestFault

    categorized = categorized_classes(
        list(parsed_modules(CONTROL_SOURCE_ROOT).values())
    )
    for error, category, builtin in (
        (AuthError("token is invalid"), SecurityRefusalError, ValueError),
        (StaleAttempt("fence is stale"), SecurityRefusalError, RuntimeError),
        (CursorError("cursor is invalid"), InvalidRequestError, ValueError),
        (RequestFault("limit is invalid"), InvalidRequestError, ValueError),
        (PasswordPolicyError("password is invalid"), InvalidRequestError, ValueError),
        (AdmissionLockBusy("busy", holder="x"), UnknownOutcomeError, RuntimeError),
    ):
        assert isinstance(error, category) and isinstance(error, builtin)
        assert type(error).__name__ in categorized
