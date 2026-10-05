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
    SecurityRefusalReason,
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
#: The contract itself, and the one place that still reads an untyped agent body.
ALLOWED_FILES = frozenset(
    {
        "agent_protocol/src/vonk_agent_protocol/lifecycle_vocabulary.py",
        "agent_protocol/src/vonk_agent_protocol/outcome.py",
        "control/src/vonk_control/agent_outcome.py",
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
TIERS = (DISTINCTIVE, STORED_STATE)

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
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        docstrings = _docstring_ids(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and (tier := tier_of(node.value)) is not None
            ):
                counts[(tier, relative)] += 1
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
    return {tier: dict(document[tier]) for tier in TIERS}


def problems(
    counts: Counter[tuple[str, str]], baseline: dict[str, dict[str, int]]
) -> list[str]:
    """Every way the repository breaks the ratchet, in a stable order."""

    found: list[str] = []
    for tier in TIERS:
        recorded = baseline[tier]
        current = {path: n for (t, path), n in counts.items() if t == tier}
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
        for path, before in baseline[tier].items():
            now = counts.get((tier, path), 0)
            if now:
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
    for line in problems(counts, load_baseline()):
        print(line)
    return 1 if problems(counts, load_baseline()) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
