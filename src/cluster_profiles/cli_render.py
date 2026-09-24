"""Task presentations for the current Controller contracts, without terminal state."""

from __future__ import annotations

import json
import shlex
import shutil
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime


def terminal_text(value: str) -> str:
    """Make untrusted terminal controls visible instead of executing them."""
    return "".join(
        (f"\\x{ord(char):02x}" if ord(char) <= 255 else f"\\u{ord(char):04x}")
        if unicodedata.category(char) in {"Cc", "Cf"}
        else char
        for char in value
    )


def _object(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} is not a valid object")
    return value


def _optional(value: object, field: str) -> Mapping[str, object]:
    return {} if value is None else _object(value, field)


def _records(document: Mapping[str, object], field: str) -> list[Mapping[str, object]]:
    value = document.get(field)
    if not isinstance(value, list) or any(
        not isinstance(row, Mapping) for row in value
    ):
        raise ValueError(f"{field} is not a valid list of records")
    return value


def _text(value: object) -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (Mapping, list, tuple)):
        raise TypeError("structured data requires its own presentation")
    encoding = sys.stdout.encoding or "utf-8"
    return (
        terminal_text(str(value)).encode(encoding, "backslashreplace").decode(encoding)
    )


def _warn(message: str) -> None:
    encoding = sys.stderr.encoding or "utf-8"
    print(
        message.encode(encoding, "backslashreplace").decode(encoding), file=sys.stderr
    )


def _words(value: object) -> str:
    if value is None:
        return "unavailable"
    if not isinstance(value, (list, tuple)):
        raise TypeError("expected a list of values")
    return ", ".join(_text(item) for item in value) if value else "none"


def _field(label: str, value: object) -> None:
    print(f"{label}: {_text(value)}")


def _bytes(value: object) -> str:
    if value is None:
        return "unavailable"
    if type(value) is not int or value < 0:
        raise ValueError("byte count is invalid")
    for unit, divisor in (
        ("TiB", 1 << 40),
        ("GiB", 1 << 30),
        ("MiB", 1 << 20),
        ("KiB", 1 << 10),
    ):
        if value >= divisor:
            return f"{value / divisor:.1f} {unit} ({value} bytes)"
    return f"{value} B"


def _headroom(value: object) -> str:
    if type(value) is int and value < 0:
        return f"short by {_bytes(-value)}"
    return _bytes(value)


def _time(value: object) -> str:
    if value is None:
        return "unavailable"
    if not isinstance(value, str):
        raise TypeError("timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        # Some operation owners supply opaque historical timestamp text.
        return _text(value)
    if parsed.tzinfo is None:
        return _text(value) + " (timezone unavailable)"
    return parsed.isoformat(sep=" ")


def _freshness(observed_at: object, projected_at: object) -> str:
    if not isinstance(observed_at, str) or not isinstance(projected_at, str):
        return "unavailable"
    try:
        observed = datetime.fromisoformat(observed_at)
        projected = datetime.fromisoformat(projected_at)
    except ValueError:
        return "unavailable"
    if observed.tzinfo is None or projected.tzinfo is None:
        return "timezone unavailable"
    age_seconds = (projected - observed).total_seconds()
    if age_seconds < 0:
        return "route observation is newer than the Controller projection"
    age = int(age_seconds)
    unit, count = (
        ("hour", age // 3600)
        if age >= 3600
        else ("minute", age // 60)
        if age >= 60
        else ("second", age)
    )
    suffix = "" if count == 1 else "s"
    return f"observed {count} {unit}{suffix} before this Controller read"


def _width(value: str) -> int:
    return sum(
        0
        if unicodedata.combining(char)
        else 2
        if unicodedata.east_asian_width(char) in {"W", "F"}
        else 1
        for char in value
    )


def _table(labels: Sequence[str], rows: Sequence[Sequence[object]]) -> None:
    """Use a table only when all values fit; otherwise preserve stacked values."""
    rendered = [[_text(value) for value in row] for row in rows]
    if not rendered:
        return
    widths = [
        max(_width(label), *(_width(row[i]) for row in rendered))
        for i, label in enumerate(labels)
    ]
    columns = shutil.get_terminal_size((80, 24)).columns
    if columns < 80 or sum(widths) + 2 * (len(labels) - 1) > columns:
        for row in rendered:
            for label, value in zip(labels, row, strict=True):
                _field(label, value)
            print()
        return
    for row in (list(labels), *rendered):
        print(
            "  ".join(
                value + " " * (widths[i] - _width(value)) for i, value in enumerate(row)
            ).rstrip()
        )


def _actions(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        raise TypeError("next actions are not a list")
    for item in value:
        # Recipe availability uses typed {key} actions; other owners use strings.
        _field("Next", item.get("key") if isinstance(item, Mapping) else item)


def _reasons(value: object, *, subject: object = None) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        raise TypeError("reasons are not a list")
    for item in value:
        if isinstance(item, Mapping):
            message = f"{_text(item.get('code'))}: {_text(item.get('detail'))}"
        else:
            message = _text(item)
        prefix = f"{_text(subject)}: " if subject is not None else ""
        _warn(f"Attention: {prefix}{message}")


def _failure(value: object) -> None:
    if value is None:
        return
    failure = _object(value, "failure")
    _warn(f"Blocker: {_text(failure.get('code'))}: {_text(failure.get('detail'))}")
    for key, label in (
        ("required_bytes", "Required"),
        ("free_bytes", "Available"),
        ("shortfall_bytes", "Shortfall"),
    ):
        if failure.get(key) is not None:
            _warn(f"{label}: {_bytes(failure[key])}")
    if failure.get("retry_time") is not None:
        _warn(f"Next attempt: {_time(failure['retry_time'])}")
    elif failure.get("retry_after_seconds") is not None:
        _warn(f"Retry after: {_text(failure['retry_after_seconds'])} seconds")
    _actions(failure.get("recovery_actions"))


def _node(node: Mapping[str, object], *, detail: bool, placements: bool = True) -> None:
    name = node.get("display_name")
    _field("Spark", name)
    _field("ID", node.get("id"))
    connection = _object(node.get("connection"), "connection")
    _field("Connection", connection.get("online_state"))
    if connection.get("offline_reason") is not None:
        _field("Connection reason", connection["offline_reason"])
    _field("Last seen", _time(connection.get("last_seen_at")))
    inventory = _optional(node.get("inventory"), "inventory")
    telemetry = _optional(node.get("telemetry"), "telemetry")
    _field("Inventory", inventory.get("freshness"))
    _field("Telemetry", telemetry.get("freshness"))
    _field("Memory free", _bytes(inventory.get("host_memory_free_bytes")))
    _field("Disk free", _bytes(inventory.get("disk_free_bytes")))
    loaded = _records(node, "loaded")
    if not placements:
        _field("Run IDs", _words([run.get("run_id") for run in loaded]))
    elif not loaded:
        print("Running workloads: none")
    for run in loaded if placements else []:
        _field("Workload", run.get("title"))
        _field("Run", run.get("run_id"))
        _field("State", run.get("run_state"))
        _field("Route", run.get("route_state"))
        _field("Members", _words(run.get("member_node_ids")))
        if run.get("degraded_reason") is not None:
            _reasons([run["degraded_reason"]], subject=name)
    if detail:
        _field("Lifecycle", node.get("lifecycle"))
        provenance = _optional(node.get("provenance"), "provenance")
        if provenance:
            invalid = _records(provenance, "invalid_operation_evidence")
            omitted = provenance.get("invalid_operation_evidence_omitted_count")
            if type(omitted) is not int or omitted < 0:
                raise ValueError("invalid evidence omitted count is invalid")
            count = len(invalid) + omitted
            if count:
                _warn(
                    f"Deployment history: {count} invalid historical evidence records; "
                    "use --json to inspect the reported details."
                )
        for key, value in _object(node.get("labels", {}), "labels").items():
            _field("Label", f"{key}={_text(value)}")
        installed = _records(node, "installed")
        if not placements:
            _field(
                "Installation IDs",
                _words([item.get("installation_id") for item in installed]),
            )
        elif not installed:
            print("Installed recipes: none")
        for recipe in installed if placements else []:
            _field("Installed recipe", recipe.get("title"))
            _field("Installation", recipe.get("installation_id"))
            _field("Installation state", recipe.get("group_state"))
            _field("Members", _words(recipe.get("member_node_ids")))
    _reasons(node.get("warnings"), subject=name)


def _fleet_placements(
    nodes: Sequence[Mapping[str, object]], *, installed: bool = False
) -> None:
    """Group presentation by the owner's identity, preserving its health decisions."""
    field = "installed" if installed else "loaded"
    identity = "installation_id" if installed else "run_id"
    grouped: dict[str, list[tuple[Mapping[str, object], Mapping[str, object]]]] = {}
    for node in nodes:
        for presence in _records(node, field):
            identifier = presence.get(identity)
            if not isinstance(identifier, str) or not identifier:
                raise ValueError(f"placement lacks its canonical {identity}")
            grouped.setdefault(identifier, []).append((node, presence))
    print(
        f"Installations: {len(grouped)} distinct placements"
        if installed
        else f"Workloads: {len(grouped)} distinct runs"
    )
    for identifier, members in sorted(grouped.items()):

        def rank_order(value: tuple[Mapping[str, object], Mapping[str, object]]) -> int:
            rank = value[1].get("rank")
            if type(rank) is not int:
                raise ValueError("placement lacks its canonical rank")
            return rank

        members.sort(key=rank_order)
        presence = members[0][1]
        _field("Installed recipe" if installed else "Workload", presence.get("title"))
        _field("Installation" if installed else "Run", identifier)
        if installed:
            _field("Installation state", presence.get("group_state"))
            _field("Complete", presence.get("complete"))
        else:
            _field("Alias", presence.get("alias"))
            _field("Controller run state", presence.get("run_state"))
            _field("Observed group", presence.get("group_state"))
            _field("Route", presence.get("route_state"))
        _field("Expected ranks", presence.get("expected_rank_count"))
        _field("Reported ranks", _words(presence.get("present_ranks")))
        _field("Reported member Sparks", _words(presence.get("member_node_ids")))
        labels = ["SPARK", "RANK", "ROLE", "OBSERVED"]
        if not installed:
            labels.extend(["FRESH", "AGE (s)"])
        rows = []
        for node, member in members:
            row = [
                node.get("display_name"),
                member.get("rank"),
                member.get("role"),
                member.get("rank_state"),
            ]
            if not installed:
                row.extend([member.get("rank_fresh"), member.get("rank_age_seconds")])
            rows.append(row)
        _table(labels, rows)
        for node, _ in members:
            _field(
                "Member ID",
                f"{_text(node.get('display_name'))}: {_text(node.get('id'))}",
            )
        reported = presence.get("member_node_ids")
        if isinstance(reported, list) and set(reported) - {
            node.get("id") for node, _ in members
        }:
            print(
                "Member details cover the selected Sparks; other reported members are outside this view."
            )
        if presence.get("degraded_reason") is not None:
            _reasons([presence["degraded_reason"]], subject=identifier)
        print()


def _library_item(item: Mapping[str, object], noun: str, *, detail: bool) -> None:
    identity = _object(item.get("identity"), "identity")
    local = _object(item.get("local"), "local")
    resources = _object(item.get("resources"), "resources")
    name = identity.get("title") if noun == "recipe" else identity.get("slug")
    _table(
        (
            noun.upper(),
            "IMAGE CACHE" if noun == "recipe" else "NAS CACHE",
            "RUNNING ON",
        ),
        [(name, local.get("controller"), _words(local.get("running_on")))],
    )
    selector = item.get("selector")
    print(f"USE {_text(selector)}")
    _field("Usage", _words(item.get("usage")))
    _field("Disk", _bytes(resources.get("disk_bytes")))
    _field("Memory", _bytes(resources.get("memory_bytes")))
    if noun == "recipe":
        assessment = _optional(item.get("assessment"), "assessment")
        if assessment:
            explained: set[str] = set()
            for key, label in (
                ("fleet_fit", "Fleet fit"),
                ("cache", "Exact NAS assets"),
                ("readiness", "Readiness"),
            ):
                check = _object(assessment.get(key), key)
                _field(label, check.get("state"))
                for reason in _records(check, "reasons"):
                    explanation = (
                        f"{_text(reason.get('code'))}: {_text(reason.get('detail'))}"
                    )
                    if explanation not in explained:
                        _field("Reason", explanation)
                        explained.add(explanation)
            group = _optional(assessment.get("group"), "group")
            if group:
                _field(
                    "Candidate Sparks",
                    _words([node.get("node_id") for node in _records(group, "nodes")]),
                )
            _field("Assessed", _time(assessment.get("observed_at")))
        else:
            _field("Readiness", "not assessed")
    preparation = _optional(local.get("preparation"), "preparation")
    if preparation:
        _field("Preparation", preparation.get("state"))
        _field("Progress", progress_line({"progress": preparation}))
        if preparation.get("operation_id"):
            _field(
                "Next",
                f"vonkctl {noun} progress {shlex.quote(str(preparation['operation_id']))} --follow",
            )
    elif local.get("controller") in {"not_cached", "failed"} and isinstance(
        selector, str
    ):
        _field("Next", f"vonkctl {noun} download {shlex.quote(selector)}")
    elif local.get("controller") == "unknown":
        print(
            "Cache availability is unknown; inspect the Controller before preparing assets."
        )
    if detail:
        _field("Publisher", identity.get("publisher"))
        _field("Content digest", identity.get("content_sha256"))
        _field("Updated", _time(item.get("updated_at")))
        if noun == "recipe":
            _field("Required Sparks", item.get("node_count"))
            _field("Models", _words(item.get("model_selectors")))
        else:
            _field("Variant", item.get("variant"))
            _field("Quantization", item.get("quantization"))


def _library(
    payload: Mapping[str, object], noun: str, *, detail: bool, wide: bool
) -> None:
    if detail:
        _library_item(payload, noun, detail=True)
        return
    rows = _records(payload, "models" if noun == "model" else "recipes")
    print(f"{noun.title()}s: {len(rows)} on this page")
    if not rows:
        print(f"No {noun}s match this page's filters.")
    for item in rows:
        _library_item(item, noun, detail=wide)
        print()
    if payload.get("generated_at") is not None:
        _field("Observed", _time(payload["generated_at"]))
    cursor = payload.get("next_cursor")
    if isinstance(cursor, str):
        print("Page incomplete: more results are available.")
        _field("Next cursor", cursor)
        print(f"Continue with --cursor {shlex.quote(cursor)} and the same filters.")
    else:
        print("End of results for these filters.")


def _profile(payload: Mapping[str, object]) -> None:
    print(f"Profile {_text(payload.get('number'))}: {_text(payload.get('name'))}")
    _field("Revision", payload.get("revision"))
    _field("Loaded revision", payload.get("loaded_revision"))
    _field("State", payload.get("status"))
    _field("Retention", payload.get("installation_policy"))
    if "favorite" in payload:
        _field("Favorite", payload["favorite"])
    if payload.get("description"):
        _field("Description", payload["description"])
    for key, value in _object(payload.get("labels", {}), "labels").items():
        _field("Label", f"{key}={_text(value)}")
    definition = _object(payload.get("definition"), "definition")
    desired = _records(definition, "assignments")
    observed = _records(payload, "assignments")
    if not desired:
        print("No assignments saved.")
    for assignment in desired:
        selector = assignment.get("recipe_selector")
        nodes = assignment.get("spark_ids")
        match = next(
            (
                row
                for row in observed
                if row.get("recipe_selector") == selector
                and row.get("spark_ids") == nodes
            ),
            None,
        )
        _field("Recipe", selector)
        _field("Sparks", _words(nodes))
        _field("Desired", assignment.get("desired_state"))
        _field("Observed", match.get("observed_state") if match is not None else None)
    _reasons(payload.get("warnings"))
    _actions(payload.get("next_actions"))


def _profile_endpoints(payload: Mapping[str, object]) -> None:
    number = payload.get("number")
    application_id = payload.get("application_id")
    if application_id is None:
        print(f"Profile {number} has no loaded application.")
        print("Saved profile edits do not publish routes until the profile is loaded.")
        return
    _field("Loaded application", application_id)
    _field("Application state", payload.get("application_state"))
    issue = _optional(payload.get("projection_issue"), "projection_issue")
    if issue:
        print(
            "Endpoint assignments are unavailable because stored application history is invalid."
        )
        _field("Stored evidence", issue.get("detail"))
        return
    assignments = _records(payload, "assignments")
    if not assignments:
        print("The loaded application has no endpoint assignments.")
        return
    for assignment in assignments:
        print()
        _field("Assignment", assignment.get("recipe_title"))
        _field("Endpoint state", assignment.get("state"))
        alias = assignment.get("alias")
        endpoint = _optional(assignment.get("endpoint"), "endpoint")
        if assignment.get("state") == "published":
            _field("Client model identifier", alias)
            api_base = endpoint.get("api_base")
            _field("API base", api_base)
            _field("Route generation", endpoint.get("generation"))
            _field("Route observed at", _time(endpoint.get("observed_at")))
            _field(
                "Freshness",
                _freshness(endpoint.get("observed_at"), payload.get("observed_at")),
            )
            _field("Route expires at", _time(endpoint.get("expires_at")))
            if isinstance(api_base, str) and isinstance(alias, str):
                print("Credential-free configuration example:")
                print(
                    "  API_BASE="
                    + terminal_text(shlex.quote(api_base))
                    + " MODEL="
                    + terminal_text(shlex.quote(alias))
                )
            continue
        if alias is not None:
            _field("Client model identifier", alias)
        messages = {
            "installed-only": "Install-only assignment; no published endpoint was requested.",
            "not-published-yet": "The current assignment route is not published yet.",
            "expired": "The published route lease has expired.",
            "withdrawn": "No current published route is associated with this assignment.",
            "unavailable": "The Controller cannot verify a current route for this assignment.",
        }
        print(
            messages.get(str(assignment.get("state")), "Endpoint state is unavailable.")
        )


def _preview(payload: Mapping[str, object]) -> None:
    print("Ready for review" if payload.get("allowed") is True else "Blocked")
    _field("Profile", payload.get("profile_name"))
    _field("Revision", payload.get("profile_revision"))
    _field("Plan digest", payload.get("plan_digest"))
    scope = _object(payload.get("scope"), "scope")
    _field("All Sparks", _words(scope.get("node_ids")))
    _field("Idle Sparks", _words(scope.get("idle_node_ids")))
    for key, value in _object(payload.get("summary"), "summary").items():
        _field(key.replace("_", " ").title(), value)
    for assignment in _records(payload, "assignments"):
        _field("Recipe", assignment.get("recipe_title"))
        _field("Sparks", _words(assignment.get("node_ids")))
        _field("Current", assignment.get("current_state"))
        _field("Desired", assignment.get("desired_state"))
        _reasons(assignment.get("reasons"), subject=assignment.get("recipe_title"))
    for step in _records(payload, "steps"):
        print(f"{_text(step.get('index'))}. {_text(step.get('label'))}")
        _field("Affected Sparks", _words(step.get("node_ids")))
    effects = _object(payload.get("effects"), "effects")
    for run in _records(effects, "runs"):
        _field(f"{_text(run.get('action')).title()} endpoint", run.get("alias"))
        _field("Run", run.get("run_id"))
        _field("Complete group", _words(run.get("node_ids")))
    for installation in _records(effects, "installations"):
        _field(
            f"{_text(installation.get('action')).title()} installation",
            installation.get("installation_id"),
        )
        _field("Complete group", _words(installation.get("node_ids")))
    for pending in _records(effects, "superseded"):
        _field(f"Supersede {_text(pending.get('kind'))}", pending.get("id"))
        _field("Complete group", _words(pending.get("node_ids")))
    for item in _records(payload, "preparation_decisions"):
        _field("Assignment", item.get("assignment_id"))
        model = _object(item.get("model"), "model identity")
        runtime = _object(item.get("runtime_image"), "runtime identity")
        _field("Exact model set", model.get("artifact_set_sha256"))
        _field("Exact image", runtime.get("image_digest"))
        _field("OCI archive SHA-256", runtime.get("oci_layout_sha256"))
        _field("Image size", _bytes(runtime.get("image_bytes")))
        _field("Architecture", runtime.get("architecture"))
        _field("Runtime interface", runtime.get("runtime_interface"))
        build_id = runtime.get("build_id")
        _field(
            "Build",
            "published image" if build_id is None else build_id,
        )
        _field("Reuse model on", _words(item.get("model_reuse_node_ids")))
        _field("Reuse image on", _words(item.get("image_reuse_node_ids")))
    for item in _records(payload, "assessments"):
        assessment = _object(item.get("assessment"), "workload assessment")
        _field("Admission for assignment", item.get("assignment_id"))
        _field("Intended endpoint", assessment.get("alias"))
        for label, key in (
            ("Current capacity", "fit_current"),
            ("Capacity after stops", "fit_after_stop"),
        ):
            fit = _optional(assessment.get(key), key)
            if not fit:
                continue
            _field(label, "fits" if fit.get("allowed") is True else "blocked")
            for node in _records(fit, "nodes"):
                node_id = node.get("node_id")
                _field("Spark", node_id)
                _field("Ports required", _words(node.get("ports_required")))
                _field("Memory demand kind", node.get("memory_kind"))
                _field("Physical memory pool", node.get("memory_pool"))
                _field("Memory required", _bytes(node.get("memory_required_bytes")))
                _field("Memory reserve", _bytes(node.get("memory_floor_bytes")))
                _field(
                    "Physical memory capacity",
                    _bytes(node.get("memory_capacity_bytes")),
                )
                _field(
                    "Available in limiting pool",
                    _bytes(node.get("memory_available_bytes")),
                )
                _field(
                    "Memory after placement",
                    _headroom(node.get("memory_free_after_bytes")),
                )
                uncertainty = _optional(
                    node.get("memory_usage_uncertainty"), "memory usage uncertainty"
                )
                if uncertainty:
                    _field(
                        "Resident usage evidence",
                        "Per-run usage is unavailable; admission uses each full residual upper bound.",
                    )
                    _field("Capacity source", uncertainty.get("source"))
                    _field(
                        "Inventory sample",
                        uncertainty.get("inventory_observed_at"),
                    )
                    _field(
                        "Inventory evidence digest",
                        uncertainty.get("inventory_evidence_digest"),
                    )
                    for residual in _records(uncertainty, "residual_ranges"):
                        _field(
                            "Possible run residual",
                            f"{residual.get('run_id')} generation "
                            f"{residual.get('run_generation')}: 0–"
                            f"{_bytes(residual.get('maximum_bytes'))} (not measured)",
                        )
                _field("Disk required", _bytes(node.get("disk_required_bytes")))
                _field(
                    "Disk after placement", _headroom(node.get("disk_free_after_bytes"))
                )
                for reason_kind, label in (
                    ("blockers", "blocker"),
                    ("warnings", "warning"),
                ):
                    _reasons(
                        node.get(reason_kind),
                        subject=f"{_text(node_id)} capacity {label}",
                    )
        condition = _optional(
            assessment.get("post_stop_memory_check"), "post-stop memory check"
        )
        if condition:
            _field(
                "Conditional memory fit",
                "stop the reviewed workloads, then recheck fresh capacity before preparing or starting",
            )
            _field("Required stops", _words(condition.get("stop_run_ids")))
        if assessment.get("stop_before_prepare") is True:
            print("Stop the reviewed workloads before preparing the replacement.")
        if assessment.get("stop_before_transfer") is True:
            print("Stop the reviewed workloads before transferring the replacement.")
        _reasons(assessment.get("blockers"))
        _reasons(assessment.get("warnings"))
    for item in _records(payload, "preparations"):
        preparation = _object(item.get("preparation"), "preparation")
        _field("Preparation", item.get("assignment_id"))
        _field("Controller ready", preparation.get("controller_ready"))
        _field("Targets ready", preparation.get("targets_ready"))
    _reasons(payload.get("reasons"))


def _cache_removal_review(payload: Mapping[str, object]) -> None:
    """Present the owning cache policy's removal review without interpretation."""
    _field("Action", payload.get("action"))
    _field("Resource", payload.get("resource_kind"))
    _field("Selector", payload.get("selector"))
    _field("Target identity", payload.get("target_identity"))
    if payload.get("with_model") is not None:
        _field("Remove dependent model", payload.get("with_model"))
    _field("Review digest", payload.get("review_digest"))
    _field("Observed", _time(payload.get("observed_at")))

    assets = _records(payload, "assets")
    if not assets:
        print("Assets: none")
    for asset in assets:
        print("Asset:")
        _field("  Kind", asset.get("kind"))
        _field("  SHA-256", asset.get("sha256"))
        _field("  Disposition", asset.get("disposition"))
        _field("  Readiness", asset.get("availability"))
        _field("  Expected bytes", _bytes(asset.get("expected_bytes")))
        _field("  Available bytes", _bytes(asset.get("available_bytes")))

    for field, heading in (
        ("references", "Saved references"),
        ("active_work", "Active work"),
    ):
        records = _records(payload, field)
        if not records:
            print(f"{heading}: none")
        for record in records:
            print(f"{heading}:")
            _field("  Classification", record.get("classification"))
            _field(
                "  Asset",
                f"{_text(record.get('asset_kind'))} "
                f"{_text(record.get('asset_sha256'))}",
            )
            _field(
                "  Owner",
                f"{_text(record.get('owner_kind'))} {_text(record.get('owner_id'))}",
            )
            _field("  State", record.get("state"))
            if record.get("detail") is not None:
                _field("  Detail", record.get("detail"))
            if record.get("reason") is not None:
                _field("  Reason", record.get("reason"))

    blockers = _records(payload, "blockers")
    if not blockers:
        print("Blockers: none")
    for blocker in blockers:
        _field(
            "Blocker",
            f"{_text(blocker.get('code'))}: {_text(blocker.get('detail'))}",
        )
        _field("  Retryable", blocker.get("retryable"))
        _actions(blocker.get("recovery_actions"))


def _application(payload: Mapping[str, object]) -> None:
    _field("Application", payload.get("id"))
    _field("State", payload.get("state"))
    if payload.get("status_reason") is not None:
        _field("Reason", payload["status_reason"])
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
    _field("Progress", progress_line(payload))
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
    progress = _object(payload.get("progress"), "job progress")
    _field(
        "Completed",
        f"{_text(progress.get('completed'))} / {_text(progress.get('total'))}",
    )
    _field("Failed", progress.get("failed"))
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


def _artifact_job_record(payload: Mapping[str, object], action: str) -> None:
    _field("Artifact job", payload.get("id"))
    _field("Command", action)
    _field("Run", payload.get("run_id"))
    _field("State", payload.get("state"))
    _field("Interface", payload.get("interface"))
    _field("Contract SHA-256", payload.get("contract_sha256"))
    _field("Input manifest SHA-256", payload.get("input_manifest_sha256"))
    _field("Input bytes", _bytes(payload.get("input_total_bytes")))
    _field("Created", _time(payload.get("created_at")))
    _field("Updated", _time(payload.get("updated_at")))
    if payload.get("status_reason") is not None:
        _field("Reason", payload.get("status_reason"))

    operation_id = payload.get("operation_id")
    if operation_id is not None:
        _field("Operation", operation_id)
        _field("Submit request", payload.get("submit_request_id"))

    input_declarations = _records(payload, "input_declarations")
    input_files = _records(payload, "input_files")
    uploaded_names = {item.get("name") for item in input_files}
    _field("Inputs", f"{len(input_declarations)} declared, {len(input_files)} uploaded")
    for item in input_declarations:
        name = item.get("name")
        _field("Input", name)
        _field("Input slot", item.get("slot"))
        _field("Input state", "uploaded" if name in uploaded_names else "not uploaded")
        _field("Input media type", item.get("media_type"))
        _field("Input size", _bytes(item.get("size_bytes")))
        _field("Input SHA-256", item.get("sha256"))

    state = payload.get("state")
    output_files = _records(payload, "output_files")
    if state == "succeeded":
        _field("Output manifest SHA-256", payload.get("output_manifest_sha256"))
        if not output_files:
            print("Result files: none (job succeeded with an empty result).")
        else:
            _field("Result files", len(output_files))
            for item in output_files:
                _field("Output file", item.get("name"))
                _field("Output media type", item.get("media_type"))
                _field("Output size", _bytes(item.get("size_bytes")))
                _field("Output SHA-256", item.get("sha256"))
    else:
        _field(
            "Result files", f"unavailable until job succeeds (state: {_text(state)})"
        )

    if isinstance(operation_id, str) and state in {"queued", "running", "cancelling"}:
        _field(
            "Reconnect",
            f"vonkctl recipe job detail {shlex.quote(str(payload.get('id')))} --follow",
        )


def _artifact_job_list(payload: Mapping[str, object]) -> None:
    jobs = _records(payload, "jobs")
    _field("Artifact jobs", len(jobs))
    if not jobs:
        print("No artifact jobs for this run.")
        return
    for job in jobs:
        print()
        _field("Artifact job", job.get("id"))
        _field("Run", job.get("run_id"))
        _field("State", job.get("state"))
        _field("Interface", job.get("interface"))
        if job.get("status_reason") is not None:
            _field("Reason", job.get("status_reason"))
        inputs = _records(job, "input_declarations")
        uploaded_inputs = _records(job, "input_files")
        _field("Inputs", f"{len(inputs)} declared, {len(uploaded_inputs)} uploaded")
        outputs = _records(job, "output_files")
        if job.get("state") == "succeeded":
            _field(
                "Outputs",
                "none (successful empty result)"
                if not outputs
                else f"{len(outputs)} verified result files",
            )
        else:
            _field(
                "Outputs",
                f"unavailable until job succeeds (state: {_text(job.get('state'))})",
            )
        operation_id = job.get("operation_id")
        if operation_id is not None:
            _field("Operation", operation_id)
        if isinstance(operation_id, str) and job.get("state") in {
            "queued",
            "running",
            "cancelling",
        }:
            _field(
                "Reconnect",
                f"vonkctl recipe job detail {shlex.quote(str(job.get('id')))} --follow",
            )


def _artifact_job_download(payload: Mapping[str, object]) -> None:
    _field("Artifact job", payload.get("job_id"))
    state = payload.get("state")
    _field("State", state)
    _field("Output manifest SHA-256", payload.get("output_manifest_sha256"))
    _field("Total output bytes", _bytes(payload.get("total_bytes")))
    files = _records(payload, "files")
    if state != "succeeded":
        raise ValueError("artifact job download receipt is not successful")
    if not files:
        print("Result files: none (job succeeded with an empty result).")
        return
    _field("Verified output files", len(files))
    for item in files:
        _field("Output file", item.get("name"))
        _field("File state", item.get("state"))
        _field("Verified path", item.get("path"))
        _field("Verified size", _bytes(item.get("size_bytes")))
        _field("Verified SHA-256", item.get("sha256"))


def _artifact_job(payload: Mapping[str, object], action: str) -> None:
    if action == "list":
        _artifact_job_list(payload)
    elif action == "download":
        _artifact_job_download(payload)
    elif action in {"detail", "create", "upload", "submit", "cancel"}:
        _artifact_job_record(payload, action)
    else:
        raise ValueError(f"no recipe job presentation for {action}")


def _activity(
    payload: Mapping[str, object], filters: Mapping[str, object] | None
) -> None:
    operations = _records(payload, "operations")
    total = payload.get("total")
    if type(total) is not int or total < 0:
        raise ValueError("activity total is invalid")
    _field("Activity", f"{len(operations)} of {total} references on this page")
    if not operations:
        print("No activity matches this request.")
    for operation in operations:
        _field("Operation", operation.get("id"))
        _field("Kind", operation.get("kind"))
        _field("State", operation.get("state"))
        _field("Created", _time(operation.get("created_at")))
        _field("Targets", _words(operation.get("node_ids")))
        if operation.get("attempt") is not None:
            _field("Attempt", operation.get("attempt"))
        owner = _optional(operation.get("owner"), "operation owner")
        if owner:
            _field("Owner", f"{_text(owner.get('kind'))} {_text(owner.get('id'))}")
            if owner.get("request_id") is not None:
                _field("Request", owner.get("request_id"))
            owner_kind = owner.get("kind")
            owner_id = owner.get("id")
            reconnect = None
            if isinstance(owner_id, str) and owner_id:
                if owner_kind == "job":
                    reconnect = ["vonkctl", "fleet", "progress", owner_id, "--follow"]
                elif owner_kind == "model-cache-operation":
                    reconnect = ["vonkctl", "model", "progress", owner_id, "--follow"]
                elif owner_kind == "fleet-profile-application":
                    reconnect = [
                        "vonkctl",
                        "profile",
                        "progress",
                        "--application",
                        owner_id,
                        "--follow",
                    ]
                elif owner_kind == "audit-event" and isinstance(
                    owner.get("request_id"), str
                ):
                    reconnect = [
                        "vonkctl",
                        "fleet",
                        "activity",
                        "--request-id",
                        owner["request_id"],
                    ]
            _field(
                "Reconnect",
                "unavailable" if reconnect is None else shlex.join(reconnect),
            )
        cancellation = _optional(operation.get("cancellation"), "profile cancellation")
        if cancellation:
            _field(
                "Cancellation",
                f"{_text(cancellation.get('state'))} ({_text(cancellation.get('cause'))})",
            )
            _field("Cancellation request", cancellation.get("request_key"))
            _field("Cancellation actor", cancellation.get("actor"))
            if cancellation.get("owner") is not None:
                _field("Cancellation owner", cancellation.get("owner"))
            if cancellation.get("dependency") is not None:
                _field("Cancellation dependency", cancellation.get("dependency"))
            if cancellation.get("deadline_at") is not None:
                _field("Cancellation deadline", _time(cancellation.get("deadline_at")))
            for field, label in (
                ("completed_effects", "Completed effect"),
                ("pending_effects", "Pending effect"),
                ("cancelled_effects", "Cancelled or unissued effect"),
            ):
                effects = _records(cancellation, field)
                _field(label + " count", len(effects))
                for effect in effects:
                    _field(
                        label,
                        f"{_text(effect.get('outcome'))}: "
                        f"{_text(effect.get('kind'))} "
                        f"{_text(effect.get('effect_id'))}: "
                        f"{_text(effect.get('label'))}",
                    )
        failure = _optional(operation.get("failure"), "operation failure")
        if failure:
            _warn(
                "Blocker: "
                + _text(failure.get("code", failure.get("error_code")))
                + ": "
                + _text(failure.get("detail", failure.get("summary")))
            )
        if operation.get("status_reason") is not None:
            _warn(f"Reason: {_text(operation['status_reason'])}")
        recovery = _optional(operation.get("recovery"), "operation recovery")
        _actions(recovery.get("actions"))
        print()

    cursor = payload.get("next_cursor")
    if cursor is None:
        _field("More results", "no")
        return
    if not isinstance(cursor, str) or not cursor or len(cursor) > 512:
        raise ValueError("activity continuation cursor is invalid")
    command = ["vonkctl", "fleet", "activity"]
    query_filters = filters or {}
    limit = query_filters.get("limit", 20)
    command.extend(("--limit", str(limit), "--cursor", cursor))
    for key, flag in (
        ("state", "--state"),
        ("target", "--target"),
        ("request_id", "--request-id"),
    ):
        value = query_filters.get(key)
        if isinstance(value, str) and value:
            command.extend((flag, value))
    print("More results are available. Continue with:")
    print(shlex.join(command))


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


def _error(payload: Mapping[str, object]) -> None:
    _field("Error", payload.get("error"))
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


def render_payload(
    payload: Mapping[str, object],
    noun: str,
    *,
    action: str | None = None,
    wide: bool = False,
    technical: bool = False,
    activity_filters: Mapping[str, object] | None = None,
    artifact_job_action: str | None = None,
) -> None:
    """Dispatch by the command's current response contract, never legacy shapes."""
    if "observation" in payload:
        observation = _object(payload["observation"], "observation")
        _field("Observation", observation.get("status"))
        _field("Last confirmed", _time(observation.get("observed_at")))
        _field("Age in seconds", observation.get("age_seconds"))
        _field("Reconnect", observation.get("reconnect_command"))
        render_payload(
            _object(payload.get("result"), "observed result"),
            noun,
            action=action,
            wide=wide,
            technical=technical,
            activity_filters=activity_filters,
            artifact_job_action=artifact_job_action,
        )
        return
    if "delivery" in payload or (noun == "fleet" and action == "enrollment"):
        _enrollment(payload)
    elif "error" in payload:
        _error(payload)
    elif action == "connection":
        _field("Connected", payload.get("connected"))
        _field("Controller", payload.get("origin"))
        _field("Authorized read", payload.get("authorized_read"))
        client = _object(payload.get("client"), "client")
        _field("CLI version", client.get("version"))
    elif noun == "fleet":
        if action is None:
            nodes = _records(payload, "nodes")
            print(f"Fleet: {len(nodes)} Sparks" if nodes else "No Sparks are enrolled.")
            for node in nodes:
                _node(node, detail=wide, placements=False)
                print()
            _fleet_placements(nodes)
            if wide:
                _fleet_placements(nodes, installed=True)
            _field("Observed", _time(payload.get("generated_at")))
        elif action == "node-profile":
            for key, label in (
                ("display_name", "Spark"),
                ("id", "ID"),
                ("hostname", "Hostname"),
                ("ip_address", "Address"),
                ("lifecycle", "Lifecycle"),
            ):
                _field(label, payload.get(key))
            labels = _object(payload.get("labels"), "labels")
            if not labels:
                print("Labels: none")
            for key, value in labels.items():
                _field("Label", f"{key}={_text(value)}")
        elif action in {"detail", "rename"}:
            _node(payload, detail=True)
        elif action == "progress":
            _job(payload)
        elif action == "activity":
            _activity(payload, activity_filters)
        elif action == "loginfo":
            _field("Spark", payload.get("node_id"))
            entries = _records(payload, "entries")
            if not entries:
                print("No retained log entries match this request.")
            for entry in entries:
                print(
                    f"{_time(entry.get('observed_at'))} {_text(entry.get('level'))} {_text(entry.get('source'))}: {_text(entry.get('message'))}"
                )
            _field("Retained evidence", payload.get("retained"))
        elif action in {"remove", "upgrade"}:
            _field("Action", payload.get("action"))
            _field("State", payload.get("state"))
            _field("Spark", payload.get("node_id"))
            _field("Targets", _words(payload.get("targets")))
            if payload.get("detail") is not None:
                _field("Detail", payload["detail"])
            if payload.get("operation_id") is not None:
                _field("Job", payload["operation_id"])
                _field(
                    "Next",
                    f"vonkctl fleet progress {shlex.quote(str(payload['operation_id']))} --follow",
                )
        else:
            raise ValueError(f"no fleet presentation for {action}")
    elif noun == "recipe" and artifact_job_action is not None:
        _artifact_job(payload, artifact_job_action)
    elif noun in {"model", "recipe"}:
        if action == "preview":
            _cache_removal_review(payload)
        elif action in {None, "library", "detail"}:
            _library(payload, noun, detail=action == "detail", wide=wide)
        else:
            _operation(payload, noun)
    elif noun == "profile":
        if action == "endpoint":
            _profile_endpoints(payload)
        elif action == "list":
            rows = _records(payload, "profiles")
            if not rows:
                print("No profiles saved.")
            _table(
                ("PROFILE", "NAME", "STATE", "REVISION"),
                [
                    (
                        row.get("number"),
                        row.get("name"),
                        row.get("status"),
                        row.get("revision"),
                    )
                    for row in rows
                ],
            )
        elif action == "preview":
            _preview(payload)
        elif action in {"progress", "load"}:
            _application(payload)
        elif action == "export":
            _field("Profile", payload.get("profile"))
            _field("Revision", payload.get("revision"))
            _field("File", payload.get("output"))
        else:
            _profile(payload)
    else:
        raise ValueError(f"no presentation for {noun}")
    if technical and "document" in payload:
        print("Canonical definition:")
        print(
            json.dumps(payload["document"], sort_keys=True, indent=2, ensure_ascii=True)
        )


def progress_line(observed: Mapping[str, object]) -> str:
    """Describe measured work; a missing or explicitly unknown total stays unknown."""
    progress = _optional(observed.get("progress"), "progress")
    nested = progress.get("operation")
    if isinstance(nested, Mapping):
        progress = nested
    phase = _text(
        progress.get("phase", observed.get("phase", observed.get("state", "working")))
    )
    completed = progress.get("completed_bytes")
    if type(completed) is int:
        total = progress.get("total_bytes")
        if type(total) is int and progress.get("total_bytes_known") is not False:
            percent = f" ({completed * 100 // total}%)" if total > 0 else ""
            return f"{phase} | {_bytes(completed)} / {_bytes(total)}{percent}"
        return f"{phase} | {_bytes(completed)}; total unknown"
    if progress.get("current_label") is not None:
        return f"{phase} | {_text(progress['current_label'])}"
    if progress.get("completed_items") is not None:
        return f"{phase} | items {_text(progress['completed_items'])} / {_text(progress.get('total_items'))}"
    return phase + " | progress unavailable"
