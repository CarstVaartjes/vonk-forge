from __future__ import annotations

from importlib.resources import files
from pathlib import Path

_RUNTIME_SCHEMAS = frozenset(
    {
        "catalog-entity-v1.schema.json",
        "harness-evidence-v1.schema.json",
        "recipe-v1.schema.json",
        "test-report-v1.schema.json",
    }
)


def read_runtime_schema(name: str) -> str:
    """Read a schema from the installed wheel or a direct source checkout."""
    if name not in _RUNTIME_SCHEMAS:
        raise ValueError(f"unknown runtime schema: {name}")
    packaged = files("vonk_control").joinpath("schemas", name)
    if packaged.is_file():
        return packaged.read_text(encoding="utf-8")
    source_checkout = Path(__file__).resolve().parents[3] / "schemas" / "global" / name
    return source_checkout.read_text(encoding="utf-8")
