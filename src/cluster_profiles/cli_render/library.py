"""Terminal presentation for library responses."""

from __future__ import annotations

import shlex
from collections.abc import Mapping

from ..cli_telemetry_render import size as _size
from .common import (
    _bytes,
    _field,
    _object,
    _optional,
    _records,
    _table,
    _text,
    _time,
    _words,
)
from .progress import progress_line


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
