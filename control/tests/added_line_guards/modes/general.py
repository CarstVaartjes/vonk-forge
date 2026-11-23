"""Added-line modes: general checks, each a function over one CheckContext."""

from __future__ import annotations

import ast
import re

from ... import (
    alias_rules,
    contract_constant_copies,
    docker_listings,
    durable_revocations,
    fixture_closures,
    git_free_test_lines,
    intent_ordinal_writers,
    job_owner_payload,
    legacy_tolerance,
    lifecycle_sets,
    model_set_first_requester,
    raw_barriers,
    repeated_parse,
    request_path_scans,
    retention_head_digests,
    scratch_names,
    shell_trap_waits,
    typed_string_lengths,
    whole_collection_readers,
)
from ... import claim_registry as claims
from ... import collection_continuation as collections
from ... import content_identity_boundaries as identity
from ... import coordination_boundaries as coordination
from ... import deterministic_retries as retries
from ... import document_read_boundaries as document_reads
from ... import lifecycle_reasons as reasons
from ... import principle_guards as principles
from ... import progress_attempt_boundaries as progress_attempts
from ... import short_wall_clocks as wall_clocks
from ... import untyped_mapping_boundaries as mappings
from ..context import CheckContext
from ..scans import (
    BACKGROUND_OWNERS,
    BOOKKEEPING_DRIFT,
    FUTURE_WORKER_MESSAGE,
    PRINTED_OUTPUT_KIND,
    PRINTED_OUTPUT_SCOPE,
    PRINTED_OUTPUT_WORDING,
    READINESS_READERS,
    REHASH_INGRESS,
    REVISION_NUMBER_ALLOCATORS,
    REVISION_NUMBER_KEEPERS,
    _catches_only,
    _is_forbidden_response,
    _rehashes_stored_row,
    _two_step_rehashes,
    background_retry_calls,
    future_workers_catching_exception,
    is_security_edge,
    planner_defect_waits,
    printed_wording_lines,
    rust_test_lines,
    security_refusals,
    unknown_to_terminal_handlers,
)


def check_noexit(ctx: CheckContext) -> None:
    path, source, added, modes, is_test, noexit_hits = (
        ctx.path,
        ctx.source,
        ctx.added,
        ctx.modes,
        ctx.is_test,
        ctx.noexit_hits,
    )
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


def check_noexit_2(ctx: CheckContext) -> None:
    path, source, added, modes, is_test, noexit_hits = (
        ctx.path,
        ctx.source,
        ctx.added,
        ctx.modes,
        ctx.is_test,
        ctx.noexit_hits,
    )
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


def check_listings(ctx: CheckContext) -> None:
    path, source, added, modes, is_test, noexit_hits = (
        ctx.path,
        ctx.source,
        ctx.added,
        ctx.modes,
        ctx.is_test,
        ctx.noexit_hits,
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


def check_excerpts(ctx: CheckContext) -> None:
    path, source, added, modes, is_test, noexit_hits = (
        ctx.path,
        ctx.source,
        ctx.added,
        ctx.modes,
        ctx.is_test,
        ctx.noexit_hits,
    )
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


def check_legacy(ctx: CheckContext) -> None:
    path, source, modes, is_test, hits = (
        ctx.path,
        ctx.source,
        ctx.modes,
        ctx.is_test,
        ctx.hits,
    )
    if "legacy" in modes and not is_test and legacy_tolerance.in_web_scope(path):
        hits.extend(legacy_tolerance.scan_web_source(source))


def check_docker_listings(ctx: CheckContext) -> None:
    path, source, modes, is_test, hits = (
        ctx.path,
        ctx.source,
        ctx.modes,
        ctx.is_test,
        ctx.hits,
    )
    if "docker-listings" in modes and docker_listings.in_scope(path) and not is_test:
        hits.extend(
            (line, docker_listings.KIND)
            for line in docker_listings.scan_source(source, rust_test_lines(source))
        )


def check_block_23(ctx: CheckContext) -> None:
    path, source, added, modes, is_test, python, hits = (
        ctx.path,
        ctx.source,
        ctx.added,
        ctx.modes,
        ctx.is_test,
        ctx.python,
        ctx.hits,
    )
    if python and not is_test:
        if "request-scans" in modes:
            hits.extend(
                (line, "unbounded request catalog/history scan")
                for line in request_path_scans.scan_tree(ast.parse(source), path=path)
            )
        if "collection-readers" in modes and path.startswith(
            "control/src/vonk_control/"
        ):
            hits.extend(whole_collection_readers.scan_source(source, path=path))
        if "repeated-parse" in modes and path.startswith("control/src/"):
            hits.extend(
                (line, "one stored value parsed twice in one function")
                for line in repeated_parse.scan_source(source)
            )
        if "legacy" in modes and legacy_tolerance.in_scope(path):
            hits.extend(legacy_tolerance.scan_source(source))
        if "job-owner-payload" in modes and job_owner_payload.in_scope(path):
            hits.extend(job_owner_payload.scan_source(source))
        if "lifecycle-sets" in modes and lifecycle_sets.in_scope(path):
            hits.extend(
                (line, lifecycle_sets.KIND)
                for line in lifecycle_sets.scan_source(source)
            )
        if "progress" in modes:
            hits.extend(
                (line, "monotonic progress without attempt key")
                for line in progress_attempts.scan_source(source)
            )
        if "mapping" in modes:
            hits.extend(
                (site.line, "untyped mapping")
                for site in (
                    *mappings.scan_source(source, path=path),
                    *mappings.scan_bare_class_fields(source, path=path),
                )
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
                        "raw or strict document read; use vonk_forge_contracts "
                        "read_recipe/read_model/read_*_display"
                    ),
                )
                for site in document_reads.scan_source(source, path=path)
            )
        if "identity" in modes:
            ingress = path.startswith(
                "control/src/vonk_control/recipe_packages/"
            ) or path in {
                "control/src/vonk_control/catalog/entities.py",
                "control/src/vonk_control/catalog/service.py",
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
            # One definition of a recipe's current revision: the catalog head
            # (catalog.queries.active_head_revision). Any use of revision_number
            # (ordering, max, comparison, sorting) can pick a reverted revision
            # over the head. Only the revision-number allocators and the column
            # definition may name it.
            if (
                path.startswith("control/src/vonk_control/")
                and path not in REVISION_NUMBER_ALLOCATORS | REVISION_NUMBER_KEEPERS
                and not path.startswith("control/src/vonk_control/models/")
            ):
                hits.extend(
                    (line, "revision_number: current is the catalog head")
                    for line, text in enumerate(source.splitlines(), 1)
                    if line in added and re.search(r"\brevision_number\b", text)
                )
            hits.extend(
                (site.line, "provenance comparison")
                for site in identity.scan_source(source, path=path)
            )
            # A readiness reader finds a recipe's cache and image facts by the
            # recipe's own artifact set and by the image digest. The recipe that
            # asked first (the set's recorded provenance, the revision or
            # document a build row names) does not own the shared bytes.
            if path.startswith("control/src/vonk_control/"):
                hits.extend(
                    (line, "cache set provenance: key by the recipe's own set")
                    for line, text in enumerate(source.splitlines(), 1)
                    if line in added
                    and re.search(r"\bModelCacheSet\.recipe_revision_sha256\b", text)
                )
            if path in READINESS_READERS:
                hits.extend(
                    (line, "readiness reader keyed by the asking recipe")
                    for line, text in enumerate(source.splitlines(), 1)
                    if line in added
                    and re.search(
                        r"\bRecipeBuild\.recipe_revision_id\b|\bdocument_id\b", text
                    )
                )
            # No internal hashing: a row's digest is recorded once, when it is
            # written. Re-hashing the Controller's own row to decide what it is
            # refuses it whenever a newer Controller reads it back differently.
            if not path.startswith(REHASH_INGRESS):
                rehash_tree = ast.parse(source)
                hits.extend(
                    (node.lineno, "stored row re-hashed")
                    for node in ast.walk(rehash_tree)
                    if isinstance(node, ast.Compare) and _rehashes_stored_row(node)
                )
                hits.extend(
                    (line, "stored row re-hashed")
                    for line in _two_step_rehashes(rehash_tree)
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
            hits.extend(
                (line, "decision by reason text")
                for line in reasons.prose_reason_reads(source)
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


def check_block_24(ctx: CheckContext) -> None:
    path, source, modes, is_test, python, hits = (
        ctx.path,
        ctx.source,
        ctx.modes,
        ctx.is_test,
        ctx.python,
        ctx.hits,
    )
    for mode in ("waits", "reads", "tests"):
        if mode not in modes or (is_test != (mode == "tests")):
            continue
        if python:
            sites = principles.scan_source(source, path=path, mode=mode)
        elif mode == "waits" and path.endswith(".rs"):
            sites = principles.scan_rust(source, path=path)
        elif mode == "waits" and path.endswith((".yaml", ".yml")):
            sites = principles.scan_compose_shell(source, path=path)
        elif mode == "waits" and (path.endswith(".sh") or source.startswith("#!")):
            sites = principles.scan_shell(source, path=path)
        else:
            sites = []
        hits.extend((site.line, site.kind) for site in sites)


def check_collections(ctx: CheckContext) -> None:
    path, source, modes, is_test, hits = (
        ctx.path,
        ctx.source,
        ctx.modes,
        ctx.is_test,
        ctx.hits,
    )
    if not is_test and "collections" in modes:
        hits.extend(
            (line, collections.KIND) for line in collections.scan_source(path, source)
        )


def check_future_workers(ctx: CheckContext) -> None:
    source, added, modes, is_test, python, hits = (
        ctx.source,
        ctx.added,
        ctx.modes,
        ctx.is_test,
        ctx.python,
        ctx.hits,
    )
    if python and not is_test and "future-workers" in modes:
        # An edit anywhere inside the worker can drop its BaseException handler.
        hits.extend(
            (changed, FUTURE_WORKER_MESSAGE)
            for start, end in future_workers_catching_exception(source)
            for changed in sorted(added)
            if start <= changed <= end
        )


def check_planner_defects(ctx: CheckContext) -> None:
    source, modes, is_test, python, hits = (
        ctx.source,
        ctx.modes,
        ctx.is_test,
        ctx.python,
        ctx.hits,
    )
    if python and not is_test and "planner-defects" in modes:
        hits.extend(
            (line, "planner contract defect hidden by broad wait")
            for line in planner_defect_waits(source)
        )


def check_waits(ctx: CheckContext) -> None:
    source, modes, is_test, python, hits = (
        ctx.source,
        ctx.modes,
        ctx.is_test,
        ctx.python,
        ctx.hits,
    )
    if python and not is_test and "waits" in modes:
        hits.extend(
            (line, "unknown outcome turned terminal (report it unknown; callers retry)")
            for line in unknown_to_terminal_handlers(source)
        )


def check_background_retries(ctx: CheckContext) -> None:
    path, source, modes, python, hits = (
        ctx.path,
        ctx.source,
        ctx.modes,
        ctx.python,
        ctx.hits,
    )
    if python and path in BACKGROUND_OWNERS and "background-retries" in modes:
        hits.extend(
            (
                line,
                (
                    "in-request retry helper in a background owner (resolve once "
                    "and re-observe on its schedule; residue: core.py "
                    "_stored_manifest.rebuild, recipe_routes service "
                    "decide_publication/_execute and withdrawal.py's hand-rolled loop)"
                ),
            )
            for line in background_retry_calls(source)
        )


def check_trap_waits(ctx: CheckContext) -> None:
    path, source, modes, is_test, hits = (
        ctx.path,
        ctx.source,
        ctx.modes,
        ctx.is_test,
        ctx.hits,
    )
    if (
        path.startswith("deploy/compose/")
        and path.endswith(".sh")
        and not is_test
        and "trap-waits" in modes
    ):
        hits.extend(
            (line, shell_trap_waits.KIND)
            for line in shell_trap_waits.scan_source(source)
        )


def check_alias_rules(ctx: CheckContext) -> None:
    path, source, modes, is_test, hits = (
        ctx.path,
        ctx.source,
        ctx.modes,
        ctx.is_test,
        ctx.hits,
    )
    if "alias-rules" in modes and alias_rules.in_scope(path) and not is_test:
        hits.extend(
            (line, alias_rules.KIND) for line in alias_rules.scan_source(source)
        )


def check_alias_rules_2(ctx: CheckContext) -> None:
    path, source, modes, is_test, hits = (
        ctx.path,
        ctx.source,
        ctx.modes,
        ctx.is_test,
        ctx.hits,
    )
    if "alias-rules" in modes and alias_rules.run_alias_in_scope(path) and not is_test:
        hits.extend(
            (line, alias_rules.RUN_ALIAS_KIND)
            for line in alias_rules.scan_run_alias(source)
        )


def check_alias_rules_3(ctx: CheckContext) -> None:
    path, source, modes, is_test, hits = (
        ctx.path,
        ctx.source,
        ctx.modes,
        ctx.is_test,
        ctx.hits,
    )
    if (
        "alias-rules" in modes
        and alias_rules.run_alias_write_in_scope(path)
        and not is_test
    ):
        hits.extend(
            (line, alias_rules.RUN_ALIAS_WRITE_KIND)
            for line in alias_rules.scan_run_alias_write(source, path)
        )


def check_intent_ordinal(ctx: CheckContext) -> None:
    path, source, modes, is_test, hits = (
        ctx.path,
        ctx.source,
        ctx.modes,
        ctx.is_test,
        ctx.hits,
    )
    if (
        "intent-ordinal" in modes
        and intent_ordinal_writers.in_scope(path)
        and not is_test
    ):
        hits.extend(
            (line, intent_ordinal_writers.KIND)
            for line in intent_ordinal_writers.scan_source(source)
        )


def check_durable_revocations(ctx: CheckContext) -> None:
    path, source, modes, is_test, python, hits = (
        ctx.path,
        ctx.source,
        ctx.modes,
        ctx.is_test,
        ctx.python,
        ctx.hits,
    )
    if (
        python
        and not is_test
        and "durable-revocations" in modes
        and durable_revocations.in_scope(path)
    ):
        hits.extend(
            (line, durable_revocations.KIND)
            for line in durable_revocations.scan_source(source)
        )


def check_typed_string_lengths(ctx: CheckContext) -> None:
    path, source, modes, python, hits = (
        ctx.path,
        ctx.source,
        ctx.modes,
        ctx.python,
        ctx.hits,
    )
    if (
        python
        and "typed-string-lengths" in modes
        and typed_string_lengths.in_scope(path)
    ):
        hits.extend(
            (line, typed_string_lengths.KIND)
            for line in typed_string_lengths.scan_source(source)
        )


def check_scratch_names(ctx: CheckContext) -> None:
    path, source, modes, is_test, python, hits = (
        ctx.path,
        ctx.source,
        ctx.modes,
        ctx.is_test,
        ctx.python,
        ctx.hits,
    )
    if python and not is_test and "scratch-names" in modes:
        hits.extend(
            (line, scratch_names.KIND)
            for line in scratch_names.scan_source(source, path)
        )


def check_head_digests(ctx: CheckContext) -> None:
    path, source, modes, python, hits = (
        ctx.path,
        ctx.source,
        ctx.modes,
        ctx.python,
        ctx.hits,
    )
    if python and "head-digests" in modes and retention_head_digests.in_scope(path):
        hits.extend(
            (line, retention_head_digests.KIND)
            for line in retention_head_digests.scan_source(source)
        )


def check_set_first_requester(ctx: CheckContext) -> None:
    path, source, modes, is_test, python, hits = (
        ctx.path,
        ctx.source,
        ctx.modes,
        ctx.is_test,
        ctx.python,
        ctx.hits,
    )
    if (
        python
        and not is_test
        and "set-first-requester" in modes
        and model_set_first_requester.in_scope(path)
    ):
        hits.extend(
            (line, model_set_first_requester.KIND)
            for line in model_set_first_requester.scan_source(source)
        )


def check_barriers(ctx: CheckContext) -> None:
    path, source, modes, python, hits = (
        ctx.path,
        ctx.source,
        ctx.modes,
        ctx.python,
        ctx.hits,
    )
    if python and "barriers" in modes and raw_barriers.in_scope(path):
        hits.extend(
            (line, raw_barriers.KIND) for line in raw_barriers.scan_source(source)
        )


def check_git_free_tests(ctx: CheckContext) -> None:
    path, source, modes, is_test, hits = (
        ctx.path,
        ctx.source,
        ctx.modes,
        ctx.is_test,
        ctx.hits,
    )
    if "git-free-tests" in modes:
        # A Rust file under tests/ is all test; elsewhere only cfg(test) items.
        every_line = set(range(1, source.count("\n") + 2))
        hits.extend(
            (line, git_free_test_lines.KIND)
            for line in git_free_test_lines.scan_path(
                path,
                source,
                is_test=is_test,
                test_lines=every_line
                if is_test
                else (rust_test_lines(source) if path.endswith(".rs") else set()),
            )
        )


def check_printed_output(ctx: CheckContext) -> None:
    path, source, modes, python, hits = (
        ctx.path,
        ctx.source,
        ctx.modes,
        ctx.python,
        ctx.hits,
    )
    if (
        python
        and "printed-output" in modes
        and path.startswith(PRINTED_OUTPUT_SCOPE)
        and path != PRINTED_OUTPUT_WORDING
    ):
        hits.extend(
            (number, PRINTED_OUTPUT_KIND) for number in printed_wording_lines(source)
        )


def check_contract_constants(ctx: CheckContext) -> None:
    path, source, modes, python, hits = (
        ctx.path,
        ctx.source,
        ctx.modes,
        ctx.python,
        ctx.hits,
    )
    if python and "contract-constants" in modes:
        hits.extend(
            (line, contract_constant_copies.LITERAL_KIND)
            for line in contract_constant_copies.scan_literals(path, source)
        )


def check_contract_constants_2(ctx: CheckContext) -> None:
    path, source, modes, is_test, python, hits = (
        ctx.path,
        ctx.source,
        ctx.modes,
        ctx.is_test,
        ctx.python,
        ctx.hits,
    )
    if python and not is_test and "contract-constants" in modes:
        hits.extend(
            (line, contract_constant_copies.KIND)
            for line in contract_constant_copies.scan_source(path, source)
        )


def check_fixture_closures(ctx: CheckContext) -> None:
    source, modes, python, hits = ctx.source, ctx.modes, ctx.python, ctx.hits
    if python and "fixture-closures" in modes:
        hits.extend(
            (line, fixture_closures.KIND)
            for line in fixture_closures.scan_source(source)
        )


def check_clocks(ctx: CheckContext) -> None:
    source, modes, is_test, python, hits = (
        ctx.source,
        ctx.modes,
        ctx.is_test,
        ctx.python,
        ctx.hits,
    )
    if python and is_test and "clocks" in modes:
        hits.extend(
            (line, wall_clocks.KIND) for line in wall_clocks.scan_source(source)
        )
