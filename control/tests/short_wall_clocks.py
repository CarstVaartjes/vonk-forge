"""Find a short wall-clock limit around real work in a test.

``future.result(timeout=1)`` or ``asyncio.wait_for(work, timeout=1)`` adds no
proof: pytest-timeout already ends a real hang, and on a loaded runner the
work outlives the limit and the test fails at random. Prove "does not wait" by
behaviour (a lock-wait probe, a fake clock, event order) and bound a hang
guard generously. An ``Event.wait`` the test expects to expire (``not
done.wait(0.1)``) or a polling interval in a loop (``while not stop.wait(1)``)
is not a limit on work. ``assert elapsed < 0.5`` is the same limit after the
fact: prove the order (the work returned while the blocker still held) instead.
Shortening a production budget (``monkeypatch.setattr(owner, "X_SECONDS",
0.1)``) is the same limit inside the code under test: freeze or advance the
module's clock instead (``pass_budget_clocks``). A deliberate short limit names
its reason on the call: ``# wall-clock: <reason>``.
"""

from __future__ import annotations

import ast
import re

KIND = "short-wall-clock-limit"
SHORT_SECONDS = 5
OPT_OUT = re.compile(r"#\s*wall-clock:\s*\S")

# Call name -> the positional index of its timeout (None: keyword only).
TIMEOUT_ARGUMENT: dict[str, int | None] = {
    "wait_for": 1,
    "result": 0,
    "wait": 0,
    "join": 0,
    "communicate": None,
    "get": None,
    "run": None,
    "check_call": None,
    "check_output": None,
    "Barrier": 2,
    "timeout": 0,
    "fail_after": 0,
}
_CLOCK = re.compile(r"\b(?:monotonic|perf_counter|time)\(\)$")


def _seconds(node: ast.expr, constants: dict[str, float]) -> float | None:
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    try:
        value = ast.literal_eval(node)
    except ValueError:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value


def _name(node: ast.Call) -> str:
    return ast.unparse(node.func).split(".")[-1]


def _limit(node: ast.Call) -> ast.expr | None:
    name = _name(node)
    if name not in TIMEOUT_ARGUMENT and ast.unparse(node.func) != "subprocess.call":
        return None
    for keyword in node.keywords:
        if keyword.arg == "timeout":
            return keyword.value
    index = TIMEOUT_ARGUMENT.get(name)
    if index is not None and len(node.args) > index:
        return node.args[index]
    return None


def _patched_budget(node: ast.Call) -> ast.expr | None:
    """The value a ``setattr``/``patch`` call gives a ``*_SECONDS`` constant."""
    if _name(node) not in {"setattr", "object", "patch"}:
        return None
    for index, argument in enumerate(node.args[:-1]):
        if (
            isinstance(argument, ast.Constant)
            and isinstance(argument.value, str)
            and argument.value.endswith("_SECONDS")
        ):
            return node.args[index + 1]
    return None


def _elapsed(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "elapsed" or node.id.endswith("_elapsed")
    return (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Sub)
        and isinstance(node.left, ast.Call)
        and bool(_CLOCK.search(ast.unparse(node.left)))
    )


def scan_source(source: str) -> list[int]:
    """Lines whose short wall-clock limit fails the test when work is slow."""
    tree = ast.parse(source)
    lines = source.splitlines()
    constants = {
        target.id: value
        for statement in tree.body
        if isinstance(statement, ast.Assign)
        for target in statement.targets
        if isinstance(target, ast.Name)
        and (value := _seconds(statement.value, {})) is not None
    }
    exempt: set[int] = set()
    for node in ast.walk(tree):
        # A polling interval: the loop, not the call, decides when to stop.
        if isinstance(node, ast.While | ast.For | ast.AsyncFor):
            if isinstance(node, ast.While):
                exempt.update(id(call) for call in ast.walk(node.test))
            # An interruptible sleep (``if stop.wait(0.05)``); a bare wait is
            # one only in a while loop, since ``Popen.wait`` raises on expiry.
            exempt.update(
                id(statement.test)
                for statement in ast.walk(node)
                if isinstance(statement, ast.If)
            )
            if isinstance(node, ast.While):
                exempt.update(
                    id(statement.value)
                    for statement in ast.walk(node)
                    if isinstance(statement, ast.Expr)
                )
        # The test expects the wait to expire; a slow runner cannot fail it.
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            exempt.add(id(node.operand))
        elif (
            isinstance(node, ast.Compare)
            and isinstance(node.ops[0], ast.Is)
            and isinstance(node.comparators[0], ast.Constant)
            and node.comparators[0].value is False
        ):
            exempt.add(id(node.left))
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.expr):
            continue
        limit: ast.expr | None = None
        if isinstance(node, ast.Call):
            if id(node) in exempt and _name(node) == "wait":
                continue
            limit = _limit(node) or _patched_budget(node)
        elif (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Add)
            and isinstance(node.left, ast.Call)
            and _CLOCK.search(ast.unparse(node.left))
        ):
            limit = node.right
        elif isinstance(node, ast.Compare):
            # ``assert elapsed < 0.5``: a latency budget measured on the wall.
            operands = (node.left, *node.comparators)
            limit = next(
                (
                    operands[index + 1]
                    for index, op in enumerate(node.ops)
                    if isinstance(op, ast.Lt | ast.LtE) and _elapsed(operands[index])
                ),
                None,
            )
        if limit is None:
            continue
        seconds = _seconds(limit, constants)
        if seconds is None or seconds <= 0 or seconds > SHORT_SECONDS:
            continue
        span = lines[node.lineno - 1 : node.end_lineno or node.lineno]
        if not any(OPT_OUT.search(text) for text in span):
            found.add(node.lineno)
    return sorted(found)
