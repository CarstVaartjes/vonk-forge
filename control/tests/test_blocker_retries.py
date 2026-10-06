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
    proof_of,
    proven,
    unknown_classes,
    unproven_sites,
)

#: The repository parse is shared setup, not the first test's own time.
pytestmark = pytest.mark.usefixtures("parsed_repository")

PATH = "control/src/vonk_control/fleet_profiles.py"


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


def test_a_retry_reached_through_a_loop_on_some_paths_only_stays_debt(
    retry_proof_graph: object,
) -> None:
    document = load_allowlist()
    path = "control/src/vonk_control/recipe_operations.py"
    for cls, function in (
        ("InstallAdmissionBusy", "RecipeOperationService._queue_in_session"),
        ("InstallAdmissionBusy", "RecipeOperationService._admit_workload_intent"),
    ):
        proof = proof_of(document, path, cls, function)
        assert proof.reached_by_a_loop and not proof.proven
        assert proven(document, path, cls, function) is None


def test_a_callback_through_an_injected_service_has_its_caller(
    retry_proof_graph: object,
) -> None:
    graph = build_graph_for(load_allowlist())
    callback = graph.by_key[
        (
            "control/src/vonk_control/distribution_executor.py",
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
