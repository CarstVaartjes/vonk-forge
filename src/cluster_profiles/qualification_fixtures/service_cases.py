"""Service case and recipe parsing."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from .contracts import _DIGEST, _KEY, _NAME, FixtureError, ServiceCase, ServiceRecipe
from .values import _integer, _object


def _parse_service_case(key: object, raw: object) -> ServiceCase:
    if not isinstance(key, str) or not _NAME.fullmatch(key):
        raise FixtureError("service case ID is invalid")
    item = _object(raw, f"service case {key}")
    method = item.get("method")
    path = item.get("path")
    body = item.get("body")
    assertions = item.get("assertions")
    if method not in {"GET", "POST"}:
        raise FixtureError(f"service case {key} method is invalid")
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or "//" in path
        or "?" in path
        or "#" in path
        or len(path) > 128
    ):
        raise FixtureError(f"service case {key} path is invalid")
    if method == "GET" and body is not None:
        raise FixtureError(f"service case {key} GET body must be null")
    if method == "POST" and not isinstance(body, dict):
        raise FixtureError(f"service case {key} POST body is invalid")
    if len(json.dumps(body, separators=(",", ":")).encode()) > 64 * 1024:
        raise FixtureError(f"service case {key} body is too large")
    if not isinstance(assertions, list) or not assertions or len(assertions) > 32:
        raise FixtureError(f"service case {key} assertions are invalid")
    parsed_assertions: list[dict[str, object]] = []
    allowed_kinds = {
        "array.path-count-equals",
        "path.count",
        "path.empty",
        "path.equals",
        "path.json-equals",
        "path.lte",
        "path.nonempty",
        "path.regex",
        "raw.not-contains",
    }
    for assertion_raw in assertions:
        assertion = dict(_object(assertion_raw, f"service case {key} assertion"))
        kind = assertion.get("kind")
        if kind not in allowed_kinds:
            raise FixtureError(f"service case {key} assertion kind is invalid")
        allowed_fields = {
            "array.path-count-equals": {
                "kind",
                "path",
                "item_path",
                "value",
                "count",
            },
            "path.count": {"kind", "path", "value"},
            "path.empty": {"kind", "path"},
            "path.equals": {"kind", "path", "value"},
            "path.json-equals": {"kind", "path", "value"},
            "path.lte": {"kind", "path", "value"},
            "path.nonempty": {"kind", "path"},
            "path.regex": {"kind", "path", "value"},
            "raw.not-contains": {"kind", "values"},
        }
        if set(assertion) - allowed_fields[str(kind)]:
            raise FixtureError(f"service case {key} assertion fields are invalid")
        path_value = assertion.get("path")
        if kind != "raw.not-contains" and (
            not isinstance(path_value, str) or not path_value or len(path_value) > 256
        ):
            raise FixtureError(f"service case {key} assertion path is invalid")
        if kind == "path.regex":
            pattern = assertion.get("value")
            if not isinstance(pattern, str) or len(pattern) > 512:
                raise FixtureError(f"service case {key} regex is invalid")
            try:
                re.compile(pattern)
            except re.error as error:
                raise FixtureError(f"service case {key} regex is invalid") from error
        if kind == "raw.not-contains":
            values = assertion.get("values")
            if (
                not isinstance(values, list)
                or not values
                or any(not isinstance(value, str) or not value for value in values)
            ):
                raise FixtureError(f"service case {key} raw assertion is invalid")
        parsed_assertions.append(assertion)
    return ServiceCase(
        key,
        str(method),
        path,
        body,
        _integer(item.get("timeout_seconds"), "timeout_seconds", 1, 900),
        _integer(item.get("max_response_bytes"), "max_response_bytes", 1, 1024 * 1024),
        tuple(parsed_assertions),
    )


def _parse_service_recipe(
    key: object, raw: object, cases: Mapping[str, ServiceCase]
) -> ServiceRecipe:
    if not isinstance(key, str) or not _KEY.fullmatch(key):
        raise FixtureError("service recipe key is invalid")
    item = _object(raw, f"service recipe {key}")
    digest = item.get("content_sha256")
    alias = item.get("alias")
    smoke_cases = item.get("smoke_cases")
    higher = _object(item.get("higher_tiers", {}), f"service recipe {key} tiers")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise FixtureError(f"service recipe {key} digest is invalid")
    if not isinstance(alias, str) or not _NAME.fullmatch(alias):
        raise FixtureError(f"service recipe {key} alias is invalid")
    if (
        not isinstance(smoke_cases, list)
        or not smoke_cases
        or len(smoke_cases) != len(set(smoke_cases))
        or any(not isinstance(case, str) or case not in cases for case in smoke_cases)
    ):
        raise FixtureError(f"service recipe {key} smoke cases are invalid")
    higher_tiers: dict[str, tuple[str, ...]] = {}
    for tier, descriptions in higher.items():
        if tier not in {"stress", "recovery"} or not isinstance(descriptions, list):
            raise FixtureError(f"service recipe {key} higher tier is invalid")
        if any(
            not isinstance(description, str) or not 1 <= len(description) <= 512
            for description in descriptions
        ):
            raise FixtureError(f"service recipe {key} tier description is invalid")
        higher_tiers[str(tier)] = tuple(descriptions)
    return ServiceRecipe(
        key,
        digest,
        alias,
        tuple(cases[str(case)] for case in smoke_cases),
        higher_tiers,
    )
