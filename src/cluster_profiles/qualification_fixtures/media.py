"""Media signatures, decoding, metadata, and safe archive entries."""

from __future__ import annotations

import base64
import binascii
import io
import json
import os
import shutil
import stat
import struct
import subprocess
import time
import wave
import zipfile
import zlib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath

from cluster_profiles.glb_validation import validate_mesh_glb_bytes

from .contracts import FixtureError, FixtureObservationUnknown
from .values import _strict_json_loads

_MEDIA_DEADLINE: ContextVar[float | None] = ContextVar(
    "qualification_media_deadline", default=None
)


@contextmanager
def media_budget(timeout_seconds: float) -> Iterator[None]:
    token = _MEDIA_DEADLINE.set(time.monotonic() + timeout_seconds)
    try:
        yield
    finally:
        _MEDIA_DEADLINE.reset(token)


def _media_timeout(normal: float) -> float:
    deadline = _MEDIA_DEADLINE.get()
    if deadline is None:
        return normal
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise FixtureObservationUnknown("media observation deadline elapsed")
    return min(normal, remaining)


def _load_content(root: Traversable, value: Mapping[str, object]) -> bytes:
    raw_path = value.get("path")
    encoding = value.get("encoding")
    if not isinstance(raw_path, str) or not raw_path:
        raise FixtureError("fixture path is invalid")
    path = PurePosixPath(raw_path)
    if path.is_absolute() or ".." in path.parts or any(not part for part in path.parts):
        raise FixtureError("fixture path is unsafe")
    source = root.joinpath(*path.parts)
    declared = value.get("size_bytes")
    if type(declared) is not int or not 0 < declared <= 1024**3:
        raise FixtureError("fixture byte descriptor is unavailable")
    maximum = declared if encoding == "identity" else 2 * declared + 4096
    try:
        if isinstance(source, Path):
            descriptor = os.open(source, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            try:
                observed = os.fstat(descriptor)
                if not stat.S_ISREG(observed.st_mode) or observed.st_size > maximum:
                    raise FixtureError("fixture source exceeds its byte contract")
                with os.fdopen(descriptor, "rb", closefd=False) as handle:
                    raw = handle.read(maximum + 1)
            finally:
                os.close(descriptor)
        else:
            with source.open("rb") as handle:
                raw = handle.read(maximum + 1)
        if len(raw) > maximum:
            raise FixtureError("fixture source exceeds its byte contract")
    except (FileNotFoundError, IsADirectoryError) as error:
        raise FixtureError(f"fixture file is unavailable: {raw_path}") from error
    if encoding == "identity":
        return raw
    if encoding == "base64":
        try:
            return base64.b64decode(b"".join(raw.split()), validate=True)
        except (binascii.Error, ValueError) as error:
            raise FixtureError(f"fixture base64 is invalid: {raw_path}") from error
    raise FixtureError("fixture encoding must be identity or base64")


def _validate_magic(content: bytes, format_name: str) -> None:
    if format_name == "png":
        if len(content) < 24 or content[:8] != b"\x89PNG\r\n\x1a\n":
            raise FixtureError("PNG assertion failed")
        width, height = struct.unpack(">II", content[16:24])
        if width < 1 or height < 1:
            raise FixtureError("PNG dimensions are invalid")
    elif format_name == "jpeg":
        if (
            len(content) < 4
            or not content.startswith(b"\xff\xd8")
            or not content.endswith(b"\xff\xd9")
        ):
            raise FixtureError("JPEG assertion failed")
    elif format_name == "wav":
        # wave.open accepts a seekable file object and validates RIFF/WAVE structure.
        try:
            with wave.open(io.BytesIO(content), "rb") as source:
                if source.getnchannels() < 1 or source.getframerate() < 1:
                    raise FixtureError("WAV stream metadata is invalid")
                source.readframes(min(1, source.getnframes()))
        except (EOFError, wave.Error) as error:
            raise FixtureError("WAV assertion failed") from error
    elif format_name == "mp4":
        if len(content) < 16 or content[4:8] != b"ftyp" or b"moov" not in content:
            raise FixtureError("MP4 container assertion failed")
    elif format_name == "glb":
        try:
            validate_mesh_glb_bytes(content, profile="geometry")
        except ValueError as error:
            raise FixtureError(f"GLB fixture structure is invalid: {error}") from error
    elif format_name == "json":
        try:
            _strict_json_loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise FixtureError("JSON assertion failed") from error
    elif format_name == "zip":
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                if not archive.namelist() or archive.testzip() is not None:
                    raise FixtureError("ZIP assertion failed")
        except (ValueError, zipfile.BadZipFile) as error:
            raise FixtureError("ZIP assertion failed") from error
    else:
        raise FixtureError("unknown semantic assertion format")


def _png_metadata(content: bytes) -> dict[str, int]:
    if not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise FixtureError("PNG signature is invalid")
    offset = 8
    chunks: list[tuple[bytes, bytes]] = []
    while offset < len(content):
        if len(content) - offset < 12:
            raise FixtureError("PNG chunk is truncated")
        size = struct.unpack(">I", content[offset : offset + 4])[0]
        chunk_type = content[offset + 4 : offset + 8]
        end = offset + 12 + size
        if end > len(content):
            raise FixtureError("PNG chunk exceeds the file")
        data = content[offset + 8 : offset + 8 + size]
        expected_crc = struct.unpack(">I", content[offset + 8 + size : end])[0]
        if zlib.crc32(chunk_type + data) & 0xFFFFFFFF != expected_crc:
            raise FixtureError("PNG chunk CRC is invalid")
        chunks.append((chunk_type, data))
        offset = end
        if chunk_type == b"IEND":
            break
    if offset != len(content):
        raise FixtureError("PNG has trailing content")
    if not chunks or chunks[0][0] != b"IHDR" or chunks[-1][0] != b"IEND":
        raise FixtureError("PNG chunk order is invalid")
    if len(chunks[0][1]) != 13:
        raise FixtureError("PNG IHDR is invalid")
    width, height, bit_depth, color_type, compression, filtering, interlace = (
        struct.unpack(">IIBBBBB", chunks[0][1])
    )
    if compression != 0 or filtering != 0 or interlace != 0:
        raise FixtureError("PNG encoding metadata is unsupported")
    channels_by_color = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    valid_depths = {
        0: {1, 2, 4, 8, 16},
        2: {8, 16},
        3: {1, 2, 4, 8},
        4: {8, 16},
        6: {8, 16},
    }
    if (
        width < 1
        or height < 1
        or width > 32_768
        or height > 32_768
        or color_type not in channels_by_color
        or bit_depth not in valid_depths[color_type]
    ):
        raise FixtureError("PNG pixel metadata is invalid")
    row_bytes = (width * channels_by_color[color_type] * bit_depth + 7) // 8
    expected_bytes = height * (row_bytes + 1)
    if expected_bytes > 512 * 1024**2:
        raise FixtureError("PNG decoded image exceeds the semantic bound")
    idat = b"".join(data for kind, data in chunks if kind == b"IDAT")
    if not idat:
        raise FixtureError("PNG has no image data")
    try:
        decompressor = zlib.decompressobj()
        pixels = decompressor.decompress(idat, expected_bytes + 1)
        if decompressor.unconsumed_tail or len(pixels) > expected_bytes:
            raise FixtureError("PNG decoded image exceeds the declared dimensions")
        pixels += decompressor.flush(expected_bytes + 1 - len(pixels))
    except zlib.error as error:
        raise FixtureError("PNG image data is invalid") from error
    if (
        len(pixels) != expected_bytes
        or not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
        or any(pixels[row * (row_bytes + 1)] > 4 for row in range(height))
    ):
        raise FixtureError("PNG decoded scanlines are invalid")
    return {
        "width": width,
        "height": height,
        "bit_depth": bit_depth,
        "color_type": color_type,
        "interlace": interlace,
    }


def _wav_metadata(content: bytes) -> dict[str, int | float]:
    try:
        with wave.open(io.BytesIO(content), "rb") as source:
            channels = source.getnchannels()
            sample_rate = source.getframerate()
            sample_width = source.getsampwidth()
            frame_count = source.getnframes()
            compression = source.getcomptype()
            frames = source.readframes(frame_count + 1)
    except (EOFError, wave.Error) as error:
        raise FixtureError("WAV structure is invalid") from error
    if compression != "NONE" or channels < 1 or sample_rate < 1 or frame_count < 1:
        raise FixtureError("WAV PCM metadata is invalid")
    if len(frames) != frame_count * channels * sample_width:
        raise FixtureError("WAV PCM payload is truncated")
    return {
        "channels": channels,
        "sample_rate": sample_rate,
        "sample_width_bytes": sample_width,
        "frame_count": frame_count,
        "duration_seconds": frame_count / sample_rate,
    }


def _ffprobe_metadata(path: Path) -> dict[str, object]:
    executable = shutil.which("ffprobe")
    if executable is None:
        raise FixtureObservationUnknown(
            "ffprobe is required for MP4 semantic qualification"
        )
    try:
        result = subprocess.run(
            [
                executable,
                "-v",
                "error",
                "-count_frames",
                "-show_entries",
                "stream=index,codec_type,codec_name,pix_fmt,width,height,r_frame_rate,avg_frame_rate,nb_frames,nb_read_frames,start_time,duration,sample_rate,channels:format=format_name,start_time,duration",
                "-of",
                "json",
                str(path),
            ],
            check=False,
            capture_output=True,
            timeout=_media_timeout(30),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FixtureObservationUnknown("ffprobe execution failed") from error
    if result.returncode != 0 or len(result.stdout) > 256 * 1024:
        raise FixtureError("ffprobe rejected the MP4 output")
    try:
        value = _strict_json_loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise FixtureObservationUnknown("ffprobe returned invalid metadata") from error
    streams = value.get("streams") if isinstance(value, Mapping) else None
    if not isinstance(streams, list) or any(
        not isinstance(item, Mapping) for item in streams
    ):
        raise FixtureObservationUnknown("MP4 stream metadata is invalid")
    video = [dict(item) for item in streams if item.get("codec_type") == "video"]
    audio = [dict(item) for item in streams if item.get("codec_type") == "audio"]
    if len(video) != 1:
        raise FixtureError("MP4 must contain exactly one video stream")
    stream = video[0]
    format_value = value.get("format") if isinstance(value, Mapping) else None
    if not isinstance(format_value, Mapping) or "mp4" not in str(
        format_value.get("format_name", "")
    ).split(","):
        raise FixtureError("artifact output is not an MP4 container")
    if isinstance(format_value, Mapping) and "duration" not in stream:
        stream["duration"] = format_value.get("duration")
    return {
        "video": stream,
        "audio": audio,
        "streams": [dict(item) for item in streams],
        "format": dict(format_value) if isinstance(format_value, Mapping) else {},
    }


def _verify_media_decode(path: Path) -> None:
    executable = shutil.which("ffmpeg")
    if executable is None:
        raise FixtureObservationUnknown(
            "ffmpeg is required for MP4 decode qualification"
        )
    try:
        result = subprocess.run(
            [
                executable,
                "-v",
                "error",
                "-xerror",
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-f",
                "null",
                "-",
            ],
            check=False,
            capture_output=True,
            timeout=_media_timeout(60),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FixtureObservationUnknown("ffmpeg decode verification failed") from error
    if result.returncode != 0 or len(result.stderr) > 256 * 1024:
        raise FixtureError("ffmpeg rejected the MP4 video stream")


def _glb_metadata(content: bytes, profile: str = "triangle-mesh") -> dict[str, int]:
    validator_profile = "geometry" if profile == "triangle-mesh" else profile
    try:
        return validate_mesh_glb_bytes(content, profile=validator_profile)
    except ValueError as error:
        raise FixtureError(f"GLB {profile} structure is invalid: {error}") from error


def _safe_zip_entries(content: bytes) -> list[tuple[str, bytes]]:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            result: list[tuple[str, bytes]] = []
            total = 0
            for info in archive.infolist():
                path = PurePosixPath(info.filename)
                if (
                    "\\" in info.filename
                    or "\x00" in info.filename
                    or path.is_absolute()
                    or ".." in path.parts
                    or info.is_dir()
                    or info.flag_bits & 0x1
                    or (info.external_attr >> 16) & 0o170000 == 0o120000
                ):
                    raise FixtureError("ZIP contains an unsafe entry")
                if any(name == info.filename for name, _ in result):
                    raise FixtureError("ZIP contains duplicate entries")
                total += info.file_size
                expansion_ratio = info.file_size / max(info.compress_size, 1)
                if (
                    total > 256 * 1024**2
                    or info.file_size > 128 * 1024**2
                    or expansion_ratio > 10_000
                    or info.compress_size == 0
                    and info.file_size > 0
                ):
                    raise FixtureError("ZIP expansion is unsafe")
                data = archive.read(info)
                if len(data) != info.file_size:
                    raise FixtureError("ZIP entry size changed")
                result.append((info.filename, data))
            if not result:
                raise FixtureError("ZIP has no files")
            return result
    except FixtureError:
        raise
    except (RuntimeError, ValueError, zipfile.BadZipFile) as error:
        raise FixtureError("ZIP structure is invalid") from error
