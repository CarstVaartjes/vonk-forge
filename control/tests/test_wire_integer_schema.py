from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
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

    # Consume the emitted schema; a rounded
    # nullable-field maximum incorrectly authorizes u64MAX+1 in real clients.
    install_canonical_openapi(app)
    emitted = json.dumps(app.openapi())
    schema = json.loads(emitted)["components"]["schemas"]["FailureLogTail"]
    validator = Draft202012Validator(schema)

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
        validator.validate(payload)
        assert (
            FailureLogTail.model_validate_json(accepted.model_dump_json()) == accepted
        )
    payload[field] = MAX_DROPPED_COUNT + 1
    assert not validator.is_valid(payload)
    with pytest.raises(ValidationError):
        FailureLogTail.model_validate_json(json.dumps(payload), strict=True)
    payload[field] = None
    validator.validate(payload)
    with TestClient(app) as client:
        response = client.get("/diagnostics-tail")
    restored = FailureLogTail.model_validate_json(response.content)
    assert restored.truncated is False
    assert getattr(restored, field) is None


def test_openapi_numeric_constraints_reject_only_out_of_bound_values() -> None:
    from typing import Annotated, Literal

    from fastapi import Query
    from pydantic import BaseModel, Field
    from vonk_control.openapi_numbers import canonical_openapi

    class NumericRules(BaseModel):
        bounded: int = Field(ge=0, le=18446744073709551615)
        exclusive: int = Field(gt=9223372036854775806, lt=9223372036854775808)
        multiple: int = Field(multiple_of=9223372036854775811)
        constant: Literal[18446744073709551615]
        choice: Literal[18446744073709551614, 18446744073709551615]

    app = FastAPI()

    def read_rules(value: int = 9223372036854775807):
        return NumericRules(
            bounded=18446744073709551615,
            exclusive=9223372036854775807,
            multiple=9223372036854775811,
            constant=18446744073709551615,
            choice=18446744073709551614,
        )

    read_rules.__annotations__["value"] = Annotated[
        int, Query(ge=9223372036854775806, le=9223372036854775807)
    ]
    app.get("/rules", response_model=NumericRules)(read_rules)
    emitted = canonical_openapi(title=app.title, version=app.version, routes=app.routes)
    schema = emitted["components"]["schemas"]["NumericRules"]
    parameter = Draft202012Validator(
        emitted["paths"]["/rules"]["get"]["parameters"][0]["schema"]
    )
    for value in (9223372036854775806, 9223372036854775807):
        parameter.validate(value)
    for value in (9223372036854775805, 9223372036854775808):
        assert not parameter.is_valid(value)
    validator = Draft202012Validator(schema)
    import httpx2

    with TestClient(app) as client:
        for invalid in (9223372036854775805, 9223372036854775808):
            with pytest.raises(httpx2.HTTPStatusError):
                client.get("/rules", params={"value": invalid}).raise_for_status()
            assert (
                client.get("/rules", params={"value": 9223372036854775807}).status_code
                == 200
            )
        response = client.get("/rules")
    valid = response.json()
    validator.validate(valid)
    assert NumericRules.model_validate_json(response.content) == read_rules()
    for field, value in (
        ("bounded", 18446744073709551616),
        ("exclusive", 9223372036854775808),
        ("exclusive", 9223372036854775806),
        ("multiple", 9223372036854775812),
        ("constant", 18446744073709551614),
        ("choice", 18446744073709551613),
    ):
        assert not validator.is_valid(valid | {field: value})
        validator.validate(valid)
