"""Images for glb validation."""

from __future__ import annotations

import struct
import zlib
from io import BytesIO


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
        if len(kind) != 4 or any(
            not (65 <= byte <= 90 or 97 <= byte <= 122) for byte in kind
        ):
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
        len(ihdr) != 13
        or chunks[-1] != (b"IEND", b"")
        or kinds.count(b"IHDR") != 1
        or kinds.count(b"IEND") != 1
        or kinds.count(b"PLTE") > 1
        or not idat_indices
        or idat_indices != list(range(idat_indices[0], idat_indices[-1] + 1))
        or (b"PLTE" in kinds and kinds.index(b"PLTE") > idat_indices[0])
    ):
        return False
    width, height, bit_depth, color_type, compression, filtering, interlace = (
        struct.unpack(">IIBBBBB", ihdr)
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
        not width
        or not height
        or width > 16_384
        or height > 16_384
        or bit_depth not in allowed_depths.get(color_type, set())
        or compression != 0
        or filtering != 0
        or interlace != 0
    ):
        return False
    if color_type == 3 and not any(
        kind == b"PLTE" and payload for kind, payload in chunks
    ):
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
        len(decoded) != decoded_bytes
        or not decoder.eof
        or decoder.unused_data
        or decoder.unconsumed_tail
    ):
        return False
    return all(decoded[row * (row_bytes + 1)] <= 4 for row in range(height))


def _valid_jpeg(value: bytes) -> bool:
    if (
        len(value) < 12
        or not value.startswith(b"\xff\xd8")
        or not value.endswith(b"\xff\xd9")
    ):
        return False
    start_of_frame = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
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
                    precision not in {0, 1}
                    or table_id > 3
                    or cursor + table_bytes > offset + segment_length
                ):
                    return False
                coefficients = value[cursor : cursor + table_bytes]
                step = precision + 1
                if any(
                    not any(coefficients[index : index + step])
                    for index in range(0, table_bytes, step)
                ):
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
                    table_class not in {0, 1}
                    or table_id > 3
                    or not symbol_count
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
            for component_offset in range(
                offset + 3, offset + 3 + 2 * component_count, 2
            ):
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
                and all(
                    table in quantization_tables for table in frame_components.values()
                )
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
        (mime_type == "image/png" and _valid_png(value))
        or (mime_type == "image/jpeg" and _valid_jpeg(value))
        or (mime_type == "image/webp" and _valid_webp(value))
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
        len(value) < 20
        or not value.startswith(b"RIFF")
        or value[8:12] != b"WEBP"
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
            frame_tag = (
                int.from_bytes(payload[:3], "little") if len(payload) >= 3 else 1
            )
            if (
                len(payload) <= 10
                or frame_tag & 1
                or not frame_tag >> 5
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
