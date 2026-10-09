"""Accessors for glb validation."""

from __future__ import annotations

import json
import struct

from .common import (
    BIN_CHUNK,
    COMPONENT_TYPES,
    COMPONENTS,
    JSON_CHUNK,
    MAX_ACCESSOR_COUNT,
    MAX_JSON_BYTES,
    Accessor,
    _array,
    _bound,
    _index,
    _object,
    _reject_nonfinite_json,
)


def _parse(data: bytes) -> tuple[dict[str, object], bytes]:
    if len(data) < 28:
        raise ValueError("GLB is shorter than its header and required chunks")
    magic, version, declared_length = struct.unpack_from("<4sII", data)
    if magic != b"glTF" or version != 2:
        raise ValueError("artifact is not a GLB 2.0 file")
    if declared_length != len(data):
        raise ValueError("GLB header length does not match the artifact size")
    chunks: list[tuple[int, bytes]] = []
    offset = 12
    while offset < len(data):
        if offset + 8 > len(data):
            raise ValueError("GLB has a truncated chunk header")
        length, kind = struct.unpack_from("<II", data, offset)
        offset += 8
        end = offset + length
        if length % 4 or end > len(data):
            raise ValueError("GLB chunk bounds or alignment are invalid")
        chunks.append((kind, data[offset:end]))
        offset = end
    if not chunks or chunks[0][0] != JSON_CHUNK:
        raise ValueError("GLB first chunk is not JSON")
    if len(chunks) != 2 or chunks[1][0] != BIN_CHUNK:
        raise ValueError(
            "GLB must contain exactly one JSON chunk followed by one BIN chunk"
        )
    if len(chunks[0][1]) > MAX_JSON_BYTES:
        raise ValueError("GLB JSON chunk exceeds the validation limit")
    json_chunk = chunks[0][1]
    stripped_json = json_chunk.rstrip(b" ")
    if (
        not stripped_json
        or not stripped_json.endswith(b"}")
        or len(json_chunk) - len(stripped_json) > 3
    ):
        raise ValueError("GLB JSON chunk padding must contain spaces only")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"GLB JSON contains duplicate key: {key}")
            value[key] = item
        return value

    def invalid_constant(value: str) -> object:
        raise ValueError(f"GLB JSON contains invalid numeric constant: {value}")

    try:
        parsed = json.loads(
            json_chunk.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=invalid_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("GLB JSON chunk is invalid") from exc
    document = _object(parsed, "JSON document")
    _reject_nonfinite_json(document)
    extensions_used = document.get("extensionsUsed", [])
    extensions_required = document.get("extensionsRequired", [])
    if (
        not isinstance(extensions_used, list)
        or not isinstance(extensions_required, list)
        or any(
            not isinstance(item, str) for item in extensions_used + extensions_required
        )
        or len(extensions_used) != len(set(extensions_used))
        or len(extensions_required) != len(set(extensions_required))
        or not set(extensions_required).issubset(extensions_used)
        or set(extensions_required) - {"EXT_texture_webp"}
    ):
        raise ValueError("GLB required extensions are invalid or unsupported")
    if (
        "EXT_texture_webp" in extensions_used
        and "EXT_texture_webp" not in extensions_required
    ):
        raise ValueError(
            "GLB WebP textures must declare EXT_texture_webp as used and required"
        )
    asset = _object(document.get("asset"), "asset")
    if asset.get("version") != "2.0":
        raise ValueError("GLB JSON does not declare glTF 2.0")
    buffer = _object(_array(document, "buffers", required=True)[0], "buffer")
    byte_length = buffer.get("byteLength")
    if isinstance(byte_length, int) and any(chunks[1][1][byte_length:]):
        raise ValueError("GLB BIN chunk padding bytes must be zero")
    return document, chunks[1][1]


def _accessors(
    document: dict[str, object], blob: bytes
) -> tuple[list[Accessor], list[tuple[int, int]]]:
    buffers = _array(document, "buffers", required=True)
    if len(buffers) != 1:
        raise ValueError("GLB must contain exactly one embedded buffer")
    buffer = _object(buffers[0], "buffer")
    if "uri" in buffer:
        raise ValueError("GLB buffer must be embedded")
    byte_length = buffer.get("byteLength")
    if (
        isinstance(byte_length, bool)
        or not isinstance(byte_length, int)
        or byte_length < 1
        or byte_length > len(blob)
        or len(blob) - byte_length > 3
    ):
        raise ValueError("GLB embedded buffer length is invalid")

    raw_views = _array(document, "bufferViews", required=True)
    views: list[tuple[int, int]] = []
    strides: list[int | None] = []
    targets: list[int | None] = []
    for raw in raw_views:
        view = _object(raw, "bufferView")
        _index(view.get("buffer", 0), 1, "bufferView buffer")
        start, length = view.get("byteOffset", 0), view.get("byteLength")
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(length, bool)
            or not isinstance(length, int)
            or start < 0
            or length < 1
            or start + length > byte_length
        ):
            raise ValueError("GLB bufferView exceeds its buffer")
        stride = view.get("byteStride")
        if stride is not None and (
            isinstance(stride, bool)
            or not isinstance(stride, int)
            or not 4 <= stride <= 252
            or stride % 4
        ):
            raise ValueError("GLB bufferView byteStride is invalid")
        target = view.get("target")
        if target is not None and (
            isinstance(target, bool)
            or not isinstance(target, int)
            or target not in {34962, 34963}
        ):
            raise ValueError("GLB bufferView target is invalid")
        views.append((start, length))
        strides.append(stride)
        targets.append(target)

    result: list[Accessor] = []
    for raw in _array(document, "accessors", required=True):
        accessor = _object(raw, "accessor")
        if "sparse" in accessor:
            raise ValueError(
                "GLB sparse accessors are not supported by this artifact contract"
            )
        if "normalized" in accessor and not isinstance(accessor["normalized"], bool):
            raise ValueError("GLB accessor normalized flag must be boolean")
        view_index = _index(
            accessor.get("bufferView"), len(views), "accessor bufferView"
        )
        component_type = accessor.get("componentType")
        kind = accessor.get("type")
        count = accessor.get("count")
        if (
            isinstance(component_type, bool)
            or not isinstance(component_type, int)
            or not isinstance(kind, str)
            or component_type not in COMPONENT_TYPES
            or kind not in COMPONENTS
        ):
            raise ValueError("GLB accessor componentType or type is invalid")
        if accessor.get("normalized") is True and component_type not in {
            5120,
            5121,
            5122,
            5123,
        }:
            raise ValueError(
                "GLB normalized accessor must use an 8-bit or 16-bit integer component"
            )
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or not 1 <= count <= MAX_ACCESSOR_COUNT
        ):
            raise ValueError("GLB accessor count is invalid")
        component_size, fmt = COMPONENT_TYPES[component_type]
        component_count = COMPONENTS[kind]
        element_size = component_size * component_count
        offset = accessor.get("byteOffset", 0)
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 0
            or offset % component_size
        ):
            raise ValueError("GLB accessor byteOffset is invalid")
        view_start, view_length = views[view_index]
        stride = strides[view_index] or element_size
        if (view_start + offset) % component_size:
            raise ValueError("GLB accessor is not aligned for its component type")
        if (
            stride < element_size
            or offset + (count - 1) * stride + element_size > view_length
        ):
            raise ValueError("GLB accessor exceeds its bufferView")

        result.append(
            Accessor(
                blob,
                view_start + offset,
                stride,
                count,
                component_type,
                kind,
                component_size,
                component_count,
                fmt,
                strides[view_index] is not None,
                _bound(accessor, component_count, "min"),
                _bound(accessor, component_count, "max"),
                view_index,
                targets[view_index],
                accessor.get("normalized", False) is True,
            )
        )
    declared_strided_views = {
        index for index, stride in enumerate(strides) if stride is not None
    }
    referenced_strided_views = {
        accessor.view_index for accessor in result if accessor.interleaved
    }
    if declared_strided_views != referenced_strided_views:
        raise ValueError("GLB byteStride bufferView must be used by an accessor")
    return result, views
