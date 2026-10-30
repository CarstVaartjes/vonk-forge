"""Count-free guards over an externally supplied unified diff. Never reads Git."""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Sequence
from difflib import SequenceMatcher
from functools import cache
from itertools import pairwise
from pathlib import Path

from . import claim_registry as claims
from . import collection_continuation as collections
from . import content_identity_boundaries as identity
from . import coordination_boundaries as coordination
from . import deterministic_retries as retries
from . import document_read_boundaries as document_reads
from . import lifecycle_reasons as reasons
from . import optimistic_boundaries as optimistic
from . import principle_guards as principles
from . import progress_attempt_boundaries as progress_attempts
from . import request_path_scans
from . import short_wall_clocks as wall_clocks
from . import untyped_mapping_boundaries as mappings
from . import vocabulary_literals as vocabulary
from .recipe_run_write_verdicts import classified_write_lines

ROOT = Path(__file__).resolve().parents[2]


rust_test_lines = principles.rust_test_lines


MODES = (
    "request-scans",
    "vocabulary",
    "mapping",
    "document-reads",
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
    "optimistic",
    "clocks",
    "excerpts",
    "collections",
    "progress",
    "contact-clocks",
    "planner-defects",
    "capacity-gates",
    "start-budgets",
    "listings",
    "movement",
    "exit-causes",
    "nas-space",
    "model-sets",
    "openers",
    "follow-validators",
    "item-counts",
    "event-loops",
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
        # The GitHub LFS link host allowlist and redirect refusal (SSRF edge).
        "control/src/vonk_control/model_cache/github_lfs.py",
        "control/src/vonk_control/browser_auth.py",
        "control/src/vonk_control/auth_api.py",
        "control/src/vonk_control/agent_api/authority.py",
    }
)


# The one owner of how a container's exit cause is spelled and read.
EXIT_CAUSE_OWNER = "rust/crates/vonk-agent-protocol/src/failure_evidence.rs"


@cache
def exit_cause_tokens() -> frozenset[str]:
    """The ``ExitCause`` tokens, read from the enum's own ``token`` mapping."""
    source = (ROOT / EXIT_CAUSE_OWNER).read_text()
    body = source[source.index("pub fn token(self)") :]
    body = body[: body.index("\n    }\n")]
    return frozenset(re.findall(r'Self::\w+ => "([a-z_]+)"', body))


# Controller-observed contact facts have one writer: an upsert in agent_contacts.
CONTACT_WRITERS = frozenset(
    {
        "control/src/vonk_control/agent_contacts.py",
        "control/src/vonk_control/db/node_facts.py",
    }
)
# Facts only the contact row has; ``last_seen_at``, ``protocol_version`` and
# ``management_address`` also name unrelated fields, so for those only a
# receiver named for the contact row counts.
_CONTACT_ONLY = frozenset(
    {
        "contact_certificate_serial",
        "contact_observation_digest",
        "contact_repeated_evidence",
        "management_observed_at",
        "recipe_observation_http_status",
        "recipe_observation_status_at",
    }
)
_CONTACT_SHARED = frozenset({"last_seen_at", "protocol_version", "management_address"})


def contact_writes(source: str) -> list[int]:
    """Lines writing a contact fact outside the one writer module."""
    if "contact" not in source and "AgentNodeContact" not in source:
        return []
    found: set[int] = set()
    tree = ast.parse(source)
    # Names bound to a contact row: ``row = session.get(AgentNodeContact, ...)``.
    rows = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign | ast.AnnAssign)
        and node.value is not None
        and "AgentNodeContact" in ast.unparse(node.value)
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AugAssign | ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Attribute) and (
                target.attr in _CONTACT_ONLY
                or target.attr in _CONTACT_SHARED
                and (
                    "contact" in ast.unparse(target.value).lower()
                    or isinstance(target.value, ast.Name)
                    and target.value.id in rows
                )
            ):
                found.add(target.lineno)
        if isinstance(node, ast.Call):
            name = ast.unparse(node.func).split(".")[-1]
            if name == "AgentNodeContact" or (
                name in {"insert", "update", "delete"}
                and node.args
                and ast.unparse(node.args[0]).split(".")[-1] == "AgentNodeContact"
            ):
                found.add(node.lineno)
    return sorted(found)


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


# Sizes the model or recipe sets (files, digests, artifacts, objects, shards)
# are bounded by bytes (the document or wire budget), never by an item count:
# a 286-file model failed planning on a 256-digest cap.
_MODEL_SIZED_TOKENS = frozenset(
    {
        "file",
        "files",
        "digest",
        "digests",
        "artifact",
        "artifacts",
        "object",
        "objects",
        "shard",
        "shards",
        "path",
        "paths",
    }
)
# Database-range and fleet-size constants are not item counts.
_RANGE_CONSTANTS = frozenset(
    {"MAX_DATABASE_INTEGER", "_MAX_SIGNED_BIGINT", "_MAX_FLEET_NODES"}
)
# A real protocol limit states why on the line: ``# item-count-limit: <why>``.
ITEM_COUNT_PRAGMA = "# item-count-limit:"


def model_sized_count_caps(source: str) -> list[int]:
    """Lines capping a model-sized list (``max_length``) or count (``le``)."""
    lines = source.splitlines()
    found: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        tokens = node.target.id.split("_")
        # A byte size is the bound the owner wants; only counts are refused.
        if "bytes" in tokens:
            continue
        sized = any(token in _MODEL_SIZED_TOKENS for token in tokens)
        counted = (
            len(tokens) > 1
            and tokens[-1] == "count"
            and tokens[-2] in _MODEL_SIZED_TOKENS
        )
        if not (sized or counted):
            continue
        annotation = ast.unparse(node.annotation)
        is_list = bool(re.search(r"\b(?:list|tuple|Sequence)\[", annotation))
        is_int = bool(re.search(r"\bint\b", annotation))
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            for keyword in call.keywords:
                capped = (
                    (is_list and keyword.arg == "max_length")
                    or (
                        is_int
                        and not is_list
                        and counted
                        and keyword.arg in {"le", "lt"}
                    )
                ) and (
                    # A number or a named constant: both are an item count.
                    (
                        isinstance(keyword.value, ast.Name)
                        and keyword.value.id not in _RANGE_CONSTANTS
                    )
                    or (
                        isinstance(keyword.value, ast.Constant)
                        and isinstance(keyword.value.value, int)
                    )
                )
                if capped and ITEM_COUNT_PRAGMA not in lines[keyword.value.lineno - 1]:
                    found.add(keyword.value.lineno)
    return sorted(found)


_MODEL_SIZED_PLURALS = frozenset(
    {"files", "digests", "artifacts", "objects", "shards", "paths"}
)
_MAX_CONSTANT = re.compile(r"^_?MAX_[A-Z0-9_]+$")


def _words(text: str) -> set[str]:
    return {
        part for token in re.split(r"\W+", text.lower()) for part in token.split("_")
    }


def runtime_count_caps(source: str) -> list[int]:
    """Lines comparing ``len(...)`` with a count constant for model-sized things.

    ``len(artifacts) > _MAX_ARTIFACTS`` is the runtime form of a ``max_length``
    cap and refuses a model for its file count; a byte bound is the one to use.
    A cap is a ``MAX_*`` name or a number of 64 or more, and the collection is
    model-sized when its own name says so (``len(files)``) or the ``MAX_*`` name
    does (``len(result) > _MAX_ARTIFACTS``). Small literals (``len(x) < 2``) are
    shape checks, and ``len(path)`` measures a string.
    """

    found: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Compare):
            continue
        if not any(
            isinstance(op, ast.Gt | ast.GtE | ast.Lt | ast.LtE) for op in node.ops
        ):
            continue
        sides = [node.left, *node.comparators]
        for left, right in pairwise(sides):
            for call, other in ((left, right), (right, left)):
                if not (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "len"
                    and call.args
                ):
                    continue
                if (
                    isinstance(other, ast.Name)
                    and _MAX_CONSTANT.match(other.id)
                    and other.id not in _RANGE_CONSTANTS
                ):
                    cap = True
                    named = bool(_words(other.id) & _MODEL_SIZED_TOKENS)
                elif (
                    isinstance(other, ast.Constant)
                    and isinstance(other.value, int)
                    and not isinstance(other.value, bool)
                    and other.value >= 64
                ):
                    cap = True
                    named = False
                else:
                    continue
                collection = bool(
                    _words(ast.unparse(call.args[0])) & _MODEL_SIZED_PLURALS
                )
                if cap and (named or collection):
                    found.add(node.lineno)
    return sorted(found)


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


_CLOCK_NAMES = frozenset({"now", "current", "timestamp"})
_QUERY_CALLS = frozenset({"scalar", "scalars", "execute", "get", "all", "first", "one"})
_STAMP_NAME = re.compile(r"observed(?:_at)?")


def _is_clock(value: ast.AST) -> bool:
    if ast.unparse(value) in _CLOCK_NAMES:
        return True
    if not isinstance(value, ast.Call):
        return False
    function = value.func
    return (
        isinstance(function, ast.Attribute)
        and (
            function.attr == "_clock"
            or (
                function.attr == "now"
                and ast.unparse(function).endswith("datetime.now")
            )
        )
    ) or (isinstance(function, ast.Name) and function.id in {"clock", "_clock"})


#: Sources that read an agent's own reports. Controller-written progress
#: timestamps (also named ``observed_at``) are not agent facts.
_AGENT_FACTS = (
    "NodeInventorySnapshot",
    "NodeTelemetrySample",
    "TelemetrySampleView",
    "InventorySnapshotView",
    "_latest_snapshots",
)
#: The two ingress handlers that judge an agent stamp against the receive
#: clock to accept or refuse a report: (path, function).
_INGRESS = frozenset(
    {
        ("control/src/vonk_control/agent_api/observations.py", "inventory"),
        ("control/src/vonk_control/telemetry.py", "_canonical_sample"),
    }
)


def agent_stamp_freshness(source: str, path: str = "") -> list[int]:
    """Lines that age an agent stamp against a Controller clock, in a source that
    reads agent facts, outside the two ingress handlers."""
    if not any(name in source for name in _AGENT_FACTS):
        return []
    return _agent_stamp_ages(ast.parse(source), path)


def _agent_stamp_ages(tree: ast.Module, path: str = "") -> list[int]:
    """Lines that age an agent's own ``observed_at`` against a Controller clock.

    An agent stamp only orders reports: a Spark whose clock runs slow, or a
    report collected minutes before it was sent, is stale on arrival if it is
    aged from the stamp. Age from ``received_at``. Aliases and
    wrappers (``_utc(x.observed_at)``) are followed within a function.
    """
    found: set[int] = set()
    for scope in ast.walk(tree):
        if not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        stamps: set[str] = set()

        def stamp(value: ast.AST, stamps: set[str] = stamps) -> bool:
            return any(
                isinstance(part, ast.Attribute)
                and part.attr == "observed_at"
                or isinstance(part, ast.Name)
                and (part.id in stamps or _STAMP_NAME.fullmatch(part.id) is not None)
                for part in ast.walk(value)
            )

        def age(value: ast.AST) -> bool:
            return (
                isinstance(value, ast.BinOp)
                and isinstance(value.op, ast.Sub)
                and _is_clock(value.left)
            )

        assigns = sorted(
            (n for n in ast.walk(scope) if isinstance(n, ast.Assign)),
            key=lambda n: n.lineno,
        )
        for node in assigns:
            reads = any(
                isinstance(p, ast.Call)
                and isinstance(p.func, ast.Attribute)
                and p.func.attr in _QUERY_CALLS
                for p in ast.walk(node.value)
            )
            # A row read by ``order_by(X.observed_at)`` is not itself a stamp.
            if (
                stamp(node.value)
                and not reads
                and not any(age(p) for p in ast.walk(node.value))
            ):
                stamps.update(
                    target.id for target in node.targets if isinstance(target, ast.Name)
                )
        for node in ast.walk(scope):
            if isinstance(node, ast.BinOp) and age(node) and stamp(node.right):
                found.add(node.lineno)
            if isinstance(node, ast.Compare):
                sides = (node.left, *node.comparators)
                if any(stamp(side) and not age(side) for side in sides) and any(
                    age(side) for side in sides
                ):
                    found.add(node.lineno)
    # A handler that accepts or refuses a report by its stamp is exempt by
    # location, including when it is nested in a router factory.
    exempt = [
        range(scope.lineno, (scope.end_lineno or scope.lineno) + 1)
        for scope in ast.walk(tree)
        if isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef)
        and (path, scope.name) in _INGRESS
    ]
    return sorted(line for line in found if not any(line in span for span in exempt))


def controller_contact_clock_checks(source: str) -> list[int]:
    """Detect future-contact rejection, not certificate dates.

    This syntax guard tracks local aliases of Controller contact/heartbeat
    timestamps, agent observation stamps and their ages: freshness is the
    Controller's receive time, an agent stamp only orders reports (see
    ``agent_stamp_freshness``). Behaviour tests own the actual clock race.
    """
    tree = ast.parse(source)
    attributes = {"last_seen_at", "received_at", "observed_at"}
    if "ControlProcessHeartbeat" in source:
        attributes.add("completed_at")
    found: set[int] = set()
    filters = {
        id(part)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "where"
        for argument in node.args
        for part in ast.walk(argument)
    }
    for scope in ast.walk(tree):
        if not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        contacts: set[str] = set()
        ages: set[str] = set()

        def contact(value: ast.AST, contacts: set[str] = contacts) -> bool:
            return any(
                isinstance(part, ast.Attribute)
                and part.attr in attributes
                or isinstance(part, ast.Name)
                and part.id in contacts
                for part in ast.walk(value)
            )

        def age(value: ast.AST, ages: set[str] = ages) -> bool:
            return (
                isinstance(value, ast.Name)
                and value.id in ages
                or isinstance(value, ast.BinOp)
                and isinstance(value.op, ast.Sub)
                and contact(value.right)
            )

        def clock(value: ast.AST) -> bool:
            return ast.unparse(value) in {"now", "current", "timestamp"}

        def zero(value: ast.AST) -> bool:
            return ast.unparse(value) in {"0", "0.0", "timedelta(0)"}

        for node in ast.walk(scope):
            if isinstance(node, ast.Assign):
                names = {
                    target.id for target in node.targets if isinstance(target, ast.Name)
                }
                if age(node.value):
                    ages.update(names)
                elif contact(node.value):
                    contacts.update(names)
            if not isinstance(node, ast.Compare):
                continue
            for left, op, right in zip(
                (node.left, *node.comparators), node.ops, node.comparators, strict=False
            ):
                if (
                    contact(left)
                    and clock(right)
                    and (
                        isinstance(op, ast.Gt | ast.GtE)
                        or id(node) in filters
                        and isinstance(op, ast.Lt | ast.LtE)
                    )
                    or clock(left)
                    and contact(right)
                    and isinstance(op, ast.Lt | ast.LtE)
                    or age(left)
                    and zero(right)
                    and isinstance(op, ast.Lt | ast.LtE)
                    or zero(left)
                    and age(right)
                    and isinstance(op, ast.Lt | ast.LtE)
                ):
                    found.add(node.lineno)
    found.update(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "LAST_SEEN_IN_FUTURE"
    )
    return sorted(found)


# A start budget is the span the Controller accepted when it queued the start
# (``settings.DISTRIBUTED_START_TIMEOUT_SECONDS``).  Deriving it again from a
# stored deadline minus the row's ``created_at`` silently shrinks or grows it
# once the deadline was anchored at dispatch (#1896).  Reviewed sites only.
START_BUDGET_VERDICTS: dict[tuple[str, str], str] = {
    (
        "control/src/vonk_control/agent_jobs/predicates.py",
        "_anchor_start_budget",
    ): "the anchoring itself: reads the queue-time budget exactly once, before "
    "the deadline moves, and stamps start_anchored_at so it never re-derives",
    (
        "control/src/vonk_control/run_switch_operations/phase_dispatch.py",
        "_execute_checkpoint",
    ): "display only (startup_budget_seconds); overstates by the queue wait "
    "once anchored, follow-up",
}
_START_STAMPS = ("start_deadline", "start_anchored_at")


def start_budget_derivations(source: str) -> list[tuple[int, str]]:
    """``start_deadline``/``start_anchored_at`` minus ``created_at``, per function.

    Reads through attributes, string subscripts, helper calls such as
    ``_aware(...)`` and local aliases.  Returns (line, enclosing function).
    """
    tree = ast.parse(source)
    found: dict[int, str] = {}
    for scope in ast.walk(tree):
        if not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        stamps: set[str] = set()
        creations: set[str] = set()

        def reads(value: ast.AST, words: tuple[str, ...], aliases: set[str]) -> bool:
            for part in ast.walk(value):
                name = (
                    part.id
                    if isinstance(part, ast.Name)
                    else part.attr
                    if isinstance(part, ast.Attribute)
                    else part.value
                    if isinstance(part, ast.Constant) and isinstance(part.value, str)
                    else None
                )
                if name is not None and (
                    name in aliases or any(word in name for word in words)
                ):
                    return True
            return False

        for _ in range(2):
            for node in ast.walk(scope):
                if isinstance(node, ast.Assign | ast.AnnAssign) and node.value:
                    targets = (
                        node.targets if isinstance(node, ast.Assign) else [node.target]
                    )
                    names = {t.id for t in targets if isinstance(t, ast.Name)}
                    if reads(node.value, _START_STAMPS, stamps):
                        stamps.update(names)
                    elif reads(node.value, ("created_at",), creations):
                        creations.update(names)
        for node in ast.walk(scope):
            if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub)):
                continue
            sides = (node.left, node.right)
            if any(
                reads(one, _START_STAMPS, stamps)
                and reads(other, ("created_at",), creations)
                for one, other in (sides, sides[::-1])
            ):
                found[node.lineno] = scope.name
    return sorted(found.items())


# A provider is contacted only through ``download.py:_open_provider``, which
# holds back a provider under its cooldown and narrows the parallel streams on
# its rate limit. A direct opener call bypassed both (the Hugging Face
# upstream-revision check, deleted with #1925's review).
_PROVIDER_OPENERS = frozenset(
    {"_open_http_response", "_open_github_release_asset", "_open_github_lfs_object"}
)
_OPENER_OWNERS = {
    "control/src/vonk_control/model_cache/providers.py": None,
    "control/src/vonk_control/model_cache/download.py": "_open_provider",
}

# The anonymous GitHub request primitive skips the cooldown and the stream
# throttle too: only the two GitHub provider modules, whose openers run under
# ``_open_provider``, may call it.
_ANONYMOUS_SEND = "_send_anonymous_github_request"
_ANONYMOUS_SEND_MODULES = frozenset(
    {
        "control/src/vonk_control/model_cache/github.py",
        "control/src/vonk_control/model_cache/github_lfs.py",
    }
)


def provider_opener_calls(source: str) -> list[tuple[int, str | None]]:
    """Calls of a provider opener or ``provider_for(...).open(``, with the
    enclosing function's name (None at module level)."""

    found: dict[int, str | None] = {}

    def visit(node: ast.AST, function: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            scope = (
                child.name
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
                else function
            )
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                owner = child.func.value
                if child.func.attr in _PROVIDER_OPENERS or (
                    child.func.attr == "open"
                    and (
                        (
                            isinstance(owner, ast.Call)
                            and isinstance(owner.func, ast.Name)
                            and owner.func.id == "provider_for"
                        )
                        or (
                            isinstance(owner, ast.Subscript)
                            and isinstance(owner.value, ast.Name)
                            and owner.value.id == "PROVIDERS"
                        )
                    )
                ):
                    found[child.lineno] = scope
            visit(child, scope)

    visit(ast.parse(source), None)
    return sorted(found.items())


# Movement is forward-only: a stall's marks keep, per child phase, the highest
# counters and the phases seen, so a copy retried from zero or a phase
# flapping back is not progress (#1899).  A comparison of one progress field
# against the previous sample's ("phase differs", "value higher than before")
# is the pairwise shape that called those movement.  Reviewed sites only.
_MOVEMENT_FIELDS = frozenset(
    {
        "phase",
        "completed_bytes",
        "completed_items",
        "activity_sequence",
        "bytes",
        "last_progress_at",
    }
)
MOVEMENT_VERDICTS: dict[tuple[str, str], str] = {
    (
        "control/src/vonk_control/stall_window.py",
        "progress_advanced",
    ): "agent operation progress within one attempt: forward-only marks need a "
    "per-attempt store (an OperationProgress protocol field); owner proposal",
}


def pairwise_movement_checks(source: str) -> list[tuple[int, str]]:
    """``a.field != b.field`` or ``a.field > b.field`` on a progress field.

    Reads through ``x or 0`` defaults.  Returns (line, enclosing function).
    """

    def field(side: ast.expr) -> tuple[str, str] | None:
        if isinstance(side, ast.BoolOp) and side.values:
            side = side.values[0]
        if isinstance(side, ast.Attribute) and side.attr in _MOVEMENT_FIELDS:
            return side.attr, ast.unparse(side.value)
        return None

    found: dict[int, str] = {}
    for scope in ast.walk(ast.parse(source)):
        if not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for node in ast.walk(scope):
            if not isinstance(node, ast.Compare):
                continue
            sides = [node.left, *node.comparators]
            for op, left, right in zip(node.ops, sides[:-1], sides[1:], strict=True):
                if not isinstance(op, ast.NotEq | ast.Gt | ast.Lt | ast.GtE | ast.LtE):
                    continue
                one, other = field(left), field(right)
                if one and other and one[0] == other[0] and one[1] != other[1]:
                    found.setdefault(node.lineno, scope.name)
    return sorted(found.items())


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
        "exit-causes" in modes
        and not is_test
        and path.endswith((".rs", ".py"))
        and path != EXIT_CAUSE_OWNER
    ):
        # P10: a container's exit cause is decided through ``ExitCause``
        # (``of_summary``/``from_token``/``token``), never by matching its
        # ``exit_cause=`` text or spelling a cause token: five such literal
        # parses let a job's host-memory kill report an output breach (#1906).
        test_lines = rust_test_lines(source) if path.endswith(".rs") else set()
        spelled = re.compile(
            r"exit_cause="
            r"|[\"'](?:" + "|".join(sorted(exit_cause_tokens())) + r")[\"']"
        )
        noexit_hits.extend(
            f"{path}:{line}: exit cause spelled outside ExitCause"
            for line, text in enumerate(source.splitlines(), 1)
            if line in added and line not in test_lines and spelled.search(text)
        )
    if "listings" in modes and not is_test:
        # GitHub lists a run's artifacts 30 to a page; a reader of one page
        # silently misses the rest (shard timings, #1900). Read every page or
        # ask for the one artifact by name; a download by id is not a listing.
        # A line that calls a function of this file whose own gh argv pages
        # reads every page. The window is four lines before (workflow
        # continuations) and one after (a split URL).
        rows = source.splitlines()
        paging = re.compile(
            r"[\"']gh[\"'],\s*[\"']api[\"'],\s*(?:[\"'][^\"']*[\"'],\s*)*[\"']--paginate[\"']"
        )
        wrappers = {
            name
            for name, body in re.findall(
                r"^def (\w+)\(([\s\S]*?)(?=^\S|\Z)", source, re.MULTILINE
            )
            if paging.search(body)
        }
        for line, text in enumerate(rows, 1):
            window = " ".join(rows[max(line - 5, 0) : line + 1])
            if (
                line in added
                and re.search(
                    r"actions/(?:runs/[^\s\"']*/)?artifacts(?!\w|/[\w{$])", text
                )
                and not re.search(r"--paginate|\bpage=|[?&]name=", window)
                and not (
                    any(re.search(rf"\b{name}\(", text) for name in wrappers)
                    and not re.search(r"[\"']gh[\"'],", window)
                )
            ):
                noexit_hits.append(f"{path}:{line}: artifact listing reads one page")
    if "excerpts" in modes and not is_test and path.endswith(".py"):
        # A process's output is bounded from its end, where a failure is
        # printed, and a retained diagnostic tail is read from both streams: a
        # log, excerpt or stream value cut from the front, or a tail's
        # ``stderr.text or stdout.text`` choosing one stream by presence, kept a
        # build's echoed command instead of its error (#1905). A raw tool's
        # ``stderr or stdout`` (skopeo prints its errors on stderr) is not a
        # retained tail and is not matched.
        name = r"\b(?:\w+_)?(?:log|excerpt|stderr|stdout)(?:_\w+)?\b"
        spelled = re.compile(
            name + r"(?:\.text)?\)?\[\s*:\s*[\w.]+\s*\]"
            r"|\bstderr\.text\s+or\s+[\w.()]*stdout\.text"
            r"|\bstdout\.text\s+or\s+[\w.()]*stderr\.text"
        )
        noexit_hits.extend(
            f"{path}:{line}: output bounded from its front or one stream preferred"
            for line, text in enumerate(source.splitlines(), 1)
            if line in added and spelled.search(text)
        )
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
    if "optimistic" in modes and python and path.startswith("control/src/"):
        hits.extend((line, optimistic.KIND) for line in optimistic.scan_source(source))
        if not is_security_edge(path) and not path.startswith(
            "control/src/vonk_control/enrollment/"
        ):
            hits.extend(
                (line, optimistic.NODE_KIND)
                for line in optimistic.scan_agent_node_locks(source)
            )
        if path not in CONTACT_WRITERS:
            hits.extend(
                (line, "contact fact written outside agent_contacts (one upsert path)")
                for line in contact_writes(source)
            )
        classified = classified_write_lines(path, source)
        hits.extend(
            (line, optimistic.WRITE_KIND)
            for line in optimistic.scan_unwrapped_recipe_run_writes(source)
            if line not in classified
        )
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
        if "document-reads" in modes and path.startswith(
            tuple(
                root.relative_to(document_reads.REPO_ROOT).as_posix() + "/"
                for root in document_reads.SOURCE_ROOTS
            )
        ):
            hits.extend(
                (
                    site.line,
                    (
                        "raw document read; use vonk_forge_contracts "
                        "read_recipe/read_model/read_*_display"
                    ),
                )
                for site in document_reads.scan_source(source, path=path)
            )
        if "identity" in modes:
            ingress = path.startswith(
                (
                    "control/src/vonk_control/catalog_sync",
                    "control/src/vonk_control/recipe_packages/",
                )
            ) or path in {
                "control/src/vonk_control/catalog_entities.py",
                "control/src/vonk_control/catalog_service.py",
            }
            if not ingress:
                tree = ast.parse(source)
                aliases = {"document_sha256"} | {
                    alias.asname or alias.name
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom)
                    for alias in node.names
                    if alias.name == "document_sha256"
                }
                hits.extend(
                    (node.lineno, "document digest outside ingress")
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and (
                        (isinstance(node.func, ast.Name) and node.func.id in aliases)
                        or (
                            isinstance(node.func, ast.Attribute)
                            and node.func.attr == "document_sha256"
                        )
                    )
                )
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
    if (
        python
        and not is_test
        and "contact-clocks" in modes
        and path.startswith("control/src/")
    ):
        hits.extend(
            (line, "Controller contact rejected for negative clock age")
            for line in controller_contact_clock_checks(source)
        )
    if (
        python
        and not is_test
        and "contact-clocks" in modes
        and path.startswith("control/src/")
    ):
        hits.extend(
            (line, "agent stamp aged against the Controller clock; use received_at")
            for line in agent_stamp_freshness(source, path)
        )
    if python and not is_test and "planner-defects" in modes:
        hits.extend(
            (line, "planner contract defect hidden by broad wait")
            for line in planner_defect_waits(source)
        )
    if (
        python
        and not is_test
        and "capacity-gates" in modes
        and path.startswith("control/src/")
    ):
        hits.extend(
            (site.line, site.kind)
            for site in principles.capacity_gates_before_claims(source, path=path)
            if (site.path, site.function) not in principles.CAPACITY_GATE_VERDICTS
        )
    if (
        python
        and not is_test
        and "start-budgets" in modes
        and path.startswith("control/src/")
    ):
        hits.extend(
            (line, "start budget derived from a stored start timestamp")
            for line, function in start_budget_derivations(source)
            if (path, function) not in START_BUDGET_VERDICTS
        )
    if (
        python
        and not is_test
        and "movement" in modes
        and path.startswith("control/src/")
    ):
        hits.extend(
            (line, "pairwise progress movement (compare against forward marks)")
            for line, function in pairwise_movement_checks(source)
            if (path, function) not in MOVEMENT_VERDICTS
        )
    if (
        python
        and not is_test
        and "nas-space" in modes
        and path.startswith("control/src/vonk_control/model_cache/")
        and path != "control/src/vonk_control/model_cache/storage.py"
    ):
        # NAS space is decided once, in storage.py's _storage_fits, next to what
        # the running transfers still write. A comparison with the free disk
        # alone let parallel downloads overcommit the volume (#1910).
        hits.extend(
            (line, "NAS space read outside storage.py; use _storage_fits")
            for line, text in enumerate(source.splitlines(), 1)
            if re.search(r"\bfree_bytes\(\)|\bdisk_usage\(", text)
        )
    if (
        python
        and not is_test
        and "model-sets" in modes
        and re.fullmatch(
            r"control/src/vonk_control/model_cache/(?:removal_\w+|fleet_removal)\.py",
            path,
        )
    ):
        # Removal takes a model's sets from the one resolver the Library shares
        # (model_cache/lineage.py). Choosing them by the first requester's
        # digest missed every set downloaded under an earlier revision, and
        # removals "succeeded" having removed nothing (#1911).
        hits.extend(
            (line, "model sets chosen by first-requester digest; use lineage.py")
            for line, text in enumerate(source.splitlines(), 1)
            if "ModelCacheSet.model_content_sha256" in text
        )
    if (
        python
        and not is_test
        and "item-counts" in modes
        and path.startswith(("control/src/", "agent_protocol/src/"))
    ):
        hits.extend(
            (line, "item-count cap on a model-sized list; bound by bytes instead")
            for line in sorted(
                {*model_sized_count_caps(source), *runtime_count_caps(source)}
            )
        )
    if (
        python
        and not is_test
        and "openers" in modes
        and path.startswith("control/src/")
    ):
        owner = _OPENER_OWNERS.get(path, "")
        hits.extend(
            (line, "provider opened outside download.py:_open_provider")
            for line, function in provider_opener_calls(source)
            if path not in _OPENER_OWNERS or (owner is not None and function != owner)
        )
    if (
        python
        and not is_test
        and "openers" in modes
        and path.startswith("control/src/")
        and path not in _ANONYMOUS_SEND_MODULES
    ):
        hits.extend(
            (
                node.lineno,
                "anonymous GitHub request outside the GitHub provider modules",
            )
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == _ANONYMOUS_SEND
        )
    if (
        python
        and not is_test
        and "follow-validators" in modes
        and path.startswith("src/cluster_profiles/controller_cli/")
    ):
        # A follow validator checks the job's identity, never how many
        # operations run: every Spark upgrades at once, and a count assertion
        # made `fleet upgrade --all` end TIMED_OUT while the upgrades ran (N1).
        hits.extend(
            (line, "follow validator asserts an operation count; check identity only")
            for line, text in enumerate(source.splitlines(), 1)
            if re.search(
                r"\blen\((?:active|operations|queued|running)\w*\)\s*(?:>|>=)"
                r"|\[[\"']running[\"']\]\s*(?:>|>=)\s*\d",
                text,
            )
        )
    if (
        python
        and not is_test
        and "event-loops" in modes
        and path.startswith("src/cluster_profiles/")
        and path != "src/cluster_profiles/control_transport.py"
    ):
        # Every event-loop entry goes through HTTPSResponse._run, which keeps
        # a Ctrl-C a Ctrl-C (exit 130) however the runner's SIGINT race lands.
        hits.extend(
            (line, "event loop entered outside control_transport.py:HTTPSResponse._run")
            for line, text in enumerate(source.splitlines(), 1)
            if re.search(
                r"\b(?:asyncio\.run|asyncio\.Runner|uvloop\.run|anyio\.run)\(|\.run_until_complete\(",
                text.split("#", 1)[0],
            )
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
                node,
                (
                    ast.Subscript,
                    ast.Compare,
                    ast.Call,
                    ast.Raise,
                    ast.Constant,
                    ast.ExceptHandler,
                ),
            )
        ]
        hits = [
            (
                line
                if kind in {optimistic.KIND, optimistic.WRITE_KIND}
                else next(
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


def planner_defect_waits(source: str) -> list[int]:
    """Planner wrappers must propagate, classify or re-plan contract defects."""
    planners = {
        "_assessment_provider",
        "inspect_request",
        "recheck_resources_in_session",
        "_preview_run",
        "_resource_fits",
        "preview_run",
        "_replan_pending_application",
    }
    # Other services (upgrades, cluster mappings) have their own ``preview``:
    # these generic names are the planner's only on the planner's own receiver.
    own_planners = {"preview", "assess"}

    def planner_call(part: ast.Call) -> bool:
        if isinstance(part.func, ast.Name):
            return part.func.id in planners
        return isinstance(part.func, ast.Attribute) and (
            part.func.attr in planners
            or (
                part.func.attr in own_planners
                and isinstance(part.func.value, ast.Name)
                and part.func.value.id in {"self", "service"}
            )
        )

    sites = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Try):
            continue
        calls = [
            part
            for statement in node.body
            for part in ast.walk(statement)
            if isinstance(part, ast.Call) and planner_call(part)
        ]
        if not calls:
            continue
        protected = False
        for handler in node.handlers:
            caught = (
                {
                    part.id if isinstance(part, ast.Name) else part.attr
                    for part in ast.walk(handler.type)
                    if isinstance(part, (ast.Name, ast.Attribute))
                }
                if handler.type is not None
                else set()
            )
            if caught == {"ValidationError"}:
                protected = any(
                    isinstance(part, ast.Raise)
                    and (
                        part.exc is None
                        or (
                            isinstance(part.exc, ast.Call)
                            and isinstance(part.exc.func, ast.Name)
                            and part.exc.func.id == "FleetProfileRecheckContractDefect"
                        )
                    )
                    or (
                        isinstance(part, ast.Call)
                        and isinstance(part.func, ast.Name)
                        and part.func.id == "planner_contract_defect"
                    )
                    for part in ast.walk(handler)
                )
            if (
                handler.type is None
                or caught & {"ValueError", "Exception", "BaseException"}
            ) and not protected:
                sites.extend([handler.lineno, *(call.lineno for call in calls)])
    return sites


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
