"""Versioned Job and RecipeRun writes must retain their fence; claims alone may lock.

Report lock syntax and owner references so a new chained lock or changed target
cannot hide behind an unchanged opening line. Editing an existing predicate is
not a new lock.
"""

from __future__ import annotations

import ast

KIND = "versioned owner lock or bulk write; use an optimistic decision or SKIP LOCKED claim"


def scan_source(
    source: str,
    versioned: frozenset[str] = frozenset({"Job", "RecipeRun"}),
    *,
    include_claims: bool = False,
) -> list[int]:
    tree = ast.parse(source)
    aliases = {
        alias.asname or alias.name: alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    def model(node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and aliases.get(node.id, node.id) in versioned

    def references(node: ast.AST) -> bool:
        return any(model(child) for child in ast.walk(node))

    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    imported_models = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("models")
        for alias in node.names
    }

    def refresh_target(node: ast.Call) -> bool:
        # Unknown helper results are guarded too. Exclude an instance only when
        # its enclosing function explicitly binds it to another canonical model.
        target = node.args[0]
        if not isinstance(target, ast.Name):
            return True
        scope: ast.AST = node
        while scope in parents and not isinstance(
            scope, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            scope = parents[scope]
        types: set[str] = set()
        for binding in ast.walk(scope):
            value: ast.AST | None = None
            if (
                isinstance(binding, ast.arg)
                and binding.arg == target.id
                or (
                    isinstance(binding, ast.AnnAssign)
                    and isinstance(binding.target, ast.Name)
                    and binding.target.id == target.id
                )
            ):
                value = binding.annotation
            elif isinstance(binding, ast.Assign) and any(
                isinstance(name, ast.Name) and name.id == target.id
                for name in binding.targets
            ):
                value = binding.value
            if value is not None:
                types.update(
                    child.id
                    for child in ast.walk(value)
                    if isinstance(child, ast.Name) and child.id in imported_models
                )
        return not types or any(aliases.get(name, name) in versioned for name in types)

    found: list[int] = []

    def report(node: ast.Call) -> None:
        # Attribute.lineno starts at the receiver's opening line. Its end marks
        # the actual method, after any unchanged select/where chain.
        start = node.func.end_lineno or node.lineno
        found.extend(range(start, (node.end_lineno or start) + 1))
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            name = (
                call.func.id
                if isinstance(call.func, ast.Name)
                else (call.func.attr if isinstance(call.func, ast.Attribute) else "")
            )
            if aliases.get(name, name) in {"select", "query", "AdmissionRowLock"}:
                found.extend(
                    child.lineno
                    for arg in call.args
                    for child in ast.walk(arg)
                    if isinstance(child, ast.Name) and model(child)
                )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr == "with_for_update":
            if not include_claims and any(
                keyword.arg == "skip_locked"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in node.keywords
            ):
                continue
            targets = [
                keyword.value for keyword in node.keywords if keyword.arg == "of"
            ]
            if any(references(target) for target in targets or [node.func.value]):
                report(node)
        elif isinstance(node.func, ast.Attribute) and node.func.attr in {
            "get",
            "get_one",
            "refresh",
            "query",
        }:
            lock = next(
                (kw.value for kw in node.keywords if kw.arg == "with_for_update"), None
            )
            # refresh receives an instance (possibly returned by a helper),
            # so conservatively guard untyped locking refreshes. Query/get carry
            # their model in the call itself.
            if (
                lock is not None
                and node.args
                and (
                    refresh_target(node)
                    if node.func.attr == "refresh"
                    else any(references(arg) for arg in node.args)
                )
            ):
                if (
                    not include_claims
                    and isinstance(lock, ast.Dict)
                    and any(
                        isinstance(key, ast.Constant)
                        and key.value == "skip_locked"
                        and isinstance(value, ast.Constant)
                        and value.value is True
                        for key, value in zip(lock.keys, lock.values, strict=True)
                    )
                ):
                    continue
                if not (isinstance(lock, ast.Constant) and lock.value is False):
                    report(node)
        elif (
            isinstance(node.func, ast.Name)
            and aliases.get(node.func.id, node.func.id) == "AdmissionRowLock"
        ):
            if len(node.args) > 1 and model(node.args[1]):
                report(node)
        elif (
            isinstance(node.func, ast.Name)
            and aliases.get(node.func.id, node.func.id) == "update"
        ):
            if node.args and model(node.args[0]):
                report(node)
        elif isinstance(node.func, ast.Attribute) and node.func.attr == "update":
            if references(node.func.value):
                report(node)
    return found


WRITE_KIND = "RecipeRun write outside transition; classify the caller-owned transaction"


def scan_unwrapped_recipe_run_writes(source: str) -> list[int]:
    """Guard explicit run writes and queue callbacks that can project run results.

    This is local syntax/data-flow, not an interprocedural transaction proof.
    A helper receiving a caller's session needs a verdict even when some callers
    use transition; its other callers can still commit a raw stale fence.
    """
    tree = ast.parse(source)
    aliases = {
        alias.asname or alias.name: alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }

    def transition_callback(scope: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        enclosing = parents.get(scope, tree)
        for node in ast.walk(enclosing):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else (node.func.attr if isinstance(node.func, ast.Attribute) else "")
            )
            if aliases.get(name, name) not in {"transition", "decide_publication"}:
                continue
            if any(
                isinstance(arg, ast.Name) and arg.id == scope.name
                for arg in [*node.args, *(kw.value for kw in node.keywords)]
            ):
                return True
        return False

    found: list[int] = []
    for scope in ast.walk(tree):
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if transition_callback(scope):
            continue

        def local_nodes(node: ast.AST):
            yield node
            for child in ast.iter_child_nodes(node):
                if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield from local_nodes(child)

        nodes = tuple(local_nodes(scope))
        runs = {name for name, model in aliases.items() if model == "RecipeRun"}
        runs.add("RecipeRun")

        def references_run(node: ast.AST, runs: set[str] = runs) -> bool:
            if isinstance(node, ast.Name):
                return node.id in runs
            if isinstance(node, ast.Subscript):
                return references_run(node.value)
            if isinstance(node, ast.IfExp):
                return references_run(node.body) or references_run(node.orelse)
            if isinstance(node, ast.Call):
                name = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else (
                        node.func.attr if isinstance(node.func, ast.Attribute) else ""
                    )
                )
                if name in {"get", "get_one", "select", "query"}:
                    return (
                        bool(node.args)
                        and isinstance(node.args[0], ast.Name)
                        and aliases.get(node.args[0].id, node.args[0].id) == "RecipeRun"
                        and (name not in {"select", "query"} or len(node.args) == 1)
                    )
                if name in {"scalar", "scalars", "execute", "tuple", "list", "sorted"}:
                    return bool(node.args) and references_run(node.args[0])
                if isinstance(node.func, ast.Attribute):
                    return references_run(node.func.value)
            return False

        bindings = [
            (node.target, node.iter)
            if isinstance(node, ast.For)
            else (node.target, node.annotation)
            if isinstance(node, ast.AnnAssign)
            else (target, node.value)
            for node in nodes
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.For))
            for target in (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
            if isinstance(target, (ast.Name, ast.Tuple, ast.List))
        ]
        for arg in scope.args.args:
            if arg.annotation is not None and any(
                isinstance(child, ast.Name)
                and aliases.get(child.id, child.id) == "RecipeRun"
                for child in ast.walk(arg.annotation)
            ):
                runs.add(arg.arg)
        # Propagate select -> rows -> loop variable, and ordinary aliases.
        while True:
            previous = set(runs)
            for target, value in bindings:
                if references_run(value):
                    runs.update(
                        child.id
                        for child in ast.walk(target)
                        if isinstance(child, ast.Name)
                    )
            if runs == previous:
                break
        for node in nodes:
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.ctx, ast.Store)
                and (
                    references_run(node.value)
                    or node.attr == "version"
                    and "RecipeRun" in aliases.values()
                )
            ):
                found.append(node.lineno)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_result_consumer"
            ):
                found.extend(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return found
