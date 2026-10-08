"""Saved profile commands and application inspection."""

from __future__ import annotations

import argparse
import shlex
from collections.abc import Callable, Mapping

from ..cli_outcome import (
    operation_state,
)
from ..control_client import (
    ControlMalformedResponse,
    validate_control_document,
)
from .common import ControllerClient, _profile_number, _quoted, _request_key
from .confirmation import _confirm_action, _require_confirmation
from .fleet import _overview
from .observation import _poll_path
from .profile_authoring import _profile_authoring
from .profile_load import _load_profile
from .submission import _submit_idempotent_request


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
    if action == "endpoint":
        result = validate_control_document(
            "FleetProfileEndpointsView",
            client.profile_endpoints(number, alias=args.alias).to_dict(),
        )
        assignments = result.get("assignments")
        if (
            args.alias is not None
            and result.get("projection_issue") is None
            and (
                not isinstance(assignments, list)
                or not any(
                    isinstance(item, dict) and item.get("alias") == args.alias
                    for item in assignments
                )
            )
        ):
            raise ValueError(f"endpoint alias is not part of profile {number}")
        return result
    if action == "progress":
        if args.application:
            path = f"/api/profile/applications/{args.application}"
            selected_profile_id: str | None = None
            if getattr(args, "profile_number", None) is not None:
                selected_profile = client.request("GET", f"/api/profile/{number}")
                selected_profile_id_value = selected_profile.get("id")
                if (
                    not isinstance(selected_profile_id_value, str)
                    or not selected_profile_id_value
                ):
                    raise ControlMalformedResponse(
                        "selected profile response has no canonical identity"
                    )
                selected_profile_id = selected_profile_id_value
        elif args.request_key:
            path = f"/api/profile/{number}/requests/{args.request_key}"
            selected_profile_id = None
        else:
            path = f"/api/profile/{number}/progress"
            selected_profile_id = None
        result = client.request("GET", path)
        application_id = result.get("id")
        if not isinstance(application_id, str) or not application_id:
            raise ControlMalformedResponse(
                "profile progress response has no durable application identity"
            )
        if args.application and application_id != args.application:
            raise ControlMalformedResponse(
                "profile application response identifies another application"
            )
        if selected_profile_id is not None:
            application_profile_id = result.get("profile_id")
            if (
                not isinstance(application_profile_id, str)
                or not application_profile_id
            ):
                raise ControlMalformedResponse(
                    "profile application response has no canonical profile identity"
                )
            if application_profile_id != selected_profile_id:
                raise ValueError(
                    f"profile application does not belong to selected profile {number}"
                )
        if not args.follow:
            return result
        # An application the Controller replaced (its own automatic retry, or a
        # later intent) ends ``superseded``: follow ``superseded_by`` to the
        # application that continues the work instead of reporting an end.
        chain: list[str] = []
        current = result
        while True:
            observed_id = str(current["id"])
            path = f"/api/profile/applications/{_quoted(observed_id)}"

            def same_application(
                observed: Mapping[str, object], expected: str = observed_id
            ) -> None:
                if observed.get("id") != expected:
                    raise ControlMalformedResponse(
                        "profile progress observation changed application identity"
                    )

            current = _poll_path(client, path, current, args, validate=same_application)
            successor = current.get("superseded_by")
            if (
                operation_state(current) != "superseded"
                # Only the Controller's own retry continues the same work; a
                # later intent another request accepted is reported as ended.
                or current.get("reason_code") != "superseded-by-retry"
                or not isinstance(successor, str)
                or not successor
                or successor in chain
                or getattr(getattr(args, "observation", None), "status", "complete")
                != "complete"
            ):
                break
            chain.append(observed_id)
            current = client.request(
                "GET", f"/api/profile/applications/{_quoted(successor)}"
            )
        if chain:
            current = {**current, "supersedes_chain": chain}
        return current
    if action in {"add", "remove", "configure", "export", "import"}:
        return _profile_authoring(args, client)
    if action == "cancel":
        if not args.yes:
            raise ValueError("profile cancel requires --yes")
        _confirm_action(
            args,
            f"Cancel profile {number} application {args.application_id} and reconcile issued effects?",
        )
        request_key = _request_key(args, factory)
        application_id = args.application_id
        path = f"/api/profile/applications/{_quoted(application_id)}/cancel"
        lookup = (
            f"/api/profile/applications/{_quoted(application_id)}"
            f"/cancellations/{_quoted(request_key)}"
        )

        def same_cancellation(receipt: Mapping[str, object]) -> str:
            cancellation = receipt.get("cancellation")
            cancellation_actor = (
                cancellation.get("actor") if isinstance(cancellation, Mapping) else None
            )
            if (
                receipt.get("id") != application_id
                or not isinstance(cancellation, Mapping)
                or cancellation.get("request_key") != request_key
                or cancellation.get("cause") != "operator"
                or not isinstance(cancellation_actor, str)
                or not cancellation_actor
            ):
                raise ControlMalformedResponse(
                    "profile cancellation receipt identifies another request or owner"
                )
            return application_id

        result = _submit_idempotent_request(
            client,
            args,
            key=request_key,
            path=path,
            lookup=lookup,
            body={"profile_number": number, "request_key": request_key},
            noun="profile",
            action="cancel",
            validate=same_cancellation,
            reconnect=shlex.join(
                [
                    "vonkctl",
                    "--profile",
                    str(number),
                    "profile",
                    "cancel",
                    application_id,
                    "--yes",
                    "--request-key",
                    request_key,
                    "--detach",
                    "--json",
                ]
            ),
        )
        if args.detach:
            return result

        def validate_observed_cancellation(
            observed: Mapping[str, object],
        ) -> None:
            same_cancellation(observed)

        return _poll_path(
            client,
            f"/api/profile/applications/{_quoted(application_id)}",
            result,
            args,
            validate=validate_observed_cancellation,
        )
    if action == "load":
        if args.dry_run:
            if args.yes or args.detach:
                raise ValueError("--review cannot be combined with --yes or --detach")
            return client.request("POST", f"/api/profile/{number}/preview")
        _require_confirmation(args, "profile load")
        return _load_profile(
            args,
            client,
            factory,
            number,
            question=f"Load profile {number} with these effects?",
        )
    raise ValueError(f"unsupported profile action: {action}")
