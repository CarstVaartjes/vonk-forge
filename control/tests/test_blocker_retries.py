"""An unknown-outcome raise counts as retried only where a loop is proven."""

from __future__ import annotations

import ast
import copy
import textwrap
from collections.abc import Mapping

import pytest

from .blocker_boundaries import RaiseSite, load_allowlist
from .blocker_callgraph import CallGraph, DeclaredEdge
from .blocker_classifier import DEBT, RETRIED, classify
from .blocker_retries import (
    Proof,
    Prover,
    _handlers_catching,
    build_graph_for,
    evaluate_retry_gate,
    loop_problems,
    promote_proven,
    proven,
    unknown_classes,
    unproven_sites,
)

#: The repository parse is shared setup, not the first test's own time.
pytestmark = pytest.mark.usefixtures("parsed_repository")

PATH = "control/src/vonk_control/fleet_profiles/apply.py"


def _site(exception_class: str, function: str) -> RaiseSite:
    return RaiseSite(PATH, exception_class, function, "x.y", 1, "message")


@pytest.mark.usefixtures("damaged_json_rows")
def test_the_registered_loops_exist_and_name_what_they_retry(
    retry_proof_graph: object,
) -> None:
    assert loop_problems(load_allowlist()) == []


def test_every_already_retried_unknown_raise_has_a_proven_loop(
    retry_proof_graph: object,
) -> None:
    assert evaluate_retry_gate(load_allowlist()) == []


def test_an_unknown_class_is_debt_where_no_loop_catches_it(
    retry_proof_graph: object,
) -> None:
    document = load_allowlist()
    assert "FleetProfileAdmissionBusy" in unknown_classes()
    # The class is unknown-outcome, but nothing proves this function is retried.
    verdict = classify(_site("FleetProfileAdmissionBusy", "no_such_function"), document)
    assert verdict.category == DEBT


def test_an_unknown_class_is_retried_where_a_registered_loop_catches_it(
    retry_proof_graph: object,
) -> None:
    document = load_allowlist()
    function = "FleetProfileService._prepare_pending_admission"
    assert proven(document, PATH, "FleetProfileAdmissionEffectBusy", function)
    verdict = classify(_site("FleetProfileAdmissionEffectBusy", function), document)
    assert verdict.category == RETRIED


def test_without_the_loop_the_same_claim_is_refused(retry_proof_graph: object) -> None:
    document = copy.deepcopy(load_allowlist())
    retried = [
        family
        for family in document["fail_closed"]  # type: ignore[attr-defined]
        if family["category"] == "already-retried"
    ]
    assert retried
    document["retry_loops"] = []
    assert evaluate_retry_gate(document)
    assert any(unproven_sites(document, family["sites"]) for family in retried)


def test_a_handler_that_raises_is_not_a_retry() -> None:
    names = frozenset({"SomeUnknown"})
    retrying = ast.parse("try:\n    f()\nexcept SomeUnknown:\n    pass\n").body[0]
    raising = ast.parse("try:\n    f()\nexcept SomeUnknown:\n    raise\n").body[0]
    broad = ast.parse("try:\n    f()\nexcept Exception:\n    pass\n").body[0]
    assert _handlers_catching(retrying, names)  # type: ignore[arg-type]
    assert not _handlers_catching(raising, names)  # type: ignore[arg-type]
    assert not _handlers_catching(broad, names)  # type: ignore[arg-type]


# ------------------------------------------------- the call graph, edge by edge

_BASE = """
class UnknownOutcomeError(Exception): ...
class Busy(UnknownOutcomeError): ...
"""
M = "pkg/m.py"


def _graph(
    sources: Mapping[str, str], declared: tuple[DeclaredEdge, ...] = ()
) -> CallGraph:
    trees = {
        f"pkg/{name}.py": ast.parse(textwrap.dedent(source))
        for name, source in sources.items()
    }
    return CallGraph(trees, declared)


def _prove(
    source: str,
    function: str,
    *,
    loops: Mapping[str, tuple[str, ...]] | None = None,
    declared: tuple[DeclaredEdge, ...] = (),
    extra: Mapping[str, str] | None = None,
    exception_class: str = "Busy",
) -> Proof:
    """Prove ``raise Busy`` in ``function`` of a one-module package.

    ``loops`` maps a function of the module to the exception names it retries.
    """

    graph = _graph({"m": _BASE + textwrap.dedent(source), **(extra or {})}, declared)
    registered = {
        (M, name): frozenset(caught) for name, caught in (loops or {}).items()
    }
    return Prover(graph, registered).prove(M, exception_class, function)


def _kinds(proof: Proof) -> set[str]:
    return {kind for kind, _function in proof.unlooped}


def test_a_loop_that_calls_the_raiser_inside_its_try_proves_it() -> None:
    proof = _prove(
        """
        def work():
            raise Busy("later")
        def tick():
            try:
                work()
            except Busy:
                pass
        """,
        "work",
        loops={"tick": ("Busy",)},
    )
    assert proof.proven
    assert proof.loop is not None and proof.loop.qualname == "tick"


def test_a_receiver_is_typed_by_annotation_not_matched_by_method_name() -> None:
    source = """
        class Builds:
            def prepare(self):
                raise Busy("builds")
        class Other:
            def prepare(self):
                raise Busy("other")
        class Loop:
            def __init__(self, builds: Builds | None) -> None:
                self._builds = builds
            def tick(self):
                try:
                    self._builds.prepare()
                except Busy:
                    pass
        """
    assert _prove(source, "Builds.prepare", loops={"Loop.tick": ("Busy",)}).proven
    # A same-named method of another class is not reached by that call: crediting
    # it by name is the over-credit this proof exists to refuse.
    decoy = _prove(source, "Other.prepare", loops={"Loop.tick": ("Busy",)})
    assert not decoy.proven and decoy.loop is None


def test_an_attribute_built_in_init_is_typed_by_its_constructor() -> None:
    proof = _prove(
        """
        class Reader:
            def list(self):
                raise Busy("x")
        class Loop:
            def __init__(self) -> None:
                self._reader = Reader()
            def tick(self):
                try:
                    return self._reader.list()
                except Busy:
                    return None
        """,
        "Reader.list",
        loops={"Loop.tick": ("Busy",)},
    )
    assert proof.proven


def test_a_protocol_call_reaches_every_implementation() -> None:
    source = """
        from typing import Protocol
        class Reader(Protocol):
            def list(self) -> list[str]: ...
        class FileReader:
            def list(self):
                raise Busy("file")
        class SqlReader:
            def list(self):
                raise Busy("sql")
        class Unrelated:
            def other(self):
                raise Busy("other")
        class Loop:
            def __init__(self, reader: Reader) -> None:
                self._reader = reader
            def tick(self):
                try:
                    self._reader.list()
                except Busy:
                    pass
        """
    loops = {"Loop.tick": ("Busy",)}
    assert _prove(source, "FileReader.list", loops=loops).proven
    assert _prove(source, "SqlReader.list", loops=loops).proven
    assert not _prove(source, "Unrelated.other", loops=loops).proven


def test_a_call_on_a_base_reaches_the_override_of_a_subclass() -> None:
    proof = _prove(
        """
        class Base:
            def get(self):
                return 1
        class Child(Base):
            def get(self):
                raise Busy("child")
        class Loop:
            def __init__(self, source: Base) -> None:
                self._source = source
            def tick(self):
                try:
                    self._source.get()
                except Busy:
                    pass
        """,
        "Child.get",
        loops={"Loop.tick": ("Busy",)},
    )
    assert proof.proven


_CALLBACK = """
    def run(before_publish=None, other=None):
        if before_publish is not None:
            before_publish()
    class Svc:
        def persist(self):
            raise Busy("x")
        def tick(self):
            try:
                {call}
            except Busy:
                pass
    """


@pytest.mark.parametrize(
    "call",
    ["run(before_publish=self.persist)", "run(self.persist)"],
)
def test_a_callback_passed_by_name_is_called_where_its_parameter_is_called(
    call: str,
) -> None:
    proof = _prove(
        _CALLBACK.format(call=call), "Svc.persist", loops={"Svc.tick": ("Busy",)}
    )
    assert proof.proven


def test_a_callback_is_bound_to_the_parameter_it_fills_only() -> None:
    # ``other`` is never called, so passing ``persist`` there reaches nothing.
    proof = _prove(
        _CALLBACK.format(call="run(other=self.persist)"),
        "Svc.persist",
        loops={"Svc.tick": ("Busy",)},
    )
    assert not proof.proven and proof.loop is None


def test_a_callback_stored_by_a_constructor_is_called_through_the_attribute() -> None:
    proof = _prove(
        """
        from typing import Callable
        class Worker:
            def __init__(self, cb: Callable[[], None]) -> None:
                self._cb = cb
            def go(self):
                self._cb()
        class Svc:
            def persist(self):
                raise Busy("x")
            def tick(self):
                try:
                    Worker(self.persist).go()
                except Busy:
                    pass
        """,
        "Svc.persist",
        loops={"Svc.tick": ("Busy",)},
    )
    assert proof.proven


def test_a_callback_that_escapes_to_a_thread_is_not_credited_to_the_try() -> None:
    proof = _prove(
        """
        import threading
        class Svc:
            def persist(self):
                raise Busy("x")
            def tick(self):
                try:
                    threading.Thread(target=self.persist).start()
                except Busy:
                    pass
        """,
        "Svc.persist",
        loops={"Svc.tick": ("Busy",)},
    )
    assert not proof.proven and "reference-escape" in _kinds(proof)


_POOL = """
    from concurrent.futures import ThreadPoolExecutor
    class Svc:
        def __init__(self) -> None:
            self._pool = ThreadPoolExecutor(1)
        def transfer(self):
            raise Busy("x")
        def start(self):
            return self._pool.submit(self.transfer)
        def tick(self, future):
            try:
                future.result()
            except Busy:
                pass
    """


def test_a_pool_task_without_a_declared_waiter_is_an_unlooped_entry() -> None:
    proof = _prove(_POOL, "Svc.transfer", loops={"Svc.tick": ("Busy",)})
    assert not proof.proven and "no-caller" in _kinds(proof)


def test_a_pool_task_is_retried_where_the_waiter_of_its_future_catches_it() -> None:
    declared = (
        DeclaredEdge(
            M,
            "Svc.tick",
            ((M, "Svc.transfer"),),
            "the pool runs transfer and its exception surfaces at result()",
        ),
    )
    proof = _prove(
        _POOL, "Svc.transfer", loops={"Svc.tick": ("Busy",)}, declared=declared
    )
    assert proof.proven


def test_a_pool_task_whose_result_is_read_outside_the_try_is_not_proven() -> None:
    source = _POOL.replace(
        "try:\n                future.result()\n            except Busy:\n                pass",
        "future.result()\n            try:\n                pass\n            except Busy:\n                pass",
    )
    declared = (
        DeclaredEdge(
            M,
            "Svc.tick",
            ((M, "Svc.transfer"),),
            "the pool runs transfer and its exception surfaces at result()",
        ),
    )
    proof = _prove(
        source, "Svc.transfer", loops={"Svc.tick": ("Busy",)}, declared=declared
    )
    assert not proof.proven


def test_a_closure_returned_by_a_factory_is_called_through_its_alias() -> None:
    proof = _prove(
        """
        def make(storage):
            def prepare(before_publish=None):
                before_publish()
            return prepare
        class Svc:
            def persist(self):
                raise Busy("x")
            def tick(self):
                prepare = make(1)
                try:
                    prepare(before_publish=self.persist)
                except Busy:
                    pass
        """,
        "Svc.persist",
        loops={"Svc.tick": ("Busy",)},
    )
    assert proof.proven


_GETATTR = """
    class Svc:
        def refresh(self):
            raise Busy("x")
    class Loop:
        def __init__(self, svc: Svc) -> None:
            self._svc = svc
        def tick(self, kind):
            handler = getattr(self._svc, {name})
            try:
                handler()
            except Busy:
                pass
    """


def test_a_getattr_of_a_literal_name_is_a_call_of_that_method() -> None:
    proof = _prove(
        _GETATTR.format(name='"refresh", None'),
        "Svc.refresh",
        loops={"Loop.tick": ("Busy",)},
    )
    assert proof.proven


def test_a_getattr_of_a_computed_name_proves_nothing_until_an_edge_is_declared() -> (
    None
):
    source = _GETATTR.format(name="kind")
    loops = {"Loop.tick": ("Busy",)}
    assert not _prove(source, "Svc.refresh", loops=loops).proven
    declared = (
        DeclaredEdge(
            M,
            "Loop.tick",
            ((M, "Svc.refresh"),),
            "kind always names the refresh method of the service",
        ),
    )
    assert _prove(source, "Svc.refresh", loops=loops, declared=declared).proven
    graph = _graph({"m": _BASE + textwrap.dedent(source)}, declared)
    assert graph.declared_problems() == []
    assert [function.qualname for function in graph.dynamic_calls] == ["Loop.tick"]


def test_a_declared_edge_needs_a_reason_and_real_ends() -> None:
    graph = _graph(
        {"m": _BASE + "def a():\n    pass\n"},
        (DeclaredEdge(M, "a", ((M, "missing"),), "why"),),
    )
    problems = graph.declared_problems()
    assert any("target does not exist" in problem for problem in problems)
    assert any("needs a written reason" in problem for problem in problems)


def test_a_declared_edge_keeps_the_try_context_of_the_dynamic_call() -> None:
    # The dynamic call is outside the try: declaring where it goes cannot make the
    # try a retry of it.
    source = """
        class Svc:
            def refresh(self):
                raise Busy("x")
        class Loop:
            def __init__(self, svc: Svc) -> None:
                self._svc = svc
            def tick(self, kind):
                handler = getattr(self._svc, kind)
                handler()
                try:
                    pass
                except Busy:
                    pass
        """
    declared = (
        DeclaredEdge(
            M,
            "Loop.tick",
            ((M, "Svc.refresh"),),
            "kind always names the refresh method of the service",
        ),
    )
    proof = _prove(
        source, "Svc.refresh", loops={"Loop.tick": ("Busy",)}, declared=declared
    )
    assert not proof.proven


def test_calls_follow_the_names_a_module_imports() -> None:
    graph = _graph(
        {
            "m": _BASE
            + textwrap.dedent(
                """
                from .work import do
                from . import helpers
                def tick():
                    try:
                        do()
                        helpers.also()
                    except Busy:
                        pass
                """
            ),
            "work": "def do():\n    raise Exception('x')\n",
            "helpers": "def also():\n    raise Exception('y')\n",
        }
    )
    prover = Prover(graph, {(M, "tick"): frozenset({"Busy"})})
    # Both raisers live in modules the loop imports from, not in the loop's own.
    for path, function in (("pkg/work.py", "do"), ("pkg/helpers.py", "also")):
        callee = graph.by_key[(path, function)]
        assert [edge.caller.qualname for edge in graph.callers[callee]] == ["tick"]
        assert prover.prove(path, "Busy", function).loop is not None


def test_an_untyped_receiver_is_never_the_evidence_of_a_retry() -> None:
    source = """
        class Svc:
            def refresh(self):
                raise Busy("x")
        def tick(handler):
            try:
                handler.refresh()
            except Busy:
                pass
        """
    # ``handler`` has no type: the call may be a ``Svc.refresh`` call, so the name
    # match counts as a caller to be looped, but not as proof that a loop retries.
    proof = _prove(source, "Svc.refresh", loops={"tick": ("Busy",)})
    assert not proof.proven and proof.loop is None
    graph = _graph({"m": _BASE + textwrap.dedent(source)})
    refresh = graph.by_key[(M, "Svc.refresh")]
    assert [edge.kind for edge in graph.callers[refresh]] == ["fallback"]


def test_a_fallback_caller_outside_the_loop_still_withdraws_the_credit() -> None:
    proof = _prove(
        """
        class Svc:
            def refresh(self):
                raise Busy("x")
        class Loop:
            def __init__(self, svc: Svc) -> None:
                self._svc = svc
            def tick(self):
                try:
                    self._svc.refresh()
                except Busy:
                    pass
        def elsewhere(handler):
            handler.refresh()
        """,
        "Svc.refresh",
        loops={"Loop.tick": ("Busy",)},
    )
    assert proof.reached_by_a_loop and not proof.proven
    assert ("no-caller", "elsewhere") in {
        (kind, function.qualname) for kind, function in proof.unlooped
    }


def test_a_library_method_name_on_an_untyped_receiver_is_not_a_call_of_ours() -> None:
    source = """
        class Runner:
            def execute(self):
                raise Busy("x")
        def tick(session):
            try:
                session.execute(1)
            except Busy:
                pass
        """
    graph = _graph({"m": _BASE + textwrap.dedent(source)})
    execute = graph.by_key[(M, "Runner.execute")]
    assert graph.callers.get(execute) is None and graph.generic_calls == 1


def test_a_handler_for_a_subclass_of_a_registered_catch_is_the_same_retry() -> None:
    source = """
        class Conflict(Exception): ...
        class Pending(UnknownOutcomeError, Conflict): ...
        def work():
            raise Pending("x")
        def tick():
            try:
                work()
            except Pending:
                pass
            except Conflict:
                pass
        """
    proof = _prove(
        source, "work", loops={"tick": ("Conflict",)}, exception_class="Pending"
    )
    assert proof.proven
    other = _prove(
        source, "work", loops={"tick": ("Unrelated",)}, exception_class="Pending"
    )
    assert not other.proven


def test_a_task_awaited_on_a_thread_runs_under_the_awaiting_try() -> None:
    template = """
        import asyncio
        def work():
            raise Busy("x")
        async def tick():
            try:
                {call}
            except Busy:
                pass
        """
    loops = {"tick": ("Busy",)}
    for call in (
        "await asyncio.to_thread(lambda: work())",
        "await asyncio.to_thread(work)",
    ):
        source = textwrap.dedent(template).format(call=call)
        assert _prove(source, "work", loops=loops).proven, call
    # Not awaited there: the exception surfaces elsewhere, so the try is no retry.
    for call in (
        "asyncio.create_task(asyncio.to_thread(work))",
        "callbacks.append(lambda: work())",
    ):
        source = textwrap.dedent(template).format(call=call)
        assert not _prove(source, "work", loops=loops).proven, call


# ------------------------------------------------------------- the flow rule


def test_a_raiser_also_reached_without_the_loop_is_not_proven() -> None:
    proof = _prove(
        """
        def work():
            raise Busy("later")
        def tick():
            try:
                work()
            except Busy:
                pass
        def api_handler():
            work()
        """,
        "work",
        loops={"tick": ("Busy",)},
    )
    # The loop reaches it, but so does a caller nobody wraps: still debt.
    assert proof.reached_by_a_loop and not proof.proven
    assert ("no-caller", "api_handler") in {
        (kind, function.qualname) for kind, function in proof.unlooped
    }


def test_a_route_that_reaches_the_raiser_is_an_unlooped_path() -> None:
    proof = _prove(
        """
        router = object()
        def work():
            raise Busy("later")
        def tick():
            try:
                work()
            except Busy:
                pass
        @router.post("/x")
        def handler():
            work()
        """,
        "work",
        loops={"tick": ("Busy",)},
    )
    assert not proof.proven and "route" in _kinds(proof)


def test_a_loop_that_also_calls_the_raiser_outside_its_try_is_not_proven() -> None:
    proof = _prove(
        """
        def work():
            raise Busy("later")
        def tick():
            work()
            try:
                work()
            except Busy:
                pass
        """,
        "work",
        loops={"tick": ("Busy",)},
    )
    assert proof.reached_by_a_loop and not proof.proven


def test_a_helper_that_swallows_the_exception_hides_it_from_the_loop() -> None:
    source = """
        def work():
            raise Busy("later")
        def helper():
            try:
                work()
            except Busy:
                return None
        def tick():
            try:
                helper()
            except Busy:
                pass
        """
    proof = _prove(source, "work", loops={"tick": ("Busy",)})
    assert not proof.proven and "swallowed" in _kinds(proof)
    # A helper that re-raises (here as itself) lets the loop see it.
    passes = source.replace("return None", "raise")
    assert _prove(passes, "work", loops={"tick": ("Busy",)}).proven


def test_a_loop_only_retries_the_classes_it_is_registered_for() -> None:
    proof = _prove(
        """
        def work():
            raise Busy("later")
        def tick():
            try:
                work()
            except Busy:
                pass
        """,
        "work",
        loops={"tick": ("SomethingElse",)},
    )
    assert not proof.proven and "swallowed" in _kinds(proof)


def test_a_handler_by_a_builtin_or_broad_class_is_no_retry() -> None:
    proof = _prove(
        """
        def work():
            raise Busy("later")
        def tick():
            try:
                work()
            except Exception:
                pass
        """,
        "work",
        loops={"tick": ("Exception",)},
    )
    assert not proof.proven


def test_a_raise_inside_the_loops_own_try_is_proven_and_one_outside_is_not() -> None:
    inside = """
        def tick():
            try:
                raise Busy("later")
            except Busy:
                pass
        """
    outside = """
        def tick(flag):
            if flag:
                raise Busy("early")
            try:
                pass
            except Busy:
                pass
        """
    assert _prove(inside, "tick", loops={"tick": ("Busy",)}).proven
    assert not _prove(outside, "tick", loops={"tick": ("Busy",)}).proven


def test_recursion_does_not_hide_an_unlooped_entry() -> None:
    source = """
        def a(n):
            if n:
                b(n - 1)
            raise Busy("x")
        def b(n):
            a(n)
        def tick():
            try:
                a(3)
            except Busy:
                pass
        {entry}
        """
    loops = {"tick": ("Busy",)}
    assert _prove(textwrap.dedent(source).format(entry=""), "a", loops=loops).proven
    unlooped = textwrap.dedent(source).format(entry="def other():\n    b(1)\n")
    assert not _prove(unlooped, "a", loops=loops).proven


# ------------------------------------------------------ the repository itself


def test_the_allowlist_credits_exactly_what_the_proof_proves(
    retry_proof_graph: object,
) -> None:
    document = copy.deepcopy(load_allowlist())
    # Nothing credited is unproven (the gate), and nothing proven is left as debt.
    assert evaluate_retry_gate(document) == []
    assert promote_proven(document) == 0


def test_cancellation_has_its_own_bounded_retry(
    retry_proof_graph: object,
) -> None:
    """Catches cancellation borrowing credit from an unrelated observer."""
    document = copy.deepcopy(load_allowlist())
    path = "control/src/vonk_control/run_switch_operations/cancellation_retry.py"
    function = "CancellationRetryMixin._cancel_once"
    assert proven(document, path, "RunSwitchRetryLater", function)
    verdict = classify(
        RaiseSite(path, "RunSwitchRetryLater", function, "x.y", 1, "message"),
        document,
    )
    assert verdict.category == RETRIED
    loops = document["retry_loops"]
    assert isinstance(loops, list)
    document["retry_loops"] = [
        entry for entry in loops if entry["function"] != "CancellationRetryMixin.cancel"
    ]
    assert not proven(document, path, "RunSwitchRetryLater", function)


def test_admission_proof_requires_the_result_retry_on_every_path() -> None:
    """Reject credit when worker admission retries but result admission does not."""

    source = """
        def _queue_in_session():
            raise Busy("admission writer busy")
        def _admit_workload_intent():
            _queue_in_session()
        def worker_tick():
            try:
                _admit_workload_intent()
            except Busy:
                pass
        def record_result():
            try:
                _admit_workload_intent()
            except Busy:
                pass
        """
    loops = {"worker_tick": ("Busy",), "record_result": ("Busy",)}
    for function in ("_queue_in_session", "_admit_workload_intent"):
        assert _prove(source, function, loops=loops).proven
        proof = _prove(source, function, loops={"worker_tick": ("Busy",)})
        assert proof.reached_by_a_loop and not proof.proven


def test_a_callback_through_an_injected_service_has_its_caller(
    retry_proof_graph: object,
) -> None:
    graph = build_graph_for(load_allowlist())
    callback = graph.by_key[
        (
            "control/src/vonk_control/distribution_executor/preparation.py",
            "CompositeDistributionPhaseExecutor._prepare_runtime_image.before_publish",
        )
    ]
    # Passed to a Protocol-typed ``self._runtime_image_preparer`` built by a
    # factory, and called as the ``before_publish`` parameter of the preparer.
    assert [edge.caller.simple for edge in graph.callers[callback]] == [
        "_prepare_from_build"
    ]
    assert callback not in graph.entries


_ROUTE = """
    @app.get("/x")
    def show():
        helper()
    def helper():
        raise Busy("x")
    def guard(call):
        try:
            return call()
        except Busy:
            return None
    """


def _guarded(source: str, guards: dict[str, tuple[str, ...]]) -> Proof:
    graph = _graph({"m": _BASE + textwrap.dedent(source)})
    return Prover(
        graph,
        {},
        {(M, name): frozenset(caught) for name, caught in guards.items()},
    ).prove(M, "Busy", "helper")


def test_a_route_is_an_unlooped_entry_without_a_guard() -> None:
    proof = _guarded(_ROUTE, {})
    assert not proof.proven and "route" in _kinds(proof)


def test_a_route_guard_that_answers_the_class_proves_the_route_path() -> None:
    assert _guarded(_ROUTE, {"guard": ("Busy",)}).proven


def test_a_route_guard_answers_only_the_classes_it_names() -> None:
    assert not _guarded(_ROUTE, {"guard": ("Other",)}).proven


def test_a_route_guard_must_answer_without_re_raising() -> None:
    document = copy.deepcopy(load_allowlist())
    document["route_guards"] = [  # type: ignore[index]
        {
            "path": "control/src/vonk_control/strict_json.py",
            "function": "ControllerAPIRoute.__init__",
            "catches": ["UnknownOutcomeError"],
            "reason": "this function does not answer anything at all",
        }
    ]
    problems = loop_problems(document)
    assert any("does not answer UnknownOutcomeError" in item for item in problems)


def test_the_route_guards_of_the_allowlist_exist_and_answer() -> None:
    document = load_allowlist()
    assert document["route_guards"]  # type: ignore[index]
    assert loop_problems(document) == []


def test_a_route_whose_reference_also_escapes_is_still_a_guarded_route() -> None:
    source = _ROUTE + "\n    handlers = [show]\n"
    proof = _guarded(source, {"guard": ("Busy",)})
    assert proof.proven


def test_endpoint_capture_guard_does_not_guard_an_unrelated_route() -> None:
    source = (
        _ROUTE
        + """
    @app.get("/owned")
    def owned():
        try:
            helper()
        except Busy:
            return None
    """
    )
    proof = _guarded(source, {"owned": ("Busy",)})
    assert not proof.proven
    assert "route" in _kinds(proof)


def test_endpoint_capture_guard_proves_only_its_own_named_handler() -> None:
    source = """
    @app.get("/owned")
    def owned():
        try:
            helper()
        except Busy:
            return None
    def helper():
        raise Busy("x")
    """
    assert _guarded(source, {"owned": ("Busy",)}).proven


def test_conditional_capture_callback_retains_both_real_call_paths() -> None:
    source = """
    def helper():
        raise Busy("x")
    def healthy():
        return None
    def install(capture):
        @app.get("/owned")
        def owned():
            try:
                capture()
            except Busy:
                return None
    install(capture=healthy if enabled else helper)
    """
    assert _guarded(source, {"install.owned": ("Busy",)}).proven
    # An unresolved caller still makes both potential callback branches public;
    # binding the known route must not erase this independent escape.
    escaped = (
        textwrap.dedent(source) + "\nunknown(capture=healthy if enabled else helper)\n"
    )
    proof = _guarded(escaped, {"install.owned": ("Busy",)})
    assert not proof.proven and "reference-escape" in _kinds(proof)


def test_protocol_return_filter_retains_subtypes_and_uncertain_candidates() -> None:
    graph = _graph(
        {
            "m": _BASE
            + textwrap.dedent("""
        from typing import Protocol
        class Reading: pass
        class DetailedReading(Reading): pass
        class Other: pass
        class Shape(Protocol):
            def value(self): ...
        class Probe(Protocol):
            def read(self) -> Reading: ...
        class Good:
            def read(self) -> DetailedReading: ...
        class Wrong:
            def read(self) -> Other: ...
        class Unknown:
            def read(self): ...
        class External:
            def read(self) -> MissingExternalClass: ...
        class Generic:
            def read(self) -> list[Reading]: ...
        class Structural:
            def read(self) -> Shape: ...
        def observe(probe: Probe):
            probe.read()
    """)
        }
    )
    observer = graph.by_key[(M, "observe")]
    targets = {edge.callee.qualname for edge in graph.callees[observer]}
    assert "Good.read" in targets
    assert "Wrong.read" not in targets
    assert {
        "Unknown.read",
        "External.read",
        "Generic.read",
        "Structural.read",
    } <= targets


def test_retry_proof_follows_imported_mixin_aliases_and_receiver_casts() -> None:
    """A split must retain the real sibling-method edge, never infer a loop."""
    graph = _graph(
        {
            "effects": _BASE
            + textwrap.dedent("""
                class Service:
                    def issue(self):
                        raise Busy("observation pending")
            """),
            "worker": """
                from typing import cast as receiver_cast
                from .effects import Busy
                from .service import Service as CompleteService
                class Service:
                    def tick(self):
                        self = receiver_cast("CompleteService", self)
                        try:
                            self.issue()
                        except Busy:
                            pass
            """,
            "service": """
                from .effects import Service as Effects
                from .worker import Service as Worker
                class Service(Effects, Worker):
                    pass
            """,
        }
    )
    loop = ("pkg/worker.py", "Service.tick")
    proof = Prover(graph, {loop: frozenset({"Busy"})}).prove(
        "pkg/effects.py", "Busy", "Service.issue"
    )
    assert proof.proven
    assert proof.loop is not None and proof.loop.path == loop[0]
    assert not Prover(graph, {}).prove("pkg/effects.py", "Busy", "Service.issue").proven


def test_recipe_build_unknown_outcomes_have_no_unhandled_or_swallowed_path(
    retry_proof_graph: object,
) -> None:
    """A broad catch or new entry must not silently restore build debt."""
    from .blocker_boundaries import scan_raises

    document = load_allowlist()
    path = "control/src/vonk_control/recipe_builds/persistence.py"
    for site in scan_raises():
        if (
            site.path.startswith("control/src/vonk_control/recipe_builds/")
            and site.exception_class in unknown_classes()
        ):
            assert proven(document, site.path, site.exception_class, site.function), (
                site.render()
            )

    # Removing the production handoff must invalidate persistence credit.
    damaged = copy.deepcopy(document)
    loops = damaged["retry_loops"]
    assert isinstance(loops, list)
    damaged["retry_loops"] = [
        entry
        for entry in loops
        if entry["function"] != "build_recipe_image_availability.builder"
    ]
    assert not proven(
        damaged,
        path,
        "RecipeBuildUnknown",
        "persist_plan_in_session",
    )


@pytest.mark.parametrize("escape", ["", "unknown(alias)", "return alias"])
def test_forwarded_callback_alias_keeps_unknown_consumer_as_debt(escape: str) -> None:
    """Reject credit if a forwarded callback also reaches an unknown consumer."""
    source = """
        def work():
            raise Busy("later")
        def invoke(callback):
            callback()
        def forward(callback):
            alias = callback
            invoke(alias)
            {escape}
        def tick():
            try:
                forward(work)
            except Busy:
                pass
    """
    source = textwrap.dedent(source).format(escape=escape or "pass")
    proof = _prove(source, "work", loops={"tick": ("Busy",)})
    assert proof.reached_by_a_loop
    assert proof.proven is (not escape)


@pytest.mark.parametrize("escape", [False, True])
@pytest.mark.parametrize("interface", ["protocol", "abstract"])
def test_interface_dispatch_preserves_each_call_context(
    interface: str, escape: bool
) -> None:
    """Reject credit if any interface invocation escapes the registered catch."""
    definition = (
        "from typing import Protocol as Interface\nclass Port(Interface):"
        if interface == "protocol"
        else "from abc import ABC as Interface, abstractmethod\nclass Port(Interface):"
    )
    decorator = "    @abstractmethod\n" if interface == "abstract" else ""
    source = definition + "\n" + decorator + "    def perform(self): ...\n"
    source += textwrap.dedent("""
        class Worker(Port):
            def perform(self):
                raise Busy("later")
        class StructuralWorker:
            def perform(self):
                raise Busy("later")
        def tick(port: Port):
            try:
                port.perform()
            except Busy:
                pass
    """)
    if escape:
        source += "\ndef other(port: Port):\n    port.perform()\n"
    target = "StructuralWorker.perform" if interface == "protocol" else "Worker.perform"
    proof = _prove(source, target, loops={"tick": ("Busy",)})
    assert proof.reached_by_a_loop
    assert proof.proven is not escape


@pytest.mark.parametrize("escape", [False, True])
def test_literal_getattr_alias_preserves_each_call_context(escape: bool) -> None:
    """Reject credit when the same literal method alias is called outside try."""
    source = """
        class Worker:
            def perform(self):
                raise Busy("later")
        def tick(worker: Worker):
            action = getattr(worker, "perform")
            try:
                action()
            except Busy:
                pass
            {escape}
    """
    proof = _prove(
        textwrap.dedent(source).format(escape="action()" if escape else "pass"),
        "Worker.perform",
        loops={"tick": ("Busy",)},
    )
    assert proof.reached_by_a_loop
    assert proof.proven is not escape


@pytest.mark.parametrize("dispatch", ["direct", "alias", "callback"])
def test_untyped_literal_getattr_never_supplies_retry_evidence(dispatch: str) -> None:
    """A guessed method-name edge may withdraw credit but cannot supply it."""
    call = {
        "direct": 'getattr(worker, "perform")()',
        "alias": 'action = getattr(worker, "perform"); action()',
        "callback": 'action = getattr(worker, "perform"); invoke(action)',
    }[dispatch]
    source = """
        def invoke(callback):
            callback()
        class Worker:
            def perform(self):
                raise Busy("later")
        def tick(worker):
            try:
                {call}
            except Busy:
                pass
    """
    proof = _prove(
        textwrap.dedent(source).format(call=call),
        "Worker.perform",
        loops={"tick": ("Busy",)},
    )
    assert not proof.proven


def test_model_source_literal_dispatch_has_a_typed_cache_receiver(
    retry_proof_graph: object,
) -> None:
    """Reject retry credit based only on an opaque adapter's method-name guess."""
    graph = build_graph_for(load_allowlist())
    caller = graph.by_key[
        ("control/src/vonk_control/distribution.py", "ModelCacheObjectSource._describe")
    ]
    target = graph.by_key[
        (
            "control/src/vonk_control/model_cache/availability.py",
            "AvailabilityMixin.resolve_verified_artifact_set",
        )
    ]
    edges = [edge for edge in graph.callees[caller] if edge.callee == target]
    assert edges and all(edge.kind != "fallback" for edge in edges)


@pytest.mark.parametrize("awaited", [False, True])
def test_forwarded_thread_callback_requires_observation(awaited: bool) -> None:
    """An awaited task surfaces its failure; an unobserved task escapes."""
    source = """
        import asyncio
        def work():
            raise Busy("later")
        async def forward(callback):
            {observe}asyncio.to_thread(callback)
        async def tick():
            try:
                await forward(work)
            except Busy:
                pass
    """
    proof = _prove(
        textwrap.dedent(source).format(observe="await " if awaited else ""),
        "work",
        loops={"tick": ("Busy",)},
    )
    assert proof.proven is awaited


@pytest.mark.parametrize("escape", [False, True])
def test_callback_binding_converges_beyond_a_fixed_pass_limit(escape: bool) -> None:
    """Reject truncated propagation that hides a deep callback's escape."""
    source = "def work():\n    raise Busy('later')\n"
    for index in range(16):
        invocation = f"forward_{index + 1}(alias)" if index < 15 else "alias()"
        source += (
            f"def forward_{index}(callback):\n    alias = callback\n    {invocation}\n"
        )
        if escape and index == 15:
            source += "    unknown(alias)\n"
    source += "def tick():\n    try:\n        forward_0(work)\n    except Busy:\n        pass\n"
    proof = _prove(source, "work", loops={"tick": ("Busy",)})
    assert proof.reached_by_a_loop
    assert proof.proven is not escape


def test_extracted_method_descriptor_dispatch_preserves_the_retry_catch():
    """A bound implementation is reached through its class, not a new uncaught entry."""
    graph = CallGraph(
        {
            "pkg/__init__.py": ast.parse(""),
            "pkg/source.py": ast.parse(
                "from .service import Service\ndef once(self: Service):\n    raise Busy()\n"
            ),
            "pkg/service.py": ast.parse(
                "from .source import once\nclass Service:\n    once = once\n    def observe(self):\n        try:\n            self.once()\n        except Busy:\n            pass\n"
            ),
            "pkg/worker.py": ast.parse(
                "from .service import Service\ndef tick(service: Service):\n    service.observe()\n"
            ),
        }
    )
    prover = Prover(graph, {("pkg/service.py", "Service.observe"): frozenset({"Busy"})})
    assert prover.prove("pkg/source.py", "Busy", "once").proven
