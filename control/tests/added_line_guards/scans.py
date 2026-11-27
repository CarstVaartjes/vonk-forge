from __future__ import annotations

import ast
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from functools import cache
from itertools import pairwise
from pathlib import Path
from typing import TypeGuard

from .. import principle_guards as principles

ROOT = Path(__file__).resolve().parents[3]


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
    "plan-validation",
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
    "fixed-reserves",
    "job-limits",
    "model-sets",
    "openers",
    "follow-validators",
    "collection-readers",
    "repeated-parse",
    "legacy",
    "item-counts",
    "event-loops",
    "trap-waits",
    "plan-digests",
    "background-retries",
    "tick-polls",
    "future-workers",
    "locked-io",
    "scratch-names",
    "typed-string-lengths",
    "capacity-refusals",
    "agent-422",
    "docker-listings",
    "lifecycle-sets",
    "alias-rules",
    "intent-ordinal",
    "savepoints",
    "durable-revocations",
    "sync-in-async",
    "git-free-tests",
    "loopback-bind",
    "job-owner-payload",
    "fixture-closures",
    "contract-constants",
    "rank-installed",
    "prebuilt-key",
    "live-reservation",
    "newest-snapshot",
    "retry-dispositions",
    "head-digests",
    "set-first-requester",
    "printed-output",
    "shared-id-patterns",
    "site-identifiers",
    "maintainershim",
)


# An agent-facing 422 tells the Spark these exact bytes can never be accepted
# and drops them (the observation outbox discards on it). Only a verdict on
# the request's own bytes may say it; a clock, a race or an owner still
# settling is a wait (temporary_http_answer or UnknownOutcomeError).
AGENT_422_VERDICT_MARKER = "422-verdict:"


# The only code that may name DEMAND_EXCEEDS_CAPACITY: sibling starts that
# cannot share one Spark, and the mapping between the resource and run-switch
# vocabularies. A disk allocation larger than its device is the one other site,
# marked on its own line.
CAPACITY_REFUSAL_SITES = frozenset(
    {
        "control/src/vonk_control/fleet_profiles/colocation.py",
        "control/src/vonk_control/fleet_profiles/dependencies.py",
    }
)


CAPACITY_REFUSAL_MARKER = "capacity-refusal: disk"


# A name that reserves volume space (not "reserved": space a run holds) bound to
# a number or a constant, anywhere on the line, annotated or not.
FIXED_RESERVE = re.compile(
    r"(?i)\b(_?\w*reserve(?!d)\w*_bytes)\b\s*(?::[^=]+)?=\s*(?!Field\()(?-i:[\dA-Z(])"
)


# Artifact-job limits have one owner, `vonk_agent_protocol/job_limits.py`;
# a second assignment restates a number that must stay equal to it.
JOB_LIMIT_ASSIGNMENT = re.compile(
    r"^\s*(?:MAX_(?:INPUT|OUTPUT)_[A-Z_]+|MAX_TIMEOUT_SECONDS)\s*(?::[^=]+)?="
)


JOB_LIMITS_OWNER = "agent_protocol/src/vonk_agent_protocol/job_limits.py"


# A display title feeding a normalised name builds identity from display text;
# a title edit then changes behaviour with no new revision (#2009).
TITLE_IDENTITY = re.compile(
    r"(?:\b\w+_title|\.title)\b(?!\()\s*\.(?:lower|casefold|replace)\(|slugify\([^)]*title"
)


# Search filters compare a title with the query; they name nothing.
TITLE_IDENTITY_VERDICTS: dict[str, str] = {
    "control/src/vonk_control/library_projection/views.py": "wanted_search in",
}


# Fixed reserves that guard something other than NAS volume space.
FIXED_RESERVE_VERDICTS: dict[tuple[str, str], str] = {
    (
        "control/src/vonk_control/recipe_builds/common.py",
        "MINIMUM_BUILD_DISK_RESERVE_BYTES",
    ): "Spark build disk floor",
    (
        "control/src/vonk_control/recipe_builds/common.py",
        "MAXIMUM_BUILD_DISK_RESERVE_BYTES",
    ): "Spark build disk cap",
    (
        "control/src/vonk_control/settings.py",
        "STORAGE_EVICTION_RESERVE_FLOOR_BYTES",
    ): "headroom the collector evicts beyond a shortfall, not a gate",
    (
        "control/src/vonk_control/unused_storage_collection/service.py",
        "reserve_floor_bytes",
    ): "the collector's headroom floor, defaulted from that setting",
}


# The only modules that may lock a whole table: one-time schema adoption at
# startup (before services run) and the administrator bootstrap (security;
# request paths read users without a lock).
TABLE_LOCK_OWNERS = frozenset(
    {
        "control/src/vonk_control/exact_integer_adoption.py",
        "control/src/vonk_control/db/node_facts.py",
        "control/src/vonk_control/security/browser_auth.py",
    }
)


# The agent's messages a Controller test may send only as the stand-in pinned
# to the agent's shared vector; built by hand they are the Controller's idea of
# the agent, which agrees with the Controller whatever the agent sends (#1579).
# Whether a rank is installed is one shared predicate, machine_states.rank_installed.
RANK_INSTALLED_COPY = re.compile(r"[!=]=\s*InstallationNodeState\.INSTALLED\b")


RANK_INSTALLED_KIND = (
    "rank INSTALLED test spelled inline; use machine_states.rank_installed"
)


# The Controller trusts the signed catalog's pin for a revision's content
# digest; a prebuilt image is identified by its manifest digest. The build key
# is the recipes CI's handle: nothing in the Controller derives or reads it,
# except catalog ingest, which stores the published field as it is.
PREBUILT_KEY_READ = re.compile(
    r"\bexecutable_build_key\(|\.build_key\b"
    r"|\[\s*[\"']build_key[\"']\s*\]|\.get\(\s*[\"']build_key[\"']"
)


PREBUILT_KEY_INGEST = "control/src/vonk_control/catalog/"


PREBUILT_KEY_KIND = (
    "prebuilt build key derived or read; use the pinned digest the signed "
    "catalog records for the content"
)


# Whether a reservation is live (not history) is one shared predicate,
# machine_states.reservation_live.
LIVE_RESERVATION_COPY = re.compile(
    r"ResourceReservation\.state\s*!=\s*(?:ReservationState\.RELEASED|\"released\")"
)


LIVE_RESERVATION_KIND = (
    "live-reservation test spelled inline; use machine_states.reservation_live"
)


# A node's newest inventory report is one indexed probe per node
# (inventory_repository.newest_snapshot_ids); a window or grouped max over
# node_id reads every retained report (~720 per node over 24 h).
# The scan runs over the whole source so a formatter-wrapped call, a further
# grouping column or a partition_by tuple is found. It stays inside the call: a
# bare partition_by= ends at its argument comma, group_by( and a partition_by
# tuple at the close paren (two nested paren levels are read).
_NEST = r"\((?:[^()]|\([^()]*\))*\)"


NEWEST_SNAPSHOT_SCAN = re.compile(
    r"(?:(?:partition_by\s*=\s*(?:\((?:[^()]|"
    + _NEST
    + r")*?|(?:[^(),]|"
    + _NEST
    + r")*?)"
    r"|group_by\s*\((?:[^()]|" + _NEST + r")*?)"
    r"\bNode(?:Inventory)?Snapshot\.node_id\b)"
    r"|func\.max\(\s*Node(?:Inventory)?Snapshot\.observed_at\s*\)"
)


NEWEST_SNAPSHOT_KIND = (
    "newest report per node by window, grouped max or max(observed_at); "
    "use inventory_repository.newest_snapshot_ids"
)


# A vonkctl test decides on data (status, requests, a typed result, the --json
# document); a positive string literal found in captured stdout/stderr is
# wording, which no test asserts. Absence, count
# and emptiness checks on captured output (a secret never printed, a line shown
# once) are leak and duplication checks and stay allowed.
CAPTURED_TEXT = re.compile(r"\.(?:out|err)\b|\.getvalue\(\)|readouterr\(\)")


PRINTED_OUTPUT_SCOPE = "tests/cluster_profiles/test_"


PRINTED_OUTPUT_WORDING = "tests/cluster_profiles/test_cli_layout.py"


PRINTED_OUTPUT_KIND = (
    "vonkctl test asserts wording in printed output; assert the status, the "
    "requests, a typed result, a view function or the --json document "
    "(vonkctl_json); wording is not tested"
)


def _captured(expr: ast.expr, names: set[str]) -> bool:
    """Captured stdout/stderr text, directly or through a stored name; never JSON."""
    if any(
        isinstance(call, ast.Call) and ast.unparse(call.func) == "json.loads"
        for call in ast.walk(expr)
    ):
        return False
    return bool(CAPTURED_TEXT.search(ast.unparse(expr))) or any(
        isinstance(name, ast.Name) and name.id in names for name in ast.walk(expr)
    )


def printed_wording_lines(source: str) -> list[int]:
    """Lines of `assert "<literal>" in <captured stdout/stderr>` assertions."""
    lines: list[int] = []
    for function in ast.walk(ast.parse(source)):
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        # Names a function binds to captured text: `out = capsys.readouterr().out`.
        names: set[str] = set()
        for assign in ast.walk(function):
            if isinstance(assign, ast.Assign) and _captured(assign.value, set()):
                names.update(
                    target.id
                    for node in assign.targets
                    for target in ast.walk(node)
                    if isinstance(target, ast.Name)
                )
        for node in ast.walk(function):
            if not isinstance(node, ast.Assert):
                continue
            if any(
                isinstance(compare, ast.Compare)
                and isinstance(compare.left, ast.Constant | ast.JoinedStr)
                and (
                    isinstance(compare.left, ast.JoinedStr)
                    or isinstance(compare.left.value, str)
                )
                and any(isinstance(op, ast.In) for op in compare.ops)
                and any(_captured(item, names) for item in compare.comparators)
                for compare in ast.walk(node.test)
            ):
                lines.extend(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return sorted(set(lines))


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
        "control/src/vonk_control/security/browser_auth.py",
        "control/src/vonk_control/auth_api.py",
        "control/src/vonk_control/agent_api/authority.py",
        # The recipe source policy refusal: the source authority edge for builds.
        "control/src/vonk_control/availability_production/production.py",
    }
)


# The files that allocate the next revision_number; no other source names it.
READINESS_READERS = frozenset(
    {"control/src/vonk_control/library_projection/local_state.py"}
)


REVISION_NUMBER_ALLOCATORS = frozenset(
    {
        "control/src/vonk_control/catalog/entities.py",
        "control/src/vonk_control/catalog/service.py",
    }
)


# Readers that keep each document's newest revision_number, so an allocator
# (max + 1) never hands one out twice; they never allocate.
REVISION_NUMBER_KEEPERS = frozenset(
    {"control/src/vonk_control/catalog/revision_collection.py"}
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
                or (
                    target.attr in _CONTACT_SHARED
                    and (
                        "contact" in ast.unparse(target.value).lower()
                        or (
                            isinstance(target.value, ast.Name)
                            and target.value.id in rows
                        )
                    )
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
    token for token in _MODEL_SIZED_TOKENS if token.endswith("s")
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
                elif isinstance(other, ast.Attribute) and re.match(
                    r"(?i)^_?max_", other.attr
                ):
                    # ``len(files) >= limits.max_files``
                    cap = True
                    named = bool(_words(other.attr) & _MODEL_SIZED_TOKENS)
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


_FETCH_CALL = re.compile(r"(?i)request|fetch|page|^(?:get|post|put|patch|send)$")


def _calls_a_fetch(node: ast.AST) -> bool:
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id
            if isinstance(func, ast.Name)
            else ""
        )
        if _FETCH_CALL.search(name):
            return True
    return False


def paging_count_ceilings(source: str) -> list[int]:
    """``for ... in range(... _MAX_* ...)`` loops whose body fetches a page.

    A paged read ends with the peer's last page, bounded by a deadline; a page
    count ceiling truncates or refuses a library that outgrows it. A real
    protocol limit (HTTP redirects) says why with ``# item-count-limit:``.
    """
    lines = source.splitlines()
    found: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.For):
            continue
        loop = node.iter
        if not (
            isinstance(loop, ast.Call)
            and isinstance(loop.func, ast.Name)
            and loop.func.id == "range"
        ):
            continue
        capped = any(
            isinstance(part, ast.Name)
            and _MAX_CONSTANT.match(part.id)
            and part.id not in _RANGE_CONSTANTS
            for argument in loop.args
            for part in ast.walk(argument)
        )
        if (
            capped
            and any(_calls_a_fetch(statement) for statement in node.body)
            and ITEM_COUNT_PRAGMA not in lines[node.lineno - 1]
        ):
            found.add(node.lineno)
    return sorted(found)


_QUOTED = re.compile(r"""(["'])([^"'\n]*)\1""")


def _spelled(lines: Iterable[str], words: frozenset[str]) -> Counter[str]:
    return Counter(
        match.group(2)
        for text in lines
        for match in _QUOTED.finditer(text)
        if match.group(2) in words
    )


def new_literal_lines(
    found: Iterable[int],
    source: str,
    added: set[int],
    removed: Sequence[str],
    words: frozenset[str],
) -> list[int]:
    """A contract word the file's removed lines spelled as often is reflowed, not new.

    A formatter reflow or an edit of an old line re-adds its literals; only a
    word the change spells more often than it removed is new debt, and then
    every added line spelling it is reported. A line whose literal the quote
    pattern cannot see stays reported. Only Rust and TypeScript pass removed
    text: there the scanner reports exactly the quoted words this pattern
    counts. The Python scanner also reports reason codes that are not words
    and skips docstrings and keys the removed text would count, so Python
    passes none and every reported added line stays reported.
    """
    lines = source.splitlines()
    # A line is counted once, however many hits the scanner reported on it.
    reported = sorted({line for line in found if line in added})
    old = _spelled(removed, words)
    new = _spelled((lines[line - 1] for line in reported), words)
    grown = {word for word, count in new.items() if count > old[word]}
    return [
        line
        for line in reported
        if not (spelled := _spelled([lines[line - 1]], words)) or grown & spelled.keys()
    ]


# Words of bookkeeping drift: an owner that is unavailable waits, state that
# changed re-plans, evidence that is unreadable is observed again.
BOOKKEEPING_DRIFT = re.compile(
    r"\b(?:changed|unavailable|unreadable|disappeared|superseded|stale"
    r"|inconsistent|ambiguous)\b",
    re.IGNORECASE,
)


# Modules that moved into security/ without being edges; judged as before.
NOT_SECURITY_EDGES = frozenset(
    {
        "control/src/vonk_control/security/edges.py",
        "control/src/vonk_control/security/passwords.py",
    }
)


def is_security_edge(path: str) -> bool:
    return path in SECURITY_EDGES or (
        path.startswith("control/src/vonk_control/security/")
        and path not in NOT_SECURITY_EDGES
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


# Where a document or payload arrives from outside, so its digest is verified
# against the bytes: comparing it there is ingress, not a re-hash.
REHASH_INGRESS = (
    "control/src/vonk_control/catalog/entities.py",
    "control/src/vonk_control/catalog/service.py",
    "control/src/vonk_control/recipe_packages/",
    "control/src/vonk_control/inventory/repository.py",
    "control/src/vonk_control/telemetry/samples.py",
    "control/src/vonk_control/model_cache/file_verification.py",
    "control/src/vonk_control/model_cache/download.py",
    "control/src/vonk_control/model_cache/transfer.py",
    "control/src/vonk_control/model_cache/split_transfer.py",
    "control/src/vonk_control/model_cache/http.py",
)
# Modules that moved into recipe_packages/ without being ingress; judged as before.
NOT_RECIPE_PACKAGE_INGRESS = frozenset(
    {
        "control/src/vonk_control/recipe_packages/library_types.py",
        "control/src/vonk_control/recipe_packages/release.py",
    }
)


_STORED_VALUES = ("document", "plan", "payload", "recipe")


def _reaches_stored_value(call: ast.Call) -> bool:
    """An argument names a stored document, plan, payload or recipe."""
    names = (
        part.attr if isinstance(part, ast.Attribute) else getattr(part, "id", "")
        for argument in (*call.args, *(keyword.value for keyword in call.keywords))
        for part in ast.walk(argument)
        if isinstance(part, ast.Attribute | ast.Name)
    )
    return any(value in name.lower() for name in names for value in _STORED_VALUES)


def _is_digest_column(operand: ast.AST) -> bool:
    return isinstance(operand, ast.Attribute) and (
        operand.attr.endswith("digest") or operand.attr.endswith("_sha256")
    )


def _is_fresh_hash(call: ast.Call) -> bool:
    return ast.unparse(call.func).endswith(("sha256", "_digest")) and (
        _reaches_stored_value(call)
    )


def _rehashes_stored_row(node: ast.Compare) -> bool:
    """A stored digest column compared with a fresh digest of a stored value.

    The row recorded its digest when it was written (no internal hashing), so
    ``row.content_digest != document_sha256(row.document)`` re-derives what the
    row already says. Any column ending in ``digest`` or ``_sha256`` counts.
    """
    operands = (node.left, *node.comparators)
    if not any(_is_digest_column(operand) for operand in operands):
        return False
    return any(
        isinstance(call, ast.Call)
        and (
            _is_fresh_hash(call)
            or (
                ast.unparse(call.func).endswith(("sha256", "_digest"))
                and any(
                    isinstance(operand, ast.Attribute)
                    and operand.attr == "payload_digest"
                    for operand in operands
                )
            )
        )
        for operand in operands
        for call in ast.walk(operand)
    )


def _two_step_rehashes(tree: ast.Module) -> list[int]:
    """``fresh = document_sha256(row.document)`` then ``fresh != row.content_digest``."""
    lines: list[int] = []
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        fresh = {
            target.id
            for node in ast.walk(function)
            if isinstance(node, ast.Assign)
            for call in ast.walk(node.value)
            if isinstance(call, ast.Call) and _is_fresh_hash(call)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for node in ast.walk(function):
            if not isinstance(node, ast.Compare):
                continue
            operands = (node.left, *node.comparators)
            if any(_is_digest_column(item) for item in operands) and any(
                isinstance(item, ast.Name) and item.id in fresh for item in operands
            ):
                lines.append(node.lineno)
    return lines


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
        ("control/src/vonk_control/telemetry/samples.py", "_canonical_sample"),
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
                (isinstance(part, ast.Attribute) and part.attr == "observed_at")
                or (
                    isinstance(part, ast.Name)
                    and (
                        part.id in stamps or _STAMP_NAME.fullmatch(part.id) is not None
                    )
                )
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
                (isinstance(part, ast.Attribute) and part.attr in attributes)
                or (isinstance(part, ast.Name) and part.id in contacts)
                for part in ast.walk(value)
            )

        def age(value: ast.AST, ages: set[str] = ages) -> bool:
            return (isinstance(value, ast.Name) and value.id in ages) or (
                isinstance(value, ast.BinOp)
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
                    (
                        contact(left)
                        and clock(right)
                        and (
                            isinstance(op, ast.Gt | ast.GtE)
                            or (
                                id(node) in filters and isinstance(op, ast.Lt | ast.LtE)
                            )
                        )
                    )
                    or (
                        clock(left)
                        and contact(right)
                        and isinstance(op, ast.Lt | ast.LtE)
                    )
                    or (age(left) and zero(right) and isinstance(op, ast.Lt | ast.LtE))
                    or (zero(left) and age(right) and isinstance(op, ast.Lt | ast.LtE))
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


def unknown_to_terminal_handlers(source: str) -> list[int]:
    """An ``except *Unknown`` handler that builds a terminal value.

    An unknown outcome is observed and retried; building ``LifecycleState.FAILED``
    or ``retryable=False`` in its handler turns it into a decision nobody made
    (the synthetic FAILED request view, 2026-11-20).
    """
    found: list[int] = []
    for handler in ast.walk(ast.parse(source)):
        if not isinstance(handler, ast.ExceptHandler) or handler.type is None:
            continue
        caught = {
            ast.unparse(node).split(".")[-1]
            for node in ast.walk(handler.type)
            if isinstance(node, ast.Name | ast.Attribute)
        }
        if not any(
            name.endswith("Unknown") or name == "UnknownOutcomeError" for name in caught
        ):
            continue
        found.extend(
            node.value.lineno if isinstance(node, ast.keyword) else node.lineno
            for node in (n for stmt in handler.body for n in ast.walk(stmt))
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "FAILED"
                and ast.unparse(node.value).split(".")[-1] == "LifecycleState"
            )
            or (
                isinstance(node, ast.keyword)
                and node.arg == "retryable"
                and isinstance(node.value, ast.Constant)
                and node.value.value is False
            )
        )
    return sorted(set(found))


def replanned_digest_reads(source: str) -> list[int]:
    """``<x>.plan_digest`` read after ``accept_run_in_session`` in one function.

    Admission re-plans and saves the run with the fresh digest, so the plan the
    caller held is stale from that call on: the saved ``run.plan_digest`` is the
    one reader of what the run carries (the 2026-10-31 superseded Start).
    """
    found: set[int] = set()
    for scope in ast.walk(ast.parse(source)):
        if not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        accepts = [
            node.lineno
            for node in ast.walk(scope)
            if isinstance(node, ast.Call)
            and (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else node.func.id
                if isinstance(node.func, ast.Name)
                else ""
            )
            == "accept_run_in_session"
        ]
        if not accepts:
            continue
        found.update(
            node.lineno
            for node in ast.walk(scope)
            if isinstance(node, ast.Attribute)
            and node.attr == "plan_digest"
            and isinstance(node.ctx, ast.Load)
            and not (isinstance(node.value, ast.Name) and node.value.id == "run")
            and node.lineno > min(accepts)
        )
    return sorted(found)


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


_MEMBERSHIP_KEYS = frozenset({"artifact_set_sha256", "artifact_sha256"})


# The one reader outside the model cache that may require NAS coverage.
NAS_COVERAGE_READER = "control/src/vonk_control/distribution/model_cache.py"
# Flat modules that moved into the model_cache package: they were scanned
# before the move, so the package exemption for the storage modules excludes them.
MOVED_FLAT_MODEL_CACHE_MODULES = frozenset(
    f"control/src/vonk_control/model_cache/{name}.py"
    for name in (
        "api",
        "contract",
        "file_verification",
        "fleet_removal_contract",
        "progress",
        "ranges",
        "states",
        "streams",
    )
)


def _targets_membership_rows(node: ast.AST) -> bool:
    """Whether a condition names specific rows: ``key.in_(...)`` or ``key == x``.

    ``not_in`` and ``is_not(None)`` still read every other set's rows.
    """

    def key(value: ast.AST) -> bool:
        return isinstance(value, ast.Attribute) and value.attr in _MEMBERSHIP_KEYS

    return any(
        (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "in_"
            and key(inner.func.value)
        )
        or (
            isinstance(inner, ast.Compare)
            and all(isinstance(op, ast.Eq) for op in inner.ops)
            and (key(inner.left) or any(key(item) for item in inner.comparators))
        )
        for inner in ast.walk(node)
    )


def unfiltered_membership_selects(source: str) -> list[int]:
    """Lines reading ``ModelCacheSetArtifact`` without naming rows or a limit.

    A request or removal path pays for the sets it names, not for every
    membership row of the cache (a Seq Scan of ~31k rows per acceptance).
    Covers ``select(...)``, ``sa.select(...)`` and ``session.query(...)``; the
    read is scoped only by a chained ``.where``/``.filter``/``.filter_by`` that
    targets ``artifact_set_sha256`` or ``artifact_sha256`` rows, or a ``.limit``.
    """

    tree = ast.parse(source)

    def is_root(node: ast.AST) -> TypeGuard[ast.Call]:
        return (
            isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == "select")
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"select", "query"}
                )
            )
            and any(
                isinstance(inner, ast.Name) and inner.id == "ModelCacheSetArtifact"
                for arg in node.args
                for inner in ast.walk(arg)
            )
        )

    scoped: set[int] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        attr = node.func.attr
        if attr == "limit":
            targeted = True
        elif attr in {"where", "filter"}:
            targeted = any(_targets_membership_rows(arg) for arg in node.args)
        elif attr == "filter_by":
            targeted = any(item.arg in _MEMBERSHIP_KEYS for item in node.keywords)
        else:
            continue
        receiver: ast.AST = node.func.value
        while isinstance(receiver, ast.Call):
            if targeted and is_root(receiver):
                scoped.add(id(receiver))
            receiver = (
                receiver.func.value
                if isinstance(receiver.func, ast.Attribute)
                else ast.Constant(None)
            )
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if is_root(node) and id(node) not in scoped
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
        "control/src/vonk_control/waits/stall_window.py",
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


def _says_422(text: str, following: str) -> bool:
    """A line that answers 422, by keyword, positional or split-line status."""
    return bool(
        re.search(
            r"\bstatus_code\s*=\s*422\b|\bHTTP_422_|\bHTTPException\(\s*422\b", text
        )
        or (
            text.rstrip().endswith("HTTPException(")
            and following.strip().startswith("422")
        )
        or (text.strip().startswith("422,") and text.strip() != "")
    )


# A 401/403 is a security verdict: the Spark treats it as fatal (it backs off
# 30-600 s in the rotation loop instead of retrying soon). Only a typed security
# refusal may say it, never a handler for a broad exception.
_SAYS_FATAL_AUTH = re.compile(
    r"\bstatus_code\s*=\s*40[13]\b|\bHTTPException\(\s*40[13]\b"
)


_BROAD_EXCEPT = re.compile(
    r"\b(?:Exception|ValueError|OSError|PermissionError|RuntimeError|KeyError"
    r"|TypeError|SQLAlchemyError)\b"
)


# A broad handler that parks or defers work repeats a deterministic refusal on
# every attempt with nothing changed (#2299: an internal InvalidValue during a
# load retry backed off for ever). It asks the one classifier first.
_BROAD_PARKED_EXCEPT = re.compile(r"\b(?:Exception|ValueError|RuntimeError|KeyError)\b")


_PARKS_WORK = re.compile(r"\b(?:_decline_retry\w*|_defer_\w+|_park_for_retry)\(")


_CONSULTS_DISPOSITION = re.compile(r"\b(?:disposition|is_definite)\(")


def _except_header(lines: list[str], except_line: int) -> tuple[str, int]:
    """The `except` clause at except_line (may span lines) and its last line."""
    parts = []
    for number, text in enumerate(lines[except_line - 1 :], except_line):
        parts.append(text)
        if text.split("#", 1)[0].rstrip().endswith(":"):
            return " ".join(parts), number
    return " ".join(parts), len(lines)


def _handler_body(lines: list[str], except_line: int) -> list[tuple[str, str]]:
    """The (line, next line) pairs of the block under the `except` at except_line."""
    header = lines[except_line - 1]
    indent = len(header) - len(header.lstrip())
    body = []
    for index in range(except_line, len(lines)):
        text = lines[index]
        if text.strip() and len(text) - len(text.lstrip()) <= indent:
            break
        body.append((text, lines[index + 1] if index + 1 < len(lines) else ""))
    return body


# Package lifecycle logic lives in vonk-agent-helper::package_hooks; the
# maintainer scripts are shims. An added line there is one of these, nothing else.
SHIM_LINE = re.compile(
    r"""(?x)^(?:
        \#.*
      | set\ -eu?
      | exit\ 0
      | \[\ "\$\{?1(?::-)?\}?"\ =\ [a-z-]+\ \]\ \|\|\ exit\ 0
      | (?:exec\ )?/usr/lib/vonk-forge/vonk-agent-helper\ --package-hook\ (?:postinst|prerm)\ [a-z" $@-]+(?:\ \|\|\ true)?
      | (?:/usr/bin/)?deb-systemd-helper\ purge\ [A-Za-z0-9_.@"$-]+(?:\ >/dev/null)?(?:\ \|\|\ true)?
      | rm\ -rf\ --\ /[A-Za-z0-9_./-]+
      | if\ \[\ "\$\{1:-\}"\ =\ purge\ \];\ then
      | for\ unit\ in\ \\
      | vonk-forge-[a-z.-]+;?(?:\ \\)?
      | do|done|fi
    )$"""
)


SHIM_SCRIPTS = frozenset({"postinst", "prerm", "postrm"})


def shim_hits(path: str, source: str, added: set[int]) -> list[str]:
    """Added shell under packaging/debian is a shim; no shell under systemd."""
    lines = source.splitlines()
    if path.startswith("packaging/debian/"):
        name = path.rsplit("/", 1)[1]
        if name in {"preinst", "config"} or name.endswith("-repair"):
            return [f"{path}:{min(added)}: maintainer logic belongs in package_hooks"]
        if name in SHIM_SCRIPTS:
            return [
                f"{path}:{line}: maintainer script line is not a shim"
                for line in sorted(added)
                if line <= len(lines)
                and lines[line - 1].strip()
                and not SHIM_LINE.match(lines[line - 1].strip())
            ]
    if path.startswith("packaging/systemd/"):
        return [
            f"{path}:{line}: a unit runs a binary, not /bin/sh -c"
            for line in sorted(added)
            if line <= len(lines) and "/bin/sh -c" in lines[line - 1]
        ]
    return []


# Background owners keep their own re-observation schedule; the in-request retry
# helpers sleep inside the caller. Known residue, not an allowlist:
# core.py `_stored_manifest.rebuild` still calls resolve_artifact_set.
BACKGROUND_OWNERS = frozenset(
    {
        "control/src/vonk_control/model_cache/revisions.py",
        "control/src/vonk_control/model_cache/inventory.py",
        "control/src/vonk_control/model_cache/observation.py",
        "control/src/vonk_control/model_cache/storage.py",
        "control/src/vonk_control/model_cache/repair.py",
        "control/src/vonk_control/recipe_routes/withdrawal.py",
        "control/src/vonk_control/recipe_routes/service.py",
    }
)


BACKGROUND_RETRY_CALLS = frozenset({"resolve_artifact_set", "bounded_attempts"})


def background_retry_calls(source: str) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and (
            (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in BACKGROUND_RETRY_CALLS
            )
            or (
                isinstance(node.func, ast.Name)
                and node.func.id in BACKGROUND_RETRY_CALLS
            )
        )
    ]


# Model-cache transfers run on the worker pool and report on a later tick, so a
# fixed number of ticks cannot know whether a thread has reported (#2076). The
# scope is a module that builds the cache and a loop, or the assertions after
# it, that read the cache: other services' ``tick`` is synchronous. Any state
# counts: a worker also reports QUEUED (an access wait, a rate limit).
TICK_POLL_SCOPE = re.compile(r"\bModelCacheService\(|\b_service\(")


TICK_POLL_MESSAGE = (
    "fixed tick count before a threaded model-cache outcome; wait with "
    "_drain, _drain_until or _wait_for"
)


CACHE_READS = frozenset(
    {"get_operation", "get_operator_operation", "get_operator_request"}
)


def _literal_range(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "range"
        and bool(node.args)
        and all(isinstance(argument, ast.Constant) for argument in node.args)
    )


def _own_nodes(loop: ast.For) -> list[ast.AST]:
    """The loop's nodes, without those of loops or functions nested in it."""
    nodes: list[ast.AST] = []
    pending: list[ast.AST] = [*loop.body, *loop.orelse]
    while pending:
        node = pending.pop()
        nodes.append(node)
        if not isinstance(
            node, (ast.For, ast.AsyncFor, ast.While, ast.FunctionDef, ast.Lambda)
        ):
            pending.extend(ast.iter_child_nodes(node))
    return nodes


def _reads_cache(nodes: list[ast.AST]) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in CACHE_READS
        for node in nodes
    )


def _asserts_thread_outcome(statements: list[ast.stmt]) -> bool:
    """Whether the asserts and assignments right after a loop read an outcome."""
    nodes: list[ast.AST] = []
    for statement in statements:
        if not isinstance(statement, (ast.Assert, ast.Assign, ast.AnnAssign)):
            break
        nodes.extend(ast.walk(statement))
    return _reads_cache(nodes) and any(
        isinstance(node, ast.Attribute) and node.attr in {"state", "failure"}
        for node in nodes
    )


def fixed_tick_polls(source: str) -> list[int]:
    """Fixed-count ``.tick()`` loops that poll for, or assert, a worker outcome.

    With a ``break`` the loop is a poll capped by a count, so by CPU speed;
    without one the assertions after it guess that the threads have reported.
    """
    sites = []
    for parent in ast.walk(ast.parse(source)):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(parent, field, None)
            if not isinstance(block, list):
                continue
            for index, loop in enumerate(block):
                if not isinstance(loop, ast.For) or not _literal_range(loop.iter):
                    continue
                own = _own_nodes(loop)
                if not any(
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "tick"
                    for node in own
                ):
                    continue
                if any(isinstance(node, ast.Break) for node in own):
                    polls = _reads_cache(own)
                else:
                    polls = _asserts_thread_outcome(block[index + 1 :])
                if polls:
                    sites.append(loop.lineno)
    return sites


# A thread that completes a Future only when the read succeeds or raises an
# Exception leaves the waiter blocked for good when it dies with a
# BaseException (CancelledError, SystemExit): the Future is never completed.
FUTURE_WORKER_MESSAGE = (
    "thread completes a Future only on Exception; complete it in except BaseException"
)


FUTURE_COMPLETIONS = frozenset({"set_result", "set_exception"})


def _is_thread_target(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    name = (
        node.func.attr
        if isinstance(node.func, ast.Attribute)
        else getattr(node.func, "id", "")
    )
    if name != "Thread":
        return None
    for keyword in node.keywords:
        if keyword.arg == "target" and isinstance(keyword.value, ast.Name):
            return keyword.value.id
    return None


def _completes_on_base_exception(function: ast.AST) -> bool:
    for handler in (n for n in ast.walk(function) if isinstance(n, ast.ExceptHandler)):
        broad = handler.type is None or (
            isinstance(handler.type, ast.Name) and handler.type.id == "BaseException"
        )
        if broad and any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in FUTURE_COMPLETIONS
            for n in ast.walk(handler)
        ):
            return True
    return False


def future_workers_catching_exception(source: str) -> list[tuple[int, int]]:
    """Spans of thread targets that complete a Future without BaseException."""
    tree = ast.parse(source)
    targets = {name for node in ast.walk(tree) if (name := _is_thread_target(node))}
    if not targets:
        return []
    return [
        (node.lineno, node.end_lineno or node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name in targets
        and any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in FUTURE_COMPLETIONS
            for n in ast.walk(node)
        )
        and not _completes_on_base_exception(node)
    ]


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
                    (
                        isinstance(part, ast.Raise)
                        and (
                            part.exc is None
                            or (
                                isinstance(part.exc, ast.Call)
                                and isinstance(part.exc.func, ast.Name)
                                and part.exc.func.id
                                == "FleetProfileRecheckContractDefect"
                            )
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
