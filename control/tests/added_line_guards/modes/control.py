"""Added-line modes: control checks, each a function over one CheckContext."""

from __future__ import annotations

import ast
import re

from ... import (
    bare_savepoints,
    intent_ordinal_writers,
    locked_io,
    sync_io_in_async,
)
from ... import optimistic_boundaries as optimistic
from ... import principle_guards as principles
from ...recipe_run_write_verdicts import classified_write_lines
from ..context import CheckContext
from ..scans import (
    _ANONYMOUS_SEND,
    _ANONYMOUS_SEND_MODULES,
    _BROAD_EXCEPT,
    _BROAD_PARKED_EXCEPT,
    _CONSULTS_DISPOSITION,
    _OPENER_OWNERS,
    _PARKS_WORK,
    _SAYS_FATAL_AUTH,
    AGENT_422_VERDICT_MARKER,
    CAPACITY_REFUSAL_MARKER,
    CAPACITY_REFUSAL_SITES,
    CONTACT_WRITERS,
    FIXED_RESERVE,
    FIXED_RESERVE_VERDICTS,
    JOB_LIMIT_ASSIGNMENT,
    JOB_LIMITS_OWNER,
    LIVE_RESERVATION_COPY,
    LIVE_RESERVATION_KIND,
    MOVED_FLAT_MODEL_CACHE_MODULES,
    MOVEMENT_VERDICTS,
    NAS_COVERAGE_READER,
    NEWEST_SNAPSHOT_KIND,
    NEWEST_SNAPSHOT_SCAN,
    PREBUILT_KEY_INGEST,
    PREBUILT_KEY_KIND,
    PREBUILT_KEY_READ,
    RANK_INSTALLED_COPY,
    RANK_INSTALLED_KIND,
    START_BUDGET_VERDICTS,
    TABLE_LOCK_OWNERS,
    TITLE_IDENTITY,
    TITLE_IDENTITY_VERDICTS,
    _except_header,
    _handler_body,
    _says_422,
    agent_stamp_freshness,
    contact_writes,
    controller_contact_clock_checks,
    is_security_edge,
    model_sized_count_caps,
    paging_count_ceilings,
    pairwise_movement_checks,
    provider_opener_calls,
    replanned_digest_reads,
    runtime_count_caps,
    start_budget_derivations,
    unfiltered_membership_selects,
)


def check_locks_2(ctx: CheckContext) -> None:
    path, source, added, modes, is_test, noexit_hits = (
        ctx.path,
        ctx.source,
        ctx.added,
        ctx.modes,
        ctx.is_test,
        ctx.noexit_hits,
    )
    if (
        "locks" in modes
        and not is_test
        and path.endswith(".py")
        and path.startswith("control/src/")
        and path not in TABLE_LOCK_OWNERS
    ):
        # A table-wide lock taken NOWAIT loses to any writer in flight, so a
        # recurring pass that needs one is refused for as long as traffic
        # lasts (the catalog collector's 21-table fence, 2026-11-04). Recheck
        # per row instead: a conditional delete, the foreign keys, and a short
        # lock_timeout that skips a held row.
        noexit_hits.extend(
            f"{path}:{line}: table-wide lock; recheck per row instead"
            for line, text in enumerate(source.splitlines(), 1)
            if line in added and re.search(r"\bLOCK\s+TABLE\b", text, re.IGNORECASE)
        )


def check_optimistic(ctx: CheckContext) -> None:
    path, source, modes, python, hits = (
        ctx.path,
        ctx.source,
        ctx.modes,
        ctx.python,
        ctx.hits,
    )
    if "optimistic" in modes and python and path.startswith("control/src/"):
        hits.extend((line, optimistic.KIND) for line in optimistic.scan_source(source))
        # workload_intent is the one writer of the intent ordinal: a conditional
        # UPDATE on that column, which locks nothing it does not write (lock plan
        # step 4). Anywhere else the same update is still refused here.
        if (
            not is_security_edge(path)
            and not path.startswith("control/src/vonk_control/enrollment/")
            and path != intent_ordinal_writers.OWNER
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


def check_contact_clocks(ctx: CheckContext) -> None:
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
        and "contact-clocks" in modes
        and path.startswith("control/src/")
    ):
        hits.extend(
            (line, "Controller contact rejected for negative clock age")
            for line in controller_contact_clock_checks(source)
        )


def check_contact_clocks_2(ctx: CheckContext) -> None:
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
        and "contact-clocks" in modes
        and path.startswith("control/src/")
    ):
        hits.extend(
            (line, "agent stamp aged against the Controller clock; use received_at")
            for line in agent_stamp_freshness(source, path)
        )


def check_capacity_gates(ctx: CheckContext) -> None:
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
        and "capacity-gates" in modes
        and path.startswith("control/src/")
    ):
        hits.extend(
            (site.line, site.kind)
            for site in principles.capacity_gates_before_claims(source, path=path)
            if (site.path, site.function) not in principles.CAPACITY_GATE_VERDICTS
        )


def check_start_budgets(ctx: CheckContext) -> None:
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
        and "start-budgets" in modes
        and path.startswith("control/src/")
    ):
        hits.extend(
            (line, "start budget derived from a stored start timestamp")
            for line, function in start_budget_derivations(source)
            if (path, function) not in START_BUDGET_VERDICTS
        )


def check_plan_digests(ctx: CheckContext) -> None:
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
        and "plan-digests" in modes
        and path.startswith("control/src/")
    ):
        hits.extend(
            (line, "plan digest read from the pre-admission plan (use run.plan_digest)")
            for line in replanned_digest_reads(source)
        )


def check_movement(ctx: CheckContext) -> None:
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
        and "movement" in modes
        and path.startswith("control/src/")
    ):
        hits.extend(
            (line, "pairwise progress movement (compare against forward marks)")
            for line, function in pairwise_movement_checks(source)
            if (path, function) not in MOVEMENT_VERDICTS
        )


def check_nas_space(ctx: CheckContext) -> None:
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
        and "nas-space" in modes
        and path.startswith("control/src/vonk_control/model_cache/")
        and path != "control/src/vonk_control/model_cache/storage.py"
    ):
        # NAS space is decided once, in storage.py's _storage_shortfall, next to what
        # the running transfers still write. A comparison with the free disk
        # alone let parallel downloads overcommit the volume (#1910).
        hits.extend(
            (line, "NAS space read outside storage.py; use _storage_shortfall")
            for line, text in enumerate(source.splitlines(), 1)
            if re.search(r"\bfree_bytes\(\)|\bdisk_usage\(", text)
        )


def check_capacity_refusals(ctx: CheckContext) -> None:
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
        and "capacity-refusals" in modes
        and path.startswith("control/src/")
        and path not in CAPACITY_REFUSAL_SITES
    ):
        # A recipe's declared peak and swap are estimates, tried on hardware and
        # judged by the host memory guard (owner 2026-11-02). "Demand exceeds
        # capacity" ends a request for good, so only the classified sites may
        # emit it; a memory or swap estimate waits or is tried, never ends.
        hits.extend(
            (line, "demand-exceeds-capacity outside its classified sites")
            for line, text in enumerate(source.splitlines(), 1)
            if re.search(r"\b(?:RESOURCE_)?DEMAND_EXCEEDS_CAPACITY\b", text)
            and CAPACITY_REFUSAL_MARKER not in text
        )


def check_agent_422(ctx: CheckContext) -> None:
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
        and "agent-422" in modes
        and path.startswith("control/src/vonk_control/agent_api/")
    ):
        lines = source.splitlines()
        hits.extend(
            (line, "agent-facing 422 outside body validation; answer a wait")
            for line, text in enumerate(lines, 1)
            if _says_422(text, lines[line] if line < len(lines) else "")
            and AGENT_422_VERDICT_MARKER not in text
        )
        # A caught exception is not a verdict: it is as often this Controller's
        # own bug or race, and a 422 makes the Spark drop real evidence. Any
        # handler body that produces an unmarked 422 is refused at its `except`
        # line; the handler answers through contract_verdict_or_wait instead.
        hits.extend(
            (line, "except handler answers 422; use contract_verdict_or_wait")
            for line, text in enumerate(lines, 1)
            if re.match(r"\s*except\b", text)
            and any(
                _says_422(body, nxt) and AGENT_422_VERDICT_MARKER not in body
                for body, nxt in _handler_body(lines, line)
            )
        )


def check_agent_422_2(ctx: CheckContext) -> None:
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
        and "agent-422" in modes
        and path.startswith("control/src/vonk_control/agent_api/")
    ):
        # A broad exception is as often this Controller's own bug as a denial
        # (#2061 review: an internal ValueError in renewal killed the agent).
        lines = source.splitlines()
        for line, text in enumerate(lines, 1):
            if not re.match(r"\s*except\b", text):
                continue
            header, header_end = _except_header(lines, line)
            if not _BROAD_EXCEPT.search(header):
                continue
            # The except line and every 401/403 body line are flagged, so a
            # 403 added into an existing handler is caught by its own line.
            fatal = [
                number
                for number, (body, _) in enumerate(
                    _handler_body(lines, header_end), header_end + 1
                )
                if _SAYS_FATAL_AUTH.search(body)
            ]
            hits.extend(
                (
                    number,
                    "broad exception answers 401/403; use contract_verdict_or_wait",
                )
                for number in (*([line] if fatal else []), *fatal)
            )


def check_retry_dispositions(ctx: CheckContext) -> None:
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
        and "retry-dispositions" in modes
        and path.startswith("control/src/")
    ):
        lines = source.splitlines()
        for line, text in enumerate(lines, 1):
            if not re.match(r"\s*except\b", text):
                continue
            header, header_end = _except_header(lines, line)
            if not _BROAD_PARKED_EXCEPT.search(header):
                continue
            body = _handler_body(lines, header_end)
            if any(_CONSULTS_DISPOSITION.search(handled) for handled, _ in body):
                continue
            # The except line and every parking line are flagged, so a park
            # added into an existing handler is caught by its own line.
            parks = [
                number
                for number, (handled, _) in enumerate(body, header_end + 1)
                if _PARKS_WORK.search(handled)
            ]
            hits.extend(
                (
                    number,
                    "broad exception parks work; classify it with disposition() first",
                )
                for number in (*([line] if parks else []), *parks)
            )


def check_fixed_reserves(ctx: CheckContext) -> None:
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
        and "fixed-reserves" in modes
        and path.startswith("control/src/")
    ):
        # A reserve of volume space is a share of the volume (#1962): a fixed
        # byte count was 0.1 % of the NAS volume PostgreSQL shares with the
        # model cache, and a full volume stopped the database.
        hits.extend(
            (line, "fixed byte reserve; derive it from the volume's total")
            for line, text in enumerate(source.splitlines(), 1)
            if (match := FIXED_RESERVE.search(text))
            and (path, match.group(1)) not in FIXED_RESERVE_VERDICTS
        )


def check_job_limits(ctx: CheckContext) -> None:
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
        and "job-limits" in modes
        and path != JOB_LIMITS_OWNER
        and path.startswith(("control/src/", "agent_protocol/src/"))
    ):
        hits.extend(
            (line, "job limit restated; import it from vonk_agent_protocol.job_limits")
            for line, text in enumerate(source.splitlines(), 1)
            if JOB_LIMIT_ASSIGNMENT.search(text)
        )


def check_identity_2(ctx: CheckContext) -> None:
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
        and "identity" in modes
        and path.startswith("control/src/")
    ):
        marker = TITLE_IDENTITY_VERDICTS.get(path)
        hits.extend(
            (line, "name built from a display title; derive it from identity")
            for line, text in enumerate(source.splitlines(), 1)
            if TITLE_IDENTITY.search(text) and not (marker and marker in text)
        )


def check_model_sets(ctx: CheckContext) -> None:
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
        and "model-sets" in modes
        and path.startswith("control/src/vonk_control/model_cache/")
        and re.fullmatch(r"(?:removal_\w+|fleet_removal)\.py", path.rpartition("/")[2])
        and path.count("/") == 4
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


def check_model_sets_2(ctx: CheckContext) -> None:
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
        and "model-sets" in modes
        and path.startswith("control/src/vonk_control/")
        and path
        not in {
            "control/src/vonk_control/model_cache/removal_execution.py",
            "control/src/vonk_control/model_cache/partials_collection.py",
        }
    ):
        # Only the removal executor unlinks model files: it alone applies the
        # yield-to-newer-owner rule (retained objects, owned sets) first.
        hits.extend(
            (line, "model file removal outside removal_execution.py")
            for line, text in enumerate(source.splitlines(), 1)
            if re.search(r"\b_remove_model_(?:object_files|partial_set)\(", text)
            and not re.match(r"\s*def ", text)
        )


def check_model_sets_3(ctx: CheckContext) -> None:
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
        and "model-sets" in modes
        and path.startswith("control/src/vonk_control/")
    ):
        hits.extend(
            (line, "whole-table membership read; select only the named sets")
            for line in unfiltered_membership_selects(source)
        )


def check_model_sets_4(ctx: CheckContext) -> None:
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
        and "model-sets" in modes
        and path.startswith("control/src/vonk_control/")
        and not (
            path.startswith("control/src/vonk_control/model_cache/")
            and path not in MOVED_FLAT_MODEL_CACHE_MODULES
        )
        and path != NAS_COVERAGE_READER
    ):
        # NAS coverage is read only where NAS bytes are read: the distribution
        # adapter. A planner or compiler that required it downloaded a model
        # every target Spark already held, after the NAS evicted it (#2337);
        # a grant built from the NAS set parked an image-only copy. Grants
        # name the accepted compiled plan's objects (``plan_objects``).
        hits.extend(
            (line, "NAS coverage required outside the copy that reads NAS bytes")
            for line, text in enumerate(source.splitlines(), 1)
            if re.search(
                r"\b(?:_require_managed_cache_coverage|resolve_verified_artifact_set"
                r"|objects_for_set|verified_model_objects_for_set)\b",
                text,
            )
        )


def check_item_counts(ctx: CheckContext) -> None:
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
        and "item-counts" in modes
        and path.startswith(("control/src/", "agent_protocol/src/"))
    ):
        hits.extend(
            (line, "item-count cap on a model-sized list; bound by bytes instead")
            for line in sorted(
                {
                    *model_sized_count_caps(source),
                    *runtime_count_caps(source),
                    *paging_count_ceilings(source),
                }
            )
        )


def check_openers(ctx: CheckContext) -> None:
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
        and "openers" in modes
        and path.startswith("control/src/")
    ):
        owner = _OPENER_OWNERS.get(path, "")
        hits.extend(
            (line, "provider opened outside download.py:_open_provider")
            for line, function in provider_opener_calls(source)
            if path not in _OPENER_OWNERS or (owner is not None and function != owner)
        )


def check_openers_2(ctx: CheckContext) -> None:
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


def check_locked_io(ctx: CheckContext) -> None:
    path, source, modes, python, hits = (
        ctx.path,
        ctx.source,
        ctx.modes,
        ctx.python,
        ctx.hits,
    )
    if python and path.startswith("control/src/") and "locked-io" in modes:
        hits.extend((line, locked_io.KIND) for line in locked_io.scan_source(source))


def check_savepoints(ctx: CheckContext) -> None:
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
        and "savepoints" in modes
        and path.startswith("control/src/")
    ):
        hits.extend(
            (line, bare_savepoints.KIND) for line in bare_savepoints.scan_source(source)
        )


def check_sync_in_async(ctx: CheckContext) -> None:
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
        and "sync-in-async" in modes
        and path.startswith("control/src/")
    ):
        hits.extend((line, kind) for line, kind in sync_io_in_async.scan_source(source))


def check_rank_installed(ctx: CheckContext) -> None:
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
        and "rank-installed" in modes
        and path.startswith("control/src/")
        and path != "control/src/vonk_control/machine_states.py"
    ):
        hits.extend(
            (number, RANK_INSTALLED_KIND)
            for number, text in enumerate(source.splitlines(), 1)
            if RANK_INSTALLED_COPY.search(text)
        )


def check_prebuilt_key(ctx: CheckContext) -> None:
    if (
        ctx.python
        and not ctx.is_test
        and "prebuilt-key" in ctx.modes
        and ctx.path.startswith(("control/src/", "src/", "scripts/"))
        and not ctx.path.startswith(PREBUILT_KEY_INGEST)
    ):
        ctx.hits.extend(
            (number, PREBUILT_KEY_KIND)
            for number, text in enumerate(ctx.source.splitlines(), 1)
            if PREBUILT_KEY_READ.search(text)
        )


def check_live_reservation(ctx: CheckContext) -> None:
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
        and "live-reservation" in modes
        and path.startswith("control/src/")
        and path != "control/src/vonk_control/machine_states.py"
    ):
        hits.extend(
            (number, LIVE_RESERVATION_KIND)
            for number, text in enumerate(source.splitlines(), 1)
            if LIVE_RESERVATION_COPY.search(text)
        )


def check_newest_snapshot(ctx: CheckContext) -> None:
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
        and "newest-snapshot" in modes
        and path.startswith("control/src/")
    ):
        hits.extend(
            (source.count("\n", 0, position) + 1, NEWEST_SNAPSHOT_KIND)
            for found in NEWEST_SNAPSHOT_SCAN.finditer(source)
            for position in (found.start(), found.end())
        )
