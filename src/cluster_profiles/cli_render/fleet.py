"""Terminal presentation for fleet responses."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .. import cli_states
from ..cli_telemetry_render import gpu_readings
from ..cli_telemetry_render import size as _size
from .common import (
    _bytes,
    _field,
    _is_update_notice,
    _object,
    _optional,
    _projection_issues,
    _reasons,
    _recommendation,
    _records,
    _table,
    _text,
    _time,
    _warn,
    _words,
)


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
        for key, value in _optional(node.get("labels", {}), "labels").items():
            _field("Label", f"{key}={_text(value)}")
        installed = _records(node, "installed")
        if not installed:
            print("Installed recipes: none")
        for recipe in installed:
            _field("Installed recipe", recipe.get("title"))
            _field("Installation", recipe.get("installation_id"))
            _field("Installation state", recipe.get("group_state"))
            _field("Members", _words(recipe.get("member_node_ids")))
    for issue in _projection_issues(node.get("projection_issues")):
        _warn(f"{_text(name)}: {_text(issue)}")
    _reasons(node.get("warnings"), subject=name)


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
    if telemetry.get("freshness") != "live":
        return "unavailable"
    return gpu_readings(
        sample.get("gpu_utilization_percent"),
        sample.get("gpu_temperature_c"),
        sample.get("gpu_unavailable_reason"),
    )


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
        for issue in _projection_issues(node.get("projection_issues")):
            notes.append(f"{name}: {_text(issue)}")
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
