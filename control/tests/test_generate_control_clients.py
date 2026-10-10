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
                "vonk_forge_contracts__recipe__RuntimeArgumentValue": {"anyOf": []},
                "KeepExact": {"type": "string"},
            }
        },
    }

    generated = module._generated_client_schema(document, python_compatibility=True)

    assert "/stream" not in generated["paths"]
    assert (
        generated["components"]["schemas"]["vonk_forge_contracts__recipe__JsonValue"]
        == {}
    )
    assert (
        generated["components"]["schemas"][
            "vonk_forge_contracts__recipe__RuntimeArgumentValue"
        ]
        == {}
    )
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

    # Use the generator installed in this test environment; resolving another
    # uv project here would build a fresh venv and download packages mid-test.
    generator = Path(sys.executable).with_name("openapi-python-client")
    assert generator.exists(), "openapi-python-client is a locked dev dependency"
    subprocess.run(
        [
            str(generator),
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
        # Generated into the temporary directory above, so the module only
        # exists at runtime.
        from generated_control.models.recipe_http_serving_request import (  # pyright: ignore[reportMissingImports]
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


def test_python_build_outputs_do_not_replace_checkout_consumers(tmp_path, monkeypatch):
    """A wheel build must not remove modules a concurrent worker imports."""
    module = _module()
    checkout = tmp_path / "checkout"
    destination = tmp_path / "wheel"
    checkout.mkdir()
    sentinel = checkout / "openapi.json"
    sentinel.write_text("browser contract")
    monkeypatch.setattr(module, "ROOT", checkout)
    monkeypatch.setattr(module, "PYTHON_OUTPUT", checkout / "generated_control")
    monkeypatch.setattr(module, "CLI_OPENAPI_OUTPUT", checkout / "cli-openapi.json")
    write_openapi = module._write_openapi
    monkeypatch.setattr(
        module,
        "_write_openapi",
        lambda document, path=sentinel: write_openapi(document, path=path),
    )
    monkeypatch.setattr(module, "_schema", lambda **_kwargs: {"paths": {}})
    monkeypatch.setattr(module, "_use_httpx2", lambda _path: None)
    monkeypatch.setattr(module, "_normalize_generated_text", lambda _path: None)
    generated = []
    monkeypatch.setattr(module, "_run", generated.append)
    monkeypatch.setattr(
        "sys.argv",
        [
            "generate-control-clients",
            "--python-only",
            "--output-root",
            str(destination),
        ],
    )
    module.main()
    assert str(destination / "generated_control") in generated[0]
    assert json.loads((destination / "cli-openapi.json").read_text()) == {"paths": {}}
    assert sentinel.read_text() == "browser contract"
    assert list(checkout.iterdir()) == [sentinel]
