"""Saved profile commands and application inspection."""

from __future__ import annotations

import argparse
import shlex
import time
from collections.abc import Callable, Mapping

from ..cli_outcome import (
    operation_state,
)
from ..control_client import (
    ControlClientError,
    ControlMalformedResponse,
    validate_control_document,
)
from .common import ControllerClient, _profile_number, _quoted, _request_key
from .confirmation import _confirm_action, _require_confirmation
from .observation import (
    _bounded_timeout,
    _poll_path,
)
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
    if action is None or action == "list":
        return _poll_path(
            client,
            "/api/profile" if action == "list" else f"/api/profile/{number}",
            {},
            args,
            fetch_initial=True,
            terminal=lambda _: True,
        )
    if action == "endpoint":

        def endpoint_view(observed: object) -> None:
            try:
                validate_control_document("FleetProfileEndpointsView", observed)
            except ControlClientError:
                raise ControlMalformedResponse(
                    "profile endpoint projection is unreadable"
                ) from None

        return _poll_path(
            client,
            f"/api/profile/{number}/endpoints",
            {},
            args,
            query={"alias": args.alias} if args.alias is not None else None,
            fetch_initial=True,
            terminal=lambda _: True,
            validate=endpoint_view,
        )
    if action == "progress":
        deadline = time.monotonic() + _bounded_timeout(args)
        if args.application:
            path = f"/api/profile/applications/{args.application}"
        elif args.request_key:
            path = f"/api/profile/{number}/requests/{args.request_key}"
        else:
            path = f"/api/profile/{number}/progress"

        def initial_identity(observed: object) -> None:
            if not isinstance(observed, Mapping):
                raise ControlMalformedResponse("profile progress is unreadable")
            identity = observed.get("id")
            if not isinstance(identity, str) or not identity:
                raise ControlMalformedResponse(
                    "profile progress identity is unreadable"
                )
            if args.application and identity != args.application:
                raise ControlMalformedResponse(
                    "profile progress identifies another application"
                )
            if args.request_key and observed.get("request_key") != args.request_key:
                raise ControlMalformedResponse(
                    "profile progress identifies another request"
                )

        result = _poll_path(
            client,
            path,
            {},
            args,
            validate=initial_identity,
            terminal=lambda _: True,
            deadline=deadline,
            fetch_initial=True,
        )
        if args.observation.status != "complete":
            return result
        if not args.follow:
            return result
        # Follow the owner's explicit predecessor/successor relationship.
        # A replacement by a later intent ends this observation instead.
        chain: list[str] = []
        current = result
        while time.monotonic() < deadline:
            observed_id = str(current["id"])
            path = f"/api/profile/applications/{_quoted(observed_id)}"

            def same_application(
                observed: Mapping[str, object], expected: str = observed_id
            ) -> None:
                if observed.get("id") != expected:
                    raise ControlMalformedResponse(
                        "profile progress observation changed application identity"
                    )

            current = _poll_path(
                client,
                path,
                current,
                args,
                validate=same_application,
                deadline=deadline,
            )
            successor = current.get("superseded_by")
            if (
                operation_state(current) != "superseded"
                or not isinstance(successor, str)
                or not successor
                or successor in {*chain, observed_id}
                or getattr(getattr(args, "observation", None), "status", "complete")
                != "complete"
            ):
                break
            predecessor = current
            predecessor_observation = args.observation

            current = _poll_path(
                client,
                f"/api/profile/applications/{_quoted(successor)}",
                predecessor,
                args,
                validate=lambda observed, expected=successor: same_application(
                    observed, expected
                ),
                terminal=lambda _: True,
                deadline=deadline,
                fetch_initial=True,
                publish=False,
            )
            if current is predecessor:
                break
            if current.get("retry_of_application_id") != observed_id:
                # A later authorized intent does not continue this request.
                current = predecessor
                args.observation = predecessor_observation
                break
            chain.append(observed_id)
        else:
            current = _poll_path(
                client,
                f"/api/profile/applications/{_quoted(str(current['id']))}",
                current,
                args,
                terminal=lambda _: False,
                deadline=deadline,
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
        if args.detach or getattr(
            getattr(args, "observation", None), "status", None
        ) in {"timed_out", "interrupted"}:
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
            return _poll_path(
                client,
                f"/api/profile/{number}/preview",
                {},
                args,
                fetch_initial=True,
                fetch=lambda remaining: client.request(
                    "POST", f"/api/profile/{number}/preview", timeout_seconds=remaining
                ),
                terminal=lambda _: True,
                attempts=3,
            )
        _require_confirmation(args, "profile load")
        return _load_profile(
            args,
            client,
            factory,
            number,
            question=f"Load profile {number} with these effects?",
        )
    raise ValueError(f"unsupported profile action: {action}")
