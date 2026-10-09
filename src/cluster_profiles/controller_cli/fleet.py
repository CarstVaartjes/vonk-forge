"""Fleet inspection, enrollment delivery, and node commands."""

from __future__ import annotations

import argparse
import re
import time
from collections.abc import Callable, Mapping
from typing import cast

from ..cli_files import PrivateOutput
from ..cli_outcome import (
    EnrollmentDeliveryError,
)
from ..cli_select import SelectorError
from ..cli_states import (
    OPERATOR_WAIT_STATES,
)
from ..control_client import (
    ControlClientError,
    ControlForbidden,
    ControlMalformedResponse,
    ControlNotFound,
    ControlUnauthorized,
    validate_control_document,
)
from .common import (
    ControllerClient,
    _log_since,
    _profile_number,
    _query,
    _quoted,
    _request_key,
)
from .confirmation import _confirm_action
from .observation import (
    _TERMINAL_STATES,
    _bounded_timeout,
    _follow_loginfo,
    _poll_path,
    _state,
    _watch_resource,
)
from .profile_load import _submit_fleet_upgrade
from .selection import _resolve_spark_selectors, _selection_remaining
from .submission import _known_http_refusal_status


def _overview(
    client: ControllerClient, noun: str, args: argparse.Namespace
) -> dict[str, object]:
    if noun == "fleet":
        return client.request("GET", "/api/fleet")
    if noun == "model":
        return client.request("GET", "/api/model/library", query={"cached": True})
    if noun == "recipe":
        return client.request("GET", "/api/recipe/library", query={"cached": True})
    return client.request("GET", f"/api/profile/{_profile_number(args)}")


def _fleet_selector(args: argparse.Namespace) -> str:
    """Return the validated Spark selector of a fleet action that requires one."""

    selector = getattr(args, "selector", None)
    if not isinstance(selector, str):
        raise TypeError("fleet action requires a Spark selector")
    return selector


def _deliver_enrollment(
    args: argparse.Namespace, client: ControllerClient, factory: Callable[[], str]
) -> dict[str, object]:
    identity = _request_key(args, factory)
    payload: dict[str, object] = {"request_key": identity}
    target: dict[str, object]
    if args.fleet_action == "enroll":
        payload.update(name=args.name)
        validate_control_document("FleetEnrollRequest", payload)
        path = "/api/fleet/enroll"
        target = {"display_name": args.name}
    else:
        validate_control_document("FleetReenrollRequest", payload)
        node = client.request("GET", f"/api/fleet/{_quoted(_fleet_selector(args))}")
        node_id = node.get("id")
        if (
            not isinstance(node_id, str)
            or re.fullmatch(r"spk_[0-9a-f]{32}", node_id) is None
        ):
            raise ValueError("Spark response has no canonical identity")
        target = {"node_id": node_id, "display_name": node.get("display_name", node_id)}
        _confirm_action(
            args,
            f"Authorize a replacement certificate for {node.get('display_name', node_id)} ({node_id})? "
            "The one-time grant permits replacing this Spark identity when consumed.",
        )
        path = f"/api/fleet/{node_id}/re-enroll"
    status_path = f"/api/fleet/enrollments/{identity}"
    receipt: dict[str, object] = {
        "id": identity,
        **target,
        "delivery": {"status": "pending"},
        "output": str(args.output.absolute()),
        "recovery": [
            f"vonkctl fleet enrollment status {identity}",
            f"vonkctl fleet enrollment revoke {identity} --yes",
        ],
    }
    issued = False
    submission_started = False
    try:
        with PrivateOutput(args.output) as destination:
            # This durable, nonsecret receipt identifies a request even if the
            # process dies before the Controller's response reaches it.
            destination.write(receipt)
            destination.retain_on_failure = True
            try:
                submission_started = True
                response = validate_control_document(
                    "FleetActionResponse", client.request("POST", path, payload)
                )
                grant = validate_control_document(
                    "EnrollmentGrantResponse", response.get("grant")
                )
                if grant["id"] != identity:
                    raise ValueError("issued grant identity does not match the request")
                issued = True
                destination.write({"id": identity, **grant})
                # Closing is part of delivery: a failed flush is reconciled here too.
                destination.stream.close()
                return {
                    **receipt,
                    "delivery": {"status": "delivered"},
                    "expires_at": grant["expires_at"],
                    "purpose": grant["purpose"],
                    "recovery": [f"vonkctl fleet enrollment status {identity}"],
                }
            except (
                ControlClientError,
                OSError,
                TypeError,
                ValueError,
                KeyboardInterrupt,
            ) as error:
                receipt["delivery"] = {
                    "status": "interrupted"
                    if isinstance(error, KeyboardInterrupt)
                    else "failed"
                }
                receipt["error"] = (
                    "Enrollment grant delivery was not confirmed; inspect the original grant before creating another."
                )
                receipt["error_type"] = "enrollment_delivery"
                receipt["cause"] = type(error).__name__
                refusal_status = (
                    _known_http_refusal_status(error)
                    if isinstance(error, ControlClientError)
                    else None
                )
                if not issued and refusal_status is not None:
                    if isinstance(error, (ControlForbidden, ControlUnauthorized)):
                        receipt["error"] = "Controller authorization denied enrollment."
                        receipt["reconciliation"] = "issuance denied"
                    else:
                        receipt["error"] = (
                            f"Controller refused enrollment (HTTP {refusal_status})."
                        )
                        receipt["reconciliation"] = "issuance refused"
                else:
                    try:
                        observed = validate_control_document(
                            "EnrollmentGrantStatus",
                            client.request("GET", status_path, timeout_seconds=5),
                        )
                        receipt["grant_status"] = observed
                        if issued and observed["state"] == "pending":
                            receipt["grant_status"] = validate_control_document(
                                "EnrollmentGrantStatus",
                                client.request(
                                    "POST", status_path + "/revoke", timeout_seconds=5
                                ),
                            )
                    except (
                        ControlClientError,
                        OSError,
                        TypeError,
                        ValueError,
                        KeyboardInterrupt,
                    ):
                        receipt["reconciliation"] = "unconfirmed"
                try:
                    destination.write(receipt)
                except (OSError, TypeError, ValueError):
                    receipt["output_status"] = "unusable; inspect grant status"
                raise EnrollmentDeliveryError(
                    receipt, isinstance(error, KeyboardInterrupt)
                ) from None
    except (OSError, KeyboardInterrupt) as error:
        receipt["delivery"] = {
            "status": "unconfirmed" if submission_started else "not_issued"
        }
        receipt["error_type"] = "enrollment_delivery"
        receipt["cause"] = type(error).__name__
        if submission_started:
            receipt["error"] = (
                "Enrollment delivery was interrupted; inspect the original grant."
            )
            receipt["reconciliation"] = "unconfirmed"
        else:
            receipt["error"] = (
                "Could not prepare the private output file. Choose a new writable "
                "destination; enrollment was not attempted."
            )
            receipt["reconciliation"] = "issuance not attempted"
            receipt["recovery"] = []
        raise EnrollmentDeliveryError(
            receipt, isinstance(error, KeyboardInterrupt)
        ) from None


def _fleet(
    args: argparse.Namespace,
    client: ControllerClient,
    factory: Callable[[], str],
) -> dict[str, object]:
    action = getattr(args, "fleet_action", None)
    if action == "enrollment":
        path = f"/api/fleet/enrollments/{args.grant_id}"
        if args.enrollment_action == "revoke":
            _confirm_action(args, f"Revoke unused enrollment grant {args.grant_id}?")
            return client.request("POST", path + "/revoke")
        return client.request("GET", path)
    if action == "progress":
        path = f"/api/jobs/{_quoted(args.job_id)}"

        def same_job(observed: Mapping[str, object]) -> None:
            if observed.get("id") != args.job_id:
                raise ControlMalformedResponse(
                    "fleet progress observation identifies another job"
                )

        return _poll_path(
            client,
            path,
            {},
            args,
            validate=same_job,
            fetch_initial=True,
            terminal=None if args.follow else lambda _: True,
        )
    if action == "activity":
        query = _query(
            cursor=args.cursor,
            limit=args.limit,
            state=args.state,
            node_id=args.target,
            request_id=args.request_id,
        )
        return client.request("GET", "/api/operations", query=query or None)
    if action == "locks":
        return client.request("GET", "/api/fleet/locks")
    if action == "evidence":
        operation_id = _quoted(args.operation_id)
        attempt = args.attempt
        if attempt is None:
            try:
                attempt = client.request("GET", f"/api/operations/{operation_id}").get(
                    "attempt"
                )
            except ControlNotFound:
                # Jobs that Activity does not project (a recipe image build, a
                # run/switch parent) are still readable as jobs, and their
                # failure evidence answers to the job's current attempt.
                attempt = client.request("GET", f"/api/jobs/{operation_id}").get(
                    "current_attempt"
                )
        if type(attempt) is not int or attempt < 0:
            raise ValueError("operation has no attempt; pass --attempt")
        with PrivateOutput(args.output) as destination:
            bundle = client.request(
                "GET",
                f"/api/operations/{operation_id}/evidence",
                query={"attempt": attempt},
            )
            destination.write(bundle)
        return {
            "operation_id": args.operation_id,
            "attempt": attempt,
            "output": str(args.output.absolute()),
        }
    if action == "resume":
        job_id = args.job_id
        path = f"/api/jobs/{_quoted(job_id)}"
        current = client.request("GET", path)
        recovery = current.get("recovery")
        actions = recovery.get("actions") if isinstance(recovery, Mapping) else None
        if (
            current.get("id") != job_id
            or current.get("state") not in OPERATOR_WAIT_STATES
            or not isinstance(actions, list)
            or "resume" not in actions
        ):
            raise ValueError(
                f"job {job_id} has no currently advertised authorized resume action"
            )
        _confirm_action(
            args, f"Resume job {job_id} using its advertised recovery action?"
        )
        accepted = client.request(
            "POST",
            f"{path}/resume",
            {"disposition": "resume"},
        )
        if accepted.get("id") != job_id:
            raise ControlMalformedResponse(
                "fleet resume receipt identifies another job"
            )
        return accepted
    if action is None:
        return _overview(client, "fleet", args)
    if action == "detail":
        selector = _fleet_selector(args)
        query = _query(technical=args.technical) or None
        result = client.request(
            "GET",
            f"/api/fleet/{_quoted(selector)}",
            query=query,
        )
        return _watch_resource(
            client, f"/api/fleet/{_quoted(selector)}", result, args, query=query
        )
    if action == "rename":
        return client.request(
            "POST",
            f"/api/fleet/{_quoted(_fleet_selector(args))}/rename",
            {"display_name": args.new_name},
        )
    if action in {"enroll", "re-enroll"}:
        return _deliver_enrollment(args, client, factory)
    if action == "remove":
        deadline = time.monotonic() + 30
        node_id = _resolve_spark_selectors(
            client, [_fleet_selector(args)], deadline=deadline
        )[0]
        if re.fullmatch(r"spk_[0-9a-f]{32}", node_id) is None:
            raise ControlMalformedResponse(
                "fleet removal target has no canonical Spark identity"
            )
        node = client.request(
            "GET",
            f"/api/fleet/{_quoted(node_id)}",
            timeout_seconds=_selection_remaining(deadline),
        )
        _selection_remaining(deadline)
        if node.get("id") != node_id:
            raise ControlMalformedResponse(
                "fleet removal detail identifies another Spark"
            )
        display_name = node.get("display_name")
        if not isinstance(display_name, str) or not display_name.strip():
            display_name = node_id
        _confirm_action(
            args,
            f"Revoke and remove Spark {display_name} ({node_id}) from the fleet?",
        )
        result = client.request(
            "POST",
            f"/api/fleet/{_quoted(node_id)}/remove",
            None,
        )
        if result.get("action") != "remove" or result.get("node_id") != node_id:
            raise ControlMalformedResponse(
                "fleet removal receipt identifies another Spark"
            )
        return result
    if action == "upgrade":
        if args.all == bool(args.selector):
            raise ValueError("fleet upgrade requires either a Spark selector or --all")
        if args.all:
            scope = "all Sparks in the current fleet"
        else:
            resolved = _resolve_spark_selectors(
                client,
                [cast(str, args.selector)],
                deadline=time.monotonic() + _bounded_timeout(args),
            )
            if len(resolved) != 1:
                raise SelectorError(
                    "fleet upgrade selector did not resolve to one Spark"
                )
            args.selector = resolved[0]
            scope = f"Spark {args.selector}"
        _confirm_action(args, f"Upgrade {scope} one at a time?")
        result = _submit_fleet_upgrade(args, client, factory)
        if getattr(getattr(args, "observation", None), "status", None) in {
            "timed_out",
            "interrupted",
        }:
            return result
        if args.detach:
            return result
        job_id = cast(str, result["operation_id"])

        def same_job(observed: Mapping[str, object]) -> None:
            if observed.get("action") == "upgrade":
                if (
                    observed.get("operation_id") != job_id
                    or observed.get("request_key") != args.request_key
                ):
                    raise ControlMalformedResponse(
                        "fleet upgrade receipt identifies another request"
                    )
                return
            if (
                observed.get("id") != job_id
                or observed.get("kind") != "agent-upgrade"
                or observed.get("targets") != result.get("targets")
            ):
                raise ControlMalformedResponse(
                    "fleet upgrade observation identifies another job"
                )
            operations = observed.get("operations")
            if isinstance(operations, list):
                # A Spark whose install failed retries behind its safety
                # fence while the rollout moves on; only dispatched work counts.
                active = [
                    item
                    for item in operations
                    if isinstance(item, Mapping)
                    and item.get("state") in {"queued", "running"}
                ]
                if len(active) > 1:
                    raise ControlMalformedResponse(
                        "fleet upgrade job has more than one active Spark"
                    )
            progress = observed.get("progress")
            if (
                isinstance(progress, Mapping)
                and type(progress.get("running")) is int
                and progress["running"] > 1
            ):
                raise ControlMalformedResponse(
                    "fleet upgrade job reports concurrent Spark upgrades"
                )

        if not result.get("targets") and _state(result) == "succeeded":
            # Nothing needed an upgrade: the receipt is the whole outcome and
            # there is no job progress to observe.
            return result
        args.follow = True
        args.fleet_action = "progress"
        return _poll_path(
            client,
            f"/api/jobs/{_quoted(job_id)}",
            result,
            args,
            query={"limit": 100},
            terminal=lambda observed: (
                # The submit receipt is not a job snapshot; always observe the job.
                observed.get("action") != "upgrade"
                and (
                    _state(observed) in _TERMINAL_STATES
                    or _state(observed) in OPERATOR_WAIT_STATES
                )
            ),
            validate=same_job,
        )
    if action == "loginfo":
        selector = _fleet_selector(args)
        query = _query(
            since=_log_since(args.since),
            lines=args.lines,
            recipe=args.recipe,
            source=args.source,
            follow=args.follow,
        )
        node_id = (
            selector
            if re.fullmatch(r"spk_[0-9a-f]{32}", selector) is not None
            else _resolve_spark_selectors(client, [selector])[0]
        )
        path = f"/api/fleet/{_quoted(node_id)}/loginfo"
        result = client.request("GET", path, query=query or None)
        return _follow_loginfo(client, path, result, args, query, node_id)
    raise ValueError(f"unsupported fleet action: {action}")
