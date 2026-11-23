"""Added-line modes: cli tests checks, each a function over one CheckContext."""

from __future__ import annotations

import ast
import re

from ..context import CheckContext
from ..scans import (
    AGENT_REQUEST_BUILDERS,
    AGENT_REQUESTS,
    TICK_POLL_MESSAGE,
    TICK_POLL_SCOPE,
    fixed_tick_polls,
)


def check_boundaries(ctx: CheckContext) -> None:
    path, source, modes, python, hits = (
        ctx.path,
        ctx.source,
        ctx.modes,
        ctx.python,
        ctx.hits,
    )
    if (
        "boundaries" in modes
        and python
        and path.startswith("src/cluster_profiles/")
        and not path.startswith("src/cluster_profiles/control_client/")
    ):
        hits.extend(
            (
                node.lineno,
                (
                    "vonkctl failure raised as a bare ControlClientError; "
                    "use a typed error with its own code (control_client/errors.py)"
                ),
            )
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Raise)
            and isinstance(node.exc, ast.Call)
            and ast.unparse(node.exc.func) == "ControlClientError"
        )


def check_boundaries_2(ctx: CheckContext) -> None:
    path, source, modes, python, hits = (
        ctx.path,
        ctx.source,
        ctx.modes,
        ctx.python,
        ctx.hits,
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


def check_tick_polls(ctx: CheckContext) -> None:
    path, source, modes, python, hits = (
        ctx.path,
        ctx.source,
        ctx.modes,
        ctx.python,
        ctx.hits,
    )
    if (
        python
        and "tick-polls" in modes
        and path.startswith("control/tests/")
        and TICK_POLL_SCOPE.search(source)
    ):
        hits.extend((line, TICK_POLL_MESSAGE) for line in fixed_tick_polls(source))


def check_follow_validators(ctx: CheckContext) -> None:
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


def check_event_loops(ctx: CheckContext) -> None:
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
