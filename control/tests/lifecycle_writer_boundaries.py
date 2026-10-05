"""Static ratchet: lifecycle state is written by the shared core, nowhere else.

``vonk_control/lifecycle`` owns one pure transition function for every lifecycle
subject (the design is in the blocker audit, section 5).  Until every kind has
moved onto it, the existing writers are listed in
``tools/lifecycle-writers-allowlist.json``, each tagged with the migration step
(``migrating_in``) that removes it.  The gate is a ratchet, like the
content-identity scanner and its allowlist:

* a write the allowlist does not name fails (a new writer must go through the
  core, not around it);
* an entry whose writer is gone fails as stale (delete it);
* an entry whose count moved fails, up (a second writer hid behind a listed
  one) and down (lower the recorded count), and the file's ``max_writes``
  ceiling may only go down;
* the file is empty when the last kind has migrated.

A *write* is any of: an attribute assignment ``x.state = ...`` where ``x`` is a
lifecycle row, a ``["state"] = ...`` store into a progress or application
document inside a module that owns a lifecycle kind, ``update(Model)...values(
state=...)``, ``Model(..., state=...)``, ``setattr(x, "state", ...)`` and a
call to ``_set_application_state``.  ``x`` is resolved to a lifecycle model
from the imports of ``vonk_control.models``, parameter annotations, constructor
calls, ``session.get(Model, ...)``, ``select(Model)`` results and helper return
annotations.  The resolution is intentionally simple: a write it cannot attach
to a model is invisible to this scan, so ``test_lifecycle_writer_boundaries``
also checks that no unresolved ``.state`` write sits in a module that owns a
lifecycle kind unless it is named in ``UNRESOLVED_STATE_WRITE_ALLOWED``.

Writes inside ``vonk_control/lifecycle/`` are the core and are never listed.
"""

from __future__ import annotations

import ast
import json
import sys
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROL_SOURCE_ROOT = REPO_ROOT / "control" / "src"
ALLOWLIST_PATH = REPO_ROOT / "tools" / "lifecycle-writers-allowlist.json"
CORE_PREFIX = "control/src/vonk_control/lifecycle/"
MODELS_MODULE = "control/src/vonk_control/models.py"

LIFECYCLE_MODELS = frozenset(
    {
        "Job",
        "JobAttempt",
        "AgentOperation",
        "AgentOperationAttempt",
        "ModelCacheOperation",
        "ArtifactJob",
        "FleetProfileApplication",
    }
)
ATTRIBUTE = "attribute"
DICT_ITEM = "dict-item"
BULK_UPDATE = "bulk-update"
CONSTRUCTOR = "constructor"
HELPER_CALL = "helper-call"
KINDS = frozenset({ATTRIBUTE, DICT_ITEM, BULK_UPDATE, CONSTRUCTOR, HELPER_CALL})
#: The steps of the migration in the blocker audit, section 5.6.
MIGRATION_STEPS = frozenset(
    {"step-2", "step-3", "step-4", "step-5", "step-6", "step-7"}
)
#: Modules that store a lifecycle ``state`` inside a progress or application
#: document; a ``["state"] =`` store elsewhere is not a lifecycle write.
DICT_STATE_OWNERS = frozenset(
    {
        "control/src/vonk_control/agent_jobs.py",
        "control/src/vonk_control/artifact_jobs.py",
        "control/src/vonk_control/fleet_profiles.py",
        "control/src/vonk_control/jobs.py",
        "control/src/vonk_control/model_cache.py",
        "control/src/vonk_control/recipe_operations.py",
        "control/src/vonk_control/run_switch_operations.py",
    }
)
#: Variables the resolver cannot type (they come out of a mapping lookup or a
#: loop over one) but whose model is fixed by the module's own conventions.
NAME_HINTS: dict[str, dict[str, str]] = {
    "control/src/vonk_control/agent_jobs.py": {
        "operation": "AgentOperation",
        "child": "AgentOperation",
        "parent": "Job",
        "attempt": "AgentOperationAttempt",
    },
    "control/src/vonk_control/recipe_operations.py": {"job": "Job"},
}
#: ``.state`` writes in a lifecycle-owning module on a variable that is
#: provably not a lifecycle row (a run, a node, an installation, a cache set).
#: Any other unresolved ``variable.state = ...`` in these modules fails
#: ``test_no_unresolved_state_write_hides_in_a_lifecycle_module``.
NON_LIFECYCLE_STATE_VARIABLES: dict[str, frozenset[str]] = {
    "control/src/vonk_control/agent_jobs.py": frozenset(
        {"installation", "reservation", "run"}
    ),
    "control/src/vonk_control/model_cache.py": frozenset({"row"}),  # ModelCacheSet
    "control/src/vonk_control/recipe_operations.py": frozenset(
        {
            "build",
            "claim",
            "installation",
            "node",
            "reservation",
            "run",
            "started_node",
        }
    ),
    "control/src/vonk_control/run_switch_operations.py": frozenset(
        {"failed_node", "self"}
    ),
}
_STATE_HELPERS = frozenset({"_set_application_state"})
_STATE_HELPER_MODEL = "FleetProfileApplication"
_QUERY_BUILDERS = frozenset({"select", "update", "delete"})
_ROW_GETTERS = frozenset({"get", "get_one"})


@dataclass(frozen=True)
class Write:
    """One lifecycle state write."""

    path: str
    function: str
    model: str
    kind: str
    line: int

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return (self.path, self.function, self.model, self.kind)

    def render(self) -> str:
        return (
            f"{self.path}:{self.line}: {self.kind} of {self.model}.state in "
            f"{self.function}"
        )


def _annotation_names(node: ast.AST | None) -> Iterator[str]:
    if node is None:
        return
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            yield from _annotation_names(ast.parse(node.value, mode="eval").body)
        except SyntaxError:
            return
        return
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            yield child.id
        elif isinstance(child, ast.Attribute):
            yield child.attr


def _import_aliases(tree: ast.Module) -> dict[str, str]:
    """Local name -> canonical lifecycle model, from ``.models`` imports."""

    aliases: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and (node.module == "models" or node.module.endswith(".models"))
            and node.level in (0, 1)
        ):
            for imported in node.names:
                if imported.name in LIFECYCLE_MODELS:
                    aliases[imported.asname or imported.name] = imported.name
    return aliases


class _Resolver:
    """Flow-insensitive variable -> lifecycle model resolution for one module."""

    def __init__(self, tree: ast.Module, aliases: dict[str, str], path: str) -> None:
        self.aliases = aliases
        self.hints = NAME_HINTS.get(path, {})
        self.returns: dict[str, str] = {}
        self.tuple_returns: dict[str, list[str | None]] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                returned = node.returns
                if (
                    isinstance(returned, ast.Subscript)
                    and isinstance(returned.value, ast.Name)
                    and returned.value.id == "tuple"
                    and isinstance(returned.slice, ast.Tuple)
                ):
                    self.tuple_returns.setdefault(
                        node.name,
                        [self.model_in_annotation(e) for e in returned.slice.elts],
                    )
                    continue
                model = self.model_in_annotation(returned)
                if model is not None:
                    self.returns.setdefault(node.name, model)

    def model_in_annotation(self, node: ast.AST | None) -> str | None:
        found = {
            self.aliases[name]
            for name in _annotation_names(node)
            if name in self.aliases
        }
        return next(iter(found)) if len(found) == 1 else None

    def model_of(self, node: ast.AST | None, env: dict[str, str]) -> str | None:
        if node is None:
            return None
        if isinstance(node, ast.Name):
            return env.get(node.id) or self.hints.get(node.id)
        if isinstance(node, ast.Call):
            func = node.func
            tail = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if isinstance(func, ast.Name) and func.id in self.aliases:
                return self.aliases[func.id]
            if tail in _QUERY_BUILDERS or tail in _ROW_GETTERS:
                for argument in node.args[:1]:
                    if isinstance(argument, ast.Name) and argument.id in self.aliases:
                        return self.aliases[argument.id]
            if tail in self.returns:
                return self.returns[tail]
            # ``session.scalars(select(Model)...).first()``: look through.
            for child in [*node.args, *(k.value for k in node.keywords)]:
                found = self.model_of(child, env)
                if found is not None and isinstance(child, (ast.Call, ast.Name)):
                    return found
            if isinstance(func, ast.Attribute):
                return self.model_of(func.value, env)
            return None
        if isinstance(node, ast.Attribute):
            return (
                self.model_of(node.value, env)
                if node.attr in {"one", "first"}
                else None
            )
        if isinstance(node, ast.IfExp):
            return self.model_of(node.body, env) or self.model_of(node.orelse, env)
        if isinstance(node, ast.BoolOp):
            for value in node.values:
                found = self.model_of(value, env)
                if found is not None:
                    return found
            return None
        if isinstance(node, ast.Await):
            return self.model_of(node.value, env)
        if isinstance(node, ast.Subscript):
            return self.model_of(node.value, env)
        return None

    def environment(
        self, function: ast.FunctionDef | ast.AsyncFunctionDef, outer: dict[str, str]
    ) -> dict[str, str]:
        env = dict(outer)
        arguments = [
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
        ]
        for argument in arguments:
            model = self.model_in_annotation(argument.annotation)
            if model is not None:
                env[argument.arg] = model
        # Two passes so that a use before the assignment text still resolves.
        for _ in range(2):
            for node in _walk_function(function):
                self._bind(node, env)
        return env

    def _bind(self, node: ast.AST, env: dict[str, str]) -> None:
        if isinstance(node, ast.Assign):
            if (
                isinstance(node.value, ast.Call)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Tuple)
            ):
                func = node.value.func
                tail = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else func.id
                    if isinstance(func, ast.Name)
                    else None
                )
                positions = self.tuple_returns.get(tail or "", [])
                for target, position in zip(
                    node.targets[0].elts, positions, strict=False
                ):
                    if position is not None and isinstance(target, ast.Name):
                        env[target.id] = position
            model = self.model_of(node.value, env)
            if model is not None:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        env[target.id] = model
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            model = self.model_in_annotation(node.annotation) or self.model_of(
                node.value, env
            )
            if model is not None:
                env[node.target.id] = model
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            model = self.model_of(node.iter, env)
            if model is not None and isinstance(node.target, ast.Name):
                env[node.target.id] = model
        elif isinstance(node, ast.NamedExpr):
            model = self.model_of(node.value, env)
            if model is not None and isinstance(node.target, ast.Name):
                env[node.target.id] = model
        elif isinstance(node, ast.With):
            for item in node.items:
                model = self.model_of(item.context_expr, env)
                if model is not None and isinstance(item.optional_vars, ast.Name):
                    env[item.optional_vars.id] = model


def _walk_function(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> Iterator[ast.AST]:
    """Nodes of ``function`` without descending into nested functions."""

    stack: list[ast.AST] = list(function.body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _values_chain_model(call: ast.Call, aliases: dict[str, str]) -> str | None:
    """The model of ``update(Model)....values(...)``."""

    current: ast.AST = call
    while isinstance(current, ast.Call) and isinstance(current.func, ast.Attribute):
        current = current.func.value
    if (
        isinstance(current, ast.Call)
        and isinstance(current.func, ast.Name)
        and current.func.id == "update"
        and current.args
        and isinstance(current.args[0], ast.Name)
    ):
        return aliases.get(current.args[0].id)
    return None


class _Collector:
    def __init__(self, path: str, tree: ast.Module) -> None:
        self.path = path
        self.aliases = _import_aliases(tree)
        self.resolver = _Resolver(tree, self.aliases, path)
        self.writes: list[Write] = []
        self.unresolved: list[tuple[str, str, int]] = []
        self.dict_owner = path in DICT_STATE_OWNERS
        self._walk(tree.body, [], {})

    def _add(self, scope: list[str], model: str, kind: str, node: ast.AST) -> None:
        self.writes.append(
            Write(
                path=self.path,
                function=".".join(scope) or "<module>",
                model=model,
                kind=kind,
                line=getattr(node, "lineno", 0),
            )
        )

    def _walk(
        self, body: Sequence[ast.stmt], scope: list[str], outer: dict[str, str]
    ) -> None:
        for statement in body:
            if isinstance(statement, ast.ClassDef):
                self._walk(statement.body, [*scope, statement.name], outer)
            elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._function(statement, [*scope, statement.name], outer)
            else:
                self._statement(statement, scope, outer)

    def _function(
        self,
        function: ast.FunctionDef | ast.AsyncFunctionDef,
        scope: list[str],
        outer: dict[str, str],
    ) -> None:
        env = self.resolver.environment(function, outer)
        for node in _walk_function(function):
            self._inspect(node, scope, env)
        for nested in _nested_functions(function):
            self._function(nested, [*scope, nested.name], env)

    def _statement(
        self, statement: ast.stmt, scope: list[str], env: dict[str, str]
    ) -> None:
        stack: list[ast.AST] = [statement]
        while stack:
            node = stack.pop()
            self._inspect(node, scope, env)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            stack.extend(ast.iter_child_nodes(node))

    def _inspect(self, node: ast.AST, scope: list[str], env: dict[str, str]) -> None:
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                self._target(target, node, scope, env)
        elif isinstance(node, ast.Call):
            self._call(node, scope, env)

    def _target(
        self, target: ast.AST, node: ast.AST, scope: list[str], env: dict[str, str]
    ) -> None:
        if isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._target(element, node, scope, env)
            return
        if isinstance(target, ast.Attribute) and target.attr == "state":
            model = self.resolver.model_of(target.value, env)
            if model is not None:
                self._add(scope, model, ATTRIBUTE, node)
            elif isinstance(target.value, ast.Name):
                self.unresolved.append(
                    (
                        ".".join(scope) or "<module>",
                        target.value.id,
                        getattr(node, "lineno", 0),
                    )
                )
        elif (
            self.dict_owner
            and isinstance(target, ast.Subscript)
            and isinstance(target.slice, ast.Constant)
            and target.slice.value == "state"
        ):
            self._add(scope, _dict_owner_model(self.path), DICT_ITEM, node)

    def _call(self, node: ast.Call, scope: list[str], env: dict[str, str]) -> None:
        func = node.func
        if isinstance(func, ast.Name) and func.id in self.aliases:
            if any(keyword.arg == "state" for keyword in node.keywords):
                self._add(scope, self.aliases[func.id], CONSTRUCTOR, node)
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "values"
            and any(keyword.arg == "state" for keyword in node.keywords)
        ):
            model = _values_chain_model(node, self.aliases)
            if model is not None:
                self._add(scope, model, BULK_UPDATE, node)
        elif (
            isinstance(func, ast.Name)
            and func.id == "setattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "state"
        ):
            model = self.resolver.model_of(node.args[0], env)
            if model is not None:
                self._add(scope, model, ATTRIBUTE, node)
        tail = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id
            if isinstance(func, ast.Name)
            else None
        )
        if tail in _STATE_HELPERS:
            self._add(scope, _STATE_HELPER_MODEL, HELPER_CALL, node)


def _nested_functions(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    stack: list[ast.AST] = list(function.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node
            continue
        stack.extend(ast.iter_child_nodes(node))


def _dict_owner_model(path: str) -> str:
    name = Path(path).stem
    return {
        "fleet_profiles": "FleetProfileApplication",
        "model_cache": "ModelCacheOperation",
        "artifact_jobs": "ArtifactJob",
        "agent_jobs": "AgentOperation",
    }.get(name, "Job")


def scan_source(source: str, *, path: str) -> list[Write]:
    """Return every lifecycle state write in one module's source."""

    if path.startswith(CORE_PREFIX) or path == MODELS_MODULE:
        return []
    collector = _Collector(path, ast.parse(source))
    return sorted(collector.writes, key=lambda write: (write.line, write.function))


def scan_unresolved(source: str, *, path: str) -> list[tuple[str, str, int]]:
    """``.state`` writes on a variable the scan could not attach to a model.

    Each entry is ``(function, variable, line)``."""

    if path.startswith(CORE_PREFIX) or path == MODELS_MODULE:
        return []
    return _Collector(path, ast.parse(source)).unresolved


def scan_lifecycle_writes(root: Path = CONTROL_SOURCE_ROOT) -> list[Write]:
    writes: list[Write] = []
    for module in sorted(root.rglob("*.py")):
        relative = module.relative_to(REPO_ROOT).as_posix()
        writes.extend(scan_source(module.read_text(encoding="utf-8"), path=relative))
    return writes


def count_sites(writes: Sequence[Write]) -> Counter[tuple[str, str, str, str]]:
    return Counter(write.identity for write in writes)


def _key(entry: dict[str, object]) -> tuple[str, str, str, str]:
    return (
        str(entry["path"]),
        str(entry["function"]),
        str(entry["model"]),
        str(entry["kind"]),
    )


def load_allowlist(path: Path = ALLOWLIST_PATH) -> tuple[int, list[dict[str, object]]]:
    """Read the reviewed allowlist. A malformed entry is a hard failure."""

    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema") != 1:
        raise ValueError(f"{path}: allowlist must be a schema-1 object")
    ceiling = document.get("max_writes")
    if not isinstance(ceiling, int) or ceiling < 0:
        raise ValueError(f"{path}: max_writes must be a non-negative integer")
    entries = document.get("writers")
    if not isinstance(entries, list):
        raise TypeError(f"{path}: allowlist needs a writers array")
    for index, entry in enumerate(entries):
        where = f"{path} writers[{index}]"
        if not isinstance(entry, dict):
            raise TypeError(f"{where}: entry is not an object")
        for field in ("path", "function", "model", "kind", "migrating_in"):
            if not isinstance(entry.get(field), str):
                raise TypeError(f"{where}: {field} must be a string")
        if entry["model"] not in LIFECYCLE_MODELS:
            raise ValueError(f"{where}: unknown lifecycle model {entry['model']!r}")
        if entry["kind"] not in KINDS:
            raise ValueError(f"{where}: unknown write kind {entry['kind']!r}")
        if entry["migrating_in"] not in MIGRATION_STEPS:
            raise ValueError(
                f"{where}: migrating_in must be one of {sorted(MIGRATION_STEPS)}"
            )
        count = entry.get("count")
        if not isinstance(count, int) or count < 1:
            raise ValueError(f"{where}: count must be a positive integer")
    return ceiling, entries


def evaluate_writer_gate(
    writes: Sequence[Write],
    allowlist: Sequence[dict[str, object]],
    ceiling: int,
) -> list[str]:
    """Return one message per violation. An empty list is a pass."""

    messages: list[str] = []
    current = count_sites(writes)
    first_line = {write.identity: write for write in reversed(writes)}
    listed = {_key(entry): int(entry["count"]) for entry in allowlist}  # type: ignore[call-overload]
    for key, count in sorted(current.items()):
        if key not in listed:
            messages.append(
                "lifecycle state written outside vonk_control.lifecycle; move the "
                f"transition into the core: {first_line[key].render()}"
            )
        elif count > listed[key]:
            messages.append(
                f"lifecycle writers rose from {listed[key]} to {count}; the "
                f"ceiling only goes down: {first_line[key].render()}"
            )
        elif count < listed[key]:
            messages.append(
                f"lifecycle writers fell from {listed[key]} to {count}; lower the "
                f"recorded count: {key}"
            )
    for key in sorted(listed):
        if key not in current:
            messages.append(f"allowlist entry no longer occurs; delete it: {key}")
    total = sum(current.values())
    if total > ceiling:
        messages.append(f"lifecycle writers total {total}, above the ceiling {ceiling}")
    elif total < ceiling:
        messages.append(
            f"lifecycle writers total {total}; lower max_writes from {ceiling}"
        )
    return messages


def migration_step(write: Write) -> str:
    """The migration step (blocker audit, 5.6) that removes ``write``."""

    if write.model in {"AgentOperation", "AgentOperationAttempt"}:
        return "step-2"
    if write.model == "Job" and write.path.endswith("/agent_jobs.py"):
        return "step-2"
    if write.model == "ArtifactJob":
        return "step-3"
    if write.model == "ModelCacheOperation":
        return "step-4"
    if write.model == "Job" and write.path.endswith("/run_switch_operations.py"):
        return "step-5"
    if write.model == "FleetProfileApplication":
        return "step-6"
    return "step-7"


def baseline_document(writes: Sequence[Write]) -> dict[str, object]:
    """The allowlist that describes exactly the current writers."""

    counts = count_sites(writes)
    steps = {write.identity: migration_step(write) for write in writes}
    return {
        "schema": 1,
        "max_writes": sum(counts.values()),
        "writers": [
            {
                "path": path,
                "function": function,
                "model": model,
                "kind": kind,
                "count": count,
                "migrating_in": steps[(path, function, model, kind)],
            }
            for (path, function, model, kind), count in sorted(counts.items())
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    writes = scan_lifecycle_writes()
    if arguments and arguments[0] == "--write-baseline":
        document = baseline_document(writes)
        ALLOWLIST_PATH.write_text(
            json.dumps(document, indent=2) + "\n", encoding="utf-8"
        )
        print(f"wrote {len(document['writers'])} entries")  # type: ignore[arg-type]
        return 0
    if arguments and arguments[0] == "--list":
        for write in writes:
            print(write.render())
        return 0
    ceiling, allowlist = load_allowlist()
    messages = evaluate_writer_gate(writes, allowlist, ceiling)
    if messages:
        for message in messages:
            print(message, file=sys.stderr)
        return 1
    print(f"lifecycle writers hold at {len(writes)} reviewed writes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
