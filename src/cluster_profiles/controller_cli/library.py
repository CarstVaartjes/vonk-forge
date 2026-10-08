"""Model and recipe library command handling."""

from __future__ import annotations

import argparse
from collections.abc import Callable

from ..cli_artifact_jobs import (
    ArtifactJobClient,
    run_artifact_job,
)
from ..control_client import (
    ControlClientError,
    ControlHTTPError,
)
from .cache_removal import _remove_model, _remove_recipe
from .cache_submission import (
    _submit_cache_request,
    _submit_model_cancellation,
    _submit_recipe_cancellation,
    _submit_recipe_retry,
)
from .common import ControllerClient, _query, _quoted, _request_key
from .fleet import _overview
from .installation import _recipe_installation_reconcile
from .observation import _cache_progress, _follow_mutation, _poll_path, _watch_resource


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
    values["cached"] = args.cached or None
    if recipe:
        values.update(
            model=getattr(args, "model", []),
            ready=getattr(args, "ready", None),
            fits_fleet=getattr(args, "fits_fleet", None),
            sparks=getattr(args, "sparks", []),
            engine=getattr(args, "engine", []),
            creator=getattr(args, "creator", []),
        )
    return _query(**values)


def _model(
    args: argparse.Namespace,
    client: ControllerClient,
    factory: Callable[[], str],
) -> dict[str, object]:
    action = getattr(args, "model_action", None)
    if action == "progress":
        return _cache_progress(client, "model", args, factory)
    if action == "cancel":
        result = _submit_model_cancellation(client, args, factory)
        return _follow_mutation(client, "model", result, args)
    if action is None:
        return _overview(client, "model", args)
    if action == "library":
        return client.request(
            "GET",
            "/api/model/library",
            query=_library_query(args, recipe=False) or None,
        )
    if action == "detail":
        result = client.request(
            "GET",
            f"/api/model/{_quoted(args.selector)}",
            query=_query(technical=args.technical) or None,
        )
        return _watch_resource(
            client,
            f"/api/model/{_quoted(args.selector)}",
            result,
            args,
            query=_query(technical=args.technical) or None,
        )
    if action == "download":
        result = _submit_cache_request(client, "model", args, factory)
        return _follow_mutation(client, "model", result, args)
    if action == "remove":
        result = _remove_model(client, args, factory)
        if args.review:
            return result
        return _follow_mutation(client, "model", result, args)
    raise ValueError(f"unsupported model action: {action}")


def _recipe(
    args: argparse.Namespace,
    client: ControllerClient,
    factory: Callable[[], str],
) -> dict[str, object]:
    action = getattr(args, "recipe_action", None)
    if action == "progress":
        return _cache_progress(client, "recipe", args, factory)
    if action == "job":
        if not isinstance(client, ArtifactJobClient):
            raise ControlClientError(
                "artifact byte transfer is unavailable in this CLI client"
            )
        return run_artifact_job(
            args,
            client,
            factory,
            request_key=_request_key,
            quote=_quoted,
            poll=_poll_path,
        )
    if action is None:
        return _overview(client, "recipe", args)
    if action == "library":
        return client.request(
            "GET",
            "/api/recipe/library",
            query=_library_query(args, recipe=True) or None,
        )
    if action == "sync-status":
        try:
            return client.request("GET", "/api/catalog/managed-recipes/sync-status")
        except ControlHTTPError as error:
            if error.status_code != 404:
                raise
            return {"state": "never-run"}
    if action == "detail":
        result = client.request(
            "GET",
            f"/api/recipe/{_quoted(args.selector)}",
            query=_query(technical=args.technical) or None,
        )
        return _watch_resource(
            client,
            f"/api/recipe/{_quoted(args.selector)}",
            result,
            args,
            query=_query(technical=args.technical) or None,
        )
    if action == "download":
        result = _submit_cache_request(client, "recipe", args, factory)
        return _follow_mutation(client, "recipe", result, args)
    if action == "update":
        if args.all == bool(args.selector):
            raise ValueError("recipe update requires a selector or --all, but not both")
        result = _submit_cache_request(client, "recipe", args, factory)
        return _follow_mutation(client, "recipe", result, args)
    if action == "remove":
        result = _remove_recipe(client, args, factory)
        if args.review:
            return result
        return _follow_mutation(client, "recipe", result, args)
    if action == "retry":
        result = _submit_recipe_retry(client, args, factory)
        return _follow_mutation(client, "recipe", result.to_dict(), args)
    if action == "cancel":
        if not args.yes:
            raise ValueError("recipe cancel requires --yes in noninteractive mode")
        result = _submit_recipe_cancellation(client, args, factory)
        return _follow_mutation(client, "recipe", result, args)
    if action == "installation":
        if getattr(args, "installation_action", None) != "reconcile":
            raise ValueError("unsupported recipe installation action")
        return _recipe_installation_reconcile(client, args, factory)
    raise ValueError(f"unsupported recipe action: {action}")
