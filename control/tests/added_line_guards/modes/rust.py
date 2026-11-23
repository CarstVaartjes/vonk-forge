"""Added-line modes: rust checks, each a function over one CheckContext."""

from __future__ import annotations

import re

from ... import content_identity_boundaries as identity
from ... import deterministic_retries as retries
from ... import (
    raw_loopback_bind,
)
from ... import vocabulary_literals as vocabulary
from ..context import CheckContext
from ..scans import (
    EXIT_CAUSE_OWNER,
    _enum_member_lines,
    exit_cause_tokens,
    new_literal_lines,
    rust_test_lines,
)


def check_causes(ctx: CheckContext) -> None:
    path, source, added, modes, is_test, noexit_hits = (
        ctx.path,
        ctx.source,
        ctx.added,
        ctx.modes,
        ctx.is_test,
        ctx.noexit_hits,
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


def check_exit_causes(ctx: CheckContext) -> None:
    path, source, added, modes, is_test, noexit_hits = (
        ctx.path,
        ctx.source,
        ctx.added,
        ctx.modes,
        ctx.is_test,
        ctx.noexit_hits,
    )
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


def check_plan_validation(ctx: CheckContext) -> None:
    path, source, added, modes, is_test, noexit_hits = (
        ctx.path,
        ctx.source,
        ctx.added,
        ctx.modes,
        ctx.is_test,
        ctx.noexit_hits,
    )
    if (
        "plan-validation" in modes
        and not is_test
        and path.startswith(
            (
                "rust/crates/vonk-agent/src/",
                "rust/crates/vonk-agent-protocol/src/compiled_oci/",
            )
        )
        and path.endswith(".rs")
        and path
        not in {
            "rust/crates/vonk-agent/src/executor/plans.rs",
            "rust/crates/vonk-agent/src/oci/custody.rs",
            "rust/crates/vonk-agent/src/oci/observation.rs",
        }
    ):
        # A compiled plan is validated once, by `refuse_invalid_plan` (plans.rs)
        # at dispatch. Another call repeats a rule. Added lines only; the
        # rustfmt split shape (`.validate()` on its own line after the plan
        # expression) counts too.
        test_lines = rust_test_lines(source)
        owner = r"\b(?:plan|spec|invocation|compiled_execution_plan)\s*"
        lines = source.splitlines()
        previous = ""
        for line, text in enumerate(lines, 1):
            if (
                line in added
                and line not in test_lines
                and (
                    re.search(owner + r"\.validate\(\)", text)
                    or (
                        text.lstrip().startswith(".validate()")
                        and re.search(owner + r"$", previous)
                    )
                )
            ):
                noexit_hits.append(
                    f"{path}:{line}: compiled plan validated again; dispatch owns it"
                )
            if text.strip():
                previous = text.rstrip()


def check_vocabulary(ctx: CheckContext) -> None:
    path, source, added, modes, words, removed, is_test, python, hits = (
        ctx.path,
        ctx.source,
        ctx.added,
        ctx.modes,
        ctx.words,
        ctx.removed,
        ctx.is_test,
        ctx.python,
        ctx.hits,
    )
    if (
        "vocabulary" in modes
        and not is_test
        and (python or path.endswith((".rs", ".ts", ".tsx")))
    ):
        definitions = _enum_member_lines(source) if python else set()
        if path.endswith(".rs"):
            definitions.update(rust_test_lines(source))
        contract = vocabulary.contract_words() if words is None else words
        hits.extend(
            (line, "contract literal")
            for line in new_literal_lines(
                (
                    line
                    for line in vocabulary.scan_source(
                        source, path=path, words=contract
                    )
                    if line not in definitions
                ),
                source,
                added,
                () if python else removed,
                contract,
            )
        )


def check_retries(ctx: CheckContext) -> None:
    path, source, modes, is_test, hits = (
        ctx.path,
        ctx.source,
        ctx.modes,
        ctx.is_test,
        ctx.hits,
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


def check_identity(ctx: CheckContext) -> None:
    path, source, modes, is_test, hits = (
        ctx.path,
        ctx.source,
        ctx.modes,
        ctx.is_test,
        ctx.hits,
    )
    if "identity" in modes and path.endswith(".rs") and not is_test:
        test_lines = rust_test_lines(source)
        hits.extend(
            (site.line, "provenance comparison")
            for site in identity.scan_source(source, path=path)
            if site.line not in test_lines
        )


def check_loopback_bind(ctx: CheckContext) -> None:
    path, source, modes, is_test, hits = (
        ctx.path,
        ctx.source,
        ctx.modes,
        ctx.is_test,
        ctx.hits,
    )
    if "loopback-bind" in modes and path.endswith(".rs"):
        every_line = set(range(1, source.count("\n") + 2))
        hits.extend(
            (line, raw_loopback_bind.KIND)
            for line in raw_loopback_bind.scan_source(
                source, every_line if is_test else rust_test_lines(source)
            )
        )
