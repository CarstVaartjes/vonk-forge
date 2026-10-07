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

import copy
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cluster_profiles.control_client import source_schema_validator
from cluster_profiles.observation_transfer_reader import (
    ObservationStream,
    receive_observation,
)

_REFERENCE = "#/components/schemas/"
_MAXIMUM_DEPTH = 16


class ContractSkew(Exception):
    """The harness sent something the Controller's published contract refuses."""


@dataclass(frozen=True)
class ObservationResponseContract:
    """Decoder selected from one verified source before making its request."""

    label: str
    media_type: str
    resource: str
    payload_schema: dict[str, object]
    record_schema: dict[str, object] | None
    record_max_bytes: int | None

    def _validate(self, value: object, schema: dict[str, object]) -> None:
        validator = source_schema_validator(schema)
        if not validator.is_valid(value):
            raise ContractSkew(f"{self.label} observation violates its source schema")

    def _payload(self, value: object) -> dict[str, object]:
        self._validate(value, self.payload_schema)
        if not isinstance(value, dict) or any(
            not isinstance(key, str) for key in value
        ):
            raise ContractSkew(f"{self.label} observation is not an object")
        return {key: item for key, item in value.items()}

    def decode(
        self, stream: ObservationStream, *, status: int, media_type: str
    ) -> dict[str, object]:
        if status != 200 or media_type.partition(";")[0].strip() != self.media_type:
            raise ContractSkew(f"{self.label} observation status or media differs")
        if self.media_type == "application/json":
            # Only historical sources explicitly declaring JSON take this path.
            from scripts.development_slice_client import MAXIMUM_RESPONSE_BYTES

            body = stream.read(MAXIMUM_RESPONSE_BYTES + 1)
            if len(body) > MAXIMUM_RESPONSE_BYTES:
                raise ContractSkew(
                    f"{self.label} historical JSON observation is too large"
                )
            return self._payload(json.loads(body))
        record_schema = self.record_schema
        allocation = self.record_max_bytes
        if record_schema is None or allocation is None:
            raise ContractSkew(f"{self.label} observation record contract is missing")
        return receive_observation(
            stream,
            resource=self.resource,
            record_max_bytes=allocation,
            validate_record=lambda record: self._validate(record, record_schema),
            validate_payload=self._payload,
        )


class ControllerContract:
    def __init__(self, openapi: Mapping[str, Any], *, label: str) -> None:
        openapi = copy.deepcopy(openapi)
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

    def observation(self, path: str) -> ObservationResponseContract:
        """Select transport solely from this verified release's OpenAPI."""
        resource = {"/api/fleet": "fleet", "/api/platform": "platform"}.get(path)
        if resource is None:
            raise ContractSkew(f"{self._label} path is not a whole observation")
        for pattern, _template, operations in self._operations:
            if pattern.fullmatch(path):
                operation = operations.get("get")
                break
        else:
            raise ContractSkew(f"{self._label} has no GET {path}")
        if not isinstance(operation, dict):
            raise ContractSkew(f"{self._label} has no GET {path}")
        content = ((operation.get("responses") or {}).get("200") or {}).get("content")
        if not isinstance(content, dict) or len(content) != 1:
            raise ContractSkew(f"{self._label} observation media is ambiguous")
        media_type, media = next(iter(content.items()))
        stream_type = "application/x-vonk-observation+ndjson"
        if media_type not in {"application/json", stream_type} or not isinstance(
            media, dict
        ):
            raise ContractSkew(f"{self._label} observation transport is unsupported")
        schema = media.get("schema")
        if not isinstance(schema, dict):
            raise ContractSkew(f"{self._label} observation schema is missing")

        def rooted(selected: dict[str, object]) -> dict[str, object]:
            return {"components": {"schemas": self._schemas}, "allOf": [selected]}

        if media_type == "application/json":
            return ObservationResponseContract(
                self._label, media_type, resource, rooted(schema), None, None
            )
        payload = operation.get("x-vonk-observation-payload")
        allocation = operation.get("x-vonk-response-record-max-bytes")
        expected = "FleetSnapshot" if resource == "fleet" else "PlatformObservation"
        if (
            not isinstance(payload, dict)
            or payload.get("$ref") != _REFERENCE + expected
            or type(allocation) is not int
            or allocation < 1
        ):
            raise ContractSkew(
                f"{self._label} observation payload or allocation is missing"
            )
        return ObservationResponseContract(
            self._label,
            media_type,
            resource,
            rooted(payload),
            rooted(schema),
            allocation,
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
