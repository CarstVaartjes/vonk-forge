"""Terminal presentation for administration responses."""

from __future__ import annotations

from collections.abc import Mapping

from .common import _actions, _bytes, _field, _optional, _records, _text, _time


def _enrollment(payload: Mapping[str, object]) -> None:
    _field("Grant", payload.get("id"))
    delivery = _optional(payload.get("delivery"), "delivery")
    if delivery:
        _field("Delivery", delivery.get("status"))
        _field("File", payload.get("output"))
    else:
        _field("State", payload.get("state"))
    for key, label in (
        ("purpose", "Purpose"),
        ("node_id", "Spark"),
        ("error", "Error"),
        ("reconciliation", "Reconciliation"),
        ("output_status", "File status"),
    ):
        if payload.get(key) is not None:
            _field(label, payload[key])
    if payload.get("expires_at") is not None:
        _field("Expires", _time(payload["expires_at"]))
    status = _optional(payload.get("grant_status"), "grant_status")
    if status:
        _field("Grant status", status.get("state"))
    _actions(payload.get("recovery"))


def _catalog_sync(payload: Mapping[str, object]) -> None:
    if payload.get("state") == "never-run":
        print("The recipe catalog has not synced yet.")
        print("Next: vonkctl recipe library")
        return
    _field("State", payload.get("state"))
    _field("Last run", _time(payload.get("completed_at") or payload.get("created_at")))
    _field("Trigger", payload.get("trigger"))
    _field("Repository", payload.get("repository"))
    _field("Library version", payload.get("library_version"))
    _field("Library updated", _time(payload.get("library_updated_at")))
    commit, expected = payload.get("commit"), payload.get("expected_commit")
    _field("Commit", commit)
    if expected is not None and expected != commit:
        _field("Expected commit", expected)
    _field(
        "Recipes",
        f"{_text(payload.get('processed_count'))} of {_text(payload.get('total_count'))} processed; "
        f"{_text(payload.get('imported_count'))} imported, {_text(payload.get('updated_count'))} updated, "
        f"{_text(payload.get('unchanged_count'))} unchanged, {_text(payload.get('skipped_count'))} skipped, "
        f"{_text(payload.get('withdrawn_count'))} withdrawn",
    )
    failure = _optional(payload.get("last_error"), "last_error")
    if failure:
        _field(
            "Last error",
            f"{_text(failure.get('code'))}: {_text(failure.get('detail'))} ({_time(failure.get('occurred_at'))})",
        )
    problems = _records(payload, "problems")
    for problem in problems:
        uri = f" [{problem['recipe_uri']}]" if problem.get("recipe_uri") else ""
        _field(
            "Problem",
            f"{_text(problem.get('code'))}: {_text(problem.get('detail'))}{uri}",
        )
    for stale in _records(payload, "stale_recipes"):
        _field(
            "Stale",
            f"{_text(stale.get('recipe_id'))}: {_text(stale.get('stale_installation_count'))} installations, "
            f"{_text(stale.get('stale_run_count'))} runs",
        )
    for gone in _records(payload, "withdrawn_recipes"):
        _field("Withdrawn", gone.get("recipe_id"))
    if problems or failure:
        print("Next: vonkctl fleet activity")


def _error(payload: Mapping[str, object]) -> None:
    _field("Error", payload.get("error"))
    if payload.get("usage") is not None:
        _field("Usage", payload["usage"])
    candidates = payload.get("candidates")
    if isinstance(candidates, (list, tuple)):
        for candidate in candidates:
            _field("Candidate", candidate)
    for key, label in (
        ("code", "Code"),
        ("detail", "Detail"),
        ("operation", "Operation"),
        ("endpoint", "Endpoint"),
        ("http_status", "HTTP status"),
        ("request_id", "Request ID"),
        ("source", "Source"),
        ("decision", "Decision"),
        ("request_key", "Request key"),
        ("transport", "Transport"),
        ("path", "Path"),
        ("errno", "OS error"),
        ("retry_time", "Next attempt"),
        ("retry_after_seconds", "Retry delay in seconds"),
        ("log_excerpt", "Diagnostic excerpt"),
    ):
        if payload.get(key) is not None:
            _field(label, payload[key])
    for key, label in (
        ("required_bytes", "Required"),
        ("free_bytes", "Available"),
        ("shortfall_bytes", "Shortfall"),
    ):
        if payload.get(key) is not None:
            _field(label, _bytes(payload[key]))
    _actions(payload.get("recovery_actions"))
    reconciliation = _optional(payload.get("reconcile"), "reconcile")
    if reconciliation:
        _field("Next", reconciliation.get("operation"))
