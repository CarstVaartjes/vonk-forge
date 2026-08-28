"""Bounded semantic validation for self-contained triangle-mesh GLBs."""

from __future__ import annotations

import json
import math
import struct
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

JSON_CHUNK = 0x4E4F534A
BIN_CHUNK = 0x004E4942
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_ACCESSOR_COUNT = 10_000_000
COMPONENTS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}
COMPONENT_TYPES = {
    5120: (1, "b"), 5121: (1, "B"), 5122: (2, "h"),
    5123: (2, "H"), 5125: (4, "I"), 5126: (4, "f"),
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


def _array(document: dict[str, object], name: str, *, required: bool = False) -> list[object]:
    value = document.get(name, [])
    if not isinstance(value, list) or (required and not value) or len(value) > 1_000_000:
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
            f"<{self.component_count}{self.fmt}", self.blob, self.start + index * self.stride
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
        raise ValueError("GLB must contain exactly one JSON chunk followed by one BIN chunk")
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
        or any(not isinstance(item, str) for item in extensions_used + extensions_required)
        or len(extensions_used) != len(set(extensions_used))
        or len(extensions_required) != len(set(extensions_required))
        or not set(extensions_required).issubset(extensions_used)
        or set(extensions_required) - {"EXT_texture_webp"}
    ):
        raise ValueError("GLB required extensions are invalid or unsupported")
    if "EXT_texture_webp" in extensions_used and "EXT_texture_webp" not in extensions_required:
        raise ValueError("GLB WebP textures must declare EXT_texture_webp as used and required")
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
        isinstance(byte_length, bool) or not isinstance(byte_length, int)
        or byte_length < 1 or byte_length > len(blob) or len(blob) - byte_length > 3
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
            isinstance(start, bool) or not isinstance(start, int)
            or isinstance(length, bool) or not isinstance(length, int)
            or start < 0 or length < 1 or start + length > byte_length
        ):
            raise ValueError("GLB bufferView exceeds its buffer")
        stride = view.get("byteStride")
        if stride is not None and (
            isinstance(stride, bool) or not isinstance(stride, int)
            or not 4 <= stride <= 252 or stride % 4
        ):
            raise ValueError("GLB bufferView byteStride is invalid")
        target = view.get("target")
        if target is not None and target not in {34962, 34963}:
            raise ValueError("GLB bufferView target is invalid")
        views.append((start, length))
        strides.append(stride)
        targets.append(target)

    result: list[Accessor] = []
    for raw in _array(document, "accessors", required=True):
        accessor = _object(raw, "accessor")
        if "sparse" in accessor:
            raise ValueError("GLB sparse accessors are not supported by this artifact contract")
        if "normalized" in accessor and not isinstance(accessor["normalized"], bool):
            raise ValueError("GLB accessor normalized flag must be boolean")
        view_index = _index(accessor.get("bufferView"), len(views), "accessor bufferView")
        component_type = accessor.get("componentType")
        kind = accessor.get("type")
        count = accessor.get("count")
        if component_type not in COMPONENT_TYPES or kind not in COMPONENTS:
            raise ValueError("GLB accessor componentType or type is invalid")
        if accessor.get("normalized") is True and component_type not in {5120, 5121, 5122, 5123}:
            raise ValueError("GLB normalized accessor must use an 8-bit or 16-bit integer component")
        if (
            isinstance(count, bool) or not isinstance(count, int)
            or not 1 <= count <= MAX_ACCESSOR_COUNT
        ):
            raise ValueError("GLB accessor count is invalid")
        component_size, fmt = COMPONENT_TYPES[int(component_type)]
        component_count = COMPONENTS[str(kind)]
        element_size = component_size * component_count
        offset = accessor.get("byteOffset", 0)
        if (
            isinstance(offset, bool) or not isinstance(offset, int)
            or offset < 0 or offset % component_size
        ):
            raise ValueError("GLB accessor byteOffset is invalid")
        view_start, view_length = views[view_index]
        stride = strides[view_index] or element_size
        if (view_start + offset) % component_size:
            raise ValueError("GLB accessor is not aligned for its component type")
        if stride < element_size or offset + (count - 1) * stride + element_size > view_length:
            raise ValueError("GLB accessor exceeds its bufferView")

        result.append(Accessor(
            blob, view_start + offset, stride, count, int(component_type), str(kind),
            component_size, component_count, fmt, strides[view_index] is not None,
            _bound(accessor, component_count, "min"),
            _bound(accessor, component_count, "max"),
            view_index,
            targets[view_index],
            accessor.get("normalized", False) is True,
        ))
    declared_strided_views = {index for index, stride in enumerate(strides) if stride is not None}
    referenced_strided_views = {
        accessor.view_index for accessor in result if accessor.interleaved
    }
    if declared_strided_views != referenced_strided_views:
        raise ValueError("GLB byteStride bufferView must be used by an accessor")
    return result, views


def _finite(accessor: Accessor, name: str) -> None:
    for value in accessor.values():
        if any(not math.isfinite(float(component)) for component in value):
            raise ValueError(f"GLB {name} accessor contains non-finite values")


def _valid_png(value: bytes) -> bool:
    if not value.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    offset = 8
    chunks: list[tuple[bytes, bytes]] = []
    while offset < len(value):
        if offset + 12 > len(value):
            return False
        length = struct.unpack_from(">I", value, offset)[0]
        kind = value[offset + 4 : offset + 8]
        if len(kind) != 4 or any(not (65 <= byte <= 90 or 97 <= byte <= 122) for byte in kind):
            return False
        if kind[0] & 0x20 == 0 and kind not in {b"IHDR", b"PLTE", b"IDAT", b"IEND"}:
            return False
        end = offset + 12 + length
        if end > len(value):
            return False
        payload = value[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack_from(">I", value, offset + 8 + length)[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != expected_crc:
            return False
        chunks.append((kind, payload))
        offset = end
        if kind == b"IEND":
            break
    if offset != len(value) or not chunks or chunks[0][0] != b"IHDR":
        return False
    ihdr = chunks[0][1]
    kinds = [kind for kind, _payload in chunks]
    idat_indices = [index for index, kind in enumerate(kinds) if kind == b"IDAT"]
    if (
        len(ihdr) != 13 or chunks[-1] != (b"IEND", b"")
        or kinds.count(b"IHDR") != 1 or kinds.count(b"IEND") != 1
        or kinds.count(b"PLTE") > 1 or not idat_indices
        or idat_indices != list(range(idat_indices[0], idat_indices[-1] + 1))
        or (b"PLTE" in kinds and kinds.index(b"PLTE") > idat_indices[0])
    ):
        return False
    width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB", ihdr
    )
    allowed_depths = {
        0: {1, 2, 4, 8, 16},
        2: {8, 16},
        3: {1, 2, 4, 8},
        4: {8, 16},
        6: {8, 16},
    }
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    if (
        not width or not height or width > 16_384 or height > 16_384
        or bit_depth not in allowed_depths.get(color_type, set())
        or compression != 0 or filtering != 0 or interlace != 0
    ):
        return False
    if color_type == 3 and not any(kind == b"PLTE" and payload for kind, payload in chunks):
        return False
    idat = b"".join(payload for kind, payload in chunks if kind == b"IDAT")
    if not idat:
        return False
    row_bytes = (width * channels[color_type] * bit_depth + 7) // 8
    decoded_bytes = (row_bytes + 1) * height
    if decoded_bytes > 512 * 1024 * 1024:
        return False
    decoder = zlib.decompressobj()
    try:
        decoded = decoder.decompress(idat, decoded_bytes + 1)
    except zlib.error:
        return False
    if (
        len(decoded) != decoded_bytes or not decoder.eof
        or decoder.unused_data or decoder.unconsumed_tail
    ):
        return False
    return all(decoded[row * (row_bytes + 1)] <= 4 for row in range(height))


def _valid_jpeg(value: bytes) -> bool:
    if len(value) < 12 or not value.startswith(b"\xff\xd8") or not value.endswith(b"\xff\xd9"):
        return False
    start_of_frame = {
        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
    }
    offset = 2
    saw_frame = False
    saw_quantization = False
    saw_coding_table = False
    quantization_tables: set[int] = set()
    dc_tables: set[int] = set()
    ac_tables: set[int] = set()
    frame_components: dict[int, int] = {}
    while offset < len(value) - 2:
        if value[offset] != 0xFF:
            return False
        while offset < len(value) and value[offset] == 0xFF:
            offset += 1
        if offset >= len(value):
            return False
        marker = value[offset]
        offset += 1
        if marker in {0x01, *range(0xD0, 0xD8)}:
            continue
        if marker in {0x00, 0xD8, 0xD9} or offset + 2 > len(value):
            return False
        segment_length = struct.unpack_from(">H", value, offset)[0]
        if segment_length < 2 or offset + segment_length > len(value):
            return False
        if marker in start_of_frame:
            component_count = value[offset + 7] if segment_length >= 8 else 0
            if (
                component_count not in {1, 3, 4}
                or segment_length != 8 + 3 * component_count
                or value[offset + 2] not in {8, 12}
            ):
                return False
            height, width = struct.unpack_from(">HH", value, offset + 3)
            if not height or not width:
                return False
            frame_components = {}
            for component_offset in range(offset + 8, offset + segment_length, 3):
                component = value[component_offset]
                sampling = value[component_offset + 1]
                quantization = value[component_offset + 2]
                if (
                    component in frame_components
                    or not 1 <= sampling >> 4 <= 4
                    or not 1 <= sampling & 0x0F <= 4
                    or quantization > 3
                ):
                    return False
                frame_components[component] = quantization
            saw_frame = True
        elif marker == 0xDB:
            cursor = offset + 2
            while cursor < offset + segment_length:
                precision_and_id = value[cursor]
                cursor += 1
                precision, table_id = precision_and_id >> 4, precision_and_id & 0x0F
                table_bytes = 64 * (precision + 1)
                if (
                    precision not in {0, 1} or table_id > 3
                    or cursor + table_bytes > offset + segment_length
                ):
                    return False
                coefficients = value[cursor : cursor + table_bytes]
                step = precision + 1
                if any(not any(coefficients[index : index + step]) for index in range(0, table_bytes, step)):
                    return False
                quantization_tables.add(table_id)
                cursor += table_bytes
            if cursor != offset + segment_length:
                return False
            saw_quantization = True
        elif marker == 0xC4:
            cursor = offset + 2
            while cursor < offset + segment_length:
                if cursor + 17 > offset + segment_length:
                    return False
                table_class_and_id = value[cursor]
                counts = value[cursor + 1 : cursor + 17]
                cursor += 17
                table_class = table_class_and_id >> 4
                table_id = table_class_and_id & 0x0F
                symbol_count = sum(counts)
                available_codes = 1
                for count in counts:
                    available_codes = available_codes * 2 - count
                    if available_codes < 0:
                        return False
                if (
                    table_class not in {0, 1} or table_id > 3 or not symbol_count
                    or cursor + symbol_count > offset + segment_length
                ):
                    return False
                (dc_tables if table_class == 0 else ac_tables).add(table_id)
                cursor += symbol_count
            if cursor != offset + segment_length:
                return False
            saw_coding_table = True
        if marker == 0xDA:
            component_count = value[offset + 2] if segment_length >= 3 else 0
            scan_components: set[int] = set()
            referenced_tables_are_valid = True
            for component_offset in range(offset + 3, offset + 3 + 2 * component_count, 2):
                component = value[component_offset]
                tables = value[component_offset + 1]
                scan_components.add(component)
                referenced_tables_are_valid &= (
                    tables >> 4 in dc_tables and tables & 0x0F in ac_tables
                )
            scan_start = offset + segment_length
            return (
                saw_frame
                and saw_quantization
                and saw_coding_table
                and component_count in {1, 3, 4}
                and segment_length == 6 + 2 * component_count
                and len(scan_components) == component_count
                and scan_components.issubset(frame_components)
                and all(table in quantization_tables for table in frame_components.values())
                and referenced_tables_are_valid
                and scan_start < len(value) - 2
                and bool(value[scan_start:-2])
                and value.find(b"\xff\xd9", scan_start) == len(value) - 2
            )
        offset += segment_length
    return False


def _image_bytes(blob: bytes, view: tuple[int, int], mime_type: str) -> None:
    start, length = view
    value = blob[start : start + length]
    valid = (
        mime_type == "image/png" and _valid_png(value)
    ) or (
        mime_type == "image/jpeg" and _valid_jpeg(value)
    ) or (
        mime_type == "image/webp" and _valid_webp(value)
    )
    if valid and mime_type in {"image/jpeg", "image/webp"}:
        try:
            from PIL import Image

            with Image.open(BytesIO(value)) as image:
                image.verify()
        except (ImportError, OSError, SyntaxError, ValueError):
            valid = False
    if not valid:
        raise ValueError("GLB embedded image payload does not match its MIME type")


def _valid_webp(value: bytes) -> bool:
    if (
        len(value) < 20 or not value.startswith(b"RIFF") or value[8:12] != b"WEBP"
        or struct.unpack_from("<I", value, 4)[0] + 8 != len(value)
    ):
        return False
    offset = 12
    image_chunks = 0
    while offset < len(value):
        if offset + 8 > len(value):
            return False
        kind = value[offset : offset + 4]
        length = struct.unpack_from("<I", value, offset + 4)[0]
        start = offset + 8
        end = start + length
        padded_end = end + (length % 2)
        if end > len(value) or padded_end > len(value) or any(value[end:padded_end]):
            return False
        payload = value[start:end]
        if kind == b"VP8 ":
            frame_tag = int.from_bytes(payload[:3], "little") if len(payload) >= 3 else 1
            if (
                len(payload) <= 10 or frame_tag & 1 or not frame_tag >> 5
                or frame_tag >> 5 > len(payload) - 10
                or payload[3:6] != b"\x9d\x01\x2a"
                or not struct.unpack_from("<H", payload, 6)[0] & 0x3FFF
                or not struct.unpack_from("<H", payload, 8)[0] & 0x3FFF
            ):
                return False
            image_chunks += 1
        elif kind == b"VP8L":
            if len(payload) <= 5 or payload[0] != 0x2F:
                return False
            packed = struct.unpack_from("<I", payload, 1)[0]
            if packed >> 29:
                return False
            image_chunks += 1
        offset = padded_end
    return offset == len(value) and image_chunks == 1


def _textures(
    document: dict[str, object], blob: bytes, views: list[tuple[int, int]],
    accessors: list[Accessor]
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    images = [_object(item, "image") for item in _array(document, "images")]
    image_payloads: list[tuple[int, str]] = []
    for image in images:
        if "uri" in image:
            raise ValueError("GLB images must be embedded")
        view = _index(image.get("bufferView"), len(views), "image bufferView")
        if any(accessor.view_index == view for accessor in accessors):
            raise ValueError("GLB image bufferView must not be shared with an accessor")
        raw_view = _object(_array(document, "bufferViews", required=True)[view], "bufferView")
        if raw_view.get("byteStride") is not None or raw_view.get("target") is not None:
            raise ValueError("GLB image bufferView must not declare byteStride or target")
        mime_type = image.get("mimeType")
        if mime_type not in IMAGE_MIME_TYPES:
            raise ValueError("GLB embedded image MIME type is unsupported")
        image_payloads.append((view, str(mime_type)))
    textures = [_object(item, "texture") for item in _array(document, "textures")]
    samplers = [_object(item, "sampler") for item in _array(document, "samplers")]
    sampler_values = {
        "magFilter": {9728, 9729},
        "minFilter": {9728, 9729, 9984, 9985, 9986, 9987},
        "wrapS": {33071, 33648, 10497},
        "wrapT": {33071, 33648, 10497},
    }
    for sampler in samplers:
        for name, allowed in sampler_values.items():
            if name in sampler and sampler[name] not in allowed:
                raise ValueError(f"GLB sampler {name} is invalid")
    webp_used = False
    for texture in textures:
        source = texture.get("source")
        core_source: int | None = None
        if source is not None:
            core_source = _index(source, len(images), "texture source")
            if images[core_source].get("mimeType") not in {"image/jpeg", "image/png"}:
                raise ValueError("GLB core texture source must use a JPEG or PNG image")
        extension = texture.get("extensions")
        extension_source: int | None = None
        if isinstance(extension, dict) and "EXT_texture_webp" in extension:
            webp = extension.get("EXT_texture_webp")
            if not isinstance(webp, dict):
                raise ValueError("GLB EXT_texture_webp must be an object")
            extension_source = _index(
                webp.get("source"), len(images), "EXT_texture_webp source"
            )
            if images[extension_source].get("mimeType") != "image/webp":
                raise ValueError("GLB EXT_texture_webp source must use a WebP image")
            webp_used = True
        if core_source is None and extension_source is None:
            raise ValueError("GLB texture source index is invalid")
        if "sampler" in texture:
            _index(texture["sampler"], len(samplers), "texture sampler")
    if webp_used:
        used = document.get("extensionsUsed")
        required = document.get("extensionsRequired")
        if (
            not isinstance(used, list) or "EXT_texture_webp" not in used
            or not isinstance(required, list) or "EXT_texture_webp" not in required
        ):
            raise ValueError("GLB WebP textures must declare EXT_texture_webp as used and required")
    for view, mime_type in image_payloads:
        _image_bytes(blob, views[view], mime_type)
    return images, textures


def _texture_index(value: object, textures: list[dict[str, object]], name: str) -> int:
    info = _object(value, name)
    tex_coord = info.get("texCoord", 0)
    if isinstance(tex_coord, bool) or not isinstance(tex_coord, int) or tex_coord != 0:
        raise ValueError(f"GLB {name} must use TEXCOORD_0")
    return _index(info.get("index"), len(textures), name)


def _texture_source(texture: dict[str, object], image_count: int) -> int:
    source: object | None = None
    extension = texture.get("extensions")
    if isinstance(extension, dict):
        webp = extension.get("EXT_texture_webp")
        if isinstance(webp, dict):
            source = webp.get("source")
    if source is None:
        source = texture.get("source")
    return _index(source, image_count, "texture source")


def _validate_materials(
    document: dict[str, object], textures: list[dict[str, object]]
) -> list[dict[str, object]]:
    materials = [_object(item, "material") for item in _array(document, "materials")]
    for material in materials:
        pbr = material.get("pbrMetallicRoughness")
        if pbr is not None:
            pbr = _object(pbr, "material pbrMetallicRoughness")
            for name in ("baseColorTexture", "metallicRoughnessTexture"):
                if name in pbr:
                    _texture_index(pbr[name], textures, name)
        for name in ("normalTexture", "occlusionTexture", "emissiveTexture"):
            if name in material:
                _texture_index(material[name], textures, name)
    return materials


def _reachable_meshes(
    document: dict[str, object], mesh_count: int, skin_count: int
) -> tuple[set[int], set[int]]:
    nodes = [_object(item, "node") for item in _array(document, "nodes", required=True)]
    children: list[list[int]] = []
    parents = [0] * len(nodes)
    for node in nodes:
        if "camera" in node:
            raise ValueError("GLB camera nodes are not supported by this artifact contract")
        if "weights" in node:
            raise ValueError("GLB morph weights are not supported by this artifact contract")
        matrix = node.get("matrix")
        transform_fields = ("translation", "rotation", "scale")
        if matrix is not None and any(field in node for field in transform_fields):
            raise ValueError("GLB node cannot combine matrix and TRS transforms")
        transform_sizes = {"matrix": 16, "translation": 3, "rotation": 4, "scale": 3}
        for field, size in transform_sizes.items():
            value = node.get(field)
            if value is not None and (
                not isinstance(value, list)
                or len(value) != size
                or any(
                    isinstance(item, bool)
                    or not isinstance(item, (int, float))
                    or not math.isfinite(float(item))
                    for item in value
                )
            ):
                raise ValueError(f"GLB node {field} transform is invalid")
        if isinstance(node.get("rotation"), list) and not math.isclose(
            sum(float(item) ** 2 for item in node["rotation"]),
            1.0,
            rel_tol=1e-6,
            abs_tol=1e-6,
        ):
            raise ValueError("GLB node rotation quaternion is not normalized")
        scale = node.get("scale")
        if isinstance(scale, list) and any(abs(float(item)) <= 1e-12 for item in scale):
            raise ValueError("GLB node scale collapses reachable geometry")
        if isinstance(matrix, list):
            if (
                any(abs(float(matrix[index])) > 1e-12 for index in (3, 7, 11))
                or not math.isclose(float(matrix[15]), 1.0, abs_tol=1e-12)
            ):
                raise ValueError("GLB node matrix is not an affine transform")
            determinant = (
                float(matrix[0]) * (
                    float(matrix[5]) * float(matrix[10])
                    - float(matrix[6]) * float(matrix[9])
                )
                - float(matrix[4]) * (
                    float(matrix[1]) * float(matrix[10])
                    - float(matrix[2]) * float(matrix[9])
                )
                + float(matrix[8]) * (
                    float(matrix[1]) * float(matrix[6])
                    - float(matrix[2]) * float(matrix[5])
                )
            )
            if abs(determinant) <= 1e-12:
                raise ValueError("GLB node matrix collapses reachable geometry")
            basis = (
                tuple(float(matrix[index]) for index in (0, 1, 2)),
                tuple(float(matrix[index]) for index in (4, 5, 6)),
                tuple(float(matrix[index]) for index in (8, 9, 10)),
            )
            lengths = [math.sqrt(sum(item * item for item in column)) for column in basis]
            for first, second in ((0, 1), (0, 2), (1, 2)):
                dot = sum(basis[first][axis] * basis[second][axis] for axis in range(3))
                if not math.isclose(
                    dot / (lengths[first] * lengths[second]), 0.0, abs_tol=1e-6
                ):
                    raise ValueError("GLB node matrix contains unsupported shear")
        raw_children = node.get("children", [])
        if not isinstance(raw_children, list):
            raise ValueError("GLB node children must be an array")  # noqa: TRY004
        node_children = [_index(item, len(nodes), "node child") for item in raw_children]
        if len(node_children) != len(set(node_children)):
            raise ValueError("GLB node contains duplicate children")
        children.append(node_children)
        for child in node_children:
            parents[child] += 1
            if parents[child] > 1:
                raise ValueError("GLB node has more than one parent")
        if "mesh" in node:
            _index(node["mesh"], mesh_count, "node mesh")
        if "skin" in node:
            _index(node["skin"], skin_count, "node skin")
    state = [0] * len(nodes)

    def visit(index: int) -> None:
        if state[index] == 1:
            raise ValueError("GLB node graph contains a cycle")
        if state[index] == 2:
            return
        state[index] = 1
        for child in children[index]:
            visit(child)
        state[index] = 2

    for index in range(len(nodes)):
        visit(index)
    scenes = [_object(item, "scene") for item in _array(document, "scenes", required=True)]
    scene_roots: list[list[int]] = []
    for scene in scenes:
        roots = scene.get("nodes", [])
        if not isinstance(roots, list):
            raise ValueError("GLB scene nodes must be an array")  # noqa: TRY004
        validated_roots = [_index(item, len(nodes), "scene node") for item in roots]
        if (
            len(validated_roots) != len(set(validated_roots))
            or any(parents[item] for item in validated_roots)
        ):
            raise ValueError("GLB scene roots are invalid")
        scene_roots.append(validated_roots)
    scene_index = _index(document.get("scene", 0), len(scenes), "default scene")
    pending = scene_roots[scene_index]
    if not pending:
        raise ValueError("GLB default scene has no root nodes")
    reachable: set[int] = set()
    seen: set[int] = set()
    while pending:
        index = pending.pop()
        if index in seen:
            continue
        seen.add(index)
        node = nodes[index]
        if "mesh" in node:
            reachable.add(int(node["mesh"]))
        pending.extend(children[index])
    if not reachable:
        raise ValueError("GLB default scene does not reach a mesh")
    return reachable, seen


def _triangle_primitive(
    primitive: dict[str, object], accessors: list[Accessor]
) -> tuple[Accessor, dict[str, object]]:
    if primitive.get("mode", 4) != 4:
        raise ValueError("GLB mesh primitive is not TRIANGLES")
    if "targets" in primitive:
        raise ValueError("GLB morph targets are not supported by this artifact contract")
    attributes = _object(primitive.get("attributes"), "primitive attributes")
    for name, value in attributes.items():
        attribute = accessors[_index(value, len(accessors), f"{name} accessor")]
        if attribute.start % 4 or attribute.stride % 4:
            raise ValueError("GLB vertex attributes must be four-byte aligned")
        if attribute.target not in {None, 34962}:
            raise ValueError("GLB vertex attribute bufferView target must be ARRAY_BUFFER")
        if attribute.component_type == 5125:
            raise ValueError("GLB vertex attributes must not use UNSIGNED_INT components")
        if attribute.component_type == 5126:
            _finite(attribute, name)
        if name == "NORMAL" and (
            attribute.component_type != 5126 or attribute.kind != "VEC3"
            or attribute.normalized
        ):
            raise ValueError("GLB NORMAL accessor must be unnormalized FLOAT VEC3")
        if name == "TANGENT" and (
            attribute.component_type != 5126 or attribute.kind != "VEC4"
            or attribute.normalized
        ):
            raise ValueError("GLB TANGENT accessor must be unnormalized FLOAT VEC4")
        if name.startswith("TEXCOORD_") and (
            attribute.kind != "VEC2" or attribute.component_type not in {5121, 5123, 5126}
            or (attribute.component_type != 5126 and not attribute.normalized)
        ):
            raise ValueError("GLB texture-coordinate accessor type is invalid")
        if name.startswith("JOINTS_") and (
            attribute.kind != "VEC4" or attribute.component_type not in {5121, 5123}
            or attribute.normalized
        ):
            raise ValueError("GLB joint accessor type or normalization is invalid")
        if name.startswith("COLOR_") and (
            attribute.kind not in {"VEC3", "VEC4"}
            or attribute.component_type not in {5121, 5123, 5126}
            or (attribute.component_type != 5126 and not attribute.normalized)
        ):
            raise ValueError("GLB color accessor type or normalization is invalid")
        if name.startswith("WEIGHTS_") and (
            attribute.kind != "VEC4"
            or attribute.component_type not in {5121, 5123, 5126}
            or (attribute.component_type != 5126 and not attribute.normalized)
        ):
            raise ValueError("GLB weight accessor type or normalization is invalid")
        if not name.startswith("_") and name not in {
            "POSITION", "NORMAL", "TANGENT", "TEXCOORD_0", "TEXCOORD_1",
            "COLOR_0", "JOINTS_0", "WEIGHTS_0",
        }:
            raise ValueError(f"GLB vertex attribute semantic is unsupported: {name}")
    position = accessors[_index(attributes.get("POSITION"), len(accessors), "POSITION accessor")]
    if position.component_type != 5126 or position.kind != "VEC3" or position.count < 3:
        raise ValueError("GLB POSITION accessor must be FLOAT VEC3 with at least three vertices")
    minimum = [math.inf, math.inf, math.inf]
    maximum = [-math.inf, -math.inf, -math.inf]
    for value in position.values():
        if any(not math.isfinite(float(component)) for component in value):
            raise ValueError("GLB POSITION accessor contains non-finite coordinates")
        for axis in range(3):
            minimum[axis] = min(minimum[axis], float(value[axis]))
            maximum[axis] = max(maximum[axis], float(value[axis]))
    if position.minimum is None or position.maximum is None:
        raise ValueError("GLB POSITION accessor must declare min and max bounds")
    for declared, actual in ((position.minimum, minimum), (position.maximum, maximum)):
        if any(not math.isclose(declared[i], actual[i], rel_tol=1e-6, abs_tol=1e-7) for i in range(3)):
            raise ValueError("GLB POSITION accessor bounds do not match its coordinates")
    if max(maximum[axis] - minimum[axis] for axis in range(3)) <= 1e-8:
        raise ValueError("GLB mesh has a zero-size position extent")
    indices = accessors[_index(primitive.get("indices"), len(accessors), "indices accessor")]
    if indices.target not in {None, 34963}:
        raise ValueError("GLB index bufferView target must be ELEMENT_ARRAY_BUFFER")
    if (
        indices.component_type not in {5121, 5123, 5125} or indices.kind != "SCALAR"
        or indices.count < 3 or indices.count % 3 or indices.interleaved
        or indices.normalized
    ):
        raise ValueError("GLB indices must be unsigned SCALAR triangle indices")
    for name, value in attributes.items():
        attribute = accessors[_index(value, len(accessors), f"{name} accessor")]
        if attribute.count != position.count:
            raise ValueError("GLB primitive vertex attribute counts do not match POSITION")
    nondegenerate = False
    triangle: list[int] = []
    for raw in indices.values():
        index = int(raw[0])
        if not 0 <= index < position.count:
            raise ValueError("GLB triangle index exceeds POSITION count")
        triangle.append(index)
        if len(triangle) == 3:
            if len(set(triangle)) == 3:
                a, b, c = (position.value(item) for item in triangle)
                ab = tuple(float(b[i]) - float(a[i]) for i in range(3))
                ac = tuple(float(c[i]) - float(a[i]) for i in range(3))
                cross = (
                    ab[1] * ac[2] - ab[2] * ac[1],
                    ab[2] * ac[0] - ab[0] * ac[2],
                    ab[0] * ac[1] - ab[1] * ac[0],
                )
                nondegenerate |= sum(value * value for value in cross) > 1e-20
            triangle.clear()
    if not nondegenerate:
        raise ValueError("GLB mesh contains no finite nondegenerate triangle")
    return position, attributes


def validate_mesh_glb_bytes(data: bytes, *, profile: str = "geometry") -> dict[str, int]:
    """Validate one in-memory artifact and return bounded structural metadata."""
    if profile not in PROFILES:
        raise ValueError(f"unsupported GLB validation profile: {profile}")
    document, blob = _parse(data)
    if "animations" in document:
        raise ValueError("GLB animations are not supported by this artifact contract")
    if "cameras" in document:
        raise ValueError("GLB cameras are not supported by this artifact contract")
    accessors, views = _accessors(document, blob)
    images, textures = _textures(document, blob, views, accessors)
    materials = _validate_materials(document, textures)
    meshes = [_object(item, "mesh") for item in _array(document, "meshes", required=True)]
    skins = [_object(item, "skin") for item in _array(document, "skins")]
    reachable, reachable_nodes = _reachable_meshes(document, len(meshes), len(skins))
    if reachable != set(range(len(meshes))):
        raise ValueError("GLB contains a mesh that is unreachable from the default scene")

    skinned_primitives: list[tuple[Accessor, dict[str, object]]] = []
    attribute_accessors: set[int] = set()
    index_accessors: set[int] = set()
    primitive_count = 0
    for mesh in meshes:
        if "weights" in mesh:
            raise ValueError("GLB morph weights are not supported by this artifact contract")
        primitives = mesh.get("primitives")
        if not isinstance(primitives, list) or not primitives:
            raise ValueError("GLB mesh has no primitives")
        for raw in primitives:
            primitive_count += 1
            primitive = _object(raw, "primitive")
            if "material" in primitive:
                _index(primitive["material"], len(materials), "primitive material")
            position, attributes = _triangle_primitive(primitive, accessors)
            index_accessors.add(
                _index(primitive.get("indices"), len(accessors), "indices accessor")
            )
            attribute_accessors.update(
                _index(value, len(accessors), f"{name} accessor")
                for name, value in attributes.items()
            )
            if profile in {"textured", "textured-pbr"}:
                uv = accessors[_index(attributes.get("TEXCOORD_0"), len(accessors), "TEXCOORD_0 accessor")]
                if uv.component_type != 5126 or uv.kind != "VEC2" or uv.count != position.count:
                    raise ValueError("GLB TEXCOORD_0 must be FLOAT VEC2 matching POSITION count")
                _finite(uv, "TEXCOORD_0")
                material = materials[_index(primitive.get("material"), len(materials), "primitive material")]
                pbr = _object(material.get("pbrMetallicRoughness"), "material pbrMetallicRoughness")
                base_texture = _texture_index(
                    pbr.get("baseColorTexture"), textures, "baseColorTexture"
                )
                if profile == "textured-pbr":
                    metallic_texture = _texture_index(
                        pbr.get("metallicRoughnessTexture"),
                        textures,
                        "metallicRoughnessTexture",
                    )
                    if base_texture == metallic_texture or _texture_source(
                        textures[base_texture], len(images)
                    ) == _texture_source(textures[metallic_texture], len(images)):
                        raise ValueError("GLB PBR textures must use distinct embedded images")
            if profile == "skinned":
                skinned_primitives.append((position, attributes))

    if any(
        accessor.interleaved and index not in attribute_accessors
        for index, accessor in enumerate(accessors)
    ):
        raise ValueError("GLB byteStride is only permitted for vertex attribute accessors")
    attribute_views = {accessors[index].view_index for index in attribute_accessors}
    index_views = {accessors[index].view_index for index in index_accessors}
    if attribute_views & index_views:
        raise ValueError("GLB bufferView must not mix vertex attributes and indices")
    for view in attribute_views:
        view_accessors = {
            index for index in attribute_accessors if accessors[index].view_index == view
        }
        if len(view_accessors) > 1 and not all(
            accessors[index].interleaved for index in view_accessors
        ):
            raise ValueError("GLB shared vertex-attribute bufferView must declare byteStride")

    if profile in {"textured", "textured-pbr"} and not images:
        raise ValueError("GLB textured mesh contains no embedded images")
    if profile == "textured-pbr" and (len(images) < 2 or len(textures) < 2):
        raise ValueError("GLB PBR mesh must contain base-color and metallic-roughness images")
    if profile == "skinned":
        if len(skins) != 1:
            raise ValueError("GLB SkinTokens profile requires exactly one skin")
        nodes = [_object(item, "node") for item in _array(document, "nodes", required=True)]
        mesh_nodes = [node for node in nodes if "mesh" in node]
        if not mesh_nodes or any(node.get("skin") != 0 for node in mesh_nodes):
            raise ValueError("every GLB skinned mesh node must bind a skin")
        for skin in skins:
            joints = skin.get("joints")
            if not isinstance(joints, list) or not joints:
                raise ValueError("GLB skin has no joints")
            if len(joints) != len(set(joints)):
                raise ValueError("GLB skin contains duplicate joints")
            for joint in joints:
                joint_index = _index(joint, len(nodes), "skin joint")
                if joint_index not in reachable_nodes:
                    raise ValueError("GLB skin joint is unreachable from the default scene")
            if skin.get("skeleton") is not None:
                skeleton = _index(skin["skeleton"], len(nodes), "skin skeleton")
                if skeleton not in joints:
                    raise ValueError("GLB skin skeleton must be one of its joints")
                parents: dict[int, int] = {}
                for parent, node in enumerate(nodes):
                    for child in node.get("children", []):
                        parents[int(child)] = parent
                for joint in joints:
                    cursor = int(joint)
                    while cursor != skeleton and cursor in parents:
                        cursor = parents[cursor]
                    if cursor != skeleton:
                        raise ValueError("GLB skin skeleton must be an ancestor of every joint")
            inverse = accessors[_index(skin.get("inverseBindMatrices"), len(accessors), "inverseBindMatrices accessor")]
            if inverse.target is not None:
                raise ValueError("GLB inverseBindMatrices bufferView must not declare a target")
            if inverse.view_index in attribute_views | index_views:
                raise ValueError("GLB inverseBindMatrices bufferView must have a unique role")
            if inverse.component_type != 5126 or inverse.kind != "MAT4" or inverse.count != len(joints):
                raise ValueError("GLB inverseBindMatrices must be FLOAT MAT4 matching joint count")
            _finite(inverse, "inverseBindMatrices")
        first_joints = _object(skins[0], "skin").get("joints")
        assert isinstance(first_joints, list)
        joint_count = len(first_joints)
        for position, attributes in skinned_primitives:
            joints = accessors[_index(attributes.get("JOINTS_0"), len(accessors), "JOINTS_0 accessor")]
            weights = accessors[_index(attributes.get("WEIGHTS_0"), len(accessors), "WEIGHTS_0 accessor")]
            normals = accessors[_index(attributes.get("NORMAL"), len(accessors), "NORMAL accessor")]
            if (
                joints.component_type not in {5121, 5123} or joints.kind != "VEC4"
                or joints.normalized
                or weights.component_type != 5126 or weights.kind != "VEC4"
                or normals.component_type != 5126 or normals.kind != "VEC3"
                or joints.count != position.count or weights.count != position.count
                or normals.count != position.count
            ):
                raise ValueError("GLB skin vertex attributes do not match POSITION count")
            _finite(normals, "NORMAL")
            for joint_value, weight_value in zip(joints.values(), weights.values(), strict=True):
                if any(int(value) >= joint_count for value in joint_value):
                    raise ValueError("GLB JOINTS_0 references an unknown joint")
                if any(not math.isfinite(float(value)) or float(value) < 0 for value in weight_value):
                    raise ValueError("GLB WEIGHTS_0 contains invalid weights")
                if not math.isclose(sum(float(value) for value in weight_value), 1.0, abs_tol=1e-3):
                    raise ValueError("GLB WEIGHTS_0 weights are not normalized")
    return {
        "mesh_count": len(meshes),
        "primitive_count": primitive_count,
        "accessor_count": len(accessors),
        "binary_bytes": len(blob),
        "material_count": len(materials),
        "texture_count": len(textures),
        "image_count": len(images),
        "skin_count": len(skins),
    }


def validate_mesh_glb(path: Path, *, profile: str = "geometry") -> dict[str, int]:
    """Validate one atomic artifact before publication."""
    return validate_mesh_glb_bytes(path.read_bytes(), profile=profile)
