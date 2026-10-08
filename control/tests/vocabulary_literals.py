"""Static ratchet: the lifecycle and outcome vocabulary is spelled by the contract, not by hand.

``vonk_agent_protocol.lifecycle_vocabulary`` is the one definition of the closed
words the lifecycle core, the agent results and the CI ratchets speak (states,
effects, outcome kinds, wait reasons, failure codes, security and invalid-request
reason codes, error categories, operator actions).  It is generated into Rust and
TypeScript.  Everything else must use the enum (or the generated type), never a
string that happens to equal one of its words.

The scan finds, in every non-test Python module of the repository and in the
TypeScript sources of the web app, string literals equal to a vocabulary word:

``distinctive``
    a word that cannot be mistaken for prose or an unrelated column: it contains
    ``-``, ``_`` or ``.`` (``waiting-for-operator``, ``needs-operator``,
    ``stop-unconfirmed``, ``operation_cancelled``, ``model_cache.credentials_denied``)
    or is a core-only state word (``observing``, ``backoff``);
``stored_state``
    ``queued``, ``running``, ``succeeded``, ``failed`` and ``cancelled``: the lifecycle
    state words that the persisted rows of the legacy kinds still carry.  Prose-like
    words that are also vocabulary members (``unknown``, ``none``, ``stop``, ``retry``)
    are not scanned: they would only produce noise.

Docstrings are not literals.  The contract modules and the Controller's legacy
adapter (:data:`ALLOWED_FILES`) are the only places allowed to spell the words.  The
gate is a ratchet, like the other allowlists: ``tools/vocabulary-literals-baseline.json``
records the literals that predate it, per file, and

* a literal in a file the baseline does not name fails (use the enum);
* a count above the recorded one fails (a second literal hid behind a listed one);
* a count below the recorded one fails until the baseline is lowered, so the
  ceiling only goes down;
* TypeScript has no baseline at all: the web app reads the generated
  ``vocabulary.generated.ts``.

Two further tiers have no baseline either; they are flat at zero.  The reason
codes (``vonk_agent_protocol.reason_codes``: blockers, refusals, warnings and
attention codes, grouped by domain) are closed enums too:

``reason_code``
    a string literal equal to a member of a reason-code enum, or a message that
    starts with one (``"run-switch.x: detail"``), anywhere outside the contract;
``code_position``
    a string constant (or an f-string) in a position that names a code: the
    ``code=`` / ``*_code=`` keyword of a call, the first argument of
    ``make_blocker``, the code argument of an error class or helper that takes
    one, a ``code`` / ``*_CODE`` class or module attribute and a ``code``
    parameter default.  This is what makes a *new* free-string code fail, whether
    or not it is a word the enums already know: add it to the enum first.

``python -m control.tests.vocabulary_literals --write-baseline`` lowers the counts
after a literal was removed; it never raises one.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import get_args

from vonk_agent_protocol import (
    LEGACY_WAIT_STATE,
    REASON_CODE_ENUMS,
    RETIRED_PROGRESS_PHASE_SPELLINGS,
    AgentResultState,
    ErrorCategory,
    FailureCode,
    InvalidRequestReason,
    LifecycleEventKind,
    LifecycleState,
    OperationMemberProgress,
    OperatorActionName,
    ProgressPhase,
    ResourceBlockerCode,
    RunAdmissionCode,
    SecurityRefusalReason,
    StateAlias,
    WaitReason,
)
from vonk_agent_protocol.state_machines import MACHINES, ModelCacheOperatorStatus

from .parsed_sources import memoized_scan, parse_file

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = REPO_ROOT / "tools" / "vocabulary-literals-baseline.json"

PYTHON_ROOTS = (
    "control/src",
    "agent_protocol/src",
    "src/cluster_profiles",
)
#: Generated Python (never hand-edited) is not scanned.
PYTHON_EXCLUDED_PREFIXES = ("src/cluster_profiles/generated_control/",)
#: The contract itself, and the one place that still reads an untyped agent body.
ALLOWED_FILES = frozenset(
    {
        "agent_protocol/src/vonk_agent_protocol/agent_words.py",
        "agent_protocol/src/vonk_agent_protocol/lifecycle_vocabulary.py",
        "agent_protocol/src/vonk_agent_protocol/reason_codes.py",
        "agent_protocol/src/vonk_agent_protocol/outcome.py",
        "agent_protocol/src/vonk_agent_protocol/state_machines.py",
        # Generated from ``GatewayRouteState`` for the LiteLLM supervisor, which
        # loads route_activation.py with no package around it.
        "agent_protocol/src/vonk_agent_protocol/route_activation_words.py",
        "control/src/vonk_control/agent_outcome.py",
        # The CLI ships without the contract package; its words are generated
        # from the contract by scripts/generate-python-vocabulary.
        "src/cluster_profiles/cli_states_generated.py",
    }
)

TYPESCRIPT_ROOT = "control/web/src"
#: Generated TypeScript and the tests that spell fixtures are not scanned.
TYPESCRIPT_GENERATED = frozenset(
    {
        "control/web/src/api/generated.d.ts",
        "control/web/src/api/vocabulary.generated.ts",
    }
)
_TYPESCRIPT_TEST = re.compile(r"\.(test|spec)\.tsx?$")
_TYPESCRIPT_STRING = re.compile(r"""(["'])((?:(?!\1)[^\\\n])*)\1""")

DISTINCTIVE = "distinctive"
STORED_STATE = "stored_state"
#: Retired spellings of a stored state that are also ordinary words
#: (``waiting``, ``partial``, ``cancelling``, ``expired``), found by context.
LEGACY_STATE = "legacy_state"
#: The plain words of the contract's state machines (installations, runs, routes,
#: distribution assignments, endpoints, ...), found by context like the retired
#: spellings: only a statement that names a state counts.
MACHINE_STATE = "machine_state"
TIERS = (DISTINCTIVE, STORED_STATE, LEGACY_STATE, MACHINE_STATE)
#: Tiers without a baseline: any occurrence fails.
REASON_CODE = "reason_code"
CODE_POSITION = "code_position"
#: A progress phase spelled by hand where measured progress is built or read.
PROGRESS_PHASE = "progress_phase"
FLAT_TIERS = (REASON_CODE, CODE_POSITION, PROGRESS_PHASE)


#: The lifecycle state words that persisted rows of the legacy kinds still carry.
STORED_STATE_WORDS = frozenset(
    {
        LifecycleState.QUEUED.value,
        LifecycleState.RUNNING.value,
        LifecycleState.SUCCEEDED.value,
        LifecycleState.FAILED.value,
        LifecycleState.CANCELLED.value,
    }
)
#: State words that are plain English yet specific to the core.
_CORE_ONLY_WORDS = frozenset(
    {LifecycleState.OBSERVING.value, LifecycleState.BACKOFF.value}
)
_SEPARATORS = ("-", "_", ".")

#: The retired state spellings the distinctive tier cannot see (it already finds
#: ``waiting-for-operator`` in any context).  Only :data:`STATE_ALIASES` may spell
#: them; they are read as a *state* when the statement that holds them also names
#: a state (``row.state``, ``state=``, ``_STORED_STATES``, ``State.RUNNING``).
LEGACY_STATE_WORDS = frozenset(
    alias.value for alias in StateAlias if alias is not StateAlias.WAITING_FOR_OPERATOR
)
#: The machine words that name nothing but a state of one of those machines: the
#: lifecycle words and the retired spellings have their own tiers, and a word that
#: is also ordinary prose or a field value elsewhere (``active``, ``pending``,
#: ``current``, ``valid``, ``complete``, ``unknown``, ``missing``, ``planned``) is not
#: scanned: it would only produce noise.
MACHINE_STATE_WORDS = frozenset(
    {
        "installing",
        "installed",
        "uninstalled",
        "starting",
        "stopping",
        "stopped",
        "lost",
        "withdrawn",
        "published",
        "promised",
        "released",
        "corrupt",
        "consumed",
        "verified",
        "syncing",
    }
)
_STATE_CONTEXT = re.compile(r"state", re.IGNORECASE)


def _machine_words() -> frozenset[str]:
    """Every word of the contract's state machines."""

    words = {member.value for machine in MACHINES.values() for member in machine}
    words.update(member.value for member in ModelCacheOperatorStatus)
    return frozenset(words)


MACHINE_WORDS = _machine_words()


def _vocabulary_words() -> frozenset[str]:
    words: set[str] = {LEGACY_WAIT_STATE, *MACHINE_WORDS}
    for enum in (
        LifecycleState,
        AgentResultState,
        LifecycleEventKind,
        OperatorActionName,
        ErrorCategory,
        WaitReason,
        InvalidRequestReason,
        SecurityRefusalReason,
        FailureCode,
        RunAdmissionCode,
        ResourceBlockerCode,
    ):
        words.update(member.value for member in enum)
    return frozenset(words)


#: The Controller is where reasons are raised.  The CLI ships without the contract
#: package, so it spells the codes it renders; ``test_the_cli_spells_only_contract_codes``
#: keeps those spellings equal to members.
REASON_CODE_ROOT = "control/src/"
#: Enums whose words are ordinary kind names that other columns share
#: (``recipe-installation`` is also an entity kind); a literal is not a code by
#: its spelling alone, so only the typed fields carry these.
PLAIN_WORD_ENUMS = frozenset({"CacheReferenceReason"})
#: Every word of a reason-code enum that cannot be mistaken for prose
#: (``reconcile.membership_changed``, ``superseded-by-intent``).  The plain words
#: some enums also carry (``stale``, ``context``) are typed by their field instead.
REASON_CODE_WORDS = frozenset(
    member.value
    for enum in REASON_CODE_ENUMS
    if enum.__name__ not in PLAIN_WORD_ENUMS
    for member in enum
    if any(mark in member.value for mark in _SEPARATORS)
)
_REASON_PREFIX = re.compile(r"^([a-z][a-z0-9_.-]*[.-][a-z0-9_.-]*?)(?::\s|\s|$)")

VOCABULARY_WORDS = _vocabulary_words()
DISTINCTIVE_WORDS = frozenset(
    word
    for word in VOCABULARY_WORDS
    if word not in STORED_STATE_WORDS
    and (any(mark in word for mark in _SEPARATORS) or word in _CORE_ONLY_WORDS)
)


#: Every word a measured progress phase can be, including the retired spellings an
#: older agent or Controller wrote (read through ``adopt_progress_phase``).
PROGRESS_PHASE_WORDS = frozenset(
    {member.value for member in ProgressPhase} | set(RETIRED_PROGRESS_PHASE_SPELLINGS)
)
_PROGRESS_CALLS = frozenset({"OperationProgress", "OperationMemberProgress"})
_PROGRESS_OWNER = re.compile(r"progress|measurement|member", re.IGNORECASE)


def _constant_strings(node: ast.AST | None) -> Iterator[ast.Constant]:
    """String constants an expression can evaluate to, or be compared with."""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node
    elif isinstance(node, ast.IfExp):
        yield from _constant_strings(node.body)
        yield from _constant_strings(node.orelse)
    elif isinstance(node, ast.BoolOp):
        for value in node.values:
            yield from _constant_strings(value)
    elif isinstance(node, (ast.Set, ast.Tuple, ast.List)):
        for element in node.elts:
            yield from _constant_strings(element)


def _is_phase_of_progress(node: ast.expr) -> bool:
    """``progress.phase``, ``member["phase"]``, ``measurement.get("phase")``."""

    if isinstance(node, ast.Attribute) and node.attr == "phase":
        return bool(_PROGRESS_OWNER.search(ast.unparse(node.value)))
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == "phase"
    ):
        return bool(_PROGRESS_OWNER.search(ast.unparse(node.value)))
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and bool(node.args)
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "phase"
        and bool(_PROGRESS_OWNER.search(ast.unparse(node.func.value)))
    )


def _progress_phase_literals(tree: ast.Module) -> Iterator[ast.Constant]:
    """Phase words spelled where measured progress is built or read.

    A phase word is plain English and the same words name other things (a plan
    phase kind, a start phase), so the scan reads only the places that are
    measured progress: the ``phase=`` of ``OperationProgress`` and
    ``OperationMemberProgress``, the ``"phase"`` of a progress-shaped dict (one that
    also carries ``completed_bytes``) or of a ``model_copy(update=...)``, and a
    comparison with the phase of a progress, measurement or member.
    """

    for node in ast.walk(tree):
        leaves: list[ast.Constant] = []
        if isinstance(node, ast.Call):
            function = node.func
            name = (
                function.id
                if isinstance(function, ast.Name)
                else function.attr
                if isinstance(function, ast.Attribute)
                else None
            )
            for keyword in node.keywords:
                if name in _PROGRESS_CALLS and keyword.arg == "phase":
                    leaves.extend(_constant_strings(keyword.value))
                if name == "model_copy" and keyword.arg == "update":
                    leaves.extend(
                        leaf
                        for key, value in _dict_items(keyword.value)
                        if key == "phase"
                        for leaf in _constant_strings(value)
                    )
        elif isinstance(node, ast.Dict):
            keys = {key for key, _ in _dict_items(node)}
            if {"phase", "completed_bytes"} <= keys:
                for key, value in _dict_items(node):
                    if key == "phase":
                        leaves.extend(_constant_strings(value))
        elif isinstance(node, ast.Compare) and any(
            isinstance(op, (ast.Eq, ast.NotEq, ast.In, ast.NotIn)) for op in node.ops
        ):
            if _is_phase_of_progress(node.left):
                for comparator in node.comparators:
                    leaves.extend(_constant_strings(comparator))
        for leaf in leaves:
            if leaf.value in PROGRESS_PHASE_WORDS:
                yield leaf


def _dict_items(node: ast.AST) -> Iterator[tuple[object, ast.expr]]:
    if isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values, strict=True):
            if isinstance(key, ast.Constant):
                yield key.value, value


def tier_of(word: str) -> str | None:
    """The tier a literal belongs to, or ``None`` when it is not scanned."""

    if word in DISTINCTIVE_WORDS:
        return DISTINCTIVE
    if word in STORED_STATE_WORDS:
        return STORED_STATE
    return None


def _docstring_ids(tree: ast.Module) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(
                node,
                (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            )
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        ):
            found.add(id(node.body[0].value))
    return found


def _header(statement: ast.stmt) -> list[ast.AST]:
    """The part of a statement that is not a nested block of statements."""

    if isinstance(statement, (ast.If, ast.While)):
        return [statement.test]
    if isinstance(statement, (ast.For, ast.AsyncFor)):
        return [statement.target, statement.iter]
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        return list(statement.items)
    if isinstance(statement, ast.Match):
        return [statement.subject]
    if isinstance(
        statement,
        (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Try, ast.TryStar),
    ):
        return []
    return [statement]


def _names_a_state(statement: ast.stmt) -> bool:
    """Whether a statement names a state: an identifier, attribute, keyword or key."""

    for part in _header(statement):
        for node in ast.walk(part):
            if isinstance(node, ast.Name) and _STATE_CONTEXT.search(node.id):
                return True
            if isinstance(node, ast.Attribute) and _STATE_CONTEXT.search(node.attr):
                return True
            if (
                isinstance(node, ast.keyword)
                and node.arg
                and _STATE_CONTEXT.search(node.arg)
            ):
                return True
            if isinstance(node, ast.Constant) and node.value == "state":
                return True
    return False


def _flag_keys(tree: ast.Module) -> set[int]:
    """Dict keys that name a boolean flag (``{"stopped": True}``), never a state."""

    return {
        id(key)
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values, strict=True)
        if key is not None
        and isinstance(value, ast.Constant)
        and isinstance(value.value, bool)
    }


def _activity_result_literals(tree: ast.Module) -> set[int]:
    """Resolve result leaves of the canonical progress activity field only."""
    imports = [
        node
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and node.level == 0
        and node.module == "vonk_agent_protocol"
        for alias in node.names
        if alias.name == "OperationMemberProgress"
    ]
    if len(imports) != 1:
        return set()
    imported = imports[0]
    name = next(
        alias.asname or alias.name
        for alias in imported.names
        if alias.name == "OperationMemberProgress"
    )
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Name)
            and node.id == name
            and isinstance(node.ctx, (ast.Store, ast.Del))
        ):
            return set()
        if isinstance(node, ast.arg) and node.arg == name:
            return set()
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == name
        ):
            return set()
        if isinstance(node, ast.ExceptHandler) and node.name == name:
            return set()
        if (
            isinstance(node, (ast.Import, ast.ImportFrom))
            and node is not imported
            and any(
                alias.name == "*" or (alias.asname or alias.name) == name
                for alias in node.names
            )
        ):
            return set()
    activity_words = {
        word
        for alternative in get_args(
            OperationMemberProgress.model_fields["activity"].annotation
        )
        for word in get_args(alternative)
        if isinstance(word, str)
    }

    def leaves(value: ast.expr) -> Iterator[ast.Constant]:
        if isinstance(value, ast.Constant) and value.value in activity_words:
            yield value
        elif isinstance(value, ast.IfExp):
            yield from leaves(value.body)
            yield from leaves(value.orelse)

    return {
        id(leaf)
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == name
        for keyword in call.keywords
        if keyword.arg == "activity"
        for leaf in leaves(keyword.value)
    }


def _state_literals(
    tree: ast.Module, docstrings: set[int], words: frozenset[str]
) -> Iterator[ast.Constant]:
    """Literals of ``words`` in a statement that names a state."""

    flags = _flag_keys(tree)
    activity = _activity_result_literals(tree)
    for statement in ast.walk(tree):
        if not isinstance(statement, ast.stmt) or not _names_a_state(statement):
            continue
        for part in _header(statement):
            for node in ast.walk(part):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and node.value in words
                    and id(node) not in docstrings
                    and id(node) not in flags
                    and id(node) not in activity
                ):
                    yield node


def _python_files() -> Iterator[Path]:
    for root in PYTHON_ROOTS:
        yield from sorted((REPO_ROOT / root).rglob("*.py"))


def _is_reason_code_text(text: str) -> bool:
    """A reason-code word, or a message that starts with one (``code: detail``)."""

    if text in REASON_CODE_WORDS:
        return True
    match = _REASON_PREFIX.match(text)
    return bool(
        match and match.group(1) in REASON_CODE_WORDS and text != match.group(1)
    )


def scan_python(
    files: Iterable[Path] | None = None, root: Path = REPO_ROOT
) -> Counter[tuple[str, str]]:
    """Literal counts per ``(tier, repository-relative path)``.

    The whole-repository scan (no ``files``) runs once while the tree is unchanged."""

    if files is not None:
        return _scan_python(files, root)
    return memoized_scan(
        ("vocabulary", root),
        [root / path for path in PYTHON_ROOTS],
        lambda: _scan_python(None, root),
    )


def _scan_python(files: Iterable[Path] | None, root: Path) -> Counter[tuple[str, str]]:
    counts: Counter[tuple[str, str]] = Counter()
    for path in files if files is not None else _python_files():
        relative = path.relative_to(root).as_posix()
        if relative in ALLOWED_FILES or relative.startswith(PYTHON_EXCLUDED_PREFIXES):
            continue
        source, tree = parse_file(path)
        docstrings = _docstring_ids(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and (tier := tier_of(node.value)) is not None
            ):
                counts[(tier, relative)] += 1
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and relative.startswith(REASON_CODE_ROOT)
                and _is_reason_code_text(node.value)
            ):
                counts[(REASON_CODE, relative)] += 1
        if relative.startswith(REASON_CODE_ROOT):
            found_phase = sum(1 for _ in _progress_phase_literals(tree))
            if found_phase:
                counts[(PROGRESS_PHASE, relative)] += found_phase
        for tier, words in (
            (LEGACY_STATE, LEGACY_STATE_WORDS),
            (MACHINE_STATE, MACHINE_STATE_WORDS),
        ):
            if not any(
                f'"{word}"' in source or f"'{word}'" in source for word in words
            ):
                continue
            found = sum(1 for _ in _state_literals(tree, docstrings, words))
            if found:
                counts[(tier, relative)] += found
    return counts


def scan_typescript(
    files: Iterable[Path] | None = None, root: Path = REPO_ROOT
) -> Counter[str]:
    """Distinctive literals per TypeScript file (the web app has no baseline)."""

    counts: Counter[str] = Counter()
    if files is None:
        files = sorted(
            path
            for suffix in ("*.ts", "*.tsx")
            for path in (REPO_ROOT / TYPESCRIPT_ROOT).rglob(suffix)
        )
    for path in files:
        relative = path.relative_to(root).as_posix()
        if relative in TYPESCRIPT_GENERATED or _TYPESCRIPT_TEST.search(relative):
            continue
        for match in _TYPESCRIPT_STRING.finditer(path.read_text(encoding="utf-8")):
            if (
                match.group(2) in DISTINCTIVE_WORDS
                or match.group(2) in REASON_CODE_WORDS
            ):
                counts[relative] += 1
    return counts


#: The Controller is where reasons are raised; the CLI only renders them.
CODE_POSITION_ROOT = "control/src"
_CODE_NAME = re.compile(r"^(code|reason_code|[a-z0-9_]+_code|[A-Z0-9_]+_CODE)$")
#: Names ending in ``_code`` that are not reason codes (HTTP and process statuses).
_NOT_REASON_CODE_NAMES = frozenset(
    {"status_code", "exit_code", "return_code", "http_code", "returncode"}
)


def _is_code_name(name: str) -> bool:
    return bool(_CODE_NAME.match(name)) and name.lower() not in _NOT_REASON_CODE_NAMES


def _string_leaves(node: ast.AST | None) -> Iterator[ast.expr]:
    """String constants and f-strings an expression can evaluate to."""

    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value
        or isinstance(node, ast.JoinedStr)
    ):
        yield node
    elif isinstance(node, ast.IfExp):
        yield from _string_leaves(node.body)
        yield from _string_leaves(node.orelse)
    elif isinstance(node, ast.BoolOp):
        for value in node.values:
            yield from _string_leaves(value)


def _code_parameter_index(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> int | None:
    arguments = [*function.args.posonlyargs, *function.args.args]
    if arguments and arguments[0].arg in {"self", "cls"}:
        arguments = arguments[1:]
    for index, argument in enumerate(arguments):
        if _is_code_name(argument.arg):
            return index
    return None


def scan_code_positions(
    files: Iterable[Path] | None = None, root: Path = REPO_ROOT
) -> list[str]:
    """Every string constant in a position that names a code, as ``path:line: why``.

    The whole-tree scan (no ``files``) runs once while the tree is unchanged."""

    if files is not None:
        return _scan_code_positions(files, root)
    return memoized_scan(
        ("code-positions", root),
        [REPO_ROOT / CODE_POSITION_ROOT],
        lambda: _scan_code_positions(None, root),
    )


def _scan_code_positions(files: Iterable[Path] | None, root: Path) -> list[str]:
    paths = (
        list(files)
        if files is not None
        else sorted((REPO_ROOT / CODE_POSITION_ROOT).rglob("*.py"))
    )
    trees = {path: parse_file(path).tree for path in paths}
    class_index: dict[str, int] = {}
    class_bases: dict[str, list[str]] = {}
    own_init: set[str] = set()
    for tree in trees.values():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            class_bases[node.name] = [
                base.id if isinstance(base, ast.Name) else ast.unparse(base)
                for base in node.bases
            ]
            for member in node.body:
                if isinstance(member, ast.FunctionDef) and member.name == "__init__":
                    own_init.add(node.name)
                    index = _code_parameter_index(member)
                    if index is not None:
                        class_index[node.name] = index

    def class_code_index(name: str, seen: frozenset[str] = frozenset()) -> int | None:
        if name in class_index:
            return class_index[name]
        if name in own_init:
            # Its own constructor takes no code (it passes a fixed one up), so a
            # string given to it is a detail, not a code.
            return None
        for base in class_bases.get(name, ()):
            if base not in seen:
                found = class_code_index(base, seen | {name})
                if found is not None:
                    return found
        return None

    found: list[str] = []

    def report(path: Path, leaf: ast.expr, why: str) -> None:
        relative = path.relative_to(root).as_posix()
        found.append(f"{relative}:{leaf.lineno}: {why}: {ast.unparse(leaf)[:60]}")

    for path, tree in trees.items():
        functions = {
            node.name: index
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name != "__init__"
            and (index := _code_parameter_index(node)) is not None
        }
        scopes = [tree, *(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef))]
        for scope in scopes:
            for statement in scope.body:
                targets: list[ast.expr] = []
                if isinstance(statement, ast.Assign):
                    targets = statement.targets
                elif isinstance(statement, ast.AnnAssign):
                    targets = [statement.target]
                for target in targets:
                    if isinstance(target, ast.Name) and _is_code_name(target.id):
                        value = getattr(statement, "value", None)
                        for leaf in _string_leaves(value):
                            report(path, leaf, f"attribute {target.id}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                        and _is_code_name(target.attr)
                    ):
                        for leaf in _string_leaves(node.value):
                            report(path, leaf, f"self.{target.attr}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                arguments = [*node.args.posonlyargs, *node.args.args]
                defaults = node.args.defaults
                for argument, default in zip(
                    arguments[len(arguments) - len(defaults) :], defaults, strict=True
                ):
                    if _is_code_name(argument.arg):
                        for leaf in _string_leaves(default):
                            report(path, leaf, f"default of {argument.arg}")
            elif isinstance(node, ast.Call):
                function = node.func
                name = (
                    function.id
                    if isinstance(function, ast.Name)
                    else function.attr
                    if isinstance(function, ast.Attribute)
                    else None
                )
                for keyword in node.keywords:
                    if keyword.arg and _is_code_name(keyword.arg):
                        for leaf in _string_leaves(keyword.value):
                            report(path, leaf, f"{name}({keyword.arg}=)")
                if name is None:
                    continue
                index = (
                    0
                    if name == "make_blocker"
                    else class_code_index(name)
                    if name in class_bases
                    else functions.get(name)
                    if isinstance(function, ast.Name)
                    else None
                )
                if index is not None and len(node.args) > index:
                    for leaf in _string_leaves(node.args[index]):
                        report(path, leaf, f"code argument of {name}")
    return sorted(set(found))


def load_baseline() -> dict[str, dict[str, int]]:
    document = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    return {tier: dict(document.get(tier, {})) for tier in TIERS}


def problems(
    counts: Counter[tuple[str, str]], baseline: dict[str, dict[str, int]]
) -> list[str]:
    """Every way the repository breaks the ratchet, in a stable order."""

    baseline = relocated_baseline(counts, baseline)
    found: list[str] = []
    for tier in TIERS:
        recorded = baseline.get(tier, {})
        current = {path: n for (t, path), n in counts.items() if t == tier and n > 0}
        for path in sorted(current.keys() | recorded.keys()):
            now, before = current.get(path, 0), recorded.get(path, 0)
            if now > before:
                found.append(
                    f"{path}: {now} {tier} vocabulary literal(s), the baseline allows "
                    f"{before}; use the contract enum instead of spelling the word"
                )
            elif now < before:
                found.append(
                    f"{path}: {tier} count fell from {before} to {now}; lower "
                    f"tools/vocabulary-literals-baseline.json "
                    f"(--write-baseline)"
                )
    return found


def flat_problems(
    counts: Counter[tuple[str, str]], positions: Iterable[str]
) -> list[str]:
    """The flat tiers: every reason-code literal and every free-string code position."""

    found = [
        f"{path}: {n} reason-code literal(s); use the member of the "
        f"vonk_agent_protocol.reason_codes enum instead of spelling the word"
        for (tier, path), n in sorted(counts.items())
        if tier == REASON_CODE
    ]
    found.extend(
        f"{path}: {n} progress phase literal(s); use the "
        f"vonk_agent_protocol.ProgressPhase member (and adopt_progress_phase to read "
        f"an agent's word) instead of spelling the phase"
        for (tier, path), n in sorted(counts.items())
        if tier == PROGRESS_PHASE
    )
    found.extend(
        f"{position} (a code is a member of a vonk_agent_protocol.reason_codes "
        f"enum, never a string; add the code to its domain enum first)"
        for position in positions
    )
    return found


def relocated_baseline(counts, baseline, moves=None):
    from .package_moves import PackageMoves

    moves = moves or PackageMoves(REPO_ROOT)
    return {
        tier: dict(
            sorted(
                moves.counts(
                    baseline.get(tier, {}),
                    {path: n for (t, path), n in counts.items() if t == tier},
                ).items()
            )
        )
        for tier in TIERS
    }


def lowered_baseline(
    counts: Counter[tuple[str, str]], baseline: dict[str, dict[str, int]]
) -> dict[str, dict[str, int]]:
    """The baseline with every fallen count lowered; a risen count is left to fail."""

    baseline = relocated_baseline(counts, baseline)
    updated: dict[str, dict[str, int]] = {}
    for tier in TIERS:
        updated[tier] = {}
        for path, before in baseline.get(tier, {}).items():
            now = counts.get((tier, path), 0)
            if now > 0:
                updated[tier][path] = min(now, before)
    return updated


def write_baseline(counts: Counter[tuple[str, str]]) -> None:
    updated = lowered_baseline(counts, load_baseline())
    document = {
        "schema": 1,
        "note": (
            "Vocabulary literals that predate the guard, per file. The counts only "
            "fall: replace a literal with the contract enum and lower the count."
        ),
        **{tier: dict(sorted(updated[tier].items())) for tier in TIERS},
    }
    BASELINE_PATH.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    counts = scan_python()
    if "--write-baseline" in argv:
        write_baseline(counts)
        return 0
    if "--list" in argv:
        for (tier, path), n in sorted(counts.items()):
            print(f"{tier}\t{n}\t{path}")
        return 0
    found = problems(counts, load_baseline()) + flat_problems(
        counts, scan_code_positions()
    )
    for line in found:
        print(line)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
