"""Common for glb validation."""

from __future__ import annotations

import math
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

JSON_CHUNK = 0x4E4F534A
BIN_CHUNK = 0x004E4942
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_ACCESSOR_COUNT = 10_000_000
COMPONENTS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}
COMPONENT_TYPES = {
    5120: (1, "b"),
    5121: (1, "B"),
    5122: (2, "h"),
    5123: (2, "H"),
    5125: (4, "I"),
    5126: (4, "f"),
}
IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
# These are deliberately stricter Vonk artifact profiles, not general glTF:
# every mesh is reachable, indexed TRIANGLES are mandatory, sparse accessors
# are rejected, and PBR/skin profiles require the exact adapter outputs.
PROFILES = {"geometry", "textured", "textured-pbr", "skinned"}


def normalize_glb_json_padding(path: Path) -> None:
    """Canonicalize exporter-added JSON whitespace to the glTF 0..3-byte form."""
    data = path.read_bytes()
    if len(data) < 28 or data[:4] != b"glTF":
        return
    _magic, version, _declared_length = struct.unpack_from("<4sII", data)
    json_length, json_kind = struct.unpack_from("<II", data, 12)
    json_end = 20 + json_length
    if json_kind != JSON_CHUNK or json_end + 8 > len(data):
        return
    json_body = data[20:json_end]
    stripped = json_body.rstrip(b" ")
    if not stripped or len(json_body) - len(stripped) <= 3:
        return
    canonical = stripped + b" " * (-len(stripped) % 4)
    remainder = data[json_end:]
    rebuilt = (
        struct.pack("<4sII", b"glTF", version, 12 + 8 + len(canonical) + len(remainder))
        + struct.pack("<II", len(canonical), JSON_CHUNK)
        + canonical
        + remainder
    )
    path.write_bytes(rebuilt)


def _array(
    document: dict[str, object], name: str, *, required: bool = False
) -> list[object]:
    value = document.get(name, [])
    if (
        not isinstance(value, list)
        or (required and not value)
        or len(value) > 1_000_000
    ):
        qualifier = " non-empty" if required else ""
        raise ValueError(f"GLB {name} must be a bounded{qualifier} array")
    return value


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"GLB {name} must be an object")  # noqa: TRY004
    return value


def _index(value: object, length: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < length:
        raise ValueError(f"GLB {name} index is invalid")
    return value


def _finite_numbers(value: object) -> list[float] | None:
    """Return a JSON number array's finite floats, or None if it is not one."""

    if not isinstance(value, list):
        return None
    numbers: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        number = float(item)
        if not math.isfinite(number):
            return None
        numbers.append(number)
    return numbers


@dataclass(frozen=True)
class Accessor:
    blob: bytes
    start: int
    stride: int
    count: int
    component_type: int
    kind: str
    component_size: int
    component_count: int
    fmt: str
    interleaved: bool
    minimum: tuple[float, ...] | None
    maximum: tuple[float, ...] | None
    view_index: int
    target: int | None
    normalized: bool

    def value(self, index: int) -> tuple[int | float, ...]:
        if not 0 <= index < self.count:
            raise ValueError("GLB accessor read is out of range")
        return struct.unpack_from(
            f"<{self.component_count}{self.fmt}",
            self.blob,
            self.start + index * self.stride,
        )

    def values(self) -> Iterator[tuple[int | float, ...]]:
        for index in range(self.count):
            yield self.value(index)


def _bound(
    accessor: dict[str, object], component_count: int, field: str
) -> tuple[float, ...] | None:
    value = accessor.get(field)
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or len(value) != component_count
        or any(
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
            for item in value
        )
    ):
        raise ValueError(f"GLB accessor {field} is invalid")
    return tuple(float(item) for item in value)


def _reject_nonfinite_json(value: object) -> None:
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("GLB JSON contains a non-finite number")
        if isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, dict):
            pending.extend(item.values())


def _finite(accessor: Accessor, name: str) -> None:
    for value in accessor.values():
        if any(not math.isfinite(float(component)) for component in value):
            raise ValueError(f"GLB {name} accessor contains non-finite values")
