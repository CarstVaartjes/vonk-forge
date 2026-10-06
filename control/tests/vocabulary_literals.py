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
from vonk_agent_protocol.state_machines import MACHINES, ModelCacheOperatorStatus

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
        "agent_protocol/src/vonk_agent_protocol/lifecycle_vocabulary.py",
        "agent_protocol/src/vonk_agent_protocol/outcome.py",
        "agent_protocol/src/vonk_agent_protocol/state_machines.py",
        # Loaded on its own by the LiteLLM supervisor (no package around it), so it
        # keeps its own two words; test_vocabulary_literals keeps them equal to
        # ``GatewayRouteState``.
        "agent_protocol/src/vonk_agent_protocol/route_activation.py",
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
#: The plain words of the contract's state machines (installations, runs, routes,
#: distribution assignments, endpoints, ...), found by context like the retired
#: spellings: only a statement that names a state counts.
MACHINE_STATE = "machine_state"
TIERS = (DISTINCTIVE, STORED_STATE, LEGACY_STATE, MACHINE_STATE)

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


def _state_literals(
    tree: ast.Module, docstrings: set[int], words: frozenset[str]
) -> Iterator[ast.Constant]:
    """Literals of ``words`` in a statement that names a state."""

    flags = _flag_keys(tree)
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
                ):
                    yield node


def _python_files() -> Iterator[Path]:
    for root in PYTHON_ROOTS:
        yield from sorted((REPO_ROOT / root).rglob("*.py"))


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
            if match.group(2) in DISTINCTIVE_WORDS:
                counts[relative] += 1
    return counts


def load_baseline() -> dict[str, dict[str, int]]:
    document = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    return {tier: dict(document.get(tier, {})) for tier in TIERS}


def problems(
    counts: Counter[tuple[str, str]], baseline: dict[str, dict[str, int]]
) -> list[str]:
    """Every way the repository breaks the ratchet, in a stable order."""

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


def lowered_baseline(
    counts: Counter[tuple[str, str]], baseline: dict[str, dict[str, int]]
) -> dict[str, dict[str, int]]:
    """The baseline with every fallen count lowered; a risen count is left to fail."""

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
    found = problems(counts, load_baseline())
    for line in found:
        print(line)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
