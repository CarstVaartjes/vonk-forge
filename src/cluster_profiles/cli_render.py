"""Small terminal renderer shared by the four operator nouns."""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping

from .cli_select import selector_for


def _cell(value: object, width: int = 44) -> str:
    if value is None:
        return "—"
    if isinstance(value, (dict, list)):
        text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _rows(payload: Mapping[str, object], noun: str) -> list[Mapping[str, object]]:
    preferred = {
        "fleet": ("nodes", "sparks", "fleet"),
        "model": ("models", "entries", "items"),
        "recipe": ("recipes", "entries", "items"),
        "profile": ("profiles", "items"),
    }[noun]
    for key in preferred:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
    return []


def _label(value: object, fallback: str = "—") -> str:
    return str(value) if value is not None else fallback


def _row_value(row: Mapping[str, object], key: str) -> object:
    """Read the current projection's nested resource/cache fields for tables."""
    if key in row:
        return row[key]
    nested_paths = {
        "display_name": (("identity", "display_name"),),
        "cache_state": (("cache", "state"), ("cache", "status")),
        "running_on": (("running", "spark_ids"), ("running", "nodes")),
        "disk_bytes": (("artifact", "size_bytes"), ("resources", "disk_bytes")),
        "model_name": (("model", "name"),),
        "state": (("operation", "state"),),
    }
    for path in nested_paths.get(key, ()):
        value: object = row
        for part in path:
            if not isinstance(value, Mapping):
                break
            value = value.get(part)
        else:
            if value is not None:
                return value
    return None


def render_payload(payload: Mapping[str, object], noun: str, *, wide: bool = False) -> None:
    """Render an adaptive snapshot without cursor control or color assumptions."""
    title = payload.get("title") or payload.get("heading") or noun.title()
    print(str(title))
    rows = _rows(payload, noun)
    if rows:
        columns_by_noun: dict[str, tuple[tuple[str, str], ...]] = {
            "fleet": (
                ("display_name", "SPARK"),
                ("state", "STATE"),
                ("operational_state", "HEALTH"),
                ("running", "RUNNING"),
                ("client_state", "CLIENT"),
            ),
            "model": (
                ("name", "MODEL"),
                ("selector", "USE"),
                ("usage", "USAGE"),
                ("cache_state", "CACHE"),
                ("running_on", "RUNNING ON"),
                ("disk_bytes", "DISK"),
            ),
            "recipe": (
                ("name", "RECIPE"),
                ("selector", "USE"),
                ("model_name", "MODEL"),
                ("cache_state", "CACHE"),
                ("running_on", "RUNNING ON"),
                ("update_state", "UPDATE"),
            ),
            "profile": (
                ("number", "PROFILE"),
                ("name", "NAME"),
                ("state", "STATE"),
                ("match_state", "MATCH"),
            ),
        }
        columns = columns_by_noun[noun]
        if not wide and shutil.get_terminal_size((80, 24)).columns < 80:
            columns = columns[:3]
        rendered = [[_cell(_row_value(row, key)) for key, _ in columns] for row in rows]
        widths = [
            max(len(label), *(len(row[i]) for row in rendered))
            for i, (_, label) in enumerate(columns)
        ]
        print("  ".join(label.ljust(widths[i]) for i, (_, label) in enumerate(columns)))
        print("  ".join("─" * width for width in widths))
        for index, row in enumerate(rendered):
            print("  ".join(value.ljust(widths[i]) for i, value in enumerate(row)))
            selector = selector_for(rows[index]) if noun in {"model", "recipe"} else None
            if selector and noun in {"model", "recipe"}:
                print(f"  USE {selector}")
    else:
        priority = (
            "state",
            "status",
            "selector",
            "name",
            "number",
            "operation_id",
            "phase",
            "next_action",
        )
        keys = [key for key in priority if key in payload]
        keys.extend(sorted(set(payload) - set(keys)))
        for key in keys:
            if key in {"title", "heading"}:
                continue
            print(f"{key.replace('_', ' ')}: {_cell(payload[key], 120)}")
    warnings = payload.get("warnings")
    if isinstance(warnings, list) and warnings:
        print("Info: " + "; ".join(_cell(item, 140) for item in warnings))
    actions = payload.get("next_actions")
    if isinstance(actions, list) and actions:
        print("Next: " + "; ".join(_cell(item, 140) for item in actions))


def progress_line(observed: Mapping[str, object]) -> str:
    """Format truthful transfer/build progress; unknown totals stay unknown."""
    phase = str(observed.get("phase") or observed.get("state") or "working")
    progress = observed.get("progress")
    if isinstance(progress, Mapping):
        completed = progress.get("completed_bytes", progress.get("transferred_bytes"))
        total = progress.get("total_bytes")
        if type(completed) is int and type(total) is int and total > 0:
            percent = min(100, max(0, completed * 100 // total))
            return f"{phase} {percent}% · {completed} / {total} bytes"
        step = progress.get("step")
        steps = progress.get("steps")
        if step is not None and steps is not None:
            return f"{phase} · step {step}/{steps}"
    return phase + " · progress unavailable"
