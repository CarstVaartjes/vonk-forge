"""Installation reconciliation and run-switch request following."""

from __future__ import annotations

import argparse
import math
import shlex
import sys
import time
from collections.abc import Callable, Mapping
from typing import cast

from ..cli_outcome import (
    Submission,
)
from ..control_client import (
    ControlConflict,
    ControlHTTPError,
    ControlMalformedResponse,
    ControlResponseTooLarge,
    ControlTransportError,
    ControlUnavailable,
    validate_control_document,
)
from .common import ControllerClient, _quoted, _request_key
from .observation import _poll_path


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
        raise ControlConflict(409, "request UUID is already owned by another operation")
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
        return validate_control_document(
            "RunSwitchPlan", client.request("POST", preview_path)
        )
    if not getattr(args, "yes", False):
        raise ValueError(
            "installation reconcile requires --yes; review with --review first"
        )
    key = _request_key(args, factory)
    request_timeout = client.request_timeout_seconds
    if not math.isfinite(request_timeout) or request_timeout <= 0:
        raise ValueError("request timeout must be finite and positive")
    submission = Submission(
        key,
        apply_path,
        f"/api/operations?request_id={key}",
        3 * request_timeout,
        action="reconcile",
    )
    args.submission = submission
    submission_deadline = time.monotonic() + submission.timeout_seconds
    reconnect = shlex.join(
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
    )
    if not (args.global_json or getattr(args, "json", False)):
        print(
            f"Request key: {key}\nReconnect: {reconnect}", file=sys.stderr, flush=True
        )

    def request(
        method: str,
        path: str,
        body: dict[str, object] | None = None,
        *,
        query: Mapping[str, object] | None = None,
    ):
        remaining = submission_deadline - time.monotonic()
        if remaining <= 0:
            raise ControlTransportError("installation reconciliation deadline reached")
        return client.request(
            method,
            path,
            body,
            query=query,
            timeout_seconds=min(request_timeout, remaining),
        )

    # A supplied UUID may already own a completed or in-flight cleanup. Resolve
    # that exact owner before consulting mutable current evidence.
    existing = _run_switch_request_operation(
        request,
        key,
        installation_id=installation_id,
    )
    if existing is not None:
        submission.acceptance = "accepted"
        submission.operation_id = cast(str, existing["operation_id"])
        return _follow_installation_reconciliation(
            client, existing, args, request_key=key
        )

    body: dict[str, object] = {"request_key": key}

    submission.acceptance = "unknown"
    try:
        raw = request("POST", apply_path, body)
        operation = validate_control_document("RunSwitchOperation", raw)
        operation_id = _validate_installation_reconcile_operation(
            operation,
            operation_id=None,
            request_key=key,
            installation_id=installation_id,
        )
    except (ControlTransportError, ControlUnavailable, OSError) as error:
        submission.failures.append({"stage": "submit", "error": type(error).__name__})
        # A successful, fresh lookup is the only basis for either reconnecting
        # to the old operation or replaying these exact bytes once.
        existing = _run_switch_request_operation(
            request,
            key,
            installation_id=installation_id,
        )
        if existing is not None:
            operation = existing
            operation_id = cast(str, existing["operation_id"])
        else:
            raw = request("POST", apply_path, body)
            operation = validate_control_document("RunSwitchOperation", raw)
            operation_id = _validate_installation_reconcile_operation(
                operation,
                operation_id=None,
                request_key=key,
                installation_id=installation_id,
            )
    except ControlHTTPError as error:
        if error.status_code < 500:
            raise
        submission.failures.append({"stage": "submit", "error": type(error).__name__})
        existing = _run_switch_request_operation(
            request,
            key,
            installation_id=installation_id,
        )
        if existing is not None:
            operation = existing
            operation_id = cast(str, existing["operation_id"])
        else:
            raw = request("POST", apply_path, body)
            operation = validate_control_document("RunSwitchOperation", raw)
            operation_id = _validate_installation_reconcile_operation(
                operation,
                operation_id=None,
                request_key=key,
                installation_id=installation_id,
            )
    except (ControlMalformedResponse, ControlResponseTooLarge):
        # Inspect only. A malformed receipt never licenses a resubmission.
        existing = _run_switch_request_operation(
            request,
            key,
            installation_id=installation_id,
        )
        if existing is None:
            raise
        operation = existing
        operation_id = cast(str, existing["operation_id"])

    submission.acceptance = "accepted"
    submission.operation_id = operation_id
    return _follow_installation_reconciliation(client, operation, args, request_key=key)
