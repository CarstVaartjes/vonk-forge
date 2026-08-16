"""Canonical serializers for allowlisted typed repository documents."""

from __future__ import annotations

import json
from collections.abc import Mapping

import tomli_w

_PRIORITY = {
    "schema_version": 0,
    "id": 1,
    "name": 2,
    "display_name": 3,
    "description": 4,
    "lifecycle": 5,
}
def _ordered(value: object) -> object:
    if isinstance(value, Mapping):
        keys = sorted(value, key=lambda key: (_PRIORITY.get(str(key), 100), str(key)))
        return {str(key): _ordered(value[key]) for key in keys}
    if isinstance(value, list):
        return [_ordered(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(f"unsupported typed document value: {type(value).__name__}")


def serialize_document(path: str, document: Mapping[str, object]) -> bytes:
    ordered = _ordered(document)
    assert isinstance(ordered, dict)
    if path.endswith(".json"):
        return (json.dumps(ordered, ensure_ascii=False, sort_keys=False, indent=2) + "\n").encode()
    if path.endswith(".toml"):
        return tomli_w.dumps(ordered, multiline_strings=False).replace("\r\n", "\n").encode()
    raise ValueError("typed proposals support only JSON and TOML documents")
