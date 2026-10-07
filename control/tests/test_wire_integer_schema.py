from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from vonk_agent_protocol.failure_evidence import MAX_DROPPED_COUNT, FailureLogTail
from vonk_control.openapi_numbers import install_canonical_openapi


@pytest.mark.parametrize("field", ["dropped_bytes", "dropped_lines"])
def test_nullable_physical_counter_keeps_exact_integer_openapi_bound(
    field: str,
) -> None:
    app = FastAPI()

    @app.get("/diagnostics-tail", response_model=FailureLogTail)
    def diagnostics_tail() -> FailureLogTail:
        return FailureLogTail(
            text="tail", truncated=False, dropped_bytes=None, dropped_lines=None
        )

    # Inspect the emitted document, including its numeric lexeme; a rounded
    # nullable-field maximum incorrectly authorizes u64MAX+1 in real clients.
    install_canonical_openapi(app)
    emitted = json.dumps(app.openapi())
    schema = json.loads(emitted)["components"]["schemas"]["FailureLogTail"]
    integer = next(
        item
        for item in schema["properties"][field]["anyOf"]
        if item["type"] == "integer"
    )
    assert type(integer["maximum"]) is int
    assert integer["maximum"] == MAX_DROPPED_COUNT
    assert f'"maximum": {MAX_DROPPED_COUNT}' in emitted

    payload: dict[str, str | bool | int | None] = {
        "text": "tail",
        "truncated": False,
        "dropped_bytes": None,
        "dropped_lines": None,
    }
    for value in (None, MAX_DROPPED_COUNT - 1, MAX_DROPPED_COUNT):
        payload[field] = value
        accepted = FailureLogTail.model_validate_json(json.dumps(payload), strict=True)
        assert accepted.model_dump(mode="json")[field] == value
    payload[field] = MAX_DROPPED_COUNT + 1
    with pytest.raises(ValidationError):
        FailureLogTail.model_validate_json(json.dumps(payload), strict=True)


def test_pinned_openapi_assembler_keeps_framework_output_and_inline_numbers() -> None:
    import hashlib
    import inspect
    from typing import Annotated, Any, Literal

    from fastapi import Query
    from fastapi.encoders import jsonable_encoder
    from fastapi.openapi.models import OpenAPI
    from fastapi.openapi.utils import get_openapi
    from pydantic import BaseModel, Field
    from pydantic.json_schema import models_json_schema
    from vonk_control.fleet_projection import FleetSnapshot
    from vonk_control.openapi_numbers import (
        UPSTREAM_ASSEMBLER_SHA256,
        canonical_openapi,
    )
    from vonk_control.platform_observation import PlatformObservation

    assert (
        hashlib.sha256(inspect.getsource(get_openapi).encode()).hexdigest()
        == UPSTREAM_ASSEMBLER_SHA256
    )

    class NumericRules(BaseModel):
        bounded: int = Field(ge=0, le=18446744073709551615)
        exclusive: int = Field(gt=9223372036854775806, lt=9223372036854775808)
        multiple: int = Field(multiple_of=9223372036854775811)
        constant: Literal[18446744073709551615]
        choice: Literal[18446744073709551614, 18446744073709551615]

    app = FastAPI()

    def read_rules(
        value: Annotated[int, Query(ge=9223372036854775806, le=9223372036854775807)],
    ) -> NumericRules:
        raise AssertionError("schema-only route must not execute")

    # Resolve the locally authored fixture annotations explicitly, without
    # creating a second contract or invoking the schema-only route.
    read_rules.__annotations__["value"] = Annotated[
        int, Query(ge=9223372036854775806, le=9223372036854775807)
    ]
    app.get("/rules", response_model=NumericRules)(read_rules)
    standard = get_openapi(title=app.title, version=app.version, routes=app.routes)
    canonical = canonical_openapi(
        title=app.title, version=app.version, routes=app.routes
    )
    # Stream records transport these canonical payloads by envelope. The
    # emitter intentionally retains their complete graph for generated clients;
    # compare framework output plus those independently derived owner schemas.
    _, observation_graph = models_json_schema(
        [(FleetSnapshot, "serialization"), (PlatformObservation, "serialization")],
        ref_template="#/components/schemas/{model}",
    )
    for name, definition in observation_graph["$defs"].items():
        standard["components"]["schemas"].setdefault(name, definition)
    # The added raw owner schemas need the framework's same final document
    # rendering (including omitted None defaults), not a second schema policy.
    standard = jsonable_encoder(OpenAPI(**standard), by_alias=True, exclude_none=True)
    numeric_keywords = {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "const",
        "enum",
    }

    def nonnumeric(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: nonnumeric(item)
                for key, item in value.items()
                if key not in numeric_keywords
            }
        if isinstance(value, list):
            return [nonnumeric(item) for item in value]
        return value

    assert nonnumeric(canonical) == nonnumeric(standard)
    emitted = canonical["components"]["schemas"]["NumericRules"]["properties"]
    assert emitted == NumericRules.model_json_schema()["properties"]
    parameter = canonical["paths"]["/rules"]["get"]["parameters"][0]["schema"]
    assert type(parameter["maximum"]) is int
    assert parameter["maximum"] == 9223372036854775807
    assert parameter["minimum"] == 9223372036854775806
