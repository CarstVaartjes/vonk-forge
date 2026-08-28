"""Command-line entry point for controller-only fleet recipe qualification."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .control_client import ControlClient, ControlClientError
from .fleet_qualification import (
    ArtifactJobSmokeAdapter,
    EvidenceLedger,
    QualificationError,
    QualificationRunner,
    RunnerOptions,
    ServiceSmokeAdapter,
    build_plan,
    load_policy,
)
from .qualification_fixtures import FixtureError, FixtureRegistry


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Inventory and sequentially qualify one- and two-Spark public recipes "
            "through durable controller operations. Preview is the default."
        )
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        default=Path("qualification-evidence.jsonl"),
        help="Append-only evidence ledger (default: qualification-evidence.jsonl)",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        help="Optional additive operator policy with explicit recipe blocks",
    )
    parser.add_argument(
        "--fixture-manifest",
        type=Path,
        help="Override the checked-in digest-bound artifact qualification fixtures",
    )
    parser.add_argument(
        "--jurisdiction",
        default=os.environ.get("VONK_OPERATOR_JURISDICTION"),
        help="Uppercase ISO alpha-2 operator jurisdiction; defaults from the environment",
    )
    parser.add_argument(
        "--recipe",
        action="append",
        default=[],
        metavar="PUBLISHER/SLUG",
        help="Limit qualification to an exact recipe identity; repeatable",
    )
    parser.add_argument(
        "--node-id",
        action="append",
        default=[],
        metavar="SPARK_ID",
        help=(
            "Restrict an explicitly selected single-Spark campaign to this "
            "controller node; repeatable"
        ),
    )
    parser.add_argument(
        "--cleanup",
        choices=("none", "stop", "uninstall"),
        default="stop",
        help="Release runtime memory after every smoke; retain installs by default",
    )
    parser.add_argument("--operation-timeout-seconds", type=float, default=7_200)
    parser.add_argument("--poll-interval-seconds", type=float, default=5)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute the exact plan sequentially; otherwise only preview it",
    )
    parser.add_argument(
        "--plan-digest",
        help="Required with --apply; must match the freshly generated catalog/fleet plan",
    )
    args = parser.parse_args(argv)
    if args.apply and not args.plan_digest:
        parser.error("--apply requires --plan-digest from a fresh preview")
    if not args.apply and args.plan_digest:
        parser.error("--plan-digest is only valid with --apply")
    return args


@contextmanager
def _ledger_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise QualificationError("qualification lock cannot be opened safely")
    descriptor = os.open(
        lock_path,
        os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | no_follow,
        0o600,
    )
    try:
        metadata = os.fstat(descriptor)
        getuid = getattr(os, "getuid", None)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or (getuid is not None and metadata.st_uid != getuid())
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise QualificationError("qualification lock is not private")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise QualificationError(
                f"another qualification runner owns {lock_path}"
            ) from error
        yield
    finally:
        os.close(descriptor)


def run(argv: list[str] | None = None) -> dict[str, object]:
    args = _arguments(argv)
    options = RunnerOptions(
        jurisdiction=args.jurisdiction,
        cleanup=args.cleanup,
        operation_timeout_seconds=args.operation_timeout_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
        selected_recipes=frozenset(args.recipe),
        allowed_node_ids=frozenset(args.node_id),
    )
    policy = load_policy(args.policy)
    fixtures = FixtureRegistry.packaged(args.fixture_manifest)
    client = ControlClient.from_environment()
    with _ledger_lock(args.ledger):
        ledger = EvidenceLedger(args.ledger)
        plan = build_plan(client, options, policy, fixtures)
        plan_digest = str(plan["plan_digest"])
        if not args.apply:
            ledger.append(
                "plan.generated",
                plan_digest=plan_digest,
                payload={"plan": plan},
            )
            return {"mode": "preview", **plan}
        result = QualificationRunner(
            client,
            ledger,
            options,
            artifact_smoke=ArtifactJobSmokeAdapter(fixtures),
            service_smoke=ServiceSmokeAdapter(fixtures),
        ).apply(plan, args.plan_digest)
        return {"mode": "apply", **result}


def main(argv: list[str] | None = None) -> int:
    try:
        result = run(argv)
    except (ControlClientError, FixtureError, QualificationError, OSError) as error:
        print(json.dumps({"error": str(error)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
