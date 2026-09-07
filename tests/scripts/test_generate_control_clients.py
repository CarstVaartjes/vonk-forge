from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _module():
    loader = importlib.machinery.SourceFileLoader(
        "generate_control_clients", str(ROOT / "scripts/generate-control-clients")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_python_compatibility_rewrites_qualified_recursive_json_values() -> None:
    module = _module()
    document = {
        "paths": {
            "/stream": {"get": {"x-vonk-streaming-transport": True}},
            "/json": {"get": {}},
        },
        "components": {
            "schemas": {
                "vonk_forge_contracts__recipe__JsonValue": {"anyOf": []},
                "vonk_forge_contracts__recipe__RuntimeArgumentValue": {
                    "anyOf": []
                },
                "KeepExact": {"type": "string"},
            }
        },
    }

    generated = module._generated_client_schema(document, python_compatibility=True)

    assert "/stream" not in generated["paths"]
    assert generated["components"]["schemas"][
        "vonk_forge_contracts__recipe__JsonValue"
    ] == {}
    assert generated["components"]["schemas"][
        "vonk_forge_contracts__recipe__RuntimeArgumentValue"
    ] == {}
    assert generated["components"]["schemas"]["KeepExact"] == {"type": "string"}
    assert document["components"]["schemas"][
        "vonk_forge_contracts__recipe__JsonValue"
    ] == {"anyOf": []}


def test_pinned_client_generator_round_trips_arbitrary_json_values(
    tmp_path: Path,
) -> None:
    module = _module()

    document = {
        "openapi": "3.1.0",
        "info": {"title": "json round trip", "version": "1"},
        "paths": {},
        "components": {
            "schemas": {
                "vonk_forge_contracts__recipe__JsonValue": {
                    "anyOf": [
                        {"type": "string"},
                        {"type": "number"},
                        {"type": "boolean"},
                        {"type": "null"},
                        {
                            "type": "array",
                            "items": {
                                "$ref": "#/components/schemas/vonk_forge_contracts__recipe__JsonValue"
                            },
                        },
                        {
                            "type": "object",
                            "additionalProperties": {
                                "$ref": "#/components/schemas/vonk_forge_contracts__recipe__JsonValue"
                            },
                        },
                    ]
                },
                "RecipeHttpServingRequest": {
                    "type": "object",
                    "properties": {
                        "value": {
                            "$ref": "#/components/schemas/vonk_forge_contracts__recipe__JsonValue"
                        }
                    },
                    "required": ["value"],
                },
            }
        },
    }
    schema_path = tmp_path / "openapi.json"
    output_path = tmp_path / "generated_control"
    schema_path.write_text(
        json.dumps(
            module._generated_client_schema(document, python_compatibility=True)
        ),
        encoding="utf-8",
    )

    subprocess.run(
        [
            "uv",
            "run",
            "--python",
            "3.12",
            "--project",
            "control",
            "--frozen",
            "--group",
            "dev",
            "openapi-python-client",
            "generate",
            "--path",
            str(schema_path),
            "--config",
            str(ROOT / "control/openapi-python-client.yaml"),
            "--meta",
            "none",
            "--output-path",
            str(output_path),
            "--overwrite",
            "--fail-on-warning",
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONHASHSEED": "0", "SOURCE_DATE_EPOCH": "0"},
        check=True,
    )

    sys.path.insert(0, str(tmp_path))
    try:
        from generated_control.models.recipe_http_serving_request import (
            RecipeHttpServingRequest,
        )

        values = [
            "text",
            3.5,
            True,
            None,
            ["nested", 4, False],
            {"nested": {"list": [1, None, "value"]}},
        ]
        for value in values:
            payload = {"value": value}
            assert RecipeHttpServingRequest.from_dict(payload).to_dict() == payload
    finally:
        sys.path.pop(0)
