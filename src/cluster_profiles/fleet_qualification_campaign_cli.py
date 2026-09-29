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
import urllib.parse
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from importlib.resources import files as package_files
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .control_client import ControlClient, ControlClientError
from .fleet_qualification import (
    ArtifactJobSmokeAdapter,
    QualificationError,
    ServiceSmokeAdapter,
)
from .qualification_fixtures import FixtureError, FixtureRegistry

AUTHORITY_SCHEMA = "qualification-authority-v5.schema.json"
MANIFEST_SCHEMA = "qualification-campaign-manifest-v2.schema.json"
PROFILE_LABEL = "qualification-authority"
# Recovery modes in the order the campaign exercises them: a rank loss leaves
# the group running, a host restart takes every selected Spark down.
FAILURE_MODES = ("single-host-restart", "dual-rank-loss-recovery", "dual-host-restart")
_TERMINAL_APPLICATIONS = frozenset(
    {"succeeded", "failed", "cancelled", "waiting-for-operator"}
)
_PASSED = frozenset({"passed", "covered"})


@dataclass(frozen=True, slots=True)
class RecoveryRef:
    failure_mode: str
    representative: str
    role: str


@dataclass(frozen=True, slots=True)
class Row:
    key: str
    sequence: int
    content_sha256: str
    node_count: int
    interface: str
    recovery: tuple[RecoveryRef, ...]
    licenses: tuple[Mapping[str, object], ...]


@dataclass(frozen=True, slots=True)
class Batch:
    batch_id: str
    sequence: int
    mode: str
    recipes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Campaign:
    authority_id: str
    rows: Mapping[str, Row]
    batches: tuple[Batch, ...]
    fixtures: FixtureRegistry
    timeout_seconds: float
    poll_seconds: float


@dataclass(frozen=True, slots=True)
class Lane:
    row: Row
    node_ids: tuple[str, ...]
    alias: str
    kind: str


@cache
def _validator(name: str) -> Draft202012Validator:
    raw = package_files("cluster_profiles").joinpath("schemas", name).read_bytes()
    return Draft202012Validator(json.loads(raw))


def _read_contract(path: Path, schema: str, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        raise QualificationError(f"{label} is unreadable: {path}") from error
    error = next(_validator(schema).iter_errors(value), None)
    if error is not None:
        location = "/".join(str(part) for part in error.absolute_path)
        raise QualificationError(
            f"{label} does not match {schema} at /{location}: {error.message}"
        )
    return value


def load_campaign(manifest_path: Path) -> Campaign:
    manifest = _read_contract(manifest_path, MANIFEST_SCHEMA, "campaign manifest")
    base = manifest_path.resolve().parent
    authority = _read_contract(
        base / manifest["qualification_authority"],
        AUTHORITY_SCHEMA,
        "qualification authority",
    )
    fixtures = FixtureRegistry.load(base / manifest["fixture_manifest"])
    options = manifest.get("options", {})
    rows = {
        raw["key"]: Row(
            key=raw["key"],
            sequence=raw["sequence"],
            content_sha256=raw["content_sha256"],
            node_count=raw["node_count"],
            interface=raw["interface"],
            recovery=tuple(
                RecoveryRef(
                    ref["failure_mode"], ref["representative_recipe"], ref["role"]
                )
                for ref in raw["recovery_coverage_refs"]
            ),
            licenses=tuple(raw["model_license_refs"]),
        )
        for raw in authority["recipes"]
    }
    batches = tuple(
        Batch(
            raw["id"],
            raw["sequence"],
            raw["mode"],
            tuple(
                item["recipe"]
                for item in sorted(raw["assignments"], key=lambda item: item["lane"])
            ),
        )
        for raw in sorted(authority["batches"], key=lambda item: item["sequence"])
    )
    if any(key not in rows for batch in batches for key in batch.recipes):
        raise QualificationError("a batch names a recipe outside the authority")
    return Campaign(
        authority_id=authority["authority_id"],
        rows=rows,
        batches=batches,
        fixtures=fixtures,
        timeout_seconds=float(options.get("operation_timeout_seconds", 86_400)),
        poll_seconds=float(options.get("poll_interval_seconds", 5)),
    )


class ResultsLog:
    """One JSON line per recipe or batch result; the latest line wins."""

    def __init__(self, path: Path, authority_id: str) -> None:
        self.path = path
        self.authority_id = authority_id

    def entries(self) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        entries: list[dict[str, Any]] = []
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue  # a torn final line from an interrupted write
            if (
                isinstance(entry, dict)
                and entry.get("authority_id") == self.authority_id
            ):
                entries.append(entry)
        return entries

    def append(self, **entry: object) -> dict[str, object]:
        record = {
            "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "authority_id": self.authority_id,
            **entry,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        return record

    def latest(self, step: str, recipe: str, failure_mode: str | None = None) -> str:
        result = "pending"
        for entry in self.entries():
            if (
                entry.get("step") == step
                and entry.get("recipe") == recipe
                and entry.get("failure_mode") == failure_mode
            ):
                result = str(entry.get("status"))
        return result

    def stopped(self, batch_id: str) -> bool:
        return any(
            entry.get("step") == "stop"
            and entry.get("batch") == batch_id
            and entry.get("status") == "passed"
            for entry in self.entries()
        )


def _modes(row: Row) -> list[str]:
    declared = {ref.failure_mode for ref in row.recovery}
    return [mode for mode in FAILURE_MODES if mode in declared]


def _recipe_summary(campaign: Campaign, log: ResultsLog, key: str) -> dict[str, Any]:
    row = campaign.rows[key]
    recovery = {mode: log.latest("recover", key, mode) for mode in _modes(row)}
    smoke = log.latest("smoke", key)
    return {
        "smoke": smoke,
        "recovery": recovery,
        "qualified": smoke == "passed"
        and all(value in _PASSED for value in recovery.values()),
        # License facts are information for the operator, never a gate.
        "licenses": [
            {"model": item.get("key"), "spdx": item.get("spdx"), "url": item.get("url")}
            for item in row.licenses
        ],
    }


def _next_step(campaign: Campaign, log: ResultsLog, batch: Batch) -> str:
    summaries = [_recipe_summary(campaign, log, key) for key in batch.recipes]
    if any(item["smoke"] == "pending" for item in summaries):
        return "load"
    if any(
        item["smoke"] == "passed"
        and any(value not in _PASSED for value in item["recovery"].values())
        for item in summaries
    ):
        return "recover"
    return "stop"


def _select_batch(
    campaign: Campaign, log: ResultsLog, batch_id: str | None
) -> Batch | None:
    if batch_id is not None:
        for batch in campaign.batches:
            if batch.batch_id == batch_id:
                return batch
        raise QualificationError(f"batch {batch_id} is not in the authority")
    return next(
        (batch for batch in campaign.batches if not log.stopped(batch.batch_id)), None
    )


def status(
    campaign: Campaign, log: ResultsLog, batch: Batch | None
) -> dict[str, object]:
    recipes = {key: _recipe_summary(campaign, log, key) for key in campaign.rows}
    return {
        "status": "complete" if batch is None else "in-progress",
        "authority_id": campaign.authority_id,
        "qualified_recipe_count": sum(
            1 for item in recipes.values() if item["qualified"]
        ),
        "recipe_count": len(recipes),
        "next": None
        if batch is None
        else {
            "batch": batch.batch_id,
            "mode": batch.mode,
            "recipes": list(batch.recipes),
            "step": _next_step(campaign, log, batch),
        },
        "recipes": recipes,
    }


# Controller helpers --------------------------------------------------------


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _alias(campaign: Campaign, row: Row) -> tuple[str, str]:
    service = campaign.fixtures.service_recipes.get(row.key)
    if service is not None:
        return service.alias, "openai-service"
    if row.key in campaign.fixtures.recipes:
        return f"q{row.sequence}", "artifact-job"
    raise QualificationError(f"{row.key} has no smoke fixture")


def _profile(client: Any, number: int, authority_id: str) -> dict[str, Any]:
    profile = client.request("GET", f"/api/profile/{number}")
    labels = profile.get("labels") or {}
    if (
        profile.get("status") != "not-created"
        and labels.get(PROFILE_LABEL) != authority_id
    ):
        raise QualificationError(
            f"profile {number} is not labelled {PROFILE_LABEL}={authority_id}; "
            "use a dedicated qualification profile"
        )
    return profile


def _save_profile(
    client: Any,
    number: int,
    authority_id: str,
    assignments: Sequence[Mapping[str, object]],
) -> None:
    profile = _profile(client, number, authority_id)
    client.request(
        "PUT",
        f"/api/profile/{number}",
        {
            "name": f"Qualification {authority_id}"[:120],
            "description": f"Recipe qualification campaign {authority_id}",
            "installation_policy": "keep-cached",
            "labels": {PROFILE_LABEL: authority_id},
            "favorite": False,
            "expected_revision": profile.get("revision") or 0,
            "assignments": list(assignments),
        },
    )


def _apply_profile(
    client: Any,
    number: int,
    campaign: Campaign,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> None:
    accepted = client.request(
        "POST", f"/api/profile/{number}/load", {"request_key": str(uuid.uuid4())}
    )
    application_id = accepted.get("id")
    if not isinstance(application_id, str):
        raise QualificationError("profile load returned no application ID")
    deadline = clock() + campaign.timeout_seconds
    while True:
        application = client.request(
            "GET", f"/api/profile/applications/{_quote(application_id)}"
        )
        state = application.get("state")
        if state in _TERMINAL_APPLICATIONS:
            if state != "succeeded":
                raise QualificationError(
                    f"profile application {application_id} entered {state}: "
                    f"{application.get('status_reason')}"
                )
            return
        if clock() >= deadline:
            raise QualificationError(f"profile application {application_id} timed out")
        sleeper(campaign.poll_seconds)


def _fleet_nodes(client: Any) -> dict[str, Mapping[str, Any]]:
    fleet = client.request("GET", "/api/fleet")
    return {str(node["id"]): node for node in fleet.get("nodes", [])}


def _serving_run(nodes: Mapping[str, Mapping[str, Any]], lane: Lane) -> str | None:
    """The run ID once every rank of the lane is healthy and published."""

    presences = [
        presence
        for node_id in lane.node_ids
        for presence in (nodes.get(node_id) or {}).get("loaded", [])
        if presence.get("alias") == lane.alias
    ]
    run_ids = {presence.get("run_id") for presence in presences}
    if (
        len(presences) != lane.row.node_count
        or len(run_ids) != 1
        or any(
            presence.get("healthy") is not True
            or presence.get("route_state") != "published"
            or presence.get("run_state") != "running"
            for presence in presences
        )
    ):
        return None
    run_id = run_ids.pop()
    return run_id if isinstance(run_id, str) else None


def _wait_serving(
    client: Any,
    lane: Lane,
    campaign: Campaign,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> str:
    deadline = clock() + campaign.timeout_seconds
    while (run_id := _serving_run(_fleet_nodes(client), lane)) is None:
        if clock() >= deadline:
            raise QualificationError(
                f"{lane.row.key} did not become healthy and published"
            )
        sleeper(campaign.poll_seconds)
    return run_id


def _smoke(
    client: Any,
    lane: Lane,
    run_id: str,
    campaign: Campaign,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
    number: int,
) -> dict[str, object]:
    if lane.kind == "openai-service":
        return ServiceSmokeAdapter(campaign.fixtures).run(
            client,
            number,
            lane.alias,
            recipe_key=lane.row.key,
            recipe_content_sha256=lane.row.content_sha256,
        )
    return ArtifactJobSmokeAdapter(campaign.fixtures).run(
        client,
        run_id,
        recipe_key=lane.row.key,
        recipe_content_sha256=lane.row.content_sha256,
        interface=lane.row.interface,
        timeout_seconds=campaign.timeout_seconds,
        poll_interval_seconds=campaign.poll_seconds,
        clock=clock,
        sleeper=sleeper,
    )


def _smoke_lanes(
    client: Any,
    lanes: Sequence[Lane],
    campaign: Campaign,
    log: ResultsLog,
    batch: Batch,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
    number: int,
    **entry: object,
) -> list[dict[str, object]]:
    """Wait for and smoke every lane concurrently; record one line per lane."""

    def one(lane: Lane) -> dict[str, object]:
        try:
            run_id = _wait_serving(client, lane, campaign, clock, sleeper)
            result = _smoke(client, lane, run_id, campaign, clock, sleeper, number)
        except (ControlClientError, FixtureError, QualificationError, OSError) as error:
            return log.append(
                batch=batch.batch_id,
                recipe=lane.row.key,
                status="failed",
                error=str(error)[:1024],
                **entry,
            )
        return log.append(
            batch=batch.batch_id,
            recipe=lane.row.key,
            status="passed",
            run_id=run_id,
            node_ids=list(lane.node_ids),
            result=result,
            **entry,
        )

    with ThreadPoolExecutor(max_workers=max(1, len(lanes))) as executor:
        return list(executor.map(one, lanes))


# Steps ---------------------------------------------------------------------


def _lanes_from_sparks(
    campaign: Campaign, batch: Batch, sparks: Sequence[str]
) -> list[Lane]:
    rows = [campaign.rows[key] for key in batch.recipes]
    needed = sum(row.node_count for row in rows)
    if len(sparks) != needed or len(set(sparks)) != len(sparks):
        raise QualificationError(
            f"{batch.batch_id} needs {needed} distinct --spark IDs in lane order"
        )
    lanes: list[Lane] = []
    offset = 0
    for row in rows:
        alias, kind = _alias(campaign, row)
        nodes = tuple(sparks[offset : offset + row.node_count])
        lanes.append(Lane(row, nodes, alias, kind))
        offset += row.node_count
    return lanes


def _lanes_from_profile(
    client: Any, number: int, campaign: Campaign, batch: Batch
) -> list[Lane]:
    profile = _profile(client, number, campaign.authority_id)
    assigned = {
        item.get("recipe_selector"): tuple(item.get("spark_ids") or ())
        for item in profile.get("assignments") or []
    }
    if set(assigned) != set(batch.recipes):
        raise QualificationError(
            f"profile {number} does not hold {batch.batch_id}; load it first"
        )
    lanes = []
    for key in batch.recipes:
        row = campaign.rows[key]
        alias, kind = _alias(campaign, row)
        lanes.append(Lane(row, assigned[key], alias, kind))
    return lanes


def load(
    client: Any,
    campaign: Campaign,
    log: ResultsLog,
    batch: Batch,
    number: int,
    sparks: Sequence[str],
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> dict[str, object]:
    lanes = _lanes_from_sparks(campaign, batch, sparks)
    for lane in lanes:
        detail = client.request("GET", f"/api/recipe/{_quote(lane.row.key)}")
        current = (detail.get("identity") or {}).get("content_sha256")
        if current != lane.row.content_sha256:
            raise QualificationError(
                f"the Controller library holds another {lane.row.key} document "
                f"({current}); sync the library release the authority names"
            )
    _save_profile(
        client,
        number,
        campaign.authority_id,
        [
            {
                "recipe_selector": lane.row.key,
                "spark_ids": sorted(lane.node_ids),
                "assignment_name": lane.alias,
                "desired_state": "running",
            }
            for lane in lanes
        ],
    )
    _apply_profile(client, number, campaign, clock, sleeper)
    results = _smoke_lanes(
        client, lanes, campaign, log, batch, clock, sleeper, number, step="smoke"
    )
    return {"step": "load", "batch": batch.batch_id, "results": results}


def _boot_id(node: Mapping[str, Any]) -> str | None:
    sample = (node.get("telemetry") or {}).get("sample") or {}
    value = sample.get("boot_id")
    return value if isinstance(value, str) and value else None


def _online(node: Mapping[str, Any]) -> bool:
    return (node.get("connection") or {}).get("online_state") == "online"


def _wait_disruption(
    client: Any,
    baseline: Mapping[str, str | None],
    campaign: Campaign,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> None:
    """Wait until every baseline Spark rebooted or was seen offline."""

    targets = set(baseline)
    disrupted: set[str] = set()
    deadline = clock() + campaign.timeout_seconds
    while True:
        nodes = _fleet_nodes(client)
        for node_id in targets:
            node = nodes.get(node_id) or {}
            boot_id, before = _boot_id(node), baseline.get(node_id)
            if not _online(node) or (boot_id and before and boot_id != before):
                disrupted.add(node_id)
        if disrupted >= targets:
            return
        if clock() >= deadline:
            missing = sorted(targets - disrupted)
            raise QualificationError(f"no restart or outage of {missing} was observed")
        sleeper(campaign.poll_seconds)


def _recovery_targets(
    mode: str, lanes: Sequence[Lane], failure_spark: str | None
) -> list[str]:
    if mode != "dual-rank-loss-recovery":
        return sorted({node_id for lane in lanes for node_id in lane.node_ids})
    (lane,) = lanes
    if failure_spark is None:
        return [lane.node_ids[-1]]
    if failure_spark not in lane.node_ids:
        raise QualificationError("--failure-spark is not one of the lane's Sparks")
    return [failure_spark]


def recover(
    client: Any,
    campaign: Campaign,
    log: ResultsLog,
    batch: Batch,
    number: int,
    failure_spark: str | None,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
    notify: Callable[[dict[str, object]], None],
) -> dict[str, object]:
    lanes = _lanes_from_profile(client, number, campaign, batch)
    results: list[dict[str, object]] = []
    for mode in FAILURE_MODES:
        pending: list[Lane] = []
        for lane in lanes:
            if (
                mode not in _modes(lane.row)
                or log.latest("smoke", lane.row.key) != "passed"
                or log.latest("recover", lane.row.key, mode) in _PASSED
            ):
                continue
            ref = next(ref for ref in lane.row.recovery if ref.failure_mode == mode)
            if (
                ref.role == "shared-member"
                and log.latest("recover", ref.representative, mode) == "passed"
            ):
                results.append(
                    log.append(
                        step="recover",
                        failure_mode=mode,
                        batch=batch.batch_id,
                        recipe=lane.row.key,
                        status="covered",
                        covered_by=ref.representative,
                    )
                )
                continue
            pending.append(lane)
        if not pending:
            continue
        targets = _recovery_targets(mode, pending, failure_spark)
        nodes = _fleet_nodes(client)
        baseline = {node_id: _boot_id(nodes.get(node_id) or {}) for node_id in targets}
        notify(
            {
                "checkpoint": mode,
                "batch": batch.batch_id,
                "recipes": [lane.row.key for lane in pending],
                "action": (
                    "take this Spark offline and bring it back"
                    if mode == "dual-rank-loss-recovery"
                    else "restart these Sparks"
                ),
                "sparks": targets,
            }
        )
        _wait_disruption(client, baseline, campaign, clock, sleeper)
        results.extend(
            _smoke_lanes(
                client,
                pending,
                campaign,
                log,
                batch,
                clock,
                sleeper,
                number,
                step="recover",
                failure_mode=mode,
            )
        )
    return {"step": "recover", "batch": batch.batch_id, "results": results}


def stop(
    client: Any,
    campaign: Campaign,
    log: ResultsLog,
    batch: Batch,
    number: int,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> dict[str, object]:
    _save_profile(client, number, campaign.authority_id, [])
    _apply_profile(client, number, campaign, clock, sleeper)
    record = log.append(
        step="stop",
        batch=batch.batch_id,
        status="passed",
        qualified={
            key: _recipe_summary(campaign, log, key)["qualified"]
            for key in batch.recipes
        },
    )
    return {"step": "stop", "batch": batch.batch_id, "result": record}


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
    campaign = load_campaign(args.manifest)
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
    except (ControlClientError, FixtureError, QualificationError, OSError) as error:
        print(
            json.dumps({"status": "failed", "error": str(error)[:1024]}),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":"), default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
