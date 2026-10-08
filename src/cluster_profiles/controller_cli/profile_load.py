"""Reviewed profile loading and fleet upgrade submission."""

from __future__ import annotations

import argparse
import re
import shlex
import sys
from collections.abc import Callable, Mapping
from contextlib import redirect_stdout
from typing import cast

from ..cli_render import render_payload
from ..control_client import (
    ControlConflict,
    ControlMalformedResponse,
)
from .common import ControllerClient, _quoted, _request_key
from .confirmation import _confirm_action
from .observation import _poll_path
from .submission import _submit_idempotent_request

_REVIEW_STALE_CODE = "profile.review_stale"


_MAX_REVIEW_ROUNDS = 3


def _reviewed_effects_digest(preview: Mapping[str, object]) -> str:
    """Require the exact effects binding before asking for reviewed consent."""
    digest = preview.get("effects_digest")
    if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
        return digest
    raise ControlMalformedResponse("profile review has no verified effects binding")


def _review_and_submit_profile_load(
    client: ControllerClient,
    number: int,
    args: argparse.Namespace,
    factory: Callable[[], str],
    *,
    question: str,
    review_when_confirmed: bool,
) -> dict[str, object]:
    """Show the plan, ask, then submit a load bound to the effects just shown.

    The Controller refuses a bound load whose plan changed since the review,
    without accepting anything. The current plan is then shown and asked about
    again; the operator never consents to a plan they did not see. With
    ``--yes`` nothing is reviewed or bound: the plan current at acceptance is
    loaded. ``review_when_confirmed`` still shows that plan (``run`` does) and
    leaves admission to the Controller at submission.
    """

    if args.yes and not review_when_confirmed:
        return _submit_profile_load(client, number, args, factory)
    for round_number in range(1, _MAX_REVIEW_ROUNDS + 1):
        preview = client.request("POST", f"/api/profile/{number}/preview")
        effects_digest = None if args.yes else _reviewed_effects_digest(preview)
        if not (getattr(args, "global_json", False) or getattr(args, "json", False)):
            with redirect_stdout(sys.stderr):
                render_payload(preview, "profile", action="preview")
        _confirm_action(args, question)
        try:
            return _submit_profile_load(
                client,
                number,
                args,
                factory,
                reviewed_effects_digest=effects_digest,
            )
        except ControlConflict as error:
            if error.code != _REVIEW_STALE_CODE or round_number == _MAX_REVIEW_ROUNDS:
                raise
        question = (
            "The plan changed since your review. "
            f"Load profile {number} with the current effects?"
        )
    raise AssertionError("unreachable")  # pragma: no cover


def _load_profile(
    args: argparse.Namespace,
    client: ControllerClient,
    factory: Callable[[], str],
    number: int,
    *,
    question: str,
    review_when_confirmed: bool = False,
) -> dict[str, object]:
    """The one load path: review, confirm, submit, then follow the application.

    ``profile load`` and ``run`` both take it with their own namespace, so the
    submission identity, the request key and the observation they leave behind
    are what the error path reports when a response is lost.
    """

    result = _review_and_submit_profile_load(
        client,
        number,
        args,
        factory,
        question=question,
        review_when_confirmed=review_when_confirmed,
    )
    if getattr(args, "detach", False):
        return result
    application_id = result.get("id")
    if not isinstance(application_id, str) or not application_id:
        raise ControlMalformedResponse(
            "profile load has no durable application identity"
        )

    def same_application(observed: Mapping[str, object]) -> None:
        if observed.get("id") != application_id:
            raise ControlMalformedResponse(
                "profile observation identifies another application"
            )

    return _poll_path(
        client,
        f"/api/profile/applications/{_quoted(application_id)}",
        result,
        args,
        validate=same_application,
    )


def _submit_profile_load(
    client: ControllerClient,
    number: int,
    args: argparse.Namespace,
    factory: Callable[[], str],
    *,
    reviewed_effects_digest: str | None = None,
) -> dict[str, object]:
    key = _request_key(args, factory)
    path = f"/api/profile/{number}/load"
    lookup = f"/api/profile/{number}/requests/{key}"
    body: dict[str, object] = {"request_key": key}
    if reviewed_effects_digest is not None:
        body["reviewed_effects_digest"] = reviewed_effects_digest

    def validate(result: Mapping[str, object]) -> str:
        operation_id = result.get("id")
        if (
            result.get("request_key") != key
            or not isinstance(operation_id, str)
            or not operation_id
        ):
            raise ControlMalformedResponse(
                "profile load receipt identifies another request"
            )
        return operation_id

    return _submit_idempotent_request(
        client,
        args,
        key=key,
        path=path,
        lookup=lookup,
        body=body,
        noun="profile",
        action="load",
        validate=validate,
        reconnect=shlex.join(
            [
                "vonkctl",
                "--profile",
                str(number),
                "profile",
                "progress",
                "--request-key",
                key,
                "--follow",
            ]
        ),
    )


def _submit_fleet_upgrade(
    args: argparse.Namespace,
    client: ControllerClient,
    factory: Callable[[], str],
) -> dict[str, object]:
    """Submit or replay one exact Controller-authorized upgrade request."""

    key = _request_key(args, factory)
    body: dict[str, object] = {
        "all": args.all,
        "request_key": key,
    }
    if args.selector:
        body["selectors"] = [args.selector]

    def validate(result: Mapping[str, object]) -> str:
        operation_id = result.get("operation_id")
        plan_digest = result.get("plan_digest")
        targets = result.get("targets")
        if (
            result.get("action") != "upgrade"
            or result.get("request_key") != key
            or not isinstance(operation_id, str)
            or not operation_id
            or not isinstance(plan_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", plan_digest) is None
            or not isinstance(targets, list)
            or len(targets) > 64
            or not all(
                isinstance(target, str)
                and re.fullmatch(r"spk_[0-9a-f]{32}", target) is not None
                for target in targets
            )
            or len(targets) != len(set(targets))
        ):
            raise ControlMalformedResponse(
                "fleet upgrade receipt does not identify this request and plan"
            )
        return operation_id

    scope = ["--all"] if args.all else [cast(str, args.selector)]
    reconnect = shlex.join(
        [
            "vonkctl",
            "fleet",
            "upgrade",
            *scope,
            "--request-key",
            key,
            "--yes",
        ]
    )
    path = "/api/fleet/upgrade"
    return _submit_idempotent_request(
        client,
        args,
        key=key,
        path=path,
        # The upgrade route is itself the request-key lookup. Reposting the
        # identical body returns the original durable job.
        lookup=path,
        body=body,
        noun="fleet",
        action="upgrade",
        validate=validate,
        reconnect=reconnect,
    )
