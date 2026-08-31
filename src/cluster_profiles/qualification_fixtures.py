"""Digest-bound artifact-job qualification fixtures and semantic assertions."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import re
import shutil
import struct
import subprocess
import tempfile
import wave
import zipfile
import zlib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Protocol

from cluster_profiles.glb_validation import validate_mesh_glb_bytes

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,62}/[a-z0-9][a-z0-9-]{0,62}\Z")
_SLOT = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_CASE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")
_MEDIA_TYPE = re.compile(
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}\Z"
)
_INTERFACES = frozenset(
    {"artifact-job", "audio-job", "image-job", "mesh-job", "video-job"}
)
_FORMATS = frozenset({"glb", "json", "jpeg", "mp4", "png", "wav", "zip"})


class FixtureError(ValueError):
    """A checked-in fixture or its recipe binding is unsafe or inconsistent."""


class ArtifactTransferClient(Protocol):
    def download_file(
        self,
        path: str,
        destination: Path,
        *,
        media_type: str,
        expected_sha256: str,
        expected_size: int,
        overwrite: bool,
    ) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class Fixture:
    fixture_id: str
    path: str
    encoding: str
    name: str
    media_type: str
    size_bytes: int
    sha256: str
    content: bytes
    provenance: dict[str, str] | None = None

    def declaration(self, slot: str) -> dict[str, object]:
        return {
            "slot": slot,
            "name": self.name,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }

    def evidence(self, slot: str) -> dict[str, object]:
        return {
            **self.declaration(slot),
            **(
                {"provenance": dict(self.provenance)}
                if self.provenance is not None
                else {}
            ),
        }


@dataclass(frozen=True, slots=True)
class RecipeFixture:
    key: str
    content_sha256: str
    interface: str
    parameters: dict[str, object]
    inputs: tuple[tuple[str, Fixture], ...]
    output_limits: dict[str, object]
    timeout_seconds: int
    assertions: tuple[dict[str, object], ...]
    case_id: str = "default"
    supplemental_cases: tuple[RecipeFixture, ...] = ()

    @property
    def all_cases(self) -> tuple[RecipeFixture, ...]:
        return (self, *self.supplemental_cases)

    def preview(self) -> dict[str, object]:
        preview = {
            "kind": "artifact-job",
            "available": True,
            "recipe": self.key,
            "recipe_content_sha256": self.content_sha256,
            "interface": self.interface,
            "parameters": self.parameters,
            "inputs": [
                {"fixture": fixture.fixture_id, **fixture.evidence(slot)}
                for slot, fixture in self.inputs
            ],
            "output_limits": self.output_limits,
            "timeout_seconds": self.timeout_seconds,
            "assertions": list(self.assertions),
            "capabilities_path": "/api/v1/artifact-jobs/capabilities",
        }
        if self.supplemental_cases:
            preview["cases"] = [
                {
                    "id": case.case_id,
                    "parameters": case.parameters,
                    "inputs": [
                        {"fixture": fixture.fixture_id, **fixture.evidence(slot)}
                        for slot, fixture in case.inputs
                    ],
                    "output_limits": case.output_limits,
                    "timeout_seconds": case.timeout_seconds,
                    "assertions": list(case.assertions),
                }
                for case in self.all_cases
            ]
        return preview

    @contextmanager
    def materialize(self) -> Iterator[list[tuple[dict[str, object], Path]]]:
        with tempfile.TemporaryDirectory(prefix="vonk-qualification-fixtures-") as root:
            directory = Path(root)
            values: list[tuple[dict[str, object], Path]] = []
            for slot, fixture in self.inputs:
                path = directory / fixture.name
                path.write_bytes(fixture.content)
                path.chmod(0o600)
                values.append((fixture.declaration(slot), path))
            yield values


@dataclass(frozen=True, slots=True)
class ServiceCase:
    case_id: str
    method: str
    path: str
    body: object
    timeout_seconds: int
    max_response_bytes: int
    assertions: tuple[dict[str, object], ...]

    def render(self, alias: str, fixtures: Mapping[str, Fixture]) -> dict[str, object]:
        def substitute(value: object) -> object:
            if value == "$ALIAS":
                return alias
            if isinstance(value, list):
                return [substitute(item) for item in value]
            if isinstance(value, dict):
                if set(value) in ({"$fixture_data_uri"}, {"$fixture_base64"}):
                    marker = next(iter(value))
                    fixture_id = value[marker]
                    if not isinstance(fixture_id, str) or fixture_id not in fixtures:
                        raise FixtureError(
                            f"service case {self.case_id} references an unknown fixture"
                        )
                    fixture = fixtures[fixture_id]
                    encoded = base64.b64encode(fixture.content).decode("ascii")
                    if marker == "$fixture_data_uri":
                        return f"data:{fixture.media_type};base64,{encoded}"
                    return encoded
                return {key: substitute(item) for key, item in value.items()}
            return value

        return {
            "id": self.case_id,
            "method": self.method,
            "path": self.path,
            "body": substitute(self.body),
            "timeout_seconds": self.timeout_seconds,
            "max_response_bytes": self.max_response_bytes,
            "assertions": [substitute(item) for item in self.assertions],
        }


@dataclass(frozen=True, slots=True)
class ServiceRecipe:
    key: str
    content_sha256: str
    alias: str
    cases: tuple[ServiceCase, ...]
    higher_tiers: dict[str, tuple[str, ...]]

    def preview(self, fixtures: Mapping[str, Fixture]) -> dict[str, object]:
        return {
            "kind": "openai-service",
            "available": True,
            "recipe": self.key,
            "recipe_content_sha256": self.content_sha256,
            "alias": self.alias,
            "cases": [case.render(self.alias, fixtures) for case in self.cases],
            "higher_tiers": {
                key: list(values) for key, values in self.higher_tiers.items()
            },
        }


def _object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise FixtureError(f"{label} must be an object")
    return value


def _strict_json_loads(content: bytes | str) -> object:
    def reject_constant(value: str) -> object:
        raise ValueError(f"non-finite JSON number: {value}")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(
        content,
        parse_constant=reject_constant,
        object_pairs_hook=unique_object,
    )


def _integer(value: object, label: str, minimum: int, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise FixtureError(f"{label} is invalid")
    return value


def _load_content(
    root: resources.abc.Traversable, value: Mapping[str, object]
) -> bytes:
    raw_path = value.get("path")
    encoding = value.get("encoding")
    if not isinstance(raw_path, str) or not raw_path:
        raise FixtureError("fixture path is invalid")
    path = PurePosixPath(raw_path)
    if path.is_absolute() or ".." in path.parts or any(not part for part in path.parts):
        raise FixtureError("fixture path is unsafe")
    source = root.joinpath(*path.parts)
    try:
        raw = source.read_bytes()
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
        raise FixtureError("ffprobe is required for MP4 semantic qualification")
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
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FixtureError("ffprobe execution failed") from error
    if result.returncode != 0 or len(result.stdout) > 256 * 1024:
        raise FixtureError("ffprobe rejected the MP4 output")
    try:
        value = _strict_json_loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise FixtureError("ffprobe returned invalid metadata") from error
    streams = value.get("streams") if isinstance(value, Mapping) else None
    if not isinstance(streams, list) or any(
        not isinstance(item, Mapping) for item in streams
    ):
        raise FixtureError("MP4 stream metadata is invalid")
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
        raise FixtureError("ffmpeg is required for MP4 decode qualification")
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
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FixtureError("ffmpeg decode verification failed") from error
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


def _validate_hunyuan_ocr_zip(content: bytes) -> None:
    entries = _safe_zip_entries(content)
    expected_names = ["manifest.json", "documents/001-digit7.md"]
    if [name for name, _ in entries] != expected_names:
        raise FixtureError("Hunyuan OCR ZIP file-name contract failed")
    payloads = dict(entries)
    manifest = _parse_json_object(payloads["manifest.json"], "Hunyuan OCR manifest")
    if set(manifest) != {
        "documents",
        "inference",
        "model",
        "model_revision",
        "runtime_source_revision",
        "sampling",
        "schema_version",
        "task_type",
    }:
        raise FixtureError("Hunyuan OCR manifest shape is invalid")
    expected = {
        "inference": "vllm-dflash",
        "model": "tencent/HunyuanOCR",
        "model_revision": "47644ecc4fc854efa4f505155158831f36773ee4",
        "runtime_source_revision": "c55965d3da1e6f41987abec8068f2e70851318bc",
        "schema_version": 1,
        "task_type": "doc_parse",
    }
    if any(manifest.get(name) != value for name, value in expected.items()):
        raise FixtureError("Hunyuan OCR manifest authority is invalid")
    if manifest.get("sampling") != {
        "repetition_penalty": 1.08,
        "temperature": 0.0,
        "top_k": -1,
        "top_p": 1.0,
    }:
        raise FixtureError("Hunyuan OCR sampling receipt is invalid")
    documents = manifest.get("documents")
    if not isinstance(documents, list) or len(documents) != 1:
        raise FixtureError("Hunyuan OCR document receipt is invalid")
    document = _object(documents[0], "Hunyuan OCR document receipt")
    if (
        set(document)
        != {
            "characters",
            "early_stopped_tail_repetition",
            "input",
            "output",
        }
        or document.get("input") != "digit7.png"
        or document.get("output") != "documents/001-digit7.md"
    ):
        raise FixtureError("Hunyuan OCR document receipt shape is invalid")
    if not isinstance(document.get("early_stopped_tail_repetition"), bool):
        raise FixtureError("Hunyuan OCR early-stop receipt is invalid")
    try:
        markdown = payloads["documents/001-digit7.md"].decode("utf-8")
    except UnicodeDecodeError as error:
        raise FixtureError("Hunyuan OCR Markdown is not UTF-8") from error
    characters = document.get("characters")
    if (
        not isinstance(characters, int)
        or isinstance(characters, bool)
        or characters != len(markdown)
        or re.search(r"(?<!\d)7(?!\d)", markdown) is None
    ):
        raise FixtureError("Hunyuan OCR Markdown semantic assertion failed")


def _validate_moss_transcript(content: bytes, expected_frame_count: int = 1) -> None:
    lines = content.splitlines()
    if not 3 <= len(lines) <= 4_096 or any(not line for line in lines):
        raise FixtureError("MOSS transcript record count is invalid")
    records = [_parse_json_object(line, "MOSS transcript record") for line in lines]
    previous_elapsed = -1.0
    allowed_shapes = {
        "session-start": {"sequence", "elapsed_seconds", "type", "model_revision"},
        "frame-ack": {
            "sequence",
            "elapsed_seconds",
            "type",
            "event_index",
            "timestamp",
            "dropped_oldest",
        },
        "output": {"sequence", "elapsed_seconds", "type", "kind", "text"},
        "session-stop": {"sequence", "elapsed_seconds", "type"},
    }
    for sequence, record in enumerate(records):
        record_type = record.get("type")
        if (
            record_type not in allowed_shapes
            or set(record) != allowed_shapes[record_type]
        ):
            raise FixtureError("MOSS transcript record shape is invalid")
        elapsed = record.get("elapsed_seconds")
        if (
            record.get("sequence") != sequence
            or not isinstance(elapsed, (int, float))
            or isinstance(elapsed, bool)
            or not 0 <= float(elapsed) <= 3_600
            or float(elapsed) < previous_elapsed
        ):
            raise FixtureError("MOSS transcript ordering is invalid")
        previous_elapsed = float(elapsed)
        if record_type == "output" and (
            record.get("kind")
            not in {"silence", "round-start", "round-end", "response-start", "text"}
            or not isinstance(record.get("text"), str)
            or len(str(record.get("text"))) > 65_536
        ):
            raise FixtureError("MOSS transcript output record is invalid")
    if (
        records[0].get("type") != "session-start"
        or records[0].get("model_revision")
        != "06b067617677661194cf837970fe3a10f1a0e56d"
    ):
        raise FixtureError("MOSS transcript model authority is invalid")
    frame_records = [record for record in records if record.get("type") == "frame-ack"]
    if len(frame_records) != expected_frame_count or any(
        record.get("event_index") != index
        or record.get("timestamp") != float(index)
        or not isinstance(record.get("dropped_oldest"), bool)
        for index, record in enumerate(frame_records)
    ):
        raise FixtureError("MOSS transcript frame acknowledgement is invalid")
    stop_indexes = [
        index
        for index, record in enumerate(records)
        if record.get("type") == "session-stop"
    ]
    if (
        len(stop_indexes) != 1
        or stop_indexes[0] <= int(frame_records[0]["sequence"])
        or any(
            record.get("type") != "output" for record in records[stop_indexes[0] + 1 :]
        )
    ):
        raise FixtureError("MOSS transcript terminal ordering is invalid")


def _validate_ltx25_receipt(
    content: bytes,
    output_contents: Mapping[str, bytes],
    expected_profile: str,
) -> None:
    document = _parse_json_object(content, "LTX 2.5 receipt")
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    if content != canonical + b"\n":
        raise FixtureError("LTX 2.5 receipt is not canonical JSON")
    if set(document) != {
        "media",
        "output_sha256",
        "profile",
        "prompt_sha256",
        "runtime",
        "seed",
        "tensors",
    }:
        raise FixtureError("LTX 2.5 receipt top-level shape is invalid")
    output = output_contents.get("ltx-2.5.mp4")
    if (
        output is None
        or document.get("output_sha256") != hashlib.sha256(output).hexdigest()
    ):
        raise FixtureError("LTX 2.5 receipt output digest is invalid")
    prompt_digest = document.get("prompt_sha256")
    if not isinstance(prompt_digest, str) or not _DIGEST.fullmatch(prompt_digest):
        raise FixtureError("LTX 2.5 receipt prompt digest is invalid")
    if document.get("profile") not in {
        "bf16-model-offload",
        "fp8-cast-model-offload",
        "fp8-cast-sequential-offload",
    }:
        raise FixtureError("LTX 2.5 receipt profile is invalid")
    if document.get("profile") != expected_profile:
        raise FixtureError("LTX 2.5 receipt does not match the requested profile")
    seed = document.get("seed")
    if (
        not isinstance(seed, int)
        or isinstance(seed, bool)
        or not 0 <= seed <= 9_223_372_036_854_775_807
    ):
        raise FixtureError("LTX 2.5 receipt seed is invalid")
    media = _object(document.get("media"), "LTX 2.5 receipt media")
    expected_media = {
        "audio_channels": 2,
        "audio_codec": "aac",
        "audio_sample_rate": 48000,
        "duration_seconds": 2.7083333333333335,
        "fps": 24,
        "frames": 65,
        "height": 512,
        "video_codec": "h264",
        "width": 768,
    }
    if set(media) != {*expected_media, "audio_samples"} or any(
        media.get(key) != value for key, value in expected_media.items()
    ):
        raise FixtureError("LTX 2.5 receipt media shape is invalid")
    audio_samples = media.get("audio_samples")
    if (
        not isinstance(audio_samples, int)
        or isinstance(audio_samples, bool)
        or audio_samples < 1
    ):
        raise FixtureError("LTX 2.5 receipt audio sample count is invalid")
    runtime = _object(document.get("runtime"), "LTX 2.5 receipt runtime")
    if set(runtime) != {
        "cuda",
        "cudnn",
        "diffusers_revision",
        "model_revision",
        "torch",
    }:
        raise FixtureError("LTX 2.5 receipt runtime shape is invalid")
    if (
        runtime.get("diffusers_revision") != "d035dcd7cc7c88e0a154609b62887d50bba9fdc2"
        or runtime.get("model_revision") != "426936f8b22dc28e4def61e515478b0b7e4a53cc"
    ):
        raise FixtureError("LTX 2.5 receipt runtime revision is invalid")
    if runtime.get("cuda") is not None and not isinstance(runtime.get("cuda"), str):
        raise FixtureError("LTX 2.5 receipt CUDA version is invalid")
    cudnn = runtime.get("cudnn")
    if cudnn is not None and (not isinstance(cudnn, int) or isinstance(cudnn, bool)):
        raise FixtureError("LTX 2.5 receipt cuDNN version is invalid")
    if not isinstance(runtime.get("torch"), str) or not runtime.get("torch"):
        raise FixtureError("LTX 2.5 receipt Torch version is invalid")
    tensors = _object(document.get("tensors"), "LTX 2.5 receipt tensors")
    if set(tensors) != {"audio", "video"}:
        raise FixtureError("LTX 2.5 receipt tensor shape is invalid")
    for name in ("audio", "video"):
        tensor = _object(tensors.get(name), f"LTX 2.5 {name} tensor")
        shape = tensor.get("shape")
        digest = tensor.get("sha256")
        if (
            set(tensor) != {"dtype", "shape", "sha256"}
            or not isinstance(tensor.get("dtype"), str)
            or not tensor.get("dtype")
            or not isinstance(shape, list)
            or not shape
            or any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in shape
            )
            or not isinstance(digest, str)
            or not _DIGEST.fullmatch(digest)
        ):
            raise FixtureError(f"LTX 2.5 {name} tensor metadata is invalid")
    video = _object(tensors["video"], "LTX 2.5 video tensor")
    if video.get("shape") != [65, 512, 768, 3]:
        raise FixtureError("LTX 2.5 video tensor shape is invalid")


def _fixture_format(media_type: str) -> str | None:
    return {
        "application/json": "json",
        "audio/wav": "wav",
        "image/jpeg": "jpeg",
        "image/png": "png",
        "model/gltf-binary": "glb",
        "video/mp4": "mp4",
    }.get(media_type)


class FixtureRegistry:
    def __init__(
        self,
        fixtures: Mapping[str, Fixture],
        recipes: Mapping[str, RecipeFixture],
        special: Mapping[str, Mapping[str, object]],
        *,
        manifest_sha256: str,
        service_cases: Mapping[str, ServiceCase] | None = None,
        service_recipes: Mapping[str, ServiceRecipe] | None = None,
    ) -> None:
        self.fixtures = dict(fixtures)
        self.recipes = dict(recipes)
        self.special = {key: dict(value) for key, value in special.items()}
        self.service_cases = dict(service_cases or {})
        self.service_recipes = dict(service_recipes or {})
        self.manifest_sha256 = manifest_sha256

    @classmethod
    def packaged(cls, path: Path | None = None) -> FixtureRegistry:
        package_root = resources.files("cluster_profiles").joinpath("resources")
        try:
            raw = (
                path.read_bytes()
                if path is not None
                else package_root.joinpath("qualification-fixtures.json").read_bytes()
            )
            value = _strict_json_loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise FixtureError(
                "qualification fixture manifest is unreadable"
            ) from error
        document = _object(value, "qualification fixture manifest")
        if document.get("schema_version") not in {1, 2}:
            raise FixtureError("qualification fixture schema_version must be 1 or 2")
        fixture_root = path.parent if path is not None else package_root
        raw_fixtures = _object(document.get("fixtures"), "fixtures")
        fixtures: dict[str, Fixture] = {}
        for fixture_id, raw_fixture in raw_fixtures.items():
            if not isinstance(fixture_id, str) or not _NAME.fullmatch(fixture_id):
                raise FixtureError("fixture ID is invalid")
            item = _object(raw_fixture, f"fixture {fixture_id}")
            if set(item) - {
                "path",
                "encoding",
                "name",
                "media_type",
                "size_bytes",
                "sha256",
                "provenance",
            }:
                raise FixtureError(f"fixture {fixture_id} fields are invalid")
            content = _load_content(fixture_root, item)  # type: ignore[arg-type]
            name = item.get("name")
            media_type = item.get("media_type")
            digest = item.get("sha256")
            size = item.get("size_bytes")
            if not isinstance(name, str) or not _NAME.fullmatch(name):
                raise FixtureError(f"fixture {fixture_id} name is invalid")
            if not isinstance(media_type, str) or not _MEDIA_TYPE.fullmatch(media_type):
                raise FixtureError(f"fixture {fixture_id} media type is invalid")
            if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
                raise FixtureError(f"fixture {fixture_id} digest is invalid")
            if size != len(content) or hashlib.sha256(content).hexdigest() != digest:
                raise FixtureError(f"fixture {fixture_id} identity does not match")
            format_name = _fixture_format(media_type)
            if format_name is not None:
                _validate_magic(content, format_name)
            provenance_value = item.get("provenance")
            if provenance_value is None:
                raise FixtureError(f"fixture {fixture_id} provenance is required")
            provenance: dict[str, str] | None = None
            if provenance_value is not None:
                source = _object(provenance_value, f"fixture {fixture_id} provenance")
                if set(source) != {
                    "origin",
                    "source_url",
                    "source_revision",
                    "license_spdx",
                    "attribution",
                }:
                    raise FixtureError(
                        f"fixture {fixture_id} provenance shape is invalid"
                    )
                if source.get("origin") not in {"generated", "upstream"}:
                    raise FixtureError(
                        f"fixture {fixture_id} provenance origin is invalid"
                    )
                source_url = source.get("source_url")
                source_revision = source.get("source_revision")
                license_spdx = source.get("license_spdx")
                attribution = source.get("attribution")
                if (
                    not isinstance(source_url, str)
                    or not (
                        source_url.startswith("https://")
                        or source_url == "urn:vonk:qualification-fixture"
                    )
                    or not isinstance(source_revision, str)
                    or not _DIGEST.fullmatch(source_revision)
                    and re.fullmatch(r"[0-9a-f]{40}", source_revision) is None
                    or license_spdx not in {"CC-BY-4.0", "CC0-1.0"}
                    or not isinstance(attribution, str)
                    or not 1 <= len(attribution) <= 256
                ):
                    raise FixtureError(f"fixture {fixture_id} provenance is invalid")
                provenance = {str(key): str(value) for key, value in source.items()}
            fixtures[fixture_id] = Fixture(
                fixture_id,
                str(item["path"]),
                str(item["encoding"]),
                name,
                media_type,
                len(content),
                digest,
                content,
                provenance,
            )
        raw_recipes = _object(document.get("recipes"), "recipes")
        if document.get("schema_version") == 1 and any(
            isinstance(raw_recipe, Mapping) and "cases" in raw_recipe
            for raw_recipe in raw_recipes.values()
        ):
            raise FixtureError("qualification fixture cases require schema_version 2")
        recipes = {
            key: _parse_recipe_fixture(key, raw_recipe, fixtures)
            for key, raw_recipe in raw_recipes.items()
        }
        raw_special = _object(document.get("special_fixtures", {}), "special_fixtures")
        special: dict[str, Mapping[str, object]] = {}
        for key, raw_special_item in raw_special.items():
            if not isinstance(key, str) or not _KEY.fullmatch(key):
                raise FixtureError("special fixture recipe key is invalid")
            item = _object(raw_special_item, f"special fixture {key}")
            content_sha256 = item.get("content_sha256")
            detail = item.get("detail")
            if (
                not isinstance(content_sha256, str)
                or not _DIGEST.fullmatch(content_sha256)
                or not isinstance(detail, str)
                or not 1 <= len(detail) <= 512
            ):
                raise FixtureError(f"special fixture {key} is invalid")
            special[key] = item
        if set(recipes) & set(special):
            raise FixtureError(
                "recipe cannot have both executable and special fixtures"
            )
        raw_service_cases = _object(
            document.get("service_case_templates", {}), "service_case_templates"
        )
        service_cases = {
            key: _parse_service_case(key, item)
            for key, item in raw_service_cases.items()
        }
        raw_service_recipes = _object(
            document.get("service_recipes", {}), "service_recipes"
        )
        service_recipes = {
            key: _parse_service_recipe(key, item, service_cases)
            for key, item in raw_service_recipes.items()
        }
        if (set(service_recipes) & set(recipes)) or (
            set(service_recipes) & set(special)
        ):
            raise FixtureError("recipe cannot have both artifact and service fixtures")

        used_fixtures = {
            fixture.fixture_id
            for recipe in recipes.values()
            for case in recipe.all_cases
            for _, fixture in case.inputs
        }

        def collect_service_fixture_references(value: object) -> None:
            if isinstance(value, Mapping):
                for field, child in value.items():
                    if field in {"$fixture_data_uri", "$fixture_base64"}:
                        if isinstance(child, str):
                            used_fixtures.add(child)
                    else:
                        collect_service_fixture_references(child)
            elif isinstance(value, list | tuple):
                for child in value:
                    collect_service_fixture_references(child)

        for recipe in service_recipes.values():
            for case in recipe.cases:
                collect_service_fixture_references(case.body)
                collect_service_fixture_references(case.assertions)
        unused_fixtures = sorted(set(fixtures) - used_fixtures)
        if unused_fixtures:
            raise FixtureError(
                "qualification fixture manifest declares unused fixtures: "
                + ", ".join(unused_fixtures)
            )
        return cls(
            fixtures,
            recipes,
            special,
            manifest_sha256=hashlib.sha256(raw).hexdigest(),
            service_cases=service_cases,
            service_recipes=service_recipes,
        )

    def resolve(
        self, key: str, content_sha256: str, interface: str
    ) -> tuple[RecipeFixture | None, dict[str, str] | None]:
        special = self.special.get(key)
        if special is not None:
            if special.get("content_sha256") != content_sha256:
                return None, _blocker(
                    "fixture.recipe_digest_mismatch",
                    "The recipe changed after its special-fixture classification.",
                )
            return None, _blocker(
                str(special.get("code") or "fixture.special_required"),
                str(special["detail"]),
            )
        recipe = self.recipes.get(key)
        if recipe is None:
            return None, _blocker(
                "fixture.missing",
                "No reviewed qualification fixture is bound to this artifact recipe.",
            )
        if recipe.content_sha256 != content_sha256:
            return None, _blocker(
                "fixture.recipe_digest_mismatch",
                "The recipe changed after its qualification fixture was reviewed.",
            )
        if recipe.interface != interface:
            return None, _blocker(
                "fixture.interface_mismatch",
                "The recipe interface changed after its qualification fixture was reviewed.",
            )
        return recipe, None

    def resolve_service(
        self, key: str, content_sha256: str
    ) -> tuple[ServiceRecipe | None, dict[str, str] | None]:
        recipe = self.service_recipes.get(key)
        if recipe is None:
            return None, _blocker(
                "service_fixture.missing",
                "No reviewed digest-bound service smoke is available for this recipe.",
            )
        if recipe.content_sha256 != content_sha256:
            return None, _blocker(
                "service_fixture.recipe_digest_mismatch",
                "The recipe changed after its service smoke was reviewed.",
            )
        return recipe, None


def _blocker(code: str, detail: str) -> dict[str, str]:
    return {"classification": "fixture", "code": code, "detail": detail}


def _parse_recipe_fixture(
    key: object, raw: object, fixtures: Mapping[str, Fixture]
) -> RecipeFixture:
    if not isinstance(key, str) or not _KEY.fullmatch(key):
        raise FixtureError("recipe fixture key is invalid")
    item = _object(raw, f"recipe fixture {key}")
    allowed = {
        "content_sha256",
        "interface",
        "parameters",
        "inputs",
        "output_limits",
        "timeout_seconds",
        "assertions",
        "cases",
    }
    if set(item) - allowed:
        raise FixtureError(f"recipe fixture {key} fields are invalid")
    base = {name: value for name, value in item.items() if name != "cases"}
    primary = _parse_recipe_case(key, base, fixtures)
    raw_cases = item.get("cases", [])
    if not isinstance(raw_cases, list) or len(raw_cases) > 16:
        raise FixtureError(f"recipe fixture {key} cases are invalid")
    case_fields = {
        "id",
        "parameters",
        "inputs",
        "output_limits",
        "timeout_seconds",
        "assertions",
    }
    parsed_cases: list[RecipeFixture] = []
    case_ids = {primary.case_id}
    for raw_case in raw_cases:
        case = _object(raw_case, f"recipe fixture {key} case")
        case_id = case.get("id")
        if (
            not set(case).issubset(case_fields)
            or len(case) < 2
            or not isinstance(case_id, str)
            or not _CASE_ID.fullmatch(case_id)
            or case_id in case_ids
        ):
            raise FixtureError(f"recipe fixture {key} case identity is invalid")
        parsed = _parse_recipe_case(
            key,
            {**base, **{name: value for name, value in case.items() if name != "id"}},
            fixtures,
        )
        parsed_cases.append(replace(parsed, case_id=case_id))
        case_ids.add(case_id)
    return replace(primary, supplemental_cases=tuple(parsed_cases))


def _parse_recipe_case(
    key: object, raw: object, fixtures: Mapping[str, Fixture]
) -> RecipeFixture:
    if not isinstance(key, str) or not _KEY.fullmatch(key):
        raise FixtureError("recipe fixture key is invalid")
    item = _object(raw, f"recipe fixture {key}")
    digest = item.get("content_sha256")
    interface = item.get("interface")
    parameters = item.get("parameters")
    inputs = item.get("inputs")
    limits = _object(item.get("output_limits"), f"recipe fixture {key} output_limits")
    timeout = item.get("timeout_seconds")
    assertions = item.get("assertions")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise FixtureError(f"recipe fixture {key} digest is invalid")
    if interface not in _INTERFACES:
        raise FixtureError(f"recipe fixture {key} interface is invalid")
    if (
        not isinstance(parameters, dict)
        or len(json.dumps(parameters).encode()) > 16_384
    ):
        raise FixtureError(f"recipe fixture {key} parameters are invalid")
    if not isinstance(inputs, list) or len(inputs) > 32:
        raise FixtureError(f"recipe fixture {key} inputs are invalid")
    parsed_inputs: list[tuple[str, Fixture]] = []
    names: set[str] = set()
    for raw_input in inputs:
        input_item = _object(raw_input, f"recipe fixture {key} input")
        slot = input_item.get("slot")
        fixture_id = input_item.get("fixture")
        if not isinstance(slot, str) or not _SLOT.fullmatch(slot):
            raise FixtureError(f"recipe fixture {key} input slot is invalid")
        if not isinstance(fixture_id, str) or fixture_id not in fixtures:
            raise FixtureError(f"recipe fixture {key} references an unknown fixture")
        fixture = fixtures[fixture_id]
        if fixture.name in names:
            raise FixtureError(f"recipe fixture {key} input names are not unique")
        names.add(fixture.name)
        parsed_inputs.append((slot, fixture))
    output_limits = {
        "max_files": _integer(limits.get("max_files"), "max_files", 1, 32),
        "max_file_bytes": _integer(
            limits.get("max_file_bytes"), "max_file_bytes", 1, 1024**3
        ),
        "max_total_bytes": _integer(
            limits.get("max_total_bytes"), "max_total_bytes", 1, 2 * 1024**3
        ),
        "allowed_media_types": limits.get("allowed_media_types"),
    }
    allowed = output_limits["allowed_media_types"]
    if (
        not isinstance(allowed, list)
        or not 1 <= len(allowed) <= 16
        or len(set(allowed)) != len(allowed)
        or any(
            not isinstance(value, str) or not _MEDIA_TYPE.fullmatch(value)
            for value in allowed
        )
        or output_limits["max_file_bytes"] > output_limits["max_total_bytes"]
    ):
        raise FixtureError(f"recipe fixture {key} output media types are invalid")
    timeout_seconds = _integer(timeout, "timeout_seconds", 1, 3_600)
    if not isinstance(assertions, list) or not assertions:
        raise FixtureError(f"recipe fixture {key} assertions are required")
    parsed_assertions = tuple(
        _parse_assertion(key, assertion) for assertion in assertions
    )
    assertion_identities = [
        (str(assertion["kind"]), str(assertion.get("media_type") or ""))
        for assertion in parsed_assertions
    ]
    if len(assertion_identities) != len(set(assertion_identities)):
        raise FixtureError(f"recipe fixture {key} has duplicate assertions")
    semantic_kind = {
        "application/json": {"json-document", "semantic-receipt"},
        "application/x-ndjson": {"jsonl-records", "moss-transcript"},
        "application/zip": {"ocr-zip", "zip-entries"},
        "audio/wav": {"audio-metadata"},
        "image/png": {"image-metadata"},
        "model/gltf-binary": {"glb-structure"},
        "video/mp4": {"video-metadata"},
    }
    for media_type in allowed:
        required_kinds = semantic_kind.get(str(media_type))
        if required_kinds is None:
            raise FixtureError(
                f"recipe fixture {key} has no semantic validator for {media_type}"
            )
        if not any(
            assertion["kind"] in required_kinds
            and (
                assertion.get("media_type") == media_type
                or len(allowed) == 1
                and assertion.get("media_type") is None
            )
            for assertion in parsed_assertions
        ):
            raise FixtureError(
                f"recipe fixture {key} lacks semantic coverage for {media_type}"
            )
    return RecipeFixture(
        key,
        digest,
        str(interface),
        dict(parameters),
        tuple(parsed_inputs),
        output_limits,
        timeout_seconds,
        parsed_assertions,
    )


def _parse_assertion(key: str, raw: object) -> dict[str, object]:
    assertion = dict(_object(raw, f"recipe fixture {key} assertion"))
    kind = assertion.get("kind")
    allowed_fields = {
        "file-count": {"kind", "exact"},
        "file-names": {"kind", "exact"},
        "media-type": {"kind", "allowed"},
        "minimum-bytes": {"kind", "value"},
        "format": {"kind", "format"},
        "media-type-counts": {"kind", "counts"},
        "image-metadata": {
            "kind",
            "media_type",
            "width",
            "height",
            "bit_depth",
            "color_type",
        },
        "audio-metadata": {
            "kind",
            "media_type",
            "channels",
            "sample_rate",
            "sample_width_bytes",
            "minimum_duration_seconds",
            "maximum_duration_seconds",
        },
        "video-metadata": {
            "kind",
            "media_type",
            "width",
            "height",
            "fps",
            "fps_tolerance",
            "frame_count",
            "codec",
            "pixel_format",
            "audio_streams",
            "stream_count",
            "audio_codec",
            "audio_sample_rate",
            "audio_channels",
            "minimum_audio_duration_seconds",
            "maximum_audio_duration_seconds",
            "maximum_av_sync_delta_seconds",
            "minimum_container_duration_seconds",
            "maximum_container_duration_seconds",
            "minimum_duration_seconds",
            "maximum_duration_seconds",
        },
        "glb-structure": {
            "kind",
            "media_type",
            "profile",
            "minimum_meshes",
            "minimum_primitives",
        },
        "zip-entries": {
            "kind",
            "media_type",
            "exact_names",
            "allowed_suffixes",
            "minimum_entries",
            "nonempty",
        },
        "ocr-zip": {"kind", "media_type", "profile"},
        "moss-transcript": {"kind", "media_type", "profile", "frame_count"},
        "json-document": {
            "kind",
            "media_type",
            "required_keys",
            "equals",
        },
        "jsonl-records": {
            "kind",
            "media_type",
            "required_keys",
            "equals",
            "minimum_records",
        },
        "semantic-receipt": {"kind", "media_type", "profile"},
    }
    if kind not in allowed_fields or set(assertion) - allowed_fields[kind]:
        raise FixtureError(f"recipe fixture {key} assertion fields are invalid")
    media_type = assertion.get("media_type")
    if media_type is not None and (
        not isinstance(media_type, str) or not _MEDIA_TYPE.fullmatch(media_type)
    ):
        raise FixtureError(f"recipe fixture {key} assertion media type is invalid")
    if kind == "file-count":
        _integer(assertion.get("exact"), "assertion exact", 1, 32)
    elif kind == "file-names":
        names = assertion.get("exact")
        if (
            not isinstance(names, list)
            or not names
            or len(names) != len(set(names))
            or any(
                not isinstance(name, str) or not _NAME.fullmatch(name) for name in names
            )
        ):
            raise FixtureError(f"recipe fixture {key} file names are invalid")
    elif kind == "media-type":
        allowed = assertion.get("allowed")
        if (
            not isinstance(allowed, list)
            or not allowed
            or any(
                not isinstance(item, str) or not _MEDIA_TYPE.fullmatch(item)
                for item in allowed
            )
        ):
            raise FixtureError(f"recipe fixture {key} media assertion is invalid")
    elif kind == "minimum-bytes":
        _integer(assertion.get("value"), "assertion minimum bytes", 1, 2 * 1024**3)
    elif kind == "format":
        if assertion.get("format") not in _FORMATS:
            raise FixtureError(f"recipe fixture {key} format assertion is invalid")
    elif kind == "media-type-counts":
        counts = _object(assertion.get("counts"), "media type counts")
        if not counts or any(
            not isinstance(name, str)
            or not _MEDIA_TYPE.fullmatch(name)
            or not isinstance(count, int)
            or isinstance(count, bool)
            or not 0 <= count <= 32
            for name, count in counts.items()
        ):
            raise FixtureError(f"recipe fixture {key} media counts are invalid")
    elif kind == "image-metadata":
        _integer(assertion.get("width"), "image width", 1, 32_768)
        _integer(assertion.get("height"), "image height", 1, 32_768)
        if "bit_depth" in assertion:
            _integer(assertion.get("bit_depth"), "image bit depth", 1, 16)
        if "color_type" in assertion:
            _integer(assertion.get("color_type"), "image color type", 0, 6)
    elif kind == "audio-metadata":
        _integer(assertion.get("channels"), "audio channels", 1, 32)
        _integer(assertion.get("sample_rate"), "audio sample rate", 1, 384_000)
        if "sample_width_bytes" in assertion:
            _integer(assertion.get("sample_width_bytes"), "audio sample width", 1, 8)
        _number_range(assertion, key, "duration_seconds")
    elif kind == "video-metadata":
        _integer(assertion.get("width"), "video width", 1, 32_768)
        _integer(assertion.get("height"), "video height", 1, 32_768)
        _positive_number(assertion.get("fps"), "video fps")
        _positive_number(assertion.get("fps_tolerance", 0.01), "video fps tolerance")
        if "frame_count" in assertion:
            _integer(assertion.get("frame_count"), "video frame count", 1, 1_000_000)
        if "codec" in assertion and (
            not isinstance(assertion.get("codec"), str) or not assertion.get("codec")
        ):
            raise FixtureError(f"recipe fixture {key} video codec is invalid")
        if "pixel_format" in assertion and (
            not isinstance(assertion.get("pixel_format"), str)
            or not assertion.get("pixel_format")
        ):
            raise FixtureError(f"recipe fixture {key} pixel format is invalid")
        if "audio_streams" in assertion:
            _integer(assertion.get("audio_streams"), "audio streams", 0, 32)
        if "stream_count" in assertion:
            _integer(assertion.get("stream_count"), "stream count", 1, 32)
        if "audio_codec" in assertion and (
            not isinstance(assertion.get("audio_codec"), str)
            or not assertion.get("audio_codec")
        ):
            raise FixtureError(f"recipe fixture {key} audio codec is invalid")
        if "audio_sample_rate" in assertion:
            _integer(
                assertion.get("audio_sample_rate"),
                "audio sample rate",
                1,
                384_000,
            )
            _number_range(assertion, key, "audio_duration_seconds")
        if "audio_channels" in assertion:
            _integer(assertion.get("audio_channels"), "audio channels", 1, 32)
        if "maximum_av_sync_delta_seconds" in assertion:
            _positive_number(
                assertion.get("maximum_av_sync_delta_seconds"),
                "maximum AV sync delta",
            )
        _number_range(assertion, key, "container_duration_seconds")
        _number_range(assertion, key, "duration_seconds")
    elif kind == "glb-structure":
        if assertion.get("profile", "triangle-mesh") not in {
            "triangle-mesh",
            "textured",
            "textured-pbr",
            "skinned",
        }:
            raise FixtureError(f"recipe fixture {key} GLB profile is invalid")
        _integer(assertion.get("minimum_meshes", 1), "minimum meshes", 1, 1_000_000)
        _integer(
            assertion.get("minimum_primitives", 1),
            "minimum primitives",
            1,
            1_000_000,
        )
    elif kind == "zip-entries":
        names = assertion.get("exact_names")
        suffixes = assertion.get("allowed_suffixes")
        if names is not None and (
            not isinstance(names, list)
            or not names
            or any(
                not isinstance(name, str) or not _NAME.fullmatch(name) for name in names
            )
        ):
            raise FixtureError(f"recipe fixture {key} ZIP names are invalid")
        if suffixes is not None and (
            not isinstance(suffixes, list)
            or not suffixes
            or any(
                not isinstance(value, str) or not value.startswith(".")
                for value in suffixes
            )
        ):
            raise FixtureError(f"recipe fixture {key} ZIP suffixes are invalid")
        _integer(assertion.get("minimum_entries", 1), "minimum ZIP entries", 1, 10_000)
    elif kind == "ocr-zip":
        if assertion.get("profile") != "hunyuanocr-digit7-v1":
            raise FixtureError(f"recipe fixture {key} OCR ZIP profile is invalid")
    elif kind == "moss-transcript":
        if assertion.get("profile") != "moss-realtime-v1":
            raise FixtureError(f"recipe fixture {key} MOSS profile is invalid")
        _integer(assertion.get("frame_count", 1), "MOSS frame count", 1, 128)
    elif kind in {"json-document", "jsonl-records"}:
        required = assertion.get("required_keys", [])
        if not isinstance(required, list) or any(
            not isinstance(name, str) or not name for name in required
        ):
            raise FixtureError(f"recipe fixture {key} JSON keys are invalid")
        if kind == "jsonl-records":
            _integer(
                assertion.get("minimum_records", 1),
                "minimum JSONL records",
                1,
                1_000_000,
            )
        equals = assertion.get("equals")
        if equals is not None:
            try:
                equals_size = len(json.dumps(equals, allow_nan=False).encode())
            except (TypeError, ValueError) as error:
                raise FixtureError(
                    f"recipe fixture {key} JSON equals is invalid"
                ) from error
            if not isinstance(equals, Mapping) or equals_size > 64 * 1024:
                raise FixtureError(f"recipe fixture {key} JSON equals is invalid")
    elif kind == "semantic-receipt":
        if assertion.get("profile") not in {
            "ltx-2.5",
            "fp8-cast-sequential-offload",
        }:
            raise FixtureError(f"recipe fixture {key} receipt profile is invalid")
    else:
        raise FixtureError(f"recipe fixture {key} assertion kind is invalid")
    return assertion


def _positive_number(value: object, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise FixtureError(f"{label} is invalid")
    return float(value)


def _number_range(assertion: Mapping[str, object], key: str, label: str) -> None:
    minimum = assertion.get(f"minimum_{label}")
    maximum = assertion.get(f"maximum_{label}")
    if minimum is None and maximum is None:
        return
    low = _positive_number(minimum, f"minimum {label}")
    high = _positive_number(maximum, f"maximum {label}")
    if low > high:
        raise FixtureError(f"recipe fixture {key} {label} range is invalid")


def _parse_service_case(key: object, raw: object) -> ServiceCase:
    if not isinstance(key, str) or not _NAME.fullmatch(key):
        raise FixtureError("service case ID is invalid")
    item = _object(raw, f"service case {key}")
    method = item.get("method")
    path = item.get("path")
    body = item.get("body")
    assertions = item.get("assertions")
    if method not in {"GET", "POST"}:
        raise FixtureError(f"service case {key} method is invalid")
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or "//" in path
        or "?" in path
        or "#" in path
        or len(path) > 128
    ):
        raise FixtureError(f"service case {key} path is invalid")
    if method == "GET" and body is not None:
        raise FixtureError(f"service case {key} GET body must be null")
    if method == "POST" and not isinstance(body, dict):
        raise FixtureError(f"service case {key} POST body is invalid")
    if len(json.dumps(body, separators=(",", ":")).encode()) > 64 * 1024:
        raise FixtureError(f"service case {key} body is too large")
    if not isinstance(assertions, list) or not assertions or len(assertions) > 32:
        raise FixtureError(f"service case {key} assertions are invalid")
    parsed_assertions: list[dict[str, object]] = []
    allowed_kinds = {
        "array.path-count-equals",
        "path.count",
        "path.empty",
        "path.equals",
        "path.json-equals",
        "path.lte",
        "path.nonempty",
        "path.regex",
        "raw.not-contains",
    }
    for assertion_raw in assertions:
        assertion = dict(_object(assertion_raw, f"service case {key} assertion"))
        kind = assertion.get("kind")
        if kind not in allowed_kinds:
            raise FixtureError(f"service case {key} assertion kind is invalid")
        allowed_fields = {
            "array.path-count-equals": {
                "kind",
                "path",
                "item_path",
                "value",
                "count",
            },
            "path.count": {"kind", "path", "value"},
            "path.empty": {"kind", "path"},
            "path.equals": {"kind", "path", "value"},
            "path.json-equals": {"kind", "path", "value"},
            "path.lte": {"kind", "path", "value"},
            "path.nonempty": {"kind", "path"},
            "path.regex": {"kind", "path", "value"},
            "raw.not-contains": {"kind", "values"},
        }
        if set(assertion) - allowed_fields[str(kind)]:
            raise FixtureError(f"service case {key} assertion fields are invalid")
        path_value = assertion.get("path")
        if kind != "raw.not-contains" and (
            not isinstance(path_value, str) or not path_value or len(path_value) > 256
        ):
            raise FixtureError(f"service case {key} assertion path is invalid")
        if kind == "path.regex":
            pattern = assertion.get("value")
            if not isinstance(pattern, str) or len(pattern) > 512:
                raise FixtureError(f"service case {key} regex is invalid")
            try:
                re.compile(pattern)
            except re.error as error:
                raise FixtureError(f"service case {key} regex is invalid") from error
        if kind == "raw.not-contains":
            values = assertion.get("values")
            if (
                not isinstance(values, list)
                or not values
                or any(not isinstance(value, str) or not value for value in values)
            ):
                raise FixtureError(f"service case {key} raw assertion is invalid")
        parsed_assertions.append(assertion)
    return ServiceCase(
        key,
        str(method),
        path,
        body,
        _integer(item.get("timeout_seconds"), "timeout_seconds", 1, 900),
        _integer(item.get("max_response_bytes"), "max_response_bytes", 1, 1024 * 1024),
        tuple(parsed_assertions),
    )


def _parse_service_recipe(
    key: object, raw: object, cases: Mapping[str, ServiceCase]
) -> ServiceRecipe:
    if not isinstance(key, str) or not _KEY.fullmatch(key):
        raise FixtureError("service recipe key is invalid")
    item = _object(raw, f"service recipe {key}")
    digest = item.get("content_sha256")
    alias = item.get("alias")
    smoke_cases = item.get("smoke_cases")
    higher = _object(item.get("higher_tiers", {}), f"service recipe {key} tiers")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise FixtureError(f"service recipe {key} digest is invalid")
    if not isinstance(alias, str) or not _NAME.fullmatch(alias):
        raise FixtureError(f"service recipe {key} alias is invalid")
    if (
        not isinstance(smoke_cases, list)
        or not smoke_cases
        or len(smoke_cases) != len(set(smoke_cases))
        or any(not isinstance(case, str) or case not in cases for case in smoke_cases)
    ):
        raise FixtureError(f"service recipe {key} smoke cases are invalid")
    higher_tiers: dict[str, tuple[str, ...]] = {}
    for tier, descriptions in higher.items():
        if tier not in {"stress", "recovery"} or not isinstance(descriptions, list):
            raise FixtureError(f"service recipe {key} higher tier is invalid")
        if any(
            not isinstance(description, str) or not 1 <= len(description) <= 512
            for description in descriptions
        ):
            raise FixtureError(f"service recipe {key} tier description is invalid")
        higher_tiers[str(tier)] = tuple(descriptions)
    return ServiceRecipe(
        key,
        digest,
        alias,
        tuple(cases[str(case)] for case in smoke_cases),
        higher_tiers,
    )


def validate_outputs(
    recipe: RecipeFixture,
    result: Mapping[str, object],
    client: ArtifactTransferClient,
) -> dict[str, object]:
    raw_outputs = result.get("output_files")
    if not isinstance(raw_outputs, list) or not raw_outputs:
        raise FixtureError("artifact job produced no output files")
    outputs: list[dict[str, object]] = []
    for raw in raw_outputs:
        item = _object(raw, "artifact output")
        name = item.get("name")
        media_type = item.get("media_type")
        size = item.get("size_bytes")
        digest = item.get("sha256")
        if (
            not isinstance(name, str)
            or not _NAME.fullmatch(name)
            or not isinstance(media_type, str)
            or not _MEDIA_TYPE.fullmatch(media_type)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 <= size <= 1024**3
            or not isinstance(digest, str)
            or not _DIGEST.fullmatch(digest)
        ):
            raise FixtureError("artifact output metadata is invalid")
        outputs.append(dict(item))
    if len(outputs) > int(recipe.output_limits["max_files"]) or len(
        {str(item["name"]) for item in outputs}
    ) != len(outputs):
        raise FixtureError("artifact output file identity exceeds its contract")
    if any(
        int(item["size_bytes"]) > int(recipe.output_limits["max_file_bytes"])
        or item["media_type"] not in recipe.output_limits["allowed_media_types"]
        for item in outputs
    ) or sum(int(item["size_bytes"]) for item in outputs) > int(
        recipe.output_limits["max_total_bytes"]
    ):
        raise FixtureError("artifact output media-type or size exceeds its limits")
    with tempfile.TemporaryDirectory(prefix="vonk-qualification-results-") as root:
        contents: list[tuple[dict[str, object], bytes, Path]] = []
        for index, item in enumerate(outputs):
            destination = Path(root) / f"{index:02d}-{item['name']}"
            client.download_file(
                f"/api/v1/artifact-jobs/{result['id']}/results/{item['sha256']}",
                destination,
                media_type=str(item["media_type"]),
                expected_sha256=str(item["sha256"]),
                expected_size=int(item["size_bytes"]),
                overwrite=False,
            )
            content = destination.read_bytes()
            if (
                len(content) != item["size_bytes"]
                or hashlib.sha256(content).hexdigest() != item["sha256"]
            ):
                raise FixtureError("downloaded artifact output digest changed")
            contents.append((item, content, destination))
        for assertion in recipe.assertions:
            kind = assertion["kind"]
            if kind == "file-count" and len(contents) != assertion["exact"]:
                raise FixtureError("artifact output file-count assertion failed")
            if (
                kind == "file-names"
                and [item["name"] for item, _, _ in contents] != assertion["exact"]
            ):
                raise FixtureError("artifact output file-name assertion failed")
            if kind == "media-type" and any(
                item["media_type"] not in assertion["allowed"]
                for item, _, _ in contents
            ):
                raise FixtureError("artifact output media-type assertion failed")
            if kind == "minimum-bytes" and any(
                len(content) < assertion["value"] for _, content, _ in contents
            ):
                raise FixtureError("artifact output minimum-bytes assertion failed")
            if kind == "format":
                for _, content, _ in contents:
                    _validate_magic(content, str(assertion["format"]))
            selected = [
                (item, content, path)
                for item, content, path in contents
                if assertion.get("media_type") is None
                or item["media_type"] == assertion.get("media_type")
            ]
            if (
                kind
                in {
                    "image-metadata",
                    "audio-metadata",
                    "video-metadata",
                    "glb-structure",
                    "zip-entries",
                    "ocr-zip",
                    "moss-transcript",
                    "json-document",
                    "jsonl-records",
                    "semantic-receipt",
                }
                and not selected
            ):
                raise FixtureError("artifact semantic assertion selected no output")
            if kind == "media-type-counts":
                actual = {
                    media_type: sum(
                        item["media_type"] == media_type for item, _, _ in contents
                    )
                    for media_type in assertion["counts"]
                }
                if actual != assertion["counts"]:
                    raise FixtureError("artifact output media counts assertion failed")
            if kind == "image-metadata":
                for _, content, _ in selected:
                    metadata = _png_metadata(content)
                    for field in ("width", "height", "bit_depth", "color_type"):
                        if field in assertion and metadata[field] != assertion[field]:
                            raise FixtureError(f"artifact PNG {field} assertion failed")
            if kind == "audio-metadata":
                for _, content, _ in selected:
                    metadata = _wav_metadata(content)
                    for field in ("channels", "sample_rate", "sample_width_bytes"):
                        if field in assertion and metadata[field] != assertion[field]:
                            raise FixtureError(f"artifact WAV {field} assertion failed")
                    _assert_number_range(metadata, assertion, "duration_seconds", "WAV")
            if kind == "video-metadata":
                for _, _, path in selected:
                    _verify_media_decode(path)
                    metadata = _ffprobe_metadata(path)
                    video = _object(metadata.get("video"), "ffprobe video stream")
                    for field in ("width", "height"):
                        if video.get(field) != assertion[field]:
                            raise FixtureError(f"artifact MP4 {field} assertion failed")
                    if (
                        "codec" in assertion
                        and video.get("codec_name") != assertion["codec"]
                    ):
                        raise FixtureError("artifact MP4 codec assertion failed")
                    if (
                        "pixel_format" in assertion
                        and video.get("pix_fmt") != assertion["pixel_format"]
                    ):
                        raise FixtureError("artifact MP4 pixel-format assertion failed")
                    fps = _parse_fraction(video.get("avg_frame_rate"), "MP4 frame rate")
                    if abs(fps - float(assertion["fps"])) > float(
                        assertion.get("fps_tolerance", 0.01)
                    ):
                        raise FixtureError("artifact MP4 frame-rate assertion failed")
                    if "frame_count" in assertion:
                        try:
                            frame_count = int(str(video.get("nb_read_frames")))
                        except (TypeError, ValueError) as error:
                            raise FixtureError(
                                "artifact MP4 decoded frame count is unavailable"
                            ) from error
                        declared_frames = video.get("nb_frames")
                        if declared_frames not in {None, "N/A"}:
                            try:
                                if int(str(declared_frames)) != frame_count:
                                    raise FixtureError(
                                        "artifact MP4 declared/decoded frame counts differ"
                                    )
                            except (TypeError, ValueError) as error:
                                raise FixtureError(
                                    "artifact MP4 declared frame count is invalid"
                                ) from error
                        if frame_count != assertion["frame_count"]:
                            raise FixtureError(
                                "artifact MP4 frame-count assertion failed"
                            )
                    _assert_number_range(video, assertion, "duration_seconds", "MP4")
                    audio = metadata.get("audio")
                    streams = metadata.get("streams")
                    format_metadata = _object(
                        metadata.get("format"), "ffprobe container metadata"
                    )
                    for timing, label in (
                        (video.get("start_time"), "video"),
                        (format_metadata.get("start_time"), "container"),
                    ):
                        try:
                            start_time = float(timing)
                        except (TypeError, ValueError) as error:
                            raise FixtureError(
                                f"artifact MP4 {label} start time is unavailable"
                            ) from error
                        if abs(start_time) > 0.05:
                            raise FixtureError(
                                f"artifact MP4 {label} start-time assertion failed"
                            )
                    if "stream_count" in assertion and (
                        not isinstance(streams, list)
                        or len(streams) != assertion["stream_count"]
                    ):
                        raise FixtureError("artifact MP4 stream-count assertion failed")
                    if "audio_streams" in assertion and (
                        not isinstance(audio, list)
                        or len(audio) != assertion["audio_streams"]
                    ):
                        raise FixtureError("artifact MP4 audio-stream assertion failed")
                    if "audio_sample_rate" in assertion:
                        if not isinstance(audio, list) or len(audio) != 1:
                            raise FixtureError("artifact MP4 audio metadata is missing")
                        audio_stream = _object(audio[0], "ffprobe audio stream")
                        if (
                            "audio_codec" in assertion
                            and audio_stream.get("codec_name")
                            != assertion["audio_codec"]
                        ):
                            raise FixtureError(
                                "artifact MP4 audio codec assertion failed"
                            )
                        try:
                            sample_rate = int(str(audio_stream.get("sample_rate")))
                        except ValueError as error:
                            raise FixtureError(
                                "artifact MP4 audio sample rate is invalid"
                            ) from error
                        if sample_rate != assertion["audio_sample_rate"]:
                            raise FixtureError(
                                "artifact MP4 audio sample-rate assertion failed"
                            )
                        if (
                            "audio_channels" in assertion
                            and audio_stream.get("channels")
                            != assertion["audio_channels"]
                        ):
                            raise FixtureError(
                                "artifact MP4 audio channel-count assertion failed"
                            )
                        audio_range = {
                            "minimum_duration_seconds": assertion[
                                "minimum_audio_duration_seconds"
                            ],
                            "maximum_duration_seconds": assertion[
                                "maximum_audio_duration_seconds"
                            ],
                        }
                        _assert_number_range(
                            audio_stream,
                            audio_range,
                            "duration_seconds",
                            "MP4 audio",
                        )
                        if "maximum_av_sync_delta_seconds" in assertion:
                            try:
                                video_duration = float(video["duration"])
                                audio_duration = float(audio_stream["duration"])
                            except (KeyError, TypeError, ValueError) as error:
                                raise FixtureError(
                                    "artifact MP4 AV duration is unavailable"
                                ) from error
                            if abs(video_duration - audio_duration) > float(
                                assertion["maximum_av_sync_delta_seconds"]
                            ):
                                raise FixtureError(
                                    "artifact MP4 AV sync assertion failed"
                                )
                    if "minimum_container_duration_seconds" in assertion:
                        _assert_number_range(
                            format_metadata,
                            assertion,
                            "container_duration_seconds",
                            "MP4 container",
                            metadata_field="duration",
                        )
            if kind == "glb-structure":
                for _, content, _ in selected:
                    metadata = _glb_metadata(
                        content, str(assertion.get("profile", "triangle-mesh"))
                    )
                    if metadata["mesh_count"] < assertion.get("minimum_meshes", 1):
                        raise FixtureError("artifact GLB mesh assertion failed")
                    if metadata["primitive_count"] < assertion.get(
                        "minimum_primitives", 1
                    ):
                        raise FixtureError("artifact GLB primitive assertion failed")
            if kind == "zip-entries":
                for _, content, _ in selected:
                    entries = _safe_zip_entries(content)
                    names = [name for name, _ in entries]
                    if "exact_names" in assertion and names != assertion["exact_names"]:
                        raise FixtureError("artifact ZIP file-name assertion failed")
                    if len(entries) < assertion.get("minimum_entries", 1):
                        raise FixtureError("artifact ZIP entry-count assertion failed")
                    suffixes = assertion.get("allowed_suffixes")
                    if isinstance(suffixes, list) and any(
                        not any(name.endswith(suffix) for suffix in suffixes)
                        for name in names
                    ):
                        raise FixtureError("artifact ZIP suffix assertion failed")
                    if assertion.get("nonempty") is True and any(
                        not data for _, data in entries
                    ):
                        raise FixtureError("artifact ZIP empty-entry assertion failed")
            if kind == "ocr-zip":
                for _, content, _ in selected:
                    _validate_hunyuan_ocr_zip(content)
            if kind == "moss-transcript":
                for _, content, _ in selected:
                    _validate_moss_transcript(
                        content, int(assertion.get("frame_count", 1))
                    )
            if kind == "json-document":
                for _, content, _ in selected:
                    document = _parse_json_object(content, "artifact JSON")
                    _assert_required_keys(document, assertion)
            if kind == "jsonl-records":
                for _, content, _ in selected:
                    records = []
                    for line in content.splitlines():
                        if not line.strip():
                            continue
                        records.append(_parse_json_object(line, "artifact JSONL"))
                    if len(records) < assertion.get("minimum_records", 1):
                        raise FixtureError(
                            "artifact JSONL record-count assertion failed"
                        )
                    for record in records:
                        _assert_required_keys(record, assertion)
            if kind == "semantic-receipt":
                output_contents = {
                    str(item["name"]): content for item, content, _ in contents
                }
                request_profiles = []
                for slot, input_fixture in recipe.inputs:
                    if slot != "request":
                        continue
                    request = _parse_json_object(
                        input_fixture.content, "LTX 2.5 qualification request"
                    )
                    request_profiles.append(request.get("profile"))
                declared_profile = str(assertion["profile"])
                if declared_profile == "ltx-2.5":
                    if len(request_profiles) > 1 or any(
                        profile
                        not in {
                            "fp8-cast-model-offload",
                            "fp8-cast-sequential-offload",
                        }
                        for profile in request_profiles
                    ):
                        raise FixtureError("LTX 2.5 qualification profile is invalid")
                    expected_profile = (
                        str(request_profiles[0])
                        if request_profiles
                        else "bf16-model-offload"
                    )
                else:
                    if request_profiles:
                        raise FixtureError(
                            "immutable LTX 2.5 qualification profile cannot be overridden"
                        )
                    expected_profile = declared_profile
                for _, content, _ in selected:
                    _validate_ltx25_receipt(content, output_contents, expected_profile)
    return {
        "assertions": list(recipe.assertions),
        "output_files": outputs,
        "output_manifest_sha256": result.get("output_manifest_sha256"),
    }


def _parse_fraction(value: object, label: str) -> float:
    if not isinstance(value, str) or "/" not in value:
        raise FixtureError(f"{label} is invalid")
    numerator, denominator = value.split("/", 1)
    try:
        result = float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError) as error:
        raise FixtureError(f"{label} is invalid") from error
    if result <= 0:
        raise FixtureError(f"{label} is invalid")
    return result


def _assert_number_range(
    metadata: Mapping[str, object],
    assertion: Mapping[str, object],
    field: str,
    label: str,
    *,
    metadata_field: str | None = None,
) -> None:
    if f"minimum_{field}" not in assertion:
        return
    try:
        value = float(metadata[metadata_field or field])
    except (KeyError, TypeError, ValueError) as error:
        raise FixtureError(f"artifact {label} {field} is unavailable") from error
    if (
        not float(assertion[f"minimum_{field}"])
        <= value
        <= float(assertion[f"maximum_{field}"])
    ):
        raise FixtureError(f"artifact {label} {field} assertion failed")


def _parse_json_object(content: bytes, label: str) -> Mapping[str, object]:
    try:
        value = _strict_json_loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise FixtureError(f"{label} is invalid") from error
    return _object(value, label)


def _assert_required_keys(
    document: Mapping[str, object], assertion: Mapping[str, object]
) -> None:
    required = assertion.get("required_keys", [])
    if any(key not in document for key in required):
        raise FixtureError("artifact JSON required-key assertion failed")
    equals = assertion.get("equals")
    if isinstance(equals, Mapping) and any(
        document.get(key) != value for key, value in equals.items()
    ):
        raise FixtureError("artifact JSON semantic assertion failed")
