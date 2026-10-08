"""Document, realtime, and synchronized-media semantic assertions."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping

from .contracts import _DIGEST, FixtureError
from .media import _safe_zip_entries
from .output_values import _parse_json_object
from .values import _integer, _object, _string_list_value


def _validate_document_archive(content: bytes, assertion: Mapping[str, object]) -> None:
    entries = _safe_zip_entries(content)
    expected_names = assertion["exact_names"]
    if [name for name, _ in entries] != expected_names:
        raise FixtureError("document archive file-name contract failed")
    payloads = dict(entries)
    manifest = _parse_json_object(
        payloads["manifest.json"], "document archive manifest"
    )
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
        raise FixtureError("document archive manifest shape is invalid")
    expected = _object(assertion["manifest_equals"], "manifest_equals")
    if any(manifest.get(name) != value for name, value in expected.items()):
        raise FixtureError("document archive manifest authority is invalid")
    if manifest.get("sampling") != assertion["sampling_equals"]:
        raise FixtureError("document archive sampling receipt is invalid")
    documents = manifest.get("documents")
    if not isinstance(documents, list) or len(documents) != 1:
        raise FixtureError("document archive receipt is invalid")
    document = _object(documents[0], "document archive receipt")
    if (
        set(document)
        != {
            "characters",
            "early_stopped_tail_repetition",
            "input",
            "output",
        }
        or document.get("input") != assertion["input_name"]
        or document.get("output") != assertion["output_name"]
    ):
        raise FixtureError("document archive receipt shape is invalid")
    if not isinstance(document.get("early_stopped_tail_repetition"), bool):
        raise FixtureError("document archive early-stop receipt is invalid")
    try:
        markdown = payloads[str(assertion["output_name"])].decode("utf-8")
    except UnicodeDecodeError as error:
        raise FixtureError("document archive text is not UTF-8") from error
    characters = document.get("characters")
    if (
        not isinstance(characters, int)
        or isinstance(characters, bool)
        or characters != len(markdown)
        or re.search(str(assertion["text_pattern"]), markdown) is None
    ):
        raise FixtureError("document archive text semantic assertion failed")


def _validate_realtime_transcript(
    content: bytes, assertion: Mapping[str, object]
) -> None:
    expected_frame_count = _integer(
        assertion.get("frame_count", 1), "frame count", 1, 128
    )
    lines = content.splitlines()
    if not 3 <= len(lines) <= 4_096 or any(not line for line in lines):
        raise FixtureError("realtime transcript record count is invalid")
    records = [_parse_json_object(line, "realtime transcript record") for line in lines]
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
            not isinstance(record_type, str)
            or record_type not in allowed_shapes
            or set(record) != allowed_shapes[record_type]
        ):
            raise FixtureError("realtime transcript record shape is invalid")
        elapsed = record.get("elapsed_seconds")
        if (
            record.get("sequence") != sequence
            or not isinstance(elapsed, (int, float))
            or isinstance(elapsed, bool)
            or not 0 <= float(elapsed) <= 3_600
            or float(elapsed) < previous_elapsed
        ):
            raise FixtureError("realtime transcript ordering is invalid")
        previous_elapsed = float(elapsed)
        if record_type == "output" and (
            record.get("kind")
            not in {"silence", "round-start", "round-end", "response-start", "text"}
            or not isinstance(record.get("text"), str)
            or len(str(record.get("text"))) > 65_536
        ):
            raise FixtureError("realtime transcript output record is invalid")
    if (
        records[0].get("type") != "session-start"
        or records[0].get("model_revision") != assertion["model_revision"]
    ):
        raise FixtureError("realtime transcript model authority is invalid")
    frame_records = [record for record in records if record.get("type") == "frame-ack"]
    if len(frame_records) != expected_frame_count or any(
        record.get("event_index") != index
        or record.get("timestamp") != float(index)
        or not isinstance(record.get("dropped_oldest"), bool)
        for index, record in enumerate(frame_records)
    ):
        raise FixtureError("realtime transcript frame acknowledgement is invalid")
    first_frame_sequence = frame_records[0].get("sequence")
    stop_indexes = [
        index
        for index, record in enumerate(records)
        if record.get("type") == "session-stop"
    ]
    if (
        len(stop_indexes) != 1
        or not isinstance(first_frame_sequence, int)
        or stop_indexes[0] <= first_frame_sequence
        or any(
            record.get("type") != "output" for record in records[stop_indexes[0] + 1 :]
        )
    ):
        raise FixtureError("realtime transcript terminal ordering is invalid")


def _validate_synchronized_media_receipt(
    content: bytes,
    output_contents: Mapping[str, bytes],
    expected_profile: str,
    assertion: Mapping[str, object],
) -> None:
    document = _parse_json_object(content, "synchronized media receipt")
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    if content != canonical + b"\n":
        raise FixtureError("synchronized media receipt is not canonical JSON")
    if set(document) != {
        "media",
        "output_sha256",
        "profile",
        "prompt_sha256",
        "runtime",
        "seed",
        "tensors",
    }:
        raise FixtureError("synchronized media receipt top-level shape is invalid")
    output = output_contents.get(str(assertion["output_name"]))
    if (
        output is None
        or document.get("output_sha256") != hashlib.sha256(output).hexdigest()
    ):
        raise FixtureError("synchronized media receipt output digest is invalid")
    prompt_digest = document.get("prompt_sha256")
    if not isinstance(prompt_digest, str) or not _DIGEST.fullmatch(prompt_digest):
        raise FixtureError("synchronized media receipt prompt digest is invalid")
    allowed_profiles = _string_list_value(
        assertion, "allowed_profiles", "synchronized media receipt"
    )
    if document.get("profile") not in allowed_profiles:
        raise FixtureError("synchronized media receipt profile is invalid")
    if document.get("profile") != expected_profile:
        raise FixtureError(
            "synchronized media receipt does not match the requested profile"
        )
    seed = document.get("seed")
    if (
        not isinstance(seed, int)
        or isinstance(seed, bool)
        or not 0 <= seed <= 9_223_372_036_854_775_807
    ):
        raise FixtureError("synchronized media receipt seed is invalid")
    media = _object(document.get("media"), "synchronized media receipt media")
    expected_media = _object(assertion["media_equals"], "media_equals")
    positive_media_fields = _string_list_value(
        assertion, "media_positive_integers", "synchronized media receipt"
    )
    if set(media) != {*expected_media, *positive_media_fields} or any(
        media.get(key) != value for key, value in expected_media.items()
    ):
        raise FixtureError("synchronized media receipt shape is invalid")
    for name in positive_media_fields:
        value = media.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise FixtureError(
                f"synchronized media receipt {name} is not a positive integer"
            )
    runtime = _object(document.get("runtime"), "synchronized media receipt runtime")
    runtime_equals = _object(assertion["runtime_equals"], "runtime_equals")
    nullable_strings = _string_list_value(
        assertion, "runtime_nullable_strings", "synchronized media receipt"
    )
    nullable_integers = _string_list_value(
        assertion, "runtime_nullable_integers", "synchronized media receipt"
    )
    nonempty_strings = _string_list_value(
        assertion, "runtime_nonempty_strings", "synchronized media receipt"
    )
    if set(runtime) != {
        *runtime_equals,
        *nullable_strings,
        *nullable_integers,
        *nonempty_strings,
    }:
        raise FixtureError("synchronized media receipt runtime shape is invalid")
    if any(runtime.get(name) != value for name, value in runtime_equals.items()):
        raise FixtureError("synchronized media receipt runtime revision is invalid")
    for name in nullable_strings:
        value = runtime.get(name)
        if value is not None and not isinstance(value, str):
            raise FixtureError(
                f"synchronized media receipt {name} is not a nullable string"
            )
    for name in nullable_integers:
        value = runtime.get(name)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool)
        ):
            raise FixtureError(
                f"synchronized media receipt {name} is not a nullable integer"
            )
    for name in nonempty_strings:
        value = runtime.get(name)
        if not isinstance(value, str) or not value:
            raise FixtureError(
                f"synchronized media receipt {name} is not a nonempty string"
            )
    tensors = _object(document.get("tensors"), "synchronized media receipt tensors")
    tensor_shapes = _object(assertion["tensor_shapes"], "tensor_shapes")
    if set(tensors) != set(tensor_shapes):
        raise FixtureError("synchronized media receipt tensor shape is invalid")
    for name, expected_shape in tensor_shapes.items():
        tensor = _object(tensors.get(name), f"synchronized media {name} tensor")
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
            raise FixtureError(f"synchronized media {name} tensor metadata is invalid")
        if expected_shape is not None and shape != expected_shape:
            raise FixtureError(
                f"synchronized media {name} tensor shape does not match the contract"
            )
