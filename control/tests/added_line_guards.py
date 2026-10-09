"""Count-free guards over an externally supplied unified diff. Never reads Git."""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Sequence
from functools import cache
from pathlib import Path

from . import content_identity_boundaries as identity
from . import coordination_boundaries as coordination
from . import principle_guards as principles
from . import untyped_mapping_boundaries as mappings
from . import vocabulary_literals as vocabulary

ROOT = Path(__file__).resolve().parents[2]


def rust_test_lines(source: str) -> set[int]:
    """Identify cfg(test) items without hiding subsequent production items."""
    masked = re.sub(
        r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"',
        lambda match: re.sub(r"[^\n]", " ", match.group()),
        source,
        flags=re.DOTALL,
    )
    lines: set[int] = set()
    for match in re.finditer(r"#\[cfg\(test\)\]", masked):
        opening = masked.find("{", match.end())
        terminator = masked.find(";", match.end())
        if terminator >= 0 and (opening < 0 or terminator < opening):
            end = terminator + 1
        elif opening >= 0:
            depth = 1
            end = opening + 1
            while depth and end < len(masked):
                depth += (masked[end] == "{") - (masked[end] == "}")
                end += 1
        else:
            continue
        lines.update(
            range(
                masked.count("\n", 0, match.start()) + 1, masked.count("\n", 0, end) + 2
            )
        )
    return lines


MODES = (
    "vocabulary",
    "mapping",
    "identity",
    "security",
    "coordination",
    "waits",
    "reads",
    "remedies",
    "retention",
    "tests",
)
# Authority and byte-verification owners; not arbitrary operation modules.
SECURITY_EDGES = frozenset(
    {
        "control/src/vonk_control/auth.py",
        "control/src/vonk_control/browser_auth.py",
        "control/src/vonk_control/auth_api.py",
        "control/src/vonk_control/agent_api/authority.py",
    }
)


@cache
def security_refusals() -> frozenset[str]:
    """Derive refusal subclasses, including imported aliases, from current code."""
    bases: dict[str, set[str]] = {}
    aliases: dict[str, str] = {}
    for directory in (ROOT / "control/src", ROOT / "agent_protocol/src"):
        for path in directory.rglob("*.py"):
            source = path.read_text()
            if "class " not in source:
                continue
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.ClassDef):
                    bases.setdefault(node.name, set()).update(
                        ast.unparse(base).split(".")[-1] for base in node.bases
                    )
                elif isinstance(node, ast.ImportFrom):
                    aliases.update(
                        (alias.asname, alias.name)
                        for alias in node.names
                        if alias.asname
                    )
    names = {"SecurityRefusalError", "PermissionDenied"}
    for _ in (*bases, *aliases):
        previous = names.copy()
        names.update(alias for alias, owner in aliases.items() if owner in names)
        names.update(owner for owner, parents in bases.items() if parents & names)
        if names == previous:
            break
    return frozenset(names)


def added_lines(patch: str) -> dict[str, set[int]]:
    """Parse zero-context or contextual hunks, including multiple hunks per file."""
    files: dict[str, set[int]] = {}
    path = ""
    line = 0
    in_hunk = False
    for text in patch.splitlines():
        if text.startswith("diff --git "):
            in_hunk = False
        elif text.startswith("+++ b/") and not in_hunk:
            path = text[6:]
            files.setdefault(path, set())
        elif match := re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", text):
            line = int(match.group(1))
            in_hunk = True
        elif in_hunk and text.startswith("+"):
            files[path].add(line)
            line += 1
        elif in_hunk and text.startswith(" "):
            line += 1
    return files


def is_security_edge(path: str) -> bool:
    return path in SECURITY_EDGES or path.startswith(
        "control/src/vonk_control/security/"
    )


def _enum_member_lines(source: str) -> set[int]:
    """Lines where a contract enum defines its own members (the one allowed spelling)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        bases = {
            base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
            for base in node.bases
        }
        if not any(name.endswith("Enum") for name in bases):
            continue
        for statement in node.body:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)) and isinstance(
                statement.value, ast.Constant
            ):
                lines.update(
                    range(
                        statement.lineno, (statement.end_lineno or statement.lineno) + 1
                    )
                )
    return lines


def check_source(
    path: str,
    source: str,
    added: set[int],
    *,
    modes: Sequence[str] = MODES,
    words: frozenset[str] | None = None,
) -> list[str]:
    """Old findings cannot fail the change; only a reported added line can."""
    if not added or "generated_control" in path or "/generated/" in path:
        return []
    is_test = (
        "/tests/" in path
        or path.startswith("tests/")
        or path.endswith(
            ("/tests.rs", ".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")
        )
    )
    python = path.endswith(".py") or (
        source.startswith("#!") and "python" in source.splitlines()[0]
    )
    hits: list[tuple[int, str]] = []
    if (
        "vocabulary" in modes
        and not is_test
        and (python or path.endswith((".rs", ".ts", ".tsx")))
    ):
        definitions = _enum_member_lines(source) if python else set()
        if path.endswith(".rs"):
            definitions.update(rust_test_lines(source))
        hits.extend(
            (line, "contract literal")
            for line in vocabulary.scan_source(
                source,
                path=path,
                words=vocabulary.contract_words() if words is None else words,
            )
            if line not in definitions
        )
    if python and not is_test:
        if "mapping" in modes:
            hits.extend(
                (site.line, "untyped mapping")
                for site in mappings.scan_source(source, path=path)
            )
        if "identity" in modes:
            hits.extend(
                (site.line, "provenance comparison")
                for site in identity.scan_source(source, path=path)
            )
        if "coordination" in modes:
            hits.extend(
                (site.line, site.kind)
                for site in coordination.scan_source(source, path=path)
            )
        if "security" in modes and not is_security_edge(path):
            tree = ast.parse(source)
            aliases = set(security_refusals())
            aliases.update(
                alias.asname or alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
                for alias in node.names
                if alias.name in aliases
            )
            for _ in tuple(ast.walk(tree)):
                previous = aliases.copy()
                aliases.update(
                    node.name
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ClassDef)
                    and any(
                        ast.unparse(base).split(".")[-1] in aliases
                        for base in node.bases
                    )
                )
                if aliases == previous:
                    break
            hits.extend(
                (node.lineno, "security refusal outside ingress")
                for node in ast.walk(tree)
                if isinstance(node, ast.Raise)
                and node.exc is not None
                and ast.unparse(
                    node.exc.func if isinstance(node.exc, ast.Call) else node.exc
                ).split(".")[-1]
                in aliases
            )
    for mode in ("waits", "reads", "remedies", "tests"):
        if mode not in modes or (is_test != (mode == "tests")):
            continue
        if python:
            sites = principles.scan_source(source, path=path, mode=mode)
        elif mode == "waits" and path.endswith(".rs"):
            sites = principles.scan_rust(source, path=path)
        elif mode == "remedies" and path.endswith(".rs"):
            sites = principles.scan_rust_remedies(source, path=path)
        elif mode == "waits" and path.endswith((".yaml", ".yml")):
            sites = principles.scan_compose_shell(source, path=path)
        elif mode == "waits" and (path.endswith(".sh") or source.startswith("#!")):
            sites = principles.scan_shell(source, path=path)
        else:
            sites = []
        hits.extend((site.line, site.kind) for site in sites)
    if python and not is_test and "retention" in modes:
        hits.extend(
            (site.line, site.kind)
            for site in principles.scan_retention([(path, ast.parse(source))])
        )
    if python:
        tree = ast.parse(source)
        spans = [
            (node.lineno, node.end_lineno or node.lineno)
            for node in ast.walk(tree)
            if isinstance(
                node, (ast.Subscript, ast.Compare, ast.Call, ast.Raise, ast.Constant)
            )
        ]
        hits = [
            (
                next(
                    (
                        changed
                        for changed in sorted(added)
                        if any(
                            start == line and start <= changed <= end
                            for start, end in spans
                        )
                    ),
                    line,
                ),
                kind,
            )
            for line, kind in hits
        ]
    return sorted({f"{path}:{line}: {kind}" for line, kind in hits if line in added})


def main(*, modes: Sequence[str] = MODES) -> int:
    messages = []
    changes = added_lines(sys.stdin.read())
    per_file_modes = tuple(mode for mode in modes if mode != "retention")
    for path, lines in changes.items():
        target = ROOT / path
        if target.is_file() and target.resolve().is_relative_to(ROOT):
            messages.extend(
                check_source(path, target.read_text(), lines, modes=per_file_modes)
            )
    if "retention" in modes and any(
        lines
        and path.endswith(".py")
        and (ROOT / path).is_file()
        and "__tablename__" in (ROOT / path).read_text()
        for path, lines in changes.items()
    ):
        # Retention policy may live in a different, unchanged producer module.
        for site in principles.scan_sites("retention"):
            if site.line in changes.get(site.path, set()):
                messages.append(f"{site.path}:{site.line}: {site.kind}")
    for message in messages:
        print(message, file=sys.stderr)
    return int(bool(messages))


if __name__ == "__main__":
    raise SystemExit(main())
