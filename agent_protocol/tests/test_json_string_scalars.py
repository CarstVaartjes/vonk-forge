"""A UUID or timestamp is read from its JSON string in every validation mode.

Since the page-cache drops shipped (2026-10-27) every failed start lost all of
its diagnostics at the Controller: its ingress validates the decoded body in
strict Python mode, which refused the drop reports' ``request_id`` and
``dropped_at`` strings, so the whole diagnostics group was dropped.
``recipe-start-page-cache-v1`` is that failure as the Rust agent sends it (its
test produces the vector).
"""

from __future__ import annotations

import copy
import importlib
import json
import pkgutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import vonk_agent_protocol
from pydantic import BaseModel
from vonk_agent_protocol import AgentFailureResult, AgentResult
from vonk_agent_protocol.optional_evidence import EvidenceDrop
from vonk_agent_protocol.page_cache import PageCacheDropProblem, PageCacheDropReport
from vonk_agent_protocol.reason_codes import AgentEvidenceCode
from vonk_agent_protocol.wire_model import JSON_STRING_BOUND, JSON_STRING_SCALARS

VECTORS = Path(__file__).parents[1] / "src/vonk_agent_protocol/vectors"
PAGE_CACHE = ("result", "evidence", "diagnostics", "page_cache")

PARSERS: dict[str, Callable[[Any], AgentResult]] = {
    # FastAPI decodes the body before validating it: Python mode.
    "python": AgentResult.model_validate,
    "parse": AgentResult.parse,
    "json": lambda document: AgentResult.model_validate_json(json.dumps(document)),
}


def _vector() -> Any:
    return json.loads((VECTORS / "recipe-start-page-cache-v1.json").read_text())


@pytest.mark.parametrize("mode", PARSERS)
def test_a_start_failure_keeps_its_page_cache_drops_in_every_mode(mode: str) -> None:
    accepted = PARSERS[mode](_vector())

    assert accepted.evidence_warnings == ()
    stored = accepted.stored_result()
    assert isinstance(stored, AgentFailureResult)
    assert stored.diagnostics is not None
    assert stored.diagnostics.stderr.text.startswith("RuntimeError")
    drops = stored.diagnostics.page_cache
    assert drops is not None
    assert drops.last_problem is not None
    assert drops.last_problem.problem is PageCacheDropProblem.DROP_IN_PROGRESS
    assert drops.last is not None and drops.last.dropped_at.tzinfo is not None


def test_a_report_read_from_its_json_strings_round_trips_unchanged() -> None:
    document = _vector()["result"]["evidence"]["diagnostics"]["page_cache"]["last"]

    report = PageCacheDropReport.model_validate(document)

    assert json.loads(report.model_dump_json(exclude_none=True)) == document


@pytest.mark.parametrize(
    ("field", "value"),
    [("request_id", "not-a-uuid"), ("dropped_at", "2026-10-27T23:11:01")],
)
def test_a_string_the_json_contract_refuses_is_still_refused(
    field: str, value: str
) -> None:
    document = dict(
        _vector()["result"]["evidence"]["diagnostics"]["page_cache"]["last"]
    )
    document[field] = value

    with pytest.raises(ValueError):
        PageCacheDropReport.model_validate(document)
    with pytest.raises(ValueError):
        PageCacheDropReport.model_validate_json(json.dumps(document))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dropped_at", "1761606661"),
        ("dropped_at", "2026-10-27"),
        ("dropped_at", "2026-10-27 23:11:01Z"),
        ("dropped_at", "2026-10-27T23:11:01.123456789Z"),
        ("request_id", "7ee6ed1ccd7a46399498e4e4e543d849"),
        ("request_id", "urn:uuid:7ee6ed1c-cd7a-4639-9498-e4e4e543d849"),
    ],
)
def test_python_mode_accepts_a_string_exactly_when_json_mode_does(
    field: str, value: str
) -> None:
    document = dict(
        _vector()["result"]["evidence"]["diagnostics"]["page_cache"]["last"]
    )
    document[field] = value

    def accepts(parse: Callable[[], object]) -> bool:
        try:
            parse()
        except ValueError:
            return False
        return True

    assert accepts(lambda: PageCacheDropReport.model_validate(document)) == accepts(
        lambda: PageCacheDropReport.model_validate_json(json.dumps(document))
    )


@pytest.mark.parametrize("mode", PARSERS)
def test_a_bad_page_cache_report_costs_only_itself(mode: str) -> None:
    document = copy.deepcopy(_vector())
    document["result"]["evidence"]["diagnostics"]["page_cache"]["first"][
        "cached_before_bytes"
    ] = -1

    accepted = PARSERS[mode](document)

    assert accepted.evidence_warnings == (AgentEvidenceCode.FAILURE_PAGE_CACHE_DROPPED,)
    assert accepted.evidence_drops == (
        EvidenceDrop(
            code=AgentEvidenceCode.FAILURE_PAGE_CACHE_DROPPED,
            location=".".join((*PAGE_CACHE, "first", "cached_before_bytes")),
            error_type="greater_than_equal",
        ),
    )
    stored = accepted.stored_result()
    assert isinstance(stored, AgentFailureResult)
    assert stored.diagnostics is not None
    assert stored.diagnostics.page_cache is None
    assert stored.diagnostics.stderr.text.startswith("RuntimeError")


def test_a_dropped_group_names_the_field_that_refused_it() -> None:
    document = copy.deepcopy(_vector())
    document["result"]["evidence"]["diagnostics"]["phase"] = ""

    accepted = AgentResult.model_validate(document)

    assert accepted.evidence_drops == (
        EvidenceDrop(
            code=AgentEvidenceCode.FAILURE_DIAGNOSTICS_DROPPED,
            location="result.evidence.diagnostics.phase",
            error_type="string_too_short",
        ),
    )


def _strict_models() -> list[type[BaseModel]]:
    for module in pkgutil.walk_packages(
        vonk_agent_protocol.__path__, f"{vonk_agent_protocol.__name__}."
    ):
        importlib.import_module(module.name)
    found: list[type[BaseModel]] = []
    pending: list[type[BaseModel]] = [BaseModel]
    while pending:
        model = pending.pop()
        for subclass in model.__subclasses__():
            pending.append(subclass)
            if subclass.__module__.startswith(
                vonk_agent_protocol.__name__
            ) and subclass.model_config.get("strict"):
                found.append(subclass)
    return found


def _unbound_scalars(node: object, *, bound: bool = False) -> list[str]:
    if isinstance(node, list):
        return [found for item in node for found in _unbound_scalars(item)]
    if not isinstance(node, dict):
        return []
    scalar = node.get("type")
    if isinstance(scalar, str) and scalar in JSON_STRING_SCALARS:
        return [] if bound else [scalar]
    metadata = node.get("metadata")
    binder = isinstance(metadata, dict) and bool(metadata.get(JSON_STRING_BOUND))
    return [
        found
        for key, value in node.items()
        for found in _unbound_scalars(value, bound=binder and key == "schema")
    ]


def test_every_strict_protocol_model_reads_uuids_and_timestamps_from_json() -> None:
    """Guard: no strict model refuses a UUID/timestamp string in Python mode."""

    unbound = {
        model.__qualname__: found
        for model in _strict_models()
        if isinstance(model.__pydantic_core_schema__, dict)
        and (found := _unbound_scalars(model.__pydantic_core_schema__))
    }

    assert unbound == {}
