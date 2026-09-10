"""The current four-noun operator CLI.

The CLI deliberately speaks the same singular operator namespaces as the
Controller. It does not retain the former plural/library/admin trees: a
selector is either the exact stable selector printed by a list or an exact
friendly name accepted by the Controller.
"""

from __future__ import annotations

import argparse
import time
import urllib.parse
import uuid
from collections.abc import Callable, Mapping
from typing import Protocol

from .cli_render import progress_line

FLEET_HEALTH = ("live", "delayed", "stale", "offline")
TELEMETRY_RANGES = ("1h", "24h", "7d", "31d")


class ControllerClient(Protocol):
    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        extra_headers: Mapping[str, str] | None = None,
        query: Mapping[str, object] | None = None,
    ) -> dict[str, object]: ...


def _quoted(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _add_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--wide", action="store_true", default=argparse.SUPPRESS)


def _query(**values: object) -> dict[str, object]:
    return {
        key: value
        for key, value in values.items()
        if value not in (None, "", [], ())
    }


def _request_key(args: argparse.Namespace, factory: Callable[[], str]) -> str:
    value = getattr(args, "request_key", None) or factory()
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise ValueError("--request-key must be a UUID") from None
    return str(parsed)


def _selector(parser: argparse.ArgumentParser, name: str, *, help: str) -> None:
    parser.add_argument(name, metavar=name.upper(), help=help)


def _filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--search", default="")
    for name in ("usage", "family", "version", "quantization"):
        parser.add_argument(f"--{name}", action="append", default=[])
    parser.add_argument("--updated-since")
    parser.add_argument("--sort", choices=("updated", "name"), default="updated")
    parser.add_argument("--cursor")
    parser.add_argument("--limit", type=int, choices=range(1, 513), default=100)


def _detail_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--metrics", choices=("glance", "all"), default="glance")
    parser.add_argument("--range", choices=TELEMETRY_RANGES, default="1h")
    parser.add_argument("--device")
    parser.add_argument("--interface")
    parser.add_argument("--run")
    parser.add_argument("--capabilities", action="store_true")


def _action_flags(
    parser: argparse.ArgumentParser,
    *,
    destructive: bool = False,
    recipe_remove: bool = False,
) -> None:
    parser.add_argument("--request-key")
    parser.add_argument("--detach", action="store_true")
    if destructive:
        parser.add_argument("--yes", action="store_true")
    if recipe_remove:
        model_choice = parser.add_mutually_exclusive_group()
        model_choice.add_argument("--with-model", action="store_true")
        model_choice.add_argument("--keep-model", action="store_true")
    _add_output(parser)


def _profile_edit_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--expected-revision", type=int)
    _add_output(parser)


def add_controller_commands(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register only Fleet, Model, Recipe and Profile command namespaces."""
    fleet = commands.add_parser("fleet", help="Show and operate enrolled Sparks")
    fleet.add_argument("--watch", action="store_true")
    fleet.add_argument("--search", default="")
    fleet.add_argument("--health", action="append", choices=FLEET_HEALTH, default=[])
    fleet.add_argument("--warnings-only", action="store_true")
    fleet.add_argument("--sort", choices=("attention", "name"), default="attention")
    _add_output(fleet)
    fleet_actions = fleet.add_subparsers(dest="fleet_action", parser_class=type(fleet))

    detail = fleet_actions.add_parser("detail", help="Show one Spark")
    _selector(detail, "selector", help="Exact Spark selector or friendly name")
    _detail_filters(detail)
    detail.add_argument("--watch", action="store_true")
    detail.add_argument("--technical", action="store_true")
    _add_output(detail)
    rename = fleet_actions.add_parser("rename", help="Change a Spark friendly name")
    _selector(rename, "selector", help="Exact Spark selector or friendly name")
    rename.add_argument("new_name")
    _action_flags(rename)
    enroll = fleet_actions.add_parser("enroll", help="Create a one-time enrollment grant")
    enroll.add_argument("name")
    enroll.add_argument("--ttl-seconds", type=int, default=900)
    _add_output(enroll)
    for action_name, help_text in (
        ("re-enroll", "Replace a Spark certificate"),
        ("remove", "Revoke and remove a Spark"),
    ):
        action = fleet_actions.add_parser(action_name, help=help_text)
        _selector(action, "selector", help="Exact Spark selector or friendly name")
        _add_output(action)
        if action_name == "remove":
            action.add_argument("--yes", action="store_true")
    upgrade = fleet_actions.add_parser("upgrade", help="Install the latest signed Spark client")
    upgrade.add_argument("selector", nargs="?")
    upgrade.add_argument("--all", action="store_true")
    upgrade.add_argument(
        "--strategy", choices=("one-at-a-time", "all-at-once"), default="one-at-a-time"
    )
    _add_output(upgrade)
    loginfo = fleet_actions.add_parser(
        "loginfo", help="Read bounded Controller-collected logs"
    )
    _selector(loginfo, "selector", help="Exact Spark selector or friendly name")
    loginfo.add_argument("--since", default="15m")
    loginfo.add_argument("--lines", type=int, choices=range(1, 1001), default=100)
    loginfo.add_argument("--recipe")
    loginfo.add_argument("--source", choices=("client", "monitor", "runtime"))
    loginfo.add_argument("--follow", action="store_true")
    _add_output(loginfo)

    model = commands.add_parser("model", help="Browse and manage model cache")
    model.add_argument("--watch", action="store_true")
    _add_output(model)
    model_actions = model.add_subparsers(dest="model_action", parser_class=type(model))
    model_library = model_actions.add_parser("library", help="List published model variants")
    _filters(model_library)
    _add_output(model_library)
    model_detail = model_actions.add_parser("detail", help="Show an exact model variant")
    _selector(model_detail, "selector", help="Exact model selector or friendly name")
    model_detail.add_argument("--watch", action="store_true")
    model_detail.add_argument("--technical", action="store_true")
    _add_output(model_detail)
    model_download = model_actions.add_parser("download", help="Cache a model variant")
    _selector(model_download, "selector", help="Exact model selector or friendly name")
    _action_flags(model_download)
    model_remove = model_actions.add_parser(
        "remove", help="Cancel/remove Controller model cache"
    )
    _selector(model_remove, "selector", help="Exact model selector or friendly name")
    _action_flags(model_remove, destructive=True)

    recipe = commands.add_parser("recipe", help="Browse and manage runnable recipes")
    recipe.add_argument("--watch", action="store_true")
    _add_output(recipe)
    recipe_actions = recipe.add_subparsers(dest="recipe_action", parser_class=type(recipe))
    recipe_library = recipe_actions.add_parser("library", help="List compatible recipes")
    _filters(recipe_library)
    recipe_library.add_argument("--model", action="append", default=[])
    recipe_library.add_argument("--all-models", action="store_true")
    _add_output(recipe_library)
    recipe_detail = recipe_actions.add_parser("detail", help="Show an exact recipe")
    _selector(recipe_detail, "selector", help="Exact recipe selector or friendly name")
    recipe_detail.add_argument("--watch", action="store_true")
    recipe_detail.add_argument("--technical", action="store_true")
    _add_output(recipe_detail)
    recipe_download = recipe_actions.add_parser(
        "download", help="Cache a recipe and missing model"
    )
    _selector(recipe_download, "selector", help="Exact recipe selector or friendly name")
    _action_flags(recipe_download)
    recipe_update = recipe_actions.add_parser(
        "update", help="List or refresh cached recipe updates"
    )
    recipe_update.add_argument("selector", nargs="?")
    recipe_update.add_argument("--all", action="store_true")
    _action_flags(recipe_update)
    recipe_remove = recipe_actions.add_parser(
        "remove", help="Cancel/remove Controller recipe cache"
    )
    _selector(recipe_remove, "selector", help="Exact recipe selector or friendly name")
    _action_flags(recipe_remove, destructive=True, recipe_remove=True)

    profile = commands.add_parser("profile", help="Edit and load a whole-fleet profile")
    _add_output(profile)
    profile_actions = profile.add_subparsers(dest="profile_action", parser_class=type(profile))
    profile_list = profile_actions.add_parser("list", help="List stable numbered profiles")
    _add_output(profile_list)
    profile_name = profile_actions.add_parser("name", help="Name the selected profile")
    profile_name.add_argument("name")
    _profile_edit_flags(profile_name)
    profile_add = profile_actions.add_parser(
        "add", help="Assign a recipe to Sparks and autosave"
    )
    profile_add.add_argument("recipe_selector")
    profile_add.add_argument("--spark", action="append", required=True)
    profile_add.add_argument("--as", dest="assignment_name")
    profile_add.add_argument("--model-variant")
    _profile_edit_flags(profile_add)
    profile_remove = profile_actions.add_parser(
        "remove", help="Remove an assignment or Spark members"
    )
    profile_remove.add_argument("assignment")
    profile_remove.add_argument("--spark", action="append", default=[])
    _profile_edit_flags(profile_remove)
    profile_load = profile_actions.add_parser(
        "load", help="Apply the entire profile to the fleet"
    )
    profile_load.add_argument("--dry-run", action="store_true")
    profile_load.add_argument("--request-key")
    profile_load.add_argument("--detach", action="store_true")
    _add_output(profile_load)
    profile_progress = profile_actions.add_parser(
        "progress", help="Show the latest profile load"
    )
    profile_progress.add_argument("--follow", action="store_true")
    profile_progress.add_argument("--timeout-seconds", type=int, default=30)
    profile_progress.add_argument("--interval-seconds", type=float, default=1.0)
    _add_output(profile_progress)


def _profile_number(args: argparse.Namespace) -> int:
    number = getattr(args, "profile_number", 1)
    if type(number) is not int or number < 1:
        raise ValueError("--profile must be a positive stable profile number")
    return number


def _overview(
    client: ControllerClient, noun: str, args: argparse.Namespace
) -> dict[str, object]:
    if noun == "fleet":
        return client.request(
            "GET",
            "/api/fleet",
            query=_query(
                search=args.search,
                health=args.health,
                warnings_only=args.warnings_only,
                sort=args.sort,
            )
            or None,
        )
    if noun == "model":
        return client.request("GET", "/api/model")
    if noun == "recipe":
        return client.request("GET", "/api/recipe")
    return client.request("GET", f"/api/profile/{_profile_number(args)}")


def _fleet(
    args: argparse.Namespace,
    client: ControllerClient,
    factory: Callable[[], str],
) -> dict[str, object]:
    action = getattr(args, "fleet_action", None)
    if action is None:
        return _overview(client, "fleet", args)
    selector = getattr(args, "selector", None)
    if action == "detail":
        return client.request(
            "GET",
            f"/api/fleet/{_quoted(selector)}",
            query=_query(
                metrics=args.metrics,
                range=args.range,
                device=args.device,
                interface=args.interface,
                run=args.run,
                capabilities=args.capabilities,
                technical=args.technical,
            )
            or None,
        )
    if action == "rename":
        return client.request(
            "POST", f"/api/fleet/{_quoted(selector)}/rename", {"display_name": args.new_name}
        )
    if action == "enroll":
        return client.request(
            "POST",
            "/api/fleet/enroll",
            {"name": args.name, "ttl_seconds": args.ttl_seconds},
        )
    if action == "re-enroll":
        return client.request(
            "POST",
            f"/api/fleet/{_quoted(selector)}/re-enroll",
            None,
        )
    if action == "remove":
        return client.request(
            "POST",
            f"/api/fleet/{_quoted(selector)}/remove",
            None,
        )
    if action == "upgrade":
        if not args.selector and not args.all:
            raise ValueError("fleet upgrade requires a Spark selector or --all")
        return client.request(
            "POST",
            "/api/fleet/upgrade",
            {
                "selectors": [args.selector] if args.selector else [],
                "all": args.all,
                "strategy": args.strategy,
            },
        )
    if action == "loginfo":
        return client.request(
            "GET",
            f"/api/fleet/{_quoted(selector)}/loginfo",
            query=_query(
                since=args.since,
                lines=args.lines,
                recipe=args.recipe,
                source=args.source,
                follow=args.follow,
            )
            or None,
        )
    raise ValueError(f"unsupported fleet action: {action}")


def _library_query(args: argparse.Namespace, *, recipe: bool) -> dict[str, object]:
    values: dict[str, object] = {
        name: getattr(args, name)
        for name in (
            "search",
            "usage",
            "family",
            "version",
            "quantization",
            "updated_since",
            "sort",
            "limit",
            "cursor",
        )
    }
    if recipe:
        values.update(model=getattr(args, "model", []), all_models=args.all_models)
    return _query(**values)


def _model(
    args: argparse.Namespace,
    client: ControllerClient,
    factory: Callable[[], str],
) -> dict[str, object]:
    action = getattr(args, "model_action", None)
    if action is None:
        return _overview(client, "model", args)
    if action == "library":
        return client.request(
            "GET", "/api/model/library", query=_library_query(args, recipe=False) or None
        )
    if action == "detail":
        return client.request(
            "GET",
            f"/api/model/{_quoted(args.selector)}",
            query=_query(technical=args.technical) or None,
        )
    if action == "download":
        return client.request(
            "POST",
            f"/api/model/{_quoted(args.selector)}/download",
            {
                "schema_version": 2,
                "request_key": _request_key(args, factory),
            },
        )
    if action == "remove":
        return client.request(
            "POST",
            f"/api/model/{_quoted(args.selector)}/remove",
            {
                "schema_version": 2,
                "request_key": _request_key(args, factory),
                "yes": args.yes,
            },
        )
    raise ValueError(f"unsupported model action: {action}")


def _recipe(
    args: argparse.Namespace,
    client: ControllerClient,
    factory: Callable[[], str],
) -> dict[str, object]:
    action = getattr(args, "recipe_action", None)
    if action is None:
        return _overview(client, "recipe", args)
    if action == "library":
        return client.request(
            "GET", "/api/recipe/library", query=_library_query(args, recipe=True) or None
        )
    if action == "detail":
        return client.request(
            "GET",
            f"/api/recipe/{_quoted(args.selector)}",
            query=_query(technical=args.technical) or None,
        )
    if action == "download":
        return client.request(
            "POST",
            f"/api/recipe/{_quoted(args.selector)}/download",
            {
                "schema_version": 2,
                "request_key": _request_key(args, factory),
            },
        )
    if action == "update":
        return client.request(
            "POST",
            "/api/recipe/update",
            {
                "schema_version": 2,
                "request_key": _request_key(args, factory),
                "selectors": [args.selector] if args.selector else [],
                "all": args.all,
            },
        )
    if action == "remove":
        return client.request(
            "POST",
            f"/api/recipe/{_quoted(args.selector)}/remove",
            {
                "schema_version": 2,
                "request_key": _request_key(args, factory),
                "with_model": args.with_model and not args.keep_model,
                "yes": args.yes,
            },
        )
    raise ValueError(f"unsupported recipe action: {action}")


def _authoring_assignments(profile: Mapping[str, object]) -> list[dict[str, object]]:
    raw = profile.get("assignments", [])
    if not isinstance(raw, list):
        return []
    allowed = {
        "recipe_selector",
        "model_variant",
        "spark_ids",
        "assignment_name",
        "desired_state",
    }
    assignments: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        assignment = {key: item[key] for key in allowed if key in item}
        sparks = assignment.get("spark_ids")
        if isinstance(sparks, list):
            assignment["spark_ids"] = sorted(
                {spark for spark in sparks if isinstance(spark, str)}
            )
        assignments.append(assignment)
    return assignments


def _profile_save(
    args: argparse.Namespace,
    client: ControllerClient,
    assignments: list[dict[str, object]],
    *,
    name: str | None = None,
    current_revision: int | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {"assignments": assignments}
    if name is not None:
        body["name"] = name
    expected_revision = (
        args.expected_revision
        if args.expected_revision is not None
        else current_revision
    )
    if expected_revision is not None:
        body["expected_revision"] = expected_revision
    return client.request("PUT", f"/api/profile/{_profile_number(args)}", body)


def _profile(
    args: argparse.Namespace,
    client: ControllerClient,
    factory: Callable[[], str],
) -> dict[str, object]:
    number = _profile_number(args)
    action = getattr(args, "profile_action", None)
    if action is None:
        return _overview(client, "profile", args)
    if action == "list":
        return client.request("GET", "/api/profile")
    if action == "progress":
        path = f"/api/profile/{number}/progress"
        if not args.follow:
            return client.request("GET", path)
        deadline = time.monotonic() + max(0, min(args.timeout_seconds, 300))
        while True:
            result = client.request("GET", path)
            state = str(result.get("state", "")).casefold()
            terminal = {"succeeded", "completed", "failed", "cancelled", "partial", "blocked"}
            if state in terminal or time.monotonic() >= deadline:
                if time.monotonic() >= deadline and state not in terminal:
                    return {**result, "timed_out": True}
                return result
            time.sleep(max(0.1, min(args.interval_seconds, 30.0)))
    if action in {"name", "add", "remove"}:
        current = client.request("GET", f"/api/profile/{number}")
        assignments = _authoring_assignments(current)
        current_revision = current.get("revision")
        if type(current_revision) is not int:
            current_revision = None
        if action == "name":
            return _profile_save(
                args,
                client,
                assignments,
                name=args.name,
                current_revision=current_revision,
            )
        if action == "add":
            new_assignment: dict[str, object] = {
                "recipe_selector": args.recipe_selector,
                "spark_ids": sorted(set(args.spark)),
                "desired_state": "running",
            }
            if args.assignment_name:
                new_assignment["assignment_name"] = args.assignment_name
            if args.model_variant:
                new_assignment["model_variant"] = args.model_variant
            matching = [
                assignment
                for assignment in assignments
                if assignment.get("recipe_selector") == args.recipe_selector
                and assignment.get("assignment_name") == args.assignment_name
            ]
            if matching:
                matching[0]["spark_ids"] = sorted(
                    set(matching[0].get("spark_ids", []))
                    | set(new_assignment["spark_ids"])
                )
            else:
                assignments.append(new_assignment)
        else:
            target = args.assignment.casefold()
            kept: list[dict[str, object]] = []
            for assignment in assignments:
                identity = str(
                    assignment.get(
                        "assignment_name", assignment.get("recipe_selector", "")
                    )
                )
                if identity.casefold() != target:
                    kept.append(assignment)
                    continue
                if not args.spark:
                    continue
                remaining = [
                    spark
                    for spark in assignment.get("spark_ids", [])
                    if spark not in args.spark
                ]
                if remaining:
                    kept.append({**assignment, "spark_ids": sorted(set(remaining))})
            assignments = kept
        current_name = current.get("name")
        return _profile_save(
            args,
            client,
            assignments,
            name=current_name if isinstance(current_name, str) else None,
            current_revision=current_revision,
        )
    if action == "load":
        if args.dry_run:
            return client.request("POST", f"/api/profile/{number}/preview", {})
        return client.request(
            "POST",
            f"/api/profile/{number}/load",
            {"request_key": _request_key(args, factory)},
        )
    raise ValueError(f"unsupported profile action: {action}")


def _operation_progress_line(observed: Mapping[str, object]) -> str | None:
    if not observed:
        return None
    return progress_line(observed)


def run_controller(
    args: argparse.Namespace,
    client: ControllerClient,
    request_id_factory: Callable[[], str],
) -> dict[str, object]:
    command = getattr(args, "command", None) or "profile"
    if command == "fleet":
        return _fleet(args, client, request_id_factory)
    if command == "model":
        return _model(args, client, request_id_factory)
    if command == "recipe":
        return _recipe(args, client, request_id_factory)
    if command == "profile":
        return _profile(args, client, request_id_factory)
    raise ValueError(f"unsupported controller command: {command}")
