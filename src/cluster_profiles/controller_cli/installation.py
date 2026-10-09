"""Installation reconciliation and run-switch request following."""

from __future__ import annotations

import argparse
import shlex
import time
from collections.abc import Callable, Mapping
from typing import cast

from ..cli_outcome import Submission
from ..cli_states_generated import UNKNOWN
from ..control_client import (
    ControlClientError,
    ControlForbidden,
    ControlMalformedResponse,
    ControlNotFound,
    ControlUnauthorized,
    ControlUnavailable,
    validate_control_document,
)
from .common import ControllerClient, _quoted, _request_key
from .observation import _poll_path
from .selection import _observe_selection, _selection_remaining
from .submission import _known_http_refusal_status, _submit_idempotent_request


def _run_switch_request_operation(
    send_request: Callable[..., dict[str, object]],
    request_key: str,
    *,
    installation_id: str,
) -> dict[str, object] | None:
    """Resolve one accepted Run/Switch request before considering a resubmit."""
    page = validate_control_document(
        "OperationsResponse",
        send_request(
            "GET",
            "/api/operations",
            query={"request_id": request_key, "limit": 100},
        ),
    )
    operations = page.get("operations")
    total = page.get("total")
    next_cursor = page.get("next_cursor")
    if (
        operations is None
        and total is None
        and isinstance(page.get("projection_issue"), str)
    ):
        raise ControlUnavailable(
            200,
            "request lookup observations are unreadable; accepted request membership is unknown",
            retryable=True,
            request_id=request_key,
        )
    if (
        not isinstance(operations, list)
        or type(total) is not int
        or next_cursor is not None
        or total != len(operations)
    ):
        raise ControlMalformedResponse(
            "request lookup returned an invalid operation page"
        )
    if total == 0 and not operations:
        return None
    summaries = [item for item in operations if isinstance(item, Mapping)]
    if len(summaries) != len(operations):
        raise ControlMalformedResponse("request lookup contains an invalid operation")
    owners = [item.get("owner") for item in summaries]
    if any(
        not isinstance(owner, Mapping)
        or owner.get("kind") != "job"
        or owner.get("request_id") != request_key
        or not isinstance(owner.get("id"), str)
        for owner in owners
    ):
        raise ControlMalformedResponse("request lookup has no exact durable owner")
    owner_ids = {cast(Mapping[str, object], owner).get("id") for owner in owners}
    owner_id = next(iter(owner_ids)) if len(owner_ids) == 1 else None
    parents = [
        item
        for item in summaries
        if item.get("kind") == "recipe.cleanup.v2" and item.get("id") == owner_id
    ]
    if not isinstance(owner_id, str) or len(parents) != 1:
        raise ControlMalformedResponse("request ownership projection is incomplete")
    operation_id = owner_id
    observed = validate_control_document(
        "RunSwitchOperation",
        send_request("GET", f"/api/run-switch/operations/{_quoted(operation_id)}"),
    )
    _validate_installation_reconcile_operation(
        observed,
        operation_id=operation_id,
        request_key=request_key,
        installation_id=installation_id,
    )
    return observed


def _validate_installation_reconcile_operation(
    value: Mapping[str, object],
    *,
    operation_id: str | None,
    request_key: str,
    installation_id: str,
) -> str:
    if (
        (operation_id is not None and value.get("operation_id") != operation_id)
        or value.get("request_key") != request_key
        or value.get("kind") != "recipe.cleanup.v2"
        or value.get("action") != "cleanup"
        or value.get("cleanup_mode") != "reconcile"
        or value.get("installation_id") != installation_id
    ):
        raise ControlMalformedResponse(
            "reconciliation status identifies another request"
        )
    returned_id = value.get("operation_id")
    if not isinstance(returned_id, str) or not returned_id:
        raise ControlMalformedResponse(
            "reconciliation status has no operation identity"
        )
    return returned_id


def _follow_installation_reconciliation(
    client: ControllerClient,
    operation: dict[str, object],
    args: argparse.Namespace,
    *,
    request_key: str,
) -> dict[str, object]:
    operation_id = _validate_installation_reconcile_operation(
        operation,
        operation_id=None,
        request_key=request_key,
        installation_id=cast(str, args.installation_id),
    )
    if getattr(args, "detach", False):
        return operation

    def same_operation(observed: Mapping[str, object]) -> None:
        _validate_installation_reconcile_operation(
            observed,
            operation_id=operation_id,
            request_key=request_key,
            installation_id=cast(str, args.installation_id),
        )

    return _poll_path(
        client,
        f"/api/run-switch/operations/{_quoted(operation_id)}",
        operation,
        args,
        validate=same_operation,
    )


def _recipe_installation_reconcile(
    client: ControllerClient,
    args: argparse.Namespace,
    factory: Callable[[], str],
) -> dict[str, object]:
    installation_id = cast(str, args.installation_id)
    preview_path = (
        f"/api/recipe/installations/{_quoted(installation_id)}/reconcile/preview"
    )
    apply_path = f"/api/recipe/installations/{_quoted(installation_id)}/reconcile"
    if getattr(args, "review", False):
        if (
            getattr(args, "yes", False)
            or getattr(args, "request_key", None) is not None
            or getattr(args, "detach", False)
        ):
            raise ValueError(
                "installation reconcile --review cannot be combined with consent or request flags"
            )
        args.outcome_context = "read"
        return _poll_path(
            client,
            preview_path,
            {},
            args,
            fetch_initial=True,
            attempts=3,
            terminal=lambda _: True,
            fetch=lambda remaining: validate_control_document(
                "RunSwitchPlan",
                client.request("POST", preview_path, timeout_seconds=remaining),
            ),
        )
    if not getattr(args, "yes", False):
        raise ValueError("installation reconcile requires --yes in noninteractive mode")
    key = _request_key(args, factory)

    def validate(observed: object) -> str:
        if not isinstance(observed, Mapping):
            raise ControlMalformedResponse("installation receipt is unavailable")
        return _validate_installation_reconcile_operation(
            observed,
            operation_id=None,
            request_key=key,
            installation_id=installation_id,
        )

    deadline = time.monotonic() + 3 * client.request_timeout_seconds
    args.submission = Submission(
        key,
        apply_path,
        f"/api/operations?request_id={key}",
        3 * client.request_timeout_seconds,
        action="reconcile",
    )

    def read_existing(*, read_deadline: float = deadline):
        def request(method, path, **kwargs):
            return client.request(
                method,
                path,
                timeout_seconds=min(
                    client.request_timeout_seconds, _selection_remaining(read_deadline)
                ),
                **kwargs,
            )

        return _run_switch_request_operation(
            request, key, installation_id=installation_id
        )

    def lookup(remaining: float) -> object:
        existing = read_existing(
            read_deadline=min(deadline, time.monotonic() + remaining)
        )

        if existing is None:
            raise ControlNotFound(404, "request acceptance is not yet observed")
        return existing

    # A complete owner lookup reconnects an existing same-key application;
    # incomplete projection pages are observed again rather than refused.
    try:
        existing = _observe_selection(read_existing, deadline=deadline)
    except (ControlForbidden, ControlUnauthorized):
        raise
    except ControlClientError as error:
        if _known_http_refusal_status(error) in {401, 403}:
            raise
        # Incomplete bookkeeping cannot gate the authorized request. The
        # idempotent owner judges this exact key and installation on acceptance.
        args.submission.acceptance = UNKNOWN
        existing = None
    if existing is not None:
        args.submission.operation_id = validate(existing)
        from .submission import _accepted_submission

        _accepted_submission(args, args.submission.operation_id)
        return _follow_installation_reconciliation(
            client, existing, args, request_key=key
        )

    operation = _submit_idempotent_request(
        client,
        args,
        key=key,
        path=apply_path,
        lookup=f"/api/operations?request_id={key}",
        body={"request_key": key},
        noun="recipe",
        action="reconcile",
        validate=validate,
        lookup_fetch=lookup,
        deadline=deadline,
        reconnect=shlex.join(
            [
                "vonkctl",
                "recipe",
                "installation",
                "reconcile",
                installation_id,
                "--request-key",
                key,
                "--yes",
            ]
        ),
    )
    if args.submission.operation_id is None:
        return operation
    return _follow_installation_reconciliation(client, operation, args, request_key=key)
