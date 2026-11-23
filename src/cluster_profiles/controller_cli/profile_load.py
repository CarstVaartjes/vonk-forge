"""Reviewed profile loading and fleet upgrade submission."""

from __future__ import annotations

import argparse
import re
import shlex
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import redirect_stdout
from dataclasses import dataclass, replace
from typing import cast

from ..cli_outcome import SubmissionAcceptance
from ..cli_render.dispatch import render_payload
from ..cli_states_generated import (
    PROFILE_LOAD,
    PROFILE_NOTHING_TO_CHANGE,
    PROFILE_REFUSAL_CODES,
    PROFILE_REVIEW_STALE,
    PROFILE_REVISION_CONFLICT,
)
from ..control_client.errors import (
    ControlClientError,
    ControlConflict,
    ControlMalformedResponse,
)
from ..control_client.schema import validate_control_document
from ..error_reporting import ClientCode, protocol_context
from ..generated_control.models.fleet_profile_load_request import (
    FleetProfileLoadRequest,
)
from ..generated_control.models.fleet_profile_load_review import FleetProfileLoadReview
from .common import ControllerClient, _quoted, _request_key
from .confirmation import _confirm_action
from .observation import _poll_path
from .submission import _accepted_submission, _submit_idempotent_request

_REVIEW_STALE_CODE = PROFILE_REVIEW_STALE


_MAX_REVIEW_ROUNDS = 3
_CHANGED_QUESTION = "The plan changed since your review. Load profile {number} with the current effects?"


@dataclass(frozen=True)
class ReviewedDraft:
    """An edit that exists only as a draft until the operator consents.

    ``definition`` is previewed unsaved. ``save`` writes it against the revision
    it was built on; it raises ``ControlConflict`` when the profile moved, and
    the review is then rebuilt on the newest profile.
    """

    definition: object
    save: Callable[[], None]


def _reviewed_effects_digest(preview: object) -> str:
    """Forward the Controller binding unchanged; admission belongs to its owner."""
    if not isinstance(preview, Mapping):
        raise ControlMalformedResponse("profile review binding is unreadable")
    digest = preview.get("effects_digest")
    if isinstance(digest, str):
        # This is peer metadata, validated inside the owning observation retry.
        try:
            validate_control_document(
                "FleetProfileLoadReview", {"effects_digest": digest}
            )
        except ControlClientError:
            raise ControlMalformedResponse(
                "profile review binding is unreadable"
            ) from None
        return digest
    raise ControlMalformedResponse("profile review binding is unreadable")


def _review_and_submit_profile_load(
    client: ControllerClient,
    number: int,
    args: argparse.Namespace,
    factory: Callable[[], str],
    *,
    question: str,
    review_when_confirmed: bool,
    lead: Callable[[object], Sequence[str]] | None = None,
    draft: Callable[[], ReviewedDraft | dict] | None = None,
) -> dict[str, object]:
    """Show the plan, ask, then submit a load bound to the effects just shown.

    The Controller refuses a bound load whose plan changed since the review,
    without accepting anything. The current plan is then shown and asked about
    again; the operator never consents to a plan they did not see.
    ``--yes`` skips the preview for a direct load. When
    ``review_when_confirmed`` shows a preview (``run`` does), its binding is
    still forwarded unchanged, including when confirmation was supplied.

    With ``draft`` the plan shown is the unsaved edit's. Consent saves it, then
    the load follows; a declined review sends nothing. A profile that moved
    under the edit (save conflict) or a stale review rebuilds the draft on the
    newest profile and asks again, so the newest request wins.
    """

    if args.yes and not review_when_confirmed:
        return _submit_profile_load(client, number, args, factory)
    review_attempt = 1
    while True:
        reviewed = draft() if draft is not None else None
        if isinstance(reviewed, dict):
            # The profile could not be read in time; its observation ends the run.
            return reviewed

        # Re-observe missing or unreadable bindings before asking for consent.
        # Review consent never authorizes an unbound current-plan submission.
        body = (
            None
            if reviewed is None
            else validate_control_document(
                "FleetProfilePreviewRequest", {"definition": reviewed.definition}
            )
        )

        def observe_preview(remaining: float, body=body) -> object:
            return client.request(
                "POST",
                f"/api/profile/{number}/preview",
                body,
                timeout_seconds=remaining,
                retry=False,
            )

        def validate_preview(observed: object) -> None:
            _reviewed_effects_digest(observed)

        preview = _poll_path(
            client,
            f"/api/profile/{number}/preview",
            {},
            args,
            fetch_initial=True,
            fetch=observe_preview,
            attempts=_MAX_REVIEW_ROUNDS,
            terminal=lambda _: True,
            validate=validate_preview,
        )
        if args.observation.status != "complete":
            return preview
        if reviewed is not None and preview.get("profile_revision") is not None:
            # A Controller that ignores the body previews the saved row: its
            # plan would not show this edit, so nothing is written.
            raise ControlMalformedResponse(
                "This Controller cannot preview unsaved changes; update the "
                "Controller, then run this again. Nothing was changed.",
                context=replace(
                    protocol_context(
                        operation="profile preview",
                        endpoint=f"/api/profile/{number}/preview",
                    ),
                    code=ClientCode.CONTRACT_MISMATCH,
                ),
            )
        effects_digest = _reviewed_effects_digest(preview)
        if not (getattr(args, "global_json", False) or getattr(args, "json", False)):
            with redirect_stdout(sys.stderr):
                if lead is not None:
                    for line in lead(preview):
                        print(line)
                render_payload(preview, "profile", action="preview")
        action = preview.get("next_action")
        if action == PROFILE_NOTHING_TO_CHANGE:
            # Everything already matches: nothing is submitted, and that is not a failure.
            return preview
        if action != PROFILE_LOAD:
            # A load that stops a serving workload asks, and so does one the
            # Controller did not classify.
            _confirm_action(args, question)
        try:
            if reviewed is not None:
                reviewed.save()
        except ControlConflict as error:
            # Only a moved revision is a race; any other refusal ends the run.
            if (
                error.code != PROFILE_REVISION_CONFLICT
                or review_attempt == _MAX_REVIEW_ROUNDS
            ):
                raise
            question = _CHANGED_QUESTION.format(number=number)
            review_attempt += 1
            continue
        try:
            return _submit_profile_load(
                client,
                number,
                args,
                factory,
                reviewed_effects_digest=effects_digest,
            )
        except ControlConflict as error:
            # Only a refusal reaches here: nothing was accepted. A stale review
            # is shown again; any other refusal ends the load with its detail.
            if error.code != _REVIEW_STALE_CODE or review_attempt == _MAX_REVIEW_ROUNDS:
                raise
        review_attempt += 1
        question = _CHANGED_QUESTION.format(number=number)


def _load_profile(
    args: argparse.Namespace,
    client: ControllerClient,
    factory: Callable[[], str],
    number: int,
    *,
    question: str,
    review_when_confirmed: bool = False,
    lead: Callable[[object], Sequence[str]] | None = None,
    draft: Callable[[], ReviewedDraft | dict] | None = None,
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
        lead=lead,
        draft=draft,
    )
    if getattr(getattr(args, "observation", None), "status", None) in {
        "timed_out",
        "interrupted",
    }:
        return result
    if getattr(args, "detach", False):
        return result
    if result.get("next_action") == PROFILE_NOTHING_TO_CHANGE:
        return result
    application_id = cast(str, result["id"])

    def same_application(observed: Mapping[str, object]) -> None:
        if observed.get("id") != application_id:
            raise ControlMalformedResponse(
                "profile observation identifies another load"
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
    request = FleetProfileLoadRequest(request_key=key)
    if reviewed_effects_digest is not None:
        request.review = FleetProfileLoadReview(effects_digest=reviewed_effects_digest)
    body = request.to_dict()

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

    try:
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
    except ControlConflict as error:
        if error.code in PROFILE_REFUSAL_CODES:
            raise
        return _observe_profile_load_request(client, number, args)


def _observe_profile_load_request(
    client: ControllerClient, number: int, args: argparse.Namespace
):
    # Observe only this request; a conflict does not license an effect replay.
    key = args.submission.request_key
    args.submission.acceptance = SubmissionAcceptance.UNKNOWN

    def same_request(observed: object) -> None:
        if not isinstance(observed, Mapping):
            raise ControlMalformedResponse("profile lookup is unreadable")
        identity = observed.get("id")
        if (
            observed.get("request_key") != key
            or not isinstance(identity, str)
            or not identity
        ):
            raise ControlMalformedResponse("profile lookup identifies another request")

    observed = _poll_path(
        client,
        f"/api/profile/{number}/requests/{_quoted(key)}",
        {},
        args,
        fetch_initial=True,
        attempts=_MAX_REVIEW_ROUNDS,
        terminal=lambda _: True,
        validate=same_request,
    )
    if args.observation.status == "complete":
        _accepted_submission(args, cast(str, observed["id"]))
    return observed


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
