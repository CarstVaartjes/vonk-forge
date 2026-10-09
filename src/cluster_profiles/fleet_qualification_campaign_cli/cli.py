"""Batch qualification of an exact recipe authority against a Controller.

Every batch is an ordinary whole-Fleet load of one dedicated profile:

* ``load`` saves the batch assignments, loads the profile (install and start)
  and smokes every lane;
* ``recover`` waits for the operator's physical failure (host restart or rank
  loss), for the Controller to heal the workload, and smokes it again;
* ``stop`` loads the empty profile, which stops the batch and advances the
  campaign to the next batch.

Each outcome is one JSON line in a plain results log. Rerunning a step simply
repeats it; the latest line for a recipe and step wins. A shared recovery group
member whose representative already passed that failure mode in this campaign
is recorded as covered instead of being disrupted again.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ..cli_states import FAILED
from ..control_client import (
    ControlClient,
    ControlClientError,
)
from ..fleet_qualification import (
    QualificationError,
    QualificationObservationUnknown,
    _observe,
)
from ..qualification_fixtures import FixtureError
from .application import load
from .contracts import ResultsLog, load_campaign
from .recovery import recover
from .status import _select_batch, status
from .stop import stop


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Qualify an exact recipe authority batch by batch on a dedicated "
            "whole-Fleet profile, recording results in a JSON-lines log."
        )
    )
    parser.add_argument(
        "step",
        nargs="?",
        default="status",
        choices=("status", "load", "recover", "stop"),
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--profile-number", type=int)
    parser.add_argument("--batch", help="default: the first batch not yet stopped")
    parser.add_argument(
        "--spark",
        action="append",
        default=[],
        help="load: Controller Spark IDs in lane order (a dual lane takes two)",
    )
    parser.add_argument(
        "--failure-spark",
        help="recover: the Spark to take offline for a dual rank loss",
    )
    args = parser.parse_args(argv)
    if args.step != "status" and (
        args.profile_number is None or args.profile_number < 1
    ):
        parser.error(f"{args.step} requires --profile-number")
    if args.step == "load" and not args.spark:
        parser.error("load requires --spark IDs")
    return args


def _notify(value: dict[str, object]) -> None:
    print(json.dumps(value, sort_keys=True), file=sys.stderr, flush=True)


def run(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[], Any] = ControlClient.from_environment,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    notify: Callable[[dict[str, object]], None] = _notify,
) -> dict[str, object]:
    args = _arguments(argv)

    def capture():
        try:
            return load_campaign(args.manifest)
        except FixtureError as error:
            raise QualificationObservationUnknown(
                "campaign fixture registry is not yet readable"
            ) from error

    campaign = _observe(capture, clock() + 20, clock, sleeper, 0.5)
    log = ResultsLog(args.results, campaign.authority_id)
    batch = _select_batch(campaign, log, args.batch)
    if args.step == "status" or batch is None:
        return status(campaign, log, batch)
    client = client_factory()
    number = args.profile_number
    if args.step == "load":
        return load(client, campaign, log, batch, number, args.spark, clock, sleeper)
    if args.step == "recover":
        return recover(
            client,
            campaign,
            log,
            batch,
            number,
            args.failure_spark,
            clock,
            sleeper,
            notify,
        )
    return stop(client, campaign, log, batch, number, clock, sleeper)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = run(argv)
    except (
        ControlClientError,
        FixtureError,
        QualificationError,
        QualificationObservationUnknown,
        OSError,
    ) as error:
        print(
            json.dumps({"status": FAILED, "error": str(error)[:1024]}),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":"), default=str))
    return 0
