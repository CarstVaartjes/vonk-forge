"""A call graph of ``control/src`` for the retry proof, built from the AST alone.

``blocker_retries`` asks one question of this graph: when an exception is raised
in function F, does every way to reach F end in a registered retry loop that
catches it?  Credit needs *every* caller, so the graph errs towards extra
callers: an edge it infers from a method name alone (``kind == "fallback"``, see
below) adds a caller that must be looped too but is never counted as evidence by
the proof, and a function whose caller it cannot see is an *entry*.

What it cannot promise is that no call goes unseen.  The known ways a path can
hide: a method of ``LIBRARY_METHOD_NAMES`` called on a receiver nothing types
(counted in ``CallGraph.generic_calls``; typing the receiver closes it), a
property read or an implicit ``__enter__`` (both are entries, not edges), and
iteration of a generator whose body runs outside the ``try`` that created it.

Edges
-----
* ``f()``: a nested function, a function of the module, a name the module imports
  (``from .x import f``, ``from . import x`` then ``x.f()``) or a class (its
  ``__init__`` and ``__post_init__``).
* ``self.m()``, ``self._x.m()``, ``param.m()``, ``local.m()``: the receiver is
  typed from annotations (class-body fields, ``self._x: T``, ``self._x = param``
  with ``param: T``, ``self._x = T(...)``, annotated parameters, locals assigned
  from a typed expression, return annotations).  The call goes to the method of
  that class, of its subclasses (overrides) and, for a ``Protocol``, of every
  class that implements all of its methods.
* A receiver the typing cannot name falls back to *every* method of that name in
  ``control/src`` (never to a stdlib or other external type): a fallback edge is
  one more caller that has to be looped.  The one exception is a name in
  ``LIBRARY_METHOD_NAMES`` (``get``, ``execute``, ...): on a receiver nothing
  types it is taken for a library call, and ``generic_calls`` counts them.
* Callbacks: a function reference passed as an argument is bound to the parameter
  it fills; a call of that parameter (or of a ``self._cb`` it was stored in) is an
  edge to every function bound to it.  A reference that reaches anything else
  (an unresolved callee, a container, a return, a thread, a pool ``submit``)
  *escapes*: the function then has a caller the analysis cannot see and counts as
  a public entry.  A closure a factory returns is reached through the alias its
  caller binds (``prepare = make(...)``), not through the ``return``.
* ``pool.submit(task, ...)``: the task is not an escape; it is an entry until a
  ``call_edges`` entry names the function that calls ``future.result()`` (a
  zero-argument ``.result()`` is a dynamic call) as its caller.
* ``getattr(recv, "name", ...)`` with a literal name (or a conditional of literals),
  then called, is a call of that method.  Any other ``getattr`` that is called is a
  *dynamic* call; a ``call_edges`` entry of the allowlist declares where it goes,
  with a reason, and the edge keeps the ``try`` context of the dynamic call.  A
  call of a local name bound by the function itself (a callable taken from a
  list) is a dynamic call too, and a declared edge gives a function whose
  reference escaped into such a list its caller, so it stops being an entry.

Entries
-------
A function nobody calls, a decorated one (routes and other registrations), an
implicit dunder, a property, and a function whose reference escapes all have a
caller the graph cannot see: they are entries.  ``Entry.kind`` says which.
"""

from __future__ import annotations

import ast
import builtins
from collections import defaultdict, deque
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field

_BUILTIN_SAFE_CALLEES = frozenset(
    {
        "callable",
        "isinstance",
        "issubclass",
        "bool",
        "id",
        "repr",
        "str",
        "type",
        "print",
        "len",
        "hasattr",
        "getattr",
        "setattr",
        "super",
        "iter",
        "next",
        "sorted",
        "list",
        "tuple",
        "set",
        "frozenset",
        "dict",
        "any",
        "all",
        "min",
        "max",
        "sum",
        "enumerate",
        "zip",
        "map",
        "filter",
    }
)
#: Decorators that leave a function an ordinary callable (anything else
#: registers it with a framework, which then calls it).
_TRANSPARENT_DECORATORS = frozenset(
    {
        "staticmethod",
        "classmethod",
        "abstractmethod",
        "override",
        "cache",
        "lru_cache",
        "cached_property",
        "contextmanager",
        "asynccontextmanager",
        "overload",
        "final",
        "wraps",
        "dataclass",
    }
)
#: Dunders the language calls for a constructor; the others have implicit callers.
_CONSTRUCTOR_DUNDERS = ("__init__", "__post_init__", "__new__")
_OPTIONAL_FORMS = frozenset({"Optional", "Union", "Annotated"})
_UNTYPED = frozenset({"Any", "object", "None", "NoReturn", "Never"})
TryNode = ast.Try | ast.TryStar
Tries = tuple[TryNode, ...]
CALLABLE = "<callable>"
BUILTIN = "<builtin>"
EXTERNAL = "<external>"
#: Method names of the standard library, SQLAlchemy and file objects.  Called on a
#: receiver nothing types they are taken for a library call, not for a call of a
#: ``control/src`` method that happens to share the name (``dict.get``,
#: ``Session.execute``); ``generic_calls`` counts them.  Typing the receiver is
#: what brings such a call back into the graph.
LIBRARY_METHOD_NAMES = frozenset(
    {
        "append",
        "close",
        "commit",
        "decode",
        "encode",
        "execute",
        "extend",
        "flush",
        "get",
        "items",
        "join",
        "keys",
        "pop",
        "read",
        "rollback",
        "scalar",
        "scalars",
        "split",
        "start",
        "strip",
        "update",
        "values",
        "write",
    }
)
_BUILTIN_NAMES = frozenset(dir(builtins))


@dataclass(frozen=True)
class Function:
    path: str
    qualname: str
    node: ast.AST

    @property
    def simple(self) -> str:
        return self.qualname.rsplit(".", 1)[-1]


@dataclass(frozen=True)
class Edge:
    caller: Function
    callee: Function
    #: The ``try`` statements whose *body* holds the call, innermost first.
    tries: Tries
    kind: str


@dataclass(frozen=True)
class DeclaredEdge:
    path: str
    function: str
    calls: tuple[tuple[str, str], ...]
    reason: str


@dataclass(eq=False)
class ClassInfo:
    path: str
    qualname: str
    node: ast.ClassDef
    bases: list[str]
    methods: dict[str, Function] = field(default_factory=dict)
    attr_types: dict[str, set[str]] = field(default_factory=dict)
    fields: list[str] = field(default_factory=list)
    is_protocol: bool = False

    @property
    def name(self) -> str:
        return self.qualname.rsplit(".", 1)[-1]

    @property
    def key(self) -> tuple[str, str]:
        return (self.path, self.qualname)


@dataclass(eq=False)
class FunctionInfo:
    function: Function
    parent: Function | None
    owner: ClassInfo | None
    self_class: ClassInfo | None
    params: list[str]
    decorators: list[str]
    nested: dict[str, Function] = field(default_factory=dict)


@dataclass(eq=False)
class ModuleInfo:
    path: str
    dotted: str
    functions: dict[str, Function] = field(default_factory=dict)
    classes: dict[str, ClassInfo] = field(default_factory=dict)
    #: name -> (dotted module, member or None) for each import of a name.
    imports: dict[str, tuple[str, str | None]] = field(default_factory=dict)


@dataclass(eq=False)
class _Arg:
    index: int | None
    keyword: str | None
    refs: set[Function]
    forward: tuple[Function, str] | None
    classes: list[ClassInfo]


@dataclass(eq=False)
class _Site:
    caller: Function
    tries: Tries
    static: set[Function]
    ctors: list[ClassInfo]
    cb_param: tuple[Function, str] | None
    cb_attrs: list[tuple[tuple[str, str], str]]
    cb_attr_name: str | None
    args: list[_Arg]
    external: bool
    fallback: bool
    node: ast.Call | None = None


@dataclass(eq=False)
class _Facts:
    sites: list[_Site] = field(default_factory=list)
    raises: dict[str, list[Tries]] = field(default_factory=lambda: defaultdict(list))
    dynamic: list[Tries] = field(default_factory=list)
    escapes: set[Function] = field(default_factory=set)
    attr_stores: list[
        tuple[tuple[str, str], str, set[Function], tuple[Function, str] | None]
    ] = field(default_factory=list)


def _name(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


_WRAPPERS = frozenset(
    {
        "Iterator",
        "Iterable",
        "Generator",
        "AsyncIterator",
        "AsyncGenerator",
        "ContextManager",
        "AbstractContextManager",
        "AsyncContextManager",
        "AbstractAsyncContextManager",
    }
)
RETURNS = "<returns:"


def annotation_names(node: ast.AST | None, *, unwrap: bool = False) -> set[str]:
    """The class names an annotation can mean (``T | None`` is ``T``).

    ``Callable[..., R]`` is the marker ``<callable>`` plus ``<returns:R>``; with
    ``unwrap`` an ``Iterator[T]`` (the annotation of a context manager) is ``T``.
    """

    if node is None:
        return set()
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            try:
                return annotation_names(
                    ast.parse(node.value, mode="eval").body, unwrap=unwrap
                )
            except SyntaxError:
                return set()
        return set()
    if isinstance(node, ast.Name):
        return set() if node.id in _UNTYPED else {node.id}
    if isinstance(node, ast.Attribute):
        return set() if node.attr in _UNTYPED else {node.attr}
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return annotation_names(node.left, unwrap=unwrap) | annotation_names(
            node.right, unwrap=unwrap
        )
    if isinstance(node, ast.Subscript):
        base = _name(node.value)
        if base in _OPTIONAL_FORMS:
            inner = node.slice
            if isinstance(inner, ast.Tuple):
                items = inner.elts[:1] if base == "Annotated" else inner.elts
                return set().union(
                    *(annotation_names(item, unwrap=unwrap) for item in items)
                )
            return annotation_names(inner, unwrap=unwrap)
        if unwrap and base in _WRAPPERS:
            inner = node.slice
            first = inner.elts[0] if isinstance(inner, ast.Tuple) else inner
            return annotation_names(first, unwrap=True)
        if base == "Callable":
            found = {CALLABLE}
            if isinstance(node.slice, ast.Tuple) and len(node.slice.elts) == 2:
                found |= {
                    f"{RETURNS}{name}>" for name in annotation_names(node.slice.elts[1])
                }
            return found
        return {base} if base is not None else set()
    return set()


_BLOCK_FIELDS = ("body", "orelse", "finalbody", "handlers", "cases")


def iter_statements(node: ast.AST, *, into_defs: bool = False) -> Iterator[ast.stmt]:
    """Statements below ``node`` without visiting any expression.

    A nested function or class is yielded but, unless ``into_defs``, not entered.
    """

    for name in _BLOCK_FIELDS:
        for child in getattr(node, name, ()):
            if isinstance(child, ast.stmt):
                yield child
                if into_defs or not isinstance(
                    child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
                ):
                    yield from iter_statements(child, into_defs=into_defs)
            else:
                yield from iter_statements(child, into_defs=into_defs)


def _dotted(path: str) -> str:
    stem = path.removeprefix("control/src/").removesuffix(".py").replace("/", ".")
    return stem.removesuffix(".__init__")


def _base_name(node: ast.AST) -> str | None:
    """``Protocol[T]`` is ``Protocol``; ``mod.Base`` is ``Base``."""

    return _name(node.value if isinstance(node, ast.Subscript) else node)


def _decorator_name(node: ast.AST) -> str:
    target = node.func if isinstance(node, ast.Call) else node
    parts: list[str] = []
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if isinstance(target, ast.Name):
        parts.append(target.id)
    return ".".join(reversed(parts))


def handler_names(handler: ast.ExceptHandler) -> list[str] | None:
    """Exception names a handler lists; None for a bare ``except:``."""

    if handler.type is None:
        return None
    items = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return [name for item in items if (name := _name(item)) is not None]


def handler_reraises(handler: ast.ExceptHandler) -> bool:
    return any(
        isinstance(child, ast.Raise)
        for statement in handler.body
        for child in ast.walk(statement)
    )


class CallGraph:
    """Functions, classes, call edges and entries of a set of parsed modules."""

    def __init__(
        self,
        trees: Mapping[str, ast.Module],
        declared: Sequence[DeclaredEdge] = (),
    ) -> None:
        self.trees = trees
        self.modules: dict[str, ModuleInfo] = {}
        self.by_dotted: dict[str, ModuleInfo] = {}
        self.functions: list[Function] = []
        self.info: dict[Function, FunctionInfo] = {}
        self.by_key: dict[tuple[str, str], Function] = {}
        self.classes: dict[str, list[ClassInfo]] = defaultdict(list)
        self.class_by_key: dict[tuple[str, str], ClassInfo] = {}
        self.parents: dict[str, set[str]] = defaultdict(set)
        self.children: dict[str, list[ClassInfo]] = defaultdict(list)
        self.methods_named: dict[str, list[Function]] = defaultdict(list)
        self.function_names: set[str] = set()
        self.factory_returns: dict[str, list[Function]] = defaultdict(list)
        self.returned_nested: dict[Function, list[Function]] = defaultdict(list)
        self.facts: dict[Function, _Facts] = {}
        self.callers: dict[Function, list[Edge]] = defaultdict(list)
        self.callees: dict[Function, list[Edge]] = defaultdict(list)
        self.entries: dict[Function, str] = {}
        self.dynamic_calls: list[Function] = []
        #: Functions registered as routes, whatever else their reference does.
        self.route_functions: set[Function] = set()
        self.generic_calls = 0
        self.declared = tuple(declared)
        self._bind: dict[tuple[Function, str], set[Function]] = defaultdict(set)
        self._attr_bind: dict[tuple[tuple[str, str], str], set[Function]] = defaultdict(
            set
        )
        self._mro_cache: dict[tuple[str, str], list[ClassInfo]] = {}
        self._dispatch_cache: dict[
            tuple[tuple[str, str], str], frozenset[Function]
        ] = {}
        self._env_cache: dict[Function, dict[str, set[str]]] = {}
        self._edge_keys: set[tuple[Function, Function, tuple[int, ...]]] = set()
        self._ancestors: dict[str, frozenset[str]] = {}
        self._collect()
        self._infer_attribute_types()
        self._env_cache.clear()
        self._walk_all()
        self._solve_callbacks()
        self._connect()

    # ------------------------------------------------------------ collection

    def _collect(self) -> None:
        for path, tree in self.trees.items():
            module = ModuleInfo(path, _dotted(path))
            self.modules[path] = module
            self.by_dotted[module.dotted] = module
        for path, tree in self.trees.items():
            module = self.modules[path]
            self._collect_imports(module, tree)
            module_function = Function(path, "<module>", tree)
            self.functions.append(module_function)
            self.by_key[(path, "<module>")] = module_function
            self.info[module_function] = FunctionInfo(
                module_function, None, None, None, [], []
            )
            self._collect_scope(module, tree, (), None, None, top=True)
        for cls_list in self.classes.values():
            for cls in cls_list:
                for base in cls.bases:
                    self.parents[cls.name].add(base)
                    self.children[base].append(cls)
                    # Imported mixin aliases name the same parent. Descendant
                    # dispatch must see the assembled class when a sibling
                    # mixin calls a method supplied by that parent.
                    module = self.modules[cls.path]
                    for parent in self._classes_named(module, base):
                        if parent.name != base:
                            self.children[parent.name].append(cls)
                if "Protocol" in cls.bases:
                    cls.is_protocol = True
        self._collect_factories()

    def _collect_factories(self) -> None:
        """Nested functions a function returns under an annotated class name."""

        for function in self.functions:
            node = function.node
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            names = annotation_names(node.returns)
            nested = self.info[function].nested
            if not nested:
                continue
            for statement in iter_statements(node):
                if (
                    isinstance(statement, ast.Return)
                    and isinstance(statement.value, ast.Name)
                    and statement.value.id in nested
                ):
                    made = nested[statement.value.id]
                    self.returned_nested[function].append(made)
                    for name in names:
                        self.factory_returns[name].append(made)

    def _collect_imports(self, module: ModuleInfo, tree: ast.Module) -> None:
        package = (
            module.dotted
            if module.path.endswith("__init__.py")
            else module.dotted.rpartition(".")[0]
        )
        for node in iter_statements(tree, into_defs=True):
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    parts = package.split(".") if package else []
                    base = ".".join(parts[: len(parts) - (node.level - 1)])
                    target = f"{base}.{node.module}" if node.module else base
                else:
                    target = node.module or ""
                for alias in node.names:
                    bound = alias.asname or alias.name
                    submodule = f"{target}.{alias.name}"
                    if submodule in self.by_dotted:
                        module.imports[bound] = (submodule, None)
                    else:
                        module.imports[bound] = (target, alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        module.imports[alias.asname] = (alias.name, None)
                    else:
                        module.imports[alias.name.split(".")[0]] = (
                            alias.name.split(".")[0],
                            None,
                        )

    def _collect_scope(
        self,
        module: ModuleInfo,
        node: ast.AST,
        scope: tuple[str, ...],
        parent: Function | None,
        owner: ClassInfo | None,
        top: bool = False,
    ) -> None:
        for child in iter_statements(node):
            if isinstance(child, ast.ClassDef):
                qual = ".".join((*scope, child.name))
                cls = ClassInfo(
                    module.path,
                    qual,
                    child,
                    [name for base in child.bases if (name := _base_name(base))],
                )
                for statement in child.body:
                    if isinstance(statement, ast.AnnAssign) and isinstance(
                        statement.target, ast.Name
                    ):
                        cls.fields.append(statement.target.id)
                        cls.attr_types.setdefault(statement.target.id, set()).update(
                            annotation_names(statement.annotation)
                        )
                self.classes[child.name].append(cls)
                self.class_by_key[cls.key] = cls
                if top and not scope:
                    module.classes[child.name] = cls
                self._collect_scope(module, child, (*scope, child.name), parent, cls)
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                qual = ".".join((*scope, child.name))
                function = Function(module.path, qual, child)
                self.functions.append(function)
                self.by_key[(module.path, qual)] = function
                args = child.args
                params = [
                    a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)
                ]
                self_class = owner
                if self_class is None and parent is not None:
                    self_class = self.info[parent].self_class
                info = FunctionInfo(
                    function,
                    parent,
                    owner,
                    self_class,
                    params,
                    [_decorator_name(d) for d in child.decorator_list],
                )
                self.info[function] = info
                self.function_names.add(child.name)
                if owner is not None:
                    owner.methods.setdefault(child.name, function)
                    self.methods_named[child.name].append(function)
                elif parent is not None:
                    self.info[parent].nested[child.name] = function
                elif top and not scope:
                    module.functions[child.name] = function
                self._collect_scope(module, child, (*scope, child.name), function, None)

    # --------------------------------------------------------------- classes

    def _classes_named(self, module: ModuleInfo, name: str) -> list[ClassInfo]:
        if name in module.classes:
            return [module.classes[name]]
        imported = module.imports.get(name)
        if imported is not None and imported[1] is not None:
            target = self.by_dotted.get(imported[0])
            if target is not None and imported[1] in target.classes:
                return [target.classes[imported[1]]]
        return self.classes.get(name, [])

    def mro(self, cls: ClassInfo) -> list[ClassInfo]:
        cached = self._mro_cache.get(cls.key)
        if cached is not None:
            return cached
        self._mro_cache[cls.key] = [cls]
        order = [cls]
        module = self.modules[cls.path]
        for base in cls.bases:
            for parent in self._classes_named(module, base):
                for item in self.mro(parent):
                    if item not in order:
                        order.append(item)
        self._mro_cache[cls.key] = order
        return order

    def descendants(self, cls: ClassInfo) -> list[ClassInfo]:
        found: list[ClassInfo] = []
        queue = deque([cls])
        seen = {cls.key}
        while queue:
            current = queue.popleft()
            for child in self.children.get(current.name, ()):
                if child.key not in seen and current in self.mro(child):
                    seen.add(child.key)
                    found.append(child)
                    queue.append(child)
        return found

    def find_method(self, cls: ClassInfo, name: str) -> Function | None:
        for item in self.mro(cls):
            if name in item.methods:
                return item.methods[name]
        return None

    def implementations(self, protocol: ClassInfo) -> list[ClassInfo]:
        """Classes that define every method the protocol declares."""

        wanted = [
            name
            for item in self.mro(protocol)
            if item.is_protocol
            for name in item.methods
            if not (name.startswith("__") and name.endswith("__"))
        ]
        if not wanted:
            return []
        found: list[ClassInfo] = []
        for classes in self.classes.values():
            for cls in classes:
                if cls is protocol or cls.is_protocol:
                    continue
                if all(
                    (candidate := self.find_method(cls, name)) is not None
                    and self._possible_protocol_return(
                        self.find_method(protocol, name), candidate
                    )
                    for name in wanted
                ):
                    found.append(cls)
        return found

    def _possible_protocol_return(
        self, expected: Function | None, candidate: Function
    ) -> bool:
        """Reject only a proven incompatible local nominal return annotation.

        Missing, generic, external and structural annotations remain uncertain
        and retain their caller edge. This is not a general type checker.
        """
        if expected is None:
            return True
        annotations = []
        for function in (expected, candidate):
            node = function.node
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                return True
            if not isinstance(node.returns, ast.Name):
                return True
            module = self._module_of(function)
            if (
                node.returns.id not in module.classes
                and node.returns.id not in module.imports
            ):
                return True
            classes = self._classes_named(module, node.returns.id)
            if len(classes) != 1 or classes[0].is_protocol:
                return True
            annotations.append(classes[0])
        required, actual = annotations
        return required in self.mro(actual)

    def dispatch(self, cls: ClassInfo, method: str) -> frozenset[Function]:
        """Every function a call of ``method`` on a ``cls`` can run."""

        key = (cls.key, method)
        cached = self._dispatch_cache.get(key)
        if cached is not None:
            return cached
        found: set[Function] = set()
        own = self.find_method(cls, method)
        if own is not None:
            found.add(own)
        for sub in self.descendants(cls):
            inherited = self.find_method(sub, method)
            if inherited is not None:
                found.add(inherited)
        if cls.is_protocol:
            for impl in self.implementations(cls):
                target = self.find_method(impl, method)
                if target is not None:
                    found.add(target)
                for sub in self.descendants(impl):
                    if method in sub.methods:
                        found.add(sub.methods[method])
        if method == "__call__":
            for item in (cls, *self.descendants(cls)):
                found.update(self.factory_returns.get(item.name, ()))
        result = frozenset(found)
        self._dispatch_cache[key] = result
        return result

    # ----------------------------------------------------------------- types

    def _module_of(self, function: Function) -> ModuleInfo:
        return self.modules[function.path]

    def _internal(self, names: Iterable[str], module: ModuleInfo) -> list[ClassInfo]:
        found: list[ClassInfo] = []
        for name in names:
            found.extend(self._classes_named(module, name))
        return found

    def attribute_type(self, cls: ClassInfo, attr: str) -> set[str]:
        found: set[str] = set()
        for item in self.mro(cls):
            found |= item.attr_types.get(attr, set())
        return found

    def env(self, function: Function) -> dict[str, set[str]]:
        cached = self._env_cache.get(function)
        if cached is not None:
            return cached
        info = self.info[function]
        env: dict[str, set[str]] = {}
        self._env_cache[function] = env
        node = function.node
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Module):
            args = node.args if not isinstance(node, ast.Module) else None
            if args is not None:
                for argument in (*args.posonlyargs, *args.args, *args.kwonlyargs):
                    names = annotation_names(argument.annotation)
                    if names:
                        env[argument.arg] = names
            if info.parent is not None:
                for name, names in self.env(info.parent).items():
                    env.setdefault(name, names)
            statements = [
                child
                for child in iter_statements(node)
                if isinstance(
                    child, ast.Assign | ast.AnnAssign | ast.With | ast.AsyncWith
                )
            ]
            statements.sort(key=lambda child: (child.lineno, child.col_offset))
            for statement in statements:
                if isinstance(statement, ast.With | ast.AsyncWith):
                    for item in statement.items:
                        if isinstance(item.optional_vars, ast.Name):
                            names = self._context_type(item.context_expr, function, env)
                            if names:
                                env.setdefault(item.optional_vars.id, set()).update(
                                    names
                                )
                    continue
                if isinstance(statement, ast.AnnAssign):
                    targets = [statement.target]
                    names = annotation_names(statement.annotation)
                    if not names and statement.value is not None:
                        names = self.type_of(statement.value, function, env)
                else:
                    targets = list(statement.targets)
                    names = self.type_of(statement.value, function, env)
                if not names:
                    continue
                for target in targets:
                    if isinstance(target, ast.Name):
                        env.setdefault(target.id, set()).update(names)
        return env

    def type_of(
        self, node: ast.AST, function: Function, env: Mapping[str, set[str]]
    ) -> set[str]:
        module = self._module_of(function)
        if isinstance(node, ast.Name):
            return self._name_type(node.id, function, env)
        if isinstance(node, ast.Attribute):
            found: set[str] = set()
            receiver = self.type_of(node.value, function, env)
            for cls in self._internal(receiver, module):
                found |= self.attribute_type(cls, node.attr)
                method = self.find_method(cls, node.attr)
                if method is not None and "property" in self.info[method].decorators:
                    found |= self._returns(method)
            if not found and receiver and not self._internal(receiver, module):
                return {EXTERNAL}
            return found
        if isinstance(node, ast.Call):
            return self._call_type(node, function, env)
        if isinstance(node, ast.IfExp):
            return self.type_of(node.body, function, env) | self.type_of(
                node.orelse, function, env
            )
        if isinstance(node, ast.BoolOp):
            return set().union(*(self.type_of(v, function, env) for v in node.values))
        if isinstance(node, ast.Await | ast.NamedExpr):
            return self.type_of(node.value, function, env)
        if isinstance(node, ast.Constant) and node.value is not None:
            return {BUILTIN}
        if isinstance(
            node,
            ast.List
            | ast.Dict
            | ast.Set
            | ast.Tuple
            | ast.JoinedStr
            | ast.ListComp
            | ast.DictComp
            | ast.SetComp
            | ast.GeneratorExp
            | ast.BinOp
            | ast.Compare,
        ):
            return {BUILTIN}
        return set()

    def _context_type(
        self, node: ast.AST, function: Function, env: Mapping[str, set[str]]
    ) -> set[str]:
        """What ``with node as x`` binds ``x`` to."""

        if isinstance(node, ast.Call):
            found: set[str] = set()
            callee = node.func
            targets: set[Function] = set()
            module = self._module_of(function)
            if isinstance(callee, ast.Name):
                targets = self._name_targets(function, callee.id)
            elif isinstance(callee, ast.Attribute):
                for cls in self._internal(
                    self.type_of(callee.value, function, env), module
                ):
                    targets |= self.dispatch(cls, callee.attr)
            for target in targets:
                if isinstance(target.node, ast.FunctionDef | ast.AsyncFunctionDef):
                    found |= annotation_names(target.node.returns, unwrap=True)
            if found:
                return found
        names = self.type_of(node, function, env)
        return {EXTERNAL} if EXTERNAL in names else set()

    def _name_type(
        self, name: str, function: Function, env: Mapping[str, set[str]]
    ) -> set[str]:
        info = self.info[function]
        module = self._module_of(function)
        if name in ("self", "cls") and info.self_class is not None:
            # An explicit receiver cast can name the assembled mixin class.
            # Keep that interface for cross-mixin methods and attributes.
            return set(env.get(name) or {info.self_class.name})
        if name in env:
            return set(env[name])
        if self._is_local(function, name):
            return set()
        imported = module.imports.get(name)
        if imported is not None:
            target = self.by_dotted.get(imported[0])
            if target is None:
                return {EXTERNAL}
            if imported[1] is not None and imported[1] in target.classes:
                return set()
            if imported[1] is not None:
                return set(
                    self.env(self.by_key[(target.path, "<module>")]).get(
                        imported[1], ()
                    )
                )
            return set()
        if name in _BUILTIN_NAMES:
            return {EXTERNAL}
        module_function = self.by_key[(module.path, "<module>")]
        if function is not module_function:
            return set(self.env(module_function).get(name, ()))
        return set()

    def _call_type(
        self, node: ast.Call, function: Function, env: Mapping[str, set[str]]
    ) -> set[str]:
        module = self._module_of(function)
        callee = node.func
        if isinstance(callee, ast.Name):
            if (
                callee.id == "cast"
                or module.imports.get(callee.id) == ("typing", "cast")
            ) and node.args:
                return annotation_names(node.args[0])
            if callee.id not in env and not self._is_local(function, callee.id):
                if self._classes_named(module, callee.id):
                    return {callee.id}
            else:
                bound = self._name_type(callee.id, function, env)
                returned = {
                    name[len(RETURNS) : -1]
                    for name in bound
                    if name.startswith(RETURNS)
                }
                if returned:
                    return returned
                if bound and not self._internal(bound, module):
                    # A parameter typed by a library class (``sessionmaker[...]``)
                    # called: its result is a library object too.
                    return {EXTERNAL}
            found: set[str] = set()
            targets = self._name_targets(function, callee.id)
            for target in targets:
                found |= self._returns(target)
            if targets:
                return found
            imported = module.imports.get(callee.id)
            if (
                imported is not None and imported[0] not in self.by_dotted
            ) or callee.id in _BUILTIN_NAMES:
                return {EXTERNAL}
            return set()
        if isinstance(callee, ast.Attribute):
            receiver = callee.value
            if isinstance(receiver, ast.Name):
                imported = module.imports.get(receiver.id)
                if imported is not None and imported[1] is None:
                    target = self.by_dotted.get(imported[0])
                    if target is None:
                        return {EXTERNAL}
                    if callee.attr in target.classes:
                        return {callee.attr}
                    found = set()
                    if callee.attr in target.functions:
                        found = self._returns(target.functions[callee.attr])
                    return found
            types = self.type_of(receiver, function, env)
            classes = self._internal(types, module)
            found = set()
            for cls in classes:
                targets = self.dispatch(cls, callee.attr)
                for target in targets:
                    found |= self._returns(target)
                if not targets:
                    # Not a method: an attribute that is called.  ``Callable[..., R]``
                    # gives R; a library type (``sessionmaker``) gives a library object.
                    held = self.attribute_type(cls, callee.attr)
                    found |= {
                        name[len(RETURNS) : -1]
                        for name in held
                        if name.startswith(RETURNS)
                    }
                    if held and not found and not self._internal(held, module):
                        found.add(EXTERNAL)
            if not found and types and not classes:
                return {EXTERNAL}
            return found
        return set()

    def _returns(self, function: Function) -> set[str]:
        node = function.node
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names = annotation_names(node.returns)
            owner = self.info[function].owner
            if "Self" in names and owner is not None:
                names = (names - {"Self"}) | {owner.name}
            return names
        return set()

    def _is_local(self, function: Function, name: str) -> bool:
        current: Function | None = function
        while current is not None:
            info = self.info[current]
            if name in info.nested or name in info.params:
                return True
            current = info.parent
        return False

    def _infer_attribute_types(self) -> None:
        stores: list[
            tuple[ClassInfo, str, Function, ast.AST | None, ast.AST | None]
        ] = []
        for cls_list in self.classes.values():
            for cls in cls_list:
                for method in cls.methods.values():
                    for node in iter_statements(method.node):
                        if isinstance(node, ast.AnnAssign):
                            target, value, annotation = (
                                node.target,
                                node.value,
                                node.annotation,
                            )
                        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
                            target, value, annotation = (
                                node.targets[0],
                                node.value,
                                None,
                            )
                        else:
                            continue
                        if (
                            isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "self"
                        ):
                            stores.append((cls, target.attr, method, value, annotation))
        for _ in range(4):
            changed = False
            self._env_cache.clear()
            for cls, attr, method, value, annotation in stores:
                names = annotation_names(annotation)
                if not names and value is not None:
                    names = self.type_of(value, method, self.env(method))
                if names - cls.attr_types.get(attr, set()):
                    cls.attr_types.setdefault(attr, set()).update(names)
                    changed = True
            if not changed:
                break

    # ------------------------------------------------------------ resolution

    def _name_targets(self, function: Function, name: str) -> set[Function]:
        """Functions a bare name can mean in ``function`` (not a parameter)."""

        current: Function | None = function
        while current is not None:
            info = self.info[current]
            if name in info.nested:
                return {info.nested[name]}
            current = info.parent
        module = self._module_of(function)
        if name in module.functions:
            return {module.functions[name]}
        imported = module.imports.get(name)
        if imported is not None and imported[1] is not None:
            target = self.by_dotted.get(imported[0])
            if target is not None and imported[1] in target.functions:
                return {target.functions[imported[1]]}
        return set()

    def _constructors(self, cls: ClassInfo) -> set[Function]:
        found: set[Function] = set()
        for name in _CONSTRUCTOR_DUNDERS:
            method = self.find_method(cls, name)
            if method is not None:
                found.add(method)
        return found

    def _constructor_fields(self, cls: ClassInfo) -> list[str]:
        order: list[str] = []
        for item in reversed(self.mro(cls)):
            for name in item.fields:
                if name not in order:
                    order.append(name)
        return order

    def _function_refs(
        self,
        node: ast.AST,
        function: Function,
        env: Mapping[str, set[str]],
        aliases: Mapping[str, set[Function]],
    ) -> tuple[set[Function], tuple[Function, str] | None, list[ClassInfo]]:
        """Functions an expression names (not calls): a reference to bind."""

        module = self._module_of(function)
        if isinstance(node, ast.Name):
            current: Function | None = function
            while current is not None:
                info = self.info[current]
                if node.id in aliases and current is function:
                    return set(aliases[node.id]), None, []
                if node.id in info.nested:
                    return {info.nested[node.id]}, None, []
                if node.id in info.params:
                    return set(), (current, node.id), []
                current = info.parent
            targets = self._name_targets(function, node.id)
            if targets:
                return targets, None, []
            classes = self._classes_named(module, node.id)
            return set(), None, list(classes)
        if isinstance(node, ast.Attribute):
            found: set[Function] = set()
            receiver = node.value
            if isinstance(receiver, ast.Name):
                imported = module.imports.get(receiver.id)
                if imported is not None and imported[1] is None:
                    target_module = self.by_dotted.get(imported[0])
                    if target_module is not None:
                        if node.attr in target_module.functions:
                            return {target_module.functions[node.attr]}, None, []
                        if node.attr in target_module.classes:
                            return set(), None, [target_module.classes[node.attr]]
                    return set(), None, []
            names = self.type_of(receiver, function, env)
            if names:
                for cls in self._internal(names, module):
                    found |= self.dispatch(cls, node.attr)
            elif node.attr.startswith("_") and not node.attr.startswith("__"):
                found |= set(self.methods_named.get(node.attr, ()))
            return found, None, []
        return set(), None, []

    def _walk_all(self) -> None:
        for function in self.functions:
            self.facts[function] = _Walker(self, function).run()

    # ------------------------------------------------------------- callbacks

    def _params_of(self, target: Function) -> list[str]:
        info = self.info[target]
        params = list(info.params)
        if info.owner is not None and params and "staticmethod" not in info.decorators:
            params = params[1:]
        return params

    def _bind_args(self, site: _Site, target: Function) -> bool:
        params = self._params_of(target)
        changed = False
        for arg in site.args:
            refs = set(arg.refs)
            if arg.forward is not None:
                refs |= self._bind.get(arg.forward, set())
            if not refs:
                continue
            if arg.keyword is not None:
                name: str | None = arg.keyword
            elif arg.index is not None and arg.index < len(params):
                name = params[arg.index]
            else:
                name = None
            if name is None or name not in self.info[target].params:
                continue
            bound = self._bind[(target, name)]
            if not refs <= bound:
                bound |= refs
                changed = True
        return changed

    def _bind_fields(self, site: _Site, cls: ClassInfo) -> bool:
        fields = self._constructor_fields(cls)
        changed = False
        for arg in site.args:
            refs = set(arg.refs)
            if arg.forward is not None:
                refs |= self._bind.get(arg.forward, set())
            if not refs:
                continue
            if arg.keyword is not None:
                name: str | None = arg.keyword
            elif arg.index is not None and arg.index < len(fields):
                name = fields[arg.index]
            else:
                name = None
            if name is None or name not in fields:
                continue
            bound = self._attr_bind[(cls.key, name)]
            if not refs <= bound:
                bound |= refs
                changed = True
        return changed

    def _site_targets(self, site: _Site) -> set[Function]:
        found = set(site.static)
        if site.cb_param is not None:
            found |= self._bind.get(site.cb_param, set())
        for key, attr in site.cb_attrs:
            found |= self._attr_funcs(key, attr)
        if site.cb_attr_name is not None:
            for (_key, attr), refs in self._attr_bind.items():
                if attr == site.cb_attr_name:
                    found |= refs
        return found

    def _attr_funcs(self, key: tuple[str, str], attr: str) -> set[Function]:
        cls = self.class_by_key[key]
        found: set[Function] = set()
        for item in (*self.mro(cls), *self.descendants(cls)):
            found |= self._attr_bind.get((item.key, attr), set())
        return found

    def _solve_callbacks(self) -> None:
        active = [
            site
            for facts in self.facts.values()
            for site in facts.sites
            if site.args or site.cb_param or site.cb_attrs or site.cb_attr_name
        ]
        stores = [store for facts in self.facts.values() for store in facts.attr_stores]
        for _ in range(12):
            changed = False
            for site in active:
                for target in self._site_targets(site):
                    if any(arg.refs or arg.forward for arg in site.args):
                        changed |= self._bind_args(site, target)
                for cls in site.ctors:
                    if any(arg.refs or arg.forward for arg in site.args):
                        changed |= self._bind_fields(site, cls)
                    for constructor in self._constructors(cls):
                        if any(arg.refs or arg.forward for arg in site.args):
                            changed |= self._bind_args(site, constructor)
            for key, attr, refs, forward in stores:
                bound = self._attr_bind[(key, attr)]
                incoming = set(refs)
                if forward is not None:
                    incoming |= self._bind.get(forward, set())
                if not incoming <= bound:
                    bound |= incoming
                    changed = True
            if not changed:
                break

    # ----------------------------------------------------------------- edges

    def _add_edge(
        self, caller: Function, callee: Function, tries: Tries, kind: str
    ) -> None:
        key = (caller, callee, tuple(id(item) for item in tries))
        if key in self._edge_keys:
            return
        self._edge_keys.add(key)
        edge = Edge(caller, callee, tries, kind)
        self.callers[callee].append(edge)
        self.callees[caller].append(edge)

    def _connect(self) -> None:
        for function, facts in self.facts.items():
            for site in facts.sites:
                kind = "fallback" if site.fallback else "call"
                for target in self._site_targets(site):
                    self._add_edge(function, target, site.tries, kind)
                for cls in site.ctors:
                    for constructor in self._constructors(cls):
                        self._add_edge(function, constructor, site.tries, "constructor")
            for escaped in facts.escapes:
                self.entries.setdefault(escaped, "reference-escape")
            if facts.dynamic:
                self.dynamic_calls.append(function)
        self._declare_edges()
        for function in self.functions:
            info = self.info[function]
            kinds = [
                d
                for d in info.decorators
                if d.split(".")[-1] not in _TRANSPARENT_DECORATORS
            ]
            if any(_is_route_decorator(d) for d in kinds):
                self.route_functions.add(function)
            if "property" in info.decorators or any(
                d.endswith(".setter") for d in info.decorators
            ):
                self.entries.setdefault(function, "property")
            elif kinds:
                route = any(_is_route_decorator(d) for d in kinds)
                self.entries.setdefault(function, "route" if route else "decorated")
            elif (
                function.simple.startswith("__")
                and function.simple.endswith("__")
                and function.simple not in _CONSTRUCTOR_DUNDERS
            ):
                self.entries.setdefault(function, "implicit-dunder")
            if function.qualname == "<module>":
                self.entries.setdefault(function, "module-level")
            elif function not in self.callers:
                self.entries.setdefault(function, "no-caller")

    def _declare_edges(self) -> None:
        for declared in self.declared:
            caller = self.by_key.get((declared.path, declared.function))
            if caller is None:
                continue
            sites = self.facts[caller].dynamic or [()]
            for path, name in declared.calls:
                callee = self.by_key.get((path, name))
                if callee is None:
                    continue
                for tries in sites:
                    self._add_edge(caller, callee, tries, "declared")
                # The declaration names who really calls a function whose
                # reference was stored in a collection (an ``escape``).
                if self.entries.get(callee) == "reference-escape":
                    del self.entries[callee]

    def declared_problems(self) -> list[str]:
        problems: list[str] = []
        for declared in self.declared:
            key = (declared.path, declared.function)
            if key not in self.by_key:
                problems.append(f"declared call edge source does not exist: {key}")
            for call in declared.calls:
                if call not in self.by_key:
                    problems.append(f"declared call edge target does not exist: {call}")
            if len(declared.reason.split()) < 5:
                problems.append(f"declared call edge needs a written reason: {key}")
        return problems

    # ---------------------------------------------------------------- proof

    def ancestors(self, exception_class: str) -> frozenset[str]:
        known = self._ancestors.get(exception_class)
        if known is not None:
            return known
        seen = {exception_class}
        queue = deque([exception_class])
        while queue:
            for parent in self.parents.get(queue.popleft(), ()):
                if parent not in seen:
                    seen.add(parent)
                    queue.append(parent)
        self._ancestors[exception_class] = frozenset(seen)
        return self._ancestors[exception_class]

    def classify_try(
        self,
        tries: Sequence[TryNode],
        exception_class: str,
        loop_catches: frozenset[str] | None,
        too_broad: frozenset[str],
    ) -> str:
        """What stops an exception raised under ``tries``: ``loop`` (a registered
        loop catches it and goes on), ``swallowed`` (another handler ends it) or
        ``propagates`` (it leaves the function)."""

        every = self.ancestors(exception_class) | _ANCESTORS_ANY
        exact = frozenset(name for name in every if name not in too_broad)
        for statement in tries:
            for handler in statement.handlers:
                listed = handler_names(handler)
                if listed is not None and not any(name in every for name in listed):
                    continue
                if handler_reraises(handler):
                    break
                if (
                    loop_catches is not None
                    and listed is not None
                    and any(
                        name in exact
                        and (
                            name in loop_catches or loop_catches & self.ancestors(name)
                        )
                        for name in listed
                    )
                ):
                    # The registered catch, or a handler for a subclass of one
                    # (a specialisation of the same retry, listed before it).
                    return "loop"
                return "swallowed"
        return "propagates"


_ANCESTORS_ANY = frozenset({"Exception", "BaseException"})


def _is_route_decorator(decorator: str) -> bool:
    return decorator.split(".")[0] in {"router", "app", "api"} or decorator.endswith(
        (".get", ".post", ".put", ".delete", ".patch")
    )


_LEAVES = frozenset(
    {
        ast.Constant,
        ast.Pass,
        ast.Break,
        ast.Continue,
        ast.Import,
        ast.ImportFrom,
        ast.Global,
        ast.Nonlocal,
    }
    | {
        cls
        for base in (ast.expr_context, ast.operator, ast.boolop, ast.unaryop, ast.cmpop)
        for cls in base.__subclasses__()
    }
)
_DEFINITIONS = frozenset({ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef})
#: Calls that run a function on a thread and are awaited on the spot, so its
#: exception surfaces at the ``await``: name -> index of the task argument.
_AWAITED_TASKS = {"to_thread": 0, "run_in_executor": 1}


class _Walker:
    """One function body, in source order, with the ``try`` bodies around each call."""

    def __init__(self, graph: CallGraph, function: Function) -> None:
        self.graph = graph
        self.function = function
        self.info = graph.info[function]
        self.module = graph.modules[function.path]
        self.facts = _Facts()
        self.env = graph.env(function)
        self.aliases: dict[str, set[Function]] = {}
        self.callee_ids: set[int] = set()
        self.arg_of: dict[int, tuple[_Site, _Arg]] = {}
        self.assign_of: dict[int, ast.AST] = {}
        self.dynamic_names: set[str] = set()
        self.awaited: set[int] = set()
        self.inline: set[int] = set()

    def run(self) -> _Facts:
        node = self.function.node
        if isinstance(node, ast.Module):
            body: list[ast.stmt] = [
                s
                for s in node.body
                if not isinstance(
                    s, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
                )
            ]
        else:
            assert isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            body = list(node.body)
        for statement in body:
            self.visit(statement, ())
        return self.facts

    # -------------------------------------------------------------- visiting

    def visit(self, node: ast.AST, tries: Tries) -> None:
        kind = node.__class__
        if kind in _LEAVES or kind in _DEFINITIONS:
            return
        if isinstance(node, ast.Lambda):
            if id(node) in self.inline:
                return
            tries = ()
        elif isinstance(node, ast.Await):
            if isinstance(node.value, ast.Call):
                self.awaited.add(id(node.value))
        elif isinstance(node, ast.Try | ast.TryStar):
            inner: Tries = (node, *tries)
            for statement in node.body:
                self.visit(statement, inner)
            for handler in node.handlers:
                self.visit(handler, tries)
            for statement in (*node.orelse, *node.finalbody):
                self.visit(statement, tries)
            return
        elif isinstance(node, ast.Return):
            if isinstance(node.value, ast.Name) and node.value.id in self.info.nested:
                # A factory handing out its closure: the closure is reached through
                # what the caller does with the result (an alias, an argument).
                return
        elif isinstance(node, ast.Raise):
            self.raise_(node, tries)
        elif isinstance(
            node,
            ast.Compare | ast.BoolOp | ast.UnaryOp | ast.If | ast.IfExp | ast.While,
        ):
            self.tested(node)
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            self.assign(node)
        elif isinstance(node, ast.Call):
            self.call(node, tries)
        elif isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load) and (
                node.id in self.graph.function_names or node.id in self.aliases
            ):
                self.reference(node)
            return
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and node.attr in self.graph.function_names
        ):
            self.reference(node)
        for name in kind._fields:
            value = getattr(node, name, None)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.AST):
                        self.visit(item, tries)
            elif isinstance(value, ast.AST):
                self.visit(value, tries)

    def tested(self, node: ast.AST) -> None:
        """A reference only compared or tested (``x is None``, ``if x``) is no use."""

        operands: list[ast.AST] = []
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
        elif isinstance(node, ast.BoolOp):
            operands = list(node.values)
        elif isinstance(node, ast.UnaryOp):
            operands = [node.operand]
        elif isinstance(node, ast.If | ast.IfExp | ast.While):
            operands = [node.test]
        for operand in operands:
            if isinstance(operand, ast.Name | ast.Attribute):
                self.assign_of[id(operand)] = node

    def raise_(self, node: ast.Raise, tries: Tries) -> None:
        if isinstance(node.exc, ast.Call):
            name = _name(node.exc.func)
            if name is not None:
                self.facts.raises[name].append(tries)

    # ------------------------------------------------------------ references

    def refs(
        self, node: ast.AST
    ) -> tuple[set[Function], tuple[Function, str] | None, list[ClassInfo]]:
        return self.graph._function_refs(node, self.function, self.env, self.aliases)

    def reference(self, node: ast.Name | ast.Attribute) -> None:
        if id(node) in self.callee_ids:
            return
        if id(node) in self.arg_of or id(node) in self.assign_of:
            return
        refs, _forward, _classes = self.refs(node)
        self.facts.escapes |= refs

    def assign(self, node: ast.Assign | ast.AnnAssign) -> None:
        value = node.value
        if value is None:
            return
        targets = (
            [node.target] if isinstance(node, ast.AnnAssign) else list(node.targets)
        )
        if isinstance(value, ast.Name | ast.Attribute) and all(
            isinstance(target, ast.Name)
            or (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            )
            for target in targets
        ):
            self.assign_of[id(value)] = node
        for target in targets:
            if isinstance(target, ast.Attribute) and (
                isinstance(target.value, ast.Name) and target.value.id == "self"
            ):
                cls = self.info.self_class
                if cls is None:
                    continue
                refs, forward, classes = (
                    self.refs(value)
                    if isinstance(value, ast.Name | ast.Attribute)
                    else (set(), None, [])
                )
                if refs or forward or classes:
                    for item in classes:
                        refs = refs | self.graph._constructors(item)
                    self.facts.attr_stores.append((cls.key, target.attr, refs, forward))
            elif isinstance(target, ast.Name):
                found = self.alias_of(value)
                if found:
                    self.aliases.setdefault(target.id, set()).update(found)
                elif self.is_dynamic_getattr(value):
                    self.aliases.setdefault(target.id, set())
                    self.dynamic_names.add(target.id)

    def alias_of(self, value: ast.AST) -> set[Function]:
        if isinstance(value, ast.Name | ast.Attribute):
            refs, _forward, classes = self.refs(value)
            for cls in classes:
                refs = refs | self.graph._constructors(cls)
            return refs
        if isinstance(value, ast.IfExp):
            return self.alias_of(value.body) | self.alias_of(value.orelse)
        if isinstance(value, ast.BoolOp):
            return set().union(*(self.alias_of(v) for v in value.values))
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
            made: set[Function] = set()
            for factory in self.graph._name_targets(self.function, value.func.id):
                made.update(self.graph.returned_nested.get(factory, ()))
            if made:
                return made
        return self.getattr_targets(value)

    @staticmethod
    def literal_names(node: ast.AST) -> list[str] | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.IfExp):
            body = _Walker.literal_names(node.body)
            other = _Walker.literal_names(node.orelse)
            return None if body is None or other is None else body + other
        return None

    def is_dynamic_getattr(self, value: ast.AST) -> bool:
        return (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "getattr"
            and len(value.args) >= 2
            and self.literal_names(value.args[1]) is None
        )

    def getattr_targets(self, value: ast.AST) -> set[Function]:
        if not (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "getattr"
            and len(value.args) >= 2
        ):
            return set()
        names = self.literal_names(value.args[1])
        if names is None:
            return set()
        found: set[Function] = set()
        types = self.graph.type_of(value.args[0], self.function, self.env)
        classes = self.graph._internal(types, self.module)
        for name in names:
            if classes:
                for cls in classes:
                    found |= self.graph.dispatch(cls, name)
            elif not types:
                found |= set(self.graph.methods_named.get(name, ()))
        return found

    # ----------------------------------------------------------------- calls

    def call(self, node: ast.Call, tries: Tries) -> None:
        func = node.func
        self.callee_ids.add(id(func))
        site = _Site(
            self.function, tries, set(), [], None, [], None, [], False, False, node
        )
        self.resolve_callee(func, site, tries)
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "result"
            and not node.args
            and not node.keywords
        ):
            # ``future.result()`` re-raises what a pool task raised: where the
            # exception comes from is not visible here, so the call is a
            # dynamic one that ``call_edges`` may declare.
            self.facts.dynamic.append(tries)
        task = self.awaited_task(node, site, tries)
        position = 0
        for argument in node.args:
            if isinstance(argument, ast.Starred):
                position += 1
                continue
            if task < 0:
                self.argument(site, argument, position, None)
            elif position > task:
                self.argument(site, argument, position - task - 1, None)
            position += 1
        for keyword in node.keywords:
            self.argument(site, keyword.value, None, keyword.arg)
        self.facts.sites.append(site)
        safe = isinstance(func, ast.Name) and func.id in _BUILTIN_SAFE_CALLEES
        if (
            not safe
            and not site.static
            and not site.ctors
            and site.cb_param is None
            and not site.cb_attrs
        ):
            pooled = (
                isinstance(func, ast.Attribute)
                and func.attr == "submit"
                and site.external
            )
            for arg in site.args:
                if pooled and arg.index == 0 and arg.keyword is None:
                    # A pool task is not an escape: it runs when the pool says
                    # so, and the failure surfaces at the ``result()`` of its
                    # future.  The task is an entry (nobody calls it) until a
                    # ``call_edges`` entry declares the function that waits for
                    # the future, with the ``try`` context of its ``result()``.
                    continue
                self.facts.escapes |= arg.refs
                for cls in arg.classes:
                    self.facts.escapes |= self.graph._constructors(cls)

    def awaited_task(self, node: ast.Call, site: _Site, tries: Tries) -> int:
        """``await asyncio.to_thread(task, ...)``: the task runs under this try.

        Returns the index of the task argument, or -1 when the call is not one.
        """

        name = _name(node.func)
        index = _AWAITED_TASKS.get(name) if name is not None else None
        if (
            index is None
            or id(node) not in self.awaited
            or site.static
            or site.ctors
            or len(node.args) <= index
        ):
            return -1
        task = node.args[index]
        if isinstance(task, ast.Lambda):
            self.inline.add(id(task))
            self.visit(task.body, tries)
            return index
        refs, _forward, _classes = self.refs(task)
        if not refs:
            return -1
        self.assign_of[id(task)] = node
        site.static = set(refs)
        site.external = False
        return index

    def argument(
        self, site: _Site, node: ast.AST, index: int | None, keyword: str | None
    ) -> None:
        refs: set[Function] = set()
        forward = None
        classes: list[ClassInfo] = []
        if isinstance(node, ast.IfExp):
            # Both possible callback values fill the same parameter; only the
            # value references are bound. The condition is walked normally.
            self.argument(site, node.body, index, keyword)
            self.argument(site, node.orelse, index, keyword)
        elif isinstance(node, ast.Name | ast.Attribute):
            refs, forward, classes = self.refs(node)
            for cls in classes:
                refs = refs | self.graph._constructors(cls)
            argument = _Arg(index, keyword, refs, forward, classes)
            self.arg_of[id(node)] = (site, argument)
            site.args.append(argument)
        elif isinstance(node, ast.Call) and self.getattr_targets(node):
            site.args.append(_Arg(index, keyword, self.getattr_targets(node), None, []))

    def resolve_callee(self, func: ast.AST, site: _Site, tries: Tries) -> None:
        graph = self.graph
        if isinstance(func, ast.Name):
            current: Function | None = self.function
            while current is not None:
                info = graph.info[current]
                if func.id in info.nested:
                    site.static = {info.nested[func.id]}
                    return
                if current is self.function and func.id in self.aliases:
                    site.static = set(self.aliases[func.id])
                    if func.id in self.dynamic_names:
                        self.facts.dynamic.append(tries)
                    return
                if func.id in info.params:
                    site.cb_param = (current, func.id)
                    site.static |= self.callable_targets(func)
                    return
                current = info.parent
            if func.id in self.dynamic_names:
                self.facts.dynamic.append(tries)
                return
            targets = graph._name_targets(self.function, func.id)
            if targets:
                site.static = targets
                return
            classes = graph._classes_named(self.module, func.id)
            if classes:
                site.ctors = list(classes)
                return
            site.external = True
            if self.is_bound_here(func):
                # A callable the function picked out of a collection (``for name,
                # source in sources: source()``): where it goes is not visible
                # here, so it is a dynamic call that ``call_edges`` may declare.
                self.facts.dynamic.append(tries)
            return
        if isinstance(func, ast.Call):
            targets = self.getattr_targets(func)
            if targets:
                site.static = targets
            elif self.is_dynamic_getattr(func):
                self.facts.dynamic.append(tries)
            return
        if not isinstance(func, ast.Attribute):
            return
        receiver, attr = func.value, func.attr
        if (
            isinstance(receiver, ast.Call)
            and isinstance(receiver.func, ast.Name)
            and receiver.func.id == "super"
        ):
            own = self.info.self_class
            if own is not None:
                for parent in graph.mro(own)[1:]:
                    if attr in parent.methods:
                        site.static = {parent.methods[attr]}
                        break
            return
        if isinstance(receiver, ast.Name):
            imported = self.module.imports.get(receiver.id)
            if (
                imported is not None
                and imported[1] is None
                and not graph._is_local(self.function, receiver.id)
            ):
                target = graph.by_dotted.get(imported[0])
                if target is not None:
                    if attr in target.functions:
                        site.static = {target.functions[attr]}
                    elif attr in target.classes:
                        site.ctors = [target.classes[attr]]
                    else:
                        site.external = True
                else:
                    site.external = True
                return
            if (
                not graph._is_local(self.function, receiver.id)
                and receiver.id not in self.env
            ):
                classes = graph._classes_named(self.module, receiver.id)
                if classes and receiver.id != "self":
                    for cls in classes:
                        site.static |= graph.dispatch(cls, attr)
                    return
        types = graph.type_of(receiver, self.function, self.env)
        if not types:
            if attr in LIBRARY_METHOD_NAMES:
                graph.generic_calls += 1
                site.external = True
            else:
                site.fallback = True
                site.static = set(graph.methods_named.get(attr, ()))
                site.cb_attr_name = attr
            return
        classes = graph._internal(types, self.module)
        if not classes:
            site.external = True
            return
        for cls in classes:
            found = graph.dispatch(cls, attr)
            site.static |= found
            if not found:
                site.cb_attrs.append((cls.key, attr))
                site.static |= self.callable_targets(func)
        if not site.static and not site.cb_attrs:
            site.external = True

    def is_bound_here(self, func: ast.Name) -> bool:
        """A call of a name the function binds itself, outside any lambda.

        A lambda body runs later, outside the ``try`` that creates it, so a call
        in one is not the call a declared edge stands for.
        """

        if not hasattr(self, "_bound"):
            node = self.function.node
            self._bound: set[str] = {
                child.id
                for child in ast.walk(node)
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store)
            }
            self._deferred: set[int] = {
                id(call.func)
                for lam in ast.walk(node)
                if isinstance(lam, ast.Lambda)
                for call in ast.walk(lam.body)
                if isinstance(call, ast.Call)
            }
        return func.id in self._bound and id(func) not in self._deferred

    def callable_targets(self, func: ast.AST) -> set[Function]:
        """``x(...)`` where ``x`` is typed with a class that is itself callable."""

        found: set[Function] = set()
        types = self.graph.type_of(func, self.function, self.env)
        for cls in self.graph._internal(types, self.module):
            found |= self.graph.dispatch(cls, "__call__")
        return found


def build_graph(
    trees: Mapping[str, ast.Module], declared: Sequence[DeclaredEdge] = ()
) -> CallGraph:
    return CallGraph(trees, declared)
