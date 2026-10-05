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
    CONTROL_STATE_ARGUMENT,
    CONTROL_STATE_ASSIGNMENT,
    RUST_RESULT,
    RaiseSite,
    WaitSite,
    dump_document,
    evaluate_raise_gate,
    evaluate_wait_gate,
    exception_classes,
    failure_code,
    load_allowlist,
    raise_paths,
    scan_python_waits,
    scan_raise_source,
    scan_raises,
    scan_rust_waits,
    scan_waits,
    write_counts,
)

PATH = "control/src/vonk_control/sample.py"


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
                return waiting_for_operator("stop remains unconfirmed");
            }
        }
        fn other() -> ExecutionResult {
            ExecutionResult { state: "waiting-for-operator", body: json!({}) }
        }
        fn waiting_for_operator(reason: &'static str) -> ExecutionResult {
            ExecutionResult { state: "waiting-for-operator", body: json!({"reason": reason}) }
        }
        // waiting_for_operator("a comment is not a site")
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


def _raise_document(families: list[dict[str, object]], total: int) -> dict[str, object]:
    return {"fail_closed": families, "debt_ceiling": {"total": total}}


def _raise(function: str = "go", code: str = "x.bad") -> RaiseSite:
    return RaiseSite(PATH, "SampleConflict", function, code, 1)


def _listed(function: str = "go", code: str = "x.bad", count: int = 1) -> list[object]:
    return [PATH, "SampleConflict", function, code, count]


def test_the_raise_gate_fails_on_new_stale_moved_and_rising_debt() -> None:
    document = _raise_document([_family([_listed()])], 1)
    assert evaluate_raise_gate([_raise()], document) == []

    new = evaluate_raise_gate([_raise(), _raise("other")], document)
    assert any("outside the blocker allowlist" in message for message in new)

    stale = evaluate_raise_gate([], document)
    assert any("no longer occurs" in message for message in stale)

    more = evaluate_raise_gate([_raise(), _raise()], document)
    assert any("rose from 1 to 2" in message for message in more)

    fewer = evaluate_raise_gate(
        [_raise()], _raise_document([_family([_listed(count=2)])], 2)
    )
    assert any("fell from 2 to 1" in message for message in fewer)

    over = evaluate_raise_gate([_raise()], _raise_document([_family([_listed()])], 0))
    assert any("above the ceiling" in message for message in over)
    under = evaluate_raise_gate([_raise()], _raise_document([_family([_listed()])], 4))
    assert any("lower debt_ceiling.total" in message for message in under)

    reviewed = evaluate_raise_gate(
        [_raise()], _raise_document([_family([_listed()], "input-validation")], 0)
    )
    assert reviewed == []  # a reviewed category carries no debt


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


def test_the_repository_holds_at_its_reviewed_blockers() -> None:
    document = load_allowlist()
    waits = scan_waits()
    raises = scan_raises(raise_paths(document))
    assert evaluate_wait_gate(waits, document) == []
    assert evaluate_raise_gate(raises, document) == []


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
    raises = scan_raises(raise_paths(document))
    assert write_counts(document, waits, raises) == document
    fewer = write_counts(document, waits[:-3], raises[:-5])
    assert fewer["max_debt"] <= document["max_debt"]  # type: ignore[operator]
    assert fewer["debt_ceiling"]["total"] <= document["debt_ceiling"]["total"]  # type: ignore[index, operator]
    assert evaluate_wait_gate(waits[:-3], fewer) == []
    assert evaluate_raise_gate(raises[:-5], fewer) == []


def test_the_committed_file_is_what_the_writer_produces() -> None:
    text = ALLOWLIST_PATH.read_text(encoding="utf-8")
    assert dump_document(json.loads(text)) == text
