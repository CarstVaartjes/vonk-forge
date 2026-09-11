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
from .cli_select import SelectorError

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
    for name in ("usage", "family", "version", "quantization", "publisher", "alignment"):
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


def _watch_controls(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--interval-seconds", type=float, default=1.0)


def _action_flags(
    parser: argparse.ArgumentParser,
    *,
    destructive: bool = False,
    recipe_remove: bool = False,
    followable: bool = False,
) -> None:
    parser.add_argument("--request-key")
    if followable:
        parser.add_argument("--detach", action="store_true")
        _watch_controls(parser)
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
    _watch_controls(fleet)
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
    _watch_controls(detail)
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
    _watch_controls(loginfo)
    _add_output(loginfo)

    model = commands.add_parser("model", help="Browse and manage model cache")
    model.add_argument("--watch", action="store_true")
    _watch_controls(model)
    _add_output(model)
    model_actions = model.add_subparsers(dest="model_action", parser_class=type(model))
    model_library = model_actions.add_parser("library", help="List published model variants")
    _filters(model_library)
    _add_output(model_library)
    model_detail = model_actions.add_parser("detail", help="Show an exact model variant")
    _selector(model_detail, "selector", help="Exact model selector or friendly name")
    model_detail.add_argument("--watch", action="store_true")
    _watch_controls(model_detail)
    model_detail.add_argument("--technical", action="store_true")
    _add_output(model_detail)
    model_download = model_actions.add_parser("download", help="Cache a model variant")
    _selector(model_download, "selector", help="Exact model selector or friendly name")
    _action_flags(model_download, followable=True)
    model_remove = model_actions.add_parser(
        "remove", help="Cancel/remove Controller model cache"
    )
    _selector(model_remove, "selector", help="Exact model selector or friendly name")
    _action_flags(model_remove, destructive=True, followable=True)

    recipe = commands.add_parser("recipe", help="Browse and manage runnable recipes")
    recipe.add_argument("--watch", action="store_true")
    _watch_controls(recipe)
    _add_output(recipe)
    recipe_actions = recipe.add_subparsers(dest="recipe_action", parser_class=type(recipe))
    recipe_library = recipe_actions.add_parser("library", help="List compatible recipes")
    _filters(recipe_library)
    recipe_library.add_argument("--model", action="append", default=[])
    recipe_library.add_argument("--all-models", action="store_true")
    recipe_library.add_argument(
        "--sparks", action="append", type=int, default=[], help="Exact topology node count"
    )
    _add_output(recipe_library)
    recipe_detail = recipe_actions.add_parser("detail", help="Show an exact recipe")
    _selector(recipe_detail, "selector", help="Exact recipe selector or friendly name")
    recipe_detail.add_argument("--watch", action="store_true")
    _watch_controls(recipe_detail)
    recipe_detail.add_argument("--technical", action="store_true")
    _add_output(recipe_detail)
    recipe_download = recipe_actions.add_parser(
        "download", help="Cache a recipe and missing model"
    )
    _selector(recipe_download, "selector", help="Exact recipe selector or friendly name")
    _action_flags(recipe_download, followable=True)
    recipe_update = recipe_actions.add_parser(
        "update", help="List or refresh cached recipe updates"
    )
    recipe_update.add_argument("selector", nargs="?")
    recipe_update.add_argument("--all", action="store_true")
    _action_flags(recipe_update, followable=True)
    recipe_remove = recipe_actions.add_parser(
        "remove", help="Cancel/remove Controller recipe cache"
    )
    _selector(recipe_remove, "selector", help="Exact recipe selector or friendly name")
    _action_flags(
        recipe_remove, destructive=True, recipe_remove=True, followable=True
    )

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
    _watch_controls(profile_load)
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


_TERMINAL_STATES = {
    "succeeded",
    "completed",
    "failed",
    "partial",
    "cancelled",
    "blocked",
    "rejected",
}


def _state(value: Mapping[str, object]) -> str:
    for key in ("state", "status", "outcome"):
        candidate = value.get(key)
        if isinstance(candidate, str):
            return candidate.casefold()
    operation = value.get("operation")
    if isinstance(operation, Mapping):
        return _state(operation)
    return ""


def _watch_callback(args: argparse.Namespace) -> Callable[[Mapping[str, object]], None] | None:
    callback = getattr(args, "_watch_callback", None)
    return callback if callable(callback) else None


def _bounded_timeout(args: argparse.Namespace) -> float:
    return max(0.0, min(float(getattr(args, "timeout_seconds", 30)), 300.0))


def _bounded_interval(args: argparse.Namespace) -> float:
    return max(0.01, min(float(getattr(args, "interval_seconds", 1.0)), 30.0))


def _poll_path(
    client: ControllerClient,
    path: str,
    initial: dict[str, object],
    args: argparse.Namespace,
    *,
    query: Mapping[str, object] | None = None,
    until_state: bool = True,
) -> dict[str, object]:
    """Observe a bounded durable snapshot, retaining the last truthful value."""
    callback = _watch_callback(args)
    current = initial
    deadline = time.monotonic() + _bounded_timeout(args)
    while True:
        if callback is not None:
            callback(current)
        if until_state and _state(current) in _TERMINAL_STATES:
            return current
        if time.monotonic() >= deadline:
            return {**current, "timed_out": True}
        time.sleep(_bounded_interval(args))
        current = client.request("GET", path, query=query)


def _follow_mutation(
    client: ControllerClient,
    noun: str,
    result: dict[str, object],
    args: argparse.Namespace,
) -> dict[str, object]:
    """Follow a model/recipe mutation through its noun-owned operation view."""
    if getattr(args, "detach", False):
        return result
    operation_id = result.get("operation_id")
    if not isinstance(operation_id, str) or not operation_id:
        return result
    path = f"/api/{noun}/operations/{_quoted(operation_id)}"
    return _poll_path(client, path, result, args)


def _watch_resource(
    client: ControllerClient,
    path: str,
    result: dict[str, object],
    args: argparse.Namespace,
    *,
    query: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if not getattr(args, "watch", False):
        return result
    return _poll_path(client, path, result, args, query=query)


def _follow_loginfo(
    client: ControllerClient,
    path: str,
    result: dict[str, object],
    args: argparse.Namespace,
    query: Mapping[str, object],
) -> dict[str, object]:
    if not getattr(args, "follow", False):
        return result
    current = result
    callback = _watch_callback(args)
    deadline = time.monotonic() + _bounded_timeout(args)
    while True:
        if callback is not None:
            callback(current)
        state = _state(current)
        if current.get("complete") is True or current.get("closed") is True or state in _TERMINAL_STATES:
            return current
        if time.monotonic() >= deadline:
            return {**current, "timed_out": True}
        time.sleep(_bounded_interval(args))
        current = client.request("GET", path, query=query)


def result_exit_code(result: Mapping[str, object]) -> int:
    """Map durable operation outcomes to shell semantics."""
    if result.get("timed_out") is True:
        return 2
    state = _state(result)
    if state == "partial":
        return 1
    if state in {"failed", "blocked", "cancelled", "rejected"}:
        return 2
    return 0


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
        return _watch_resource(client, "/api/fleet", _overview(client, "fleet", args), args)
    selector = getattr(args, "selector", None)
    if action == "detail":
        result = client.request(
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
        return _watch_resource(
            client, f"/api/fleet/{_quoted(selector)}", result, args
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
        path = f"/api/fleet/{_quoted(selector)}/loginfo"
        query = _query(
            since=args.since,
            lines=args.lines,
            recipe=args.recipe,
            source=args.source,
            follow=args.follow,
        )
        result = client.request("GET", path, query=query or None)
        return _follow_loginfo(client, path, result, args, query)
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
            "publisher",
            "alignment",
            "updated_since",
            "sort",
            "limit",
            "cursor",
        )
    }
    if recipe:
        values.update(
            model=getattr(args, "model", []),
            all_models=args.all_models,
            sparks=getattr(args, "sparks", []),
        )
    return _query(**values)


def _model(
    args: argparse.Namespace,
    client: ControllerClient,
    factory: Callable[[], str],
) -> dict[str, object]:
    action = getattr(args, "model_action", None)
    if action is None:
        return _watch_resource(client, "/api/model", _overview(client, "model", args), args)
    if action == "library":
        return client.request(
            "GET", "/api/model/library", query=_library_query(args, recipe=False) or None
        )
    if action == "detail":
        result = client.request(
            "GET",
            f"/api/model/{_quoted(args.selector)}",
            query=_query(technical=args.technical) or None,
        )
        return _watch_resource(
            client, f"/api/model/{_quoted(args.selector)}", result, args,
            query=_query(technical=args.technical) or None,
        )
    if action == "download":
        result = client.request(
            "POST",
            f"/api/model/{_quoted(args.selector)}/download",
            {
                "schema_version": 2,
                "request_key": _request_key(args, factory),
            },
        )
        return _follow_mutation(client, "model", result, args)
    if action == "remove":
        if not args.yes:
            raise ValueError("model remove requires --yes in noninteractive mode")
        result = client.request(
            "POST",
            f"/api/model/{_quoted(args.selector)}/remove",
            {
                "schema_version": 2,
                "request_key": _request_key(args, factory),
            },
        )
        return _follow_mutation(client, "model", result, args)
    raise ValueError(f"unsupported model action: {action}")


def _recipe(
    args: argparse.Namespace,
    client: ControllerClient,
    factory: Callable[[], str],
) -> dict[str, object]:
    action = getattr(args, "recipe_action", None)
    if action is None:
        return _watch_resource(client, "/api/recipe", _overview(client, "recipe", args), args)
    if action == "library":
        return client.request(
            "GET", "/api/recipe/library", query=_library_query(args, recipe=True) or None
        )
    if action == "detail":
        result = client.request(
            "GET",
            f"/api/recipe/{_quoted(args.selector)}",
            query=_query(technical=args.technical) or None,
        )
        return _watch_resource(
            client, f"/api/recipe/{_quoted(args.selector)}", result, args,
            query=_query(technical=args.technical) or None,
        )
    if action == "download":
        result = client.request(
            "POST",
            f"/api/recipe/{_quoted(args.selector)}/download",
            {
                "schema_version": 2,
                "request_key": _request_key(args, factory),
            },
        )
        return _follow_mutation(client, "recipe", result, args)
    if action == "update":
        result = client.request(
            "POST",
            "/api/recipe/update",
            {
                "schema_version": 2,
                "request_key": _request_key(args, factory),
                "selectors": [args.selector] if args.selector else [],
                "all": args.all,
            },
        )
        return _follow_mutation(client, "recipe", result, args)
    if action == "remove":
        if not args.yes:
            raise ValueError("recipe remove requires --yes in noninteractive mode")
        if not (args.with_model or args.keep_model):
            raise ValueError("recipe remove requires --with-model or --keep-model")
        result = client.request(
            "POST",
            f"/api/recipe/{_quoted(args.selector)}/remove",
            {
                "schema_version": 2,
                "request_key": _request_key(args, factory),
                "with_model": args.with_model and not args.keep_model,
            },
        )
        return _follow_mutation(client, "recipe", result, args)
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


def _recipe_rows(client: ControllerClient) -> list[Mapping[str, object]]:
    """Load the complete recipe library for friendly local resolution."""

    rows: list[Mapping[str, object]] = []
    cursor: str | None = None
    while True:
        query: dict[str, object] = {"all_models": True, "limit": 512, "sort": "name"}
        if cursor is not None:
            query["cursor"] = cursor
        payload = client.request("GET", "/api/recipe/library", query=query)
        raw_recipes = payload.get("recipes")
        if not isinstance(raw_recipes, list):
            raise TypeError("recipe library response does not contain recipes")
        rows.extend(row for row in raw_recipes if isinstance(row, Mapping))
        next_cursor = payload.get("next_cursor")
        if next_cursor is None:
            return rows
        if not isinstance(next_cursor, str) or not next_cursor:
            raise TypeError("recipe library response contains an invalid cursor")
        cursor = next_cursor


def _resolve_recipe_selector(client: ControllerClient, requested: str) -> str:
    """Resolve a canonical selector, slug, or title to publisher/slug."""

    needle = requested.strip().casefold()
    if not needle:
        raise SelectorError("recipe selector cannot be empty")
    if needle.count("/") == 1 and all(part for part in needle.split("/")):
        return needle
    matches: set[str] = set()
    for row in _recipe_rows(client):
        identity = row.get("identity")
        identity_map = identity if isinstance(identity, Mapping) else {}
        canonical = row.get("selector")
        if not isinstance(canonical, str) or not canonical:
            publisher = identity_map.get("publisher")
            slug = identity_map.get("slug")
            if isinstance(publisher, str) and isinstance(slug, str):
                canonical = f"{publisher}/{slug}"
        if not isinstance(canonical, str) or not canonical:
            raise ValueError(
                "recipe library response contains a recipe without a canonical selector"
            )
        candidates = [canonical, str(identity_map.get("slug", ""))]
        title = identity_map.get("title")
        if isinstance(title, str):
            candidates.append(title)
        if any(candidate.casefold() == needle for candidate in candidates if candidate):
            matches.add(canonical)
    if len(matches) == 1:
        return next(iter(matches))
    if not matches:
        raise SelectorError(f"unknown recipe selector: {requested}")
    candidates = tuple(sorted(matches))
    raise SelectorError(
        f"ambiguous recipe selector: {requested}; choose one of {', '.join(candidates)}",
        candidates=candidates,
    )


def _resolve_spark_selectors(
    client: ControllerClient, selectors: list[str]
) -> list[str]:
    """Resolve operator-facing Spark names to immutable node IDs."""

    if not selectors:
        return []
    payload = client.request("GET", "/api/fleet")
    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, list):
        raise TypeError("fleet response does not contain nodes")
    nodes = [node for node in raw_nodes if isinstance(node, Mapping)]
    resolved: list[str] = []
    for requested in selectors:
        needle = requested.strip().casefold()
        if not needle:
            raise SelectorError("spark selector cannot be empty")
        matches = [
            node
            for node in nodes
            if any(
                isinstance(value, str) and value.casefold() == needle
                for key in ("id", "display_name", "hostname")
                for value in (node.get(key),)
            )
        ]
        if not matches:
            raise SelectorError(f"unknown spark selector: {requested}")
        if len(matches) > 1:
            candidates = tuple(
                str(node.get("display_name") or node.get("id")) for node in matches
            )
            raise SelectorError(
                f"ambiguous spark selector: {requested}; choose one of {', '.join(candidates)}",
                candidates=candidates,
            )
        node_id = matches[0].get("id")
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("fleet response contains a Spark without an ID")
        resolved.append(node_id)
    return sorted(set(resolved))


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
        result = client.request("GET", path)
        if not args.follow:
            return result
        return _poll_path(client, path, result, args)
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
            recipe_selector = _resolve_recipe_selector(client, args.recipe_selector)
            spark_ids = _resolve_spark_selectors(client, list(args.spark))
            new_assignment: dict[str, object] = {
                "recipe_selector": recipe_selector,
                "spark_ids": spark_ids,
                "desired_state": "running",
            }
            if args.assignment_name:
                new_assignment["assignment_name"] = args.assignment_name
            if args.model_variant:
                new_assignment["model_variant"] = args.model_variant
            matching = [
                assignment
                for assignment in assignments
                if assignment.get("recipe_selector") == recipe_selector
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
            spark_ids = _resolve_spark_selectors(client, list(args.spark))
            if not any(
                str(assignment.get("assignment_name", "")).casefold() == target
                for assignment in assignments
            ):
                try:
                    target = _resolve_recipe_selector(client, args.assignment).casefold()
                except SelectorError as error:
                    if error.candidates:
                        raise
            kept: list[dict[str, object]] = []
            for assignment in assignments:
                identity = str(
                    assignment.get("assignment_name")
                    or assignment.get("recipe_selector", "")
                )
                if identity.casefold() != target:
                    kept.append(assignment)
                    continue
                if not args.spark:
                    continue
                remaining = [
                    spark
                    for spark in assignment.get("spark_ids", [])
                    if spark not in spark_ids
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
        result = client.request(
            "POST",
            f"/api/profile/{number}/load",
            {"request_key": _request_key(args, factory)},
        )
        if args.detach or not isinstance(result.get("operation_id"), str):
            return result
        return _poll_path(client, f"/api/profile/{number}/progress", result, args)
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
