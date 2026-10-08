"""Qualification assertion declaration validation."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import PurePosixPath

from .contracts import _FORMATS, _MEDIA_TYPE, _NAME, FixtureError
from .values import _integer, _number_range, _object, _positive_number


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
        "document-archive": {
            "kind",
            "media_type",
            "exact_names",
            "manifest_equals",
            "sampling_equals",
            "input_name",
            "output_name",
            "text_pattern",
        },
        "realtime-transcript": {
            "kind",
            "media_type",
            "model_revision",
            "frame_count",
        },
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
        "synchronized-media-receipt": {
            "kind",
            "media_type",
            "profile",
            "profile_from_request",
            "default_profile",
            "allowed_profiles",
            "output_name",
            "media_equals",
            "media_positive_integers",
            "runtime_equals",
            "runtime_nullable_strings",
            "runtime_nullable_integers",
            "runtime_nonempty_strings",
            "tensor_shapes",
        },
    }
    if (
        not isinstance(kind, str)
        or kind not in allowed_fields
        or set(assertion) - allowed_fields[kind]
    ):
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
    elif kind == "document-archive":
        names = assertion.get("exact_names")
        if (
            not isinstance(names, list)
            or len(names) < 2
            or any(
                not isinstance(name, str)
                or not _NAME.fullmatch(PurePosixPath(name).name)
                for name in names
            )
            or names[0] != "manifest.json"
            or not isinstance(assertion.get("manifest_equals"), Mapping)
            or not isinstance(assertion.get("sampling_equals"), Mapping)
            or not isinstance(assertion.get("input_name"), str)
            or not _NAME.fullmatch(str(assertion["input_name"]))
            or assertion.get("output_name") not in names
            or not isinstance(assertion.get("text_pattern"), str)
        ):
            raise FixtureError(f"recipe fixture {key} document archive is invalid")
        try:
            re.compile(str(assertion["text_pattern"]))
        except re.error as error:
            raise FixtureError(
                f"recipe fixture {key} document archive regex is invalid"
            ) from error
    elif kind == "realtime-transcript":
        if not isinstance(assertion.get("model_revision"), str) or not assertion.get(
            "model_revision"
        ):
            raise FixtureError(f"recipe fixture {key} transcript authority is invalid")
        _integer(assertion.get("frame_count", 1), "frame count", 1, 128)
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
    elif kind == "synchronized-media-receipt":
        allowed_profiles = assertion.get("allowed_profiles")
        list_fields = (
            "media_positive_integers",
            "runtime_nullable_strings",
            "runtime_nullable_integers",
            "runtime_nonempty_strings",
        )
        parsed_lists: list[list[str]] = []
        for name in list_fields:
            raw_values = assertion.get(name)
            if not isinstance(raw_values, list) or any(
                not isinstance(value, str) or not value for value in raw_values
            ):
                raise FixtureError(f"recipe fixture {key} receipt contract is invalid")
            values = [value for value in raw_values if isinstance(value, str)]
            if len(values) != len(set(values)):
                raise FixtureError(f"recipe fixture {key} receipt contract is invalid")
            parsed_lists.append(values)
        tensor_shapes = assertion.get("tensor_shapes")
        runtime_equals = assertion.get("runtime_equals")
        media_equals = assertion.get("media_equals")
        if (
            not isinstance(assertion.get("profile"), str)
            or not isinstance(assertion.get("profile_from_request"), bool)
            or not isinstance(assertion.get("default_profile"), str)
            or not isinstance(allowed_profiles, list)
            or not allowed_profiles
            or any(
                not isinstance(value, str) or not value for value in allowed_profiles
            )
            or not isinstance(assertion.get("output_name"), str)
            or not isinstance(media_equals, Mapping)
            or not isinstance(runtime_equals, Mapping)
            or len({value for values in parsed_lists for value in values})
            != sum(len(values) for values in parsed_lists)
            or any(
                value in runtime_equals
                for values in parsed_lists[1:]
                for value in values
            )
            or any(value in media_equals for value in parsed_lists[0])
            or not isinstance(tensor_shapes, Mapping)
            or not tensor_shapes
            or any(
                not isinstance(name, str)
                or not name
                or shape is not None
                and (
                    not isinstance(shape, list)
                    or not shape
                    or any(
                        not isinstance(value, int)
                        or isinstance(value, bool)
                        or value < 0
                        for value in shape
                    )
                )
                for name, shape in tensor_shapes.items()
            )
        ):
            raise FixtureError(f"recipe fixture {key} receipt contract is invalid")
    else:
        raise FixtureError(f"recipe fixture {key} assertion kind is invalid")
    return assertion
