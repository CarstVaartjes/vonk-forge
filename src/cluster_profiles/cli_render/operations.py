"""Terminal presentation for operations responses."""

from __future__ import annotations

import shlex
from collections.abc import Mapping

from .. import cli_states
from .common import (
    _actions,
    _bytes,
    _failure,
    _field,
    _object,
    _optional,
    _records,
    _text,
    _time,
    _waiting,
    _warn,
    _words,
)
from .profile import _profile_endpoints
from .progress import _measured_progress, progress_line


def _run_result(payload: Mapping[str, object]) -> None:
    """A finished run is its recipe, its application, and where it is served."""

    application = payload.get("application")
    if not isinstance(application, Mapping):
        # An interrupted or timed-out run reports the application it was following.
        _application(payload)
        return
    _field("Recipe", payload.get("recipe"))
    _application(application)
    endpoints = payload.get("endpoints")
    if isinstance(endpoints, Mapping):
        _profile_endpoints(endpoints)


def _application(payload: Mapping[str, object]) -> None:
    _field("Application", payload.get("id"))
    _field("State", payload.get("state"))
    if payload.get("status_reason") is not None:
        _field("Reason", payload["status_reason"])
    if payload.get("reason_code") is not None:
        _field("Reason code", payload["reason_code"])
    if payload.get("superseded_by") is not None:
        # Not a failure: the Controller continued this work under a successor.
        _field("Continued by", payload["superseded_by"])
    chain = payload.get("supersedes_chain")
    if isinstance(chain, list) and chain:
        _field("Superseded applications", _words(chain))
    cancellation = _optional(payload.get("cancellation"), "cancellation")
    if cancellation:
        _field("Cancellation", cancellation.get("state"))
    _waiting(payload)
    progress = _optional(payload.get("progress"), "progress")
    _field(
        "Steps",
        f"{_text(progress.get('completed_steps'))} / {_text(progress.get('total_steps'))}",
    )
    _field("current step", progress.get("current_label"))
    child = _optional(progress.get("child_progress"), "child_progress")
    if child:
        operation = _optional(child.get("operation"), "child operation")
        _field("load/JIT phase", operation.get("phase", child.get("phase")))
        if child.get("startup_budget_seconds") is not None:
            _field(
                "initial start budget",
                f"{_text(child['startup_budget_seconds'])} seconds",
            )
        if child.get("start_deadline") is not None:
            _field("initial start deadline", child["start_deadline"])
        if operation:
            _field("Progress", progress_line({"progress": operation}))
    _field("Updated", _time(payload.get("updated_at")))


def _operation(payload: Mapping[str, object], noun: str) -> None:
    availability = payload.get("kind") == "recipe.image.availability.v2"
    update = payload.get("kind") == "recipe.cache.update.v2"
    identifier = (
        payload.get("id") if availability or update else payload.get("operation_id")
    )
    _field("Operation", identifier)
    _field(
        "Request",
        payload.get("request_id")
        if availability or update
        else payload.get("request_key"),
    )
    _field("State", payload.get("state"))
    if noun == "model" and (
        payload.get("cancellation") is not None
        or payload.get("state") == cli_states.CANCELLED
    ):
        cancellation = _optional(payload.get("cancellation"), "model cancellation")
        observation = _optional(
            cancellation.get("observation"), "cancellation observation"
        )
        _field("Cancellation effect", observation.get("effect", "unknown"))
        _field(
            "Cancellation evidence",
            observation.get(
                "detail",
                "Stopping the writer remains unconfirmed."
                if cancellation
                else "Writer stop evidence unavailable.",
            ),
        )
        if observation.get("observed_at") is not None:
            _field("Observed", _time(observation["observed_at"]))
    _field("Progress", _measured_progress(payload))
    _waiting(payload)
    if payload.get("selector") is not None:
        print(f"USE {_text(payload['selector'])}")
    if availability:
        for child in _records(payload, "children"):
            _field("Child", f"{_text(child.get('kind'))} {_text(child.get('id'))}")
            _field("Child state", child.get("state"))
            _field("Child progress", progress_line(child))
            _failure(child.get("failure"))
    if update:
        children = _records(payload, "children")
        _field("Recipes", len(children))
        if not children:
            print("No cached recipes to update.")
        for child in children:
            _field("Recipe", child.get("recipe_name"))
            _field("Revision", child.get("recipe_revision_id"))
            _field("State", child.get("state"))
            if child.get("operation_id") is not None:
                _field("Child", child["operation_id"])
            _failure(child.get("failure"))
        if payload.get("waiting_on") is not None:
            _field("Waiting for", payload["waiting_on"])
            _field("Next observation", _time(payload.get("next_attempt_at")))
    _failure(payload.get("failure"))
    if "preserved" in payload:
        _field("Preserved", _words(payload["preserved"]))
    if payload.get("projection_issue") is not None:
        issue = _object(payload["projection_issue"], "removal projection issue")
        _field("Cause", issue.get("detail"))
        _field("Next", issue.get("next_action"))
    if "reclaimed_bytes" in payload:
        _field("Reclaimed", _bytes(payload["reclaimed_bytes"]))
    _actions(payload.get("actions") if availability else payload.get("next_actions"))
    if isinstance(identifier, str):
        _field("Inspect", f"vonkctl {noun} progress {shlex.quote(identifier)}")


def _job(payload: Mapping[str, object]) -> None:
    _field("Job", payload.get("id"))
    _field("State", payload.get("state"))
    _field("Targets", _words(payload.get("targets")))
    if payload.get("status_reason") is not None:
        _field("Reason", payload["status_reason"])
    progress = _optional(payload.get("progress"), "job progress")
    if progress:
        _field(
            "Completed",
            f"{_text(progress.get('completed'))} / {_text(progress.get('total'))}",
        )
        _field("Failed", progress.get("failed"))
    if payload.get("projection_issue") is not None:
        _field("Observation", payload["projection_issue"])
    if payload.get("operations") is None:
        return
    for operation in _records(payload, "operations"):
        _field("Operation", operation.get("id"))
        _field("Spark", operation.get("node_id"))
        _field("State", operation.get("state"))
        _field("Progress", progress_line(operation))
        failure = _optional(operation.get("failure"), "job failure")
        if "code" in failure:
            _failure(failure)
        elif failure:
            # These are the two current agent/evidence alternatives in the
            # JobOperationResponse union, not retired availability shapes.
            _warn(f"Blocker: {_text(failure.get('error_code'))}")
            for key in (
                "summary",
                "reason",
                "detail",
                "stage",
                "diagnostic",
                "uncertain",
            ):
                if failure.get(key) is not None:
                    _warn(f"{key.title()}: {_text(failure[key])}")
        recovery = _optional(operation.get("recovery"), "job recovery")
        if recovery.get("explanation") is not None:
            _field("Recovery", recovery["explanation"])
        _actions(recovery.get("actions"))
    for key in ("target_next_cursor", "operation_next_cursor"):
        if payload.get(key) is not None:
            _field(key.replace("_", " ").title(), payload[key])
            print("Partial page; additional job evidence is available.")
