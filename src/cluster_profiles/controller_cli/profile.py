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
from .common import (
    ControllerClient,
    NoLoadedProfile,
    _profile_number,
    _quoted,
    _request_key,
)
from .confirmation import _confirm_action, _require_confirmation
from .observation import (
    _bounded_timeout,
    _poll_path,
)
from .profile_authoring import _profile_authoring
from .profile_load import _load_profile
from .selection import _spark_names
from .submission import _submit_idempotent_request


def _list_profiles(client: ControllerClient, remaining: float) -> object:
    """Every saved profile, paged."""

    page = client.request("GET", "/api/profile", timeout_seconds=remaining, retry=False)
    entries = page.get("profiles")
    if not isinstance(entries, list):
        raise ControlMalformedResponse("profile listing is unreadable")
    profiles = list(entries)
    previous = 0
    after = page.get("next_after")
    while after is not None:
        if type(after) is not int or after <= previous:
            raise ControlMalformedResponse("profile listing cursor did not advance")
        previous = after
        page = client.request(
            "GET",
            "/api/profile",
            query={"after": after},
            timeout_seconds=remaining,
            retry=False,
        )
        entries = page.get("profiles")
        if not isinstance(entries, list):
            raise ControlMalformedResponse("profile listing is unreadable")
        profiles.extend(entries)
        after = page.get("next_after")
    return {**page, "profiles": profiles}


def _loaded_profile_number(listing: object) -> int | None:
    """The profile that is loaded now, from a profile listing; None when none is."""

    rows = listing.get("profiles") if isinstance(listing, Mapping) else None
    for row in rows if isinstance(rows, list) else ():
        if isinstance(row, Mapping) and row.get("loaded_revision") is not None:
            number = row.get("number")
            if type(number) is int and number >= 1:
                return number
    return None


def _profile(
    args: argparse.Namespace,
    client: ControllerClient,
    factory: Callable[[], str],
) -> dict[str, object]:
    action = getattr(args, "profile_action", None)
    if action == "endpoint" and getattr(args, "profile_number", None) is None:
        # No profile named: answer for the loaded one, read with the same
        # bounded retry as every other observation.
        profiles = _poll_path(
            client,
            "/api/profile",
            {},
            args,
            fetch_initial=True,
            fetch=lambda remaining: _list_profiles(client, remaining),
            terminal=lambda _: True,
        )
        if args.observation.status != "complete":
            return profiles
        loaded = _loaded_profile_number(profiles)
        if loaded is None:
            raise NoLoadedProfile("No profile is loaded.")
        number = loaded
    else:
        number = _profile_number(args)
    if action == "list":

        def listing(remaining: float):
            return _list_profiles(client, remaining)

        return _poll_path(
            client,
            "/api/profile",
            {},
            args,
            fetch_initial=True,
            fetch=listing,
            terminal=lambda _: True,
        )
    if action is None:
        shown = _poll_path(
            client,
            f"/api/profile/{number}",
            {},
            args,
            fetch_initial=True,
            terminal=lambda _: True,
        )
        if (
            isinstance(shown, dict)
            and "assignments" in shown
            and not (
                getattr(args, "global_json", False) or getattr(args, "json", False)
            )
        ):
            # Human output names the Sparks; the JSON keeps the ids alone.
            return {**shown, "spark_names": _spark_names(client)}
        return shown
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
        if args.load:
            path = f"/api/profile/applications/{args.load}"
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
            if args.load and identity != args.load:
                raise ControlMalformedResponse(
                    "profile progress identifies another load"
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
                        "profile progress observation changed load identity"
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
            f"Cancel profile {number} load {args.load_id} and reconcile issued effects?",
        )
        request_key = _request_key(args, factory)
        application_id = args.load_id
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
    if action == "delete":
        _confirm_action(
            args,
            f"Delete saved profile {number}? Running workloads are not stopped.",
        )
        path = f"/api/profile/{number}/delete"
        body = (
            {}
            if args.expected_revision is None
            else {"expected_revision": args.expected_revision}
        )

        def delete_validated(observed: object) -> None:
            try:
                validate_control_document("FleetProfileDeletion", observed)
            except ControlClientError:
                raise ControlMalformedResponse(
                    "profile deletion answer is unreadable"
                ) from None

        # A profile still loading is cancelled first; the same request is sent
        # again until the cancel has ended and the profile is gone.
        return _poll_path(
            client,
            path,
            {},
            args,
            fetch_initial=True,
            fetch=lambda remaining: client.request(
                "POST", path, body, timeout_seconds=remaining, retry=False
            ),
            terminal=lambda observed: observed.get("outcome") != "cancelling",
            validate=delete_validated,
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
                    "POST",
                    f"/api/profile/{number}/preview",
                    timeout_seconds=remaining,
                    retry=False,
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
