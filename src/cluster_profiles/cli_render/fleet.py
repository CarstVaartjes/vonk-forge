"""Terminal presentation for fleet responses."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum

from .. import cli_states
from ..cli_states_generated import (
    ENDPOINT_UNAVAILABLE,
    RUN_RUNNING,
    UNKNOWN,
    GpuClockMetric,
)
from ..cli_telemetry_render import NOT_REPORTED, gpu_readings, round_half_up
from ..cli_telemetry_render import size as _size
from ..generated_control.models.display_memory_observation import (
    DisplayMemoryObservation,
)
from ..generated_control.models.gpu_clock_observation import GpuClockObservation
from ..generated_control.models.gpu_clock_policy import GpuClockPolicy
from ..generated_control.models.gpu_clock_range import GpuClockRange
from ..generated_control.models.run_presence import RunPresence
from ..generated_control.models.swap_checkpoint import SwapCheckpoint
from ..generated_control.models.swap_intent import SwapIntent
from ..generated_control.models.swap_observation import SwapObservation
from ..generated_control.models.swap_policy import SwapPolicy
from ..generated_control.types import Unset
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
from .throttle import throttle_words

NO_SPARKS = "No Sparks are enrolled."


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
    _field("GPU clock", _gpu_clock(node))
    host = _optional(inventory.get("host_settings"), "host settings")
    _field("Swap", _swap(node))
    raw_memory = host.get("display_memory")
    observation = (
        None
        if raw_memory is None
        else DisplayMemoryObservation.from_dict(
            dict(_optional(raw_memory, "display memory"))
        )
    )
    _field("Display memory", _display_memory(observation))
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
        # An unavailable presence (no rank freshness) names no swap it holds.
        if run.get("rank_fresh") is not None:
            held = RunPresence.from_dict(dict(run)).load_swap_held_bytes
            if isinstance(held, int):
                _field("Swap held while loading", _size(held))
        if run.get("degraded_reason") is not None:
            _reasons([run["degraded_reason"]], subject=name)
    if detail:
        runtime = _optional(node.get("agent_runtime"), "agent runtime identity")
        _field("Agent version", runtime.get("semantic_version", UNKNOWN))
        _field("Agent build", runtime.get("build_digest", UNKNOWN))
        _field("Agent binary", runtime.get("binary_digest", UNKNOWN))
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


def _display_memory(observation: DisplayMemoryObservation | None) -> str:
    if observation is None:
        return UNKNOWN
    firmware = observation.soc_firmware_version
    version = f"0x{firmware:08x}" if type(firmware) is int else UNKNOWN
    reserved = observation.carveout_bytes
    amount = _bytes(reserved) if type(reserved) is int else UNKNOWN
    connected = observation.display_connected
    display = str(connected).lower() if type(connected) is bool else UNKNOWN
    return f"reserved {amount} · SoC {version} · UEFI {_text(observation.bios_version) if isinstance(observation.bios_version, str) else UNKNOWN} · display connected {display}"


def _live(node: object) -> bool:
    telemetry = _optional(
        _object(node, "Fleet node").get("telemetry"), "telemetry state"
    )
    return telemetry.get("freshness") == "live"


def _live_sample(node: object):
    """The telemetry sample while telemetry is live, otherwise nothing."""
    telemetry = _optional(
        _object(node, "Fleet node").get("telemetry"), "telemetry state"
    )
    if telemetry.get("freshness") != "live":
        return {}
    return _optional(telemetry.get("sample"), "telemetry sample")


def _memory(node: object) -> str:
    """Used memory beside the total, in the words the Controller web uses."""
    sample = _live_sample(node)
    inventory = _optional(
        _object(node, "Fleet node").get("inventory"), "node inventory"
    )
    total, free = (
        (sample.get("memory_total_bytes"), sample.get("memory_available_bytes"))
        if _live(node)
        else (
            inventory.get("host_memory_total_bytes"),
            inventory.get("host_memory_free_bytes"),
        )
    )
    if type(total) is not int or type(free) is not int or total <= 0:
        return NOT_REPORTED
    percent = round_half_up(100 * ((total - free) / total))
    return f"{_size(total - free)} of {_size(total)} ({percent}%)"


def _run_state_word(healthy: object, run_state: object) -> str:
    """One workload's state in a word, as the Controller web Fleet page says it.

    Health only qualifies a run that is running; any other run state is shown as is.
    """
    if run_state != RUN_RUNNING:
        return _text(run_state)
    if healthy is None:
        return "state unknown"
    return "degraded" if healthy is False else _text(run_state)


def _node_workloads(node: object, names: dict[object, str]) -> list[str]:
    """What a Spark serves, one line per run: title, alias, state, and its partners."""
    document = _object(node, "Fleet node")
    seen: set[object] = set()
    lines: list[str] = []
    for run in _records(document, "loaded"):
        if run.get("run_id") in seen:
            continue
        seen.add(run.get("run_id"))
        members = run.get("member_node_ids")
        partners = [
            names.get(member, _text(member))
            for member in (members if isinstance(members, list) else [])
            if member != document.get("id")
        ]
        parts = [
            _text(run.get("title") or run.get("run_id")),
            _text(run.get("alias")) if run.get("alias") else "",
            _run_state_word(run.get("healthy"), run.get("run_state")),
            f"with {', '.join(partners)}" if partners else "",
        ]
        lines.append(" · ".join(part for part in parts if part))
    return lines


def _disk(node: object) -> str:
    """Free disk beside the total: the inventory's pair, else the live sample's."""
    inventory = _optional(
        _object(node, "Fleet node").get("inventory"), "node inventory"
    )
    for source in (inventory, _live_sample(node)):
        free, total = source.get("disk_free_bytes"), source.get("disk_total_bytes")
        if type(free) is int and type(total) is int and total > 0:
            return f"{_size(free)} free of {_size(total)}"
    return NOT_REPORTED


def _gpu(node: object) -> str:
    sample = _live_sample(node)
    if not _live(node):
        return NOT_REPORTED
    return gpu_readings(
        sample.get("gpu_utilization_percent"),
        sample.get("gpu_temperature_c"),
        sample.get("gpu_unavailable_reason"),
    )


def _gpu_clock(node: object) -> str:
    document = _object(node, "Fleet node")
    telemetry = _optional(document.get("telemetry"), "fleet telemetry evidence")
    sample = _optional(telemetry.get("sample"), "sample")
    clock = GpuClockObservation.from_dict(
        dict(_optional(sample.get("gpu_clock"), "GPU clock"))
    )
    desired_document = _optional(document.get("gpu_clock_desired"), "GPU desired range")
    desired = (
        GpuClockRange.from_dict(dict(desired_document)) if desired_document else None
    )
    policy_document = _optional(document.get("gpu_clock_policy"), "GPU clock policy")
    policy = (
        GpuClockPolicy.from_dict(dict(policy_document)) if policy_document else None
    )
    applied = (
        clock.locked_range if isinstance(clock.locked_range, GpuClockRange) else None
    )

    def clock_range(value: GpuClockRange | None) -> str:
        return f"{value.min_mhz}–{value.max_mhz} MHz" if value is not None else UNKNOWN

    def metric(value: object, name: GpuClockMetric) -> str:
        if (
            (value is None or isinstance(value, Unset))
            and not isinstance(clock.unavailable, Unset)
            and clock.unavailable is not None
        ):
            for failure in clock.unavailable:
                if failure.metric == name:
                    return f"{failure.reason.replace('-', ' ')} ({failure.detail})"
        return _clock_reading(value)

    reasons = clock.throttle_reasons
    throttle = (
        throttle_words(reasons)
        if isinstance(reasons, int)
        else metric(reasons, GpuClockMetric.THROTTLE)
    )
    return (
        f"graphics {metric(clock.graphics_mhz, GpuClockMetric.GRAPHICS)} / SM {metric(clock.sm_mhz, GpuClockMetric.SM)} MHz; "
        f"last applied {clock_range(applied)} (helper receipt); desired {clock_range(desired)}; "
        f"reported max graphics {metric(clock.max_graphics_mhz, GpuClockMetric.MAX_GRAPHICS)} / SM {metric(clock.max_sm_mhz, GpuClockMetric.MAX_SM)} MHz; "
        f"policy limit {_clock_reading(policy.hard_max_mhz if policy is not None else None)} MHz; throttle reasons {throttle}; "
        f"{_clock_reading(clock.apply_state)}; next {_clock_reading(clock.next_attempt_at)}; deadline {_clock_reading(clock.deadline)}; "
        f"{_clock_reading(clock.detail)}"
        + (
            f"; receipt {clock.receipt_detail}"
            if isinstance(clock.receipt_detail, str)
            else ""
        )
        + (
            "; No GPU clock observation received from monitor"
            if not sample.get("gpu_clock")
            else ""
        )
    )


def _swap(node: object) -> str:
    """Active swap beside the desired size and the helper's last receipt."""
    document = _object(node, "Fleet node")
    inventory = _optional(document.get("inventory"), "Spark inventory evidence")
    host = _optional(inventory.get("host_settings"), "host settings")
    desired_document = _optional(document.get("swap_desired"), "swap desired")
    desired = SwapIntent.from_dict(dict(desired_document)) if desired_document else None
    policy_document = _optional(document.get("swap_policy"), "swap policy")
    policy = SwapPolicy.from_dict(dict(policy_document)) if policy_document else None
    wanted = (
        f"desired {desired.size_gib} GiB"
        if desired is not None
        else f"desired {UNKNOWN}"
    ) + (
        f" (policy {policy.minimum_gib}–{policy.maximum_gib} GiB)"
        if policy is not None
        else ""
    )
    swap_document = _optional(host.get("swap"), "swap")
    if not swap_document:
        return f"{UNKNOWN}; {wanted}"
    swap = SwapObservation.from_dict(dict(swap_document))
    actual = f"{_bytes(swap.total_bytes)} active, {_bytes(swap.free_bytes)} free"
    receipt = swap.receipt if isinstance(swap.receipt, SwapCheckpoint) else None
    if receipt is None:
        detail = (
            swap.receipt_detail if isinstance(swap.receipt_detail, str) else UNKNOWN
        )
        return f"{actual}; {wanted}; receipt {detail}"
    reason = receipt.reason
    return (
        f"{actual}; {wanted}; {_clock_reading(receipt.state)}"
        + (
            f" ({_clock_reading(reason)})"
            if reason is not None and not isinstance(reason, Unset)
            else ""
        )
        + f"; next {_clock_reading(receipt.next_attempt_at)}"
        + f"; deadline {_clock_reading(receipt.deadline)}"
        + f"; {_clock_reading(receipt.detail)}"
    )


def _clock_reading(value: object) -> str:
    if value is None or isinstance(value, Unset):
        return UNKNOWN
    return _text(value.value if isinstance(value, Enum) else value)


def _cpu_clock(node: Mapping[str, object]) -> str:
    """Average current CPU clock against the hardware maximum, when reported."""
    telemetry = _optional(node.get("telemetry"), "telemetry")
    sample = _optional(telemetry.get("sample"), "telemetry sample")
    current = sample.get("cpu_frequency_avg_mhz")
    maximum = sample.get("cpu_frequency_max_mhz")
    if telemetry.get("freshness") != "live" or type(current) is not int:
        return ENDPOINT_UNAVAILABLE
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
        return ENDPOINT_UNAVAILABLE
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
        return ENDPOINT_UNAVAILABLE
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


def _needs_attention(node: object) -> bool:
    """A Spark needs attention when it is offline or warns, or its inventory is stale."""
    return bool(_fleet_attention([_object(node, "Fleet node")]))


def _is_serving(run: object) -> bool:
    """A workload is serving while it is running and reported healthy."""
    document = _object(run, "run")
    return document.get("run_state") == RUN_RUNNING and document.get("healthy") is True


def _free_memory_text(online: Sequence[object]) -> str:
    """Free memory across the online Sparks, saying so when only some reported it."""
    samples = [
        _optional(
            _optional(
                _object(node, "Fleet node").get("telemetry"),
                "fleet telemetry evidence",
            ).get("sample"),
            "fleet telemetry sample",
        ).get("memory_available_bytes")
        for node in online
    ]
    free = [value for value in samples if type(value) is int]
    if not free:
        return "unified memory not reported"
    text = f"{_size(sum(free))} unified memory free"
    return (
        text
        if len(free) == len(online)
        else f"{text} on {len(free)} of {len(online)} online Sparks"
    )


def _fleet_status_line(payload: object) -> str:
    """The Fleet in one line, in the words the Controller web Fleet page uses."""
    document = _object(payload, "Fleet")
    nodes = _records(document, "nodes")
    if not nodes:
        return NO_SPARKS
    online = [
        node
        for node in nodes
        if _object(node.get("connection"), "connection").get("online_state") == "online"
    ]
    serving = len(
        {
            run.get("run_id")
            for node in nodes
            for run in _records(node, "loaded")
            if _is_serving(run)
        }
    )
    sparks = sum(1 for node in nodes if _needs_attention(node))
    firing = document.get("attention")
    alerts = len(firing) if isinstance(firing, list) else 0
    attention = [
        *(
            [f"{sparks} {'Spark needs' if sparks == 1 else 'Sparks need'} attention"]
            if sparks
            else []
        ),
        *(
            [f"{alerts} {'alert' if alerts == 1 else 'alerts'} firing"]
            if alerts
            else []
        ),
        *(
            ["alerts not reported"]
            if not alerts and document.get("attention_unavailable")
            else []
        ),
    ]
    return " · ".join(
        (
            f"{len(online)} of {len(nodes)} {'Spark' if len(nodes) == 1 else 'Sparks'} online",
            _free_memory_text(online),
            f"{serving} {'workload' if serving == 1 else 'workloads'} serving"
            if serving
            else "no workloads serving",
            *(attention or ["nothing needs attention"]),
        )
    )


def _alert_notes(payload: object) -> list[str]:
    """Firing Prometheus alerts as `Severity: summary (Spark)`, as the Controller web says them."""
    document = _object(payload, "Fleet")
    names = {
        node.get("id"): _text(node.get("display_name"))
        for node in _records(document, "nodes")
    }
    notes: list[str] = []
    alerts = document.get("attention")
    for alert in alerts if isinstance(alerts, list) else []:
        record = _object(alert, "alert")
        node_id = _optional(record.get("labels"), "alert labels").get("node_id")
        severity = _text(record.get("severity"))
        notes.append(
            f"{severity[:1].upper()}{severity[1:]}: {_text(record.get('summary'))}"
            + (f" ({names.get(node_id, _text(node_id))})" if node_id else "")
        )
    if document.get("attention_unavailable"):
        notes.append("Prometheus alerts unavailable. Retrying automatically.")
    return notes


def _fleet_workloads(nodes: Sequence[Mapping[str, object]], *, wide: bool) -> list[str]:
    """Show each run once, its members by name, and return its problems."""
    names = {node.get("id"): _text(node.get("display_name")) for node in nodes}
    runs: dict[str, list[tuple[str, Mapping[str, object]]]] = {}
    for node in nodes:
        for presence in _records(node, "loaded"):
            identifier = presence.get("run_id")
            if not isinstance(identifier, str) or not identifier:
                _field("Run identity", ENDPOINT_UNAVAILABLE)
                continue
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
        print(_fleet_status_line(payload))
        print("Next: vonkctl fleet enroll <name> --output <file>")
        return
    print(_fleet_status_line(payload))
    print()
    labels = ["SPARK", "STATUS", "MEMORY USED", "DISK FREE", "GPU", "SERVING"]
    if wide:
        labels[5:5] = ["CPU"]
        labels.append("ID")
    names = {node.get("id"): _text(node.get("display_name")) for node in nodes}
    rows = []
    for node in nodes:
        row: list[object] = [
            node.get("display_name"),
            _status(node),
            _memory(node),
            _disk(node),
            _gpu(node),
            "; ".join(_node_workloads(node, names)) or "Idle",
        ]
        if wide:
            row.insert(5, _cpu_clock(node))
            row.append(node.get("id"))
        rows.append(row)
    _table(labels, rows)
    print()
    notes = (
        _alert_notes(payload)
        + _fleet_attention(nodes)
        + _fleet_workloads(nodes, wide=wide)
    )
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
