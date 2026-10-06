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

from vonk_agent_protocol import (
    LEGACY_WAIT_STATE,
    REASON_CODE_ENUMS,
    AgentResultState,
    ErrorCategory,
    FailureCode,
    InvalidRequestReason,
    LifecycleEventKind,
    LifecycleState,
    OperatorActionName,
    ResourceBlockerCode,
    RunAdmissionCode,
    SecurityRefusalReason,
    StateAlias,
    WaitReason,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = REPO_ROOT / "tools" / "vocabulary-literals-baseline.json"

PYTHON_ROOTS = (
    "control/src",
    "agent_protocol/src",
    "src/cluster_profiles",
)
#: Generated Python (never hand-edited) is not scanned.
PYTHON_EXCLUDED_PREFIXES = ("src/cluster_profiles/generated_control/",)
#: Sites whose ``expired`` / ``partial`` / ``waiting`` belong to a state machine that
#: is not a lifecycle subject (a certificate, an enrollment grant, a distribution
#: assignment, a recipe installation or model file record, a catalog sync), counted
#: per file.  The legacy-state tier subtracts them, so the baseline holds only
#: lifecycle debt and ends at zero; a count that no longer matches fails as stale.
NON_LIFECYCLE_STATE_SITES: dict[str, tuple[int, str]] = {
    "control/src/vonk_control/agent_jobs.py": (
        2,
        "distribution assignment; installation",
    ),
    "control/src/vonk_control/fleet_profile_contract.py": (1, "endpoint state"),
    "control/src/vonk_control/fleet_profiles.py": (1, "installation state"),
    "control/src/vonk_control/recipe_operations.py": (7, "installation state"),
    "control/src/vonk_control/run_switch_operations.py": (2, "installation state"),
    "control/src/vonk_control/artifact_reference_scan.py": (
        2,
        "distribution assignment",
    ),
    "control/src/vonk_control/attempt_residues.py": (
        2,
        "installation; distribution assignment",
    ),
    "control/src/vonk_control/catalog_api.py": (1, "catalog sync state"),
    "control/src/vonk_control/catalog_sync.py": (1, "catalog sync state"),
    "control/src/vonk_control/catalog_sync_contract.py": (1, "catalog sync state"),
    "control/src/vonk_control/disk_reservations.py": (1, "recipe installation state"),
    "control/src/vonk_control/distribution.py": (1, "distribution assignment"),
    "control/src/vonk_control/enrollment.py": (1, "enrollment grant"),
    "control/src/vonk_control/enrollment_contract.py": (1, "enrollment grant"),
    "control/src/vonk_control/fleet_projection.py": (
        3,
        "certificate and installation state",
    ),
    "control/src/vonk_control/library_contract.py": (
        3,
        "installation and install_state",
    ),
    "control/src/vonk_control/library_projection.py": (1, "installation state"),
    "control/src/vonk_control/metrics.py": (1, "certificate state"),
    "control/src/vonk_control/model_cache.py": (
        9,
        "model file, asset and installation state",
    ),
    "control/src/vonk_control/model_cache_contract.py": (1, "model file state"),
    "control/src/vonk_control/recipe_action_plans.py": (1, "installation state"),
    "control/src/vonk_control/unused_storage_collection.py": (1, "installation state"),
}
#: The contract itself, and the one place that still reads an untyped agent body.
ALLOWED_FILES = frozenset(
    {
        "agent_protocol/src/vonk_agent_protocol/lifecycle_vocabulary.py",
        "agent_protocol/src/vonk_agent_protocol/reason_codes.py",
        "agent_protocol/src/vonk_agent_protocol/outcome.py",
        "control/src/vonk_control/agent_outcome.py",
        # The CLI ships without the contract package; this is its one copy of the
        # words, and test_vocabulary_literals keeps it equal to the contract.
        "src/cluster_profiles/cli_states.py",
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
TIERS = (DISTINCTIVE, STORED_STATE, LEGACY_STATE)
#: Tiers without a baseline: any occurrence fails.
REASON_CODE = "reason_code"
CODE_POSITION = "code_position"
FLAT_TIERS = (REASON_CODE, CODE_POSITION)

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
_STATE_CONTEXT = re.compile(r"state", re.IGNORECASE)


def _vocabulary_words() -> frozenset[str]:
    words: set[str] = {LEGACY_WAIT_STATE}
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


def _legacy_state_literals(
    tree: ast.Module, docstrings: set[int]
) -> Iterator[ast.Constant]:
    """Retired state spellings in a statement that names a state."""

    for statement in ast.walk(tree):
        if not isinstance(statement, ast.stmt) or not _names_a_state(statement):
            continue
        for part in _header(statement):
            for node in ast.walk(part):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and node.value in LEGACY_STATE_WORDS
                    and id(node) not in docstrings
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
    """Literal counts per ``(tier, repository-relative path)``."""

    counts: Counter[tuple[str, str]] = Counter()
    for path in files if files is not None else _python_files():
        relative = path.relative_to(root).as_posix()
        if relative in ALLOWED_FILES or relative.startswith(PYTHON_EXCLUDED_PREFIXES):
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative)
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
        found = (
            sum(1 for _ in _legacy_state_literals(tree, docstrings))
            if any(
                f'"{word}"' in source or f"'{word}'" in source
                for word in LEGACY_STATE_WORDS
            )
            else 0
        )
        if found:
            counts[(LEGACY_STATE, relative)] += found
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
    """Every string constant in a position that names a code, as ``path:line: why``."""

    paths = (
        list(files)
        if files is not None
        else sorted((REPO_ROOT / CODE_POSITION_ROOT).rglob("*.py"))
    )
    trees = {
        path: ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for path in paths
    }
    class_index: dict[str, int] = {}
    class_bases: dict[str, list[str]] = {}
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
                    index = _code_parameter_index(member)
                    if index is not None:
                        class_index[node.name] = index

    def class_code_index(name: str, seen: frozenset[str] = frozenset()) -> int | None:
        if name in class_index:
            return class_index[name]
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
    return {tier: dict(document[tier]) for tier in TIERS}


def floor_problems(counts: Counter[tuple[str, str]]) -> list[str]:
    """Non-lifecycle floors that the repository no longer holds exactly."""

    found: list[str] = []
    for path, (floor, reason) in sorted(NON_LIFECYCLE_STATE_SITES.items()):
        if counts.get((LEGACY_STATE, path), 0) < floor:
            found.append(
                f"{path}: fewer than the {floor} non-lifecycle legacy-state site(s) "
                f"({reason}) recorded in NON_LIFECYCLE_STATE_SITES; lower the floor"
            )
    return found


def problems(
    counts: Counter[tuple[str, str]], baseline: dict[str, dict[str, int]]
) -> list[str]:
    """Every way the repository breaks the ratchet, in a stable order."""

    found: list[str] = []
    for tier in TIERS:
        recorded = baseline[tier]
        current = {
            path: n
            - (
                NON_LIFECYCLE_STATE_SITES[path][0]
                if tier == LEGACY_STATE and path in NON_LIFECYCLE_STATE_SITES
                else 0
            )
            for (t, path), n in counts.items()
            if t == tier
        }
        current = {path: n for path, n in current.items() if n > 0}
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
        f"{position} (a code is a member of a vonk_agent_protocol.reason_codes "
        f"enum, never a string; add the code to its domain enum first)"
        for position in positions
    )
    return found


def lowered_baseline(
    counts: Counter[tuple[str, str]], baseline: dict[str, dict[str, int]]
) -> dict[str, dict[str, int]]:
    """The baseline with every fallen count lowered; a risen count is left to fail."""

    updated: dict[str, dict[str, int]] = {}
    for tier in TIERS:
        updated[tier] = {}
        for path, before in baseline[tier].items():
            now = counts.get((tier, path), 0)
            if tier == LEGACY_STATE:
                now -= NON_LIFECYCLE_STATE_SITES.get(path, (0, ""))[0]
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
    found = (
        floor_problems(counts)
        + problems(counts, load_baseline())
        + flat_problems(counts, scan_code_positions())
    )
    for line in found:
        print(line)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
