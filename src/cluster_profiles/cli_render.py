"""Task presentations for the current Controller contracts, without terminal state."""

from __future__ import annotations

import json
import shlex
import shutil
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime

from . import cli_states
from .cli_states import ARTIFACT_JOB_IN_FLIGHT, lifecycle_state


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
    """Use a table when all values fit; otherwise preserve stacked values."""
    rendered = [[_text(value) for value in row] for row in rows]
    if not rendered:
        return
    widths = [
        max(_width(label), *(_width(row[i]) for row in rendered))
        for i, label in enumerate(labels)
    ]
    # A terminal too narrow for the table gets stacked records; a pipe gets
    # the table, one line per row, whatever its width.
    columns = shutil.get_terminal_size((80, 24)).columns
    if sys.stdout.isatty() and (
        columns < 80 or sum(widths) + 2 * (len(labels) - 1) > columns
    ):
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


def _recommendation(item: Mapping[str, object]) -> str:
    """The Controller's own next step for a warning, when it names one."""
    recommendation = item.get("recommendation")
    return f" {_text(recommendation)}" if recommendation is not None else ""


def _reasons(value: object, *, subject: object = None) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        raise TypeError("reasons are not a list")
    for item in value:
        if _is_update_notice(item):
            # Informational: the recipe's own sentence says what to do.
            _warn(f"Note: {_text(item.get('detail'))}")
            continue
        if isinstance(item, Mapping):
            message = f"{_text(item.get('code'))}: {_text(item.get('detail'))}"
            message += _recommendation(item)
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


def _waiting(payload: Mapping[str, object]) -> None:
    """What a queued or blocked operation waits for, and when it is checked again."""
    blockers = payload.get("blockers")
    if isinstance(blockers, list):
        for item in blockers:
            if not isinstance(item, Mapping):
                continue
            nodes = item.get("node_ids")
            where = f" [{_words(nodes)}]" if isinstance(nodes, list) and nodes else ""
            _field(
                "Waiting for",
                f"{_text(item.get('code'))}: {_text(item.get('detail'))}{where}",
            )
    if payload.get("next_attempt_at") is not None:
        _field("Next attempt", _time(payload["next_attempt_at"]))


def _node(node: Mapping[str, object], *, detail: bool) -> None:
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
    temperature = _optional(telemetry.get("sample"), "telemetry sample").get(
        "gpu_temperature_c"
    )
    if telemetry.get("freshness") == "live" and type(temperature) is int:
        _field("Temperature", f"{temperature} °C")
    _field("CPU", _cpu_clock(node))
    _field("NAS route", _nas_route(inventory))
    _field("Wired ports", _wired_ports(inventory))
    loaded = _records(node, "loaded")
    if not loaded:
        print("Running workloads: none")
    for run in loaded:
        _field("Workload", run.get("title"))
        _field("Run", run.get("run_id"))
        _field("State", run.get("run_state"))
        _field("Route", run.get("route_state"))
        if run.get("route_reason") is not None:
            _field("Route reason", run.get("route_reason"))
        _field("Members", _words(run.get("member_node_ids")))
        if run.get("degraded_reason") is not None:
            _reasons([run["degraded_reason"]], subject=name)
    if detail:
        _field("Lifecycle", node.get("lifecycle"))
        for key, value in _object(node.get("labels", {}), "labels").items():
            _field("Label", f"{key}={_text(value)}")
        installed = _records(node, "installed")
        if not installed:
            print("Installed recipes: none")
        for recipe in installed:
            _field("Installed recipe", recipe.get("title"))
            _field("Installation", recipe.get("installation_id"))
            _field("Installation state", recipe.get("group_state"))
            _field("Members", _words(recipe.get("member_node_ids")))
    _reasons(node.get("warnings"), subject=name)


def _size(value: object) -> str:
    """A short human size for tables; the exact byte count stays in --json."""
    if type(value) is not int or value < 0:
        return "unavailable"
    for unit, divisor in (("TiB", 1 << 40), ("GiB", 1 << 30), ("MiB", 1 << 20)):
        if value >= divisor:
            return f"{value / divisor:.1f} {unit}"
    return f"{value} B"


def _memory(node: Mapping[str, object]) -> str:
    """Used share of the Spark's memory, preferring the live telemetry sample."""
    telemetry = _optional(node.get("telemetry"), "telemetry")
    sample = _optional(telemetry.get("sample"), "telemetry sample")
    inventory = _optional(node.get("inventory"), "inventory")
    total, free = (
        (sample.get("memory_total_bytes"), sample.get("memory_available_bytes"))
        if telemetry.get("freshness") == "live"
        else (
            inventory.get("host_memory_total_bytes"),
            inventory.get("host_memory_free_bytes"),
        )
    )
    if type(total) is not int or type(free) is not int or total <= 0:
        return "unavailable"
    return f"{round(100 * (total - free) / total)}% of {_size(total)}"


def _gpu(node: Mapping[str, object]) -> str:
    telemetry = _optional(node.get("telemetry"), "telemetry")
    sample = _optional(telemetry.get("sample"), "telemetry sample")
    value = sample.get("gpu_utilization_percent")
    if telemetry.get("freshness") != "live" or not isinstance(value, (int, float)):
        return "unavailable"
    return f"{round(value)}%"


def _cpu_clock(node: Mapping[str, object]) -> str:
    """Average current CPU clock against the hardware maximum, when reported."""
    telemetry = _optional(node.get("telemetry"), "telemetry")
    sample = _optional(telemetry.get("sample"), "telemetry sample")
    current = sample.get("cpu_frequency_avg_mhz")
    maximum = sample.get("cpu_frequency_max_mhz")
    if telemetry.get("freshness") != "live" or type(current) is not int:
        return "unavailable"
    text = f"{current / 1000:.1f} GHz"
    if type(maximum) is int:
        text = f"{current / 1000:.1f} of {maximum / 1000:.1f} GHz"
    warnings = node.get("warnings")
    if isinstance(warnings, list) and any(
        isinstance(item, Mapping) and item.get("code") == "cpu.low-clock"
        for item in warnings
    ):
        text += " (throttled?)"
    return text


def _nas_route(inventory: Mapping[str, object]) -> str:
    """The interface the NAS is reached through, with its kind, speed and link."""
    route = inventory.get("nas_route_interface")
    interfaces = inventory.get("network_interfaces")
    if route is None or not isinstance(interfaces, list):
        return "unavailable"
    for item in interfaces:
        if isinstance(item, Mapping) and item.get("name") == route:
            speed = item.get("link_speed_mbps")
            parts = [_text(item.get("kind"))]
            if type(speed) is int:
                parts.append(f"{speed} Mb/s")
            parts.append("link up" if item.get("carrier") else "no link")
            return f"{_text(route)} ({', '.join(parts)})"
    return _text(route)


def _wired_ports(inventory: Mapping[str, object]) -> str:
    interfaces = inventory.get("network_interfaces")
    if not isinstance(interfaces, list):
        return "unavailable"
    ports = []
    for item in interfaces:
        if isinstance(item, Mapping) and item.get("kind") == "wired":
            speed = item.get("link_speed_mbps")
            state = f"{speed} Mb/s" if type(speed) is int else "link up"
            ports.append(
                f"{_text(item.get('name'))} ({state if item.get('carrier') else 'no link'})"
            )
    return ", ".join(ports) if ports else "none"


def _status(node: Mapping[str, object]) -> str:
    connection = _object(node.get("connection"), "connection")
    state = _text(connection.get("online_state"))
    telemetry = _optional(node.get("telemetry"), "telemetry").get("freshness")
    if connection.get("online_state") == "online" and telemetry not in {None, "live"}:
        return f"{state}, telemetry {_text(telemetry)}"
    return state


_UPDATE_CODE = "recipe.update_available"


def _is_update_notice(value: object) -> bool:
    return isinstance(value, Mapping) and value.get("code") == _UPDATE_CODE


def _fleet_updates(nodes: Sequence[Mapping[str, object]]) -> list[str]:
    """Runs on an older recipe revision: informational, one line per notice."""
    notes: list[str] = []
    for node in nodes:
        warnings = node.get("warnings")
        for warning in warnings if isinstance(warnings, list) else []:
            if _is_update_notice(warning):
                detail = _text(warning.get("detail"))
                if detail not in notes:
                    notes.append(detail)
    return notes


def _fleet_attention(nodes: Sequence[Mapping[str, object]]) -> list[str]:
    """Collect what the owners report as not healthy, in their own terms."""
    notes: list[str] = []
    for node in nodes:
        name = _text(node.get("display_name"))
        connection = _object(node.get("connection"), "connection")
        if connection.get("online_state") != "online":
            reason = connection.get("offline_reason")
            notes.append(
                f"{name} is {_text(connection.get('online_state'))}"
                + (f" ({_text(reason)})" if reason is not None else "")
                + f"; last seen {_time(connection.get('last_seen_at'))}"
            )
        else:
            telemetry = _optional(node.get("telemetry"), "telemetry").get("freshness")
            if telemetry not in {None, "live"}:
                notes.append(f"{name} telemetry is {_text(telemetry)}")
            inventory = _optional(node.get("inventory"), "inventory").get("freshness")
            if inventory not in {None, "fresh"}:
                notes.append(f"{name} inventory is {_text(inventory)}")
        warnings = node.get("warnings")
        if not isinstance(warnings, list):
            warnings = []
        for warning in warnings:
            if _is_update_notice(warning):
                continue
            if isinstance(warning, Mapping):
                notes.append(
                    f"{name}: {_text(warning.get('detail') or warning.get('code'))}"
                    + _recommendation(warning)
                )
            else:
                notes.append(f"{name}: {_text(warning)}")
    return notes


def _fleet_workloads(nodes: Sequence[Mapping[str, object]], *, wide: bool) -> list[str]:
    """Show each run once, its members by name, and return its problems."""
    names = {node.get("id"): _text(node.get("display_name")) for node in nodes}
    runs: dict[str, list[tuple[str, Mapping[str, object]]]] = {}
    for node in nodes:
        for presence in _records(node, "loaded"):
            identifier = presence.get("run_id")
            if not isinstance(identifier, str) or not identifier:
                raise ValueError("placement lacks its canonical run_id")
            runs.setdefault(identifier, []).append(
                (_text(node.get("display_name")), presence)
            )
    notes: list[str] = []
    if runs:
        print("Workloads")
    for identifier, members in sorted(runs.items()):
        members.sort(key=lambda member: str(member[1].get("rank")))
        first = members[0][1]
        # The alias is the run's name for clients and tells same-recipe runs apart.
        title = _text(first.get("alias") or first.get("title"))
        expected = first.get("expected_rank_count")
        present = first.get("present_ranks")
        ranks = (
            f"{len(present)}/{expected} ranks"
            if isinstance(present, list) and type(expected) is int
            else "ranks unavailable"
        )
        print(f"  {title}")
        print(
            f"    {_text(first.get('run_state'))}, group {_text(first.get('group_state'))}, "
            f"route {_text(first.get('route_state'))}, {ranks}"
        )
        print(f"    Recipe: {_text(first.get('title'))}")
        sparks = ", ".join(
            f"{name} (rank {_text(member.get('rank'))}, {_text(member.get('role'))})"
            for name, member in members
        )
        reported = first.get("member_node_ids")
        outside = [
            _text(member_id)
            for member_id in (reported if isinstance(reported, list) else [])
            if member_id not in names
        ]
        print(
            f"    Sparks: {sparks}"
            + (f"; also {', '.join(outside)}" if outside else "")
        )
        if wide:
            print(f"    Run: {_text(identifier)}")
            print(f"    Installation: {_text(first.get('installation_id'))}")
        if first.get("run_state") != "running":
            notes.append(f"{title} is {_text(first.get('run_state'))}")
        if first.get("group_state") != "healthy":
            notes.append(f"{title} group is {_text(first.get('group_state'))}")
        if first.get("route_state") != cli_states.PUBLISHED:
            route_note = f"{title} route is {_text(first.get('route_state'))}"
            if first.get("route_reason") is not None:
                route_note += f": {_text(first.get('route_reason'))}"
            notes.append(route_note)
        if (
            isinstance(present, list)
            and type(expected) is int
            and len(present) < expected
        ):
            notes.append(f"{title} reports {ranks}")
        stale = [name for name, member in members if member.get("rank_fresh") is False]
        if stale:
            notes.append(f"{title} has stale rank reports from {', '.join(stale)}")
        for _, member in members:
            if member.get("projection_issue") is not None:
                notes.append(f"{title}: {_text(member.get('projection_issue'))}")
                break
            if member.get("degraded_reason") is not None:
                reason = member["degraded_reason"]
                detail = (
                    reason.get("detail") or reason.get("code")
                    if isinstance(reason, Mapping)
                    else reason
                )
                notes.append(f"{title}: {_text(detail)}")
                break
    loaded = {
        member.get("installation_id")
        for members in runs.values()
        for _, member in members
    }
    stopped: dict[str, tuple[str, list[str]]] = {}
    for node in nodes:
        for presence in _records(node, "installed"):
            if presence.get("projection_issue") is not None:
                notes.append(
                    f"{_text(presence.get('title'))}: {_text(presence.get('projection_issue'))}"
                )
            installation = presence.get("installation_id")
            if not isinstance(installation, str) or installation in loaded:
                continue
            entry = stopped.setdefault(installation, (_text(presence.get("title")), []))
            entry[1].append(_text(node.get("display_name")))
    if stopped:
        print("Installed, not running")
        for installation, (title, sparks) in sorted(stopped.items()):
            print(f"  {title} on {', '.join(sparks)}")
            if wide:
                print(f"    Installation: {_text(installation)}")
    if runs or stopped:
        print()
    return notes


def _fleet_overview(payload: Mapping[str, object], *, wide: bool) -> None:
    nodes = _records(payload, "nodes")
    if not nodes:
        print("No Sparks are enrolled.")
        print("Next: vonkctl fleet enroll <name> --output <file>")
        return
    online = sum(
        1
        for node in nodes
        if _object(node.get("connection"), "connection").get("online_state") == "online"
    )
    run_ids = {
        presence.get("run_id")
        for node in nodes
        for presence in _records(node, "loaded")
    }
    plural = "" if len(nodes) == 1 else "s"
    workloads = "1 workload" if len(run_ids) == 1 else f"{len(run_ids)} workloads"
    print(f"Fleet: {len(nodes)} Spark{plural}, {online} online, {workloads} running")
    print()
    labels = ["SPARK", "STATUS", "MEMORY USED", "DISK FREE", "GPU", "RUNNING"]
    if wide:
        labels[5:5] = ["CPU"]
        labels.append("ID")
    rows = []
    for node in nodes:
        inventory = _optional(node.get("inventory"), "inventory")
        loaded = _records(node, "loaded")
        row: list[object] = [
            node.get("display_name"),
            _status(node),
            _memory(node),
            _size(inventory.get("disk_free_bytes")),
            _gpu(node),
            "idle"
            if not loaded
            else f"{len(loaded)} workload" + ("s" if len(loaded) > 1 else ""),
        ]
        if wide:
            row.insert(5, _cpu_clock(node))
            row.append(node.get("id"))
        rows.append(row)
    _table(labels, rows)
    print()
    notes = _fleet_attention(nodes) + _fleet_workloads(nodes, wide=wide)
    if notes:
        print("Needs attention")
        for note in notes:
            print(f"  {note}")
    else:
        print("Nothing needs attention.")
    updates = _fleet_updates(nodes)
    if updates:
        print()
        print("Updates available")
        for note in updates:
            print(f"  {note}")
    if wide:
        _field("Observed", _time(payload.get("generated_at")))
    print()
    print("Next: vonkctl fleet detail <spark>  |  vonkctl profile endpoint")


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
        [(name, _cache_word(local.get("controller")), _words(local.get("running_on")))],
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
                _field(label, _check_word(key, check, assessment))
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
            _field("Engine", item.get("engine"))
            _field("Creator", item.get("creator"))
            _field("Models", _words(item.get("model_selectors")))
            _recipe_options(_optional(item.get("document"), "document"))
            _recipe_alternatives(item)
        else:
            _field("Variant", item.get("variant"))
            _field("Quantization", item.get("quantization"))


_ACTIVE_PREPARATION = {"queued", "running", "pending", "preparing"}


def _cache_word(value: object) -> str:
    return _text(value).replace("_", " ")


def _needs_download(assessment: Mapping[str, object]) -> bool:
    """True when the only thing missing is the cache, not a real blocker."""
    readiness = _optional(assessment.get("readiness"), "readiness")
    cache = _optional(assessment.get("cache"), "cache")
    fit = _optional(assessment.get("fleet_fit"), "fleet_fit")
    reasons = _records(readiness, "reasons")
    return (
        readiness.get("state") == "blocked"
        and cache.get("state") == "blocked"
        and fit.get("state") != "blocked"
        and bool(reasons)
        and all(reason.get("code") == "library.cache_missing" for reason in reasons)
    )


def _check_word(
    key: str, check: Mapping[str, object], assessment: Mapping[str, object]
) -> object:
    """Name an assessment check; a missing download is not a blocker."""
    if key == "cache" and check.get("state") == "blocked":
        reasons = _records(check, "reasons")
        if reasons and all(r.get("code") == "library.cache_missing" for r in reasons):
            return "not cached"
    if key == "readiness" and _needs_download(assessment):
        return "needs download"
    return check.get("state")


def _alternative_line(alternative: Mapping[str, object]) -> str:
    """One comparable line: engine, Sparks, creator, version, cache and fit."""
    sparks = alternative.get("node_count")
    fit = {
        "ready": "fits fleet",
        "blocked": "does not fit fleet",
    }.get(_text(alternative.get("fits_fleet")), "fit unknown")
    return (
        f"{_text(alternative.get('selector'))}  "
        f"{_text(alternative.get('engine'))}, "
        f"{_text(sparks)} Spark{'s' if sparks != 1 else ''}, "
        f"{_text(alternative.get('creator') or 'unknown creator')}, "
        f"v{_text(alternative.get('version'))}, "
        f"{_cache_word(alternative.get('cache'))}, {fit}"
    )


def _recipe_alternatives(item: Mapping[str, object]) -> None:
    """Other recipes for the same model, with a command to open each."""
    alternatives = item.get("alternatives")
    if not isinstance(alternatives, list) or not alternatives:
        return
    print("Other recipes for this model")
    for alternative in alternatives:
        if isinstance(alternative, Mapping):
            print(f"  {_alternative_line(alternative)}")
    print("  Compare: vonkctl recipe detail <recipe>")


def _library_row(item: Mapping[str, object], noun: str) -> list[object]:
    local = _object(item.get("local"), "local")
    resources = _object(item.get("resources"), "resources")
    preparation = _optional(local.get("preparation"), "preparation")
    cache = _cache_word(local.get("controller"))
    if preparation.get("state") in _ACTIVE_PREPARATION:
        cache = progress_line({"progress": preparation}).replace(" | ", ": ")
    running = local.get("running_on")
    row: list[object] = [
        item.get("selector"),
        cache,
        _size(resources.get("disk_bytes")),
        f"{len(running)} Spark{'s' if len(running) != 1 else ''}"
        if isinstance(running, list) and running
        else "no",
    ]
    if noun == "recipe":
        assessment = _optional(item.get("assessment"), "assessment")
        readiness = _optional(assessment.get("readiness"), "readiness")
        row.insert(1, item.get("node_count"))
        row.append(
            _check_word("readiness", readiness, assessment)
            if readiness
            else "not assessed"
        )
    return row


_LIBRARY_FILTER_FLAGS = (
    "cached",
    "model",
    "search",
    "usage",
    "family",
    "version",
    "quantization",
    "publisher",
    "alignment",
    "sparks",
    "engine",
    "creator",
    "updated_since",
    "ready",
    "fits_fleet",
)


def _applied_filters(payload: Mapping[str, object]) -> list[str]:
    """The filters the Controller applied, as the flags that set them."""
    filters = _optional(payload.get("filters"), "filters")
    applied: list[str] = []
    for key in _LIBRARY_FILTER_FLAGS:
        value = filters.get(key)
        flag = "--" + key.replace("_", "-")
        if value is True:
            applied.append(flag)
        elif isinstance(value, list):
            applied.extend(f"{flag} {_text(item)}" for item in value)
        elif isinstance(value, str) and value:
            applied.append(f"{flag} {value}")
    return applied


def _library(
    payload: Mapping[str, object], noun: str, *, detail: bool, wide: bool
) -> None:
    if detail:
        _library_item(payload, noun, detail=True)
        return
    rows = _records(payload, "models" if noun == "model" else "recipes")
    cursor = payload.get("next_cursor")
    more = " (more available)" if isinstance(cursor, str) else ""
    library = _optional(payload.get("library"), "library")
    if library:
        print(
            f"Recipe library v{_text(library.get('version'))}, "
            f"updated {_time(library.get('updated_at'))}"
        )
    print(f"{noun.title()}s: {len(rows)}{more}")
    applied = _applied_filters(payload)
    if not rows:
        if applied:
            print(f"No {noun}s match {' '.join(applied)}.")
        else:
            print(f"The {noun} library is empty.")
    elif wide:
        for item in rows:
            print()
            _library_item(item, noun, detail=True)
    else:
        print()
        labels = [noun.upper(), "CACHE", "DISK", "RUNNING"]
        if noun == "recipe":
            labels.insert(1, "SPARKS")
            labels.append("READY")
        _table(labels, [_library_row(item, noun) for item in rows])
        for item in rows:
            preparation = _optional(
                _object(item.get("local"), "local").get("preparation"), "preparation"
            )
            operation = preparation.get("operation_id")
            if preparation.get("state") in _ACTIVE_PREPARATION and operation:
                _field(
                    "Follow",
                    f"vonkctl {noun} progress {shlex.quote(str(operation))} --follow",
                )
    print()
    if isinstance(cursor, str):
        print(f"Next page: add --cursor {shlex.quote(cursor)} with the same filters.")
    if not rows and applied:
        print(f"Next: vonkctl {noun} library  (no filters)")
    elif not rows:
        print("Next: vonkctl recipe sync-status  (when the catalog last synced)")
    else:
        print(f"Next: vonkctl {noun} detail <{noun}>  (--wide shows every field here)")


def _option_choices(value: object) -> None:
    """One line per option a recipe was assigned with (nothing without options)."""
    if isinstance(value, Mapping):
        for name, chosen in value.items():
            _field("Option", f"{_text(name)}={_text(chosen)}")


def _recipe_options(document: Mapping[str, object]) -> None:
    options = document.get("options")
    if not isinstance(options, list):
        return
    for option in options:
        if not isinstance(option, Mapping):
            continue
        _field("Option", f"{_text(option.get('name'))}: {_text(option.get('label'))}")
        if option.get("help"):
            print(f"  {_text(option['help'])}")
        for choice in option.get("choices") or []:
            if isinstance(choice, Mapping):
                marker = " (default)" if choice.get("default") else ""
                print(
                    f"  - {_text(choice.get('value'))}{marker}: "
                    f"{_text(choice.get('label'))}"
                )
        print(f"  Choose with: --option {_text(option.get('name'))}=VALUE")


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
        _option_choices(assignment.get("option_choices"))
        _field("Observed", match.get("observed_state") if match is not None else None)
        update = match.get("recipe_update") if match is not None else None
        if isinstance(update, Mapping):
            _field("Update", update.get("detail"))
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
        if assignment.get("state") == cli_states.PUBLISHED:
            _field("Client model identifier", alias)
            api_base = endpoint.get("api_base")
            _field("API base (inference gateway)", api_base)
            _field("Route generation", endpoint.get("generation"))
            _field("Route observed at", _time(endpoint.get("observed_at")))
            _field(
                "Freshness",
                _freshness(endpoint.get("observed_at"), payload.get("observed_at")),
            )
            _field("Spark backend (diagnostic)", endpoint.get("backend_api_base"))
            if isinstance(api_base, str) and isinstance(alias, str):
                print("Client configuration:")
                print(
                    "  export OPENAI_BASE_URL=" + terminal_text(shlex.quote(api_base))
                )
                print('  export OPENAI_API_KEY="$(cat CLIENT_KEY_FILE)"')
                print("  model: " + terminal_text(alias))
                print(
                    "Create a client key with: "
                    "vonkctl key create NAME --output CLIENT_KEY_FILE"
                )
            continue
        if alias is not None:
            _field("Client model identifier", alias)
        messages = {
            cli_states.ENDPOINT_INSTALLED_ONLY: "Install-only assignment; no published endpoint was requested.",
            cli_states.ENDPOINT_NOT_PUBLISHED_YET: "The current assignment route is not published yet.",
            cli_states.ENDPOINT_EXPIRED: "The published route lease has expired.",
            cli_states.ENDPOINT_WITHDRAWN: "No current published route is associated with this assignment.",
            cli_states.ENDPOINT_UNAVAILABLE: "The Controller cannot verify a current route for this assignment.",
        }
        print(
            messages.get(str(assignment.get("state")), "Endpoint state is unavailable.")
        )


def _gateway_keys(payload: Mapping[str, object], action: object) -> None:
    if action in ("create", "roll"):
        _field("Key", payload.get("name"))
        _field("Models", _words(payload.get("models") or ["all"]))
        _field("Expires", payload.get("expires_at") or "never")
        if payload.get("output") is not None:
            _field("Written to", payload.get("output"))
        else:
            print(_text(payload.get("key")))
            print("Store this key now; it is not shown again.")
    elif action == "revoke":
        print(f"Revoked key {_text(payload.get('name'))}.")
    else:
        rows = _records(payload, "keys")
        if not rows:
            print("No gateway client keys. Create one with: vonkctl key create NAME")
            return
        _table(
            ("NAME", "MODELS", "CREATED", "EXPIRES", "LAST USED"),
            [
                (
                    row.get("name"),
                    _words(row.get("models") or ["all"]),
                    _time(row.get("created_at")),
                    _time(row.get("expires_at")) if row.get("expires_at") else "never",
                    _time(row.get("last_used_at")),
                )
                for row in rows
            ],
        )


def _preview(payload: Mapping[str, object]) -> None:
    if payload.get("allowed") is True:
        print("Ready for review")
    elif payload.get("waits_for_preparation") is True:
        print("Ready after preparation")
        print("The Controller prepares what is listed below, then continues.")
    else:
        print("Blocked")
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
        _option_choices(assignment.get("option_choices"))
        _reasons(assignment.get("reasons"), subject=assignment.get("recipe_title"))
    for step in (
        _records(payload, "preparation_steps") if "preparation_steps" in payload else []
    ):
        print(f"Prepare {_text(step.get('index'))}. {_text(step.get('label'))}")
        _field("Affected Sparks", _words(step.get("node_ids")))
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
        _field("Image manifest", runtime.get("oci_layout_sha256"))
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


def _artifact_job_record(payload: Mapping[str, object], action: str) -> None:
    _field("Artifact job", payload.get("id"))
    _field("Command", action)
    _field("Run", payload.get("run_id"))
    _field("State", lifecycle_state(dict(payload)))
    if payload.get("cancel_requested_at") is not None:
        _field("Cancel requested", _time(payload.get("cancel_requested_at")))
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

    if isinstance(operation_id, str) and state in ARTIFACT_JOB_IN_FLIGHT:
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
        _field("State", lifecycle_state(dict(job)))
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
        if isinstance(operation_id, str) and job.get("state") in ARTIFACT_JOB_IN_FLIGHT:
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


def _age(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "unavailable"
    return f"{value:.1f}"


def _locks(payload: Mapping[str, object]) -> None:
    held = _records(payload, "held")
    if not held:
        print("No admission locks are held.")
    else:
        _table(
            ("Node", "Holder", "State", "Age (s)", "Query"),
            [
                (
                    row.get("node_id") or row.get("namespace"),
                    row.get("holder"),
                    row.get("state"),
                    _age(row.get("transaction_age_seconds")),
                    row.get("query"),
                )
                for row in held
            ],
        )
    transactions = _records(payload, "open_transactions")
    if transactions:
        print()
        print("Open transactions:")
        _table(
            ("Application", "State", "Age (s)", "Query"),
            [
                (
                    row.get("application_name"),
                    row.get("state"),
                    _age(row.get("transaction_age_seconds")),
                    row.get("query"),
                )
                for row in transactions
            ],
        )


def _activity(
    payload: Mapping[str, object], filters: Mapping[str, object] | None
) -> None:
    if (
        payload.get("operations") is None
        and payload.get("total") is None
        and isinstance(payload.get("projection_issue"), str)
    ):
        _field("Activity observation", payload["projection_issue"])
        return
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
        _waiting(operation)
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
            _fleet_overview(payload, wide=wide)
        elif action == "rename":
            # The Controller answers a rename with the Spark's identity only.
            print(
                f"Renamed {_text(payload.get('id'))} to {_text(payload.get('display_name'))}."
            )
        elif action == "detail":
            _node(payload, detail=True)
        elif action == "progress":
            _job(payload)
        elif action == "activity":
            _activity(payload, activity_filters)
        elif action == "locks":
            _locks(payload)
        elif action == "evidence":
            _field("Operation", payload.get("operation_id"))
            _field("Attempt", payload.get("attempt"))
            _field("File", payload.get("output"))
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
        elif (
            action == "upgrade"
            and payload.get("action") == "upgrade"
            and not payload.get("targets")
            and payload.get("state") == "succeeded"
        ):
            print("All Sparks already run the current agent; nothing to upgrade.")
        elif action == "resume":
            # The Controller answers a resume with the job's identity and its
            # new state only; the work itself is followed through progress.
            job_id = payload.get("id")
            print(
                f"Resumed job {_text(job_id)}; it is now {_text(payload.get('state'))}."
            )
            _field(
                "Next",
                f"vonkctl fleet progress {shlex.quote(str(job_id))} --follow",
            )
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
    elif noun == "recipe" and action == "sync-status":
        _catalog_sync(payload)
    elif noun in {"model", "recipe"}:
        if action == "preview":
            _cache_removal_review(payload)
        elif action in {None, "library", "detail"}:
            _library(payload, noun, detail=action == "detail", wide=wide)
        else:
            _operation(payload, noun)
    elif noun == "key":
        _gateway_keys(payload, action)
    elif noun == "run":
        _run_result(payload)
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
        elif action in {"progress", "load", "cancel"}:
            # These return the profile application, not the saved profile.
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
    """Describe measured work, and what it waits for when it is waiting."""
    line = _measured_progress(observed)
    blockers = observed.get("blockers")
    first = blockers[0] if isinstance(blockers, list) and blockers else None
    if isinstance(first, Mapping):
        return f"{line} (waiting: {_text(first.get('code'))})"
    return line


def _measured_progress(observed: Mapping[str, object]) -> str:
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
