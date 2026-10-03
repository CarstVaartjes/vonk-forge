"""A published Controller's request contract, applied to what the lane sends.

The upgrade-carry lane drives two Controllers with one harness: the previous
promoted release first, the candidate after the redeploy. The harness is newer
than both, so a request it builds from today's models can carry a field the
Controller in front of it has never heard of. That Controller refuses the
request (HTTP 422) and the lane dies in its first phase, which blocks every
release until the harness is fixed (#1086).

``ControllerContract`` reads the OpenAPI document published with that
Controller's source and rejects such a request before it is sent, naming the
field, the operation and the release, so the failure says "version skew in the
harness" instead of a bare 422 from hardware.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

_REFERENCE = "#/components/schemas/"
_MAXIMUM_DEPTH = 16


class ContractSkew(Exception):
    """The harness sent something the Controller's published contract refuses."""


class ControllerContract:
    def __init__(self, openapi: Mapping[str, Any], *, label: str) -> None:
        paths = openapi.get("paths")
        schemas = (openapi.get("components") or {}).get("schemas")
        if not isinstance(paths, dict) or not isinstance(schemas, dict):
            raise TypeError(f"{label} publishes no OpenAPI paths and schemas")
        self._label = label
        self._schemas: dict[str, Any] = schemas
        # Longest literal text first, so /recipe/library beats /recipe/{selector}
        # and /recipe/{selector}/download beats both.
        self._operations = sorted(
            (
                (self._pattern(template), template, operations)
                for template, operations in paths.items()
                if isinstance(operations, dict)
            ),
            key=lambda item: -len(re.sub(r"\{[^}]*\}", "", item[1])),
        )

    @staticmethod
    def _pattern(template: str) -> re.Pattern[str]:
        parts = re.split(r"(\{[^}]*\})", template)
        return re.compile(
            "".join(
                "[^?#]+" if part.startswith("{") else re.escape(part) for part in parts
            )
        )

    def check(self, method: str, path: str, body: bytes | None) -> None:
        """Raise ``ContractSkew`` when this request is not in the contract."""

        if body is None:
            return
        route = path.partition("?")[0]
        for pattern, template, operations in self._operations:
            if pattern.fullmatch(route):
                break
        else:
            raise ContractSkew(f"{self._label} has no operation {method} {route}")
        operation = operations.get(method.lower())
        if not isinstance(operation, dict):
            raise ContractSkew(f"{self._label} has no operation {method} {template}")
        schema = (
            ((operation.get("requestBody") or {}).get("content") or {})
            .get("application/json", {})
            .get("schema")
        )
        if schema is None:
            raise ContractSkew(
                f"{self._label} accepts no JSON body for {method} {template}"
            )
        try:
            value = json.loads(body)
        except ValueError as error:
            raise ContractSkew(
                f"{method} {template} sent a body that is not JSON"
            ) from error
        problems = self._problems(value, schema, "body", 0)
        if problems:
            raise ContractSkew(
                f"{method} {template} sends what {self._label} does not accept: "
                + "; ".join(problems[:3])
            )

    def _resolve(self, schema: Any) -> Any:
        while isinstance(schema, dict) and isinstance(schema.get("$ref"), str):
            reference = schema["$ref"]
            name = reference.removeprefix(_REFERENCE)
            if not reference.startswith(_REFERENCE) or name not in self._schemas:
                return {}
            schema = self._schemas[name]
        return schema if isinstance(schema, dict) else {}

    def _problems(self, value: Any, schema: Any, where: str, depth: int) -> list[str]:
        if depth > _MAXIMUM_DEPTH:
            return []
        schema = self._resolve(schema)
        alternatives = schema.get("anyOf") or schema.get("oneOf")
        if isinstance(alternatives, list) and alternatives:
            attempts = [
                self._problems(value, alternative, where, depth + 1)
                for alternative in alternatives
            ]
            return (
                []
                if any(not attempt for attempt in attempts)
                else min(attempts, key=len)
            )
        if value is None:
            nullable = schema.get("type") == "null" or schema.get("nullable") is True
            return [] if nullable or not schema else [f"{where} is null"]
        if isinstance(value, dict):
            properties = schema.get("properties")
            if not isinstance(properties, dict):
                return []
            extra = schema.get("additionalProperties")
            problems: list[str] = []
            for key, item in value.items():
                if key in properties:
                    problems += self._problems(
                        item, properties[key], f"{where}.{key}", depth + 1
                    )
                elif isinstance(extra, dict):
                    problems += self._problems(item, extra, f"{where}.{key}", depth + 1)
                elif extra is not True:
                    problems.append(f"{where}.{key} is not a known field")
            return problems
        if isinstance(value, list):
            items = schema.get("items")
            return [
                problem
                for index, item in enumerate(value)
                for problem in self._problems(
                    item, items, f"{where}[{index}]", depth + 1
                )
            ]
        return []
