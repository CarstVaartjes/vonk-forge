"""Recipe fixture and case parsing."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace

from .assertions import _parse_assertion
from .contracts import (
    _CASE_ID,
    _DIGEST,
    _INTERFACES,
    _KEY,
    _MEDIA_TYPE,
    _SLOT,
    Fixture,
    FixtureError,
    RecipeFixture,
    _OutputLimits,
)
from .values import _integer, _object


def _blocker(code: str, detail: str) -> dict[str, str]:
    return {"classification": "fixture", "code": code, "detail": detail}


def _parse_recipe_fixture(
    key: object, raw: object, fixtures: Mapping[str, Fixture]
) -> RecipeFixture:
    if not isinstance(key, str) or not _KEY.fullmatch(key):
        raise FixtureError("recipe fixture key is invalid")
    item = _object(raw, f"recipe fixture {key}")
    allowed = {
        "content_sha256",
        "interface",
        "parameters",
        "inputs",
        "output_limits",
        "timeout_seconds",
        "assertions",
        "cases",
    }
    if set(item) - allowed:
        raise FixtureError(f"recipe fixture {key} fields are invalid")
    base = {name: value for name, value in item.items() if name != "cases"}
    primary = _parse_recipe_case(key, base, fixtures)
    raw_cases = item.get("cases", [])
    if not isinstance(raw_cases, list) or len(raw_cases) > 16:
        raise FixtureError(f"recipe fixture {key} cases are invalid")
    case_fields = {
        "id",
        "parameters",
        "inputs",
        "output_limits",
        "timeout_seconds",
        "assertions",
    }
    parsed_cases: list[RecipeFixture] = []
    case_ids = {primary.case_id}
    for raw_case in raw_cases:
        case = _object(raw_case, f"recipe fixture {key} case")
        case_id = case.get("id")
        if (
            not set(case).issubset(case_fields)
            or len(case) < 2
            or not isinstance(case_id, str)
            or not _CASE_ID.fullmatch(case_id)
            or case_id in case_ids
        ):
            raise FixtureError(f"recipe fixture {key} case identity is invalid")
        parsed = _parse_recipe_case(
            key,
            {**base, **{name: value for name, value in case.items() if name != "id"}},
            fixtures,
        )
        parsed_cases.append(replace(parsed, case_id=case_id))
        case_ids.add(case_id)
    return replace(primary, supplemental_cases=tuple(parsed_cases))


def _parse_recipe_case(
    key: object, raw: object, fixtures: Mapping[str, Fixture]
) -> RecipeFixture:
    if not isinstance(key, str) or not _KEY.fullmatch(key):
        raise FixtureError("recipe fixture key is invalid")
    item = _object(raw, f"recipe fixture {key}")
    digest = item.get("content_sha256")
    interface = item.get("interface")
    parameters = item.get("parameters")
    inputs = item.get("inputs")
    limits = _object(item.get("output_limits"), f"recipe fixture {key} output_limits")
    timeout = item.get("timeout_seconds")
    assertions = item.get("assertions")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise FixtureError(f"recipe fixture {key} digest is invalid")
    if interface not in _INTERFACES:
        raise FixtureError(f"recipe fixture {key} interface is invalid")
    if (
        not isinstance(parameters, dict)
        or len(json.dumps(parameters).encode()) > 16_384
    ):
        raise FixtureError(f"recipe fixture {key} parameters are invalid")
    if not isinstance(inputs, list) or len(inputs) > 32:
        raise FixtureError(f"recipe fixture {key} inputs are invalid")
    parsed_inputs: list[tuple[str, Fixture]] = []
    names: set[str] = set()
    for raw_input in inputs:
        input_item = _object(raw_input, f"recipe fixture {key} input")
        slot = input_item.get("slot")
        fixture_id = input_item.get("fixture")
        if not isinstance(slot, str) or not _SLOT.fullmatch(slot):
            raise FixtureError(f"recipe fixture {key} input slot is invalid")
        if not isinstance(fixture_id, str) or fixture_id not in fixtures:
            raise FixtureError(f"recipe fixture {key} references an unknown fixture")
        fixture = fixtures[fixture_id]
        if fixture.name in names:
            raise FixtureError(f"recipe fixture {key} input names are not unique")
        names.add(fixture.name)
        parsed_inputs.append((slot, fixture))
    max_files = _integer(limits.get("max_files"), "max_files", 1, 32)
    max_file_bytes = _integer(
        limits.get("max_file_bytes"), "max_file_bytes", 1, 1024**3
    )
    max_total_bytes = _integer(
        limits.get("max_total_bytes"), "max_total_bytes", 1, 2 * 1024**3
    )
    raw_allowed = limits.get("allowed_media_types")
    if (
        not isinstance(raw_allowed, list)
        or not 1 <= len(raw_allowed) <= 16
        or len(set(raw_allowed)) != len(raw_allowed)
        or any(
            not isinstance(value, str) or not _MEDIA_TYPE.fullmatch(value)
            for value in raw_allowed
        )
    ):
        raise FixtureError(f"recipe fixture {key} output media types are invalid")
    allowed = raw_allowed
    output_limits: _OutputLimits = {
        "max_files": max_files,
        "max_file_bytes": max_file_bytes,
        "max_total_bytes": max_total_bytes,
        "allowed_media_types": allowed,
    }
    if max_file_bytes > max_total_bytes:
        raise FixtureError(f"recipe fixture {key} output media types are invalid")
    timeout_seconds = _integer(timeout, "timeout_seconds", 1, 3_600)
    if not isinstance(assertions, list) or not assertions:
        raise FixtureError(f"recipe fixture {key} assertions are required")
    parsed_assertions = tuple(
        _parse_assertion(key, assertion) for assertion in assertions
    )
    assertion_identities = [
        (str(assertion["kind"]), str(assertion.get("media_type") or ""))
        for assertion in parsed_assertions
    ]
    if len(assertion_identities) != len(set(assertion_identities)):
        raise FixtureError(f"recipe fixture {key} has duplicate assertions")
    semantic_kind = {
        "application/json": {"json-document", "synchronized-media-receipt"},
        "application/x-ndjson": {"jsonl-records", "realtime-transcript"},
        "application/zip": {"document-archive", "zip-entries"},
        "audio/wav": {"audio-metadata"},
        "image/png": {"image-metadata"},
        "model/gltf-binary": {"glb-structure"},
        "video/mp4": {"video-metadata"},
    }
    for media_type in allowed:
        required_kinds = semantic_kind.get(str(media_type))
        if required_kinds is None:
            raise FixtureError(
                f"recipe fixture {key} has no semantic validator for {media_type}"
            )
        if not any(
            assertion["kind"] in required_kinds
            and (
                assertion.get("media_type") == media_type
                or len(allowed) == 1
                and assertion.get("media_type") is None
            )
            for assertion in parsed_assertions
        ):
            raise FixtureError(
                f"recipe fixture {key} lacks semantic coverage for {media_type}"
            )
    return RecipeFixture(
        key,
        digest,
        str(interface),
        dict(parameters),
        tuple(parsed_inputs),
        output_limits,
        timeout_seconds,
        parsed_assertions,
    )
