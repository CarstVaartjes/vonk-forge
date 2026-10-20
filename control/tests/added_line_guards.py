"""Count-free guards over an externally supplied unified diff. Never reads Git."""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Sequence
from difflib import SequenceMatcher
from functools import cache
from pathlib import Path

from . import claim_registry as claims
from . import collection_continuation as collections
from . import content_identity_boundaries as identity
from . import coordination_boundaries as coordination
from . import deterministic_retries as retries
from . import lifecycle_reasons as reasons
from . import principle_guards as principles
from . import request_path_scans
from . import progress_attempt_boundaries as progress_attempts
from . import short_wall_clocks as wall_clocks
from . import untyped_mapping_boundaries as mappings
from . import vocabulary_literals as vocabulary

ROOT = Path(__file__).resolve().parents[2]


rust_test_lines = principles.rust_test_lines


MODES = (
    "request-scans",
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
    "noexit",
    "causes",
    "retries",
    "repeats",
    "residues",
    "claims",
    "reasons",
    "boundaries",
    "locks",
    "clocks",
    "collections",
    "progress",
)
# The agent's messages a Controller test may send only as the stand-in pinned
# to the agent's shared vector; built by hand they are the Controller's idea of
# the agent, which agrees with the Controller whatever the agent sends (#1579).
AGENT_REQUESTS = "control/tests/agent_grant_requests.py"
AGENT_REQUEST_BUILDERS = frozenset({"HostRuntimeRequest", "stop_payload_from_job_run"})
# Authority and byte-verification owners; not arbitrary operation modules.
SECURITY_EDGES = frozenset(
    {
        "control/src/vonk_control/auth.py",
        # Canonical provider HTTP authentication and model-byte ingress verification.
        "control/src/vonk_control/model_cache/http.py",
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
    """Parse hunks, carrying unchanged relocated blocks without duplicating debt.

    A deleted block may account for one identical added block in the same
    language. Copies and changed lines remain additions. Matching preserves
    context and permits a uniform extraction indentation shift, so a common isolated line cannot hide new work.
    All evidence comes from the supplied patch, never repository history.
    """
    files: dict[str, set[int]] = {}
    additions: list[tuple[str, list[tuple[int, str]]]] = []
    deletions: list[tuple[str, list[str]]] = []
    path = ""
    old_path = ""
    line = 0
    in_hunk = False
    added: list[tuple[int, str]] = []
    deleted: list[str] = []

    def finish() -> None:
        if added:
            additions.append((path, added.copy()))
            added.clear()
        if deleted:
            deletions.append((old_path, deleted.copy()))
            deleted.clear()

    for text in patch.splitlines():
        if text.startswith("diff --git "):
            finish()
            in_hunk = False
        elif text.startswith("--- a/") and not in_hunk:
            old_path = text[6:]
        elif text.startswith("+++ b/") and not in_hunk:
            path = text[6:]
            files.setdefault(path, set())
        elif match := re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", text):
            finish()
            line = int(match.group(1))
            in_hunk = True
        elif in_hunk and text.startswith("+"):
            files[path].add(line)
            added.append((line, text[1:]))
            line += 1
        elif in_hunk and text.startswith("-"):
            deleted.append(text[1:])
        elif in_hunk and text.startswith(" "):
            finish()
            line += 1
    finish()
    # Hunk boundaries describe the original layout, not the relocated block.
    # Gather each file's changed text so a split module can spend deleted text
    # across hunks. Consumption still permits only one relocated occurrence.
    added_by_file: dict[str, list[tuple[int, str]]] = {}
    deleted_by_file: dict[str, list[str]] = {}
    for target, lines in additions:
        added_by_file.setdefault(target, []).extend(lines)
    for owner, removed in deletions:
        deleted_by_file.setdefault(owner, []).extend(removed)
    for target, lines in added_by_file.items():
        for owner, removed in deleted_by_file.items():
            if Path(owner).suffix != Path(target).suffix:
                continue
            old_lines = [
                (index, text) for index, text in enumerate(removed) if text.strip()
            ]
            new_lines = [
                (index, text) for index, (_, text) in enumerate(lines) if text.strip()
            ]
            matches = SequenceMatcher(
                a=[text.lstrip() for _, text in old_lines],
                b=[text.lstrip() for _, text in new_lines],
                autojunk=False,
            ).get_matching_blocks()
            shifted_blocks: list[tuple[int, int, int]] = []
            for old, new, size in matches:
                start = 0
                previous_shift = None
                for offset in range(size):
                    left = old_lines[old + offset][1]
                    right = new_lines[new + offset][1]
                    shift = (
                        len(left)
                        - len(left.lstrip())
                        - len(right)
                        + len(right.lstrip())
                    )
                    if previous_shift is not None and shift != previous_shift:
                        shifted_blocks.append(
                            (old + start, new + start, offset - start)
                        )
                        start = offset
                    previous_shift = shift
                shifted_blocks.append((old + start, new + start, size - start))
            for old, new, size in shifted_blocks:
                if size < 2:
                    continue
                old_indices = [index for index, _ in old_lines[old : old + size]]
                new_indices = [index for index, _ in new_lines[new : new + size]]
                # Require a block, including its separating blank line, never
                # an isolated vocabulary word. A uniform indentation shift is
                # expected when extracting a function from a dispatch wrapper.
                shifts = {
                    len(removed[left])
                    - len(removed[left].lstrip())
                    - len(lines[right][1])
                    + len(lines[right][1].lstrip())
                    for left, right in zip(old_indices, new_indices, strict=True)
                }
                if len(shifts) != 1 or any(
                    lines[index][0] not in files[target]
                    for index in range(new_indices[0], new_indices[-1] + 1)
                ):
                    continue
                old_end = old_indices[-1] + 1
                new_end = new_indices[-1] + 1
                while old_end < len(removed) and not removed[old_end].strip():
                    old_end += 1
                while new_end < len(lines) and not lines[new_end][1].strip():
                    new_end += 1
                if min(old_end - old_indices[0], new_end - new_indices[0]) < 3:
                    continue
                new_end = (
                    new_indices[-1]
                    + 1
                    + min(
                        old_end - old_indices[-1] - 1,
                        new_end - new_indices[-1] - 1,
                    )
                )
                for offset in range(new_indices[0], new_end):
                    files[target].discard(lines[offset][0])
                for offset in range(old_indices[0], old_end):
                    # A second copy cannot spend the same deleted occurrence.
                    removed[offset] = f"\0consumed:{offset}"
            # Reordered extracted methods need not preserve the old file's
            # sequence alignment. Spend remaining exact blocks independently.
            for _ in range(len(lines)):
                match = SequenceMatcher(
                    a=removed,
                    b=[
                        text if number in files[target] else f"\0added:{number}"
                        for number, text in lines
                    ],
                    autojunk=False,
                ).find_longest_match()
                if match.size < 3:
                    break
                for offset in range(match.size):
                    files[target].discard(lines[match.b + offset][0])
                    removed[match.a + offset] = f"\0consumed:{match.a + offset}"
    return files


# Words of bookkeeping drift: an owner that is unavailable waits, state that
# changed re-plans, evidence that is unreadable is observed again.
BOOKKEEPING_DRIFT = re.compile(
    r"\b(?:changed|unavailable|unreadable|disappeared|superseded|stale"
    r"|inconsistent|ambiguous)\b",
    re.IGNORECASE,
)


def is_security_edge(path: str) -> bool:
    return path in SECURITY_EDGES or path.startswith(
        "control/src/vonk_control/security/"
    )


def _catches_only(handler: ast.ExceptHandler, classes: set[str]) -> bool:
    caught = handler.type
    names = caught.elts if isinstance(caught, ast.Tuple) else [caught]
    return all(
        name is not None and ast.unparse(name).split(".")[-1] in classes
        for name in names
    )


def _is_forbidden_response(node: ast.Call) -> bool:
    return ast.unparse(node.func).split(".")[-1] == "HTTPException" and any(
        keyword.arg == "status_code"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value == 403
        for keyword in node.keywords
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


def _rehashes_stored_row(node: ast.Compare) -> bool:
    """``row.payload_digest`` compared with a fresh ``sha256`` of a document."""
    operands = (node.left, *node.comparators)
    return any(
        isinstance(operand, ast.Attribute) and operand.attr == "payload_digest"
        for operand in operands
    ) and any(
        isinstance(call, ast.Call) and ast.unparse(call.func).endswith("sha256")
        for operand in operands
        for call in ast.walk(operand)
    )


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
    noexit_hits: list[str] = []
    if "noexit" in modes and not is_test:
        test_lines = rust_test_lines(source) if path.endswith(".rs") else set()
        patterns = [
            r"^\s*RestartPreventExitStatus\s*=",
            r"^\s*TimeoutStartSec\s*=\s*(?:0|infinity)\s*$",
            r"^\s*StartLimit(?:IntervalSec|Interval|Burst)\s*=\s*(?!0(?:\s|$))\S+",
        ]
        if path.endswith(".rs"):
            patterns.extend(
                [
                    r"(?:std::)?process::(?:exit|abort)\s*\(",
                    r"ExitCode::(?:from|FAILURE)",
                    r"\b(?:panic!|unreachable!)\s*\(",
                    r"\.expect\s*\(",
                ]
            )
        if path.startswith(("packaging/", "deploy/")):
            patterns.extend(
                [r"\bexit\s+(?:78|\$EX_CONFIG)\b", r"^\s*Restart\s*=\s*no\b"]
            )
        for line, text in enumerate(source.splitlines(), 1):
            if (
                line in added
                and line not in test_lines
                and any(re.search(pattern, text) for pattern in patterns)
            ):
                noexit_hits.append(f"{path}:{line}: permanent process stop")
    if "noexit" in modes and not is_test and path.endswith(".service"):
        restart = re.search(r"^Restart=(.+)$", source, re.MULTILINE)
        oneshot = re.search(r"^Type=oneshot$", source, re.MULTILINE)
        if (restart is not None or not oneshot) and (
            restart is None
            or restart[1] not in {"always", "on-failure"}
            or not re.search(r"^RestartSec=(?!0(?:s)?$).+$", source, re.MULTILINE)
            or not re.search(r"^StartLimitIntervalSec=0$", source, re.MULTILINE)
        ):
            noexit_hits.append(
                f"{path}:{min(added)}: service without bounded perpetual recovery"
            )
    if "causes" in modes and not is_test and path.endswith(".rs"):
        # P6: evidence is never lost. A refusal, a failed send or an error is
        # kept with its cause (in the outcome's evidence, the result spool or
        # the journal), never dropped by `.ok();`, `Err(_) =>` or `let _ =`.
        test_lines = rust_test_lines(source)
        for line, text in enumerate(source.splitlines(), 1):
            if (
                line in added
                and line not in test_lines
                and re.search(
                    r"\btemporary_runtime_observation_failure\s*\(\s*\)"
                    r"|\blet\s+_\s*=\s*(?:error|err|e)\s*;"
                    r"|\.ok\(\)\s*;"
                    r"|\bErr\s*\(\s*_\s*\)\s*=>"
                    r"|\blet\s+_\s*=.*\.(?:send|post|put|report)\w*\s*\(",
                    text,
                )
            ):
                noexit_hits.append(f"{path}:{line}: failure discards its cause")
            # A missing observation is re-observed, never replaced by a
            # literal exit status: an exit is measured or stays unknown. A
            # deliberate helper-ended exit is one documented constant.
            if (
                line in added
                and line not in test_lines
                and re.search(
                    r"\bSome\s*\(\s*124\s*\)"
                    r"|\bexit_code\s*:\s*Some\s*\(\s*-?\d+\s*\)"
                    r"|\bOk\s*\(\s*\(\s*Some\s*\(\s*-?\d+\s*\)",
                    text,
                )
            ):
                noexit_hits.append(f"{path}:{line}: exit code invented, not measured")
    if (
        "locks" in modes
        and not is_test
        and path.endswith(".rs")
        and not path.endswith("/held_lock.rs")
    ):
        # A flock belongs to the open file, which a child forked by any thread
        # shares until exec: released only by closing our descriptor, the lock
        # stays held and the next locker is refused as busy. HeldLock unlocks
        # explicitly on drop.
        test_lines = rust_test_lines(source)
        noexit_hits.extend(
            f"{path}:{line}: raw exclusive flock; take a HeldLock"
            for line, text in enumerate(source.splitlines(), 1)
            if line in added
            and line not in test_lines
            and re.search(
                r"FlockOperation::(?:NonBlocking)?LockExclusive|\bLOCK_EX\b", text
            )
        )
    python = path.endswith(".py") or (
        source.startswith("#!") and "python" in source.splitlines()[0]
    )
    hits: list[tuple[int, str]] = []
    if (
        "boundaries" in modes
        and python
        and path.startswith("control/tests/")
        and path != AGENT_REQUESTS
    ):
        hits.extend(
            (node.lineno, f"agent request built by hand; send {AGENT_REQUESTS}'s")
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and ast.unparse(node.func).split(".")[-1] in AGENT_REQUEST_BUILDERS
        )
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
    if (
        "retries" in modes
        and not is_test
        and path.startswith("rust/crates/vonk-agent/src/")
        and path.endswith(".rs")
    ):
        hits.extend(
            retries.scan_rust_dispositions(
                source, path=path, test_lines=rust_test_lines(source)
            )
        )
    if "identity" in modes and path.endswith(".rs") and not is_test:
        test_lines = rust_test_lines(source)
        hits.extend(
            (site.line, "provenance comparison")
            for site in identity.scan_source(source, path=path)
            if site.line not in test_lines
        )
    if python and not is_test:
        if "request-scans" in modes:
            hits.extend(
                (line, "unbounded request catalog/history scan")
                for line in request_path_scans.scan_tree(ast.parse(source), path=path)
            )
        if "progress" in modes:
            hits.extend(
                (line, "monotonic progress without attempt key")
                for line in progress_attempts.scan_source(source)
            )
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
            # No internal hashing: a row's digest is recorded once, when it is
            # written. Re-hashing the Controller's own row to decide what it is
            # refuses it whenever a newer Controller reads it back differently.
            hits.extend(
                (node.lineno, "stored row re-hashed")
                for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.Compare) and _rehashes_stored_row(node)
            )
        if "coordination" in modes:
            hits.extend(
                (site.line, site.kind)
                for site in coordination.scan_source(source, path=path)
            )
        if "retries" in modes and path.startswith("control/src/"):
            hits.extend(
                (line, "deterministic error retried")
                for line in retries.scan_source(source)
            )
            hits.extend(retries.scan_dispositions(source, path=path))
            hits.extend(
                (line, "count ends standing intent")
                for line in retries.scan_count_endings(source)
            )
            hits.extend(
                (line, "durable retry can exhaust")
                for line in retries.scan_recovery_policy_users(source, path=path)
            )
        if "repeats" in modes and path.startswith("control/src/"):
            hits.extend(
                (line, "retry scheduled without note_repeat")
                for line in retries.scan_repeats(source)
            )
        if "residues" in modes and path.startswith("control/src/"):
            # A stored column read answers with a Residue for a damaged document;
            # handing that answer straight to a parser or consumer makes the
            # unknown a crash of whoever called it (core rule 5).
            hits.extend(
                (node.lineno, "stored read used without its residue")
                for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.Call)
                and ast.unparse(node.func) != "isinstance"
                and any(
                    isinstance(argument, ast.Call)
                    and ast.unparse(argument.func).split(".")[-1]
                    in {"read_row_column", "read_column"}
                    for argument in (
                        *node.args,
                        *(keyword.value for keyword in node.keywords),
                    )
                )
            )
        if "reasons" in modes and path.startswith("control/src/"):
            # Lifecycle reasons are contract enum values, never prose.
            hits.extend(
                (line, "free-text lifecycle reason")
                for line in reasons.scan_source(source, path=path)
            )
        if "claims" in modes and path.startswith("control/src/"):
            hits.extend(
                (line, "in-flight-by-stored-state")
                for line in claims.in_flight_by_stored_state(source, path=path)
            )
        if "security" in modes:
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
            refusals = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Raise)
                and node.exc is not None
                and ast.unparse(
                    node.exc.func if isinstance(node.exc, ast.Call) else node.exc
                ).split(".")[-1]
                in aliases
            ]
            if not is_security_edge(path):
                hits.extend(
                    (node.lineno, "security refusal outside ingress")
                    for node in refusals
                )
            # Bookkeeping drift is a wait, a conflict or an unknown, never a
            # terminal security refusal, even at a declared edge.
            hits.extend(
                (node.lineno, "security refusal for bookkeeping drift")
                for node in refusals
                if isinstance(node.exc, ast.Call)
                and any(
                    isinstance(part, ast.Constant)
                    and isinstance(part.value, str)
                    and BOOKKEEPING_DRIFT.search(part.value)
                    for argument in node.exc.args
                    for part in ast.walk(argument)
                )
            )
            # A 403 is a terminal security answer: an edge may turn only a
            # reviewed refusal class into one, never a broad error that also
            # carries drift (the grant route's 403 for stale authority).
            if is_security_edge(path):
                hits.extend(
                    (node.lineno, "403 for an error that is not a security refusal")
                    for handler in ast.walk(tree)
                    if isinstance(handler, ast.ExceptHandler)
                    and not _catches_only(handler, aliases)
                    for node in ast.walk(handler)
                    if isinstance(node, ast.Call) and _is_forbidden_response(node)
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
    if not is_test and "collections" in modes:
        hits.extend(
            (line, collections.KIND) for line in collections.scan_source(path, source)
        )
    if python and is_test and "clocks" in modes:
        hits.extend(
            (line, wall_clocks.KIND) for line in wall_clocks.scan_source(source)
        )
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
    return sorted(
        set(noexit_hits)
        | {f"{path}:{line}: {kind}" for line, kind in hits if line in added}
    )


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
